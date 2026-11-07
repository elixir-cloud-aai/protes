"""Tests of the retention of finished tasks with the app (`pro_tes.retention`).

Tasks are created via the API, with the fake TES backends of the tests of
the TES API (cf. `tests/retention/conftest.py`), and purged by the Celery
task, in the app context; tombstones are not found via the API.
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
import time
from typing import Any

from bson.objectid import ObjectId
from connexion import FlaskApp
from flask.testing import FlaskClient
from foca import get_custom_config
from pymongo import MongoClient
import pytest

from pro_tes.config_models import CustomConfig
from pro_tes.ga4gh.tes import task_runs
from pro_tes.retention import Retention
from pro_tes.retention.tasks import rules_adapter, task__purge_finished_tasks
from pro_tes.utils.db import DELETED_FIELD
from tests.conftest import ADMIN, DB, make_headers
from tests.ga4gh.tes.conftest import BASE_PATH, TASK, FakeBackend, stored

OLD = (datetime.now(UTC) - timedelta(days=40)).isoformat()


def age(mongo_client: MongoClient[dict[str, Any]], task_id: str) -> None:
    """Make a stored task finished 40 days ago, also by its document identifier.

    Args:
        mongo_client: Client of the database.
        task_id: Identifier of the task.
    """
    tasks = mongo_client[DB]["tasks"]
    document = stored(mongo_client, task_id)
    tasks.delete_one({"_id": document["_id"]})
    document["_id"] = ObjectId.from_datetime(datetime.fromisoformat(OLD))
    document["task"]["state"] = "COMPLETE"
    for log in document["task"]["logs"]:
        log["end_time"] = OLD
    tasks.insert_one(document)


def purge(app: FlaskApp, finished_days: int | None = 30) -> int:
    """Run the purge of the Celery worker, in the app context.

    Args:
        app: The app.
        finished_days: Days after which finished tasks are deleted.

    Returns:
        Number of tasks deleted.
    """
    with app.app.app_context():
        config = get_custom_config(CustomConfig)
        config.retention = Retention(finished_days=finished_days)
        deleted: int = task__purge_finished_tasks.run()
    return deleted


@pytest.fixture
def tombstone(task_id: str, mongo_client: MongoClient[dict[str, Any]]) -> Iterator[str]:
    """Identifier of a task replaced by a tombstone; its owner roles are kept.

    As if removing the roles had failed, or another instance still had them.
    """
    mongo_client[DB]["tasks"].replace_one(
        {"task.id": task_id},
        {"task": {"id": task_id}, DELETED_FIELD: datetime.now(UTC)},
    )
    yield task_id


def test_purged(
    app: FlaskApp,
    client: FlaskClient,
    task_id: str,
    backend: type[FakeBackend],
    mongo_client: MongoClient[dict[str, Any]],
) -> None:
    """Expired tasks are deleted with their owner roles; others are kept.

    Their owner can no longer access them, and they are no longer listed.
    """
    age(mongo_client, task_id)
    kept = client.post(f"{BASE_PATH}/tasks", json=TASK).get_json()["id"]
    assert purge(app) == 1
    assert client.get(f"{BASE_PATH}/tasks/{task_id}").status_code == 403
    listed = client.get(f"{BASE_PATH}/tasks").get_json()["tasks"]
    assert [task["id"] for task in listed] == [kept]
    rules = mongo_client[DB]["policy_rules"].find({"ptype": "g2"})
    assert all(kept in rule["v2"] for rule in rules)


def test_finished_recently_kept(
    app: FlaskApp, task_id: str, mongo_client: MongoClient[dict[str, Any]]
) -> None:
    """Tasks forwarded long ago, whose final state was stored lately, are kept."""
    age(mongo_client, task_id)
    mongo_client[DB]["tasks"].update_one(
        {"task.id": task_id}, {"$set": {"tracking.polled_at": time.time()}}
    )
    assert purge(app) == 0


def test_off_by_default(
    app: FlaskApp, task_id: str, mongo_client: MongoClient[dict[str, Any]]
) -> None:
    """Without `finished_days`, no task is deleted."""
    age(mongo_client, task_id)
    assert purge(app, finished_days=None) == 0
    assert stored(mongo_client, task_id)["task"]["state"] == "COMPLETE"


@pytest.mark.parametrize("path", ["", "?view=FULL", ":cancel"])
def test_tombstone_not_found(client: FlaskClient, tombstone: str, path: str) -> None:
    """Tombstones are not found, also by owners whose roles are left."""
    method = client.post if path == ":cancel" else client.get
    response = method(f"{BASE_PATH}/tasks/{tombstone}{path}")
    assert response.status_code == 404


def test_tombstone_provenance(client: FlaskClient, tombstone: str) -> None:
    """Tombstones have no provenance, also for admins."""
    for headers in ({}, make_headers(user=ADMIN)):
        response = client.get(
            f"{BASE_PATH}/tasks/{tombstone}/provenance", headers=headers
        )
        assert response.status_code == 404


def test_tombstone_not_listed(client: FlaskClient, tombstone: str) -> None:
    """Tombstones are never listed, in any view."""
    for view in ("MINIMAL", "BASIC", "FULL"):
        response = client.get(f"{BASE_PATH}/tasks?view={view}")
        assert response.get_json()["tasks"] == []


def test_identifier_not_reused(
    client: FlaskClient,
    backend: type[FakeBackend],
    monkeypatch: pytest.MonkeyPatch,
    mongo_client: MongoClient[dict[str, Any]],
    tombstone: str,
) -> None:
    """Identifiers of deleted tasks are not used for new tasks."""
    mongo_client[DB]["policy_rules"].delete_many({"ptype": "g2"})
    ids = iter([tombstone, "NEW123"])
    monkeypatch.setattr(task_runs, "generate_id", lambda **kwargs: next(ids))
    response = client.post(f"{BASE_PATH}/tasks", json=TASK)
    assert response.get_json()["id"] == "NEW123"


def test_rules_adapter(
    app: FlaskApp,
    monkeypatch: pytest.MonkeyPatch,
    mongo_client: MongoClient[dict[str, Any]],
) -> None:
    """The rules are those of the app's access control, also in `MONGO_DBNAME`."""
    with app.app.app_context():
        tasks = mongo_client[DB]["tasks"]
        collection = rules_adapter(tasks=tasks)._collection
        assert (collection.database.name, collection.name) == (DB, "policy_rules")
        monkeypatch.setenv("MONGO_DBNAME", "protes")
        assert rules_adapter(tasks=tasks)._collection.database.name == "protes"
