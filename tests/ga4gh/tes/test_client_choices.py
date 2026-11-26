"""Tests of the choices of clients per task (#218).

Clients choose the pipeline of a task (tag `gateway.pipeline`, a preset or
a pipeline as JSON) and its TES instances (tag `gateway.backends`); FOCA's
plugin engine validates both. `GET /plugins` and `GET /plugins/{plugin_id}`
(FOCA's plugin listing, under the TES base path) tell authenticated users
what they may choose, and the service info tells everyone where to look.
"""

import json
from typing import Any

from flask.testing import FlaskClient
from foca import get_custom_config
from foca.plugins.backends import BackendListConfig
from pymongo import MongoClient
import pytest
import requests
import tes

from pro_tes.config_models import CustomConfig
from tests.conftest import ADMIN, DB, OUTSIDER, make_headers
from tests.ga4gh.tes.conftest import (
    BACKENDS,
    BASE_PATH,
    TASK,
    FakeBackend,
    PluginClient,
    relayed,
    stored,
)
from tests.ga4gh.tes.test_plugins import Rename, step
from tests.ga4gh.tes.test_server import NEEDS_11

# Plugins of the tests, by identifier
PLUGINS = {"test.rename": Rename}
# Settings of `plugins` that let clients choose pipelines, with a preset
CHOICES: dict[str, Any] = {
    "presets": {"renamed": [step("test.rename")]},
    "client_pipelines": {"enabled": True},
}


def submit(client: FlaskClient, **tags: str) -> Any:
    """Submit `TASK` with tags.

    Args:
        client: Test client.
        **tags: Tags of the task, with `__` for `.` in the keys (e.g.,
            `gateway__pipeline`).

    Returns:
        The response.
    """
    task = {**TASK, "tags": {key.replace("__", "."): val for key, val in tags.items()}}
    return client.post(f"{BASE_PATH}/tasks", json=task)


def test_preset(
    with_plugins: PluginClient,
    backend: type[FakeBackend],
    mongo_client: MongoClient[dict[str, Any]],
) -> None:
    """Clients choose a preset by name; it runs for their task only."""
    client = with_plugins(PLUGINS, **CHOICES)
    response = submit(client, gateway__pipeline="renamed")
    assert response.status_code == 200
    assert submit(client).status_code == 200
    (_, chosen), (_, default) = relayed(backend)
    assert chosen.name == "renamed"
    assert default.name == TASK["name"]
    assert chosen.tags is None or not any(
        key.startswith("gateway.") for key in chosen.tags
    )
    tags = stored(mongo_client, response.get_json()["id"])["task"]["tags"]
    assert tags["gateway.pipeline"] == "renamed"
    assert tags["gateway.pipeline_source"] == "preset:renamed"


def test_inline_pipeline(
    with_plugins: PluginClient,
    backend: type[FakeBackend],
    mongo_client: MongoClient[dict[str, Any]],
) -> None:
    """Clients send a pipeline as JSON, framed by FOCA's route step."""
    client = with_plugins(PLUGINS, **CHOICES)
    pipeline = json.dumps([step("test.rename")])
    response = submit(client, gateway__pipeline=pipeline)
    assert response.status_code == 200
    ((url, task),) = relayed(backend)
    assert url == BACKENDS[0]
    assert task.name == "renamed"
    tags = stored(mongo_client, response.get_json()["id"])["task"]["tags"]
    assert tags["gateway.pipeline_source"] == "client"


@pytest.mark.parametrize(
    ("pipeline", "detail"),
    [
        ("unknown", "preset"),
        (json.dumps([step("test.unknown")]), "not available"),
        ("[", "JSON"),
    ],
)
def test_invalid_pipeline(
    with_plugins: PluginClient,
    backend: type[FakeBackend],
    mongo_client: MongoClient[dict[str, Any]],
    pipeline: str,
    detail: str,
) -> None:
    """Unknown presets and plugins and invalid JSON are rejected (`400`)."""
    client = with_plugins(PLUGINS, **CHOICES)
    response = submit(client, gateway__pipeline=pipeline)
    assert response.status_code == 400
    assert detail.lower() in response.get_json()["detail"].lower()
    assert not relayed(backend)
    assert mongo_client["taskStore"]["tasks"].count_documents({}) == 0


