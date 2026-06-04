"""End-to-end tests against two live db-sync databases.

Skipped unless both DSNs are provided via environment variables:

    DBSYNC_COMPARE_TEST_DSN1="dbname=... host=/var/run/postgresql"
    DBSYNC_COMPARE_TEST_DSN2="dbname=... host=/var/run/postgresql"

Then run: pytest -m integration
"""

from __future__ import annotations

import os

import pytest

DSN1 = os.environ.get("DBSYNC_COMPARE_TEST_DSN1")
DSN2 = os.environ.get("DBSYNC_COMPARE_TEST_DSN2")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not (DSN1 and DSN2), reason="set DBSYNC_COMPARE_TEST_DSN1/DSN2 to run"),
]


@pytest.fixture(scope="module")
def conns():
    from db_sync_comparator.db import connect

    c1, c2 = connect(DSN1), connect(DSN2)
    yield c1, c2
    c1.close()
    c2.close()


def test_introspect_finds_core_tables(conns):
    from db_sync_comparator.schema import introspect

    schema = introspect(conns[0])
    for t in ("block", "tx", "tx_out", "multi_asset"):
        assert t in schema, f"expected table {t} missing"
    assert "id" in schema["block"].columns


def test_block_window_matches(conns):
    """A historical block window should be content-equivalent across the two
    DBs (this is the property the whole tool checks)."""
    from db_sync_comparator.compare import compare_table
    from db_sync_comparator.planning import plan_table
    from db_sync_comparator.ranges import compute_spine_ranges
    from db_sync_comparator.schema import introspect

    c1, c2 = conns
    s1, s2 = introspect(c1), introspect(c2)
    common = {t: set(s1[t].columns) & set(s2[t].columns) for t in set(s1) & set(s2)}
    window = (8_000_000, 8_001_000)
    r1 = compute_spine_ranges(c1, 0, window)
    r2 = compute_spine_ranges(c2, 0, window)

    plan = plan_table("block", s1["block"], s2["block"], common, full=False, giant_fk_depth=1)
    result = compare_table(plan, c1, c2, r1, r2, cutoff_epoch=0, in_block_range=True, timeout_ms=0)
    assert result.status == "MATCH", result.note
    assert result.n1 == result.n2 > 0
