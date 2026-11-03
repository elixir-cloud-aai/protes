"""Tests of the TES versions of backends."""

from collections.abc import Iterator
from typing import Any, ClassVar

from connexion import FlaskApp
from foca import get_custom_config
from foca.plugins import Backend
import pydantic
import pytest
import requests

from pro_tes.config_models import CustomConfig, Tes
from pro_tes.ga4gh.tes import backends
from pro_tes.ga4gh.tes.backends import (
    backend_version,
    backend_versions,
    reported_version,
)
from pro_tes.ga4gh.tes.versions import TesVersion
from tests.ga4gh.tes.conftest import tes_backends

URL = "https://tes.example.org"
V10 = TesVersion(1, 0)
V11 = TesVersion(1, 1)
BACKEND = Backend(id=URL, url=URL)


def info(**service_type: Any) -> dict[str, Any]:
    """Get a service info with a type."""
    return {"type": {"group": "org.ga4gh", "artifact": "tes", **service_type}}


class FakeClient:
    """Fake client of a backend, serving a service info.

    Attributes:
        served: Service info, or the error to raise.
        calls: Number of requests.
    """

    served: ClassVar[Any] = info(version="1.1.0")
    calls: ClassVar[int] = 0

    def service_info(self) -> Any:
        """Get the service info."""
        type(self).calls += 1
        if isinstance(self.served, Exception):
            raise self.served
        return self.served


@pytest.fixture
def app_context(app: FlaskApp) -> Iterator[None]:
    """Run a test in an app context."""
    with app.app.app_context():
        yield


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> type[FakeClient]:
    """Backends get the fake client."""
    monkeypatch.setattr(FakeClient, "served", info(version="1.1.0"))
    monkeypatch.setattr(FakeClient, "calls", 0)
    monkeypatch.setattr(backends, "client_for", lambda *_, **__: FakeClient())
    return FakeClient


@pytest.mark.parametrize(
    ("service_info", "expected"),
    [
        # as served by TESK, Poiesis and proTES
        ({"type": {"group": "org.ga4gh", "artifact": "TES", "version": 1.0}}, V10),
        (info(version="v1.1.0"), V11),
        (info(version="1.1.0"), V11),
        # Funnel: its own version
        (info(version="0.12.0"), None),
        (info(version="2.0.0"), None),
        ({"type": {"group": "org.ga4gh", "artifact": "wes", "version": "1.1"}}, None),
        ({"type": {"group": "org.example", "artifact": "tes", "version": "1.1"}}, None),
        ({"type": {"artifact": "tes", "version": "1.1"}}, None),
        ({"type": "tes"}, None),
        ({}, None),
        ([], None),
    ],
)
def test_reported_version(service_info: Any, expected: TesVersion | None) -> None:
    """Service infos are read leniently; other versions count as none."""
    assert reported_version(service_info) == expected


def test_label_wins(client: type[FakeClient]) -> None:
    """Backends with a label are not probed."""
    backend = Backend(id=URL, url=URL, labels={"tes.version": "1.0"})
    assert backend_version(backend) == V10
    assert client.calls == 0


@pytest.mark.usefixtures("app_context")
def test_invalid_label_probed(client: type[FakeClient]) -> None:
    """Labels are checked on startup; invalid ones here count as none."""
    backend = Backend(id=URL, url=URL, labels={"tes.version": "x"})
    assert backend_version(backend) == V11


@pytest.mark.usefixtures("app_context")
def test_probe_cached(client: type[FakeClient]) -> None:
    """The service info is read once, until the cached version expires."""
    assert backend_version(BACKEND) == V11
    assert backend_version(BACKEND) == V11
    assert client.calls == 1
    backend_versions._cache[URL] = (V10, 0.0)
    assert backend_version(BACKEND) == V11
    assert client.calls == 2


@pytest.mark.usefixtures("app_context")
def test_probe_failure_retried_later(client: type[FakeClient]) -> None:
    """Failures count as the oldest version, and are cached for a short time."""
    client.served = requests.ConnectionError("down")
    assert backend_version(BACKEND) == V10
    assert backend_version(BACKEND) == V10
    assert client.calls == 1
    backend_versions._cache[URL] = (None, 0.0)
    client.served = info(version="1.1")
    assert backend_version(BACKEND) == V11


@pytest.mark.usefixtures("app_context")
def test_probe_failure_keeps_known_version(client: type[FakeClient]) -> None:
    """A failed request doesn't make a backend count as another version."""
    assert backend_version(BACKEND) == V11
    backend_versions._cache[URL] = (V11, 0.0)
    client.served = requests.ConnectionError("down")
    assert backend_version(BACKEND) == V11
    assert client.calls == 2
    assert backend_versions._cache[URL][1] > 0.0


@pytest.mark.usefixtures("app_context")
def test_probe_unknown_warns(
    client: type[FakeClient], caplog: pytest.LogCaptureFixture
) -> None:
    """Unknown versions count as the oldest one, with a warning on the label."""
    client.served = info(version="0.12.0")
    assert backend_version(BACKEND) == V10
    assert "tes.version" in caplog.text


@pytest.mark.usefixtures("app_context")
def test_no_probe(client: type[FakeClient]) -> None:
    """Without probing, cached versions count, also expired ones."""
    assert backend_version(BACKEND, probe=False) == V10
    assert client.calls == 0
    backend_versions._cache[URL] = (V11, 0.0)
    assert backend_version(BACKEND, probe=False) == V11
    assert client.calls == 0


def test_service_info_request(app: FlaskApp, monkeypatch: pytest.MonkeyPatch) -> None:
    """The service info is read from the API path, with the given timeout."""
    calls: list[tuple[str, str, Any]] = []

    def request(method: str, url: str, timeout: Any, **kwargs: Any) -> Any:
        """Record the request; serve a service info."""
        calls.append((method, url, timeout))
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"type": {}}'
        return response

    monkeypatch.setattr(requests, "request", request)
    with app.app.app_context():
        get_custom_config(CustomConfig).tes.backends = tes_backends(URL)
        served = backends.client_for(
            Backend(id=URL, url=URL, auth="none"), timeout=3
        ).service_info()
    assert served == {"type": {}}
    assert calls == [("GET", f"{URL}/ga4gh/tes/v1/service-info", (3, 3))]


def test_label_validated() -> None:
    """Labels of unsupported versions stop the app."""
    entry = {
        "type": "ga4gh-tes",
        "url": URL,
        "auth": "none",
        "labels": {"tes.version": "1.1"},
    }
    assert Tes.model_validate({"backends": [entry]}).backends.root[0].labels == {
        "tes.version": "1.1"
    }
    with pytest.raises(pydantic.ValidationError, match=r"tes\.version"):
        Tes.model_validate({"backends": [{**entry, "labels": {"tes.version": "0.12"}}]})
