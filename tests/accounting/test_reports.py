"""Tests of the execution reports of the usage records."""

from datetime import UTC, datetime, timedelta, timezone
import json
from pathlib import Path
import re
from typing import Any

from connexion import FlaskApp
from flask.testing import FlaskClient
from foca import get_custom_config
from jsonschema import (  # type: ignore[import-untyped]
    Draft202012Validator,
    FormatChecker,
)
from pymongo import MongoClient
from pymongo.collection import Collection
import pytest
import tes

from pro_tes.accounting import (
    ExecutionReports,
    TooManyRecords,
    execution_reports,
    usage_record,
)
from pro_tes.accounting.config import IRI_PATTERN, Location, Provider, ReportedBackend
from pro_tes.accounting.reports import (
    REPORT_FORMAT_VERSION,
    report_time,
    rounded_size,
    trusted_image,
)
from pro_tes.config_models import CustomConfig
from tests.conftest import ADMIN, ADMIN_USER, DB, OUTSIDER, USER, make_headers

# The Tool Execution Report schema of ELIXIR Compute, as vendored (see the
# `.license` file next to it)
SCHEMA = json.loads(
    (Path(__file__).parent / "data" / "execution_report.schema.json").read_text()
)
START = datetime(2026, 11, 1, tzinfo=UTC)
PATH = "/ga4gh/tes/v1/usage/reports"
BACKEND = "https://tes.example.org"
# Personal data and details of tasks that reports must not hold
SECRETS = (
    "alice@example.org",
    "secret-group",
    "TASK01",
    "/data/alice.fq",
    "s3://bucket/alice.bam",
    "/outputs/alice.bam",
    "TOKEN_VALUE",
    "s3://bucket/alice-input.fq",
    "alice-project",
    "--very-secret-arg",
)

# Formats that jsonschema checks only with optional dependencies
format_checker = FormatChecker()
IRI = re.compile(IRI_PATTERN)
DATE_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z")


@format_checker.checks("iri")
def is_iri(value: object) -> bool:
    """Check an IRI, as proTES writes them.

    Args:
        value: The value.

    Returns:
        Whether it is an IRI with scheme, without spaces and delimiters.
    """
    return not isinstance(value, str) or IRI.fullmatch(value) is not None


@format_checker.checks("date-time")
def is_date_time(value: object) -> bool:
    """Check a date-time of RFC 3339, in UTC, as proTES writes them.

    Args:
        value: The value.

    Returns:
        Whether it is a valid date-time in UTC (`Z`).
    """
    if not isinstance(value, str):
        return True
    if DATE_TIME.fullmatch(value) is None:
        return False
    datetime.fromisoformat(value)
    return True


VALIDATOR = Draft202012Validator(SCHEMA, format_checker=format_checker)


def validate(reports: list[dict[str, Any]]) -> None:
    """Validate reports against the schema, with its formats.

    Args:
        reports: The reports.
    """
    assert reports
    for report in reports:
        errors = [error.message for error in VALIDATOR.iter_errors(report)]
        assert errors == []


def add(
    usage: Collection[dict[str, Any]],
    task_id: str = "TASK01",
    image: Any = "quay.io/biocontainers/bwa:0.7.18--he4a0461_1",
    logs: list[Any] | None = None,
    day: int = 0,
    cpu_cores: Any = 2,
    backend: str = BACKEND,
) -> dict[str, Any]:
    """Store the record of a task with personal data and details.

    Args:
        usage: Collection of the records.
        task_id: Identifier of the task.
        image: Image of its executor.
        logs: Task logs; by default, one of an hour from `START` plus `day`
            days, with outputs.
        day: Day after `START` on which the task ran.
        cpu_cores: Requested CPU cores.
        backend: URL of its TES instance.

    Returns:
        The record.
    """
    start = START + timedelta(days=day)
    if logs is None:
        logs = [
            tes.TaskLog(
                start_time=start,
                end_time=start + timedelta(hours=1),
                outputs=[
                    tes.OutputFileLog(
                        url="s3://bucket/alice.bam",
                        path="/outputs/alice.bam",
                        size_bytes="123456",
                    )
                ],
                system_logs=["alice@example.org"],
            )
        ]
    document = {
        "user_id": "alice@example.org",
        "groups": ["secret-group"],
        "task": {
            "id": task_id,
            "name": "alice-project",
            "resources": {"cpu_cores": cpu_cores, "ram_gb": 8},
            "executors": [
                {
                    "image": image,
                    "command": ["bwa", "mem", "--very-secret-arg", "/data/alice.fq"],
                    "env": {"TOKEN": "TOKEN_VALUE"},
                }
            ],
            "inputs": [{"url": "s3://bucket/alice-input.fq", "path": "/data/alice.fq"}],
            "tags": {"biotools_id": "bwa", "project": "alice-project"},
        },
    }
    record = usage_record(
        document=document,
        logs=logs,
        backend=backend,
        state="COMPLETE",
        now=start + timedelta(days=1),
    )
    usage.insert_one(record)
    return record


