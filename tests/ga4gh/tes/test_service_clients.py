"""Tests of service clients, which create and own tasks with their own tokens.

The identity provider is mocked (cf. :py:func:`tests.conftest.mock_get`): it
issues the service tokens of the service clients as some identity providers
issue client-credentials tokens: with the client ID as `client_id`, `sub` and
`aud`.
TES backends are faked (cf. :py:class:`tests.ga4gh.tes.conftest.FakeBackend`).
"""

from pathlib import Path
from typing import Any

from connexion import FlaskApp
from flask.testing import FlaskClient
from foca import get_config
from pymongo import MongoClient
import pytest
import yaml

from pro_tes import service_clients
from pro_tes.app import init_app
from pro_tes.exceptions import Unauthorized
from pro_tes.ga4gh.tes import ownership
from pro_tes.ga4gh.tes.backends import CLIENT_ID_ENV
from tests.conftest import (
    ADMIN,
    AUDIENCE,
    DB,
    GROUPS,
    ISSUER,
    OTHER_USER,
    USER,
    USER_GROUP,
    make_headers,
    make_token,
    set_overlays,
)
from tests.ga4gh.tes.conftest import BASE_PATH, TASK, FakeBackend, relayed

# Service clients of the overlay `service_overlay()`
CLIENT = "workflow-engine"
OTHER_CLIENT = "smoke-tests"
# Owner of the tasks of `CLIENT`, as stored (`user_id`)
CLIENT_OWNER = f"client:{CLIENT}"


def service_overlay(**auth: Any) -> dict[str, Any]:
    """Get a config overlay with the service clients `CLIENT` and `OTHER_CLIENT`.

    Both are clients of the trusted issuer, mapped to the role `user`, and
    their client IDs are accepted audiences, as some identity providers put
    only them into `aud` of their service tokens.

    Args:
        **auth: Settings of `security.auth` replacing those of the overlay.

    Returns:
        Config overlay.
    """
    return {
        "security": {
            "auth": {
                "audience": [AUDIENCE, CLIENT, OTHER_CLIENT],
                "service_clients": [
                    {"client_id": CLIENT, "issuer": ISSUER},
                    {"client_id": OTHER_CLIENT, "issuer": ISSUER},
                ],
                **auth,
            },
            "access_control": {
                "client_mapping": {CLIENT: "user", OTHER_CLIENT: "user"},
            },
        }
    }


def use_overlay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, config: dict[str, Any]
) -> None:
    """Create apps with a config overlay after the test overlay.

    Args:
        monkeypatch: Monkeypatch fixture of the test.
        tmp_path: Directory of the overlay.
        config: Config overlay.
    """
    path = tmp_path / "service-clients.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    set_overlays(monkeypatch, path)


def client_headers(
    client_id: str = CLIENT, audience: str | None = None, **claims: Any
) -> dict[str, str]:
    """Get request headers with a service token of a service client.

    As some identity providers issue them (client credentials grant):
    `client_id`, `sub` and, by default, `aud` are the client ID.

    Args:
        client_id: Client ID.
        audience: Audience; by default, the client ID.
        **claims: Other claims, or claims replacing those of service tokens.

    Returns:
        Request headers.
    """
    return make_headers(
        user=client_id,
        audience=client_id if audience is None else audience,
        claims={"client_id": client_id, **claims},
    )


@pytest.fixture
def service_app(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, app_env: None
) -> FlaskApp:
    """App with the service clients of :py:func:`service_overlay`."""
    use_overlay(monkeypatch, tmp_path, service_overlay())
    return init_app()


@pytest.fixture
def app(service_app: FlaskApp) -> FlaskApp:
    """App of the fixtures of fake backends: the one with service clients."""
    return service_app


@pytest.fixture
def service_client(service_app: FlaskApp) -> FlaskClient:
    """Test client of the app with service clients, without access token."""
    return service_app.app.test_client()


def create(client: FlaskClient, headers: dict[str, str]) -> str:
    """Create a task.

    Args:
        client: Test client.
        headers: Request headers, with the token of the creator.

    Returns:
        Identifier of the task.
    """
    response = client.post(f"{BASE_PATH}/tasks", json=TASK, headers=headers)
    assert response.status_code == 200, response.get_json()
    return str(response.get_json()["id"])


