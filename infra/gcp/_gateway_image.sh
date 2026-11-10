# shellcheck shell=bash
# Sourced, not run. Resolves a gateway commit sha to the image that holds it.
#
# TWO REPOSITORIES, ONE ORDER. Gateway images built on merge by PIVOTA-Agent's build-gateway-image
# workflow land in `pivota-gateway`, a repository of their own with IMMUTABLE tags, where that
# workflow's identity may write and nothing else may. Hand builds (`cloudbuild.gateway.yaml`, and every
# gateway image before the workflow existed) are in `pivota`, beside the backend image. The CI
# identity is kept out of `pivota` on purpose: `backend:latest` runs in prod as sa-worker, and
# `pivota`'s tags are mutable, so write access there would be a path to prod code execution.
#
#   resolve_gateway_image <sha>   prints the first image that exists, pivota-gateway first; exit 1 if none.
#
# Uses $GCLOUD when set (the scripts' test injection point), else `gcloud`.
GATEWAY_IMAGE_REPOS="pivota-gateway pivota"
resolve_gateway_image(){
  local tag="$1" repo image
  for repo in $GATEWAY_IMAGE_REPOS; do
    image="us-west1-docker.pkg.dev/pivota-shared/$repo/gateway:$tag"
    if "${GCLOUD:-gcloud}" artifacts docker images describe "$image" >/dev/null 2>&1; then
      printf '%s\n' "$image"
      return 0
    fi
  done
  return 1
}