def test_plugin_not_for_role(
    with_plugins: PluginClient, backend: type[FakeBackend]
) -> None:
    """Plugins only for other roles are not available to the caller (`400`)."""
    client = with_plugins(
        PLUGINS,
        available={"test.rename": {"roles": ["admin"]}},
        client_pipelines={"enabled": True},
    )
    response = submit(client, gateway__pipeline=json.dumps([step("test.rename")]))
    assert response.status_code == 400
    assert "not available" in response.get_json()["detail"]
    assert not relayed(backend)


def test_reserved_tag(with_plugins: PluginClient, backend: type[FakeBackend]) -> None:
    """Other tags under `gateway.` are rejected (`400`)."""
    client = with_plugins(PLUGINS, **CHOICES)
    assert submit(client, gateway__other="x").status_code == 400
    assert not relayed(backend)


def test_pipelines_disabled(
    with_plugins: PluginClient, backend: type[FakeBackend]
) -> None:
    """Without `client_pipelines.enabled`, the tag is rejected (`400`)."""
    client = with_plugins(PLUGINS, presets=CHOICES["presets"])
    assert submit(client, gateway__pipeline="renamed").status_code == 400
    assert not relayed(backend)


@pytest.mark.parametrize(
    ("selection", "used"),
    [
        ({"type": "order", "backends": [BACKENDS[1]]}, BACKENDS[1]),
        ({"type": "forbid", "backends": [BACKENDS[0]]}, BACKENDS[1]),
        ({"type": "allow", "backends": [BACKENDS[1]]}, BACKENDS[1]),
    ],
)
def test_backends(
    with_plugins: PluginClient,
    backend: type[FakeBackend],
    selection: dict[str, Any],
    used: str,
) -> None:
    """Clients order, restrict or exclude TES instances."""
    client = with_plugins({})
    response = submit(client, gateway__backends=json.dumps(selection))
    assert response.status_code == 200
    ((url, _),) = relayed(backend)
    assert url == used


# Routes in the order of the TES instances, so that pins show
ORDERED = [{"id": "route", "alternatives": [{"plugin": "foca.routing.ordered"}]}]


def named(client: FlaskClient) -> None:
    """Name the TES instances of an app `site-1` and `site-2` (`id`).

    Args:
        client: Test client of the app.
    """
    with client.application.app_context():
        get_custom_config(CustomConfig).tes.backends = BackendListConfig.model_validate(
            [
                {"type": "ga4gh-tes", "url": url, "auth": "none", "id": f"site-{n}"}
                for n, url in enumerate(BACKENDS, start=1)
            ]
        )


@pytest.mark.parametrize("pin", ["site-2", BACKENDS[1], f"{BACKENDS[1].upper()}/"])
def test_pin_to_one_tes_instance(
    with_plugins: PluginClient, backend: type[FakeBackend], pin: str
) -> None:
    """Clients pin tasks to a TES instance by its `id` or its URL.

    URLs are compared normalized. The pipeline routes in order, so that an
    unpinned task goes to the first TES instance, and a pinned one to the
    second shows the pin.
    """
    client = with_plugins({}, pipeline=ORDERED)
    named(client)
    assert submit(client).status_code == 200
    selection = json.dumps({"type": "allow", "backends": [pin]})
    assert submit(client, gateway__backends=selection).status_code == 200
    assert [url for url, _ in relayed(backend)] == [BACKENDS[0], BACKENDS[1]]


