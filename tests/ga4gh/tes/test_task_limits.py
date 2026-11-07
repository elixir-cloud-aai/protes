"""Tests of the limits of the tasks per user at `POST /tasks` (`pro_tes.limits`)."""

from typing import Any

from connexion import FlaskApp
from flask.testing import FlaskClient
from foca import get_custom_config
from pymongo import MongoClient

from pro_tes.config_models import CustomConfig
from pro_tes.limits import TaskLimits
from tests.conftest import ADMIN_USER, DB, OTHER_USER, USER, make_headers
from tests.ga4gh.tes.conftest import BASE_PATH, TASK, FakeBackend, relayed, set_state


def set_limits(app: FlaskApp, **limits: Any) -> None:
    """Set the limits of the app.

    Args:
        app: The app.
        **limits: Settings of the limits.
    """
    with app.app.app_context():
        config = get_custom_config(CustomConfig)
        config.controllers.post_task.limits = TaskLimits(**limits)


def test_create_task_limited(
    app: FlaskApp,
    client: FlaskClient,
    backend: type[FakeBackend],
    mongo_client: MongoClient[dict[str, Any]],
) -> None:
    """Tasks over a limit are rejected (`429`), and neither stored nor relayed.

    The problem details name the limit. Once a task has finished, the user
    may create another one; other users are not affected.
    """
    set_limits(app, max_unfinished=1)
    first = client.post(f"{BASE_PATH}/tasks", json=TASK)
    assert first.status_code == 200
    response = client.post(f"{BASE_PATH}/tasks", json=TASK)
    assert response.status_code == 429
    problem = response.get_json()
    assert problem["status"] == 429
    assert problem["title"] == "Too Many Requests"
    assert "`max_unfinished`" in problem["detail"]
    assert len(relayed(backend)) == 1
    assert mongo_client[DB]["tasks"].count_documents({}) == 1
    other = client.post(
        f"{BASE_PATH}/tasks", json=TASK, headers=make_headers(user=OTHER_USER)
    )
    assert other.status_code == 200
    set_state(mongo_client, first.get_json()["id"], "COMPLETE")
    assert client.post(f"{BASE_PATH}/tasks", json=TASK).status_code == 200


def test_create_task_stored_limit(
    app: FlaskApp,
    client: FlaskClient,
    backend: type[FakeBackend],
    mongo_client: MongoClient[dict[str, Any]],
) -> None:
    """Finished tasks count against `max_stored`."""
    set_limits(app, max_stored=1)
    first = client.post(f"{BASE_PATH}/tasks", json=TASK)
    set_state(mongo_client, first.get_json()["id"], "COMPLETE")
    response = client.post(f"{BASE_PATH}/tasks", json=TASK)
    assert response.status_code == 429
    assert "`max_stored`" in response.get_json()["detail"]
    assert mongo_client[DB]["tasks"].count_documents({"user_id": USER}) == 1


def test_create_task_exempt(
    app: FlaskApp,
    client: FlaskClient,
    backend: type[FakeBackend],
) -> None:
    """Users with an exempt role (from their groups) are not limited."""
    set_limits(app, max_unfinished=1, exempt_roles=["admin"])
    headers = make_headers(user=ADMIN_USER)
    for _ in range(2):
        response = client.post(f"{BASE_PATH}/tasks", json=TASK, headers=headers)
        assert response.status_code == 200
    assert len(relayed(backend)) == 2


def test_create_task_unlimited(client: FlaskClient, backend: type[FakeBackend]) -> None:
    """By default, users may create any number of tasks."""
    for _ in range(3):
        assert client.post(f"{BASE_PATH}/tasks", json=TASK).status_code == 200
    assert len(relayed(backend)) == 3


def test_owner_state_index(
    app: FlaskApp, mongo_client: MongoClient[dict[str, Any]]
) -> None:
    """The tasks have an index on their owner and state, for the counts."""
    keys = [
        list(index["key"])
        for index in mongo_client[DB]["tasks"].index_information().values()
    ]
    assert [("user_id", 1), ("task.state", 1)] in keys
