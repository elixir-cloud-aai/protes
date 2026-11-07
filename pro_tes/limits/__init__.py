"""Limits of the tasks per user (opt-in).

Deployments may limit the number of unfinished tasks (`max_unfinished`) and
of stored tasks (`max_stored`) of each user, so that a single user can't
occupy the TES instances, the Celery workers or the database; users with one
of the `exempt_roles` are not limited. Off by default (cf.
`custom.controllers.post_task.limits`).

The module is self-contained: proTES calls :py:func:`check_task_limits` once
per new task, before its plugin pipeline runs, and the tasks have an index on
their owner and state (`db` in `config.yaml`). It uses no other part of
proTES than the states of tasks and the fields of stored tasks (`user_id`,
`task.state`).
"""

from pro_tes.limits.check import TaskLimitReached, check_task_limits
from pro_tes.limits.config import TaskLimits

__all__ = ["TaskLimitReached", "TaskLimits", "check_task_limits"]
