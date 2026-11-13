"""Controllers for GA4GH TES API endpoints."""

from datetime import UTC, datetime
import logging
import re
from typing import Any

from connexion import request
from flask.wrappers import Response
from foca import get_collection, get_custom_config
from foca.plugins import GatewayError
from foca.plugins.adapter import (
    current_principal,
    inbound_from_request,
    problem,
    request_id,
)
from foca.utils.logging import log_traffic

from pro_tes.accounting import usage_totals
from pro_tes.config_models import CustomConfig
from pro_tes.exceptions import InvalidUsageTime
from pro_tes.ga4gh.tes.task_runs import TaskRuns
from pro_tes.utils.db import TASK_DB, USAGE_COLLECTION

# Date-times of RFC 3339 (section 5.6), whose time zone may be left out
RFC3339_DATE_TIME = re.compile(
    r"\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}:\d{2}(\.\d+)?"
    r"([Zz]|[+-]([01]\d|2[0-3]):[0-5]\d)?",
    re.ASCII,
)

# Get logger instance
logger = logging.getLogger(__name__)


# POST /tasks/{id}:cancel
@log_traffic
def CancelTask(
    id: str,
    *args: Any,
    **kwargs: Any,
) -> dict[str, Any]:
    """Cancel unfinished task.

    Args:
        id: Task identifier.
        *args: Variable length argument list.
        **kwargs: Arbitrary keyword arguments.
    """
    task_runs = TaskRuns()
    response = task_runs.cancel_task(id=id, **kwargs)
    return response


# POST /tasks
@log_traffic
def CreateTask(*args: Any, **kwargs: Any) -> tuple[Any, int, dict[str, str]] | Response:
    """Create task.

    The request is read here only: the task processing gets the task, the
    request path, and the inbound request and caller as plain data (cf.
    :py:mod:`foca.plugins.adapter`), not the request. Errors of the plugin
    engine get the problem details of the error (its status, title, detail
    and problem type).

    Args:
        *args: Variable length argument list.
        **kwargs: Arbitrary keyword arguments, with the task as `body`.

    Returns:
        Response to the client (by default, the task identifier), with its
        status and headers; or the problem details of an error of the plugin
        engine.
    """
    task_runs = TaskRuns()
    task = kwargs.pop("body", None)
    try:
        body, headers = task_runs.create_task(
            task=task,
            collection=request.path,
            inbound=inbound_from_request(task),
            principal=current_principal(),
            request_id=request_id(),
            **kwargs,
        )
    except GatewayError as exc:
        logger.warning("Task not created: %s (%s): %s", exc.title, exc.code, exc.detail)
        return problem(exc)
    return body, 200, headers


# GET /tasks/{id}
@log_traffic
def GetTask(id: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
    """Get info for individual task.

    Args:
        id: Task identifier.
        *args: Variable length argument list.
        **kwargs: Arbitrary keyword arguments.
    """
    task_runs = TaskRuns()
    response = task_runs.get_task(id=id, **kwargs)
    return response


# GET /tasks/{id}/provenance
@log_traffic
def GetTaskProvenance(id: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
    """Get the provenance of a task (proTES extension).

    Args:
        id: Task identifier.
        *args: Variable length argument list.
        **kwargs: Arbitrary keyword arguments.

    Returns:
        The provenance of the task, with its references expanded (cf.
        :py:meth:`pro_tes.ga4gh.tes.task_runs.TaskRuns.get_task_provenance`).
    """
    task_runs = TaskRuns()
    return task_runs.get_task_provenance(id=id)


# GET /usage
@log_traffic
def GetUsage(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Get the resources that tasks used, per user (proTES extension).

    For admins only (enforced by the policies).

    Args:
        *args: Variable length argument list.
        **kwargs: Arbitrary keyword arguments: the user (`user_id`) and the
            time range (`since`, `until`) of the tasks, if any.

    Returns:
        Whether resources are recorded (`recording`, cf.
        `custom.accounting.enabled`), and the totals per user (`users`, cf.
        :py:func:`pro_tes.accounting.usage_totals`).
    """
    totals = usage_totals(
        get_collection(TASK_DB, USAGE_COLLECTION),
        user_id=kwargs.get("user_id"),
        since=_parse_time(kwargs.get("since"), "since"),
        until=_parse_time(kwargs.get("until"), "until"),
    )
    return {
        "recording": get_custom_config(CustomConfig).accounting.enabled,
        "users": totals,
    }


def _parse_time(value: str | None, name: str) -> datetime | None:
    """Parse a time of a query parameter (RFC 3339).

    Args:
        value: The time, as sent.
        name: Name of the parameter.

    Returns:
        The time, in UTC (also if it has no time zone); `None` if not sent.
        Only RFC 3339 date-times are valid, except that the time zone may be
        left out, and that leap seconds (`:60`) are not.

    Raises:
        pro_tes.exceptions.InvalidUsageTime: The value is no valid time, or
            is out of range in UTC.
    """
    if value is None:
        return None
    # `fromisoformat` also takes other ISO 8601 forms (e.g., dates), and only
    # an uppercase `T` and `Z`, which RFC 3339 allows in lowercase too
    if RFC3339_DATE_TIME.fullmatch(value) is None:
        raise InvalidUsageTime(f"`{name}` is no valid time")
    try:
        parsed = datetime.fromisoformat(value.upper())
        # in UTC, as MongoDB stores times; out of its range (e.g., year 10000
        # in UTC) is invalid
        return (
            parsed.astimezone(UTC)
            if parsed.tzinfo is not None
            else parsed.replace(tzinfo=UTC)
        )
    except (ValueError, OverflowError) as exc:
        raise InvalidUsageTime(f"`{name}` is no valid time") from exc


# GET /tasks
@log_traffic
def ListTasks(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """List all available tasks.

    Args:
        *args: Variable length argument list.
        **kwargs: Arbitrary keyword arguments.
    """
    # Connexion sets the default of the schema of states (`UNKNOWN`) if the
    # request has no filter; no filter means all states (TES 1.1)
    if "state" not in request.args:
        kwargs.pop("state", None)
    # as sent: Connexion splits values at commas, which tags may contain
    for name in ("tag_key", "tag_value"):
        kwargs[name] = request.args.getlist(name)
    tasks_run = TaskRuns()
    response = tasks_run.list_tasks(collection=request.path, **kwargs)
    return response
