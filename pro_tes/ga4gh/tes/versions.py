"""TES versions, and adapters of tasks between them.

proTES stores and serves tasks in the newest TES version it supports
(:py:data:`CANONICAL`), and relays them to backends that may implement an
older one. Adapters (:py:class:`TesAdapter`) convert between two adjacent
versions, in both directions; they are registered in :py:data:`ADAPTERS` and
applied in chains, so that supporting a new version needs its specification
and one adapter (see `docs/tes-versions.md`). They work on tasks as plain JSON
objects, without the database, the tracking of tasks or HTTP clients, so that
they serve any code that relays tasks or fetches them from backends.

Going down, an adapter declares what the older version can't express without
changing what the task does (:py:meth:`TesAdapter.incompatibilities`): such
tasks are sent only to backends that can run them. Everything else that the
older version doesn't know is dropped.
"""

from abc import ABC, abstractmethod
from collections.abc import Iterator
from copy import deepcopy
from dataclasses import dataclass
import re
from typing import Any, Self

_VERSION = re.compile(r"v?(\d+)\.(\d+)(?:[.\-+].*)?", re.IGNORECASE)


@dataclass(frozen=True, order=True)
class TesVersion:
    """Version of the TES API; patch versions don't change the API.

    Attributes:
        major: Major version.
        minor: Minor version.
    """

    major: int
    minor: int

    @classmethod
    def parse(cls, value: object) -> Self | None:
        """Parse a version leniently, as backends report them.

        E.g., `1.1.0`, `v1.1.0` and `1.0` (also as a number).

        Args:
            value: Version.

        Returns:
            The version, or `None` if it is none.
        """
        if isinstance(value, bool) or not isinstance(value, str | float):
            return None
        match = _VERSION.fullmatch(str(value).strip())
        if match is None:
            return None
        return cls(major=int(match[1]), minor=int(match[2]))

    def __str__(self) -> str:
        """Get the version as `<major>.<minor>`.

        Returns:
            The version.
        """
        return f"{self.major}.{self.minor}"


@dataclass(frozen=True)
class Incompatibility:
    """Part of a task that a version can't express.

    Attributes:
        path: JSON pointer to the field.
        reason: Why dropping the field would change what the task does.
    """

    path: str
    reason: str

    def __str__(self) -> str:
        """Get the incompatibility, for messages to clients.

        Returns:
            The path and the reason.
        """
        return f"{self.path}: {self.reason}"


class TesAdapter(ABC):
    """Adapter of tasks between two adjacent TES versions.

    Attributes:
        newer: The newer version.
        older: The older version.
    """

    newer: TesVersion
    older: TesVersion

    @abstractmethod
    def incompatibilities(self, task: dict[str, Any]) -> list[Incompatibility]:
        """Get what the older version can't express without changing the task.

        Args:
            task: Task of the newer version; not changed.

        Returns:
            The parts of the task that the older version can't express.
        """

    @abstractmethod
    def downgrade_task(self, task: dict[str, Any]) -> dict[str, Any]:
        """Convert a task to the older version.

        Args:
            task: Task of the newer version; not changed.

        Returns:
            The task, without what the older version doesn't know.
        """

    @abstractmethod
    def upgrade_task(self, task: dict[str, Any]) -> dict[str, Any]:
        """Convert a task that a backend serves to the newer version.

        Args:
            task: Task of the older version; not changed.

        Returns:
            The task.
        """


class Tes11To10(TesAdapter):
    """Adapter between TES 1.1 and TES 1.0.

    All tasks of TES 1.0 are valid in TES 1.1, as it only adds optional
    fields and states, and relaxes `type` of inputs and outputs.
    """

    newer = TesVersion(1, 1)
    older = TesVersion(1, 0)

    def incompatibilities(self, task: dict[str, Any]) -> list[Incompatibility]:
        """Get what TES 1.0 can't express without changing the task.

        Args:
            task: Task of TES 1.1; not changed.

        Returns:
            Executors that ignore errors (a failing one would stop the task),
            strict backend parameters (they would not be honored, instead of
            failing the task) and outputs with wildcards (taken literally).
        """
        found: list[Incompatibility] = []
        for index, executor in _items(task, "executors"):
            if executor.get("ignore_error") is True:
                found.append(
                    Incompatibility(
                        f"/executors/{index}/ignore_error",
                        "executors can't ignore errors",
                    )
                )
        resources = task.get("resources")
        if (
            isinstance(resources, dict)
            and resources.get("backend_parameters_strict") is True
            and resources.get("backend_parameters")
        ):
            found.append(
                Incompatibility(
                    "/resources/backend_parameters_strict",
                    "backend parameters are not supported",
                )
            )
        for index, output in _items(task, "outputs"):
            if has_wildcards(str(output.get("path"))):
                found.append(
                    Incompatibility(
                        f"/outputs/{index}/path", "wildcards are not supported"
                    )
                )
        return found

    def downgrade_task(self, task: dict[str, Any]) -> dict[str, Any]:
        """Convert a task to TES 1.0.

        Drops the fields of TES 1.1, which backends of TES 1.0 may reject,
        and sets `type` of inputs and outputs, which TES 1.0 requires.

        Args:
            task: Task of TES 1.1, without incompatibilities; not changed.

        Returns:
            The task of TES 1.0.
        """
        task = deepcopy(task)
        for _, executor in _items(task, "executors"):
            executor.pop("ignore_error", None)
        for _, item in _items(task, "inputs"):
            item.pop("streamable", None)
            item["type"] = item.get("type") or "FILE"
        for _, item in _items(task, "outputs"):
            item.pop("path_prefix", None)
            item["type"] = item.get("type") or "FILE"
        resources = task.get("resources")
        if isinstance(resources, dict):
            resources.pop("backend_parameters", None)
            resources.pop("backend_parameters_strict", None)
        return task

    def upgrade_task(self, task: dict[str, Any]) -> dict[str, Any]:
        """Convert a task that a backend serves to TES 1.1.

        Args:
            task: Task of TES 1.0.

        Returns:
            The task: all tasks of TES 1.0 are valid in TES 1.1.
        """
        return task


