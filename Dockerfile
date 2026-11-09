##### TOOLS #####
## uv, to export the locked requirements (`uv.lock`) for the image build, and
## to install them; mounted into the steps that use it only, so that the
## image doesn't contain it. Pinned by digest; Dependabot updates the tag and
## digest together. CI's `push` job exports the build requirements with
## this image too (`.github/workflows/ci.yml`), as the `build` stage below
## does; it finds this line with a pattern (`test_dev_version_uv_image` in
## `tests/test_workflows.py` checks its format): keep it as it is
FROM ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 AS uv

##### BASE IMAGE #####
## Pinned by digest, so that rebuilds (e.g., of a release) use the same base
## image; Dependabot updates the tag and digest together
## (`.github/dependabot.yml`)
FROM elixircloud/foca:0.22.0-py3.13@sha256:bf02db1195d60940dd3b29e53ce4757854155e258f35199e28d778cbf6128f9a AS base

##### BUILD #####
## Build the app's wheel, in a stage of its own, so that the image doesn't
## contain the build requirements. They (`build-system.requires` of
## `pyproject.toml`) are locked in `uv.lock`, as the dependency group
## `build-system`, and installed from there, as the runtime dependencies are
## below: at the exact versions of the lock, checked against its hashes, as
## wheels only, into a virtual environment of their own (created by uv for
## the Python of the base image, without pip). The wheel is then built with
## them by uv, without an isolated build environment (which would be filled
## with downloads that aren't hash-checked), offline and without network, so
## that it downloads nothing
FROM base AS build
USER root
COPY pyproject.toml uv.lock /tmp/src/
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
  export UV_NO_CACHE=1 UV_PYTHON_DOWNLOADS=never \
  && uv export --frozen --only-group build-system --no-emit-project \
    --project /tmp/src --output-file /tmp/build-requirements.txt \
  && uv venv --python /usr/local/bin/python /tmp/build-env \
  && uv pip install --python /tmp/build-env --require-hashes \
    --only-binary :all: --no-deps -r /tmp/build-requirements.txt
## (`README.md` is the package's long description; the license text is
## installed with the package metadata, e.g.,
## `site-packages/pro_tes-<version>.dist-info/licenses/LICENSE`)
## The build context has no Git metadata, so the version is passed as a build
## argument (e.g., `--build-arg SETUPTOOLS_SCM_PRETEND_VERSION=1.2.3`, as the
## workflows do); without it, the app reports version `0.0.0.dev0`
COPY README.md LICENSE /tmp/src/
COPY pro_tes /tmp/src/pro_tes
## The wheels of foca-plugins, built the same way, from the commit of
## `pyproject.toml`, as its releases don't allow this FOCA yet (the image has no
## Git, and such requirements have no hashes; `scripts/image_requirements.py`
## leaves them out, and `tests/test_image_requirements.py` checks that the
## commits match). Installed with the app's wheel below; goes with the next
## release of foca-plugins
ADD https://github.com/elixir-cloud-aai/foca-plugins.git#92e6a8231824409f3a5a0fb1e7f9671ca7509007 /tmp/foca-plugins
ARG SETUPTOOLS_SCM_PRETEND_VERSION
RUN --network=none --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
  export UV_NO_CACHE=1 UV_PYTHON_DOWNLOADS=never \
  && uv build --wheel --offline --no-build-isolation \
    --python /tmp/build-env/bin/python --out-dir /tmp/dist /tmp/src \
  && for plugin in meta plugins/generic/routing/random plugins/tes/routing/distance; do \
    uv build --wheel --offline --no-build-isolation \
      --python /tmp/build-env/bin/python --out-dir /tmp/dist \
      "/tmp/foca-plugins/${plugin}"; \
  done

##### IMAGE #####
FROM base

##### METADATA #####
## OCI annotations; images built from the FOCA image inherit its own, so all
## static ones are set here. CI's `docker/metadata-action` sets the version,
## revision and creation date and overrides title, description, URLs and
## licenses with the repository's metadata
LABEL org.opencontainers.image.title="proTES"
LABEL org.opencontainers.image.description="Proxy for GA4GH Task Execution Service (TES) instances by ELIXIR On-Cloud"
LABEL org.opencontainers.image.url="https://github.com/elixir-cloud-aai/protes"
LABEL org.opencontainers.image.source="https://github.com/elixir-cloud-aai/protes"
LABEL org.opencontainers.image.documentation="https://protes.readthedocs.io/"
LABEL org.opencontainers.image.licenses="Apache-2.0"
LABEL org.opencontainers.image.authors="ELIXIR On-Cloud <cloud-service@elixir-europe.org>"
LABEL org.opencontainers.image.vendor="ELIXIR On-Cloud"