def listed(client: FlaskClient, headers: dict[str, str]) -> list[str]:
    """List the identifiers of the tasks that a caller sees.

    Args:
        client: Test client.
        headers: Request headers, with the token of the caller.

    Returns:
        Identifiers of the tasks.
    """
    response = client.get(f"{BASE_PATH}/tasks", headers=headers)
    assert response.status_code == 200
    return [task["id"] for task in response.get_json()["tasks"]]


# Startup


@pytest.mark.usefixtures("app_env")
def test_default_no_service_clients() -> None:
    """Without service clients (the default), nothing changes.

    No request hook is added, and service tokens for their own audience are
    rejected, as that is not an accepted audience.
    """
    default = init_app()
    assert service_clients.check_service_audience not in [
        hook for hooks in default.app.before_request_funcs.values() for hook in hooks
    ]
    response = default.app.test_client().get(
        f"{BASE_PATH}/tasks", headers=client_headers()
    )
    assert response.status_code == 401


def test_hook_registered(service_app: FlaskApp) -> None:
    """With service clients, the audience of each request's token is checked."""
    hooks = service_app.app.before_request_funcs[None]
    assert service_clients.check_service_audience in hooks


@pytest.mark.parametrize(
    "config, message",
    [
        (
            {"security": {"auth": {"on_behalf_of_header": "X-On-Behalf-Of"}}},
            "act for users",
        ),
        (
            service_overlay(
                service_clients=[
                    {"client_id": CLIENT, "issuer": ISSUER, "on_behalf_of": True},
                    {"client_id": OTHER_CLIENT, "issuer": ISSUER},
                ],
                on_behalf_of_header="X-On-Behalf-Of",
            ),
            "act for users",
        ),
        (
            service_overlay(
                service_clients=[
                    {"client_id": CLIENT},
                    {"client_id": OTHER_CLIENT, "issuer": ISSUER},
                ]
            ),
            "`issuer`",
        ),
        (
            {
                "security": {
                    "auth": {
                        "service_clients": [{"client_id": CLIENT, "issuer": ISSUER}]
                    }
                }
            },
            "role 'user'",
        ),
        (
            {
                **service_overlay(),
                "security": {
                    **service_overlay()["security"],
                    "access_control": {
                        "client_mapping": {CLIENT: "admin", OTHER_CLIENT: "user"}
                    },
                },
            },
            "role 'user'",
        ),
    ],
    ids=["header", "on behalf of", "no issuer", "no role", "admin"],
)
def test_refused(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    app_env: None,
    config: dict[str, Any],
    message: str,
) -> None:
    """The app refuses to start with service clients it doesn't support.

    Clients acting for users, clients without explicit issuer, and clients
    with another role than `user` (or none).
    """
    use_overlay(monkeypatch, tmp_path, config)
    with pytest.raises(RuntimeError, match=message):
        init_app()


def test_refused_own_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, app_env: None
) -> None:
    """The client of proTES itself can't be a service client.

    TES backends get its tokens, so they could use them at proTES.
    """
    monkeypatch.setenv(CLIENT_ID_ENV, CLIENT)
    use_overlay(monkeypatch, tmp_path, service_overlay())
    with pytest.raises(RuntimeError, match="own client"):
        init_app()


def test_audience_warning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    app_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A warning names service clients whose ID is not an accepted audience.

    The service clients are logged, too.
    """
    use_overlay(monkeypatch, tmp_path, service_overlay(audience=[AUDIENCE, CLIENT]))
    init_app()
    # FOCA configures logging to stderr
    lines = capsys.readouterr().err.splitlines()
    warnings = [
        line for line in lines if "WARNING" in line and "Service client" in line
    ]
    assert len(warnings) == 1
    assert f"Service client {OTHER_CLIENT} is not an accepted audience" in warnings[0]
    assert any(
        f"{CLIENT} (issuer {ISSUER}), {OTHER_CLIENT} (issuer {ISSUER})" in line
        for line in lines
    )


def test_no_audience_check(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    app_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Without audience validation, no audience is required of clients."""
    use_overlay(
        monkeypatch, tmp_path, service_overlay(audience=None, verify_audience=False)
    )
    init_app()
    assert "is not an accepted audience" not in capsys.readouterr().err