ADAPTERS: tuple[TesAdapter, ...] = (Tes11To10(),)
"""Registered adapters, each between two adjacent versions."""

CANONICAL: TesVersion = max(adapter.newer for adapter in ADAPTERS)
"""Version in which proTES stores and serves tasks: the newest it supports."""

OLDEST: TesVersion = min(adapter.older for adapter in ADAPTERS)
"""Oldest supported version, assumed for backends of unknown versions."""

SUPPORTED: frozenset[TesVersion] = frozenset(
    version for adapter in ADAPTERS for version in (adapter.newer, adapter.older)
)
"""Versions that proTES can relay tasks to."""


def has_wildcards(path: str) -> bool:
    """Check whether an output path has wildcards of TES 1.1.

    Wildcards are those of POSIX (IEEE Std 1003.1-2017, 2.13): `*`, `?`, and
    bracket expressions, which need a closing bracket after at least one
    character (a bracket right after the opening one is a literal one). A
    linear scan, as paths come from clients: a bracket expression exists if,
    and only if, the first opening bracket and the last closing one are at
    least two characters apart.

    Args:
        path: Output path.

    Returns:
        Whether the path has wildcards.
    """
    if "*" in path or "?" in path:
        return True
    opening = path.find("[")
    return opening != -1 and path.rfind("]") >= opening + 2


def _items(task: dict[str, Any], field: str) -> Iterator[tuple[int, dict[str, Any]]]:
    """Get the JSON objects in a list of a task, with their indexes.

    Args:
        task: Task.
        field: Field of the list.

    Yields:
        The index of each object in the list, and the object.
    """
    items = task.get(field)
    for index, item in enumerate(items if isinstance(items, list) else []):
        if isinstance(item, dict):
            yield index, item


def chain(version: TesVersion) -> list[TesAdapter]:
    """Get the adapters between the canonical version and a version.

    Args:
        version: A supported version.

    Returns:
        The adapters, from the newest down to the version; none for the
        canonical version.

    Raises:
        ValueError: The version is not supported.
    """
    if version not in SUPPORTED:
        raise ValueError(f"TES version not supported: {version}")
    return sorted(
        (adapter for adapter in ADAPTERS if adapter.older >= version),
        key=lambda adapter: adapter.newer,
        reverse=True,
    )


def incompatibilities(
    task: dict[str, Any], version: TesVersion = OLDEST
) -> list[Incompatibility]:
    """Get what a version can't express of a task of the canonical version.

    Each adapter sees the task as the adapters before it left it.

    Args:
        task: Task of the canonical version; not changed.
        version: Version.

    Returns:
        The parts of the task that keep it from running unchanged in the
        version; none if it can.

    Raises:
        ValueError: The version is not supported.
    """
    found: list[Incompatibility] = []
    for adapter in chain(version):
        found.extend(adapter.incompatibilities(task))
        task = adapter.downgrade_task(task)
    return found


def downgrade_task(task: dict[str, Any], version: TesVersion) -> dict[str, Any]:
    """Convert a task of the canonical version to a version.

    Args:
        task: Task of the canonical version, without incompatibilities; not
            changed.
        version: Version.

    Returns:
        The task in the version (the same object for the canonical version).

    Raises:
        ValueError: The version is not supported.
    """
    for adapter in chain(version):
        task = adapter.downgrade_task(task)
    return task


def upgrade_task(task: dict[str, Any], version: TesVersion) -> dict[str, Any]:
    """Convert a task that a backend serves to the canonical version.

    Args:
        task: Task of the version; not changed.
        version: Version.

    Returns:
        The task in the canonical version.

    Raises:
        ValueError: The version is not supported.
    """
    for adapter in reversed(chain(version)):
        task = adapter.upgrade_task(task)
    return task
