"""The tools that tasks ran, as named in execution reports.

The tool of a task is the image of its first executor: its name is the last
component of the image repository (e.g., `samtools` of
`quay.io/biocontainers/samtools:1.17--h00cdaf9_0`), its version the tag of
the image. Its identifiers are those of the tags `biotools_id` (a bio.tools
identifier) and `rrid` (an RRID of software, `SCR_<digits>`) of the task, if
valid, and the image repository (`docker://<repository>`, without tag and
digest). Nothing else of the task is kept: no commands, arguments,
environment variables, inputs, outputs or other tags.
"""

import re
from typing import Any

# Image references (cf. the grammar of the Distribution project,
# https://github.com/distribution/reference): an optional registry (host and
# port), lowercase path components, an optional tag and an optional digest
_DOMAIN_COMPONENT = r"(?:[a-zA-Z0-9]|[a-zA-Z0-9][a-zA-Z0-9-]*[a-zA-Z0-9])"
_DOMAIN = rf"{_DOMAIN_COMPONENT}(?:\.{_DOMAIN_COMPONENT})*(?::[0-9]+)?"
_PATH_COMPONENT = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
IMAGE_REFERENCE = re.compile(
    rf"(?P<repository>(?:{_DOMAIN}/)?{_PATH_COMPONENT}(?:/{_PATH_COMPONENT})*)"
    r"(?::(?P<tag>[\w][\w.-]{0,127}))?"
    r"(?:@(?P<digest>[A-Za-z][A-Za-z0-9]*(?:[-_+.][A-Za-z][A-Za-z0-9]*)*"
    r":[0-9a-fA-F]{32,}))?",
    re.ASCII,
)
# Longest image repository (cf. the Distribution project)
MAX_REPOSITORY_LENGTH = 255
# Identifiers of tools in tags of tasks: patterns of the values, and the
# IRIs they give
BIOTOOLS_ID = re.compile(r"[A-Za-z0-9_~-][A-Za-z0-9._~-]{0,99}", re.ASCII)
RRID = re.compile(r"(?:RRID:)?(?P<id>SCR_[0-9]{1,12})", re.ASCII)
BIOTOOLS_IRI = "https://bio.tools/{}"
RRID_IRI = "https://identifiers.org/RRID/RRID:{}"
# Tags of tasks that are read
TAG_KEYS = ("biotools_id", "rrid")


def _image_tool(image: Any) -> dict[str, Any] | None:
    """Get the tool of an image.

    Args:
        image: Image of an executor, as stored.

    Returns:
        Name, version, package version and identifier of the tool; `None`
        if the image is no valid image reference.
    """
    if not isinstance(image, str):
        return None
    match = IMAGE_REFERENCE.fullmatch(image)
    if match is None or len(match["repository"]) > MAX_REPOSITORY_LENGTH:
        return None
    tag = match["tag"]
    version = None if tag in (None, "latest") else tag
    package_version = None
    # BioContainers: `<version>--<build>`
    if version is not None and "--" in version:
        package_version = version
        version = version.split("--", 1)[0]
    return {
        "name": match["repository"].rsplit("/", 1)[-1],
        "version": version,
        "package_version": package_version,
        "identifiers": [f"docker://{match['repository']}"],
    }


def _tag_identifiers(tags: Any) -> list[str]:
    """Get the identifiers of the tool of a task from its tags.

    Args:
        tags: Tags of the task, as stored.

    Returns:
        The IRIs of the valid tags `biotools_id` and `rrid`, in this order.
    """
    if not isinstance(tags, dict):
        return []
    identifiers = []
    biotools_id = tags.get("biotools_id")
    if isinstance(biotools_id, str) and BIOTOOLS_ID.fullmatch(biotools_id):
        identifiers.append(BIOTOOLS_IRI.format(biotools_id))
    rrid = tags.get("rrid")
    match = RRID.fullmatch(rrid) if isinstance(rrid, str) else None
    if match is not None:
        identifiers.append(RRID_IRI.format(match["id"]))
    return identifiers


def task_tool(task: dict[str, Any]) -> dict[str, Any] | None:
    """Get the tool that a task ran, for execution reports.

    Args:
        task: The task, as stored (`task` of its document).

    Returns:
        Name (`name`), version (`version`) and package version
        (`package_version`) of the tool, if known, and its identifiers
        (`identifiers`, IRIs); `None` if the first executor has no valid
        image.
    """
    executors = task.get("executors")
    if not isinstance(executors, list) or not executors:
        return None
    executor = executors[0]
    tool = _image_tool(executor.get("image") if isinstance(executor, dict) else None)
    if tool is None:
        return None
    tool["identifiers"] = [*_tag_identifiers(task.get("tags")), *tool["identifiers"]]
    return tool
