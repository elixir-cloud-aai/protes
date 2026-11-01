"""Tests of the default routing: random order, without third-party lookups.

The image ships the routing plugins of foca-plugins (`tes.routing.distance`
and `generic.routing.random`). The base config makes only the random one
available and uses it as the default pipeline: distance routing can't
locate the hosts of inputs without a lookup service, its default provider
(`dbip`) sends their addresses to DB-IP and Nominatim, and `offline` needs a
GeoIP database that the image doesn't contain (#384). Admins opt in to
distance routing in the config. The test overlay of the other tests sets
FOCA's ordered failover instead, so that they are independent of the
plugins; these tests run the app with the pipeline of the base config (or
the opt-in) and the installed plugins, without network access.
"""

from importlib.metadata import PackageNotFoundError, entry_points, version
from pathlib import Path
import socket
from typing import Any

from connexion import FlaskApp
from flask.testing import FlaskClient
from foca import get_config
from foca_plugins.tes.routing.distance.providers import DbipProvider
from pymongo import MongoClient
import pytest
import yaml

from pro_tes import app as app_module
from pro_tes.app import init_app
from tests.conftest import set_overlays
from tests.ga4gh.tes.conftest import (
    BACKENDS,
    BASE_PATH,
    TASK,
    FakeBackend,
    relayed,
    stored,
)

BASE_CONFIG = Path(app_module.__file__).parent / "config.yaml"
# A public address, as the host of an input
PUBLIC = "8.8.8.8"
# The routing plugins, with their distributions
DISTANCE = ("tes.routing.distance", "foca-plugin-tes-routing-distance")
RANDOM = ("generic.routing.random", "foca-plugin-generic-routing-random")
# The opt-in to lookups with DB-IP and Nominatim, as the docs show it
OPT_IN: dict[str, Any] = {
    "available": {
        DISTANCE[0]: {"distribution": DISTANCE[1], "config": {"provider": "dbip"}}
    },
    "pipeline": [
        {
            "id": "route",
            "alternatives": [{"plugin": DISTANCE[0]}, {"plugin": RANDOM[0]}],
        }
    ],
}
# A task with an input on a public address
TASK_WITH_INPUT = {
    **TASK,
    "inputs": [{"url": f"https://{PUBLIC}/in", "path": "/in", "type": "FILE"}],
}


def fail_lookup(*args: Any, **kwargs: Any) -> None:
    """Fail a test that makes a lookup (of an address or of a host name).

    Args:
        *args: Arguments of the lookup.
        **kwargs: Keyword arguments of the lookup.

    Raises:
        AssertionError: Always.
    """
    raise AssertionError(f"unexpected lookup: {args} {kwargs}")


def base_plugins() -> dict[str, Any]:
    """Get the section `plugins` of the base config.

    Returns:
        The section.
    """
    plugins: dict[str, Any] = yaml.safe_load(BASE_CONFIG.read_text())["plugins"]
    return plugins


def test_default_pipeline() -> None:
    """The default pipeline routes in random order only."""
    plugins = base_plugins()
    (step,) = plugins["pipeline"]
    assert step["id"] == "route"
    assert [alt["plugin"] for alt in step["alternatives"]] == [RANDOM[0]]
    assert not plugins.get("client_pipelines", {}).get("enabled", False)


def test_default_plugins_available() -> None:
    """Only the random routing plugin is available, pinned to its distribution.

    The distance plugin is installed, but not enabled: using it must be a
    choice of the admin, as it locates addresses with third-party services.
    """
    available = base_plugins()["available"]
    assert set(available) == {RANDOM[0]}
    assert available[RANDOM[0]]["distribution"] == RANDOM[1]


@pytest.mark.parametrize(("plugin", "distribution"), [DISTANCE, RANDOM])
def test_plugins_installed(plugin: str, distribution: str) -> None:
    """The plugins are installed in the environment of the app.

    The image installs the dependencies of `pyproject.toml`, which pin the
    plugins by the release train `foca-plugins` (hashes are in `uv.lock`).
    """
    names = {ep.name: ep for ep in entry_points(group="foca.plugins")}
    dist = names[plugin].dist
    assert dist is not None
    assert dist.name == distribution
    for name in (distribution, "foca-plugins"):
        try:
            assert version(name)
        except PackageNotFoundError:  # pragma: no cover
            pytest.fail(f"{name} is not installed")