# Tasks of service clients


@pytest.mark.usefixtures("backend")
def test_own_tasks(service_client: FlaskClient) -> None:
    """A service client creates tasks, and lists, reads and cancels its own."""
    task_id = create(service_client, client_headers())
    assert listed(service_client, client_headers()) == [task_id]
    for method, path in [
        ("GET", f"{BASE_PATH}/tasks/{task_id}"),
        ("GET", f"{BASE_PATH}/tasks/{task_id}/provenance"),
        ("POST", f"{BASE_PATH}/tasks/{task_id}:cancel"),
    ]:
        response = service_client.open(path, method=method, headers=client_headers())
        assert response.status_code == 200, (method, path)


def test_owner_stored(
    service_client: FlaskClient,
    backend: type[FakeBackend],
    mongo_client: MongoClient[dict[str, Any]],
) -> None:
    """The client is the owner of its tasks, as a client (`client:`)."""
    task_id = create(service_client, client_headers())
    document = mongo_client[DB]["tasks"].find_one({"task.id": task_id})
    assert document is not None
    assert document["user_id"] == CLIENT_OWNER
    rules = {
        (rule["v0"], rule["v2"])
        for rule in mongo_client[DB]["policy_rules"].find({"ptype": "g2"})
    }
    assert rules == {
        (CLIENT_OWNER, f"{BASE_PATH}/tasks/{task_id}"),
        (CLIENT_OWNER, f"{BASE_PATH}/tasks/{task_id}:cancel"),
    }
    assert len(relayed(backend)) == 1


@pytest.mark.usefixtures("backend")
def test_separate_from_users(service_client: FlaskClient) -> None:
    """Tasks of service clients and of users are invisible to each other."""
    client_task = create(service_client, client_headers())
    user_task = create(service_client, make_headers(USER))
    assert listed(service_client, client_headers()) == [client_task]
    assert listed(service_client, make_headers(USER)) == [user_task]
    for headers, task_id in [
        (make_headers(USER), client_task),
        (make_headers(OTHER_USER), client_task),
        (make_headers(ADMIN), client_task),
        (client_headers(OTHER_CLIENT), client_task),
        (client_headers(), user_task),
    ]:
        for method, path in [
            ("GET", f"{BASE_PATH}/tasks/{task_id}"),
            ("POST", f"{BASE_PATH}/tasks/{task_id}:cancel"),
        ]:
            response = service_client.open(path, method=method, headers=headers)
            assert response.status_code == 403, (method, path)


