"""TES versions that proTES knows."""

from dataclasses import dataclass
import re
from typing import Self

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


SUPPORTED: frozenset[TesVersion] = frozenset({TesVersion(1, 0), TesVersion(1, 1)})
"""Versions that proTES can relay tasks to."""

OLDEST: TesVersion = min(SUPPORTED)
"""Oldest supported version, assumed for backends of unknown versions."""
