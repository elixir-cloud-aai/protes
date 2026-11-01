"""Tests of the TES models and their conversion."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
import tes
import yaml

from pro_tes.ga4gh.tes.models import (
    DbDocument,
    TesExecutor,
    TesTask,
    TesTaskLog,
)
from pro_tes.gateway import relayed_task
from pro_tes.utils.models import TaskModelConverter

ADDITIONS = Path(__file__).parents[3] / "pro_tes" / "api" / "additions.openapi.yaml"


def test_executor_defaults() -> None:
    """Executors without command get none, tasks without executors none."""
    assert not TesExecutor(image="alpine").command
    assert not TesTask().executors
    assert not DbDocument().task_original.executors


def test_ignore_error_relayed() -> None:
    """`ignore_error` of executors is relayed to TES backends (cf. #176)."""
    executor = {"image": "alpine", "command": ["true"], "ignore_error": True}
    task = relayed_task({"executors": [executor]}, prefix="gateway.")
    assert task.executors[0].ignore_error is True


def test_ignore_error_converted() -> None:
    """`ignore_error` of executors of backend tasks is kept."""
    task = tes.Task(
        executors=[tes.Executor(image="alpine", command=["true"], ignore_error=True)]
    )
    converted = TaskModelConverter(task=task).convert_task()
    assert converted.executors[0].ignore_error is True


def test_forwarded_to_removed() -> None:
    """`forwarded_to` is no longer in the spec additions (cf. #324)."""
    schemas = yaml.safe_load(ADDITIONS.read_text(encoding="utf-8"))["components"][
        "schemas"
    ]
    assert "tesForward" not in schemas
    assert "tesTaskLog" not in schemas
    assert "forwarded_to" not in ADDITIONS.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "forwards",
    [
        [{"id": "task-1", "url": "https://first.example.org", "forward_time": "t"}],
        {
            "id": "task-1",
            "url": "https://first.example.org",
            "forwarded_to": {"id": "task-2", "url": "https://second.example.org"},
        },
    ],
)
def test_stored_forwards_dropped(forwards: object) -> None:
    """Forwards stored before 0.4.0 are dropped, string metadata is kept."""
    log = TesTaskLog.model_validate(
        {
            "logs": [],
            "outputs": [],
            "metadata": {
                "forwarded_to": forwards,
                "remote_task_id": "task-1",
                "host": "worker-001",
            },
        }
    )
    assert log.metadata == {"remote_task_id": "task-1", "host": "worker-001"}


def test_metadata_optional() -> None:
    """Logs without metadata have none."""
    assert TesTaskLog(logs=[], outputs=[]).metadata is None


def test_task_converted() -> None:
    """Tasks of backends are converted with all their fields."""
    time = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
    task = tes.Task(
        id="remote-1",
        state="COMPLETE",
        creation_time=time,
        inputs=[tes.Input(url="s3://bucket/in", path="/in", type="FILE")],
        outputs=[tes.Output(url="s3://bucket/out", path="/out", type="DIRECTORY")],
        resources=tes.Resources(cpu_cores=2, ram_gb=4.0, zones=["eu"]),
        executors=[tes.Executor(image="alpine", command=["true"])],
        logs=[
            tes.TaskLog(
                start_time=time,
                end_time=time,
                logs=[
                    tes.ExecutorLog(
                        start_time=time, end_time=time, stdout="out", exit_code=0
                    )
                ],
                outputs=[tes.OutputFileLog(url="s3://bucket/out", path="/out")],
            ),
            tes.TaskLog(),
        ],
    )
    converted = TaskModelConverter(task=task).convert_task()
    assert converted.creation_time == time.isoformat()
    assert converted.inputs is not None
    assert converted.inputs[0].url == "s3://bucket/in"
    assert converted.outputs is not None
    assert converted.outputs[0].type == "DIRECTORY"
    assert converted.resources is not None
    assert (converted.resources.cpu_cores, converted.resources.zones) == (2, ["eu"])
    assert converted.logs is not None
    log, empty = converted.logs
    assert (log.start_time, log.end_time) == (time.isoformat(), time.isoformat())
    (executor_log,) = log.logs
    assert (executor_log.start_time, executor_log.stdout) == (time.isoformat(), "out")
    assert (empty.logs, empty.outputs, empty.start_time) == ([], [], None)
