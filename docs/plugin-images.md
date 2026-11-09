# Plugin images

Plugins are Python distributions that must be installed in the image of
proTES: nothing is installed at runtime, and the Helm chart keeps the root
file system of the containers read-only. The published images of proTES
have FOCA's built-in plugins and two routing plugins from foca-plugins
(`generic.routing.random`, enabled by default, and `tes.routing.distance`,
which admins enable explicitly, see [Plugins](plugins.md#pipelines));
there is no image with all plugins, as plugins come from several
repositories, in many combinations. To deploy other plugins, or to add data
that plugins need (e.g., the GeoIP database of the offline provider of
`tes.routing.distance`), build an image derived from a release image of proTES with
the recipe of this page: it installs exactly the distribution files that you
pinned, reviewed and verified, and nothing else. Then enable the plugins in
the config (see [Plugins](plugins.md)): installed is not enabled.

!!! note "Plugins of foca-plugins"

    The plugins of ELIXIR On-Cloud, e.g., `tes.routing.distance`, are
    developed in the repository foca-plugins and published to PyPI as
    single distributions (`foca-plugin-<group>-<subgroup>-<name>`, e.g.,
    `foca-plugin-tes-routing-distance` and
    `foca-plugin-generic-routing-random`) and as release trains (extras of
    the distribution `foca-plugins`, e.g., `foca-plugins[tes-routing]`). See
    its
    [documentation](https://foca-plugins.readthedocs.io/en/latest/installing/).
    The images of proTES contain the two routing plugins already (for now
    built from the commit of foca-plugins in `pyproject.toml`, as its releases
    don't allow FOCA 0.22 yet, see
    [Dependencies](development.md#dependencies)); don't list them in
    `plugins.in`. The recipe works the same for plugins of any repository, and CI
    tests it with a test plugin (see [Testing the
    recipe](#testing-the-recipe)).

The commands below run in a directory of their own (the build context),
with Docker and the files `resolve.sh`, `download.sh`, `common.sh`,
`Dockerfile` and `Dockerfile.dockerignore` of `examples/plugin-image/` of
the proTES
repository (of the release that you build on), copied to the directory.
The proTES image has no pip and no uv: the scripts and the `Dockerfile`
mount uv, from the image pinned by digest in the `Dockerfile`, so that the
plugins are resolved for the image's Python version, platform and installed
packages, and uv never stays in an image.

## Base image

Build on a release image of proTES, by digest (see [Verifying images and
charts](operations.md#verifying-images-and-charts) to check it):

```bash
docker buildx imagetools inspect docker.io/elixircloud/protes:{{ version }}  # shows the digest
export PROTES_IMAGE=docker.io/elixircloud/protes:{{ version }}@sha256:<digest>
```

## Pinning plugins

List the plugins in `plugins.in`, as requirements: single distributions
or a release train, e.g.:

```text
foca-plugin-<group>-<subgroup>-<name>==<version>
```

Resolve them against the image:

```bash
./resolve.sh "$PROTES_IMAGE" plugins.in plugins.txt
```

`resolve.sh` runs `uv pip compile` inside the image, with the image's
installed packages as constraints, and writes the distributions that the
image lacks (the plugins and their dependencies), with their exact versions
and the hashes of their wheels, to `plugins.txt`. Options of `docker run`
follow the file names, e.g., `--env UV_DEFAULT_INDEX=<index>` for another
package index, which must also serve the dependencies of the plugins that
the image has (e.g., FOCA), as uv doesn't take them from the image. They go
to the `docker run` that resolves, so run the scripts on the image's
architecture: `--platform` doesn't work.

The constraints keep the packages of the image as they are: `resolve.sh`
fails (with uv's errors) and removes `plugins.txt` if a plugin needs other
versions of them (e.g., another FOCA version), or if a distribution can't be
found; choose plugin versions that work with those of the image.
Review `plugins.txt` and keep it, e.g., in the repository of your
deployment: it is the complete list of what the image adds. If it is empty,
the image has all the plugins already: skip the remaining steps.

## Downloading and verifying

Download the wheels:

```bash
./download.sh "$PROTES_IMAGE" plugins.txt wheels
```

`download.sh` runs pip for this one command, as uv has no download command:
a version pinned in the script, without a hash check of its own, from uv's
temporary environment, not from the image. Options of `docker run` follow
the directory, e.g., `--env PIP_INDEX_URL=<index>` for the wheels and `--env
UV_DEFAULT_INDEX=<index>` for pip itself. pip checks the hashes of
`plugins.txt`, and the image build checks them again. Before the build, also check where
the wheels come from: the plugins of foca-plugins are published with [PEP
740](https://peps.python.org/pep-0740/) attestations and GitHub artifact
attestations of their build provenance (see [Verifying
distributions](https://foca-plugins.readthedocs.io/en/latest/installing/#verifying-distributions)).
For example, check the GitHub artifact attestation of each of their wheels,
with the repository that publishes the plugin:

```bash
gh attestation verify wheels/<wheel> --repo <owner>/<repository>
```

Check other plugins and dependencies as their projects document.

## Building

Build the image:

```bash
docker build --build-arg PROTES_IMAGE="$PROTES_IMAGE" \
  --tag registry.example.org/protes-plugins:{{ version }}-1 .
```

The `Dockerfile`:

```dockerfile
--8<-- "examples/plugin-image/Dockerfile"
```

The build installs the wheels of `wheels/` that `plugins.txt` lists, and
nothing else: without the package index and without further dependencies
(`--no-index --no-deps --require-hashes`). It fails if a hash differs or a
dependency of an installed distribution is missing (`uv pip check`). The image
runs as user 1000, as that of proTES. Only `plugins.txt` and `wheels/` are
sent to the build (`Dockerfile.dockerignore`), so that other files of the
directory, e.g., `.env` files, never reach it.

Scan the image for known vulnerabilities (e.g., with
[Trivy](https://trivy.dev/): `trivy image <image>`), push it to your
registry and sign it (e.g., with [cosign](https://docs.sigstore.dev/cosign/):
`cosign sign <image>@<digest>`), as the workflows of proTES do for its
images.

## Deploying

Deploy the image and enable the plugins, e.g., in the values file of the
Helm chart (see [Configuration](deployment.md#configuration) of the
chart's README):

```yaml
protes:
  image: registry.example.org/protes-plugins@sha256:<digest>
  tag: ""
  imagePullPolicy: IfNotPresent
plugins:
  available:
    org.example.plugin:
      distribution: foca-plugin-<group>-<subgroup>-<name>
    # The distance routing plugin of the image (not enabled by default):
    # locate addresses offline with a GeoIP database that your image adds,
    # e.g., by a `COPY` of your own (see the plugin's README), and put it
    # into the pipeline, e.g., before `generic.routing.random`
    tes.routing.distance:
      distribution: foca-plugin-tes-routing-distance
      config:
        provider: offline
        database: /opt/geoip/dbip-city-lite.mmdb
```

The app refuses to start if an enabled plugin isn't installed, doesn't match
its pins (`distribution`, `version`), or doesn't support the TES API: a
wrong image fails at once, not with the first task.

Rebuild the image for every release of proTES that you deploy, and resolve
`plugins.in` against the new image again (see [Pinning
plugins](#pinning-plugins)), as its packages change. Plugins declare the
FOCA versions that they support (e.g., `foca>=0.22.0,<0.23`), so resolving
them against an image with another FOCA version fails.

## Local builds

For development, the `Dockerfile` of proTES installs plugins with uv, given
as requirements in the build argument `PLUGINS` (empty by default), from the
package index and with their dependencies; Docker Compose passes the host's
`PLUGINS`:

```bash
PLUGINS="foca-plugin-<group>-<subgroup>-<name>==<version>" docker compose up --build --wait
```

Enable them in the config overlay (e.g., `config.dev.yaml`). Unlike the
dependencies of proTES, which the image build installs from the lock, these
plugins are **not** pinned by hash nor reviewed: use the recipe above for
images that you deploy. `PLUGINS` is split at spaces (write specifiers without
spaces, e.g., `name>=1.0`) and recorded in the image's history: pass
requirement specifiers only, never credentials (e.g., of a private package
index).

## Testing the recipe

The smoke test of CI (`tests/smoke/plugins.sh`, see
[Development](development.md)) builds the image of the recipe from the app's
image with a test plugin (`tests/smoke/plugin/`, which tags each new task),
pinned by hash with `resolve.sh` and downloaded with `download.sh` (the
plugin from a local directory, its dependencies from the package index, and
pip itself too), checks that a wrong hash fails the build, runs proTES with
it enabled, and checks that new tasks get its tag.
