"""Tests of the tools of tasks, as named in execution reports."""

from typing import Any

import pytest

from pro_tes.accounting.tool import task_tool


def _task(image: Any, tags: Any = None) -> dict[str, Any]:
    """Get a task with one executor.

    Args:
        image: Image of the executor.
        tags: Tags of the task.

    Returns:
        The task, as stored.
    """
    return {"executors": [{"image": image, "command": ["run"]}], "tags": tags}


@pytest.mark.parametrize(
    ("image", "repository", "version", "package_version"),
    [
        ("ubuntu", "ubuntu", None, None),
        ("ubuntu:latest", "ubuntu", None, None),
        ("ubuntu:24.04", "ubuntu", "24.04", None),
        (
            "quay.io/biocontainers/samtools:1.17--h00cdaf9_0",
            "quay.io/biocontainers/samtools",
            "1.17",
            "1.17--h00cdaf9_0",
        ),
        ("localhost:5000/tools/bwa:0.7.18", "localhost:5000/tools/bwa", "0.7.18", None),
        (f"ubuntu@sha256:{'a' * 64}", "ubuntu", None, None),
        (f"ubuntu:24.04@sha256:{'a' * 64}", "ubuntu", None, None),
        (
            f"quay.io/biocontainers/samtools:1.17--h00cdaf9_0@sha256:{'a' * 64}",
            "quay.io/biocontainers/samtools",
            None,
            None,
        ),
        # Pulled by digest, the tag is never checked: it could be any text,
        # e.g. the name of a user, and is not reported
        (f"ubuntu:john.doe@sha256:{'a' * 64}", "ubuntu", None, None),
    ],
)
def test_tool_of_image(
    image: str, repository: str, version: str | None, package_version: str | None
) -> None:
    """The tool is the last component of the image repository, its tag the version.

    Tags of BioContainers (`<version>--<build>`) give the version and the
    package version; `latest` and references with a digest give no version,
    also with a tag. The identifier is the repository of the image, without
    tag and digest.
    """
    assert task_tool(_task(image)) == {
        "name": repository.rsplit("/", 1)[-1],
        "repository": repository,
        "version": version,
        "package_version": package_version,
        "identifiers": [f"docker://{repository}"],
    }


@pytest.mark.parametrize(
    "image",
    [
        None,
        42,
        "",
        "Ubuntu",
        "ubuntu:",
        "ubuntu latest",
        "https://registry.example.org/tool",
        "user:password@registry.example.org/tool",
        "registry.example.org/tool:1.0\n",
        "a" * 256,
    ],
)
def test_tool_of_invalid_image(image: Any) -> None:
    """Tasks whose image is no valid image reference have no tool."""
    assert task_tool(_task(image)) is None


@pytest.mark.parametrize(
    "task",
    [
        {},
        {"executors": None},
        {"executors": []},
        {"executors": ["ubuntu"]},
        {"executors": [{}]},
    ],
)
def test_tool_without_executor(task: dict[str, Any]) -> None:
    """Tasks without an executor with an image have no tool."""
    assert task_tool(task) is None


def test_tool_of_first_executor() -> None:
    """The tool is that of the first executor."""
    task = {
        "executors": [
            {"image": "samtools:1.17", "command": ["samtools"]},
            {"image": "bwa:0.7.18", "command": ["bwa"]},
        ]
    }
    tool = task_tool(task)
    assert tool is not None
    assert tool["name"] == "samtools"


def test_tool_identifiers_of_tags() -> None:
    """Tags `biotools_id` and `rrid` add the identifiers of the tool.

    Other tags and invalid values are left out.
    """
    tags = {
        "biotools_id": "bwa",
        "rrid": "RRID:SCR_010910",
        "project": "secret-project",
    }
    tool = task_tool(_task("bwa:0.7.18", tags))
    assert tool is not None
    assert tool["identifiers"] == [
        "https://bio.tools/bwa",
        "https://identifiers.org/RRID/RRID:SCR_010910",
        "docker://bwa",
    ]
    tool = task_tool(_task("bwa", {"rrid": "SCR_010910"}))
    assert tool is not None
    assert tool["identifiers"][0] == "https://identifiers.org/RRID/RRID:SCR_010910"


@pytest.mark.parametrize(
    "tags",
    [
        None,
        "bwa",
        {"biotools_id": "https://evil.example.org/x"},
        {"biotools_id": "bwa/../x"},
        {"biotools_id": ""},
        {"biotools_id": 42},
        {"rrid": "AB_123456"},
        {"rrid": "SCR_"},
        {"rrid": "RRID:SCR_010910 alice"},
    ],
)
def test_tool_identifiers_of_invalid_tags(tags: Any) -> None:
    """Invalid tags add no identifiers."""
    tool = task_tool(_task("bwa", tags))
    assert tool is not None
    assert tool["identifiers"] == ["docker://bwa"]
