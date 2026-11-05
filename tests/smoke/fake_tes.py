"""Fake TES backend and identity provider for the Compose smoke test.

Serves the parts of the TES API that proTES uses (service info, task
creation, retrieval and cancellation) from memory: created tasks are
complete right away, unless held (see below). Also serves an OpenID Connect
identity provider for
proTES (issuer `ISSUER`; discovery, keys and user info), which issues access
tokens of the users of `GROUPS` at `GET /oidc/token?sub=<user>` (no login),
and access tokens of proTES itself at `POST /oidc/token` (client credentials
grant, with the client `CLIENT_ID` and `CLIENT_SECRET`). Except for the
service info, the TES API requires access tokens of proTES, as TES backends
of proTES may.
Run it with `python fake_tes.py [port]` (default 8080); it depends on the
standard library, PyJWT and cryptography (dependencies of FOCA).

With the environment variable `FAKE_TES_VERSION=1.0`, it serves TES 1.0 as TESK
does (compared with tesk-api 1.1.0, see `tests/integration/tesk/`): it reports
the version so (artifact `tes`, version `1.0`), rejects tasks with fields of TES
1.1 (`400`), serves only the identifier and state in the view `MINIMAL`, one log
per executor without `stderr`, refuses to cancel tasks that have finished (`400`) or
that it doesn't have (`404`), and requires no access tokens, as those of the
identity provider of another instance can't be checked. It is stricter than
TESK in one way, so that the smoke test sees whether proTES adapts tasks:
TESK accepts `backend_parameters` and files without `type`, and ignores the
former. Tasks with the tag `fake.hold` run until they are canceled. Otherwise, it
serves tasks with a field that the TES specification doesn't have (`author`, as
TESP does): TES instances may add such fields.

With the environment variable `FAKE_TES_BASE_PATH=/v1`, it serves the TES API at
that path only, and requires no access tokens, as TESP does (behind no rewriting
proxy, and as those of the identity provider of another instance can't be
checked).
"""

import base64
import binascii
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import sys
import threading
import time
from typing import Any
from urllib.parse import parse_qs, unquote_plus, urlsplit

from cryptography.hazmat.primitives.asymmetric import rsa
import jwt

BASE_PATH = os.environ.get("FAKE_TES_BASE_PATH", "/ga4gh/tes/v1")
# Issuer of the fake identity provider, as proTES reaches it (`config.yaml` of
# this directory)
ISSUER = "http://fake-tes:8080/oidc/"
AUDIENCE = "smoke-client"
# Client of proTES (fictitious; as in `compose.yaml` of this directory)
CLIENT_ID = "smoke-protes"
CLIENT_SECRET = "smoke-protes-secret"  # noqa: S105 (fictitious)
# Groups of the users (user info), as mapped to roles in `config.yaml`
GROUPS = {
    "alice": ["smoke-users"],
    "bob": ["smoke-users"],
    "admin": ["smoke-admins"],
    "eve": ["other-group"],
}
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
# TES version of this instance
TES_10 = os.environ.get("FAKE_TES_VERSION") == "1.0"
SERVICE_INFO = {
    "id": "org.example.fake-tes",
    "name": "Fake TES",
    "type": {
        "group": "org.ga4gh",
        "artifact": "tes",
        "version": "1.0" if TES_10 else "1.1.0",
    },
    "organization": {"name": "Example Organization", "url": "https://example.org"},
    "version": "1.0.0",
}
# Logs of tasks in the view `FULL` only, as the TES specification has it
FULL_LOGS = [
    {
        "logs": [{"exit_code": 0, "stdout": "fake stdout", "stderr": "fake stderr"}],
        "outputs": [],
        "system_logs": ["fake system log"],
    }
]
# The same as TESK has them, with both streams of an executor in `stdout`
FULL_LOGS_10 = [
    {
        "logs": [{"exit_code": 0, "stdout": "fake stdout\nfake stderr\n"}],
        "metadata": {"USER_ID": "anonymousUser"},
        "system_logs": ["fake system log"],
    }
]
TASKS: dict[str, dict[str, Any]] = {}
LOCK = threading.Lock()