@pytest.fixture
def usage(mongo_client: MongoClient[dict[str, Any]]) -> Collection[dict[str, Any]]:
    """Collection of the records."""
    return mongo_client[DB]["usage"]


def test_report(usage: Collection[dict[str, Any]]) -> None:
    """A record of a task with one attempt gives one valid report.

    With the tool, the TES instance and proTES, the requested CPU cores, the
    times of the attempt and the rounded size of its outputs.
    """
    record = add(usage)
    (report,) = execution_reports(usage, ExecutionReports())
    validate([report])
    assert report == {
        "report_format_version": "0.0.2",
        "tool_identifier": [
            "https://bio.tools/bwa",
            "docker://quay.io/biocontainers/bwa",
        ],
        "tool_name": "bwa",
        "tool_version": "0.7.18",
        "tool_package_version": "0.7.18--he4a0461_1",
        "infra": [
            {"infra_name": "tes.example.org", "infra_identifier": [BACKEND]},
            {
                "infra_name": "proTES",
                "infra_identifier": ["https://github.com/elixir-cloud-aai/protes"],
                "infra_version": record["protes_version"],
            },
        ],
        "cpu_cores_assigned": 2,
        "start_time": "2026-11-01T00:00:00Z",
        "end_time": "2026-11-01T01:00:00Z",
        "final_outputs_size": 120000,
    }


def test_report_version_pinned() -> None:
    """Reports have the version of the vendored schema."""
    assert SCHEMA["properties"]["report_format_version"]["enum"] == [
        REPORT_FORMAT_VERSION
    ]


def test_reports_without_personal_data(usage: Collection[dict[str, Any]]) -> None:
    """Reports hold no user identifiers, task details, URLs or file names."""
    add(usage)
    text = json.dumps(execution_reports(usage, ExecutionReports()))
    for secret in SECRETS:
        assert secret not in text
    assert "alice" not in text
    assert "user" not in text


def test_report_per_attempt(usage: Collection[dict[str, Any]]) -> None:
    """Each attempt of a task (task log) gives a report.

    Attempts without known times or outputs give reports without them.
    """
    add(
        usage,
        logs=[
            tes.TaskLog(start_time=START),
            tes.TaskLog(
                start_time=START + timedelta(hours=1),
                end_time=START + timedelta(hours=2),
            ),
        ],
    )
    reports = execution_reports(usage, ExecutionReports())
    validate(reports)
    assert len(reports) == 2
    for name in ("start_time", "end_time", "final_outputs_size"):
        assert name not in reports[0]
    assert reports[1]["start_time"] == "2026-11-01T01:00:00Z"
    assert "final_outputs_size" not in reports[1]


def test_reports_without_tool_or_attempts(usage: Collection[dict[str, Any]]) -> None:
    """Records without tool or attempts give no reports.

    Also records of earlier versions of proTES, which have neither.
    """
    add(usage, task_id="T1", image="not a valid image")
    add(usage, task_id="T2", logs=[])
    usage.insert_one({"_id": "T3", "finished_at": START, "backend": BACKEND})
    assert execution_reports(usage, ExecutionReports()) == []


