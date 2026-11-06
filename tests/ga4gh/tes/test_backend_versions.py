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
    _Probe,
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
        auths: Credentials asked for by each request (`None`: those of the
            backend).
    """

    served: ClassVar[Any] = info(version="1.1.0")
    calls: ClassVar[int] = 0
    auths: ClassVar[list[str | None]] = []

    def service_info(self, auth: str | None = None) -> Any:
        """Get the service info."""
        type(self).calls += 1
        type(self).auths.append(auth)
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
    monkeypatch.setattr(FakeClient, "auths", [])
    monkeypatch.setattr(backends, "client_for", lambda *_, **__: FakeClient())
    return FakeClient


@pytest.mark.parametrize(
    ("service_info", "expected"),
    [
        # as served by TESK, Poiesis and proTES
        ({"type": {"group": "org.ga4gh", "artifact": "TES", "version": 1.0}}, V10),
        (info(version="v1.1.0"), V11),
        (info(version="1.1.0"), V11),
        # as served by Funnel 0.11.14
        (info(version=""), None),
        # not a version of TES, e.g., of the implementation
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
    backend_versions._cache[URL] = _Probe(V10, 0.0)
    assert backend_version(BACKEND) == V11
    assert client.calls == 2


@pytest.mark.usefixtures("app_context")
def test_probe_failure_retried_later(client: type[FakeClient]) -> None:
    """Failures count as the oldest version, and are cached for a short time."""
    client.served = requests.ConnectionError("down")
    assert backend_version(BACKEND) == V10
    assert backend_version(BACKEND) == V10
    assert client.calls == 1
    backend_versions._cache[URL] = _Probe(None, 0.0)
    client.served = info(version="1.1")
    assert backend_version(BACKEND) == V11


@pytest.mark.usefixtures("app_context")
def test_probe_failure_keeps_known_version(client: type[FakeClient]) -> None:
    """A failed request doesn't make a backend count as another version."""
    client.served = {
        **info(version="1.1"),
        "tesResources_backend_parameters": ["a"],
    }
    assert backend_version(BACKEND) == V11
    backend_versions._cache[URL] = backend_versions._cache[URL]._replace(expires_at=0.0)
    client.served = requests.ConnectionError("down")
    assert backend_version(BACKEND) == V11
    assert client.calls == 2
    assert backend_versions.parameters() == ["a"]
    assert backend_versions.confirmed(BACKEND)
    assert backend_versions._cache[URL].expires_at > 0.0


@pytest.mark.usefixtures("app_context")
def test_probe_without_version_counts_as_oldest(client: type[FakeClient]) -> None:
    """An answer without a valid version replaces the one read before."""
    assert backend_version(BACKEND) == V11
    backend_versions._cache[URL] = backend_versions._cache[URL]._replace(expires_at=0.0)
    client.served = {"tesResources_backend_parameters": ["a"]}
    assert backend_version(BACKEND) == V10
    assert backend_versions.parameters() == ["a"]


