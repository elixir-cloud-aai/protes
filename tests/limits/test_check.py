"""Unit tests of the limits of the tasks per user (`pro_tes.limits`)."""

from pathlib import Path
from typing import Any

from pydantic import ValidationError
from pymongo import MongoClient
import pytest
import yaml

from pro_tes.config_models import CustomConfig, PostTask
from pro_tes.ga4gh.tes.states import States
from pro_tes.limits import TaskLimitReached, TaskLimits, check_task_limits
from pro_tes.limits.check import PROBLEM_TYPE
from tests.conftest import DB

ROOT = Path(__file__).parents[2]

OWNER = "alice"


def store_tasks(
    mongo_client: MongoClient[dict[str, Any]], owner: str | None, *states: str
) -> None:
    """Store tasks of an owner directly, one per state.

    Args:
        mongo_client: Client of the database.
        owner: Owner of the tasks (`user_id`); `None` for tasks without one.
        *states: States of the tasks.
    """
    for index, state in enumerate(states):
        document: dict[str, Any] = {"task": {"id": f"{owner}-{index}", "state": state}}
        if owner is not None:
            document["user_id"] = owner
        mongo_client[DB]["tasks"].insert_one(document)


def check(
    mongo_client: MongoClient[dict[str, Any]],
    roles: tuple[str, ...] = ("user",),
    **limits: Any,
) -> None:
    """Check the limits of `OWNER`.

    Args:
        mongo_client: Client of the database.
        roles: Roles of the user.
        **limits: Settings of the limits.
    """
    check_task_limits(
        collection=mongo_client[DB]["tasks"],
        owner=OWNER,
        roles=roles,
        limits=TaskLimits(**limits),
    )


def test_defaults() -> None:
    """Limits are off by default, and nobody is exempt."""
    limits = PostTask().limits
    assert limits == TaskLimits()
    assert limits.max_unfinished is None
    assert limits.max_stored is None
    assert limits.exempt_roles == []


@pytest.mark.parametrize(
    "settings",
    [{"max_unfinished": 0}, {"max_stored": -1}, {"max_tasks": 1}],
)
def test_config_invalid(settings: dict[str, Any]) -> None:
    """Limits must be positive, and unknown keys are errors."""
    with pytest.raises(ValidationError):
        CustomConfig.model_validate(
            {"controllers": {"post_task": {"limits": settings}}}
        )


def test_base_config() -> None:
    """The base config sets the limits as the model does: off."""
    config = yaml.safe_load((ROOT / "pro_tes" / "config.yaml").read_text())
    limits = config["custom"]["controllers"]["post_task"]["limits"]
    assert TaskLimits.model_validate(limits) == TaskLimits()


def test_problem_type_documented() -> None:
    """The problem type of the errors points to a section of the docs."""
    assert PROBLEM_TYPE.endswith("/configuration/#task-limits")
    text = (ROOT / "docs" / "configuration.md").read_text()
    assert "\n### Task limits\n" in text


def test_no_limits(mongo_client: MongoClient[dict[str, Any]]) -> None:
    """Without limits, any number of tasks is accepted."""
    store_tasks(mongo_client, OWNER, *(["RUNNING"] * 5))
    check(mongo_client)


@pytest.mark.parametrize("state", States.UNFINISHED)
def test_max_unfinished(mongo_client: MongoClient[dict[str, Any]], state: str) -> None:
    """Tasks in any unfinished state count against `max_unfinished`."""
    store_tasks(mongo_client, OWNER, state, *States.FINISHED)
    check(mongo_client, max_unfinished=2)
    store_tasks(mongo_client, OWNER, "QUEUED")
    with pytest.raises(TaskLimitReached) as info:
        check(mongo_client, max_unfinished=2)
    assert info.value.code == 429
    assert info.value.limit == "max_unfinished"
    assert "`max_unfinished`" in info.value.detail
    assert "at most 2" in info.value.detail
    assert info.value.problem_type == PROBLEM_TYPE


def test_max_stored(mongo_client: MongoClient[dict[str, Any]]) -> None:
    """Finished and unfinished tasks count against `max_stored`."""
    store_tasks(mongo_client, OWNER, "COMPLETE", "RUNNING")
    check(mongo_client, max_stored=3, max_unfinished=2)
    store_tasks(mongo_client, OWNER, "SYSTEM_ERROR")
    with pytest.raises(TaskLimitReached) as info:
        check(mongo_client, max_stored=3, max_unfinished=2)
    assert info.value.limit == "max_stored"
    assert "`max_stored`" in info.value.detail


def test_other_owners(mongo_client: MongoClient[dict[str, Any]]) -> None:
    """Only the tasks of the user count, not those of others or without owner."""
    store_tasks(mongo_client, "bob", "RUNNING", "RUNNING")
    store_tasks(mongo_client, None, "RUNNING", "RUNNING")
    check(mongo_client, max_unfinished=1, max_stored=1)


def test_exempt_roles(mongo_client: MongoClient[dict[str, Any]]) -> None:
    """Users with an exempt role are not limited; others are."""
    store_tasks(mongo_client, OWNER, "RUNNING")
    check(mongo_client, roles=("user", "admin"), max_stored=1, exempt_roles=["admin"])
    with pytest.raises(TaskLimitReached):
        check(mongo_client, roles=("user",), max_stored=1, exempt_roles=["admin"])


def test_both_limits(mongo_client: MongoClient[dict[str, Any]]) -> None:
    """If both limits are reached, the error names `max_unfinished`."""
    store_tasks(mongo_client, OWNER, "RUNNING")
    with pytest.raises(TaskLimitReached) as info:
        check(mongo_client, max_unfinished=1, max_stored=1)
    assert info.value.limit == "max_unfinished"
    assert "unfinished tasks" in info.value.detail


def test_no_roles(mongo_client: MongoClient[dict[str, Any]]) -> None:
    """Users without roles are limited, also with exempt roles configured."""
    store_tasks(mongo_client, OWNER, "COMPLETE")
    with pytest.raises(TaskLimitReached) as info:
        check(mongo_client, roles=(), max_stored=1, exempt_roles=["admin"])
    assert "stored tasks" in info.value.detail


def test_rejection_logged(
    mongo_client: MongoClient[dict[str, Any]], caplog: pytest.LogCaptureFixture
) -> None:
    """Rejections are logged with the limit, without the user."""
    store_tasks(mongo_client, OWNER, "RUNNING")
    with caplog.at_level("INFO"), pytest.raises(TaskLimitReached):
        check(mongo_client, max_unfinished=1)
    assert "`max_unfinished`" in caplog.text
    assert OWNER not in caplog.text