@pytest.fixture
def app(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    app_env: None,
) -> FlaskApp:
    """App with the plugins and the pipeline of the base config only.

    The overlay repeats the pipeline of the base config over that of the
    test overlay (ordered failover); the plugins are loaded as installed.
    Tests may set other settings of `plugins` with an indirect parameter
    (`OPT_IN`), which replace the pipeline.
    """
    plugins = getattr(request, "param", {"pipeline": base_plugins()["pipeline"]})
    overlay = tmp_path / "default-routing.yaml"
    overlay.write_text(yaml.safe_dump({"plugins": plugins}))
    set_overlays(monkeypatch, overlay)
    return init_app()


def pipeline_plugins(app: FlaskApp) -> tuple[set[str], set[str]]:
    """Get the plugins of the pipeline and the available ones of an app.

    Args:
        app: The app.

    Returns:
        The identifiers of the plugins of the pipeline and of those that
        are available.
    """
    with app.app.app_context():
        plugins = get_config().plugins
    assert plugins is not None
    return (
        {alt.plugin for s in plugins.pipeline for alt in s.alternatives},
        set(plugins.available),
    )


def test_app_loads_plugins(app: FlaskApp) -> None:
    """The app starts with the random routing plugin of the default pipeline."""
    in_pipeline, available = pipeline_plugins(app)
    assert in_pipeline == {RANDOM[0]}
    assert DISTANCE[0] not in available


@pytest.mark.parametrize(
    "task", [TASK, TASK_WITH_INPUT], ids=["no-inputs", "public-input"]
)
def test_default_routing_makes_no_lookups(
    client: FlaskClient,
    backend: type[FakeBackend],
    mongo_client: MongoClient[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    task: dict[str, Any],
) -> None:
    """Tasks go to a backend in random order, without any lookup.

    Also those with an input on a public address, which the distance plugin
    would look up at DB-IP: neither addresses nor host names are looked up.
    """
    monkeypatch.setattr(DbipProvider, "locate", fail_lookup)
    monkeypatch.setattr(socket, "getaddrinfo", fail_lookup)
    response = client.post(f"{BASE_PATH}/tasks", json=task)
    assert response.status_code == 200
    ((url, _),) = relayed(backend)
    assert url in BACKENDS
    tags = stored(mongo_client, response.get_json()["id"])["task"]["tags"]
    assert RANDOM[0] in tags["gateway.plugins"]
    assert DISTANCE[0] not in tags["gateway.plugins"]


@pytest.mark.parametrize("app", [OPT_IN], indirect=True)
def test_opt_in_loads_distance_plugin(app: FlaskApp) -> None:
    """Admins enable the lookups at DB-IP and Nominatim in the config."""
    in_pipeline, available = pipeline_plugins(app)
    assert in_pipeline == {DISTANCE[0], RANDOM[0]}
    assert DISTANCE[0] in available


@pytest.mark.parametrize("app", [OPT_IN], indirect=True)
def test_opt_in_makes_lookups(
    client: FlaskClient,
    backend: type[FakeBackend],
    mongo_client: MongoClient[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the opt-in, the address of an input is looked up (at DB-IP).

    The lookup is faked and finds nothing, so that the task goes on in random
    order.
    """
    lookups: list[str] = []

    def locate(self: DbipProvider, address: str, ctx: Any) -> None:
        """Record the address of a lookup, and find nothing."""
        lookups.append(address)

    monkeypatch.setattr(DbipProvider, "locate", locate)
    response = client.post(f"{BASE_PATH}/tasks", json=TASK_WITH_INPUT)
    assert response.status_code == 200
    assert lookups == [PUBLIC]
    ((url, _),) = relayed(backend)
    assert url in BACKENDS
    tags = stored(mongo_client, response.get_json()["id"])["task"]["tags"]
    assert RANDOM[0] in tags["gateway.plugins"]
