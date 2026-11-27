"""Tests of API keys of TES backends (`auth: api_key`)."""

from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from pathlib import Path
import threading
from typing import Any, ClassVar

from connexion import FlaskApp
from foca import get_custom_config
from foca.plugins import Backend, Headers, OutboundRequest
from foca.plugins.backends import BackendListConfig
import pydantic
import pytest
import requests
import tes
import yaml

from pro_tes.app import init_app
from pro_tes.config_models import (
    API_KEY_HEADER,
    ApiKey,
    ClientCredentials,
    CustomConfig,
    Tes,
    TesServiceEntry,
)
from pro_tes.exceptions import BackendAuthError
from pro_tes.ga4gh.tes import backends
from pro_tes.ga4gh.tes.backends import backend_versions
from pro_tes.gateway import RESERVED_HEADERS
from pro_tes.security import check_backends
from tests.conftest import set_overlays

BACKEND_URL = "https://tes.example.org"
ISSUER = "https://idp.example.org/oidc/"
KEY_ENV = "TES_API_KEY"
# Fictitious API key, in the format of Poiesis
KEY = "poi_k7H2pQ-fictitious-key"  # gitleaks:allow (fictitious)


def tes_config(*entries: dict[str, Any]) -> Tes:
    """Get a TES backend configuration with entries.

    Args:
        *entries: Entries of the backend list.

    Returns:
        The configuration.
    """
    return Tes(
        backends=list(entries),  # type: ignore[arg-type]
        client_credentials=ClientCredentials(issuer=ISSUER),
    )


def key_entry(url: str = BACKEND_URL, **api_key: Any) -> dict[str, Any]:
    """Get an entry of a TES backend with an API key.

    Args:
        url: URL of the backend.
        **api_key: Settings of the API key, besides `key_env`.

    Returns:
        The entry.
    """
    return {
        "type": "ga4gh-tes",
        "url": url,
        "url_schemes": ["https", "http"],
        "auth": "api_key",
        "api_key": {"key_env": KEY_ENV, **api_key},
    }


def test_config_default_header() -> None:
    """Backends with API keys send them as bearer tokens by default."""
    (entry,) = tes_config(key_entry()).backends.root
    assert isinstance(entry, TesServiceEntry)
    assert entry.auth == "api_key"
    assert entry.api_key == ApiKey(key_env=KEY_ENV)
    assert entry.api_key.header == API_KEY_HEADER == "Authorization"
    assert not tes_config(key_entry()).uses_tokens


def test_config_custom_header() -> None:
    """Backends may get their API keys in a header of their own."""
    (entry,) = tes_config(key_entry(header="X-API-Key")).backends.root
    assert isinstance(entry, TesServiceEntry)
    assert entry.api_key is not None
    assert entry.api_key.header == "X-API-Key"


@pytest.mark.parametrize(
    "entry",
    [
        {"type": "ga4gh-tes", "url": BACKEND_URL, "auth": "api_key"},
        {"type": "ga4gh-tes", "url": BACKEND_URL, "api_key": {"key_env": KEY_ENV}},
        {
            "type": "ga4gh-tes",
            "url": BACKEND_URL,
            "auth": "basic",
            "basic": {"username_env": "U", "password_env": "P"},
            "api_key": {"key_env": KEY_ENV},
        },
        key_entry(key_env=""),
        key_entry(key_env="1KEY"),
        key_entry(key_env="TES-KEY"),
        key_entry(header=""),
        key_entry(header="X API Key"),
        key_entry(header="X-Key:"),
        key_entry(header="Host"),
        key_entry(header="content-length"),
        key_entry(header="Content-Type"),
        key_entry(header="Cookie"),
        key_entry(header="Proxy-Authorization"),
        *[key_entry(header=name.title()) for name in sorted(RESERVED_HEADERS)],
        {**key_entry(), "audience": "urn:example:tes"},
        {
            "type": "ga4gh-service-registry",
            "url": "https://registry.example.org",
            "auth": "api_key",
        },
    ],
)
def test_config_invalid(entry: dict[str, Any]) -> None:
    """API keys need `auth: api_key`, a variable and a header that can carry them.

    Registries can't give API keys to their backends.
    """
    with pytest.raises(pydantic.ValidationError):
        tes_config(entry)


