"""Records of the resources that finished tasks used."""

from collections.abc import Iterable
from datetime import UTC, datetime
from importlib.metadata import version
import logging
import math
from typing import Any

from pymongo.collection import Collection
from pymongo.errors import DuplicateKeyError

from pro_tes.accounting.tool import TAG_KEYS, task_tool

logger = logging.getLogger(__name__)

# Largest size (in bytes) that records hold: MongoDB's 64-bit integers
MAX_SIZE = 2**63 - 1

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


def _endpoints(log: Any) -> tuple[datetime | None, datetime | None]:
    """Get the start and the end of an attempt, each if known.

    Those of :py:func:`_span` if both are known; else each of the task log,
    or else of its executors (the earliest start, the latest end), so that
    a known start is kept without end, and vice versa.

    Args:
        log: Task log of the TES instance (`tes.TaskLog`).

    Returns:
        Start and end, each `None` if unknown; both `None` if the end is
        before the start.
    """
    span = _span(log)
    if span is not None:
        return span
    executors = getattr(log, "logs", None) or []
    start = _utc(getattr(log, "start_time", None)) or min(
        (
            time
            for time in (_utc(getattr(item, "start_time", None)) for item in executors)
            if time is not None
        ),
        default=None,
    )
    end = _utc(getattr(log, "end_time", None)) or max(
        (
            time
            for time in (_utc(getattr(item, "end_time", None)) for item in executors)
            if time is not None
        ),
        default=None,
    )
    if start is not None and end is not None:
        return None, None
    return start, end


def _amount(value: Any) -> float | None:
    """Get the amount of a requested resource.

    Args:
        value: The amount, as stored.

    Returns:
        The amount; `None` if it is no finite, non-negative number.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    amount = float(value)
    return amount if math.isfinite(amount) and amount >= 0 else None


def _outputs_bytes(log: Any) -> int | None:
    """Get the total size of the outputs of an attempt of a task.

    Args:
        log: Task log of the TES instance (`tes.TaskLog`).

    Returns:
        The sum of the sizes of its output logs (`outputs[].size_bytes`,
        integers or strings of digits); `None` if there are none, if any
        size is missing or invalid (the sum would be partial), or if a size
        or the sum exceeds what MongoDB stores (64-bit integers).
    """
    sizes = []
    for output in getattr(log, "outputs", None) or []:
        size = getattr(output, "size_bytes", None)
        if isinstance(size, str) and size.isascii() and size.isdigit():
            # longer strings can't be 64-bit integers (and `int()` refuses
            # strings of more than 4300 digits, since Python 3.11)
            if len(size) > len(str(MAX_SIZE)):
                return None
            size = int(size)
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            return None
        sizes.append(size)
    total = sum(sizes)
    return total if sizes and total <= MAX_SIZE else None


def _attempt(log: Any) -> dict[str, Any]:
    """Get the record of an attempt of a task at its TES instance.

    Args:
        log: Task log of the TES instance (`tes.TaskLog`).

    Returns:
        Its start and end, each if known (cf. :py:func:`_endpoints`), and
        the total size of its outputs (`outputs_bytes`), if logged.
    """
    start, end = _endpoints(log)
    return {"start_time": start, "end_time": end, "outputs_bytes": _outputs_bytes(log)}


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
    whose run time is unknown (`timed` false) count 0 seconds. Amounts that
    are not finite, non-negative numbers, or whose resource-seconds would not
    be finite, are not accounted (`None`, 0 resource-seconds), so that
    records stay valid JSON (totals too, cf. :py:func:`usage_totals`).

    For execution reports (cf. :py:mod:`pro_tes.accounting.reports`), the
    record also holds the tool of the task (`tool`, cf.
    :py:func:`pro_tes.accounting.tool.task_tool`), the start, end and total
    size of the outputs of each attempt (`attempts`, in the order of the task
    logs), and the version of proTES (`protes_version`); nothing else of the
    task.

    Args:
        document: Stored task, with its owner (`user_id`), identifier
            (`task.id`), requested resources (`task.resources`), and the
            images of its executors (`task.executors[].image`) and its tags
            with tool identifiers (`task.tags`), if any.
        logs: Task logs of the TES instance (`tes.TaskLog`).
        backend: URL of the TES instance.
        state: Final state of the task at its TES instance.
        now: Current time, in UTC.

    Returns:
        The record, by task identifier (`_id`).
    """
    logs = list(logs)
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
        product = (amount or 0.0) * seconds
        if not math.isfinite(product):
            amount, product = None, 0.0
        record[name] = amount
        record[field] = product
    # for execution reports (cf. :py:mod:`pro_tes.accounting.reports`)
    record["tool"] = task_tool(task)
    record["attempts"] = [_attempt(log) for log in logs]
    record["protes_version"] = version("pro-tes")
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
    stores its final state is retried), unless it is untimed and the earlier
    one is timed: a poll delivered again gets the task without its full logs,
    which may lack the times.

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
            # for the tool of execution reports only
            "task.executors.image": True,
            **{f"task.tags.{key}": True for key in TAG_KEYS},
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
    if record["timed"]:
        usage.replace_one({"_id": record["_id"]}, record, upsert=True)
        return
    try:
        usage.insert_one(record)
    except DuplicateKeyError:
        # the filter keeps a timed record: an untimed one is then dropped
        usage.replace_one({"_id": record["_id"], "timed": {"$ne": True}}, record)
