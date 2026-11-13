"""Totals of the resources that tasks used, per user."""

from datetime import datetime
from typing import Any

from pymongo.collection import Collection

from pro_tes.accounting.record import RESOURCES

# Fields of the records that are summed up per user
SUMMED = ("wall_seconds", *RESOURCES.values())


def usage_totals(
    usage: Collection[dict[str, Any]],
    user_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[dict[str, Any]]:
    """Get the totals of the resources that tasks used, per user.

    Args:
        usage: Database collection of the records (cf.
            :py:func:`pro_tes.accounting.record_usage`).
        user_id: Only the tasks of this user; all users if `None`.
        since: Only tasks that finished at or after this time (cf.
            `finished_at` of the records).
        until: Only tasks that finished before this time.

    Returns:
        Per user (`user_id`; `None` for tasks without owner), in the order
        of the user identifiers: the number of tasks (`tasks`), of those
        whose run time is unknown (`untimed_tasks`), and the sums of their
        run times (`wall_seconds`) and resource-seconds.
    """
    match: dict[str, Any] = {}
    if user_id is not None:
        match["user_id"] = user_id
    finished: dict[str, datetime] = {}
    if since is not None:
        finished["$gte"] = since
    if until is not None:
        finished["$lt"] = until
    if finished:
        match["finished_at"] = finished
    group: dict[str, Any] = {
        "_id": "$user_id",
        "tasks": {"$sum": 1},
        "untimed_tasks": {"$sum": {"$cond": ["$timed", 0, 1]}},
    }
    for field in SUMMED:
        group[field] = {"$sum": f"${field}"}
    totals = usage.aggregate(
        [{"$match": match}, {"$group": group}, {"$sort": {"_id": 1}}]
    )
    return [{"user_id": total.pop("_id"), **total} for total in totals]
