# shellcheck shell=bash
# Sourced, not run. Resolves a gateway commit sha to the image that holds it.
#
# TWO REPOSITORIES, `pivota` FIRST.
#   pivota          hand builds (`cloudbuild.gateway.yaml`), and every gateway image built before the
#                   build-on-merge workflow existed. Mutable tags, writable by humans and by the
#                   backend's deploy identity.
#   pivota-gateway  images built on merge by PIVOTA-Agent's build-gateway-image workflow. Immutable
#                   tags. Its CI identity can write here and nowhere else; the backend's deploy
#                   identity and humans with project-level roles can also write here.
# `pivota` is checked first ON PURPOSE (delta review of be#2779, D-P2-1). Immutability protects only a
# tag's FIRST push, and every sha built so far exists only in `pivota`, so its tag is still free in
# `pivota-gateway`. If `pivota-gateway` won, one push of `gateway:<an existing sha>` there would become
# what every later deploy, rollback and re-image of that sha pulls. With `pivota` first, a sha that has
# a hand build keeps resolving to exactly today's image, and only a sha that exists nowhere but
# `pivota-gateway` (built by CI) resolves there.
#
# ONLY "NOT FOUND" COUNTS AS ABSENT. Any other failure (auth, quota, network) is an error, never a
# reason to try the next repository: a PERMISSION_DENIED must not silently pick a different image.
# And `pivota-gateway` is consulted only for a full 40-hex sha, so a name like `latest` that someone
# managed to create there can never be picked.
#
#   resolve_gateway_image <tag>
#     exit 0  prints the image (the first repository that holds the tag)
#     exit 1  neither repository holds it
#     exit 2  a lookup failed for a reason other than "not found" (message on stderr)
#
# Uses $GCLOUD when set (the scripts' test injection point), else `gcloud`.
GATEWAY_IMAGE_REPOS="pivota pivota-gateway"
resolve_gateway_image(){
  local tag="$1" repo image err rc
  for repo in $GATEWAY_IMAGE_REPOS; do
    if [ "$repo" = pivota-gateway ] && ! [[ "$tag" =~ ^[0-9a-f]{40}$ ]]; then
      continue
    fi
    image="us-west1-docker.pkg.dev/pivota-shared/$repo/gateway:$tag"
    rc=0
    err="$("${GCLOUD:-gcloud}" artifacts docker images describe "$image" 2>&1 >/dev/null)" || rc=$?
    if [ "$rc" = 0 ]; then
      printf '%s\n' "$image"
      return 0
    fi
    case "$err" in
      *NOT_FOUND*|*"not found"*|*"Not found"*) ;;  # absent here; try the next repository
      *) printf 'gateway image lookup failed for %s (not a "not found"; refusing to fall through):\n%s\n' \
           "$image" "$err" >&2
         return 2 ;;
    esac
  done
  return 1
}
