"""Fixtures of the tests of the limits of the tasks per user.

The fake TES backends of the tests of the TES API (cf.
`tests/ga4gh/tes/conftest.py`), for the tests at `POST /tasks`; the only
coupling of these tests to the other tests.
"""

from tests.ga4gh.tes.conftest import backend, tracked

__all__ = ["backend", "tracked"]
