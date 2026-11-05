# check=skip=InvalidDefaultArgInFrom
## (The base image has no default: builds name the release image explicitly)
## Image with plugins, derived from a proTES release image (see
## `docs/plugin-images.md`). The build context holds `plugins.txt`, the
## distributions to install, each pinned with its hash, and `wheels/`, their
## files, downloaded and verified before the build; the build installs these
## files only (no index, no other dependencies), as root, then checks that
## the dependencies of all installed distributions are met. Build it with
## the release image, by digest:
##   docker build --file Dockerfile \
##     --build-arg PROTES_IMAGE=docker.io/elixircloud/protes:<version>@sha256:<digest> \
##     --tag <registry>/protes-plugins:<tag> <context>
## The smoke test builds it in CI with a test plugin (`tests/smoke/plugins.sh`)
ARG PROTES_IMAGE
FROM ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 AS uv

FROM ${PROTES_IMAGE}

USER root
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
  --mount=type=bind,source=plugins.txt,target=/tmp/plugins/plugins.txt \
  --mount=type=bind,source=wheels,target=/tmp/plugins/wheels \
  export UV_NO_CACHE=1 UV_PYTHON_DOWNLOADS=never \
  && uv pip install --system --no-index --no-deps --require-hashes \
    --only-binary :all: --compile-bytecode --find-links /tmp/plugins/wheels \
    --requirement /tmp/plugins/plugins.txt \
  && uv pip check --system \
  && rm -f /tmp/uv-*.lock
USER 1000