## Install dependencies first, so that code changes do not reinstall them;
## the FOCA image runs as non-root user. Only the locked ones are installed
## (for now without foca-plugins, whose wheels come from the `build` stage):
## the runtime dependencies (`project.dependencies` of `pyproject.toml`; the
## export leaves out the extras, and `--no-dev` a `dev` dependency group) and
## all their own dependencies, at the exact versions of `uv.lock`, checked
## against its hashes, as wheels only, and without resolving anything else
## (`--no-deps`). `--frozen` takes the lock as is, unchecked against
## `pyproject.toml` (CI's `uv lock --check` checks it). A direct reference to
## a FOCA commit (temporary) has no hash and is left out, as the image has no
## Git: the build fails unless the base image has FOCA of that commit. It also
## fails unless the base image has the locked FOCA release, if one is locked
## (`scripts/image_requirements.py`). uv installs them into the Python of the
## base image (`--system`), and its lock file in `/tmp` is removed. Packages
## that the base image already has at the locked version are kept as they
## are (trusted through its digest), not checked against the hashes again
USER root
COPY pyproject.toml uv.lock scripts/image_requirements.py /tmp/src/
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
  export UV_NO_CACHE=1 UV_PYTHON_DOWNLOADS=never \
  && uv export --frozen --no-dev --no-emit-project \
    --project /tmp/src --output-file /tmp/src/locked.txt \
  && python /tmp/src/image_requirements.py /tmp/src/locked.txt > /tmp/src/requirements.txt \
  && uv pip install --system --require-hashes --only-binary :all: --no-deps \
    -r /tmp/src/requirements.txt \
  && rm -f /tmp/uv-*.lock

## Install app from the wheel of the build stage (non-editable; the base
## config is installed with the package, and deployments override it via
## `FOCA_CONFIG_OVERLAY`), without its dependencies, installed above, and
## without an index and without network, so that nothing is downloaded; then
## uv checks that the installed packages (with those of the base image) meet
## each other's requirements, as nothing was resolved
RUN --network=none --mount=from=build,source=/tmp/dist,target=/tmp/dist \
  --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
  export UV_NO_CACHE=1 UV_PYTHON_DOWNLOADS=never \
  && uv pip install --system --offline --no-index --no-deps /tmp/dist/*.whl \
  && uv pip check --system \
  && rm -rf /tmp/src /tmp/uv-*.lock

## Plugins for local builds (e.g., `--build-arg PLUGINS="<distribution>==<version>"`,
## requirement specifiers without spaces, separated by spaces; recorded in
## the image's history, so never credentials), installed with their
## dependencies from the package index, NOT pinned by hash; then uv checks
## that all dependencies are met. Empty by default, as for the published
## images; images for deployments derive from a release image instead (see
## `docs/plugin-images.md`)
ARG PLUGINS=""
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
  if [ -n "${PLUGINS}" ]; then \
    export UV_NO_CACHE=1 UV_PYTHON_DOWNLOADS=never \
    && uv pip install --system ${PLUGINS} \
    && uv pip check --system \
    && rm -f /tmp/uv-*.lock; \
  fi
USER 1000

## Probe the Swagger UI, which needs neither the database nor the message
## broker; set `HEALTHCHECK_URL` if Gunicorn's `--bind` changes the port, or
## the config changes the API base path or the Swagger UI setting
## (`api.specs.tes`). Containers of the Celery worker (same image, other
## command) need a health check of their own (see `docker-compose.yaml`)
ENV HEALTHCHECK_URL=http://localhost:8080/ga4gh/tes/v1/ui/
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen(os.environ['HEALTHCHECK_URL'], timeout=4)"]

## Serve with Gunicorn (FOCA's settings for containers, `foca.gunicorn_config`;
## override them via `GUNICORN_CMD_ARGS`, e.g., `--workers 2`); each worker
## creates the app (no preloading). The Celery worker runs
## `celery --app pro_tes.celery_worker worker`; `python -m pro_tes.app` runs
## Flask's development server instead
CMD ["gunicorn", "--config", "python:foca.gunicorn_config", \
  "pro_tes.app:init_app()"]
