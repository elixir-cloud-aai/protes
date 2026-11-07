"""Fixtures of the tests of the retention of finished tasks.

The fake TES backends of the tests of the TES API (cf.
`tests/ga4gh/tes/conftest.py`), for the tests with tasks created via the
API; the only coupling of these tests to the other tests.
"""

from tests.ga4gh.tes.conftest import backend, task_id, tracked

__all__ = ["backend", "task_id", "tracked"]
