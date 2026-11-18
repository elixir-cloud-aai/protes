"""Tests of the records of the resources that finished tasks used."""

from datetime import UTC, datetime, timedelta, timezone
from importlib.metadata import version
from types import SimpleNamespace
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
        "tool": None,
        "attempts": [
            {
                "start_time": START,
                "end_time": START + timedelta(hours=1),
                "outputs_bytes": None,
            }
        ],
        "protes_version": version("pro-tes"),
    }


def test_record_for_reports() -> None:
    """Records hold the tool, and the times and output sizes per attempt.

    For execution reports: the tool of the image of the first executor (not
    the commands, environment, inputs, outputs or other tags of the task),
    and per attempt (task log) its times, if known, and the total size of its
    outputs, if logged; not the URLs or paths of the outputs.
    """
    document = {
        "user_id": "alice@example.org",
        "task": {
            "id": "TASK01",
            "executors": [
                {
                    "image": "quay.io/biocontainers/bwa:0.7.18--he4a0461_1",
                    "command": ["bwa", "mem", "/data/alice.fq"],
                    "env": {"TOKEN": "secret"},
                }
            ],
            "tags": {"biotools_id": "bwa", "owner": "alice"},
        },
    }
    outputs = [
        tes.OutputFileLog(url="s3://bucket/alice.bam", path="/out/a", size_bytes="100"),
        tes.OutputFileLog(url="s3://bucket/alice.bai", path="/out/b", size_bytes="23"),
    ]
    usage = record(
        [
            tes.TaskLog(start_time=START),
            tes.TaskLog(
                start_time=START + timedelta(hours=1),
                end_time=START + timedelta(hours=2),
                outputs=outputs,
            ),
        ],
        document=document,
    )
    assert usage["tool"] == {
        "name": "bwa",
        "version": "0.7.18",
        "package_version": "0.7.18--he4a0461_1",
        "identifiers": [
            "https://bio.tools/bwa",
            "docker://quay.io/biocontainers/bwa",
        ],
    }
    assert usage["attempts"] == [
        {"start_time": None, "end_time": None, "outputs_bytes": None},
        {
            "start_time": START + timedelta(hours=1),
            "end_time": START + timedelta(hours=2),
            "outputs_bytes": 123,
        },
    ]


@pytest.mark.parametrize(
    ("outputs", "total"),
    [
        (None, None),
        ([], None),
        ([tes.OutputFileLog(size_bytes=None)], None),
        ([tes.OutputFileLog(size_bytes="-1"), SimpleNamespace(size_bytes="x")], None),
        ([tes.OutputFileLog(size_bytes="5"), SimpleNamespace(size_bytes="1e3")], 5),
        ([SimpleNamespace(size_bytes="7"), SimpleNamespace(size_bytes=True)], 7),
        ([object(), tes.OutputFileLog(size_bytes=0)], 0),
    ],
)
def test_record_outputs_size(outputs: Any, total: int | None) -> None:
    """The output size of an attempt is the sum of the valid logged sizes.

    Sizes (`size_bytes`, integers as py-tes converts them, or strings of
    digits) that are negative or no numbers are left out; without any, the
    size is unknown.
    """
    usage = record([SimpleNamespace(start_time=START, outputs=outputs)])
    assert usage["attempts"][0]["outputs_bytes"] == total


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


def test_record_usage_tool(
    collections: tuple[Collection[dict[str, Any]], Collection[dict[str, Any]]],
) -> None:
    """The stored record has the tool of the stored task."""
    tasks, usage = collections
    tasks.update_one(
        {"worker_id": "worker-1"},
        {
            "$set": {
                "task.executors": [{"image": "bwa:0.7.18", "command": ["bwa"]}],
                "task.tags": {"rrid": "SCR_010910", "other": "x"},
            }
        },
    )
    record_usage(
        tasks=tasks,
        usage=usage,
        worker_id="worker-1",
        response=tes.Task(state="COMPLETE", logs=[tes.TaskLog(start_time=START)]),
        backend="https://tes.example.org",
    )
    (stored,) = usage.find()
    assert stored["tool"] == {
        "name": "bwa",
        "version": "0.7.18",
        "package_version": None,
        "identifiers": [
            "https://identifiers.org/RRID/RRID:SCR_010910",
            "docker://bwa",
        ],
    }


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
