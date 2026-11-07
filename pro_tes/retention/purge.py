"""Purge of finished tasks after their retention time."""

from datetime import UTC, datetime, timedelta
import logging
from typing import Any

from bson.objectid import ObjectId
from foca.security.access_control.constants import OWNER_ROLE
from foca.security.access_control.enforcement import RESOURCE_ROLE_PTYPE
from foca.security.access_control.foca_casbin_adapter.adapter import Adapter
from pymongo.collection import Collection

from pro_tes.ga4gh.tes.ownership import task_resources
from pro_tes.ga4gh.tes.states import States
from pro_tes.gateway import TASKS_PATH
from pro_tes.utils.db import DELETED_FIELD

logger = logging.getLogger(__name__)


def _parse_time(value: Any) -> datetime | None:
    """Parse a time of a stored task (RFC 3339).

    Args:
        value: The time, as stored.

    Returns:
        The time, in UTC if it has no time zone; `None` if it is not a valid
        time.
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def finished_at(document: dict[str, Any]) -> datetime:
    """Get the time that the retention of a finished task counts from.

    Args:
        document: Stored task, with its identifier (`_id`), and its logs and
            creation time, if any.

    Returns:
        The end time of the last log of the task (`task.logs[].end_time`),
        or else the creation time of the task (`task.creation_time`), or
        else the time it was stored (by its document identifier).
    """
    task = document.get("task") or {}
    logs = task.get("logs")
    last = logs[-1] if isinstance(logs, list) and logs else None
    end_time = _parse_time(last.get("end_time")) if isinstance(last, dict) else None
    if end_time is not None:
        return end_time
    creation_time = _parse_time(task.get("creation_time"))
    if creation_time is not None:
        return creation_time
    stored: datetime = document["_id"].generation_time
    return stored


def remove_owner_roles(rules: Adapter, task_id: str) -> None:
    """Remove the owner roles on a task, of all subjects.

    Via FOCA's adapter, which records the change, so that the enforcers of
    all app instances drop the roles too.

    Args:
        rules: Adapter of the stored rules of access control.
        task_id: Identifier of the task.
    """
    for resource in task_resources(collection=TASKS_PATH, task_id=task_id):
        rules.remove_filtered_policy(
            "g", RESOURCE_ROLE_PTYPE, 1, OWNER_ROLE, resource, keep_configured=True
        )


def purge_finished_tasks(
    tasks: Collection[dict[str, Any]],
    rules: Adapter,
    finished_days: int,
    batch_size: int,
    now: datetime | None = None,
) -> int:
    """Delete finished tasks whose retention time has passed.

    Finished tasks (cf. :py:attr:`pro_tes.ga4gh.tes.states.States.FINISHED`)
    whose last log ended (or, without one, that were created) more than
    `finished_days` days ago are deleted, oldest first, at most `batch_size`
    per call. The owner roles on each are removed first, then the task is
    replaced by a tombstone with its identifier and the time of the
    deletion only, so that its identifier is never used again (cf.
    :py:meth:`pro_tes.ga4gh.tes.task_runs.TaskRuns._write_doc_to_db`), and
    only if it is still finished. Idempotent and safe with several workers:
    a task deleted by another worker meanwhile is skipped, and a task whose
    roles were removed, but which was not replaced (e.g., as the worker
    stopped), is purged on the next run.

    Args:
        tasks: Database collection of the tasks.
        rules: Adapter of the stored rules of access control.
        finished_days: Days after which finished tasks are deleted.
        batch_size: Maximum number of tasks deleted.
        now: Current time; the current time by default.

    Returns:
        Number of tasks deleted.
    """
    now = datetime.now(UTC) if now is None else now
    cutoff = now - timedelta(days=finished_days)
    # tasks stored after the cutoff were created, and ended, after it
    candidates = {
        "task.state": {"$in": States.FINISHED},
        DELETED_FIELD: {"$exists": False},
        "_id": {"$lt": ObjectId.from_datetime(cutoff)},
    }
    projection = ["task.id", "task.logs.end_time", "task.creation_time"]
    deleted = 0
    cursor = tasks.find(candidates, projection=projection).sort("_id", 1)
    try:
        for document in cursor:
            if deleted >= batch_size:
                break
            if finished_at(document) >= cutoff:
                continue
            task_id = (document.get("task") or {}).get("id")
            if not isinstance(task_id, str):
                # no tombstone without identifier (unique index)
                continue
            remove_owner_roles(rules=rules, task_id=task_id)
            result = tasks.replace_one(
                {"_id": document["_id"], "task.state": {"$in": States.FINISHED}},
                {"task": {"id": task_id}, DELETED_FIELD: now},
            )
            deleted += result.modified_count
    finally:
        cursor.close()
    if deleted:
        logger.info(
            "Deleted %d tasks finished more than %d days ago", deleted, finished_days
        )
    return deleted
