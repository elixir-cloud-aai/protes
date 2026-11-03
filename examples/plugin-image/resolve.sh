#!/usr/bin/env bash
# Pin plugins for the derived-image recipe (see `docs/plugin-images.md`):
# resolves the requirements of `<plugins.in>` inside `<image>` (the proTES
# release image) with uv, which the image doesn't have and which is mounted
# from the image pinned in this directory's `Dockerfile`, with the image's
# installed packages as constraints, and writes the distributions that the
# image lacks, with their exact versions and the hashes of their wheels, to
# `<plugins.txt>`. Fails, removing it, if uv can't resolve them (e.g., a plugin
# needs other versions of the image's packages). Further arguments are options
# of `docker run` (e.g., `--env UV_DEFAULT_INDEX=<index>`). Needs Docker.
# Usage: resolve.sh <image> <plugins.in> <plugins.txt> [<docker run option>...]
set -euo pipefail

if [ "$#" -lt 3 ]; then
  echo "Usage: $0 <image> <plugins.in> <plugins.txt> [<docker run option>...]" >&2
  exit 2
fi
image="$1" requirements="$2" output="$3"
shift 3
# No stale pins of an earlier run if this one fails
rm -f "${output}"

# shellcheck source=examples/plugin-image/common.sh
source "$(dirname "$0")/common.sh"
cp "${requirements}" "${work}/plugins.in"
chmod -R a+rX "${work}"

docker run --rm --entrypoint python "${image}" -c '
import importlib.metadata as metadata, re
for dist in metadata.distributions():
    name = re.sub(r"[-_.]+", "-", dist.metadata["Name"]).lower()
    print(f"{name}=={dist.version}")
' > "${work}/image.txt"
compiled="$(docker run --rm "$@" --env UV_NO_CACHE=1 \
  --env UV_PYTHON_DOWNLOADS=never --volume "${work}:/work:ro" \
  --volume "${work}/uv:/usr/local/bin/uv:ro" --entrypoint uv "${image}" \
  pip compile --quiet --system --generate-hashes --only-binary :all: \
  --strip-extras --no-header --no-annotate --constraint /work/image.txt \
  /work/plugins.in)"

# One line per distribution, leaving out those of the image
printf '%s\n' "${compiled}" | awk -F '==' '
  NR == FNR { image[$1]; next }
  /^[^ ]/ { if (line != "" && !(name in image)) print line; name = $1; line = "" }
  { sub(/^ +/, ""); sub(/ *\\$/, ""); line = (line == "" ? $0 : line " " $0) }
  END { if (line != "" && !(name in image)) print line }
' "${work}/image.txt" - > "${output}"
