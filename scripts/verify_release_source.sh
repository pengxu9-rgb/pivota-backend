#!/usr/bin/env bash
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
repo_name="$(basename "$repo_root")"
branch="${GITHUB_REF_NAME:-$(git rev-parse --abbrev-ref HEAD)}"
sha="$(git rev-parse --short HEAD)"
event_name="${GITHUB_EVENT_NAME:-local}"

expected_repo="${EXPECTED_REPO_NAME:-}"
allowed_branch_regex="${ALLOWED_BRANCH_REGEX:-}"

if [[ -n "$expected_repo" && "$repo_name" != "$expected_repo" ]]; then
  echo "release-source-check failed: repo mismatch"
  echo "expected_repo=$expected_repo actual_repo=$repo_name repo_root=$repo_root"
  exit 1
fi

if [[ "$event_name" != "pull_request" && -n "$allowed_branch_regex" ]]; then
  if ! [[ "$branch" =~ $allowed_branch_regex ]]; then
    echo "release-source-check failed: branch not allowed for release event"
    echo "event=$event_name branch=$branch allowed_branch_regex=$allowed_branch_regex"
    exit 1
  fi
fi

echo "release-source-check ok"
echo "repo_root=$repo_root"
echo "repo_name=$repo_name"
echo "branch=$branch"
echo "sha=$sha"
echo "event_name=$event_name"
