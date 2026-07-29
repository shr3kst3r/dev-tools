# Resolving the PR image's immutable digest

Shared by `/pr-notebook` and `/azdo-then-notebook`. Edit here, not in either
SKILL.md.

## Why a digest and not a tag

The `pr-<branch>` tag in ECR is **mutable** — every push to the branch moves it.
Databricks workers cache by image reference, so a cluster asked for
`…/etl-service:pr-my-branch` will sometimes start with the image that tag
pointed at an hour ago. Pinning `…/etl-service@sha256:…` makes that impossible.
Both skills exist to make the notebook run against *the code in this PR*; using
the tag form defeats the entire point.

## Account facts

| Fact | Value |
|---|---|
| AWS account | `000000000000` |
| Region | `us-east-1` |
| Registry host | `000000000000.dkr.ecr.us-east-1.amazonaws.com` |
| AWS profile | `Administrator-000000000000` |
| ECR repo name | **equals** the GitHub repo name (`etl-service`, `report-service`, …) |

## Steps

```bash
PR_JSON=$(gh pr view --json number,headRefName,baseRefName,url,title,headRepository)
REPO_NAME=$(jq -r '.headRepository.name' <<<"$PR_JSON")
PR_NUMBER=$(jq -r '.number'              <<<"$PR_JSON")
BRANCH=$(jq -r '.headRefName'            <<<"$PR_JSON")
PR_URL=$(jq -r '.url'                    <<<"$PR_JSON")
PR_TITLE=$(jq -r '.title'                <<<"$PR_JSON")

# Tag slug: lowercase, `/` → `_`, every other non-alnum → `-`.
#   plat-1444-populate-bronze-table  → pr-plat-1444-populate-bronze-table
#   dennisrowe/plat-1424-foo         → pr-dennisrowe_plat-1424-foo
SLUG=$(printf '%s' "$BRANCH" | tr '[:upper:]' '[:lower:]' | sed 's|/|_|g; s|[^a-z0-9_-]|-|g')
IMAGE_TAG="pr-$SLUG"

ECR=$(aws ecr describe-images \
  --repository-name "$REPO_NAME" \
  --image-ids imageTag="$IMAGE_TAG" \
  --query 'imageDetails[0].{digest:imageDigest,pushedAt:imagePushedAt}' \
  --output json --profile Administrator-000000000000 2>&1) || true

DIGEST=$(jq -r '.digest // empty'   <<<"$ECR")
PUSHED_AT=$(jq -r '.pushedAt // empty' <<<"$ECR")
IMAGE_URL="000000000000.dkr.ecr.us-east-1.amazonaws.com/${REPO_NAME}@${DIGEST}"
```

Surface `repo`, `tag → short digest`, and `pushedAt` to the user before anything
spins up, so they can sanity-check the pin.

## Two hard stops

**1. No such image.** If `$DIGEST` is empty or the call returned
`ImageNotFoundException`, stop. Do **not** fall back to `:prod`, `:latest`, or the
base branch's image — a silent fallback produces a run that looks like it tested
the PR and did not. Show what was tried and how to list the real tags:

```
No image in ECR for repo=$REPO_NAME tag=$IMAGE_TAG (branch $BRANCH).
Likely: the build has not published yet, or the branch→tag transform differs.
  aws ecr describe-images --repository-name $REPO_NAME \
    --profile Administrator-000000000000 \
    | jq -r '.imageDetails[].imageTags[]?' | grep -i '^pr-'
```

**2. Stale image.** Compare `imagePushedAt` against the head commit's timestamp
(`gh pr view --json headRefOid` → `git show -s --format=%cI <sha>`), or against
the azdo build start time when `/azdo-then-notebook` drove the build. If the
image predates the code, the publish step did not run for this commit — the
digest is real but wrong. Surface it and stop rather than pinning it.