@pytest.mark.parametrize(
    "image",
    [
        "ghcr.io/alice-smith/tool:run-alice",
        "registry.example.org/alice/bwa:0.7.18",
        "alice/bwa:0.7.18",
        "quay.io/biocontainers-alice/bwa:1.0",
        "quay.io/alice/biocontainers/bwa:1.0",
    ],
)
def test_reports_of_untrusted_images(
    usage: Collection[dict[str, Any]], image: str
) -> None:
    """Images outside the trusted namespaces give no reports.

    Their names and tags may name users (e.g., personal namespaces).
    """
    add(usage, image=image)
    assert execution_reports(usage, ExecutionReports()) == []


@pytest.mark.parametrize(
    ("repository", "trusted"),
    [
        ("ubuntu", True),
        ("library/ubuntu", True),
        ("docker.io/ubuntu", True),
        ("docker.io/library/ubuntu", True),
        ("biocontainers/bwa", True),
        ("docker.io/biocontainers/bwa", True),
        ("quay.io/biocontainers/bwa", True),
        ("quay.io/biocontainers", True),
        ("quay.io/biocontainersx/bwa", False),
        ("quay.io/bwa", False),
        ("alice/bwa", False),
        ("registry.example.org/team/bwa", False),
        (None, False),
    ],
)
def test_trusted_image(repository: Any, trusted: bool) -> None:
    """Repositories in (or equal to) a trusted namespace are trusted.

    By whole path components; Docker Hub's official images are `library`.
    """
    images = ExecutionReports().images
    assert trusted_image(repository, images) is trusted


def test_trusted_images_configured(usage: Collection[dict[str, Any]]) -> None:
    """Operators may trust further namespaces, e.g., their own registry."""
    add(usage, image="registry.example.org/team/bwa:0.7.18")
    config = ExecutionReports(images=["registry.example.org/team"])
    (report,) = execution_reports(usage, config)
    validate([report])
    assert report["tool_identifier"][-1] == "docker://registry.example.org/team/bwa"


def test_report_without_backend(usage: Collection[dict[str, Any]]) -> None:
    """Records without valid URL of their TES instance name only proTES."""
    add(usage, task_id="T1", backend="")
    (report,) = execution_reports(usage, ExecutionReports())
    validate([report])
    assert [item["infra_name"] for item in report["infra"]] == ["proTES"]


@pytest.mark.parametrize(
    ("value", "time"),
    [
        (datetime(2026, 11, 1, 10, 0, 0, 999999), "2026-11-01T10:00:00Z"),
        (datetime(2026, 11, 1, 10, tzinfo=UTC), "2026-11-01T10:00:00Z"),
        (
            datetime(2026, 11, 1, 11, tzinfo=timezone(timedelta(hours=1))),
            "2026-11-01T10:00:00Z",
        ),
        (None, None),
        ("2026-11-01T10:00:00Z", None),
    ],
)
def test_report_time(value: Any, time: str | None) -> None:
    """Times are date-times of RFC 3339 in UTC, to the second.

    Times without time zone (as PyMongo reads them) are UTC.
    """
    assert report_time(value) == time


@pytest.mark.parametrize("cpu_cores", [None, 0, 1.5, "2"])
def test_report_without_cores(
    usage: Collection[dict[str, Any]], cpu_cores: Any
) -> None:
    """CPU cores that are no positive whole numbers are left out."""
    add(usage, cpu_cores=cpu_cores)
    (report,) = execution_reports(usage, ExecutionReports())
    validate([report])
    assert "cpu_cores_assigned" not in report


def test_reports_in_time_range(usage: Collection[dict[str, Any]]) -> None:
    """Reports may be limited to tasks that finished in a time range.

    In the order in which the tasks finished; the start is included, the end
    is not.
    """
    for day in (2, 0, 1):
        add(usage, task_id=f"T{day}", day=day)
    reports = execution_reports(usage, ExecutionReports())
    assert [report["start_time"][:10] for report in reports] == [
        "2026-11-01",
        "2026-11-02",
        "2026-11-03",
    ]
    reports = execution_reports(
        usage,
        ExecutionReports(),
        since=START + timedelta(days=1, hours=1),
        until=START + timedelta(days=2, hours=1),
    )
    assert [report["start_time"][:10] for report in reports] == ["2026-11-02"]


