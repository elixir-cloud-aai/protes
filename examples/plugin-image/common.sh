# Sourced by `resolve.sh` and `download.sh`: a temporary directory for mounts
# (`work`, removed on exit) with uv, from the image pinned in the `Dockerfile`
# next to the scripts. The proTES image has no uv, and images don't keep it
work="$(mktemp -d)"
container=""
cleanup() {
  rm -rf "${work}"
  if [ -n "${container}" ]; then
    docker rm --force "${container}" > /dev/null
  fi
}
trap cleanup EXIT
chmod 755 "${work}"
uv_image="$(sed -n 's/^FROM \(.*\) AS uv$/\1/p' "$(dirname "$0")/Dockerfile")"
container="$(docker create "${uv_image}")"
docker cp --quiet "${container}:/uv" "${work}/uv"
