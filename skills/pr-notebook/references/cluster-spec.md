# The inline `new_cluster` spec

Shared by `/pr-notebook` and `/azdo-then-notebook`. Edit here, not in either
SKILL.md.

Submitting with `new_cluster` (rather than `existing_cluster_id`) creates the
cluster when the run starts and **tears it down when the run ends** — the closest
thing Databricks offers to "run this notebook as a job". Nothing is left idle
afterwards.

The spec below was read off the live `ci:multi:prod:etl-service:vendor2` cluster,
which is the freshest hand-tuned template in the account.

## Submit

```bash
databricks jobs submit --profile "$PROFILE" --json "$(cat <<EOF
{
  "run_name": "${SKILL_NAME} pr-${PR_NUMBER}: ${NOTEBOOK_BASENAME}",
  "notebook_task": {"notebook_path": "${NOTEBOOK_PATH}"},
  "new_cluster": {
    "cluster_name": "ci:multi:prod:${REPO_NAME}:${RUN_SLUG}",
    "spark_version": "14.3.x-scala2.12",
    "node_type_id": "${NODE_TYPE:-i3.8xlarge}",
    "driver_node_type_id": "i3.xlarge",
    "num_workers": ${WORKERS:-3},
    "autotermination_minutes": 30,
    "data_security_mode": "SINGLE_USER",
    "single_user_name": "you@example.com",
    "runtime_engine": "STANDARD",
    "enable_elastic_disk": false,
    "enable_local_disk_encryption": false,
    "aws_attributes": {
      "availability": "SPOT_WITH_FALLBACK",
      "first_on_demand": 3,
      "instance_profile_arn": "arn:aws:iam::000000000000:instance-profile/my-instance-profile",
      "spot_bid_price_percent": 100,
      "zone_id": "auto"
    },
    "custom_tags": {
      "cost-center": "platform",
      "data-type": "personal",
      "env": "prod",
      "team": "data"
    },
    "spark_conf": {
      "spark.databricks.cloudFiles.schemaInference.sampleSize.numFiles": "10000",
      "spark.driver.maxResultSize": "20g",
      "spark.rpc.message.maxSize": "1024"
    },
    "spark_env_vars": {
      "AWS_DEFAULT_REGION": "us-east-1",
      "RUNTIME": "databricks",
      "SECRETS_ARN": "arn:aws:secretsmanager:us-east-1:000000000000:secret:MY_SECRET-abc123"
    },
    "docker_image": {"url": "${IMAGE_URL}"}
  }
}
EOF
)"
```

`RUN_SLUG` is `pr-<N>` from `/pr-notebook` and `azdo-pr-<N>` from
`/azdo-then-notebook`, so the cluster name says which path created it.
Capture `run_id` from the response.

## Notes that look like bugs but are not

- **`SECRETS_ARN` points at `MY_SECRET` for both `etl-service` and
  `report-service`.** Verified against the live clusters. Do not "correct" it to
  `MY_OTHER_SECRET` without checking with Dennis first.
- **`instance_profile_arn` is `my-instance-profile`** — the standard for
  `:prod` clusters in this account.
- **`docker_image.url` must be the `@sha256:…` form.** See `ecr-digest.md`. A
  `:tag` here silently reintroduces the caching bug both skills exist to avoid.
- **Single-node/SQL-only work needs no image at all** (`ci:single:sql` has none),
  but that is not what these skills do.

## Existing-cluster override (`--cluster <id>`)

Skips the digest lookup entirely and runs on a cluster that already exists:

```bash
databricks jobs submit --profile "$PROFILE" --json "$(cat <<EOF
{
  "run_name": "${SKILL_NAME} pr-${PR_NUMBER}: ${NOTEBOOK_BASENAME}",
  "existing_cluster_id": "${CLUSTER_ID}",
  "notebook_task": {"notebook_path": "${NOTEBOOK_PATH}"}
}
EOF
)"
```

Use it when you want the cluster to stick around afterwards. You are trusting
whatever image that cluster already booted with — **say so in the summary**, or
the run reads as verified against the PR when it was not.

## Watch and triage

`dbtools` (the `databricks-tools` skill) blocks until terminal and mirrors the
run result in its exit code:

```bash
dbtools follow-run "$RUN_ID" --plain -p "$PROFILE"; echo "exit=$?"
dbtools notebook pull "$NOTEBOOK_PATH" --out "/tmp/${RUN_SLUG}.ipynb" --json -p "$PROFILE"
dbtools notebook triage "/tmp/${RUN_SLUG}.ipynb" --json   # exit 1 = findings exist
dbtools runs show "$RUN_ID" --json -p "$PROFILE"          # run-metadata anomalies
```

`triage` covers tracebacks and explicit error outputs, suspicious warnings
(Future/Deprecation/User/Runtime/Performance), and empty results ("Showing 0 of
0", "Empty DataFrame"). `runs show` flags `result_state != SUCCESS`, retries, cold
start, and slow cleanup.

Cap the watch at **4 hours**, then ask whether to keep waiting. Export the
notebook **once**, after terminal state — not on every poll.