@pytest.mark.parametrize(
    ("pin", "status", "problem"),
    [
        ("site-3", 400, "invalid-backends"),
        ("https://tes3.example.org", 503, "no-backend-available"),
    ],
)
def test_pin_to_unknown_tes_instance(
    with_plugins: PluginClient,
    backend: type[FakeBackend],
    mongo_client: MongoClient[dict[str, Any]],
    pin: str,
    status: int,
    problem: str,
) -> None:
    """A pin to an unknown TES instance fails: no task, no fallback.

    A pin by an identifier that names no TES instance is refused (`400`,
    `invalid-backends`, FOCA 0.23); one by an unknown URL leaves no TES
    instance (`503`, `no-backend-available`).
    """
    client = with_plugins({})
    named(client)
    selection = json.dumps({"type": "allow", "backends": [pin]})
    response = submit(client, gateway__backends=selection)
    assert response.status_code == status
    assert response.get_json()["problem_type"].endswith(f"#{problem}")
    assert not relayed(backend)
    assert mongo_client[DB]["tasks"].count_documents({}) == 0


def test_pin_to_ineligible_tes_instance(
    with_plugins: PluginClient,
    backend: type[FakeBackend],
    mongo_client: MongoClient[dict[str, Any]],
) -> None:
    """A pin by `id` to a TES instance that can't run the task gets `400`.

    The instance is known, though left out for its TES version: the reason
    is the task (proTES's problem type `tes-versions/#routing`, naming the
    field), not an unknown choice (`invalid-backends`, FOCA 0.23 without
    `known`).
    """
    client = with_plugins({})
    named(client)
    backend.versions[BACKENDS[0]] = "1.0"
    selection = json.dumps({"type": "allow", "backends": ["site-1"]})
    response = client.post(
        f"{BASE_PATH}/tasks",
        json={**NEEDS_11, "tags": {"gateway.backends": selection}},
    )
    assert response.status_code == 400
    body = response.get_json()
    assert body["problem_type"].endswith("/tes-versions/#routing")
    assert "ignore_error" in body["detail"]
    assert not relayed(backend)
    assert mongo_client[DB]["tasks"].count_documents({}) == 0


