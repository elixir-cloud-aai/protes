"""Execution reports of the usage records (Tool Execution Report format).

The usage records (cf. :py:mod:`pro_tes.accounting.record`) are exported as
reports of the Tool Execution Report format of ELIXIR Compute, version
0.0.2 (https://github.com/elixir-europe/compute_wp5): one report per
attempt of a task at its TES instance (task log), with the tool (the image
of the first executor of the task, cf. :py:mod:`pro_tes.accounting.tool`),
the providers (`infra`: those configured, the TES instance and proTES), the
location (if configured), the CPU cores that the task requested, the start
and end of the attempt, if known, and the total size of its outputs,
rounded.

Reports hold no personal data and no details of tasks: no user identifiers,
task identifiers, commands, arguments, environment variables, URLs or paths
of inputs and outputs, or other tags; the records hold none of these but the
owner and the task identifier, which are not read. Only tasks of images in
trusted namespaces (`custom.accounting.reports.images`, e.g., BioContainers)
are reported, as image names and tags in other namespaces (e.g., personal
ones) may name users.
"""

from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from pymongo.collection import Collection

from pro_tes.accounting.config import ExecutionReports
from pro_tes.accounting.report import finished_range

# Version of the report format (`report_format_version`); the schema in
# `tests/accounting/data` must be of the same version (cf. its `.license`
# file for its source)
REPORT_FORMAT_VERSION = "0.0.2"
# proTES, as infrastructure provider (with the version that recorded the task)
PROTES = {
    "infra_name": "proTES",
    "infra_identifier": ["https://github.com/elixir-cloud-aai/protes"],
}
# Fields of the records that reports are made of; not the owner (`user_id`)
# or the task identifier
FIELDS = ("backend", "cpu_cores", "tool", "attempts", "protes_version")
# Significant digits of sizes
SIZE_DIGITS = 2


class TooManyRecords(Exception):
    """Raised when a request for reports covers more records than allowed.

    Cf. `max_records` of :py:class:`pro_tes.accounting.ExecutionReports`.
    """


def rounded_size(size: int) -> int:
    """Round a size, so that reports don't give exact sizes.

    Args:
        size: Size, in bytes.

    Returns:
        The size, rounded to two significant digits (half up).
    """
    factor = 10 ** max(len(str(size)) - SIZE_DIGITS, 0)
    quotient, remainder = divmod(size, factor)
    # half up (`round()` rounds half to even); sizes of up to two digits
    # have no remainder
    if 2 * remainder >= factor and remainder:
        quotient += 1
    return quotient * factor


def report_time(value: Any) -> str | None:
    """Get a time of a record as date-time of RFC 3339, in UTC.

    Args:
        value: The time, as stored (in UTC, without time zone as PyMongo
            reads it by default).

    Returns:
        The date-time, to the second (e.g., `2026-11-01T10:00:00Z`); `None`
        if it is no time.
    """
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is not None:
        value = value.astimezone(UTC)
    return value.replace(tzinfo=None).isoformat(timespec="seconds") + "Z"


def _cores(value: Any) -> int | None:
    """Get the CPU cores that a task requested, as whole number.

    Args:
        value: The CPU cores, as recorded.

    Returns:
        The CPU cores; `None` if they are no positive whole number.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if value <= 0 or not float(value).is_integer():
        return None
    return int(value)


def _backend(
    record: dict[str, Any], config: ExecutionReports
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Get the TES instance of a record, as provider, and its location.

    Args:
        record: The record.
        config: Settings of the reports.

    Returns:
        The provider (as configured in `backends`, or else named by the host
        of its URL) and the location (that of the TES instance, or else that
        of all reports), if any.
    """
    url = record.get("backend")
    reported = config.backends.get(url) if isinstance(url, str) else None
    location = reported.location if reported is not None else None
    location = location or config.location
    location_fields = (
        location.model_dump(by_alias=True, exclude_none=True) if location else None
    )
    if reported is not None:
        provider = reported.model_dump(exclude_none=True, exclude={"location"})
    elif isinstance(url, str) and urlsplit(url).hostname:
        provider = {"infra_name": urlsplit(url).hostname, "infra_identifier": [url]}
    else:
        provider = None
    return provider, location_fields


