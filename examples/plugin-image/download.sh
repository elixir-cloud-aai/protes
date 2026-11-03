#!/usr/bin/env bash
# Download the wheels of `<plugins.txt>` for the derived-image recipe (see
# `docs/plugin-images.md`) into `<wheels>`, inside `<image>` (the proTES release
# image), for its Python version and platform. The image has no installer, so
# uv, mounted from the image pinned in this directory's `Dockerfile`, runs pip
# (of this version, which isn't checked by hash, unlike what it downloads) for
# this one command; pip checks the hashes of `<plugins.txt>`. Further
# arguments are options of `docker run` (e.g., `--env
# PIP_INDEX_URL=<index>`). Needs Docker.
# Usage: download.sh <image> <plugins.txt> <wheels> [<docker run option>...]
set -euo pipefail

if [ "$#" -lt 3 ]; then
  echo "Usage: $0 <image> <plugins.txt> <wheels> [<docker run option>...]" >&2
  exit 2
fi
image="$1" requirements="$2" wheels="$3"
shift 3
mkdir -p "${wheels}"

# shellcheck source=examples/plugin-image/common.sh
source "$(dirname "$0")/common.sh"
cp "${requirements}" "${work}/plugins.txt"
chmod -R a+rX "${work}"

docker run --rm "$@" --user "$(id -u):$(id -g)" --env HOME=/tmp \
  --env UV_NO_CACHE=1 --env UV_PYTHON_DOWNLOADS=never \
  --volume "${work}:/work:ro" --volume "${work}/uv:/usr/local/bin/uv:ro" \
  --volume "$(realpath "${wheels}"):/wheels" --entrypoint uv "${image}" \
  tool run --from "pip==26.3" pip download --no-cache-dir --no-deps \
  --only-binary :all: --require-hashes --requirement /work/plugins.txt \
  --dest /wheels
