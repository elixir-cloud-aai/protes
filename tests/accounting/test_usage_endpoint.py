"""Tests of serving the resources that tasks used, per user (`GET /usage`)."""

from datetime import UTC, datetime, timedelta
from typing import Any

from connexion import FlaskApp
from flask.testing import FlaskClient
from foca import get_custom_config
from pymongo import MongoClient
import pytest
import tes

from pro_tes.accounting import Accounting, usage_record
from pro_tes.config_models import CustomConfig
from tests.conftest import ADMIN, ADMIN_USER, DB, OUTSIDER, USER, make_headers

PATH = "/ga4gh/tes/v1/usage"
START = datetime(2026, 11, 1, tzinfo=UTC)


@pytest.fixture
def records(mongo_client: MongoClient[dict[str, Any]]) -> None:
    """Records of a task of `USER` and one of `ADMIN_USER`, a day apart."""
    for day, (task_id, user_id) in enumerate((("T1", USER), ("T2", ADMIN_USER))):
        start = START + timedelta(days=day)
        document = {
            "user_id": user_id,
            "task": {"id": task_id, "resources": {"cpu_cores": 2}},
        }
        mongo_client[DB]["usage"].insert_one(
            usage_record(
                document=document,
                logs=[
                    tes.TaskLog(start_time=start, end_time=start + timedelta(hours=1))
                ],
                backend="https://tes.example.org",
                state="COMPLETE",
                now=start + timedelta(hours=2),
            )
        )


@pytest.mark.parametrize("user", [ADMIN, ADMIN_USER])
def test_get_usage(client: FlaskClient, records: None, user: str) -> None:
    """Admins get the totals per user; by default, nothing is recorded."""
    response = client.get(PATH, headers=make_headers(user))
    assert response.status_code == 200
    report = response.get_json()
    assert report["recording"] is False
    assert [total["user_id"] for total in report["users"]] == [USER, ADMIN_USER]
    assert report["users"][0] == {
        "user_id": USER,
        "tasks": 1,
        "untimed_tasks": 0,
        "wall_seconds": 3600.0,
        "cpu_core_seconds": 7200.0,
        "ram_gb_seconds": 0.0,
        "disk_gb_seconds": 0.0,
    }


def test_get_usage_filtered(client: FlaskClient, records: None) -> None:
    """Admins may filter by user and time range.

    UTC without time zone; `t` and `z` may be lowercase, as in RFC 3339.
    """
    headers = make_headers(ADMIN)
    response = client.get(PATH, query_string={"user_id": USER}, headers=headers)
    assert [total["user_id"] for total in response.get_json()["users"]] == [USER]
    response = client.get(
        PATH,
        query_string={"since": "2026-11-02t00:00:00", "until": "2026-11-03T00:00:00z"},
        headers=headers,
    )
    assert response.status_code == 200
    assert [total["user_id"] for total in response.get_json()["users"]] == [ADMIN_USER]


def test_get_usage_recording(app: FlaskApp, client: FlaskClient) -> None:
    """The report tells whether usage is recorded."""
    with app.app.app_context():
        get_custom_config(CustomConfig).accounting = Accounting(enabled=True)
    response = client.get(PATH, headers=make_headers(ADMIN))
    assert response.get_json() == {"recording": True, "users": []}


@pytest.mark.parametrize(
    "value",
    [
        "yesterday",
        "9999-12-31T23:59:59-05:00",
        "0001-01-01T00:00:00+01:00",
        "2026-11-01",
        "20261101T000000Z",
        "2026-11-01T00:00",
        "2026-W44-7T00:00:00Z",
        "2016-12-31T23:59:60Z",
    ],
)
@pytest.mark.parametrize("name", ["since", "until"])
def test_get_usage_invalid_time(client: FlaskClient, name: str, value: str) -> None:
    """Invalid times are rejected (`400`).

    Also times out of range in UTC, and ISO 8601 values that are no RFC 3339
    date-time (e.g., dates without time).
    """
    response = client.get(PATH, query_string={name: value}, headers=make_headers(ADMIN))
    assert response.status_code == 400
    assert response.get_json()["detail"] == (
        "`since` or `until` is no valid time (RFC 3339)."
    )


@pytest.mark.parametrize("user", [USER, OUTSIDER])
def test_get_usage_forbidden(client: FlaskClient, records: None, user: str) -> None:
    """Users who are not admins get `403`."""
    assert client.get(PATH, headers=make_headers(user)).status_code == 403


def test_get_usage_anonymous(anonymous_client: FlaskClient) -> None:
    """Requests without access token get `401`."""
    assert anonymous_client.get(PATH).status_code == 401


def test_usage_indexes(
    app: FlaskApp, mongo_client: MongoClient[dict[str, Any]]
) -> None:
    """The records have indexes for the time ranges, also per user."""
    keys = [
        list(index["key"])
        for index in mongo_client[DB]["usage"].index_information().values()
    ]
    assert [("finished_at", 1)] in keys
    assert [("user_id", 1), ("finished_at", 1)] in keys
