"""Check of the limits of the tasks per user."""

from collections.abc import Iterable
import logging
from typing import Any

from foca.plugins import GatewayError
from pymongo.collection import Collection

from pro_tes.ga4gh.tes.states import States
from pro_tes.limits.config import TaskLimits

logger = logging.getLogger(__name__)

# Documentation of the limits, the problem type of their errors
PROBLEM_TYPE = "https://protes.readthedocs.io/en/latest/configuration/#task-limits"


class TaskLimitReached(GatewayError):
    """A user has as many tasks as a limit allows; the new task is rejected.

    A gateway error, so that the controller of `POST /tasks` answers with its
    problem details (`429 Too Many Requests`, with the limit in `detail`).

    Attributes:
        limit: Name of the setting of the limit (e.g., `max_unfinished`).
    """

    code = 429
    title = "Too Many Requests"
    slug = "task-limit-reached"

    def __init__(self, detail: str, limit: str) -> None:
        """Create the error.

        Args:
            detail: Description of the problem, for the client.
            limit: Name of the setting of the limit.
        """
        super().__init__(detail)
        self.limit = limit
        self.problem_type = PROBLEM_TYPE


def check_task_limits(
    collection: Collection[dict[str, Any]],
    owner: str,
    roles: Iterable[str],
    limits: TaskLimits,
) -> None:
    """Check that a user may create another task.

    The tasks of the user (`user_id`) are counted, unfinished ones and all
    stored ones, up to the limit each, before the new task is stored. The
    limits are soft: concurrent requests of a user may each count the
    tasks before the others store theirs, so that the user may have up to
    one more task per concurrent request.

    Args:
        collection: Database collection of the tasks.
        owner: Owner of the new task.
        roles: Roles of the user (cf. :py:class:`foca.plugins.Principal`).
        limits: The limits.

    Raises:
        TaskLimitReached: The user has as many unfinished or stored tasks
            as allowed, and none of the exempt roles.
    """
    if limits.max_unfinished is None and limits.max_stored is None:
        return
    if set(roles) & set(limits.exempt_roles):
        return
    checks = (
        (
            "max_unfinished",
            limits.max_unfinished,
            {"user_id": owner, "task.state": {"$in": States.UNFINISHED}},
            "unfinished",
        ),
        ("max_stored", limits.max_stored, {"user_id": owner}, "stored"),
    )
    for name, limit, query, kind in checks:
        if limit is None:
            continue
        if collection.count_documents(query, limit=limit) >= limit:
            logger.info("Task rejected: limit `%s` reached", name)
            raise TaskLimitReached(
                f"Limit of {kind} tasks per user reached: at most {limit} (`{name}`)",
                limit=name,
            )
