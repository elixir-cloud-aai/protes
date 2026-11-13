"""Records of the resources that finished tasks used."""

from collections.abc import Iterable
from datetime import UTC, datetime
import logging
from typing import Any

from pymongo.collection import Collection

logger = logging.getLogger(__name__)

# Requested resources of tasks (`task.resources`) that are accounted, by
# the field of their resource-seconds in the records
RESOURCES = {
    "cpu_cores": "cpu_core_seconds",
    "ram_gb": "ram_gb_seconds",
    "disk_gb": "disk_gb_seconds",
}


def _utc(value: Any) -> datetime | None:
    """Get a time of a log of a TES instance, in UTC.

    Args:
        value: The time, as parsed by py-tes.

    Returns:
        The time, in UTC if it has no time zone; `None` if it is no time.
    """
    if not isinstance(value, datetime):
        return None
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


def _span(log: Any) -> tuple[datetime, datetime] | None:
    """Get the start and end of an attempt of a task at its TES instance.

    The times of the task log, or else the earliest start and the latest
    end of its executors.

    Args:
        log: Task log of the TES instance (`tes.TaskLog`).

    Returns:
        Start and end; `None` if either is unknown, or the end is before the
        start.
    """
    start = _utc(getattr(log, "start_time", None))
    end = _utc(getattr(log, "end_time", None))
    if start is None or end is None:
        executors = getattr(log, "logs", None) or []
        starts = [_utc(getattr(item, "start_time", None)) for item in executors]
        ends = [_utc(getattr(item, "end_time", None)) for item in executors]
        start = min((time for time in starts if time is not None), default=None)
        end = max((time for time in ends if time is not None), default=None)
    if start is None or end is None or end < start:
        return None
    return start, end


def _amount(value: Any) -> float | None:
    """Get the amount of a requested resource.

    Args:
        value: The amount, as stored.

    Returns:
        The amount; `None` if it is no non-negative number.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if value >= 0 else None


def usage_record(
    document: dict[str, Any],
    logs: Iterable[Any],
    backend: str,
    state: str,
    now: datetime,
) -> dict[str, Any]:
    """Get the record of the resources that a finished task used.

    The run time is the sum of the times of the attempts of the task at its
    TES instance (its task logs) whose start and end are known; the
    resource-seconds are the requested resources times the run time. Tasks
    whose run time is unknown (`timed` false) count 0 seconds.

    Args:
        document: Stored task, with its owner (`user_id`), identifier
            (`task.id`) and requested resources (`task.resources`), if any.
        logs: Task logs of the TES instance (`tes.TaskLog`).
        backend: URL of the TES instance.
        state: Final state of the task at its TES instance.
        now: Current time, in UTC.

    Returns:
        The record, by task identifier (`_id`).
    """
    task = document.get("task") or {}
    spans = [span for span in map(_span, logs) if span is not None]
    seconds = sum((end - start).total_seconds() for start, end in spans)
    resources = task.get("resources") or {}
    record: dict[str, Any] = {
        "_id": task.get("id"),
        "task_id": task.get("id"),
        "user_id": document.get("user_id"),
        "backend": backend,
        "state": state,
        "start_time": min((start for start, _ in spans), default=None),
        "end_time": max((end for _, end in spans), default=None),
        "recorded_at": now,
        "timed": bool(spans),
        "wall_seconds": seconds,
        "preemptible": resources.get("preemptible") is True,
    }
    # the time ranges of reports select records by the end of the task, or
    # else by the time it was recorded
    record["finished_at"] = record["end_time"] or now
    for name, field in RESOURCES.items():
        amount = _amount(resources.get(name))
        record[name] = amount
        record[field] = (amount or 0.0) * seconds
    return record


def record_usage(
    tasks: Collection[dict[str, Any]],
    usage: Collection[dict[str, Any]],
    worker_id: str,
    response: Any,
    backend: str,
) -> None:
    """Record the resources that a task used, once it finished.

    The record replaces an earlier one of the task (e.g., if the poll that
    stores its final state is retried).

    Args:
        tasks: Database collection of the tasks.
        usage: Database collection of the records.
        worker_id: Worker identifier of the task.
        response: The finished task, as served by its TES instance
            (`tes.Task`).
        backend: URL of the TES instance.
    """
    document = tasks.find_one(
        {"worker_id": worker_id},
        projection={
            "_id": False,
            "user_id": True,
            "task.id": True,
            "task.resources": True,
        },
    )
    if document is None or not isinstance((document.get("task") or {}).get("id"), str):
        logger.warning("[%s] Usage not recorded: task not found", worker_id)
        return
    record = usage_record(
        document=document,
        logs=getattr(response, "logs", None) or [],
        backend=backend,
        state=str(response.state),
        now=datetime.now(UTC),
    )
    usage.replace_one({"_id": record["_id"]}, record, upsert=True)
