"""Configuration of the accounting of the resources that tasks used."""

from pydantic import BaseModel, ConfigDict


class Accounting(BaseModel):
    """Accounting of the resources that tasks used (`custom.accounting`).

    Unknown keys are errors, as in the rest of the custom configuration.

    Args:
        enabled: Whether the Celery workers record the resources of each
            task that finished at its TES instance; stored records are
            served either way.

    Attributes:
        enabled: Whether the resources of finished tasks are recorded.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