def test_too_many_records(usage: Collection[dict[str, Any]]) -> None:
    """Requests for more records than `max_records` are rejected."""
    for day in range(3):
        add(usage, task_id=f"T{day}", day=day)
    config = ExecutionReports(max_records=2)
    with pytest.raises(TooManyRecords):
        execution_reports(usage, config)
    assert len(execution_reports(usage, config, since=START + timedelta(days=1))) == 2


def test_reports_configured(usage: Collection[dict[str, Any]]) -> None:
    """Configured providers and locations are named in the reports.

    The providers of `infra` first, then the TES instance, as configured,
    and its location, or else the location of all reports.
    """
    add(usage, task_id="T1")
    add(usage, task_id="T2", day=1, backend="https://other.example.org")
    location = Location.model_validate(
        {"name": "Data centre", "address": {"addressCountry": "FI"}}
    )
    config = ExecutionReports.model_validate(
        {
            "infra": [
                {
                    "infra_name": "Example Organization",
                    "infra_identifier": ["https://ror.org/000000000"],
                }
            ],
            "location": {"address": {"addressCountry": "DE"}},
            "backends": {
                "HTTPS://TES.example.org:443/": {
                    "infra_name": "Example TES",
                    "infra_identifier": ["https://registry.example.org/tes"],
                    "infra_version": "1.0",
                    "location": location.model_dump(by_alias=True),
                }
            },
        }
    )
    reports = execution_reports(usage, config)
    validate(reports)
    assert [item["infra_name"] for item in reports[0]["infra"]] == [
        "Example Organization",
        "Example TES",
        "proTES",
    ]
    assert reports[0]["infra"][1] == {
        "infra_name": "Example TES",
        "infra_identifier": ["https://registry.example.org/tes"],
        "infra_version": "1.0",
    }
    assert reports[0]["location"] == {
        "name": "Data centre",
        "address": {"addressCountry": "FI"},
    }
    assert reports[1]["infra"][1]["infra_name"] == "other.example.org"
    assert reports[1]["location"] == {"address": {"addressCountry": "DE"}}


@pytest.mark.parametrize(
    "config",
    [
        {"max_records": 0},
        {"infra": [{"infra_name": "X", "infra_identifier": []}]},
        {"infra": [{"infra_name": "X", "infra_identifier": ["no iri"]}]},
        {"infra": [{"infra_name": "", "infra_identifier": ["https://x.org"]}]},
        {"location": {"address": {"addressCountry": "Finland"}}},
        {"location": {"address": {"country": "FI"}}},
        {"backends": {"ftp://tes.example.org": {}}},
        {"images": ["quay.io/biocontainers/"]},
        {"images": ["Quay.io/BioContainers"]},
        {"images": ["quay.io/biocontainers:latest"]},
        {"images": [""]},
        {
            "backends": {
                url: {"infra_name": "X", "infra_identifier": ["https://x.org"]}
                for url in ("https://tes.example.org", "https://TES.example.org/")
            }
        },
    ],
)
def test_config_invalid(config: dict[str, Any]) -> None:
    """Invalid settings are errors."""
    with pytest.raises(ValueError):
        ExecutionReports.model_validate(config)


def test_config_images() -> None:
    """Namespaces of images may have a registry with port."""
    config = ExecutionReports(images=["localhost:5000/tools", "ghcr.io/org"])
    assert config.images == ["localhost:5000/tools", "ghcr.io/org"]


