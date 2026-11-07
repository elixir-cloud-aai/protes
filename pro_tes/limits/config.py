"""Configuration of the limits of the tasks per user."""

from pydantic import BaseModel, ConfigDict, PositiveInt


class TaskLimits(BaseModel):
    """Limits of the tasks per user (`custom.controllers.post_task.limits`).

    Unknown keys are errors, as in the rest of the custom configuration.

    Args:
        max_unfinished: Maximum number of unfinished tasks per user (any
            state but those of finished tasks, also `UNKNOWN`); `None` for
            no limit.
        max_stored: Maximum number of stored tasks per user, finished or
            not; `None` for no limit.
        exempt_roles: Roles of users who are not limited (e.g., `admin`).

    Attributes:
        max_unfinished: Maximum number of unfinished tasks per user; `None`
            for no limit.
        max_stored: Maximum number of stored tasks per user; `None` for no
            limit.
        exempt_roles: Roles of users who are not limited.
    """

    model_config = ConfigDict(extra="forbid")

    max_unfinished: PositiveInt | None = None
    max_stored: PositiveInt | None = None
    exempt_roles: list[str] = []
