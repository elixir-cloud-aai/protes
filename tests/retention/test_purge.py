"""Unit tests of the purge of finished tasks (`pro_tes.retention.purge`)."""

from datetime import UTC, datetime, timedelta
from typing import Any

from bson.objectid import ObjectId
from foca.security.access_control.foca_casbin_adapter.adapter import Adapter
from pymongo import MongoClient
from pymongo.collection import Collection
import pytest

from pro_tes.ga4gh.tes.states import States
from pro_tes.retention.purge import TASKS_PATH, finished_at, purge_finished_tasks
from pro_tes.utils.db import DELETED_FIELD
from tests.conftest import DB

NOW = datetime(2026, 11, 7, 12, tzinfo=UTC)
DAY = timedelta(days=1)


@pytest.fixture
def tasks(mongo_client: MongoClient[dict[str, Any]]) -> Collection[dict[str, Any]]:
    """Collection of the tasks."""
    return mongo_client[DB]["tasks"]


@pytest.fixture
def rules(mongo_client: MongoClient[dict[str, Any]]) -> Adapter:
    """Adapter of the rules of access control."""
    return Adapter(collection=mongo_client[DB]["policy_rules"])


def store_task(
    tasks: Collection[dict[str, Any]],
    task_id: str,
    state: str = "COMPLETE",
    stored: datetime = NOW - 40 * DAY,
    end_time: str | None = None,
    creation_time: str | None = None,
) -> None:
    """Store a task.

    Args:
        tasks: Collection of the tasks.
        task_id: Identifier of the task.
        state: State of the task.
        stored: Time the task was stored (of its document identifier).
        end_time: End time of its last log, if any.
        creation_time: Creation time of the task, if any.
    """
    task: dict[str, Any] = {"id": task_id, "state": state}
    if creation_time is not None:
        task["creation_time"] = creation_time
    if end_time is not None:
        task["logs"] = [{"end_time": None}, {"end_time": end_time}]
    # unique identifiers of the same time
    _id = ObjectId(ObjectId.from_datetime(stored).binary[:4] + ObjectId().binary[4:])
    tasks.insert_one({"_id": _id, "task": task, "user_id": "alice"})


def grant_owner(rules: Adapter, task_id: str, subject: str = "user:alice") -> None:
    """Store the owner roles of a subject on a task.

    Args:
        rules: Adapter of the rules.
        task_id: Identifier of the task.
        subject: Subject, with prefix.
    """
    for resource in (f"{TASKS_PATH}/{task_id}", f"{TASKS_PATH}/{task_id}:cancel"):
        rules.add_policy("g", "g2", [subject, "owner", resource])


def purge(
    tasks: Collection[dict[str, Any]],
    rules: Adapter,
    finished_days: int = 30,
    batch_size: int = 500,
) -> int:
    """Purge tasks, at `NOW`.

    Args:
        tasks: Collection of the tasks.
        rules: Adapter of the rules.
        finished_days: Days after which finished tasks are deleted.
        batch_size: Maximum number of tasks deleted.

    Returns:
        Number of tasks deleted.
    """
    return purge_finished_tasks(
        tasks=tasks,
        rules=rules,
        finished_days=finished_days,
        batch_size=batch_size,
        now=NOW,
    )


@pytest.mark.parametrize(
    ("task", "expected"),
    [
        (
            {"logs": [{"end_time": "2026-10-01T00:00:00Z"}], "creation_time": "x"},
            datetime(2026, 10, 1, tzinfo=UTC),
        ),
        (
            {"logs": [{"end_time": "2026-10-01T00:00:00"}]},
            datetime(2026, 10, 1, tzinfo=UTC),
        ),
        (
            {"logs": [{}], "creation_time": "2026-09-01T00:00:00+02:00"},
            datetime(2026, 8, 31, 22, tzinfo=UTC),
        ),
        (
            {"logs": [{"end_time": "invalid"}], "creation_time": 5},
            datetime(2026, 7, 1, tzinfo=UTC),
        ),
        ({"logs": ["invalid"]}, datetime(2026, 7, 1, tzinfo=UTC)),
        ({"logs": []}, datetime(2026, 7, 1, tzinfo=UTC)),
    ],
)
def test_finished_at(task: dict[str, Any], expected: datetime) -> None:
    """Retention counts from the last end time, else creation, else storage."""
    stored = ObjectId.from_datetime(datetime(2026, 7, 1, tzinfo=UTC))
    assert finished_at({"_id": stored, "task": task}) == expected