def test_config_models() -> None:
    """Providers and TES instances take the keys of the report format."""
    provider = Provider(infra_name="X", infra_identifier=["https://x.org"])
    assert provider.model_dump(exclude_none=True) == {
        "infra_name": "X",
        "infra_identifier": ["https://x.org"],
    }
    backend = ReportedBackend.model_validate(
        {
            "infra_name": "X",
            "infra_identifier": ["https://x.org"],
            "location": {"address": {"address_locality": "Espoo"}},
        }
    )
    assert backend.location is not None
    assert backend.location.model_dump(by_alias=True, exclude_none=True) == {
        "address": {"addressLocality": "Espoo"}
    }


@pytest.mark.parametrize(
    ("size", "rounded"),
    [
        (0, 0),
        (7, 7),
        (99, 99),
        (123, 120),
        (125, 130),
        (149, 150),
        (123456, 120000),
        (10**20 + 5 * 10**18, 11 * 10**19),
    ],
)
def test_rounded_size(size: int, rounded: int) -> None:
    """Sizes are rounded to two significant digits (half up)."""
    assert rounded_size(size) == rounded


@pytest.mark.parametrize("user", [ADMIN, ADMIN_USER])
def test_get_reports(
    client: FlaskClient, usage: Collection[dict[str, Any]], user: str
) -> None:
    """Admins get the reports, valid and without personal data."""
    add(usage)
    response = client.get(PATH, headers=make_headers(user))
    assert response.status_code == 200
    reports = response.get_json()
    validate(reports)
    assert reports[0]["tool_name"] == "bwa"
    for secret in SECRETS:
        assert secret not in response.get_data(as_text=True)


def test_get_reports_in_time_range(
    client: FlaskClient, usage: Collection[dict[str, Any]]
) -> None:
    """Admins may limit the reports to a time range."""
    for day in range(3):
        add(usage, task_id=f"T{day}", day=day)
    response = client.get(
        PATH,
        query_string={"since": "2026-11-02T00:00:00Z", "until": "2026-11-03t00:00:00"},
        headers=make_headers(ADMIN),
    )
    assert response.status_code == 200
    assert [report["start_time"] for report in response.get_json()] == [
        "2026-11-02T00:00:00Z"
    ]


def test_get_reports_configured(
    app: FlaskApp, client: FlaskClient, usage: Collection[dict[str, Any]]
) -> None:
    """The reports name the configured providers and location."""
    add(usage)
    with app.app.app_context():
        get_custom_config(CustomConfig).accounting.reports = ExecutionReports(
            location=Location.model_validate({"address": {"addressCountry": "SE"}})
        )
    (report,) = client.get(PATH, headers=make_headers(ADMIN)).get_json()
    assert report["location"] == {"address": {"addressCountry": "SE"}}


def test_get_reports_too_many(
    app: FlaskApp, client: FlaskClient, usage: Collection[dict[str, Any]]
) -> None:
    """Requests for more records than `max_records` get `400`."""
    for day in range(2):
        add(usage, task_id=f"T{day}", day=day)
    with app.app.app_context():
        get_custom_config(CustomConfig).accounting.reports = ExecutionReports(
            max_records=1
        )
    response = client.get(PATH, headers=make_headers(ADMIN))
    assert response.status_code == 400
    assert "`max_records`" in response.get_json()["detail"]


def test_get_reports_invalid_time(client: FlaskClient) -> None:
    """Invalid times are rejected (`400`)."""
    response = client.get(
        PATH, query_string={"since": "yesterday"}, headers=make_headers(ADMIN)
    )
    assert response.status_code == 400
    assert response.get_json()["detail"] == (
        "`since` or `until` is no valid time (RFC 3339)."
    )


@pytest.mark.parametrize("user", [USER, OUTSIDER])
def test_get_reports_forbidden(client: FlaskClient, user: str) -> None:
    """Users who are not admins get `403`."""
    assert client.get(PATH, headers=make_headers(user)).status_code == 403


def test_get_reports_anonymous(anonymous_client: FlaskClient) -> None:
    """Requests without access token get `401`."""
    assert anonymous_client.get(PATH).status_code == 401


def test_config_of_app(app: FlaskApp) -> None:
    """The base config has the default settings of the reports."""
    with app.app.app_context():
        reports = get_custom_config(CustomConfig).accounting.reports
    assert reports == ExecutionReports()