def test_config_no_key_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, app_env: None
) -> None:
    """Keys can't be in the config itself; the app's startup error doesn't show them.

    FOCA leaves the input out of errors of the custom config.
    """
    entry = key_entry()
    entry["api_key"]["key"] = KEY
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(
        yaml.safe_dump(
            {
                "custom": {
                    "tes": {
                        "backends": [entry],
                        "client_credentials": {"issuer": ISSUER},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    set_overlays(monkeypatch, overlay)
    with pytest.raises(ValueError, match="custom configuration") as exc_info:
        init_app()
    assert "api_key.key" in str(exc_info.value.__cause__)
    assert KEY not in str(exc_info.value)
    assert KEY not in str(exc_info.value.__cause__)


def test_read_api_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keys come from the variable, or from the file named in `<name>_FILE`.

    A final line break is dropped (e.g., of a Secret created from a file),
    and the file is read on each call, so that rotated keys of mounted
    Secrets are used at once.
    """
    monkeypatch.setenv(KEY_ENV, KEY)
    monkeypatch.delenv(f"{KEY_ENV}_FILE", raising=False)
    assert backends.read_api_key(KEY_ENV) == KEY
    monkeypatch.setenv(KEY_ENV, f"{KEY}\r\n")
    assert backends.read_api_key(KEY_ENV) == KEY
    path = tmp_path / "key"
    path.write_text(f"{KEY}\n")
    monkeypatch.delenv(KEY_ENV)
    monkeypatch.setenv(f"{KEY_ENV}_FILE", str(path))
    assert backends.read_api_key(KEY_ENV) == KEY
    path.write_text("poi_rotated")
    assert backends.read_api_key(KEY_ENV) == "poi_rotated"


@pytest.mark.parametrize(
    "value, file, match",
    [
        (None, None, f"{KEY_ENV} .or {KEY_ENV}_FILE. is not set"),
        ("", None, f"{KEY_ENV} .or {KEY_ENV}_FILE. is not set"),
        (None, "", f"{KEY_ENV} .or {KEY_ENV}_FILE. is not set"),
        ("\n", None, f"{KEY_ENV} .or {KEY_ENV}_FILE. is not set"),
        (f"{KEY}\n\n", None, "visible ASCII"),
        (KEY, KEY, "must not both be set"),
        (None, "/nonexistent/key", f"{KEY_ENV}_FILE' could not be read"),
        (f"{KEY} x", None, "visible ASCII"),
        (f" {KEY}", None, "visible ASCII"),
        (f"{KEY}\r\nX-Injected: 1", None, "visible ASCII"),
        (f"{KEY}é", None, "visible ASCII"),
    ],
)
def test_read_api_key_invalid(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    value: str | None,
    file: str | None,
    match: str,
) -> None:
    """Missing, unreadable and invalid keys are refused, without their values.

    `file` is the content of the key file, or the path itself if absolute.
    """
    monkeypatch.delenv(KEY_ENV, raising=False)
    monkeypatch.delenv(f"{KEY_ENV}_FILE", raising=False)
    if value is not None:
        monkeypatch.setenv(KEY_ENV, value)
    if file is not None:
        path = Path(file) if file.startswith("/") else tmp_path / "key"
        if not file.startswith("/"):
            path.write_text(file)
        monkeypatch.setenv(f"{KEY_ENV}_FILE", str(path) if file else "")
    with pytest.raises(BackendAuthError, match=match) as exc_info:
        backends.read_api_key(KEY_ENV)
    assert KEY not in str(exc_info.value)


def test_api_key_auth() -> None:
    """API keys are set as bearer tokens, or as they are in other headers.

    Their representation names the header only.
    """
    prepared = requests.Request("GET", BACKEND_URL).prepare()
    bearer = backends.ApiKeyAuth(header="authorization", key=KEY)
    custom = backends.ApiKeyAuth(header="X-API-Key", key=KEY)
    assert bearer(prepared).headers["Authorization"] == f"Bearer {KEY}"
    assert custom(prepared).headers["X-API-Key"] == KEY
    same = backends.ApiKeyAuth(header="Authorization", key=KEY)
    assert bearer == same
    assert hash(bearer) == hash(same)
    assert len({bearer, same, custom}) == 2
    assert KEY not in repr(bearer)
    assert KEY not in repr(custom)
    assert "X-API-Key" in repr(custom)


@pytest.fixture
def configured(app: FlaskApp, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """App context with backends with API keys, in the default and another header."""
    monkeypatch.setenv(KEY_ENV, KEY)
    monkeypatch.setenv("TES_OTHER_KEY", "other-key")
    with app.app.app_context():
        conf = get_custom_config(CustomConfig).tes
        conf.backends = tes_config(
            key_entry(f"{BACKEND_URL}/key"),
            {
                **key_entry(f"{BACKEND_URL}/custom", header="X-API-Key"),
                "api_key": {"key_env": "TES_OTHER_KEY", "header": "X-API-Key"},
            },
        ).backends
        yield


def test_client_api_key(configured: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Backends with `auth: api_key` get the key of their entry, read per request."""
    kwargs = backends.create_client(host=f"{BACKEND_URL}/key")._request_params()
    assert kwargs["auth"] == backends.ApiKeyAuth(header="Authorization", key=KEY)
    assert "Authorization" not in kwargs["headers"]
    other = backends.create_client(host=f"{BACKEND_URL}/custom")._request_params()
    assert other["auth"] == backends.ApiKeyAuth(header="X-API-Key", key="other-key")
    assert other["auth"] != kwargs["auth"]
    assert other["auth"] != "other-key"
    monkeypatch.delenv(KEY_ENV)
    with pytest.raises(BackendAuthError, match=KEY_ENV):
        backends.create_client(host=f"{BACKEND_URL}/key")._request_params()


def test_client_api_key_unset(configured: None) -> None:
    """Backends with `auth: api_key`, but no `api_key` (not validated), get none."""
    backend = Backend(id=BACKEND_URL, url=BACKEND_URL, auth="api_key")
    entry = TesServiceEntry.model_construct(
        type="ga4gh-tes", url=BACKEND_URL, auth="api_key", api_key=None
    )
    client = backends.TesClient(url=BACKEND_URL, backend=backend, entry=entry)
    with pytest.raises(BackendAuthError, match="No API key"):
        client._request_params()


def test_client_api_key_not_chosen(configured: None) -> None:
    """Requests can't use API keys of other backends (e.g., chosen by name)."""
    conf = get_custom_config(CustomConfig).tes
    conf.backends = BackendListConfig(
        [
            *conf.backends.root,
            TesServiceEntry(type="ga4gh-tes", url=f"{BACKEND_URL}/none", auth="none"),
        ]
    )
    client = backends.create_client(host=f"{BACKEND_URL}/none")
    with pytest.raises(BackendAuthError, match="No API key"):
        client._request_params(auth="api_key")


A_URL = "https://a.example.org/x"


@pytest.mark.parametrize(
    "target, kept",
    [
        ("https://a.example.org/final", True),
        ("https://b.example.org/final", False),
        ("https://a.example.org:8443/final", False),
        ("http://a.example.org/final", False),
    ],
)
def test_session_redirect_api_key(target: str, kept: bool) -> None:
    """Redirects keep API keys in any header on the same host and port only."""
    session = backends.BackendSession()
    request = requests.Request(
        "GET", A_URL, auth=backends.ApiKeyAuth(header="X-API-Key", key=KEY)
    )
    first = session.prepare_request(request)
    assert first.headers["X-API-Key"] == KEY
    response = requests.Response()
    response.request = first
    redirected = first.copy()
    redirected.prepare_url(target, None)
    session.rebuild_auth(redirected, response)
    assert ("X-API-Key" in redirected.headers) is kept


class KeyRequiringHandler(BaseHTTPRequestHandler):
    """Handler of a fake TES backend that requires an API key.

    Requires `KEY` in the header `header`, as a bearer token in
    `Authorization`; records the headers of each request. `/redirect`
    redirects to a task on the same server.
    """

    header: ClassVar[str] = "Authorization"
    received: ClassVar[list[tuple[str, str, dict[str, str]]]] = []

    def _expected(self) -> str:
        """Get the expected value of the header."""
        return f"Bearer {KEY}" if self.header == "Authorization" else KEY

    def _send(self, status: int, body: object) -> None:
        """Send a JSON answer.

        Args:
            status: Status of the answer.
            body: Body of the answer.
        """
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve(self) -> None:
        """Serve the service info, tasks and cancels, with the key only."""
        self.received.append((self.command, self.path, dict(self.headers)))
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        if self.headers.get(self.header) != self._expected():
            self._send(401, {"msg": "API key required"})
        elif self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/ga4gh/tes/v1/tasks/remote-1")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path.endswith("/service-info"):
            self._send(
                200,
                {"id": "poiesis", "type": {"artifact": "tes", "version": "1.1.0"}},
            )
        elif self.command == "POST" and self.path.endswith(":cancel"):
            self._send(200, {})
        elif self.command == "POST":
            self._send(200, {"id": "remote-1"})
        else:
            self._send(200, {"id": "remote-1", "state": "COMPLETE"})

    do_GET = _serve
    do_POST = _serve

    def log_message(self, format: str, *args: Any) -> None:
        """Log nothing."""


@pytest.fixture
def key_server(
    app: FlaskApp, loopback: None, monkeypatch: pytest.MonkeyPatch
) -> Iterator[str]:
    """A fake TES backend that requires an API key, configured in an app context.

    Yields:
        Its URL.
    """
    monkeypatch.setattr(KeyRequiringHandler, "received", [])
    monkeypatch.setenv(KEY_ENV, KEY)
    server = ThreadingHTTPServer(("127.0.0.1", 0), KeyRequiringHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with app.app.app_context():
            backend_versions.clear()
            yield url
    finally:
        server.shutdown()
        server.server_close()


def use_backend(url: str, **api_key: Any) -> backends.TesClient:
    """Configure the fake backend with an API key, and get its client.

    Args:
        url: URL of the backend.
        **api_key: Settings of the API key, besides `key_env`.

    Returns:
        The client of the backend.
    """
    conf = get_custom_config(CustomConfig).tes
    conf.backends = tes_config(
        {**key_entry(url, **api_key), "labels": {"tes.version": "1.1"}}
    ).backends
    return backends.create_client(host=url)


@pytest.mark.parametrize("header", ["Authorization", "X-API-Key"])
def test_backend_requires_api_key(
    key_server: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    header: str,
) -> None:
    """Tasks are created, got and canceled at a backend that requires a key.

    Tasks are forwarded as plugins forward them, whose header of the same
    name the key replaces. Each request carries the key, also after a
    redirect on the same host; the key is not logged.
    """
    monkeypatch.setattr(KeyRequiringHandler, "header", header)
    kwargs = {} if header == "Authorization" else {"header": header}
    client = use_backend(key_server, **kwargs)
    caplog.set_level(logging.DEBUG)
    task = tes.Task(executors=[tes.Executor(image="alpine", command=["true"])])
    outbound = OutboundRequest(
        backend=client.backend,
        method="POST",
        path="/ga4gh/tes/v1/tasks",
        headers=Headers({header: "plugin-value"}),
    )
    task_id, _ = client.forward(outbound, task)
    assert task_id == "remote-1"
    assert client.get_task("remote-1").state == "COMPLETE"
    client.cancel_task("remote-1")
    assert client.service_info()["id"] == "poiesis"
    params = client._request_params()
    redirected = client._request("GET", f"{key_server}/redirect", **params)
    assert redirected.json()["state"] == "COMPLETE"
    expected = f"Bearer {KEY}" if header == "Authorization" else KEY
    assert [request[:2] for request in KeyRequiringHandler.received] == [
        ("POST", "/ga4gh/tes/v1/tasks"),
        ("GET", "/ga4gh/tes/v1/tasks/remote-1?view=BASIC"),
        ("POST", "/ga4gh/tes/v1/tasks/remote-1:cancel"),
        ("GET", "/ga4gh/tes/v1/service-info"),
        ("GET", "/redirect"),
        ("GET", "/ga4gh/tes/v1/tasks/remote-1"),
    ]
    assert all(
        request[2].get(header) == expected for request in KeyRequiringHandler.received
    )
    assert KEY not in caplog.text


def test_backend_refuses_wrong_key(
    key_server: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A refused key fails the request, without the key in the error or logs."""
    monkeypatch.setenv(KEY_ENV, "poi_wrong-key")
    client = use_backend(key_server)
    caplog.set_level(logging.DEBUG)
    with pytest.raises(requests.HTTPError) as exc_info:
        client.get_task("remote-1")
    assert exc_info.value.response.status_code == 401
    assert "poi_wrong-key" not in str(exc_info.value)
    assert "poi_wrong-key" not in caplog.text


def test_check_backends_api_key(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """On startup, backends with API keys are logged, without the keys."""
    monkeypatch.setenv(KEY_ENV, KEY)
    caplog.set_level(logging.INFO)
    check_backends(tes_config(key_entry(header="X-API-Key")))
    assert (
        f"TES backend {BACKEND_URL}: API key (`auth: api_key`, header X-API-Key)"
        in caplog.text
    )
    assert "owned by the account of the key" in caplog.text
    assert KEY not in caplog.text


def test_check_backends_api_key_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """On startup, missing keys are named with the other missing credentials.

    Also keys of empty files.
    """
    monkeypatch.delenv(KEY_ENV, raising=False)
    monkeypatch.delenv(f"{KEY_ENV}_FILE", raising=False)
    expected = f"environment variables {KEY_ENV} .or {KEY_ENV}_FILE.$"
    with pytest.raises(RuntimeError, match=expected):
        check_backends(tes_config(key_entry()))
    empty = tmp_path / "key"
    empty.write_text("")
    monkeypatch.setenv(f"{KEY_ENV}_FILE", str(empty))
    with pytest.raises(RuntimeError, match=expected):
        check_backends(tes_config(key_entry()))


def test_check_backends_api_key_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    """On startup, keys that can't be used are refused, without their values."""
    monkeypatch.setenv(KEY_ENV, f"{KEY} x")
    with pytest.raises(RuntimeError, match=f"TES backend {BACKEND_URL}") as exc_info:
        check_backends(tes_config(key_entry()))
    assert KEY not in str(exc_info.value)
    monkeypatch.setenv(f"{KEY_ENV}_FILE", "/nonexistent/key")
    with pytest.raises(RuntimeError, match="must not both be set"):
        check_backends(tes_config(key_entry()))


def test_check_backends_api_key_unvalidated() -> None:
    """Backends with `auth: api_key` but no `api_key` are rejected on startup."""
    entry = TesServiceEntry.model_construct(
        type="ga4gh-tes",
        url=BACKEND_URL,
        url_schemes=["https"],
        auth="api_key",
        api_key=None,
    )
    conf = Tes.model_construct(
        backends=BackendListConfig.model_construct([entry]), url_schemes=["https"]
    )
    with pytest.raises(RuntimeError, match="without `api_key`"):
        check_backends(conf)