@pytest.mark.parametrize("status", [None, 400])
def test_pin_to_refusing_tes_instance(
    with_plugins: PluginClient,
    backend: type[FakeBackend],
    mongo_client: MongoClient[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    status: int | None,
) -> None:
    """A pin to a TES instance that doesn't accept the task fails (`503`).

    Whether it refuses with a status (`400`) or without a response, the
    other TES instance gets no request, and no task is stored.
    """
    client = with_plugins({}, pipeline=ORDERED)
    named(client)
    if status is None:
        backend.failing.add(BACKENDS[1])
    else:
        create_task = backend.create_task

        def refuse(self: FakeBackend, task: tes.Task) -> str:
            """Refuse tasks at the second TES instance with `status`."""
            if self.url == BACKENDS[1]:
                response = requests.Response()
                response.status_code = status
                raise requests.HTTPError(str(status), response=response)
            return create_task(self, task)

        monkeypatch.setattr(backend, "create_task", refuse)
    selection = json.dumps({"type": "allow", "backends": ["site-2"]})
    assert submit(client, gateway__backends=selection).status_code == 503
    assert not relayed(backend)
    assert mongo_client[DB]["tasks"].count_documents({}) == 0


def test_backends_unknown(
    with_plugins: PluginClient,
    backend: type[FakeBackend],
    mongo_client: MongoClient[dict[str, Any]],
) -> None:
    """Unknown TES instances are never used, but recorded in the provenance."""
    client = with_plugins({})
    unknown = "https://unknown.example.org/"
    selection = {"type": "order", "backends": [unknown]}
    response = submit(client, gateway__backends=json.dumps(selection))
    assert response.status_code == 200
    ((url, _),) = relayed(backend)
    assert url == BACKENDS[0]
    document = stored(mongo_client, response.get_json()["id"])
    selection = document["provenance"]["selection"]
    assert [url.rstrip("/") for url in selection["discarded"]] == [unknown.rstrip("/")]


@pytest.mark.parametrize(
    "selection",
    ["[", json.dumps({"type": "first", "backends": []}), json.dumps([BACKENDS[0]])],
)
def test_backends_invalid(
    with_plugins: PluginClient, backend: type[FakeBackend], selection: str
) -> None:
    """Invalid selections of TES instances are rejected (`400`)."""
    client = with_plugins({})
    assert submit(client, gateway__backends=selection).status_code == 400
    assert not relayed(backend)


def test_backends_disabled(
    with_plugins: PluginClient, backend: type[FakeBackend]
) -> None:
    """Without `client_backends.enabled`, the tag is rejected (`400`)."""
    client = with_plugins({}, client_backends={"enabled": False})
    selection = json.dumps({"type": "allow", "backends": [BACKENDS[1]]})
    assert submit(client, gateway__backends=selection).status_code == 400
    assert not relayed(backend)


def test_list_plugins(with_plugins: PluginClient) -> None:
    """Users list the plugins and presets they may choose."""
    client = with_plugins(PLUGINS, **CHOICES)
    response = client.get(f"{BASE_PATH}/plugins")
    assert response.status_code == 200
    listing = response.get_json()
    # FOCA's built-in plugins are available, too
    assert {plugin["id"] for plugin in listing["plugins"]} >= {"test.rename"}
    assert [preset["name"] for preset in listing["presets"]] == ["renamed"]
    assert listing["client_pipelines"]["allowed_for_caller"] is True
    assert listing["tag_prefix"] == "gateway."
    response = client.get(f"{BASE_PATH}/plugins/test.rename")
    assert response.status_code == 200
    assert response.get_json()["id"] == "test.rename"
    assert client.get(f"{BASE_PATH}/plugins/test.unknown").status_code == 404


class StoringRename(Rename):
    """`Rename` that declares stored data (`data_stored`, FOCA 0.19)."""

    data_stored = ("names of renamed tasks",)


def test_list_plugins_storage(with_plugins: PluginClient) -> None:
    """Plugins are listed with their stored data and the mode of their store.

    With the app's database, plugins share their store (`shared`), unless
    their store is turned off (`off`, `plugins.available.<id>.store`).
    """
    client = with_plugins(
        {"test.rename": Rename, "test.storing": StoringRename},
        **CHOICES,
        available={"test.rename": {"store": {"enabled": False}}, "test.storing": {}},
    )
    listing = client.get(f"{BASE_PATH}/plugins").get_json()
    plugins = {plugin["id"]: plugin for plugin in listing["plugins"]}
    assert plugins["test.rename"]["data_stored"] == []
    assert plugins["test.rename"]["storage"] == "off"
    assert plugins["test.storing"]["data_stored"] == ["names of renamed tasks"]
    assert plugins["test.storing"]["storage"] == "shared"
    plugin = client.get(f"{BASE_PATH}/plugins/test.storing").get_json()
    assert plugin["storage"] == "shared"


def test_list_plugins_disabled(with_plugins: PluginClient) -> None:
    """Without client pipelines, users may choose no plugins."""
    client = with_plugins(PLUGINS)
    listing = client.get(f"{BASE_PATH}/plugins").get_json()
    assert listing["plugins"] == []
    assert listing["client_pipelines"]["allowed_for_caller"] is False
    assert listing["client_backends"]["enabled"] is True


@pytest.mark.parametrize("path", ["plugins", "plugins/test.rename"])
def test_list_plugins_access(with_plugins: PluginClient, path: str) -> None:
    """Callers need an access token and the role `user`.

    Admins without the role `user` get `403`, too.
    """
    client = with_plugins(PLUGINS, **CHOICES)
    url = f"{BASE_PATH}/{path}"
    assert client.get(url, headers={"Authorization": ""}).status_code == 401
    assert client.get(url, headers=make_headers(OUTSIDER)).status_code == 403
    assert client.get(url, headers=make_headers(ADMIN)).status_code == 403


def test_service_info(with_plugins: PluginClient) -> None:
    """The service info tells where the plugins are listed, without plugins."""
    client = with_plugins(PLUGINS, **CHOICES)
    response = client.get(f"{BASE_PATH}/service-info", headers={"Authorization": ""})
    assert response.status_code == 200
    gateway = response.get_json()["gateway"]
    assert gateway["plugins_url"] == f"{BASE_PATH}/plugins"
    assert gateway["client_pipelines"] is True
    assert gateway["client_backends"] is True
    assert "test.rename" not in json.dumps(gateway)
