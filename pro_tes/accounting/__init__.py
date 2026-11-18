"""Accounting of the resources that tasks used, per user (opt-in).

Deployments may record, for each task that finished at its TES instance,
which resources it used: how long it ran (wall-clock time, from the logs of
its TES instance), and the resources it requested (`resources` of the task:
CPU cores, RAM, disk), multiplied by that time. Admins get the totals per
user via the API (`GET /usage`). Off by default (cf. `custom.accounting`).

TES instances don't report the resources that tasks consumed, so the records
hold allocations, as requested, not measurements; the time is that of the
logs of the TES instance (`logs[].start_time` and `end_time`, or else those
of its executors).

Each record holds the owner of the task (`user_id`, the subject identifier
that the task document holds already), the task identifier, the TES
instance, its final state, the times and the numbers; nothing else of the
task. Records are kept apart from the tasks, so that the retention of
finished tasks (:py:mod:`pro_tes.retention`) doesn't remove them; operators
delete them (see `docs/operations.md`).

Admins also get the records as execution reports of the Tool Execution
Report format of ELIXIR Compute (`GET /usage/reports`, cf.
:py:mod:`pro_tes.accounting.reports`): one per attempt of a task, with the
tool that it ran, the providers, the requested CPU cores, the times and the
rounded size of its outputs, but no user identifiers or other details of
the task. For these, the records also hold the tool (from the image of the
first executor and the tool identifiers in the tags of the task, cf.
:py:mod:`pro_tes.accounting.tool`), the times and output sizes of each
attempt, and the version of proTES.

The module is self-contained: the Celery worker calls
:py:func:`record_usage` once a task finished at its TES instance, before
its final state is stored, and the controllers of `GET /usage` and `GET
/usage/reports` call :py:func:`usage_totals` and
:py:func:`execution_reports`. It uses no other part of proTES than the
fields of stored tasks (`user_id`, `task.id`, `task.resources`,
`task.executors[].image`, `task.tags`).
"""

from pro_tes.accounting.config import Accounting, ExecutionReports
from pro_tes.accounting.record import record_usage, usage_record
from pro_tes.accounting.report import usage_totals
from pro_tes.accounting.reports import TooManyRecords, execution_reports

__all__ = [
    "Accounting",
    "ExecutionReports",
    "TooManyRecords",
    "execution_reports",
    "record_usage",
    "usage_record",
    "usage_totals",
]
