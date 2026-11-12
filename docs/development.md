# Development

See [Contributing](contributing/index.md) for how to contribute; this page
describes the development setup, the checks, the documentation and the
versions.

Development needs Python 3.13 or later and
[uv](https://docs.astral.sh/uv/) (e.g., `pipx install uv`; see its
[installation guide](https://docs.astral.sh/uv/getting-started/installation/)).
To set up a development environment with
the versions of the lock (`uv.lock`, see [Dependencies](#dependencies)), as
CI does, and run the checks that CI runs:

```bash
uv sync --locked --extra dev  # the app, with the dev dependencies, in `.venv`
source .venv/bin/activate  # or `activate.fish` etc.
pre-commit install  # run the checks on every commit
pre-commit run --all-files  # ruff, mypy, semgrep, ast-grep, actionlint
pre-commit run --all-files --hook-stage manual gitleaks-all  # secrets
coverage run -m pytest  # the tests, with coverage
coverage report  # fails below 100%
```

Instead of activating `.venv`, commands can be run with `uv run` (e.g.,
`uv run pytest`). `pip install -e ".[dev]"` also works, but installs the
newest versions in the ranges of `pyproject.toml` instead of the locked
ones, so its results can differ from CI's.

Coverage of `pro_tes` and `scripts` is kept at 100%, which is enforced:
`coverage report` (and CI) fails below 100% (`fail_under` in
`pyproject.toml`), and so do Codecov's checks of the whole code and of the
changes of a pull request (`codecov.yml`). Add tests that fail without your
change.

The tests in `tests/` mirror the package (`pro_tes/`). They need neither a
database nor TES backends: they use an in-memory database
([mongomock](https://github.com/mongomock/mongomock)) and fake TES backends,
and have no network access (fixture `no_network` in `tests/conftest.py`
blocks all connections except to the test database, and fails tests that try
with `Network access blocked in tests`). CI also runs them against MongoDB
7.0; to do so locally, set `MONGODB_TEST_URI` to a standalone MongoDB (not a
replica set) without other data, one per test run (the tests drop its
databases `taskStore` and `protes` before each test, and refuse to start if
one of them exists), e.g.:

```bash
docker run -d --rm --name mongodb-test -p 127.0.0.1:27017:27017 mongo:7.0
until docker exec mongodb-test mongosh --quiet --eval 'db.adminCommand(ping).ok'; do sleep 1; done
MONGODB_TEST_URI=mongodb://localhost:27017 pytest
docker stop mongodb-test
```

CI also runs the app with Docker Compose, with a fake TES backend instead of
the TES instances of the config (`tests/smoke/`), which is also the identity
provider (it issues access tokens at
`http://fake-tes:8080/oidc/token?sub=<user>`, inside the Compose network),
and checks that tasks are relayed to it and tracked until they are complete,
and access control. To run it locally (with curl and jq):

```bash
export COMPOSE_FILE=docker-compose.yaml:tests/smoke/compose.yaml
docker compose up -d --build --wait
tests/smoke/check.sh
tests/smoke/plugins.sh  # the derived-image recipe for plugins
docker compose down
```

`tests/smoke/plugins.sh` builds the image of the [recipe for
plugins](plugin-images.md) from the app's image, with a test plugin
(`tests/smoke/plugin/`, built with `python3 -m pip wheel`), restarts proTES
and the Celery worker with it, and checks that the plugin tags new tasks.

CI's `Integration` job runs the app against real TES implementations in Docker
Compose (`tests/integration/`): it creates, tracks and cancels tasks on Funnel
0.12.3 and on TESP-API 0.2.0 with Pulsar 0.14.16, in images pinned by digest
(see [Notes on TES
implementations](configuration.md#notes-on-tes-implementations)), with
`tests/integration/check.sh`. The backends' containers run through the Docker
socket of the host, and the images come from
third parties' registries, so the gate doesn't need this job until it has
proven reliable. Access to the Docker socket amounts to control of the host, so
run the stack locally only on a disposable machine or VM.

`tests/integration/versions.sh` also checks the paths of [TES
versions](tes-versions.md) on the same backends, with two tasks:

- A task with fields of TES 1.1 that can be dropped: Funnel gets it in TES 1.1
  (by its label `tes.version`), with these fields; TESP-API gets it in TES
  1.0, without them. Clients get the task back as they sent it.
- A task with `ignore_error: true` on a failing executor: Funnel gets it,
  runs the next executor and ends the task in `COMPLETE`, as TES 1.1 has it;
  proTES rejects it for TESP-API (`400`).

The script's last argument is the TES version that proTES must find for the
backend; `tests/test_workflows.py` checks that it matches each backend of
the job's config. In this job, it runs before `check.sh`, as the
canceled tasks of `check.sh` keep running on TESP-API with Pulsar for
minutes, and later tasks there wait for them.

Both scripts source their helpers from `tests/integration/lib.sh`: requests
to proTES (with the token on curl's standard input), waits for a state, and
calls to the backend from the Compose network. Every request of these
helpers and every call to the backend has a limit of 60 seconds (for the
whole Compose call where `timeout` of GNU coreutils exists), and a wait for
a state polls up to `FINAL_WAIT` times (default 180, about one poll a
second), so that a stuck backend fails the job instead of stalling it. The
scripts of `tests/integration/tesk/` call Compose without these limits.

To run the job locally (with curl and jq), follow its steps in
`.github/workflows/ci.yml`, including the pin of TESP-API's stage image:

```bash
export COMPOSE_FILE=docker-compose.yaml:tests/integration/compose.yaml
export SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0+compose
docker compose up -d --build --wait --wait-timeout 300
# Pin the stage image here, as in the job's step
API=http://localhost:8080/ga4gh/tes/v1
tests/integration/versions.sh "${API}" http://funnel:8000 "" 1.1
tests/integration/check.sh "${API}" http://funnel:8000 ""
tests/integration/versions.sh "${API}" http://tesp-api:8080 /v1 1.0
tests/integration/check.sh "${API}" http://tesp-api:8080 /v1
docker compose down
```

CI's `Integration (TESK)` job, which the gate doesn't need either, runs the same
checks against [TESK](https://github.com/elixir-cloud-aai/TESK) (in TES 1.0),
from its chart in a throwaway [k3s](https://k3s.io/) cluster in a privileged
container (`tests/integration/tesk/`). The chart comes from the commit of TESK's repository
that `install.sh` pins (or `TESK_COMMIT`), the images from `values.yaml`. As for
the other backends, the check uses inputs with `content` only, and TESK has
neither FTP nor S3 there. `COMBINED_LOGS=true` tells the check that TESK writes
both streams of an executor to `stdout`. To run it locally (with curl, jq, git
and Helm):

```bash
export COMPOSE_FILE=docker-compose.yaml:tests/integration/compose.yaml:tests/integration/tesk/compose.yaml
export SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0+compose
docker compose up -d --wait k3s
tests/integration/tesk/install.sh
docker compose up -d --build --wait protes protes-worker fake-tes
COMBINED_LOGS=true tests/integration/check.sh \
  http://localhost:8080/ga4gh/tes/v1 http://k3s:31567 /ga4gh/tes/v1
tests/integration/tesk/stopped.sh
tests/integration/versions.sh \
  http://localhost:8080/ga4gh/tes/v1 http://k3s:31567 /ga4gh/tes/v1 1.0
docker compose down --volumes
```

To try changes in the running app, rebuild it with
`docker compose up --build -d`. `python -m pro_tes.app` runs the app with
Flask's development server instead of Gunicorn (see
[Server](configuration.md#server)); it needs a MongoDB at `db.host` and a
RabbitMQ at `jobs.host` of the config, and a config overlay with the
security settings (e.g., `FOCA_CONFIG_OVERLAY=config.dev.yaml`; see [Access
control](access-control.md#deployment-settings)), as do the Celery workers.

Run `pre-commit` in the environment with the dev dependencies: the mypy and
ast-grep hooks use the installed packages. The first run sets up the other
hooks, which takes a few minutes.

The checks are configured in
[`.pre-commit-config.yaml`](https://github.com/elixir-cloud-aai/protes/blob/dev/.pre-commit-config.yaml)
and
[`pyproject.toml`](https://github.com/elixir-cloud-aai/protes/blob/dev/pyproject.toml).
Project-specific rules are in `.semgrep/` (security) and `.ast-grep/`
(anti-patterns such as HTTP status codes as strings). The ast-grep rule
`framework-free-tasks` forbids imports of Flask, Werkzeug, Connexion and
Starlette in `pro_tes/`, except in `app.py`, `exceptions.py` and the
controllers (`pro_tes/ga4gh/tes/server.py`): the rest gets plain data (the
task, the request path, and the inbound request and caller that
`foca.plugins.adapter` builds) from the controllers, so that the move to
Connexion 3 without Flask (FOCA 1.0) changes only the controller glue. They still raise the exceptions
of `pro_tes.exceptions`, which derive from Connexion's and Werkzeug's, so
those change with that move.
[actionlint](https://github.com/rhysd/actionlint) checks the workflows in
`.github/workflows/`, and their shell scripts with
[ShellCheck](https://www.shellcheck.net/) if it is installed (as on
GitHub-hosted runners, so CI may find more than a local run without it). To
suppress a false positive, add a comment with the reason to the line:
`# noqa: <code>` (ruff), `# type: ignore[<code>]` (mypy),
`# nosemgrep: <rule-id>`, `# ast-grep-ignore: <rule-id>` or
`# gitleaks:allow`.

The gate of the CI workflow (`passed`, "CI passed", the only required
check) uses the shared `gate` action of
[elixir-cloud-aai/.github](https://github.com/elixir-cloud-aai/.github), and
the image scans use its `scan-image` action (see [Versioning and
releases](#versioning-and-releases)); no workflow uses a local action.
Shared actions are pinned to the full commit SHA of a release, with the
release in a comment (`@<sha> # vX.Y.Z`); all pins name the same release, so
bump them together (Dependabot does), as `tests/test_workflows.py` checks.

## Testing a deployment

The checks of a deployed instance, e.g., from the chart on CSC's Rahti with
real TES instances, are scripts too. CI can't run them against a
deployment, as they need the instance and access tokens of its users. Run
them from a clone of the repository, with curl and jq, after each install
or upgrade:

- `tests/smoke/deployment.sh` checks that the API is served over HTTPS only
  (plain HTTP is refused or redirected to HTTPS, as by the chart's OpenShift
  Route), that the service info is public, and access control: no token
  and an invalid token get `401`, a user with the role `user` lists tasks
  and plugins, unknown tasks are forbidden, and, with `OTHER_TOKEN`, a user
  without a role gets `403`. It creates no tasks. The smoke test runs its
  access checks against the Compose stack, over plain HTTP
  (`ALLOW_HTTP=true`); the HTTPS checks run only against deployments.
- `tests/integration/check.sh`, the check of CI's integration jobs, runs
  against the instance when `TOKEN` is set: per TES instance, it creates
  tasks that finish, fail and are canceled, and checks their states and
  logs. Pass the TES instance's URL and API path as in the config
  (`tes.backends`), as the tasks are sent there with the tag
  `gateway.backends`. Without the Compose stack, the script can't ask the
  TES instance, so it takes the states that proTES reports, and doesn't
  check whether the TES instance stopped a canceled task. The tasks run a
  pinned Alpine image; canceled tasks may run for up to 5 minutes, as
  TESP-API leaves their containers running.
- Set `COMBINED_LOGS=true` for TESK (as CI does), and `EMPTY_LOGS=true` for
  TESP-API with Pulsar 0.15 or later, which returns empty logs (see [Notes
  on TES implementations](configuration.md#notes-on-tes-implementations)).

For example, in Bash, with the instance's values (get the tokens from your
identity provider; `read` keeps them out of the shell history):

```bash
API=https://tes.example.org/ga4gh/tes/v1
read -rs TOKEN && export TOKEN        # a user with the role `user`
read -rs OTHER_TOKEN && export OTHER_TOKEN  # optional: a user without a role
tests/smoke/deployment.sh "${API}"
# Funnel serves the TES API at the default path, TESP-API at `/v1`
tests/integration/check.sh "${API}" https://funnel.example.org ""
tests/integration/check.sh "${API}" https://tesp.example.org /v1
# With Pulsar 0.15 or later:
# EMPTY_LOGS=true tests/integration/check.sh "${API}" https://tesp.example.org /v1
COMBINED_LOGS=true tests/integration/check.sh \
  "${API}" https://tesk.example.org /ga4gh/tes/v1
unset TOKEN OTHER_TOKEN
```

Each script prints failed checks with the response, and stops with an
error (while waiting for a state, at the end of the wait). With `TOKEN`,
`tests/integration/check.sh` refuses an API URL without HTTPS, as
`tests/smoke/deployment.sh` does. What the scripts can't see from outside, check in the cluster:
that the executors of canceled tasks stopped (for TESK, as
`tests/integration/tesk/stopped.sh` does in CI), and the values of the
Route (`tests/test_chart.py` checks that the README's values for OpenShift
render a Route with TLS and a router timeout above the longest request to
create a task). Where a real TES instance behaves differently from the fake
backend of the smoke test (`tests/smoke/fake_tes.py`), correct the fake, and
add the difference to [Notes on TES
implementations](configuration.md#notes-on-tes-implementations).

## Dependencies

The dependencies are declared, with version ranges, in `pyproject.toml`
(which installs of the package, e.g., with pip, use) and locked in
`uv.lock`, with exact versions and hashes, by
[uv](https://docs.astral.sh/uv/) (also a dev dependency, so that the lock
pins the version of uv that CI uses). CI's lint, test, chart and docs jobs
install the locked versions, checked against their hashes
(`uv sync --locked`, with uv of a pinned `astral-sh/setup-uv`, at the
version of the lock), so that Dependabot's pull requests, which change only
the lock, are tested on the versions they lock; the lint job also checks
that the lock matches `pyproject.toml` (`uv lock --check`). The app itself
is installed editable there, built with build requirements from the package
index (not the lock), as `pip install -e` does; those jobs hold no
publishing credentials. The build requirements of the app
(`build-system.requires`) are locked the same way, as the dependency group
`build-system`: CI's `push` job derives the version of dev images with
them, exported by the uv image of the `Dockerfile` (pinned by digest) and
installed with hash checks (by the pip of the runner's Python,
`ensurepip`, nothing downloaded), so that no workflow step of a job that
holds publishing credentials installs a package without hash checks (the
tests check this). Keep the group and `build-system.requires` the same
(the tests fail if they differ).

The image build (`Dockerfile`) installs the locked packages only, with the
uv of the same pinned image, mounted into the steps that use it (the image
doesn't contain uv), so that two builds of the same commit get the same
packages, and a new release of a dependency gets into the image only
through a reviewed change of the lock:

- the runtime dependencies: exported from the lock
  (`uv export --frozen --no-dev --no-emit-project`, which leaves out the
  extras and the dependency groups), then installed into the Python of the
  base image with
  `uv pip install --system --require-hashes --only-binary :all: --no-deps`
  (exact versions, hash-checked wheels, nothing else resolved);
- the app: built as a wheel in a build stage of its own, with the
  `build-system` group installed the same way into a virtual environment,
  without build isolation and without network (`uv build --wheel --offline
  --no-build-isolation`), then installed offline without its dependencies;
  the image doesn't contain the build requirements. As nothing is resolved,
  `uv pip check` then checks that all installed packages meet each other's
  requirements.

Packages that the base image already has at the locked version (e.g., FOCA)
are kept as they are, trusted through the base image's digest, not checked
against the hashes of the lock again. The build needs BuildKit (the default
builder of Docker since version 23, or `docker buildx`), for `RUN --mount`
and `RUN --network=none`.

`scripts/image_requirements.py` leaves a temporary FOCA commit pin (see
[Versioning and releases](#versioning-and-releases)) out of the export and
fails the build unless the base image has the locked FOCA release (or FOCA
of the pinned commit); `tests/test_image_build.py` checks that every install
of the image build is hash-checked or offline. The only exception is the
build argument `PLUGINS` for local builds, empty for the published images
(see [Plugin images](plugin-images.md#local-builds)). The image contains
no pip and no uv: the FOCA base image has no pip, and uv is mounted from a
stage pinned by digest into the steps that use it, which the smoke test of
CI checks.

After adding, removing or changing a dependency in `pyproject.toml`, update
the lock and commit both:

```bash
uv lock  # update `uv.lock` after changes to `pyproject.toml`
uv lock --upgrade-package <name>  # upgrade one locked package
uv lock --check  # what CI checks: the lock matches `pyproject.toml`
```

Dependabot updates `pyproject.toml` and `uv.lock` together, including the
`build-system` group (`uv` entry of `.github/dependabot.yml`), and the uv
image of the `Dockerfile` (`docker` entry). The lock also pins FOCA, and the
image build fails unless the base image has exactly the locked FOCA, so FOCA
is updated by hand, in the base image and the lock together
(`uv lock --upgrade-package foca`); Dependabot ignores new FOCA versions in
both entries, but still proposes digest updates of the base image's tag.

The routing plugins come from the release train `foca-plugins`
(`foca-plugins[generic-routing,tes-routing]==<version>` in `pyproject.toml`),
which pins each plugin to an exact version, locked with its hashes like all
other dependencies. A new FOCA minor needs a release of foca-plugins that
allows it first: a commit pin of foca-plugins would fail the release pin
check (see [Versioning and releases](#versioning-and-releases)). For tests
of unreleased plugins, [#434](https://github.com/elixir-cloud-aai/protes/pull/434)
shows how a commit pin is built into the image.

## Helm chart

The [Helm chart](deployment.md) in `deployment/` is a thin chart over FOCA's
library chart `foca-common` (see the [library chart's
documentation](https://github.com/elixir-cloud-aai/foca/blob/chart-v0.4.0/docs/helm-chart.md)):
its templates are one-line includes of the library's templates, with the
app's settings and checks in `templates/_helpers.tpl` (`protes.settings`,
`protes.validate`), and templates of its own for Flower. `values.yaml` has
all values of the library chart, and `values.schema.json` is the library's
`app-values.schema.json` with the app's key (`protes`, plus `extraEnv`) and
`flower` added; update both with the library chart (the schema's
`$comment` names its version, `foca-common X.Y.Z`, as the release pin check
requires, see [Versioning and releases](#versioning-and-releases)). The
tests in `tests/test_chart.py` render the chart with `helm template` and need
[Helm](https://helm.sh/docs/intro/install/) (the version of the `chart` job
of `.github/workflows/ci.yml`) and the library chart in
`deployment/charts/`; they are skipped if Helm is not installed:

```bash
helm dependency build deployment  # the version locked in `Chart.lock`
(cd deployment && sha256sum -c charts.sha256)  # as CI checks it
helm lint deployment --strict -f deployment/ci/test-values.yaml
pytest tests/test_chart.py
```

`deployment/Chart.yaml` pins the version of the library chart, from
`oci://ghcr.io/elixir-cloud-aai/charts`, and `deployment/Chart.lock` locks
it; `helm dependency build deployment` downloads it into
`deployment/charts/` (ignored by Git), and the release workflow packages it
with the chart. CI and the release workflow check the downloaded archive
against its digest in `deployment/charts.sha256`, as OCI tags can be pushed
again. Dependabot doesn't update it (the `helm` entry of
`.github/dependabot.yml` ignores it): to bump it, change the version in
`Chart.yaml`, run `helm dependency update deployment` (which rewrites
`Chart.lock`), update `deployment/charts.sha256` (`cd deployment &&
sha256sum charts/foca-common-X.Y.Z.tgz > charts.sha256`, after checking the
archive), copy the library's `app-values.schema.json` to
`deployment/values.schema.json` with the changes noted in its `$comment`
(which names the new version, `foca-common X.Y.Z`, as the release pin check
requires), add values it introduces to `deployment/values.yaml`, update
the links to the library's docs in `deployment/README.md` (the tests check
that they point to the pinned version, `chart-vX.Y.Z`), and check the
rendered chart with the tests and `helm template`.

The chart's values pin the images of MongoDB, RabbitMQ and Flower
(`mongodb.image`, `broker.image`, `flower.image`), and Docker Compose
(`docker-compose.yaml`) runs the same image references, as
`tests/test_repository.py` checks; bump them in both files together.
Dependabot does so in one pull request (multi-ecosystem group
`service-images`, with the `docker-compose` and `helm` entries of
`.github/dependabot.yml`).

## TES specification

The TES specification is vendored as
`pro_tes/api/<date>.<commit>.task_execution_service.openapi.yaml`, with the
date and the short hash of the commit of the upstream
[tag](https://github.com/ga4gh/task-execution-schemas/tags), unchanged but for
commented deviations (the references to the schemas of the service info, which
`additions.openapi.yaml` holds, and which would otherwise be fetched at
startup); the extensions of proTES go into `additions.openapi.yaml`. To update
it, replace the file, change `api.specs.tes.path` and
`service_info.service.type.version` in `pro_tes/config.yaml`, the paths in
`.editorconfig` and `.pre-commit-config.yaml`, and the link in `docs/api.md`,
and follow the changes in the models (`pro_tes/ga4gh/tes/models.py`, kept by
hand, as generating them would undo manual fixes).
`tests/ga4gh/tes/test_contract.py` checks responses against the file.

## Documentation

This documentation is built with [MkDocs](https://www.mkdocs.org/) and
[Material for MkDocs](https://squidfunk.github.io/mkdocs-material/), from
`docs/` and
[`mkdocs.yml`](https://github.com/elixir-cloud-aai/protes/blob/dev/mkdocs.yml),
by Read the Docs (configured in
[`.readthedocs.yaml`](https://github.com/elixir-cloud-aai/protes/blob/dev/.readthedocs.yaml))
and in CI; warnings, e.g., of broken links, fail the build. The [API
reference](reference.md) is generated from the docstrings with
[mkdocstrings](https://mkdocstrings.github.io/). To build it locally, or serve
it with live reloading, in the dev environment set up as above, with the
locked docs dependencies too (as CI does; `uv sync` removes the extras that
it isn't given, so `dev` stays in the command; Read the Docs installs the
`docs` extra with pip, from the ranges of `pyproject.toml`):

```bash
uv sync --locked --extra dev --extra docs
mkdocs build --strict  # into `site/`, ignored by Git
mkdocs serve
```

Parts of the docs are kept elsewhere, so that they are written only once: the
introduction, the known deployments and the quick start of the home and
getting started pages are sections of `README.md` (between
`--8<-- [start:<name>]` and `--8<-- [end:<name>]` comments), and the
[deployment page](deployment.md) is the README of the Helm chart
(`deployment/README.md`), which `docs/hooks.py` includes, with its links to
pages of the docs and to files of the repository adapted. Links in the
chart's README must be absolute (to `https://protes.readthedocs.io/en/latest/`
and to files on branch `dev` on GitHub), as they must also work in the
packaged chart.

## Versioning and releases

The project adopts the [semantic versioning](https://semver.org/) scheme for
versioning. Currently the service is in beta stage, so the API may change
without further notice.

The version is only stored in Git tags (`vX.Y.Z`): the package version is
derived from them (via
[setuptools-scm](https://setuptools-scm.readthedocs.io/)), and the service
info reports it. Builds without Git metadata, such as local Docker builds
(e.g., with Docker Compose), report `0.0.0.dev0`; the workflows pass the
version to the image build (build argument `SETUPTOOLS_SCM_PRETEND_VERSION`).
Images on Docker Hub (`elixircloud/protes`) are tagged:

- `X.Y.Z`: release `X.Y.Z`; `latest`: the highest stable release (not an
  alpha, beta or release candidate) on `main`;
- `dev`: the latest commit on `dev`; `sha-<commit>`: commit `<commit>` on
  `dev` (full SHA), to deploy a fixed commit. Only commits that are still the
  head of `dev` when their CI run reaches the image job get images.

The current release version is taken from one source, so that a release
doesn't need to bump it in many places: the release tag and, on `dev`, the
`version` in `CITATION.cff` (set before tagging, see below; from then on, the
latest docs show the upcoming version, and before the first release, they
show `X.Y.Z`). The docs and the chart's README (`deployment/README.md`) write
it as a placeholder, `version` in double curly braces with a space inside
each pair, and the chart version (e.g., `1.2.3-rc1` for `1.2.3rc1`, as in the
names of the packaged charts) as `chart_version` in the same braces
(`VARIABLE` and `CHART_VARIABLE` in `scripts/chart_readme.py`; this page
can't show them, as they would be replaced), also in commands and links. The
docs hooks (`docs/hooks.py`) replace them with the version of `CITATION.cff`,
and `scripts/chart_readme.py` with the release version when the chart is
packaged; files read on GitHub (e.g., `deployment/README.md`) show the
placeholders, and sections of `README.md` included as snippets can't use
them. Comments, templates and tests use placeholders (e.g., `1.2.3`) and
refer to sections by anchor. `tests/test_version_mentions.py` fails if the
current release version is mentioned as a release of the app (as a tag,
image or chart, or as "release" or "version" in prose; not, e.g., as the
version of a dependency) anywhere else than in `CITATION.cff`,
`docs/releases/`, the tests of the release tooling, links to the notes and
assets of a release, and migration headings (e.g., "From versions before
X"), which are about that release itself.

Features and fixes are merged into `dev`. Branch `main` only ever moves
forward to commits on `dev` that are released. First, shortly before the
release, set its version and date in the citation metadata, which Zenodo
archives with each release, and merge the change into `dev`; each version,
also each pre-release (e.g., `X.Y.Zrc1`), needs its own such change:

```bash
python scripts/citation.py set X.Y.Z  # date: today (UTC), or --date YYYY-MM-DD
```

Then, to release version `X.Y.Z` from commit `<sha>` on `dev` (with that
change), after CI passed for it:

```bash
git switch --detach <sha>
# The release workflow's checks that need no tag, as it runs them
python scripts/release_version.py outputs vX.Y.Z
python scripts/citation.py check X.Y.Z
python scripts/check_release_pins.py
! grep -n TODO docs/releases/X.Y.Z.md  # curated notes, if any
[ ! -f docs/releases/X.Y.Z.md ] || python scripts/release_notes.py check docs/releases/X.Y.Z.md
readme="$(mktemp -d)/README.md" && cp deployment/README.md "${readme}"
python scripts/chart_readme.py "${readme}" vX.Y.Z  # links of the chart
git push origin <sha>:main  # fails unless it is a fast-forward
git tag -s vX.Y.Z <sha> -m "vX.Y.Z"
git push origin vX.Y.Z
```

Tags can't be moved or deleted (see the repository settings below), and the
release workflow checks the tagged commit, so a check that fails after the
tag is pushed fails again on re-runs: the release then needs a new patch
version, with its own citation metadata and notes. Run these checks before
tagging, therefore. The CI job "Release readiness" runs the pin and
the notes checks, too, for the version in `CITATION.cff` while it has no
tag (or its tag is on the checked commit): on pull requests and in forks
(which often lack upstream's later tags) as warnings, on pushes to `dev` as
errors, which fail the CI run that the release workflow requires for the
release commit. The image scan of the release workflow can
still fail after the tag, e.g., on a vulnerability published since CI last
scanned the image of the commit; check the code scanning alerts before
tagging.

The tag must be annotated and signed (`git tag -s`), with a key that GitHub can
verify (added to the tagger's GitHub account). The [release
workflow](https://github.com/elixir-cloud-aai/protes/blob/dev/.github/workflows/release.yml),
triggered by the tag, checks that GitHub verified the signature, that the
tag's commit is on `main` and its `CITATION.cff` has the release version and
a date that is not in the future (UTC), that the dependencies are releases
(see below), and that the [CI
workflow](https://github.com/elixir-cloud-aai/protes/blob/dev/.github/workflows/ci.yml)
passed for it (a run for the push of the commit to `dev`; if CI hasn't
finished, failed or was cancelled, e.g., the image job of a commit that a
later one superseded, re-run CI, then the release workflow). It packages
the Helm chart with the release version as `version` and `appVersion` (the
default image tag). In the packaged README, links to the latest docs and to
files on branch `dev` point to the release (`/en/vX.Y.Z/`, `blob/vX.Y.Z/`);
the release fails if any link to a version that moves is left
(`scripts/chart_readme.py`). The README in the repository is unchanged.
After approval in the `release` environment, it builds the image and pushes
it by digest (untagged, with SBOM and provenance attestations) and checks
that the app reports the release version; it then scans exactly that digest
for vulnerabilities, in a job without credentials. After a second approval
in the `release` environment (once the scan passed), it checks that the
chart it downloads is the one it packaged (a single file with the name and
SHA-256 recorded when packaging; if not, the job fails before it attests,
signs or tags anything), attests the build provenance of the scanned digest
and signs it (see [Verifying images and
charts](operations.md#verifying-images-and-charts)), tags exactly that image
`X.Y.Z` (and `latest`, if it is the highest stable release on `main`),
attests and signs the chart, and creates a GitHub release with the chart
(`.tgz`), its Sigstore bundle (`.tgz.sigstore.json`) and the release notes
(see below). Releases are immutable (see the repository settings below):
once published, their assets can't be added, replaced or deleted. The
workflow therefore creates the release as a draft, uploads the assets and
publishes it last. A re-run finishes a draft left by an earlier attempt; if
the release is already published (e.g., by hand), it uploads nothing and
fails unless the release has exactly the assets that the run built
(only the Sigstore bundles may differ, as every attempt signs anew). The
error says what to do: a new patch version, or, if releases are not
immutable, deleting the release and re-running. Add curated notes to the
[changelog](changelog.md) in `mkdocs.yml` (`nav`; the docs build fails
otherwise), and refer to pages of the docs by name rather than by relative
links, which don't work in GitHub releases. No packages are published to
PyPI. Tags of alpha, beta and release candidate versions (e.g., `vX.Y.Zrc1`,
also `aN` and `bN`) get chart version `X.Y.Z-rc1` (or `-aN`, `-bN`) and never
update the `latest` image. These and, until 1.0.0, all other releases are
GitHub pre-releases (never marked as the latest release). Digests of failed
runs stay on Docker Hub, untagged and unsigned. Images of commits on `dev`
are published by the CI workflow (environment `dev-images`).

The release workflow derives the version, the chart version, the kind of
release (stable, GitHub pre-release), whether the image is tagged `latest`
and the previous release (for the generated notes) from the tag with
`scripts/release_version.py`, which is tested over a table of tags
(`tests/test_release_version.py`) and checked like the app (ruff, mypy,
coverage); change these rules there, not in the workflow. It finds the
GitHub release of the tag, if any, and checks the assets of a published one
with `scripts/release_assets.py` (tested in `tests/test_release_assets.py`).

To test unreleased changes of FOCA, `pyproject.toml` may pin a FOCA commit
(`foca @ git+https://github.com/elixir-cloud-aai/foca@<sha>`), with FOCA's
development image of that commit as the base image (tagged by date, e.g.,
`elixircloud/foca:20261023-py3.13`, pinned by digest, as the tag moves with
later commits of that day). The `Dockerfile` records the commit as
`## FOCA commit: <sha>`, which the tests check (`tests/test_build.py`); the
image build doesn't reinstall FOCA, but fails unless the base image has FOCA
of that commit (`scripts/image_requirements.py`; the lock has no hash for
such a pin, so the image build leaves it out, while FOCA's dependencies stay
locked). Meanwhile, have Dependabot ignore the base image
(`.github/dependabot.yml`), as updates of its digest would change FOCA.
Such pins are temporary: the release workflow fails while a requirement
(also an optional one, a dependency group or a build requirement) is a
direct reference or names a pre-release or a local version
(e.g., `+g<commit>`), a FOCA image (in any stage) has no release tag, or a
chart dependency has no version, another version than a release or no chart
repository (`oci://`, `https://`; e.g., a `file://` path to a checkout of
the library chart) (`scripts/check_release_pins.py`,
tested in `tests/test_check_release_pins.py`, whose tests also fail for
checkouts of release tags with such pins), so replace them by the released
versions before tagging. It also fails unless `deployment/Chart.yaml` and
`Chart.lock` pin the version of the library chart that the `$comment` of
`deployment/values.schema.json` names (`foca-common X.Y.Z`): the version
that the release requires, whose values schema the chart copies. When moving
to a new FOCA minor version, update the comment with the library chart. This
applies to pre-releases of the app, too: they are built on FOCA releases.

Release notes follow the format agreed for all repositories of ELIXIR
On-Cloud:

1. An intro paragraph saying what the release brings, with a link to the
   upgrade steps (e.g., on the [Upgrading](upgrading.md) page) if there are
   any.
2. Grouped sections, with empty ones left out, in this order:
   `## Breaking changes`, `## Features`, `## Security`, `## Fixes`,
   `## Documentation`, `## Maintenance`. They hold bullets only, and each
   bullet ends with its issue and pull request numbers, e.g., `(#12, #34)`
   (several pairs separated by semicolons, e.g., `(#12, #34; #56, #78)`).
3. `## Commits`: GitHub's generated notes, verbatim (including "New
   Contributors"), without their `## What's Changed` header, as they credit
   the authors.
4. Last, GitHub's generated `**Full Changelog**: ...` line (no compare link
   of our own).

The intro of the curated notes also says against which TES implementations
the release was tested, as does a paragraph `**Tested TES implementations:**`
in the release's section of the [Upgrading](upgrading.md) page: those of
CI's jobs `Integration` and `Integration (TESK)` on the release commit, in
the versions that [Notes on TES
implementations](configuration.md#notes-on-tes-implementations) lists, with
the TES version that each got (check that both jobs passed, as the gate
doesn't need them), and any tested by hand, e.g., on a deployment. Name TES
implementations whose job failed or was skipped, and deployments not
tested, as not tested.

The curated notes in `docs/releases/X.Y.Z.md` (for pre-releases, e.g.,
`X.Y.Zrc1.md`) hold parts 1 and 2, without a title (the release's title is
its tag); the release workflow adds parts 3 and 4 from GitHub's
[generate-notes](https://docs.github.com/en/rest/releases/releases#generate-release-notes-content-for-a-release)
API, for the changes since the previous published release in the history of
the release commit (for stable releases the previous stable release, or the
previous pre-release if there is no stable one, as the generated notes can't
start before the first tag; for alpha, beta and release candidate versions
the previous release of any kind). For the first release (no published
release before it, even if older tags exist), the API would start at an older
tag of its choice, so the workflow lists all merged pull requests in the
history of the release commit itself, in the API's format:
`scripts/release_notes.py first` reads the closed pull requests (`gh api
--paginate --slurp "repos/<owner>/<repo>/pulls?state=closed&per_page=100"`,
for which the job has `pull-requests: read`) and the commits (`git rev-list
HEAD` at the release commit); run these commands locally to preview the
notes. Generated notes in an unexpected format (without the `**Full Changelog**`
line) are kept as they are, with a warning, as the image is published by
then. Without curated notes, the release gets GitHub's generated notes
alone. Curated notes are published as written, so the release workflow
fails if they contain `TODO`, are not in this format or link to versions
that move (the latest docs, `/en/latest/`, or files on branch `dev`,
`blob/dev/`; link the release's, e.g., `/en/vX.Y.Z/`, `blob/vX.Y.Z/`, as the
published notes don't change) or have relative links (see below)
(`scripts/release_notes.py check`, tested in
`tests/test_release_notes.py`, which also checks the notes in
`docs/releases/`), and so does the CI job "Release readiness" on
pushes (see above); on re-runs, it replaces the notes of an existing release
(also overwriting manual edits).
Relative links, e.g., `../upgrading.md#to-version-050`, work in the docs built
from `docs/releases/`, but not in the release body: link the release's docs
with their full URL, e.g.,
`https://protes.readthedocs.io/en/vX.Y.Z/upgrading/#to-version-050`,
or within the page with `#...`. The check reports every link target without
a scheme (such as `https:` or `mailto:`) of inline links, images, reference
definitions and the HTML attributes `href`, `src` and `srcset` (each of its
URLs), anywhere in the notes, code blocks included (and lines of paragraphs that
start like a definition, `[label]: ...`). Detailed upgrade steps belong on the
[Upgrading](upgrading.md) page, linked from the intro.

Maintainers: the workflows rely on these repository settings: environment
`dev-images` limited to branch `dev`; environment `release` with a required
reviewer; a tag ruleset for `v*` that restricts who may create them and
prevents moving or deleting them; immutable releases; `main` protected
against non-fast-forward pushes; and the Docker Hub secrets
(`DOCKERHUB_LOGIN`, `DOCKERHUB_TOKEN`) only in the two environments, not as
repository secrets. The Docker Hub organization is the repository variable
`DOCKERHUB_ORG` (by default `elixircloud`; forks set it to the organization
they publish to, with the Docker Hub secrets in an environment
`dev-images`), so that the scan jobs, which have no secrets, know the image
name too, and the released chart's default image (`protes.image`) is the
published one (the chart's README still names upstream's default). On Read
the Docs, the default version is `stable` (the highest release) from the
first release on, and the default branch is left empty, so that `latest`
follows the repository's default branch, `dev`; an automation rule activates
the versions of tags (`v*`), so that the docs linked from packaged charts
exist.

The CI workflow builds the image of every pull request and scans it with
[Trivy](https://trivy.dev/) (the shared action
[`scan-image`](https://github.com/elixir-cloud-aai/.github/tree/main/actions/scan-image),
pinned to the commit of a release), and a weekly workflow (`scan.yml`)
scans `latest`. For pushes to `dev` (in forks only if
they set `DOCKERHUB_ORG`) and for releases, the workflows push the image by
digest, untagged, scan exactly that digest in a job without credentials or
signing identity, and only if the scan passes attest, sign and tag it, so
that the published images are the scanned ones; a digest that fails the scan
stays untagged and unsigned. The scans fail on critical and high
vulnerabilities that have a fix, and upload the findings that have a fix
(all severities) to code scanning (Security tab; not in forks, nor for pull
requests from forks, nor in the release workflow); findings without a fix
appear there once a fix is released. Forks without `DOCKERHUB_ORG` publish
nothing: their CI builds and scans the image locally, as for pull requests,
and passes without the secrets; in all forks, Codecov upload errors don't
fail CI. The weekly scan also uploads a full report, findings without a fix
included, as the workflow artifact `trivy-report-release-image` of each run
(`trivy-report.json` and `trivy-report.txt`, a table; Actions tab, "Scan");
it is not uploaded to code scanning. If no FOCA image has the fix of a system
package yet, or moving to one has to wait, upgrade that package in the stage
`base` of the `Dockerfile`, until then. Accepted findings are left out of all
results. To accept a finding (e.g., if it doesn't affect the app), add it to
`.trivyignore.yaml` with the affected package versions (`purls`), the reason
(`statement`) and an expiry date (`expired_at`), after which the scans fail
again. Trivy also scans for secrets (critical and high ones fail the scans
too); accept those under `secrets:` in the same file. Dependabot updates the
actions, including the pins of the shared actions (`@<commit> # vX.Y.Z`), to
new releases of `elixir-cloud-aai/.github`; the Trivy version is the
default of the shared action's `trivy-version` input, which Dependabot
doesn't bump here: change it in the shared action, whose release then
reaches this repository as a Dependabot update of the pins. Images of commits
on `dev` are signed and attested like release images, by the CI workflow.
All of this needs no secrets beyond the Docker Hub ones: signing uses the
workflows' OIDC tokens (permission `id-token: write`). Re-running all jobs
of the release workflow builds, scans, signs and tags a new image digest
(the image build is not reproducible); an earlier digest that was published
stays signed. Re-running only the failed jobs reuses the digest already
pushed. Re-running all jobs also packages the chart again and replaces the
chart artifact; the release checks the new chart against its new SHA-256.

The release workflow installs a fixed Helm version (`version:` of
`azure/setup-helm` in `release.yml`). Dependabot updates the action, but not
that version: bump it by hand.
