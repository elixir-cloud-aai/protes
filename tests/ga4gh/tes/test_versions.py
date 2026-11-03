"""Tests of TES versions and adapters."""

from copy import deepcopy
from itertools import pairwise
from typing import Any

import pytest

from pro_tes.ga4gh.tes.versions import (
    ADAPTERS,
    CANONICAL,
    OLDEST,
    SUPPORTED,
    Incompatibility,
    Tes11To10,
    TesAdapter,
    TesVersion,
    chain,
    downgrade_task,
    incompatibilities,
    upgrade_task,
)

V10 = TesVersion(1, 0)
V11 = TesVersion(1, 1)
EXECUTOR = {"image": "alpine", "command": ["true"]}
TASK_11: dict[str, Any] = {
    "inputs": [
        {"url": "s3://b/a", "path": "/a", "streamable": True},
        {"url": "s3://b/b", "path": "/b", "type": "DIRECTORY"},
    ],
    "outputs": [{"url": "s3://b/c", "path": "/c", "path_prefix": "/"}],
    "resources": {
        "cpu_cores": 1,
        "backend_parameters": {"VmSize": "small"},
        "backend_parameters_strict": False,
    },
    "executors": [{**EXECUTOR, "ignore_error": False}, EXECUTOR],
}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1.1.0", V11),
        ("v1.1.0", V11),
        ("V1.0", V10),
        (1.0, V10),
        (" 1.1 ", V11),
        ("1.1.0-rc1", V11),
        ("0.12.0", TesVersion(0, 12)),
        ("1", None),
        ("latest", None),
        ("1.x", None),
        (None, None),
        (True, None),
        (1, None),
    ],
)
def test_parse(value: object, expected: TesVersion | None) -> None:
    """Versions are parsed as backends report them."""
    assert TesVersion.parse(value) == expected


def test_registry() -> None:
    """The registry knows TES 1.0 and 1.1; versions are ordered and printed."""
    assert V11 == CANONICAL
    assert V10 == OLDEST
    assert set(SUPPORTED) == {V10, V11}
    assert V10 < V11
    assert str(V11) == "1.1"


def test_adapters_adjacent() -> None:
    """Adapters form a single chain from the canonical to the oldest version."""
    versions = sorted(SUPPORTED)
    pairs = sorted((adapter.older, adapter.newer) for adapter in ADAPTERS)
    assert pairs == list(pairwise(versions))


def test_chain() -> None:
    """Chains lead down from the canonical version."""
    assert chain(V11) == []
    assert [type(adapter) for adapter in chain(V10)] == [Tes11To10]
    with pytest.raises(ValueError, match="not supported"):
        chain(TesVersion(0, 12))


def test_chain_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """Chains apply adapters in order, each on the result of the one before."""

    class Fake(TesAdapter):
        """Adapter that records its steps in the task."""

        def __init__(self, newer: TesVersion, older: TesVersion) -> None:
            """Create the adapter."""
            self.newer = newer
            self.older = older

        def incompatibilities(self, task: dict[str, Any]) -> list[Incompatibility]:
            """Report the steps so far."""
            return [Incompatibility(str(self.newer), str(task["steps"]))]

        def downgrade_task(self, task: dict[str, Any]) -> dict[str, Any]:
            """Add the step down."""
            return {"steps": [*task["steps"], str(self.older)]}

        def upgrade_task(self, task: dict[str, Any]) -> dict[str, Any]:
            """Add the step up."""
            return {"steps": [*task["steps"], str(self.newer)]}

    v20 = TesVersion(2, 0)
    adapters = (Fake(V11, V10), Fake(v20, V11))
    monkeypatch.setattr("pro_tes.ga4gh.tes.versions.ADAPTERS", adapters)
    monkeypatch.setattr("pro_tes.ga4gh.tes.versions.SUPPORTED", {V10, V11, v20})
    assert downgrade_task({"steps": []}, V10) == {"steps": ["1.1", "1.0"]}
    assert upgrade_task({"steps": []}, V10) == {"steps": ["1.1", "2.0"]}
    assert [str(item) for item in incompatibilities({"steps": []}, V10)] == [
        "2.0: []",
        "1.1: ['1.1']",
    ]
    assert upgrade_task({"steps": []}, v20) == {"steps": []}