@pytest.mark.parametrize(
    ("polled_at", "expected"),
    [
        (
            datetime(2026, 10, 2, tzinfo=UTC).timestamp(),
            datetime(2026, 10, 2, tzinfo=UTC),
        ),
        (
            datetime(2026, 9, 1, tzinfo=UTC).timestamp(),
            datetime(2026, 10, 1, tzinfo=UTC),
        ),
        (True, datetime(2026, 10, 1, tzinfo=UTC)),
        ("2026-10-02", datetime(2026, 10, 1, tzinfo=UTC)),
    ],
)
def test_finished_at_polled(polled_at: Any, expected: datetime) -> None:
    """The last poll counts if it is later (and a number of seconds)."""
    document = {
        "_id": ObjectId(),
        "task": {"logs": [{"end_time": "2026-10-01T00:00:00+00:00"}]},
        "tracking": {"polled_at": polled_at},
    }
    assert finished_at(document) == expected


def test_purge(tasks: Collection[dict[str, Any]], rules: Adapter) -> None:
    """Finished tasks past the retention time become tombstones.

    Their owner roles are removed, also those of other subjects; others are
    kept.
    """
    store_task(tasks, "OLD", end_time=(NOW - 31 * DAY).isoformat())
    store_task(tasks, "NEW", end_time=(NOW - 29 * DAY).isoformat())
    for task_id in ("OLD", "NEW"):
        grant_owner(rules, task_id)
    grant_owner(rules, "OLD", subject="group:team")
    assert purge(tasks, rules) == 1
    old = tasks.find_one({"task.id": "OLD"})
    assert old is not None
    assert old.keys() == {"_id", "task", DELETED_FIELD}
    assert old["task"] == {"id": "OLD"}
    assert old[DELETED_FIELD].replace(tzinfo=UTC) == NOW
    resources = [rule["v2"] for rule in rules._collection.find({"ptype": "g2"})]
    assert sorted(resources) == [f"{TASKS_PATH}/NEW", f"{TASKS_PATH}/NEW:cancel"]
    new = tasks.find_one({"task.id": "NEW"})
    assert new is not None and new["task"]["state"] == "COMPLETE"


@pytest.mark.parametrize("state", States.UNFINISHED)
def test_unfinished_kept(
    tasks: Collection[dict[str, Any]], rules: Adapter, state: str
) -> None:
    """Unfinished tasks are kept, however old."""
    store_task(tasks, "OLD", state=state)
    grant_owner(rules, "OLD")
    assert purge(tasks, rules) == 0
    assert rules._collection.count_documents({"ptype": "g2"}) == 2


@pytest.mark.parametrize("state", States.FINISHED)
def test_finished_states(
    tasks: Collection[dict[str, Any]], rules: Adapter, state: str
) -> None:
    """Tasks in each finished state are purged, by their creation time."""
    store_task(tasks, "OLD", state=state, creation_time=(NOW - 31 * DAY).isoformat())
    assert purge(tasks, rules) == 1


def test_creation_time(tasks: Collection[dict[str, Any]], rules: Adapter) -> None:
    """Without end time, retention counts from creation."""
    store_task(tasks, "OLD", creation_time=(NOW - 31 * DAY).isoformat())
    store_task(tasks, "NEW", creation_time=(NOW - 29 * DAY).isoformat())
    assert purge(tasks, rules) == 1
    assert tasks.count_documents({DELETED_FIELD: {"$exists": False}}) == 1


def test_stored_recently(tasks: Collection[dict[str, Any]], rules: Adapter) -> None:
    """Tasks stored within the retention time are not considered."""
    store_task(tasks, "NEW", stored=NOW - DAY, end_time=(NOW - 31 * DAY).isoformat())
    assert purge(tasks, rules) == 0


