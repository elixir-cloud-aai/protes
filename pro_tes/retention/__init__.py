"""Retention of finished tasks (opt-in).

Deployments may delete finished tasks a number of days after they finished
(`finished_days`), so that the database and the rules of access control
(owner roles, whose number slows down every request) stay bounded. Off by
default (cf. `custom.retention`), as many deployments must keep all tasks,
e.g., for accounting and audits.

A Celery worker purges expired tasks when it starts, then every `interval`
seconds, at most `batch_size` per run (cf.
:py:mod:`pro_tes.retention.tasks`). Each deleted task is replaced by a
tombstone with its identifier only, so that identifiers are never used
again; the API treats tombstones as not found.

The module is self-contained: it is registered with the Celery worker
(`jobs.include` in `config.yaml`) and uses no other part of proTES than the
states of tasks, the fields of stored tasks, the field of tombstones (cf.
:py:data:`pro_tes.utils.db.DELETED_FIELD`) and the resources of owner roles
(cf. :py:func:`pro_tes.ga4gh.tes.ownership.task_resources`).
"""

from pro_tes.retention.config import Retention

__all__ = ["Retention"]