@pytest.mark.usefixtures("backend")
def test_user_named_as_client(
    service_client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user whose subject is a client's ID doesn't get the client's tasks."""
    monkeypatch.setitem(GROUPS, CLIENT, [USER_GROUP])
    task_id = create(service_client, client_headers())
    headers = make_headers(user=CLIENT)
    assert listed(service_client, headers) == []
    response = service_client.get(f"{BASE_PATH}/tasks/{task_id}", headers=headers)
    assert response.status_code == 403


@pytest.mark.parametrize(
    "method, path",
    [
        ("GET", "/admin/access-control/permissions"),
        ("GET", f"{BASE_PATH}/usage"),
        ("GET", f"{BASE_PATH}/usage/reports"),
    ],
)
def test_nothing_beyond(service_client: FlaskClient, method: str, path: str) -> None:
    """Service clients have no admin rights (`403`)."""
    response = service_client.open(path, method=method, headers=client_headers())
    assert response.status_code == 403


def test_relayed_with_own_credentials(
    service_client: FlaskClient, backend: type[FakeBackend]
) -> None:
    """Tasks of service clients are relayed with proTES' credentials.

    The backends of the tests need none (`auth: none`); the token of the
    client is not passed on.
    """
    headers = client_headers()
    create(service_client, headers)
    (outbound,) = backend.outbound
    assert "Authorization" not in outbound.headers
    assert headers["Authorization"].removeprefix("Bearer ") not in repr(outbound)


def test_owner_refuses_client_prefix(
    service_app: FlaskApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Users whose subject looks like a client owner are not identified.

    So that stored owners (`user_id`) of users and clients can't be mixed up.
    """
    monkeypatch.setattr(ownership, "get_current_client", lambda: None)
    monkeypatch.setattr(ownership, "get_current_subject", lambda: CLIENT_OWNER)
    with service_app.app.test_request_context(), pytest.raises(Unauthorized):
        ownership.get_owner()


def test_owner_of_client(
    service_app: FlaskApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner of tasks of a service client is the client, with prefix."""
    monkeypatch.setattr(ownership, "get_current_client", lambda: CLIENT)
    with service_app.app.test_request_context():
        assert ownership.get_owner() == CLIENT_OWNER
    assert ownership.owner_subject(CLIENT_OWNER) == ("client", CLIENT)
    assert ownership.owner_subject(USER) == ("user", USER)


# Audiences of service clients


@pytest.mark.usefixtures("backend")
@pytest.mark.parametrize(
    "headers",
    [
        # a user's token obtained by the client, e.g., by token exchange
        client_headers(sub=USER),
        # a service token for the audience of another service client
        client_headers(audience=OTHER_CLIENT),
        # a service token of a client that is not a service client
        client_headers(client_id="other-app", audience=CLIENT),
        # a user's token for the audience of a service client
        make_headers(USER, audience=CLIENT),
    ],
    ids=["user via client", "other client", "unknown client", "user"],
)
@pytest.mark.parametrize(
    "method, path",
    [
        ("GET", f"{BASE_PATH}/tasks"),
        ("POST", f"{BASE_PATH}/tasks"),
        ("GET", "/admin/access-control/permissions"),
        ("GET", f"{BASE_PATH}/plugins"),
    ],
)
def test_service_audience_only_for_service_tokens(
    service_client: FlaskClient, headers: dict[str, str], method: str, path: str
) -> None:
    """The audience of a service client is accepted for its own tokens only.

    Other tokens for it are rejected as invalid (`401`, with an
    `invalid_token` challenge) on all endpoints, before any other check.
    """
    response = service_client.open(
        path, method=method, headers=headers, json=TASK if method == "POST" else None
    )
    assert response.status_code == 401
    assert 'error="invalid_token"' in response.headers["WWW-Authenticate"]
    assert response.get_json()["status"] == 401


@pytest.mark.usefixtures("backend")
@pytest.mark.parametrize(
    "headers",
    [
        # users' tokens for a configured audience, also obtained by a client
        make_headers(USER),
        make_headers(USER, claims={"client_id": CLIENT}),
        make_headers(USER, claims={"aud": [AUDIENCE, CLIENT]}),
        # service tokens for a configured audience
        client_headers(audience=AUDIENCE),
    ],
    ids=["user", "user via client", "user, both audiences", "client"],
)
def test_other_audiences_unchanged(
    service_client: FlaskClient, headers: dict[str, str]
) -> None:
    """Tokens for the other audiences are accepted as before."""
    response = service_client.get(f"{BASE_PATH}/tasks", headers=headers)
    assert response.status_code == 200


@pytest.mark.parametrize(
    "authorization",
    [
        None,
        "Basic dXNlcjpwYXNz",
        "Bearer ",
        "Bearer not-a-token",
        f"Bearer {make_token(user=CLIENT, issuer='https://other.example.org/')}",
        f"Bearer {make_token(user=CLIENT, claims={'aud': 42})}",
    ],
    ids=["none", "basic", "empty", "malformed", "other issuer", "invalid aud"],
)
def test_audience_check_skips(service_app: FlaskApp, authorization: str | None) -> None:
    """Requests without a decodable token of a trusted issuer are left alone.

    FOCA rejects them where a token is required, as without service clients.
    """
    headers = {} if authorization is None else {"Authorization": authorization}
    with service_app.app.test_request_context(headers=headers):
        service_clients.check_service_audience()


def test_audience_check_without_audience_validation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, app_env: None
) -> None:
    """Without audience validation, the hook doesn't check audiences."""
    use_overlay(
        monkeypatch, tmp_path, service_overlay(audience=None, verify_audience=False)
    )
    app = init_app()
    headers = client_headers(sub=USER)
    with app.app.test_request_context(headers=headers):
        assert get_config().security.auth.verify_audience is False
        service_clients.check_service_audience()
