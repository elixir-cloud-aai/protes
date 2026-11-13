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

The module is self-contained: the Celery worker calls
:py:func:`record_usage` once a task finished at its TES instance, before
its final state is stored, and the controller of `GET /usage` calls
:py:func:`usage_totals`. It uses no other part of proTES than the fields of
stored tasks (`user_id`, `task.id`, `task.resources`).
"""

from pro_tes.accounting.config import Accounting
from pro_tes.accounting.record import record_usage, usage_record
from pro_tes.accounting.report import usage_totals

__all__ = ["Accounting", "record_usage", "usage_record", "usage_totals"]