def test_downgrade() -> None:
    """The fields of TES 1.1 are dropped; `type` is set; the task is kept."""
    task = deepcopy(TASK_11)
    assert downgrade_task(task, V10) == {
        "inputs": [
            {"url": "s3://b/a", "path": "/a", "type": "FILE"},
            {"url": "s3://b/b", "path": "/b", "type": "DIRECTORY"},
        ],
        "outputs": [{"url": "s3://b/c", "path": "/c", "type": "FILE"}],
        "resources": {"cpu_cores": 1},
        "executors": [EXECUTOR, EXECUTOR],
    }
    assert task == TASK_11


def test_downgrade_canonical() -> None:
    """Tasks stay as they are for the canonical version."""
    assert downgrade_task(TASK_11, V11) is TASK_11


@pytest.mark.parametrize("task", [{}, {"inputs": None, "executors": ["invalid"]}])
def test_downgrade_incomplete(task: dict[str, Any]) -> None:
    """Fields that are missing or malformed are left to the validation."""
    assert downgrade_task(task, V10) == task
    assert incompatibilities(task, V10) == []


def test_upgrade() -> None:
    """Tasks of TES 1.0 are valid in TES 1.1."""
    task = {"id": "a", "state": "RUNNING"}
    assert upgrade_task(task, V10) == task


@pytest.mark.parametrize(
    ("task", "paths"),
    [
        (TASK_11, []),
        ({"executors": [EXECUTOR, {**EXECUTOR, "ignore_error": True}]}, ["1"]),
        ({"resources": {"backend_parameters_strict": True}}, []),
        (
            {
                "resources": {
                    "backend_parameters_strict": True,
                    "backend_parameters": {},
                }
            },
            [],
        ),
        (
            {
                "resources": {
                    "backend_parameters_strict": True,
                    "backend_parameters": {"a": "b"},
                }
            },
            ["strict"],
        ),
        ({"outputs": [{"url": "s3://b", "path": "/out/*"}]}, ["path"]),
        ({"outputs": [{"url": "s3://b", "path": "/out/?.txt"}]}, ["path"]),
        ({"outputs": [{"url": "s3://b", "path": "/out/[ab]"}]}, ["path"]),
        ({"outputs": [{"url": "s3://b", "path": "/out/plain.txt"}]}, []),
    ],
)
def test_incompatibilities(task: dict[str, Any], paths: list[str]) -> None:
    """TES 1.0 can't express what changes the task if it is dropped."""
    found = incompatibilities(task)
    assert len(found) == len(paths)
    for incompatibility, path in zip(found, paths, strict=True):
        assert path in incompatibility.path


def test_incompatibilities_canonical() -> None:
    """The canonical version expresses everything."""
    task = {"executors": [{**EXECUTOR, "ignore_error": True}]}
    assert incompatibilities(task, V11) == []
    assert str(incompatibilities(task)[0]) == (
        "/executors/0/ignore_error: executors can't ignore errors"
    )


@pytest.mark.parametrize(
    ("path", "wildcard"),
    [
        ("/data/a*", True),
        ("/data/a?", True),
        ("/data/a[bc]", True),
        ("/data/a[b", False),
        ("/data/a]", False),
        ("/data/a[]", False),
        ("/data/a", False),
    ],
)
def test_output_wildcards(path: str, wildcard: bool) -> None:
    """Only valid wildcards in output paths need TES 1.1."""
    task = {"outputs": [{"url": "s3://a/b", "path": path}]}
    assert bool(incompatibilities(task)) is wildcard
