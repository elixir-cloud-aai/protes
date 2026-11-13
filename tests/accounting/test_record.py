"""Tests of the records of the resources that finished tasks used."""

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import mongomock
from pymongo import MongoClient
from pymongo.collection import Collection
import pytest
import tes

from pro_tes.accounting import Accounting, record_usage, usage_record

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
START = datetime(2026, 11, 13, 10, 0, tzinfo=UTC)
DOCUMENT: dict[str, Any] = {
    "user_id": "alice@example.org",
    "task": {
        "id": "TASK01",
        "resources": {
            "cpu_cores": 2,
            "ram_gb": 4.0,
            "disk_gb": 10,
            "preemptible": True,
        },
    },
}


def record(logs: list[Any], document: dict[str, Any] = DOCUMENT) -> dict[str, Any]:
    """Get the record of a task with logs.

    Args:
        logs: Task logs of the TES instance.
        document: Stored task.

    Returns:
        The record.
    """
    return usage_record(
        document=document,
        logs=logs,
        backend="https://tes.example.org",
        state="COMPLETE",
        now=NOW,
    )


def test_config_default() -> None:
    """Accounting is off by default, and unknown keys are errors."""
    assert not Accounting().enabled
    with pytest.raises(ValueError, match="extra"):
        Accounting(retention_days=1)  # type: ignore[call-arg]


def test_record_from_task_log() -> None:
    """The run time is that of the task log; resources are times run time."""
    usage = record([tes.TaskLog(start_time=START, end_time=START + timedelta(hours=1))])
    assert usage == {
        "_id": "TASK01",
        "task_id": "TASK01",
        "user_id": "alice@example.org",
        "backend": "https://tes.example.org",
        "state": "COMPLETE",
        "start_time": START,
        "end_time": START + timedelta(hours=1),
        "finished_at": START + timedelta(hours=1),
        "recorded_at": NOW,
        "timed": True,
        "wall_seconds": 3600.0,
        "preemptible": True,
        "cpu_cores": 2.0,
        "cpu_core_seconds": 7200.0,
        "ram_gb": 4.0,
        "ram_gb_seconds": 14400.0,
        "disk_gb": 10.0,
        "disk_gb_seconds": 36000.0,
    }


def test_record_from_executor_logs() -> None:
    """Without times of the task log, those of its executors count.

    From the earliest start to the latest end; times without time zone are
    UTC, others are converted to UTC.
    """
    plus_one = timezone(timedelta(hours=1))
    executors = [
        tes.ExecutorLog(
            start_time=START.replace(tzinfo=None),
            end_time=START.replace(tzinfo=None) + timedelta(minutes=10),
        ),
        tes.ExecutorLog(
            start_time=(START + timedelta(minutes=10)).astimezone(plus_one),
            end_time=(START + timedelta(minutes=30)).astimezone(plus_one),
        ),
        tes.ExecutorLog(),
    ]
    usage = record([tes.TaskLog(start_time=START, logs=executors)])
    assert usage["start_time"] == START
    assert usage["end_time"] == START + timedelta(minutes=30)
    assert usage["wall_seconds"] == 1800.0


def test_record_attempts_summed() -> None:
    """The times of several attempts (task logs) add up."""
    usage = record(
        [
            tes.TaskLog(start_time=START, end_time=START + timedelta(minutes=5)),
            tes.TaskLog(
                start_time=START + timedelta(hours=1),
                end_time=START + timedelta(hours=1, minutes=10),
            ),
        ]
    )
    assert usage["wall_seconds"] == 900.0
    assert usage["start_time"] == START
    assert usage["end_time"] == START + timedelta(hours=1, minutes=10)


@pytest.mark.parametrize(
    "logs",
    [
        [],
        [tes.TaskLog()],
        [tes.TaskLog(start_time=START)],
        [tes.TaskLog(start_time=START, end_time=START - timedelta(seconds=1))],
        [object()],
    ],
)
def test_record_untimed(logs: list[Any]) -> None:
    """Tasks without known run time count 0 seconds, as of their recording."""
    usage = record(logs)
    assert not usage["timed"]
    assert usage["wall_seconds"] == 0
    assert usage["cpu_core_seconds"] == 0
    assert usage["start_time"] is None
    assert usage["end_time"] is None
    assert usage["finished_at"] == NOW


