"""Tests of the default routing: distance, with random order as the fallback.

The image ships the routing plugins of foca-plugins (`tes.routing.distance`
and `generic.routing.random`), and the base config uses them as the default
pipeline, as proTES did with its former middlewares (#379). The test
overlay of the other tests sets FOCA's ordered failover instead, so that they
are independent of the plugins; these tests run the app with the pipeline of
the base config and the installed plugins, without network access.
"""

from importlib.metadata import PackageNotFoundError, entry_points, version
from pathlib import Path
from typing import Any

from connexion import FlaskApp
from flask.testing import FlaskClient
from foca import get_config
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
# Plugins of the default pipeline, with their distributions
DISTANCE = ("tes.routing.distance", "foca-plugin-tes-routing-distance")
RANDOM = ("generic.routing.random", "foca-plugin-generic-routing-random")


def base_plugins() -> dict[str, Any]:
    """Get the section `plugins` of the base config.

    Returns:
        The section.
    """
    plugins: dict[str, Any] = yaml.safe_load(BASE_CONFIG.read_text())["plugins"]
    return plugins


def test_default_pipeline() -> None:
    """The default pipeline tries distance routing, then random order."""
    plugins = base_plugins()
    (step,) = plugins["pipeline"]
    assert step["id"] == "route"
    assert [alt["plugin"] for alt in step["alternatives"]] == [
        DISTANCE[0],
        RANDOM[0],
    ]
    assert not plugins.get("client_pipelines", {}).get("enabled", False)


def test_default_plugins_available() -> None:
    """The plugins are pinned to their distributions.

    The distance plugin locates addresses with DB-IP and Nominatim, as the
    former middleware did (the offline provider needs a GeoIP database that
    the image doesn't contain).
    """
    available = base_plugins()["available"]
    assert set(available) == {DISTANCE[0], RANDOM[0]}
    for plugin, distribution in (DISTANCE, RANDOM):
        assert available[plugin]["distribution"] == distribution
    assert available[DISTANCE[0]]["config"] == {"provider": "dbip"}


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
def app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, app_env: None) -> FlaskApp:
    """App with the plugins and the pipeline of the base config only.

    The overlay repeats the pipeline of the base config over that of the
    test overlay (ordered failover); the plugins are loaded as installed.
    """
    overlay = tmp_path / "default-routing.yaml"
    pipeline = base_plugins()["pipeline"]
    overlay.write_text(yaml.safe_dump({"plugins": {"pipeline": pipeline}}))
    set_overlays(monkeypatch, overlay)
    return init_app()


def test_app_loads_plugins(app: FlaskApp) -> None:
    """The app starts with the installed plugins of the default pipeline."""
    with app.app.app_context():
        plugins = get_config().plugins
    assert plugins is not None
    assert {alt.plugin for s in plugins.pipeline for alt in s.alternatives} == {
        DISTANCE[0],
        RANDOM[0],
    }


@pytest.mark.parametrize(
    "task",
    [
        # no inputs: distance routing doesn't apply
        TASK,
        # inputs that can't be located without network access
        {
            **TASK,
            "inputs": [
                {"url": "https://data.example.org/in", "path": "/in", "type": "FILE"}
            ],
        },
    ],
    ids=["no-inputs", "unlocated-input"],
)
def test_default_routing_falls_back(
    client: FlaskClient,
    backend: type[FakeBackend],
    mongo_client: MongoClient[dict[str, Any]],
    task: dict[str, Any],
) -> None:
    """Tasks that distance routing can't place go to a backend in random order.

    No lookups are made (the tests block network access): the next
    alternative routes, so that the task is forwarded.
    """
    response = client.post(f"{BASE_PATH}/tasks", json=task)
    assert response.status_code == 200
    ((url, _),) = relayed(backend)
    assert url in BACKENDS
    tags = stored(mongo_client, response.get_json()["id"])["task"]["tags"]
    assert RANDOM[0] in tags["gateway.plugins"]
