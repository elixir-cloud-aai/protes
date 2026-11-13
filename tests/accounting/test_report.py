"""Tests of the totals of the resources that tasks used, per user."""

from datetime import UTC, datetime, timedelta
import sys
from typing import Any

from pymongo import MongoClient
from pymongo.collection import Collection
import pytest
import tes

from pro_tes.accounting import usage_record, usage_totals
from tests.conftest import DB

START = datetime(2026, 11, 1, tzinfo=UTC)


def add(
    usage: Collection[dict[str, Any]],
    task_id: str,
    user_id: str | None,
    hours: float | None,
    day: int = 0,
) -> None:
    """Store the record of a task with 1 CPU core, 2 GB RAM and 3 GB disk.

    Args:
        usage: Collection of the records.
        task_id: Identifier of the task.
        user_id: Owner of the task.
        hours: Run time of the task; `None` if unknown.
        day: Day after `START` on which the task started.
    """
    start = START + timedelta(days=day)
    end = None if hours is None else start + timedelta(hours=hours)
    document = {
        "user_id": user_id,
        "task": {
            "id": task_id,
            "resources": {"cpu_cores": 1, "ram_gb": 2, "disk_gb": 3},
        },
    }
    usage.insert_one(
        usage_record(
            document=document,
            logs=[tes.TaskLog(start_time=start, end_time=end)],
            backend="https://tes.example.org",
            state="COMPLETE",
            now=start + timedelta(days=1),
        )
    )


@pytest.fixture
def usage(mongo_client: MongoClient[dict[str, Any]]) -> Collection[dict[str, Any]]:
    """Collection of records of tasks of two users and one without owner."""
    collection = mongo_client[DB]["usage"]
    add(collection, "A1", "alice", 1)
    add(collection, "A2", "alice", 2, day=10)
    add(collection, "A3", "alice", None, day=20)
    add(collection, "B1", "bob", 0.5)
    add(collection, "X1", None, 1)
    return collection


def test_totals(usage: Collection[dict[str, Any]]) -> None:
    """Records are summed up per user, in the order of the users."""
    totals = usage_totals(usage)
    assert [total["user_id"] for total in totals] == [None, "alice", "bob"]
    alice = totals[1]
    assert alice == {
        "user_id": "alice",
        "tasks": 3,
        "untimed_tasks": 1,
        "wall_seconds": 3 * 3600.0,
        "cpu_core_seconds": 3 * 3600.0,
        "ram_gb_seconds": 6 * 3600.0,
        "disk_gb_seconds": 9 * 3600.0,
    }
    assert totals[2]["wall_seconds"] == 1800.0


def test_totals_of_user(usage: Collection[dict[str, Any]]) -> None:
    """The totals may be limited to one user."""
    totals = usage_totals(usage, user_id="bob")
    assert [total["user_id"] for total in totals] == ["bob"]
    assert usage_totals(usage, user_id="carol") == []


def test_totals_in_time_range(usage: Collection[dict[str, Any]]) -> None:
    """The totals may be limited to tasks that finished in a time range.

    The start is included, the end is not; untimed tasks count by the time
    they were recorded.
    """
    since = START + timedelta(days=10, hours=2)
    until = START + timedelta(days=22)
    totals = usage_totals(usage, user_id="alice", since=since, until=until)
    assert totals[0]["tasks"] == 2
    assert totals[0]["untimed_tasks"] == 1
    assert totals[0]["wall_seconds"] == 2 * 3600.0
    totals = usage_totals(usage, user_id="alice", until=since)
    assert totals[0]["tasks"] == 1
    assert usage_totals(usage, since=START + timedelta(days=30)) == []


def test_totals_overflow(usage: Collection[dict[str, Any]]) -> None:
    """Sums too large for a float are the largest float, which JSON can hold."""
    for task in ("TASK01", "TASK02"):
        usage.insert_one(
            {
                "_id": task,
                "user_id": "carol",
                "timed": True,
                "finished_at": datetime(2026, 11, 13, tzinfo=UTC),
                "wall_seconds": 1.0,
                "cpu_core_seconds": 0.0,
                "ram_gb_seconds": 1e308,
                "disk_gb_seconds": 0.0,
            }
        )
    (total,) = usage_totals(usage, user_id="carol")
    assert total["ram_gb_seconds"] == sys.float_info.max
    assert total["wall_seconds"] == 2.0
    assert total["tasks"] == 2
