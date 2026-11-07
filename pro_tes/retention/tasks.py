"""Celery task and worker thread of the retention of finished tasks.

Registered with the Celery worker by `jobs.include` in `config.yaml`. When a
worker starts, it purges expired tasks (cf.
:py:func:`pro_tes.retention.purge.purge_finished_tasks`) and then every
`custom.retention.interval` seconds, from a daemon thread of its main
process, as it resumes the tracking of tasks; nothing runs unless
`custom.retention.finished_days` is set.
"""

import logging
from threading import Event, Thread
from typing import Any

from celery.signals import worker_ready
from foca import get_collection, get_config, get_custom_config
from foca.database.register_mongodb import get_mongo_db_name
from foca.security.access_control.foca_casbin_adapter.adapter import Adapter
from pymongo.collection import Collection

from pro_tes.celery_worker import celery
from pro_tes.config_models import CustomConfig
from pro_tes.retention.purge import purge_finished_tasks
from pro_tes.utils.db import TASK_COLLECTION, TASK_DB

logger = logging.getLogger(__name__)


def rules_adapter(tasks: Collection[dict[str, Any]]) -> Adapter:
    """Get an adapter of the stored rules of access control of the app.

    The Celery worker doesn't register access control, so the adapter is
    created on the collection of the rules of the app's config
    (`security.access_control`), with the MongoDB client of the tasks.

    Args:
        tasks: Database collection of the tasks.

    Returns:
        The adapter.
    """
    access_control = get_config().security.access_control
    database = tasks.database.client[get_mongo_db_name(access_control.db_name)]
    return Adapter(collection=database[access_control.collection_name])


@celery.task(name="tasks.purge_finished_tasks", ignore_result=True)
def task__purge_finished_tasks() -> int:
    """Delete finished tasks whose retention time has passed.

    Runs in the app context; does nothing unless
    `custom.retention.finished_days` is set.

    Returns:
        Number of tasks deleted.
    """
    retention = get_custom_config(CustomConfig).retention
    if retention.finished_days is None:
        return 0
    collection = get_collection(TASK_DB, TASK_COLLECTION)
    return purge_finished_tasks(
        tasks=collection,
        rules=rules_adapter(tasks=collection),
        finished_days=retention.finished_days,
        batch_size=retention.batch_size,
    )


def purge_periodically(stop: Event, interval: float) -> None:
    """Purge expired tasks every `interval` seconds.

    Args:
        stop: Event that ends the loop.
        interval: Seconds between runs of :py:func:`task__purge_finished_tasks`.
    """
    while not stop.wait(interval):
        try:
            task__purge_finished_tasks.delay()
        except Exception:
            logger.exception("Could not purge finished tasks")


@worker_ready.connect
def purge_on_start(**kwargs: Any) -> Thread | None:
    """Purge expired tasks when a worker starts, and periodically.

    Only if `custom.retention.finished_days` is set.

    Args:
        **kwargs: Arguments of the signal; ignored.

    Returns:
        Thread that purges expired tasks periodically; `None` if retention
        is off.
    """
    # signal handlers run outside of the app context (`get_custom_config`)
    custom = (celery.conf.foca.model_extra or {}).get("custom")
    config = custom if isinstance(custom, CustomConfig) else CustomConfig()
    retention = config.retention
    if retention.finished_days is None:
        return None
    task__purge_finished_tasks.delay()
    thread = Thread(
        target=purge_periodically,
        kwargs={"stop": Event(), "interval": retention.interval},
        name="purge-finished-tasks",
        daemon=True,
    )
    thread.start()
    return thread
