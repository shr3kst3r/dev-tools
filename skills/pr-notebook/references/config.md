# Local configuration (`~/.dev-tools.env`)

Shared by `/pr-notebook` and `/azdo-then-notebook`. Edit here, not in either
SKILL.md.

Account-specific values — the AWS account behind the ECR registry, the
Databricks identity a single-user cluster runs as, the instance profile and
secrets ARNs the cluster needs — are **not** committed. They live in
`~/.dev-tools.env`, per-developer and outside the repo, so these skills work
against any account without the repo carrying anyone's.

`dev-tools.env.example` in the repo root is the template. Nothing in the file is
a credential — auth still comes from the `aws` and `databricks` CLI profiles —
but the values do identify an account, so it stays in `$HOME`.

## Load it before anything else

Every phase that needs one of these values starts here:

```bash
CONFIG="${DEV_TOOLS_ENV:-$HOME/.dev-tools.env}"
if [[ ! -f "$CONFIG" ]]; then
  echo "missing $CONFIG — copy dev-tools.env.example from the dev-tools repo" >&2
  exit 1
fi
set -a; . "$CONFIG"; set +a
```

Then assert exactly the keys that phase needs. **Fail loudly; never substitute a
default.** A missing account id that silently falls back to the caller's ambient
AWS profile resolves a digest from the wrong registry and the run looks fine:

```bash
: "${AWS_ACCOUNT_ID:?set AWS_ACCOUNT_ID in $CONFIG}"
: "${AWS_REGION:?set AWS_REGION in $CONFIG}"
: "${AWS_ECR_PROFILE:?set AWS_ECR_PROFILE in $CONFIG}"
ECR_REGISTRY="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
```

## Keys

| Key | Needed by | What it is |
|---|---|---|
| `AWS_ACCOUNT_ID` | `ecr-digest.md` | Account owning the ECR registry |
| `AWS_REGION` | `ecr-digest.md` | Registry region |
| `AWS_ECR_PROFILE` | `ecr-digest.md` | `aws` CLI profile with ECR read access |
| `DATABRICKS_PROFILE` | both | Default `databricks` CLI profile when `--profile` is absent |
| `DATABRICKS_SINGLE_USER` | `cluster-spec.md` | Workspace login the `SINGLE_USER` cluster runs as |
| `DATABRICKS_INSTANCE_PROFILE_ARN` | `cluster-spec.md` | Instance profile the cluster assumes |
| `DATABRICKS_SECRETS_ARN` | `cluster-spec.md` | Exported to the cluster as `$SECRETS_ARN` |
| `DATABRICKS_CLUSTER_PREFIX` | `cluster-spec.md` | Leading segments of the inline cluster's name |

## Rules

- **Never echo a resolved ARN, account id, or registry host into a Slack summary,
  a PR comment, or a commit message.** Report the repo, the tag, and the short
  digest — those identify the image without publishing the account.
- **Never write a resolved value back into this repo.** If a value turns out to be
  wrong, fix `~/.dev-tools.env`; the repo only ever holds key names and the
  placeholder example.
- **If the file is missing, stop and say how to create it.** Do not prompt for the
  values interactively and do not guess them from the caller's environment.
