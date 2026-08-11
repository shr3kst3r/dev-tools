# Resolving the PR image's immutable digest

Shared by `/pr-notebook` and `/azdo-then-notebook`. Edit here, not in either
SKILL.md.

## Why a digest and not a tag

The `pr-<branch>` tag in ECR is **mutable** — every push to the branch moves it.
Databricks workers cache by image reference, so a cluster asked for
`…/<repo>:pr-my-branch` will sometimes start with the image that tag pointed at
an hour ago. Pinning `…/<repo>@sha256:…` makes that impossible. Both skills
exist to make the notebook run against *the code in this PR*; using the tag form
defeats the entire point.

## Account facts

The account-specific values come from `~/.dev-tools.env` — see `config.md`, and
load it before the steps below.

| Fact | Source |
|---|---|
| AWS account | `$AWS_ACCOUNT_ID` |
| Region | `$AWS_REGION` |
| Registry host | `$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com` |
| AWS profile | `$AWS_ECR_PROFILE` |
| ECR repo name | **equals** the GitHub repo name — read it off the PR, never hardcode it |

## Steps

```bash
CONFIG="${DEV_TOOLS_ENV:-$HOME/.dev-tools.env}"
set -a; . "$CONFIG"; set +a
: "${AWS_ACCOUNT_ID:?set AWS_ACCOUNT_ID in $CONFIG}"
: "${AWS_REGION:?set AWS_REGION in $CONFIG}"
: "${AWS_ECR_PROFILE:?set AWS_ECR_PROFILE in $CONFIG}"
ECR_REGISTRY="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

PR_JSON=$(gh pr view --json number,headRefName,baseRefName,url,title,headRepository)
REPO_NAME=$(jq -r '.headRepository.name' <<<"$PR_JSON")
PR_NUMBER=$(jq -r '.number'              <<<"$PR_JSON")
BRANCH=$(jq -r '.headRefName'            <<<"$PR_JSON")
PR_URL=$(jq -r '.url'                    <<<"$PR_JSON")
PR_TITLE=$(jq -r '.title'                <<<"$PR_JSON")

# Tag slug: lowercase, `/` → `_`, every other non-alnum → `-`.
#   proj-1444-populate-bronze-table  → pr-proj-1444-populate-bronze-table
#   alice/proj-1424-foo              → pr-alice_proj-1424-foo
SLUG=$(printf '%s' "$BRANCH" | tr '[:upper:]' '[:lower:]' | sed 's|/|_|g; s|[^a-z0-9_-]|-|g')
IMAGE_TAG="pr-$SLUG"

ECR=$(aws ecr describe-images \
  --repository-name "$REPO_NAME" \
  --image-ids imageTag="$IMAGE_TAG" \
  --query 'imageDetails[0].{digest:imageDigest,pushedAt:imagePushedAt}' \
  --output json --profile "$AWS_ECR_PROFILE" 2>&1) || true

DIGEST=$(jq -r '.digest // empty'   <<<"$ECR")
PUSHED_AT=$(jq -r '.pushedAt // empty' <<<"$ECR")
IMAGE_URL="${ECR_REGISTRY}/${REPO_NAME}@${DIGEST}"
```

Surface `repo`, `tag → short digest`, and `pushedAt` to the user before anything
spins up, so they can sanity-check the pin. The short digest is enough — do not
print `$IMAGE_URL` or `$ECR_REGISTRY` into a summary that leaves the terminal.

## Two hard stops

**1. No such image.** If `$DIGEST` is empty or the call returned
`ImageNotFoundException`, stop. Do **not** fall back to `:prod`, `:latest`, or the
base branch's image — a silent fallback produces a run that looks like it tested
the PR and did not. Show what was tried and how to list the real tags:

```
No image in ECR for repo=$REPO_NAME tag=$IMAGE_TAG (branch $BRANCH).
Likely: the build has not published yet, or the branch→tag transform differs.
  aws ecr describe-images --repository-name $REPO_NAME \
    --profile "$AWS_ECR_PROFILE" \
    | jq -r '.imageDetails[].imageTags[]?' | grep -i '^pr-'
```

**2. Stale image.** Compare `imagePushedAt` against the head commit's timestamp
(`gh pr view --json headRefOid` → `git show -s --format=%cI <sha>`), or against
the azdo build start time when `/azdo-then-notebook` drove the build. If the
image predates the code, the publish step did not run for this commit — the
digest is real but wrong. Surface it and stop rather than pinning it.
