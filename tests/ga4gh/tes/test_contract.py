"""Contract tests: responses of proTES against the vendored TES specification."""

from pathlib import Path
from typing import Any

from flask.testing import FlaskClient
import jsonschema  # type: ignore[import-untyped]
import pytest
import yaml

from pro_tes.ga4gh.tes import models
from tests.ga4gh.tes.conftest import BASE_PATH, FakeBackend, set_state

API_DIR = Path(models.__file__).parents[2] / "api"
SPEC = API_DIR / "20221114.117cd92.task_execution_service.openapi.yaml"


def _components() -> dict[str, Any]:
    """Get the schemas of the specification, with the additions of proTES.

    Returns:
        The components of the specification files.
    """
    additions = yaml.safe_load((API_DIR / "additions.openapi.yaml").read_text())
    spec = yaml.safe_load(SPEC.read_text())
    schemas = {**additions["components"]["schemas"], **spec["components"]["schemas"]}
    return {"schemas": schemas}


def _validate(instance: Any, schema: str) -> None:
    """Validate a response against a schema of the specification.

    Args:
        instance: Response body.
        schema: Name of the schema.
    """
    jsonschema.validate(
        instance,
        {"$ref": f"#/components/schemas/{schema}", "components": _components()},
    )


def test_spec_version() -> None:
    """The service info reports the version of the vendored specification."""
    spec = yaml.safe_load(SPEC.read_text())
    config = yaml.safe_load((API_DIR.parent / "config.yaml").read_text())
    version = config["service_info"]["service"]["type"]["version"]
    assert version == spec["info"]["version"] == "1.1.0"


def test_service_info(client: FlaskClient) -> None:
    """The service info is valid."""
    _validate(client.get(f"{BASE_PATH}/service-info").get_json(), "tesServiceInfo")


def test_create_task(client: FlaskClient, backend: type[FakeBackend]) -> None:
    """Responses to new tasks are valid, also for the fields of TES 1.1."""
    response = client.post(
        f"{BASE_PATH}/tasks",
        json={
            "inputs": [{"path": "/in", "url": "s3://b/in", "streamable": True}],
            "outputs": [
                {"path": "/out/*", "path_prefix": "/out", "url": "s3://b/out"},
            ],
            "resources": {
                "backend_parameters": {"VmSize": "small"},
                "backend_parameters_strict": False,
            },
            "executors": [
                {"image": "alpine", "command": ["true"], "ignore_error": True}
            ],
        },
    )
    assert response.status_code == 200
    _validate(response.get_json(), "tesCreateTaskResponse")
    task = client.get(f"{BASE_PATH}/tasks/{response.get_json()['id']}?view=FULL")
    _validate(task.get_json(), "tesTask")
    assert task.get_json()["outputs"][0]["path_prefix"] == "/out"
    assert task.get_json()["inputs"][0]["streamable"] is True
    assert task.get_json()["resources"]["backend_parameters"] == {"VmSize": "small"}


@pytest.mark.parametrize("view", ["BASIC", "FULL"])
def test_get_list_tasks(client: FlaskClient, task_id: str, view: str) -> None:
    """Tasks and lists of tasks are valid.

    `MINIMAL` is left out: it doesn't match `tesTask.required` (cf.
    https://github.com/ga4gh/task-execution-schemas/issues/136).
    """
    _validate(
        client.get(f"{BASE_PATH}/tasks/{task_id}?view={view}").get_json(), "tesTask"
    )
    _validate(
        client.get(f"{BASE_PATH}/tasks?view={view}").get_json(),
        "tesListTasksResponse",
    )


@pytest.mark.parametrize("state", ["PREEMPTED", "CANCELING"])
def test_new_states(
    client: FlaskClient, task_id: str, state: str, mongo_client: Any
) -> None:
    """The states of TES 1.1 are served."""
    set_state(mongo_client, task_id, state)
    _validate(
        client.get(f"{BASE_PATH}/tasks/{task_id}?view=BASIC").get_json(), "tesTask"
    )