@pytest.mark.usefixtures("app_context")
def test_probe_failure_keeps_version_read_meanwhile(
    client: type[FakeClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failures don't undo what another thread has read in the meantime."""

    def service_info(self: FakeClient, auth: str | None = None) -> Any:
        """Fail, after another thread has read the version."""
        backend_versions._cache[URL] = _Probe(V11, 0.0, ("a",))
        raise requests.ConnectionError("down")

    monkeypatch.setattr(client, "service_info", service_info)
    assert backend_version(BACKEND) == V11
    assert backend_versions.parameters() == ["a"]


@pytest.mark.usefixtures("app_context")
def test_eligible_reads_backends_never_read_first(
    client: type[FakeClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A backend that stays unavailable can't keep others from being read."""
    needs = {"executors": [{"image": "a", "command": ["b"], "ignore_error": True}]}
    down = backend_with("https://down.example.org")
    fine = backend_with(URL)
    read: list[str] = []
    now = [0.0]

    def probe(self: Any, backend: Backend) -> _Probe:
        """Take all the time if the backend is down."""
        read.append(backend.url)
        if backend is down:
            now[0] += backends.PROBE_TIMEOUT
            return _Probe(None, now[0] + backends.PROBE_RETRY)
        return _Probe(V11, now[0] + 3600)

    monkeypatch.setattr(backends.BackendVersions, "_probe", probe)
    monkeypatch.setattr(backends.time, "monotonic", lambda: now[0])
    backend_versions._cache[down.url] = _Probe(None, 0.0)
    assert backends.eligible_backends(needs, [down, fine]) == [fine]
    assert read == [URL, down.url]


@pytest.mark.usefixtures("app_context")
def test_confirmed(client: type[FakeClient]) -> None:
    """Versions count as confirmed if stated, or if the backend answered."""
    assert not backend_versions.confirmed(BACKEND)
    assert backend_versions.confirmed(
        Backend(id=URL, url=URL, labels={"tes.version": "1.0"})
    )
    client.served = requests.ConnectionError("down")
    backend_version(BACKEND)
    assert not backend_versions.confirmed(BACKEND)
    backend_versions.clear()
    client.served = {"tesResources_backend_parameters": ["a"]}
    assert backend_version(BACKEND) == V10
    assert backend_versions.confirmed(BACKEND)
    assert backend_versions.parameters() == ["a"]


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
    backend_versions._cache[URL] = _Probe(V11, 0.0)
    assert backend_version(BACKEND, probe=False) == V11
    assert client.calls == 0


@pytest.mark.usefixtures("app_context")
def test_probe_anonymous_by_default(client: type[FakeClient]) -> None:
    """Probes send no credentials, unless `probe_with_token` is set."""
    assert not get_custom_config(CustomConfig).tes.probe_with_token
    backend_version(BACKEND)
    get_custom_config(CustomConfig).tes.probe_with_token = True
    backend_versions.clear()
    backend_version(BACKEND)
    assert client.auths == ["none", None]


def refused(status: int) -> requests.HTTPError:
    """Get the error of a response with a status."""
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(str(status), response=response)


@pytest.mark.usefixtures("app_context")
@pytest.mark.parametrize("status", [401, 403])
def test_probe_refused_hints(
    client: type[FakeClient], caplog: pytest.LogCaptureFixture, status: int
) -> None:
    """Refused anonymous probes name the label and the setting."""
    backend = Backend(id=URL, url=URL, auth="client_credentials")
    client.served = refused(status)
    assert backend_version(backend) == V10
    assert "tes.version" in caplog.text
    assert "probe_with_token" in caplog.text
    caplog.clear()
    get_custom_config(CustomConfig).tes.probe_with_token = True
    backend_versions.clear()
    backend_version(backend)
    assert "probe_with_token" not in caplog.text


@pytest.mark.usefixtures("app_context")
def test_probe_failure_no_hint(
    client: type[FakeClient], caplog: pytest.LogCaptureFixture
) -> None:
    """Other failures of anonymous probes get no hint at credentials."""
    client.served = refused(404)
    backend_version(BACKEND)
    assert "probe_with_token" not in caplog.text


@pytest.mark.usefixtures("app_context")
def test_probe_refused_no_credentials(
    client: type[FakeClient], caplog: pytest.LogCaptureFixture
) -> None:
    """Backends without credentials are only hinted at the label."""
    client.served = refused(403)
    backend_version(Backend(id=URL, url=URL, auth="none"))
    assert "tes.version" in caplog.text
    assert "probe_with_token" not in caplog.text


def test_probe_without_config(
    client: type[FakeClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the config, the version is the oldest one, as for failures."""

    def no_config(*_: Any, **__: Any) -> Any:
        """Fail, as outside of an app context."""
        raise RuntimeError("no app context")

    monkeypatch.setattr(backends, "get_custom_config", no_config)
    assert backend_version(BACKEND) == V10
    assert client.calls == 0


def test_probe_without_basic_credentials(
    app: FlaskApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Anonymous probes send no Basic credentials either."""
    kwargs_sent: list[dict[str, Any]] = []

    def request(method: str, url: str, timeout: Any, **kwargs: Any) -> Any:
        """Record the arguments; serve a service info."""
        kwargs_sent.append(kwargs)
        response = requests.Response()
        response.status_code = 200
        response._content = b"{}"
        return response

    monkeypatch.setattr(requests, "request", request)
    monkeypatch.setenv("TES_USER", "protes")
    monkeypatch.setenv("TES_PASSWORD", "secret")
    backend = Backend(id=URL, url=URL, auth="basic")
    with app.app.app_context():
        get_custom_config(CustomConfig).tes.backends = tes_backends(
            URL,
            auth="basic",
            basic={"username_env": "TES_USER", "password_env": "TES_PASSWORD"},
        )
        backend_version(backend)
    assert "auth" not in kwargs_sent[0]


@pytest.mark.parametrize("with_token", [False, True])
def test_probe_credentials_sent(
    app: FlaskApp, monkeypatch: pytest.MonkeyPatch, with_token: bool
) -> None:
    """Probes send proTES' token only with `probe_with_token`."""
    headers: list[Any] = []
    tokens: list[str] = []

    def request(method: str, url: str, timeout: Any, **kwargs: Any) -> Any:
        """Record the headers; serve a service info."""
        headers.append(kwargs["headers"])
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"type": {"group": "org.ga4gh", "artifact": "tes"}}'
        return response

    def get_token(*_: Any, **__: Any) -> str:
        """Record that a token was fetched."""
        tokens.append("token-1")
        return "token-1"

    monkeypatch.setattr(requests, "request", request)
    monkeypatch.setattr(backends.token_provider, "get_token", get_token)
    backend = Backend(id=URL, url=URL, auth="client_credentials")
    with app.app.app_context():
        config = get_custom_config(CustomConfig).tes
        config.backends = tes_backends(URL)
        config.probe_with_token = with_token
        backend_version(backend)
    sent = headers[0]
    if with_token:
        assert sent["Authorization"] == "Bearer token-1"
    else:
        assert "Authorization" not in sent
        assert not tokens


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


def backend_with(url: str) -> Backend:
    """Get a backend without a label."""
    return Backend(id=url, url=url)


@pytest.mark.usefixtures("app_context")
def test_parameters_known(client: type[FakeClient]) -> None:
    """Parameters come from what was read, without reading service infos."""
    assert backend_versions.parameters() == []
    assert client.calls == 0
    client.served = {
        **info(version="1.1"),
        "tesResources_backend_parameters": ["b", "", "a", 1],
    }
    backend_version(BACKEND)
    assert backend_versions.parameters() == ["a", "b"]


@pytest.mark.usefixtures("app_context")
def test_eligible(client: type[FakeClient], monkeypatch: pytest.MonkeyPatch) -> None:
    """Tasks that need TES 1.1 go to its backends; others need no service info."""
    new, old = (
        backend_with(URL),
        Backend(id="o", url="https://o.example.org", labels={"tes.version": "1.0"}),
    )
    plain = {"executors": [{"image": "a", "command": ["b"]}]}
    assert backends.eligible_backends(plain, [new, old]) == [new, old]
    assert client.calls == 0
    needs = {"executors": [{"image": "a", "command": ["b"], "ignore_error": True}]}
    assert backends.eligible_backends(needs, [new, old]) == [new]
    assert client.calls == 1
    assert backends.eligible_backends("no task", [old]) == [old]


@pytest.mark.usefixtures("app_context")
def test_eligible_probes_bounded(
    client: type[FakeClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once the time is up, backends count with what is cached."""
    needs = {"executors": [{"image": "a", "command": ["b"], "ignore_error": True}]}
    times = iter([0.0, 1.0, 1000.0, 1000.0, 1000.0])
    monkeypatch.setattr(backends.time, "monotonic", lambda: next(times))
    first, second = backend_with(URL), backend_with("https://b.example.org")
    assert backends.eligible_backends(needs, [first, second]) == [first]
    assert client.calls == 1


def test_get_task_upgraded(app: FlaskApp, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tasks of backends are upgraded in the version of the backend."""
    upgraded: list[TesVersion] = []

    def upgrade(task: dict[str, Any], version: TesVersion) -> dict[str, Any]:
        """Record the version."""
        upgraded.append(version)
        return {**task, "state": "RUNNING"}

    def request(method: str, url: str, timeout: Any, **kwargs: Any) -> Any:
        """Serve a task."""
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"id": "a", "state": "QUEUED", "executors": []}'
        return response

    monkeypatch.setattr(requests, "request", request)
    monkeypatch.setattr(backends, "upgrade_task", upgrade)
    backend = Backend(id=URL, url=URL, auth="none", labels={"tes.version": "1.0"})
    with app.app.app_context():
        get_custom_config(CustomConfig).tes.backends = tes_backends(URL)
        task = backends.client_for(backend).get_task("a")
    assert task.state == "RUNNING"
    assert upgraded == [V10]


def test_get_task_reads_no_service_info(
    app: FlaskApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reading a task takes one request, also if no version is cached."""
    urls: list[str] = []

    def request(method: str, url: str, timeout: Any, **kwargs: Any) -> Any:
        """Serve a task."""
        urls.append(url)
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"id": "a", "state": "QUEUED", "executors": []}'
        return response

    monkeypatch.setattr(requests, "request", request)
    with app.app.app_context():
        get_custom_config(CustomConfig).tes.backends = tes_backends(URL)
        backends.client_for(BACKEND).get_task("a")
    assert len(urls) == 1
