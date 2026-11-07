"""Configuration of the retention of finished tasks."""

from pydantic import BaseModel, ConfigDict, PositiveInt


class Retention(BaseModel):
    """Retention of finished tasks (`custom.retention`).

    Unknown keys are errors, as in the rest of the custom configuration.

    Args:
        finished_days: Days after which finished tasks are deleted, counted
            from the end of their last log (`logs[].end_time`), or from
            their creation if they have none, but not before their final
            state was stored (`tracking.polled_at`); `None` to keep all
            tasks.
        interval: Seconds between runs of the purge in each Celery worker.
        batch_size: Maximum number of tasks deleted per run.

    Attributes:
        finished_days: Days after which finished tasks are deleted; `None`
            to keep all tasks.
        interval: Seconds between runs of the purge in each Celery worker.
        batch_size: Maximum number of tasks deleted per run.
    """

    model_config = ConfigDict(extra="forbid")

    finished_days: PositiveInt | None = None
    interval: PositiveInt = 3600
    batch_size: PositiveInt = 500