def test_batch_size(tasks: Collection[dict[str, Any]], rules: Adapter) -> None:
    """At most `batch_size` tasks are deleted per run, the oldest first."""
    for index in range(5):
        store_task(tasks, f"T{index}", stored=NOW - (40 - index) * DAY)
    assert purge(tasks, rules, batch_size=2) == 2
    deleted = tasks.find({DELETED_FIELD: {"$exists": True}})
    assert sorted(document["task"]["id"] for document in deleted) == ["T0", "T1"]
    assert purge(tasks, rules, batch_size=2) == 2
    assert purge(tasks, rules, batch_size=2) == 1
    assert purge(tasks, rules, batch_size=2) == 0


def test_idempotent(tasks: Collection[dict[str, Any]], rules: Adapter) -> None:
    """Tombstones are not purged again."""
    store_task(tasks, "OLD")
    assert purge(tasks, rules) == 1
    assert purge(tasks, rules) == 0
    assert tasks.count_documents({}) == 1


def test_without_identifier(tasks: Collection[dict[str, Any]], rules: Adapter) -> None:
    """Tasks without identifier are skipped (tombstones need one)."""
    tasks.insert_one(
        {"_id": ObjectId.from_datetime(NOW - 40 * DAY), "task": {"state": "COMPLETE"}}
    )
    assert purge(tasks, rules) == 0


def test_state_changed(
    tasks: Collection[dict[str, Any]],
    rules: Adapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A task no longer finished when it would be replaced is kept.

    E.g., if a worker of an earlier version stored another state meanwhile.
    """
    store_task(tasks, "OLD")

    def remove_roles(rules: Adapter, task_id: str) -> None:
        """Change the state of the task instead of removing its roles."""
        tasks.update_one({"task.id": task_id}, {"$set": {"task.state": "RUNNING"}})

    monkeypatch.setattr("pro_tes.retention.purge.remove_owner_roles", remove_roles)
    assert purge(tasks, rules) == 0
    document = tasks.find_one({"task.id": "OLD"})
    assert document is not None and document["task"]["state"] == "RUNNING"


def test_logged(
    tasks: Collection[dict[str, Any]],
    rules: Adapter,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Deletions are logged, by number."""
    store_task(tasks, "OLD")
    with caplog.at_level("INFO"):
        purge(tasks, rules)
    assert "Deleted 1 tasks finished more than 30 days ago" in caplog.text


def test_finished_recently(tasks: Collection[dict[str, Any]], rules: Adapter) -> None:
    """Tasks stored long ago, but finished recently, are kept.

    They don't count against `batch_size`.
    """
    store_task(
        tasks, "LONG", stored=NOW - 50 * DAY, end_time=(NOW - 5 * DAY).isoformat()
    )
    store_task(tasks, "OLD", stored=NOW - 45 * DAY)
    assert purge(tasks, rules, batch_size=1) == 1
    kept = tasks.find_one({"task.id": "LONG"})
    assert kept is not None and DELETED_FIELD not in kept
    old = tasks.find_one({"task.id": "OLD"})
    assert old is not None and DELETED_FIELD in old


def test_change_recorded(tasks: Collection[dict[str, Any]], rules: Adapter) -> None:
    """Removed owner roles are recorded, for the enforcers of all instances."""
    store_task(tasks, "OLD")
    grant_owner(rules, "OLD")
    before = rules.get_changes().version
    purge(tasks, rules)
    assert rules.get_changes().version > before


def test_long_task_finished_recently(
    tasks: Collection[dict[str, Any]], rules: Adapter
) -> None:
    """Tasks that ran longer than the retention time are kept after they finish.

    The log of proTES ends when the task is forwarded (`logs[].end_time`), so
    the time of the poll that stored the final state (`tracking.polled_at`)
    counts, if it is later.
    """
    store_task(tasks, "LONG", end_time=(NOW - 40 * DAY).isoformat())
    polled_at = (NOW - timedelta(hours=1)).timestamp()
    tasks.update_one({"task.id": "LONG"}, {"$set": {"tracking.polled_at": polled_at}})
    assert purge(tasks, rules) == 0
    tasks.update_one(
        {"task.id": "LONG"},
        {"$set": {"tracking.polled_at": (NOW - 31 * DAY).timestamp()}},
    )
    assert purge(tasks, rules) == 1
