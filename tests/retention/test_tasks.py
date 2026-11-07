"""Tests of the Celery task and worker thread of the retention of tasks."""

from typing import Any

from foca.models.config import Config
from kombu.exceptions import OperationalError
import pytest

from pro_tes.celery_worker import celery
from pro_tes.config_models import CustomConfig
from pro_tes.retention import Retention, tasks


class FakeThread:
    """Thread that is not started.

    Attributes:
        kwargs: Arguments of its target.
        started: Whether it was started.
    """

    def __init__(self, target: Any, kwargs: dict[str, Any], **options: Any) -> None:
        """Create the thread."""
        self.kwargs = kwargs
        self.started = False

    def start(self) -> None:
        """Start the thread (do nothing)."""
        self.started = True


class Stop:
    """Event that is set after a number of waits.

    Attributes:
        remaining: Waits left before the event is set.
        waits: Intervals waited for.
    """

    def __init__(self, waits: int) -> None:
        """Create the event."""
        self.remaining = waits
        self.waits: list[float] = []

    def wait(self, timeout: float) -> bool:
        """Wait (without waiting).

        Returns:
            Whether the event is set.
        """
        self.waits.append(timeout)
        self.remaining -= 1
        return self.remaining < 0


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Purges started (not run); threads are not started."""
    started: list[bool] = []
    monkeypatch.setattr(
        tasks.task__purge_finished_tasks, "delay", lambda: started.append(True)
    )
    monkeypatch.setattr(tasks, "Thread", FakeThread)
    return started


def worker_custom_config() -> CustomConfig:
    """Get the custom config of the Celery worker.

    Returns:
        The config, as the worker has it.
    """
    custom = (celery.conf.foca.model_extra or {})["custom"]
    assert isinstance(custom, CustomConfig)
    return custom


def test_registered() -> None:
    """The purge is a task of the Celery worker."""
    assert "tasks.purge_finished_tasks" in celery.tasks


def test_off_by_default(calls: list[bool]) -> None:
    """Without `finished_days`, workers start no purge; also without config."""
    assert tasks.purge_on_start(sender=None) is None
    assert calls == []


def test_off_without_config(calls: list[bool], monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the custom config of proTES, no purge starts."""
    monkeypatch.setitem(celery.conf, "foca", Config())
    assert tasks.purge_on_start(sender=None) is None
    assert calls == []


def test_purge_on_start(calls: list[bool], monkeypatch: pytest.MonkeyPatch) -> None:
    """With `finished_days`, workers purge on start, then every `interval`."""
    retention = Retention(finished_days=30, interval=60)
    monkeypatch.setattr(worker_custom_config(), "retention", retention)
    thread = tasks.purge_on_start(sender=None)
    assert calls == [True]
    assert isinstance(thread, FakeThread)
    assert thread.started
    assert thread.kwargs["interval"] == 60


def test_purge_periodically(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The purge runs at the interval until stopped; errors don't end it."""
    started: list[bool] = []

    def delay() -> None:
        """Record the call; fail on the first one."""
        started.append(True)
        if len(started) == 1:
            raise OperationalError("unavailable")

    monkeypatch.setattr(tasks.task__purge_finished_tasks, "delay", delay)
    stop = Stop(waits=2)
    tasks.purge_periodically(stop=stop, interval=5)  # type: ignore[arg-type]
    assert started == [True, True]
    assert stop.waits == [5, 5, 5]
    assert "Could not purge finished tasks" in caplog.text
