"""Tests of TES versions."""

import pytest

from pro_tes.ga4gh.tes.versions import OLDEST, SUPPORTED, TesVersion

V10 = TesVersion(1, 0)
V11 = TesVersion(1, 1)


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


def test_supported() -> None:
    """TES 1.0 and 1.1 are supported; versions are ordered and printed."""
    assert OLDEST == V10
    assert {V10, V11} == SUPPORTED
    assert V10 < V11
    assert str(V11) == "1.1"