def trusted_image(repository: Any, images: list[str]) -> bool:
    """Check whether an image is in a trusted namespace.

    Image names and tags are chosen by whoever pushed the image: in a
    personal namespace (e.g., `ghcr.io/<user>/<tool>`), they may name the
    user, so only images of trusted namespaces are reported.

    Args:
        repository: Image repository, without tag and digest.
        images: Trusted namespaces (cf. `custom.accounting.reports.images`):
            repositories or their prefixes (whole path components), with
            the registry; without registry for Docker Hub, whose official
            images are `library` (e.g., `ubuntu`, also `docker.io/ubuntu`).

    Returns:
        Whether the repository is one of the namespaces, or in one.
    """
    if not isinstance(repository, str):
        return False
    repository = repository.removeprefix("docker.io/")
    if "/" not in repository:
        repository = f"library/{repository}"
    return any(
        repository == image or repository.startswith(f"{image}/") for image in images
    )


def _reports(record: dict[str, Any], config: ExecutionReports) -> list[dict[str, Any]]:
    """Get the reports of a record, one per attempt.

    Args:
        record: The record (cf. :py:data:`FIELDS`).
        config: Settings of the reports.

    Returns:
        The reports, in the order of the attempts.
    """
    tool = record["tool"]
    if not trusted_image(tool.get("repository"), config.images):
        return []
    common: dict[str, Any] = {
        "report_format_version": REPORT_FORMAT_VERSION,
        "tool_identifier": list(tool["identifiers"]),
        "tool_name": tool["name"],
    }
    for field in ("version", "package_version"):
        if tool.get(field):
            common[f"tool_{field}"] = tool[field]
    infra = [provider.model_dump(exclude_none=True) for provider in config.infra]
    provider, location = _backend(record, config)
    if provider is not None:
        infra.append(provider)
    protes = {**PROTES, "infra_identifier": list(PROTES["infra_identifier"])}
    if isinstance(record.get("protes_version"), str):
        protes["infra_version"] = record["protes_version"]
    common["infra"] = [*infra, protes]
    if location:
        common["location"] = location
    cores = _cores(record.get("cpu_cores"))
    if cores is not None:
        common["cpu_cores_assigned"] = cores
    reports = []
    for attempt in record["attempts"]:
        report = dict(common)
        for field in ("start_time", "end_time"):
            time = report_time(attempt.get(field))
            if time is not None:
                report[field] = time
        size = attempt.get("outputs_bytes")
        if isinstance(size, int) and not isinstance(size, bool) and size >= 0:
            report["final_outputs_size"] = rounded_size(size)
        reports.append(report)
    return reports


def execution_reports(
    usage: Collection[dict[str, Any]],
    config: ExecutionReports,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[dict[str, Any]]:
    """Get the execution reports of the tasks that finished in a time range.

    Records without tool or attempts (e.g., of earlier versions of proTES,
    or of tasks whose first executor has no valid image), and records of
    images outside the trusted namespaces (`config.images`) give no reports.

    Args:
        usage: Database collection of the records (cf.
            :py:func:`pro_tes.accounting.record_usage`).
        config: Settings of the reports (`custom.accounting.reports`).
        since: Only tasks that finished at or after this time (cf.
            `finished_at` of the records).
        until: Only tasks that finished before this time.

    Returns:
        The reports, in the order in which the tasks finished (and of their
        identifiers), and of their attempts.

    Raises:
        TooManyRecords: More than `config.max_records` records are in the
            time range.
    """
    match: dict[str, Any] = {
        "tool": {"$type": "object"},
        "attempts.0": {"$exists": True},
        **finished_range(since, until),
    }
    records = list(
        usage.find(
            match,
            projection={"_id": False, **dict.fromkeys(FIELDS, True)},
            sort=[("finished_at", 1), ("_id", 1)],
            limit=config.max_records + 1,
        )
    )
    if len(records) > config.max_records:
        raise TooManyRecords(
            f"More than {config.max_records} records (`max_records`) in the time range"
        )
    return [report for record in records for report in _reports(record, config)]