def fields_of_tes_11(task: dict[str, Any]) -> list[str]:
    """Get the fields of a task that TES 1.0 doesn't know.

    Args:
        task: Task.

    Returns:
        The names of the fields, as this fake rejects them.
    """
    items: list[tuple[dict[str, Any], tuple[str, ...]]]
    items = [(item, ("ignore_error",)) for item in task.get("executors") or []]
    items += [(item, ("streamable",)) for item in task.get("inputs") or []]
    items += [(item, ("path_prefix",)) for item in task.get("outputs") or []]
    items += [
        (
            task.get("resources") or {},
            ("backend_parameters", "backend_parameters_strict"),
        )
    ]
    found = [name for item, names in items for name in names if name in item]
    # required in TES 1.0
    files = (task.get("inputs") or []) + (task.get("outputs") or [])
    return found + ["no type" for item in files if "type" not in item]


class Handler(BaseHTTPRequestHandler):
    """Handler of requests to the fake TES API."""

    def _send(self, status: HTTPStatus, body: object) -> None:
        """Send a JSON response.

        Args:
            status: HTTP status.
            body: JSON body.
        """
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_idp(self, path: str) -> bool:
        """Serve the endpoints of the identity provider.

        Args:
            path: Request path.

        Returns:
            Whether the path is one of the identity provider.
        """
        if path == "/oidc/.well-known/openid-configuration":
            self._send(
                HTTPStatus.OK,
                {
                    "issuer": ISSUER,
                    "jwks_uri": f"{ISSUER}jwk",
                    "token_endpoint": f"{ISSUER}token",
                    "userinfo_endpoint": f"{ISSUER}userinfo",
                },
            )
        elif path == "/oidc/jwk":
            key = jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key(), as_dict=True)
            self._send(HTTPStatus.OK, {"keys": [{**key, "kid": "smoke", "use": "sig"}]})
        elif path == "/oidc/userinfo":
            token = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
            try:
                claims = jwt.decode(
                    token, KEY.public_key(), algorithms=["RS256"], audience=AUDIENCE
                )
            except jwt.InvalidTokenError:
                self._send(HTTPStatus.UNAUTHORIZED, {"error": "invalid_token"})
                return True
            sub = claims["sub"]
            self._send(HTTPStatus.OK, {"sub": sub, "groups": GROUPS.get(sub, [])})
        elif path == "/oidc/token":
            sub = parse_qs(urlsplit(self.path).query).get("sub", ["alice"])[0]
            now = int(time.time())
            token = jwt.encode(
                {
                    "iss": ISSUER,
                    "aud": AUDIENCE,
                    "sub": sub,
                    "iat": now,
                    "exp": now + 600,
                },
                KEY,
                algorithm="RS256",
                headers={"kid": "smoke", "typ": "at+jwt"},
            )
            self._send(HTTPStatus.OK, {"access_token": token})
        else:
            return False
        return True

    def _issue_client_token(self, body: bytes) -> None:
        """Issue an access token of proTES (client credentials grant).

        The client authenticates with HTTP Basic (`client_secret_basic`).

        Args:
            body: Request body (form-encoded).
        """
        form = parse_qs(body.decode())
        header = self.headers.get("Authorization") or ""
        scheme, _, credentials = header.partition(" ")
        try:
            decoded = base64.b64decode(credentials, validate=True).decode()
        except (binascii.Error, UnicodeDecodeError):
            decoded = ""
        client = tuple(unquote_plus(value) for value in decoded.split(":", 1))
        if form.get("grant_type") != ["client_credentials"]:
            self._send(HTTPStatus.BAD_REQUEST, {"error": "unsupported_grant_type"})
            return
        if scheme != "Basic" or client != (CLIENT_ID, CLIENT_SECRET):
            self._send(HTTPStatus.UNAUTHORIZED, {"error": "invalid_client"})
            return
        now = int(time.time())
        token = jwt.encode(
            {
                "iss": ISSUER,
                "sub": CLIENT_ID,
                "client_id": CLIENT_ID,
                "iat": now,
                "exp": now + 300,
            },
            KEY,
            algorithm="RS256",
            headers={"kid": "smoke", "typ": "at+jwt"},
        )
        self._send(
            HTTPStatus.OK,
            {"access_token": token, "token_type": "Bearer", "expires_in": 300},
        )

    def _authorized(self) -> bool:
        """Check that the request has an access token of proTES.

        Sends `401` if not.

        Returns:
            Whether the request has a valid access token of proTES.
        """
        if TES_10 or "FAKE_TES_BASE_PATH" in os.environ:
            return True
        token = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
        try:
            claims = jwt.decode(
                token,
                KEY.public_key(),
                algorithms=["RS256"],
                issuer=ISSUER,
                options={"verify_aud": False},
            )
        except jwt.InvalidTokenError:
            claims = {}
        if claims.get("client_id") != CLIENT_ID:
            self._send(HTTPStatus.UNAUTHORIZED, {"msg": "token of proTES required"})
            return False
        return True

    def do_GET(self) -> None:
        """Serve the service info, tasks, the list of tasks, and the IdP."""
        path = self.path.split("?", 1)[0]
        if self._serve_idp(path):
            return
        if path == f"{BASE_PATH}/service-info":
            self._send(HTTPStatus.OK, SERVICE_INFO)
            return
        if not self._authorized():
            return
        if path == f"{BASE_PATH}/tasks":
            with LOCK:
                tasks = [
                    {"id": key, "state": task["state"]} for key, task in TASKS.items()
                ]
            self._send(HTTPStatus.OK, {"tasks": tasks})
            return
        task_id = path.removeprefix(f"{BASE_PATH}/tasks/")
        with LOCK:
            task = TASKS.get(task_id)
        if task is None or task_id == path:
            self._send(HTTPStatus.NOT_FOUND, {"msg": "not found"})
            return
        view = parse_qs(urlsplit(self.path).query).get("view", ["MINIMAL"])[0]
        if TES_10 and view == "MINIMAL":
            task = {"id": task["id"], "state": task["state"]}
        elif view == "FULL":
            task = {**task, "logs": FULL_LOGS_10 if TES_10 else FULL_LOGS}
        self._send(HTTPStatus.OK, task)

    def do_POST(self) -> None:
        """Create tasks (complete right away), cancel them (no-op), issue tokens."""
        path = self.path.split("?", 1)[0]
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        if path == "/oidc/token":
            self._issue_client_token(body)
            return
        if not self._authorized():
            return
        if path == f"{BASE_PATH}/tasks":
            try:
                task = json.loads(body)
            except ValueError:
                self._send(HTTPStatus.BAD_REQUEST, {"msg": "invalid JSON"})
                return
            unknown = fields_of_tes_11(task) if TES_10 else []
            if unknown:
                self._send(HTTPStatus.BAD_REQUEST, {"msg": f"unknown: {unknown}"})
                return
            hold = "fake.hold" in (task.get("tags") or {})
            with LOCK:
                task_id = f"fake-{len(TASKS) + 1}"
                TASKS[task_id] = {
                    **task,
                    **({} if TES_10 else {"author": None}),
                    "id": task_id,
                    "state": "RUNNING" if hold else "COMPLETE",
                    "logs": [{"logs": [], "outputs": []}],
                }
            self._send(HTTPStatus.OK, {"id": task_id})
            return
        if path.startswith(f"{BASE_PATH}/tasks/") and path.endswith(":cancel"):
            task_id = path.removeprefix(f"{BASE_PATH}/tasks/").removesuffix(":cancel")
            with LOCK:
                task = TASKS.get(task_id)
                state = task["state"] if task is not None else None
                if task is not None and state == "RUNNING":
                    task["state"] = "CANCELED"
            if TES_10 and state is None:
                self._send(HTTPStatus.NOT_FOUND, {"message": "not found"})
            elif TES_10 and state != "RUNNING":
                self._send(HTTPStatus.BAD_REQUEST, {"message": "no running pods"})
            else:
                self._send(HTTPStatus.OK, {})
            return
        self._send(HTTPStatus.NOT_FOUND, {"msg": "not found"})


def main() -> None:
    """Serve the fake TES API on all interfaces (in a container)."""
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)  # noqa: S104
    server.serve_forever()


if __name__ == "__main__":
    main()
