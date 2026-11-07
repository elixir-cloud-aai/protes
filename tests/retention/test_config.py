"""Tests of the config of the retention of finished tasks."""

from pathlib import Path
from typing import Any

from pydantic import ValidationError
import pytest
import yaml

from pro_tes.config_models import CustomConfig
from pro_tes.retention import Retention

ROOT = Path(__file__).parents[2]


def test_defaults() -> None:
    """Retention is off by default."""
    retention = CustomConfig().retention
    assert retention == Retention()
    assert retention.finished_days is None
    assert retention.interval == 3600
    assert retention.batch_size == 500


def test_base_config() -> None:
    """The base config sets retention as the model does: off."""
    config = yaml.safe_load((ROOT / "pro_tes" / "config.yaml").read_text())
    assert Retention.model_validate(config["custom"]["retention"]) == Retention()
    assert "pro_tes.retention.tasks" in config["jobs"]["include"]
    # the purge finds finished tasks oldest first, by index, not tombstones
    indexes = config["db"]["dbs"]["taskStore"]["collections"]["tasks"]["indexes"]
    assert {"task.state": 1, "_id": 1} in [index["keys"] for index in indexes]


@pytest.mark.parametrize(
    "settings",
    [{"finished_days": 0}, {"interval": 0}, {"batch_size": -1}, {"days": 1}],
)
def test_config_invalid(settings: dict[str, Any]) -> None:
    """Settings must be positive, and unknown keys are errors."""
    with pytest.raises(ValidationError):
        CustomConfig.model_validate({"retention": settings})