@pytest.mark.parametrize(
    "resources",
    [
        None,
        {},
        {"cpu_cores": None, "ram_gb": True, "disk_gb": -1, "preemptible": "yes"},
        {"cpu_cores": "2"},
        {"cpu_cores": float("inf"), "ram_gb": float("nan"), "disk_gb": 1e306},
    ],
)
def test_record_without_resources(resources: dict[str, Any] | None) -> None:
    """Resources that are not requested (or invalid) are not accounted.

    Also amounts that are not finite, or whose resource-seconds would not be.
    """
    document = {"task": {"id": "TASK01", "resources": resources}}
    usage = record(
        [tes.TaskLog(start_time=START, end_time=START + timedelta(hours=1))],
        document=document,
    )
    assert usage["user_id"] is None
    assert usage["wall_seconds"] == 3600.0
    for name in ("cpu_cores", "ram_gb", "disk_gb"):
        assert usage[name] is None
    for field in ("cpu_core_seconds", "ram_gb_seconds", "disk_gb_seconds"):
        assert usage[field] == 0.0
    assert usage["preemptible"] is False


@pytest.fixture
def collections() -> tuple[Collection[dict[str, Any]], Collection[dict[str, Any]]]:
    """Collections of the tasks (with a task) and of the records."""
    client: MongoClient[dict[str, Any]] = mongomock.MongoClient()
    db = client["taskStore"]
    db["tasks"].insert_one({"worker_id": "worker-1", **DOCUMENT})
    return db["tasks"], db["usage"]


def test_record_usage(
    collections: tuple[Collection[dict[str, Any]], Collection[dict[str, Any]]],
) -> None:
    """The record of a task is stored once, by task identifier."""
    tasks, usage = collections
    response = tes.Task(
        state="COMPLETE",
        logs=[tes.TaskLog(start_time=START, end_time=START + timedelta(hours=1))],
    )
    for _ in range(2):
        record_usage(
            tasks=tasks,
            usage=usage,
            worker_id="worker-1",
            response=response,
            backend="https://tes.example.org",
        )
    records = list(usage.find())
    assert len(records) == 1
    assert records[0]["_id"] == "TASK01"
    assert records[0]["user_id"] == "alice@example.org"
    assert records[0]["state"] == "COMPLETE"
    assert records[0]["wall_seconds"] == 3600.0


def test_record_usage_without_task(
    collections: tuple[Collection[dict[str, Any]], Collection[dict[str, Any]]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Tasks that are gone (or have no identifier) are not recorded."""
    tasks, usage = collections
    tasks.insert_one({"worker_id": "worker-2", "task": {}})
    for worker_id in ("worker-0", "worker-2"):
        record_usage(
            tasks=tasks,
            usage=usage,
            worker_id=worker_id,
            response=tes.Task(state="COMPLETE"),
            backend="https://tes.example.org",
        )
    assert usage.count_documents({}) == 0
    assert "Usage not recorded: task not found" in caplog.text


def test_record_usage_untimed_keeps_timed(
    collections: tuple[Collection[dict[str, Any]], Collection[dict[str, Any]]],
) -> None:
    """An untimed record doesn't replace a timed one, but a timed one does.

    E.g., a poll delivered again, which gets the task without its full logs.
    """
    tasks, usage = collections
    timed = tes.Task(
        state="COMPLETE",
        logs=[tes.TaskLog(start_time=START, end_time=START + timedelta(hours=1))],
    )
    untimed = tes.Task(state="COMPLETE")
    for response in (untimed, timed, untimed):
        record_usage(
            tasks=tasks,
            usage=usage,
            worker_id="worker-1",
            response=response,
            backend="https://tes.example.org",
        )
    (stored,) = usage.find()
    assert stored["timed"]
    assert stored["wall_seconds"] == 3600.0
