#!/usr/bin/env bash
# Restore only a successful run of this repository's default-branch collector.
set -euo pipefail

: "${GITHUB_REPOSITORY:?}"
: "${DEFAULT_BRANCH:?}"
: "${GH_TOKEN:?}"

run_ids=$(gh api --method GET "repos/$GITHUB_REPOSITORY/actions/workflows/refresh-prices.yml/runs" \
  -f branch="$DEFAULT_BRANCH" -f status=success -f per_page=10 --jq '.workflow_runs[].id')
while IFS= read -r run_id; do
  [[ "$run_id" =~ ^[0-9]+$ ]] || continue
  artifact_id=$(gh api "repos/$GITHUB_REPOSITORY/actions/runs/$run_id/artifacts" \
    --jq '.artifacts[] | select(.name == "latest-steam-price-shards" and .expired == false) | .id')
  [[ "$artifact_id" =~ ^[0-9]+$ ]] || continue
  snapshot_dir=$(mktemp -d)
  gh run download "$run_id" --repo "$GITHUB_REPOSITORY" \
    --name latest-steam-price-shards --dir "$snapshot_dir"
  python scripts/restore_prices.py --source "$snapshot_dir"
  echo "Restored price observations from successful run $run_id."
  exit 0
done <<< "$run_ids"
echo 'No earlier successful snapshot exists yet; using the Git baseline.'
