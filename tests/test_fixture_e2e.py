"""End-to-end tests against a real PostgreSQL, on tiny synthetic fixtures.

These exercise the *execution* path that the pure-logic unit tests can't:
``introspect`` → ``compute_spine_ranges`` (the index-seek SQL) → ``compare_table``
→ ``localize``, against two miniature "db-sync-shaped" databases.

The two fixture databases deliberately bake in the structural realities the tool
exists to handle:

* **surrogate-id drift** - DB2 uses an id offset, so the same logical rows have
  different ``id`` and foreign-key values (a correct comparison must still MATCH);
* **a tip difference** - DB2 has one extra block beyond the common cutoff, which
  bounding must exclude.

On top of the matching baseline, individual tests introduce a single deliberate
fault (a corrupted value, a dropped row, the real pool_relay port overflow, an
extra accumulator row) and assert the tool classifies it correctly.

Engine choice: a real PostgreSQL (not SQLite) because the tool's whole value is
the PostgreSQL SQL it generates - ``md5(ROW(...)::text)``, ``::bit(60)::numeric``,
``information_schema``/``pg_catalog`` - none of which SQLite can run. The server
is provided by pytest-postgresql locally, or an external server (a CI service)
when ``DBSYNC_COMPARE_PG_EXTERNAL`` is set.

Marked ``fixture`` and excluded from the default test run (which is DB-free); run
with ``pytest -m fixture`` or ``make test-db``.
"""

from __future__ import annotations

import glob
import os

import psycopg
import pytest

pytestmark = pytest.mark.fixture

EXTERNAL = bool(os.environ.get("DBSYNC_COMPARE_PG_EXTERNAL"))

if not EXTERNAL:
    pytest.importorskip("pytest_postgresql", reason="install pytest-postgresql or set DBSYNC_COMPARE_PG_EXTERNAL")
    from pytest_postgresql import factories


def _find_pg_ctl() -> str | None:
    """Locate pg_ctl on Debian/Ubuntu/RHEL layouts where it isn't on PATH."""
    for pattern in ("/usr/lib/postgresql/*/bin/pg_ctl", "/usr/pgsql-*/bin/pg_ctl"):
        hits = sorted(glob.glob(pattern))
        if hits:
            return hits[-1]
    return None


if not EXTERNAL:
    _exe = _find_pg_ctl()
    _db_sync_pg_proc = factories.postgresql_proc(executable=_exe) if _exe else factories.postgresql_proc()


# --------------------------------------------------------------------------- #
# Synthetic "db-sync-shaped" schema + seed
# --------------------------------------------------------------------------- #

_DDL = [
    "CREATE TABLE block (id bigint PRIMARY KEY, hash bytea, block_no int, epoch_no int, slot_no bigint)",
    "CREATE TABLE tx (id bigint PRIMARY KEY, hash bytea, block_id bigint)",
    'CREATE TABLE tx_out (id bigint PRIMARY KEY, tx_id bigint, "index" int, value numeric, address text)',
    "CREATE TABLE multi_asset (id bigint PRIMARY KEY, policy bytea, name bytea, fingerprint text)",
    "CREATE TABLE ma_tx_out (id bigint PRIMARY KEY, ident bigint, quantity numeric, tx_out_id bigint)",
    "CREATE TABLE stake_address (id bigint PRIMARY KEY, hash_raw bytea, view text)",
    "CREATE TABLE pool_hash (id bigint PRIMARY KEY, hash_raw bytea, view text)",
    "CREATE TABLE pool_update (id bigint PRIMARY KEY, hash_id bigint, registered_tx_id bigint, cert_index int)",
    "CREATE TABLE pool_relay (id bigint PRIMARY KEY, update_id bigint, ipv4 text, port int)",
    "CREATE TABLE epoch_stake (id bigint PRIMARY KEY, addr_id bigint, pool_id bigint, amount numeric, epoch_no int)",
    # Since schema stage-two v49, finalized-epoch aggregates live in the
    # epoch_finalized base table; `epoch` is a VIEW over it (+ the live current
    # epoch); epoch_sync_enabled is a single-row operator toggle.
    "CREATE TABLE epoch_finalized (id bigint PRIMARY KEY, out_sum numeric, fees numeric, "
    "tx_count int, blk_count int, no int, start_time timestamp, end_time timestamp)",
    "CREATE TABLE epoch_sync_enabled (singleton boolean PRIMARY KEY, enabled boolean)",
    "CREATE VIEW epoch AS SELECT id, out_sum, fees, tx_count, blk_count, no, start_time, end_time FROM epoch_finalized",
    "CREATE TABLE gov_action_proposal (id bigint PRIMARY KEY, tx_id bigint, index int)",
    "CREATE TABLE meta (id bigint PRIMARY KEY, version text)",
]


def _create_schema(conn) -> None:
    for ddl in _DDL:
        conn.execute(ddl)


def _seed_data(conn, off: int, extra_tip: bool, version: str) -> None:
    """Insert identical *logical* content, but with every id (and FK) shifted by
    ``off`` - modelling two syncs whose surrogate ids drifted. ``extra_tip`` adds
    one block beyond the others (the tip gap)."""
    blocks = range(0, 7 if extra_tip else 6)  # block_no 0..5 (+6 for the ahead DB)
    txs = range(1, 7 if extra_tip else 6)  # one tx per block 1..5 (+6)

    for bn in blocks:
        conn.execute(
            "INSERT INTO block VALUES (%s, decode(md5(%s),'hex'), %s, 0, %s)",
            (bn + 1 + off, f"block-{bn}", bn, bn),
        )
    for t in txs:
        conn.execute(
            "INSERT INTO tx VALUES (%s, decode(md5(%s),'hex'), %s)",
            (t + off, f"tx-{t}", t + 1 + off),  # tx t lives in block_no t -> block id (t+1)
        )
        for o in (0, 1):
            conn.execute(
                "INSERT INTO tx_out VALUES (%s, %s, %s, %s, %s)",
                ((t - 1) * 2 + o + 1 + off, t + off, o, 1000 * t + o, f"addr{(t + o) % 3}"),
            )
    for k in (1, 2):
        conn.execute(
            "INSERT INTO multi_asset VALUES (%s, decode(md5(%s),'hex'), decode(md5(%s),'hex'), %s)",
            (k + off, f"pol-{k}", f"name-{k}", f"asset{k}"),
        )
    for t in range(1, 6):  # ma_tx_out only for the in-range txs
        conn.execute(
            "INSERT INTO ma_tx_out VALUES (%s, %s, %s, %s)",
            (t + off, (t % 2) + 1 + off, 5 * t, (t - 1) * 2 + 1 + off),
        )
    conn.execute("INSERT INTO stake_address VALUES (%s, decode(md5('stake-1'),'hex'), 'stake1xyz')", (1 + off,))
    conn.execute("INSERT INTO pool_hash VALUES (%s, decode(md5('pool-1'),'hex'), 'pool1xyz')", (1 + off,))
    conn.execute("INSERT INTO pool_update VALUES (%s, %s, %s, 0)", (1 + off, 1 + off, 2 + off))
    conn.execute("INSERT INTO pool_relay VALUES (%s, %s, '1.2.3.4', 52636)", (1 + off, 1 + off))
    conn.execute("INSERT INTO epoch_stake VALUES (%s, %s, %s, 999, 0)", (1 + off, 1 + off, 1 + off))
    # Finalized-epoch aggregate for epoch 0 - identical logical content on both
    # sides (drift only in the surrogate id). epoch_sync_enabled is identical too.
    conn.execute(
        "INSERT INTO epoch_finalized VALUES (%s, 12345, 678, 5, 6, 0, '2020-01-01 00:00:00', '2020-01-05 00:00:00')",
        (1 + off,),
    )
    conn.execute("INSERT INTO epoch_sync_enabled VALUES (true, true)")
    conn.execute("INSERT INTO meta VALUES (%s, %s)", (1 + off, version))


@pytest.fixture(scope="session")
def pg_params(request) -> dict:
    if EXTERNAL:
        return {
            "host": os.environ.get("PGHOST", "localhost"),
            "port": os.environ.get("PGPORT", "5432"),
            "user": os.environ.get("PGUSER", "postgres"),
            "password": os.environ.get("PGPASSWORD", ""),
        }
    proc = request.getfixturevalue("_db_sync_pg_proc")
    return {"host": proc.host, "port": proc.port, "user": proc.user, "password": proc.password or ""}


def _conninfo(params: dict, dbname: str) -> str:
    s = f"host={params['host']} port={params['port']} user={params['user']} dbname={dbname}"
    if params["password"]:
        s += f" password={params['password']}"
    return s


_ALL_TABLES = (
    "block, tx, tx_out, multi_asset, ma_tx_out, stake_address, pool_hash, "
    "pool_update, pool_relay, epoch_stake, epoch_finalized, epoch_sync_enabled, "
    "gov_action_proposal, meta"
)


@pytest.fixture(scope="session")
def _db_dsns(pg_params):
    """Create two empty databases with the schema - once per session.

    CREATE DATABASE is the slow part (template copy + checkpoint), so we do it
    once and let each test reseed cheaply via TRUNCATE.
    """
    n1, n2 = "dbsync_cmp_v1", "dbsync_cmp_v2"
    admin = psycopg.connect(_conninfo(pg_params, "postgres"), autocommit=True)
    for n in (n1, n2):
        admin.execute(f'DROP DATABASE IF EXISTS "{n}" WITH (FORCE)')
        admin.execute(f'CREATE DATABASE "{n}"')
    dsn1, dsn2 = _conninfo(pg_params, n1), _conninfo(pg_params, n2)
    for dsn in (dsn1, dsn2):
        with psycopg.connect(dsn, autocommit=True) as c:
            _create_schema(c)
    try:
        yield dsn1, dsn2
    finally:
        for n in (n1, n2):
            admin.execute(f'DROP DATABASE IF EXISTS "{n}" WITH (FORCE)')
        admin.close()


@pytest.fixture
def two_dbs(_db_dsns):
    """Reset to a clean, matching baseline and yield two open connections.

    DB2 carries an id offset (drift) and one extra block beyond the tip. Each
    test gets a freshly reseeded pair (truncate + reinsert - no CREATE DATABASE),
    and may then mutate DB2 to model a fault.
    """
    dsn1, dsn2 = _db_dsns
    c1 = psycopg.connect(dsn1, autocommit=True)
    c2 = psycopg.connect(dsn2, autocommit=True)
    for c in (c1, c2):
        c.execute(f"TRUNCATE {_ALL_TABLES}")
    _seed_data(c1, off=0, extra_tip=False, version="v1")
    _seed_data(c2, off=100, extra_tip=True, version="v2")
    try:
        yield c1, c2
    finally:
        c1.close()
        c2.close()


# --------------------------------------------------------------------------- #
# A mini driver mirroring cli.main's per-table flow (with epoch-margin 0)
# --------------------------------------------------------------------------- #


def _compare_one(c1, c2, table: str, full: bool = False, cap_lifecycle: bool = True):
    from db_sync_comparator.compare import compare_table
    from db_sync_comparator.planning import plan_table
    from db_sync_comparator.ranges import compute_spine_ranges, get_tip
    from db_sync_comparator.schema import introspect

    s1, s2 = introspect(c1), introspect(c2)
    common = {t: set(s1[t].columns) & set(s2[t].columns) for t in set(s1) & set(s2)}
    (bn1, en1), (bn2, en2) = get_tip(c1), get_tip(c2)
    cutoff_block, cutoff_epoch = min(bn1, bn2), min(en1, en2)
    r1 = compute_spine_ranges(c1, cutoff_block, None)
    r2 = compute_spine_ranges(c2, cutoff_block, None)
    # Like cli.main: lifecycle epochs are capped at the cutoff epoch.
    cap = cutoff_epoch if cap_lifecycle else None
    plan = plan_table(table, s1[table], s2[table], common, full, 1, lifecycle_cutoff_epoch=cap)
    result = compare_table(plan, c1, c2, r1, r2, cutoff_epoch, False, 0)
    return plan, result, cutoff_block


# --------------------------------------------------------------------------- #
# Positive tests
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("table", ["block", "tx_out", "ma_tx_out", "epoch_stake", "epoch_finalized", "pool_relay"])
def test_identical_content_matches_despite_id_drift(two_dbs, table):
    _, result, _ = _compare_one(*two_dbs, table)
    assert result.status == "MATCH", f"{table}: {result.note}"
    assert result.n1 == result.n2 > 0


def test_epoch_finalized_is_epoch_anchored(two_dbs):
    # The finalized-epoch aggregate must be bounded by epoch number, not treated
    # as an unanchored accumulator.
    plan, result, _ = _compare_one(*two_dbs, "epoch_finalized")
    assert plan.anchor_kind == "epoch"
    assert result.status == "MATCH", result.note


def test_epoch_view_is_dropped_not_compared(two_dbs):
    # `epoch` is a VIEW (over epoch_finalized); introspect() keeps only base
    # tables, so it must not surface for comparison on either side.
    from db_sync_comparator.schema import introspect

    c1, c2 = two_dbs
    assert "epoch" not in introspect(c1)
    assert "epoch" not in introspect(c2)
    # The backing base tables are still present.
    assert "epoch_finalized" in introspect(c1)


def test_epoch_sync_enabled_is_excluded(two_dbs):
    # Operator config (whether epoch aggregation is on), not chain data.
    from db_sync_comparator.planning import plan_table
    from db_sync_comparator.schema import introspect

    c1, c2 = two_dbs
    s1, s2 = introspect(c1), introspect(c2)
    common = {t: set(s1[t].columns) & set(s2[t].columns) for t in set(s1) & set(s2)}
    plan = plan_table("epoch_sync_enabled", s1["epoch_sync_enabled"], s2["epoch_sync_enabled"], common, False, 1)
    assert plan.kind == "excluded"


def test_tip_difference_is_excluded(two_dbs):
    # DB2 has an extra block (block_no 6) beyond the common cutoff (5); both
    # sides must compare exactly the 6 shared blocks.
    _, result, cutoff = _compare_one(*two_dbs, "block")
    assert cutoff == 5
    assert result.status == "MATCH"
    assert result.n1 == result.n2 == 6


def test_giant_table_has_value_proof(two_dbs):
    _, result, _ = _compare_one(*two_dbs, "tx_out")  # tx_out is a GIANT table
    assert result.value1 is not None and result.value1 == result.value2


# --------------------------------------------------------------------------- #
# Negative tests (one deliberate fault each, applied to DB2)
# --------------------------------------------------------------------------- #


def _covers(window: str, block_no: int) -> bool:
    # parse "block_no LO..HI: ..." and check LO <= block_no <= HI
    import re

    m = re.search(r"block_no (\d+)\.\.(\d+)", window)
    if not m:
        return False
    return int(m.group(1)) <= block_no <= int(m.group(2))


def test_localize_buckets_finds_the_faulty_block(two_dbs):
    # Inject a fault in a KNOWN block (block_no 3 = tx3, tx_id 103 in DB2) and
    # check both localizers point at it. tx3's two outputs get a bumped value.
    from db_sync_comparator.compare import localize, localize_buckets

    c1, c2 = two_dbs
    c2.execute("UPDATE tx_out SET value = value + 1 WHERE tx_id = 103")
    plan, result, cutoff = _compare_one(c1, c2, "tx_out")
    assert result.status == "HASH_DIFF"

    bucket_wins = localize_buckets(plan, c1, c2, cutoff, 0, n_buckets=1024).windows
    assert bucket_wins, "buckets localizer returned no window"
    assert any(_covers(w, 3) for w in bucket_wins), bucket_wins

    # parity: the bisection localizer also brackets block 3
    bisect_wins = localize(plan, c1, c2, 0, cutoff, cutoff, 0).windows
    assert any(_covers(w, 3) for w in bisect_wins), bisect_wins


def test_bisection_says_when_it_stops_at_the_window_cap(two_dbs):
    # Every in-range block (1..5) gets a differing output. With a cap of 2 the
    # bisection must return the 2 LOWEST windows and say it stopped early; with
    # the default cap it finds all 5 and says the list is complete.
    from db_sync_comparator.compare import localize

    c1, c2 = two_dbs
    c2.execute("UPDATE tx_out SET value = value + 1 WHERE tx_id BETWEEN 101 AND 105")
    plan, result, cutoff = _compare_one(c1, c2, "tx_out")
    assert result.status == "HASH_DIFF"

    capped = localize(plan, c1, c2, 0, cutoff, cutoff, 0, max_leaves=2, min_width=0)
    assert [w.split(":")[0] for w in capped.windows] == ["block_no 1..1", "block_no 2..2"]
    assert capped.truncated is True

    full = localize(plan, c1, c2, 0, cutoff, cutoff, 0, min_width=0)
    assert [w.split(":")[0] for w in full.windows] == [f"block_no {b}..{b}" for b in range(1, 6)]
    assert full.truncated is False

    # Reaching the cap exactly is not "stopped early": with a cap of 5 the 5th
    # window is the last one and nothing is left unsearched.
    exact = localize(plan, c1, c2, 0, cutoff, cutoff, 0, max_leaves=5, min_width=0)
    assert len(exact.windows) == 5
    assert exact.truncated is False


def test_a_failed_localization_is_recorded_not_just_printed(two_dbs):
    # A localization query that errors must leave a trace in the result (and so
    # in the JSON report), not an empty window list that reads as "nothing found".
    from db_sync_comparator.cli import _localize_mismatch
    from db_sync_comparator.model import TablePlan, TableResult

    c1, c2 = two_dbs
    plan = TablePlan(
        "no_such_table", "normal", select_exprs=['t0."x"'], anchor_kind="idrange", spine="tx", anchor_col="tx_id"
    )
    r = TableResult("no_such_table", "normal", status="HASH_DIFF")
    _localize_mismatch(r, plan, c1, c2, cutoff_block=5, cutoff_epoch=0, method="bisect", n_buckets=1024, timeout_ms=0)
    assert r.localized == []
    assert r.localized_truncated is False
    assert r.localize_error.startswith("UndefinedTable")


def test_corrupted_value_is_hash_diff(two_dbs):
    c1, c2 = two_dbs
    c2.execute("UPDATE tx_out SET value = value + 1 WHERE id = 103")  # one in-range output
    _, result, _ = _compare_one(c1, c2, "tx_out")
    assert result.status == "HASH_DIFF"
    assert result.n1 == result.n2  # same row count - only a value changed


def test_dropped_row_is_count_diff(two_dbs):
    c1, c2 = two_dbs
    c2.execute("DELETE FROM tx_out WHERE id = 104")
    _, result, _ = _compare_one(c1, c2, "tx_out")
    assert result.status == "COUNT_DIFF"
    assert result.n2 == result.n1 - 1


def test_pool_relay_port_overflow_is_found_and_localized(two_dbs):
    # Reproduces the real db-sync 13.7.1.0 regression: a port > 32767 stored as a
    # signed-16-bit negative (52636 -> 52636 - 65536 = -12900).
    c1, c2 = two_dbs
    c2.execute("UPDATE pool_relay SET port = -12900 WHERE id = 101")
    plan, result, cutoff = _compare_one(c1, c2, "pool_relay")
    assert result.status == "HASH_DIFF"
    assert result.n1 == result.n2

    from db_sync_comparator.compare import localize

    windows = localize(plan, c1, c2, 0, cutoff, cutoff, 0).windows
    assert windows and any("block_no" in w for w in windows)


def test_verify_accumulator_subset_check(two_dbs, _db_dsns):
    # DB2 gets an extra multi_asset row → the subset check must report it as a
    # clean superset (only_db2 = 1, only_db1 = 0), confirming a tip-gap-style delta.
    from db_sync_comparator.verify import verify_accumulator

    _, c2 = two_dbs
    c2.execute(
        "INSERT INTO multi_asset VALUES (777, decode(md5('extra-pol'),'hex'), decode(md5('extra-name'),'hex'), 'x')"
    )
    v = verify_accumulator(_db_dsns[0], _db_dsns[1], "multi_asset")
    assert v["verified"] is True
    assert v["only_db1"] == 0
    assert v["only_db2"] == 1
    assert "db1 ⊆ db2" in v["verdict"]


def test_one_sided_zero_is_flagged_as_likely_disabled(two_dbs):
    # Mirrors the real pool_stat case: one version has the table populated, the
    # other has 0 rows because the feature was disabled in its insert_options.
    # The tool must call this out (not localize it as data corruption).
    c1, c2 = two_dbs
    c1.execute("TRUNCATE epoch_stake")  # DB1 now empty, DB2 still populated
    _, result, _ = _compare_one(c1, c2, "epoch_stake")
    assert result.status == "COUNT_DIFF"
    assert result.n1 == 0 and result.n2 > 0
    assert "disabled in config" in result.note


def test_accumulator_count_delta_is_informational(two_dbs):
    c1, c2 = two_dbs
    c2.execute(
        "INSERT INTO multi_asset VALUES (601, decode(md5('pol-extra'),'hex'), decode(md5('name-extra'),'hex'), 'extra')"
    )
    plan, result, _ = _compare_one(c1, c2, "multi_asset")
    assert plan.kind == "accumulator"
    assert result.status == "COUNT_DIFF"  # treated as informational by the reporter, not a hard failure


# --------------------------------------------------------------------------- #
# Address (use_address_table) variant fixtures + tests
#
# In the Address variant the output address is not inline on tx_out; it lives in
# a separate, deduplicated `address` table that tx_out points at via address_id.
# These tests prove end-to-end that the tool (a) translates address_id to the
# address's natural key so the per-output address is actually compared, and
# (b) treats the address table as a subset-checkable accumulator. They reuse the
# session's PostgreSQL (pg_params) but build their own pair of databases.
# --------------------------------------------------------------------------- #

_DDL_ADDR = [
    "CREATE TABLE block (id bigint PRIMARY KEY, hash bytea, block_no int, epoch_no int, slot_no bigint)",
    "CREATE TABLE tx (id bigint PRIMARY KEY, hash bytea, block_id bigint)",
    'CREATE TABLE tx_out (id bigint PRIMARY KEY, tx_id bigint, "index" int, value numeric, address_id bigint)',
    "CREATE TABLE address (id bigint PRIMARY KEY, address text, raw bytea, has_script boolean, "
    "payment_cred bytea, stake_address_id bigint)",
    "CREATE TABLE stake_address (id bigint PRIMARY KEY, hash_raw bytea, view text)",
    # Spine tables compute_spine_ranges always probes; left empty here.
    "CREATE TABLE pool_update (id bigint PRIMARY KEY, hash_id bigint, registered_tx_id bigint, cert_index int)",
    "CREATE TABLE gov_action_proposal (id bigint PRIMARY KEY, tx_id bigint, index int)",
    "CREATE TABLE meta (id bigint PRIMARY KEY, version text)",
]

_ALL_TABLES_ADDR = "block, tx, tx_out, address, stake_address, pool_update, gov_action_proposal, meta"


def _seed_data_addr(conn, off: int, extra_tip: bool, version: str) -> None:
    """Address-variant seed: same logical content with every id (and FK) shifted
    by ``off`` (drift), and ``extra_tip`` adding one block beyond the cutoff. The
    output address lives in the `address` table; tx_out references it by id."""
    blocks = range(0, 7 if extra_tip else 6)  # block_no 0..5 (+6 for the ahead DB)
    txs = range(1, 7 if extra_tip else 6)

    conn.execute("INSERT INTO stake_address VALUES (%s, decode(md5('stake-1'),'hex'), 'stake1xyz')", (1 + off,))
    # Three distinct addresses; addr0 carries a stake part, the others don't.
    for k in (0, 1, 2):
        conn.execute(
            "INSERT INTO address VALUES (%s, %s, decode(md5(%s),'hex'), false, NULL, %s)",
            (k + 1 + off, f"addr{k}", f"addr{k}", (1 + off) if k == 0 else None),
        )
    for bn in blocks:
        conn.execute(
            "INSERT INTO block VALUES (%s, decode(md5(%s),'hex'), %s, 0, %s)",
            (bn + 1 + off, f"block-{bn}", bn, bn),
        )
    for t in txs:
        conn.execute(
            "INSERT INTO tx VALUES (%s, decode(md5(%s),'hex'), %s)",
            (t + off, f"tx-{t}", t + 1 + off),
        )
        for o in (0, 1):
            conn.execute(
                "INSERT INTO tx_out VALUES (%s, %s, %s, %s, %s)",
                ((t - 1) * 2 + o + 1 + off, t + off, o, 1000 * t + o, ((t + o) % 3) + 1 + off),
            )
    conn.execute("INSERT INTO meta VALUES (%s, %s)", (1 + off, version))


@pytest.fixture(scope="session")
def _db_dsns_addr(pg_params):
    """Two empty Address-variant databases with the schema, created once."""
    n1, n2 = "dbsync_cmp_addr_v1", "dbsync_cmp_addr_v2"
    admin = psycopg.connect(_conninfo(pg_params, "postgres"), autocommit=True)
    for n in (n1, n2):
        admin.execute(f'DROP DATABASE IF EXISTS "{n}" WITH (FORCE)')
        admin.execute(f'CREATE DATABASE "{n}"')
    dsn1, dsn2 = _conninfo(pg_params, n1), _conninfo(pg_params, n2)
    for dsn in (dsn1, dsn2):
        with psycopg.connect(dsn, autocommit=True) as c:
            for ddl in _DDL_ADDR:
                c.execute(ddl)
    try:
        yield dsn1, dsn2
    finally:
        for n in (n1, n2):
            admin.execute(f'DROP DATABASE IF EXISTS "{n}" WITH (FORCE)')
        admin.close()


@pytest.fixture
def two_dbs_addr(_db_dsns_addr):
    """Reset to a clean, matching Address-variant baseline (DB2 drifted + a tip
    block ahead) and yield two open connections."""
    dsn1, dsn2 = _db_dsns_addr
    c1 = psycopg.connect(dsn1, autocommit=True)
    c2 = psycopg.connect(dsn2, autocommit=True)
    for c in (c1, c2):
        c.execute(f"TRUNCATE {_ALL_TABLES_ADDR}")
    _seed_data_addr(c1, off=0, extra_tip=False, version="v1")
    _seed_data_addr(c2, off=100, extra_tip=True, version="v2")
    try:
        yield c1, c2
    finally:
        c1.close()
        c2.close()


def test_address_variant_txout_matches_despite_id_drift(two_dbs_addr):
    # tx_out.address_id differs between the DBs (id drift), but translating it to
    # the address's natural key (raw) must still produce a MATCH.
    plan, result, _ = _compare_one(*two_dbs_addr, "tx_out")
    assert not any("address_id" in s for s in plan.skipped_cols)  # translated, not UNMAPPED
    assert result.status == "MATCH", result.note
    assert result.n1 == result.n2 > 0


def test_address_variant_corrupted_address_is_detected(two_dbs_addr):
    # The whole point of the fix: corrupting the address an in-range output points
    # at must surface as a tx_out HASH_DIFF. Before address_id was mapped, this
    # column was dropped and the corruption was invisible.
    c1, c2 = two_dbs_addr
    c2.execute("UPDATE address SET raw = decode(md5('corrupt'),'hex') WHERE id = 101")  # addr0 in DB2
    _, result, _ = _compare_one(c1, c2, "tx_out")
    assert result.status == "HASH_DIFF"
    assert result.n1 == result.n2  # only the address content changed, no row count change


def test_address_table_accumulator_subset_check(two_dbs_addr, _db_dsns_addr):
    # The address table is an accumulator with a registered natural key (raw), so
    # --verify-accumulators can subset-check it. An extra row in DB2 is a clean
    # superset (only_db2 = 1, only_db1 = 0).
    from db_sync_comparator.verify import verify_accumulator

    _, c2 = two_dbs_addr
    c2.execute("INSERT INTO address VALUES (888, 'addr-extra', decode(md5('addr-extra'),'hex'), false, NULL, NULL)")
    v = verify_accumulator(_db_dsns_addr[0], _db_dsns_addr[1], "address")
    assert v["verified"] is True
    assert v["only_db1"] == 0
    assert v["only_db2"] == 1
    assert "db1 ⊆ db2" in v["verdict"]


# --------------------------------------------------------------------------- #
# db-sync 13.7.2.1 epoch schema fixtures + tests
#
# Migration migration-2-0049 turned `epoch` from a base table into a VIEW
# (epoch_finalized UNION ALL epoch_current) and added the `epoch_finalized` base
# table plus the `epoch_current` view. These tests prove end-to-end that the tool
# (a) drops the views from introspection and keeps the base table, (b) compares
# `epoch_finalized` epoch-bounded (not as an accumulator), so an extra finalised
# epoch beyond the common cutoff is excluded rather than raising a spurious
# COUNT_DIFF, and (c) still detects a corrupted aggregate.
# --------------------------------------------------------------------------- #

_DDL_EPOCHF = [
    "CREATE TABLE block (id bigint PRIMARY KEY, hash bytea, block_no int, epoch_no int, slot_no bigint)",
    "CREATE TABLE tx (id bigint PRIMARY KEY, hash bytea, block_id bigint)",
    'CREATE TABLE tx_out (id bigint PRIMARY KEY, tx_id bigint, "index" int, value numeric, address text)',
    "CREATE TABLE pool_update (id bigint PRIMARY KEY, hash_id bigint, registered_tx_id bigint, cert_index int)",
    "CREATE TABLE gov_action_proposal (id bigint PRIMARY KEY, tx_id bigint, index int)",
    "CREATE TABLE meta (id bigint PRIMARY KEY, version text)",
    # epoch_finalized is a base table (compared); epoch and epoch_current are
    # VIEWS the tool must drop from introspection.
    "CREATE TABLE epoch_finalized (id bigint PRIMARY KEY, out_sum numeric, fees numeric, "
    "tx_count int, blk_count int, no int UNIQUE, start_time timestamp, end_time timestamp)",
    "CREATE VIEW epoch_current AS SELECT id, out_sum, fees, tx_count, blk_count, no, "
    "start_time, end_time FROM epoch_finalized WHERE false",
    "CREATE VIEW epoch AS SELECT * FROM epoch_finalized UNION ALL SELECT * FROM epoch_current",
]

# Views cannot be truncated - list base tables only.
_ALL_TABLES_EPOCHF = "block, tx, tx_out, pool_update, gov_action_proposal, meta, epoch_finalized"


def _seed_data_epochf(conn, off: int, extra_tip: bool, version: str) -> None:
    """Seed blocks spanning several epochs (epoch_no = block_no // 2) and one
    `epoch_finalized` row per finalised epoch. ``off`` drifts every id (the two
    syncs disagree on surrogate ids); ``extra_tip`` gives the ahead DB one more
    block - and thus one more finalised epoch - beyond the common cutoff."""
    max_bn = 7 if extra_tip else 6  # DB1 block_no 0..5 (epochs 0,0,1,1,2,2 -> max 2);
    #                                 DB2 adds block_no 6 -> epoch 3
    for bn in range(0, max_bn):
        conn.execute(
            "INSERT INTO block VALUES (%s, decode(md5(%s),'hex'), %s, %s, %s)",
            (bn + 1 + off, f"block-{bn}", bn, bn // 2, bn),
        )
    max_epoch = 3 if extra_tip else 2
    for e in range(0, max_epoch + 1):
        # Aggregates are a deterministic function of the epoch number, identical in
        # both DBs (only the surrogate id, which is not hashed, differs).
        conn.execute(
            "INSERT INTO epoch_finalized VALUES (%s, %s, %s, %s, %s, %s, "
            "TIMESTAMP '2020-01-01' + (%s * INTERVAL '5 days'), "
            "TIMESTAMP '2020-01-01' + ((%s + 1) * INTERVAL '5 days'))",
            (e + 1 + off, 1000 * (e + 1), 10 * (e + 1), e + 1, e + 1, e, e, e),
        )
    conn.execute("INSERT INTO meta VALUES (%s, %s)", (1 + off, version))


@pytest.fixture(scope="session")
def _db_dsns_epochf(pg_params):
    """Two empty 13.7.2.1-epoch-schema databases with the schema, created once."""
    n1, n2 = "dbsync_cmp_epochf_v1", "dbsync_cmp_epochf_v2"
    admin = psycopg.connect(_conninfo(pg_params, "postgres"), autocommit=True)
    for n in (n1, n2):
        admin.execute(f'DROP DATABASE IF EXISTS "{n}" WITH (FORCE)')
        admin.execute(f'CREATE DATABASE "{n}"')
    dsn1, dsn2 = _conninfo(pg_params, n1), _conninfo(pg_params, n2)
    for dsn in (dsn1, dsn2):
        with psycopg.connect(dsn, autocommit=True) as c:
            for ddl in _DDL_EPOCHF:
                c.execute(ddl)
    try:
        yield dsn1, dsn2
    finally:
        for n in (n1, n2):
            admin.execute(f'DROP DATABASE IF EXISTS "{n}" WITH (FORCE)')
        admin.close()


@pytest.fixture
def two_dbs_epochf(_db_dsns_epochf):
    """Reset to a clean, matching baseline (DB2 drifted + one finalised epoch
    ahead) and yield two open connections."""
    dsn1, dsn2 = _db_dsns_epochf
    c1 = psycopg.connect(dsn1, autocommit=True)
    c2 = psycopg.connect(dsn2, autocommit=True)
    for c in (c1, c2):
        c.execute(f"TRUNCATE {_ALL_TABLES_EPOCHF}")
    _seed_data_epochf(c1, off=0, extra_tip=False, version="v1")
    _seed_data_epochf(c2, off=100, extra_tip=True, version="v2")
    try:
        yield c1, c2
    finally:
        c1.close()
        c2.close()


def test_epochf_introspect_drops_views_keeps_base_table(two_dbs_epochf):
    from db_sync_comparator.schema import introspect

    c1, _ = two_dbs_epochf
    s = introspect(c1)
    assert "epoch_finalized" in s  # base table is compared
    assert "epoch" not in s  # view is dropped
    assert "epoch_current" not in s  # view is dropped


def test_epochf_matches_and_excludes_extra_finalized_epoch(two_dbs_epochf):
    # epoch_finalized must be epoch-anchored (bounded), not an accumulator: DB1 has
    # finalised epochs 0..2, DB2 has 0..3, but epoch 3 is beyond the common cutoff
    # (2), so both compare exactly the 3 shared epochs and MATCH despite id drift.
    plan, result, _ = _compare_one(*two_dbs_epochf, "epoch_finalized")
    assert plan.anchor_kind == "epoch"  # NOT accumulator
    assert result.status == "MATCH", result.note
    assert result.n1 == result.n2 == 3


def test_epochf_corrupted_out_sum_is_detected(two_dbs_epochf):
    c1, c2 = two_dbs_epochf
    c2.execute("UPDATE epoch_finalized SET out_sum = out_sum + 1 WHERE no = 1")
    _, result, _ = _compare_one(c1, c2, "epoch_finalized")
    assert result.status == "HASH_DIFF"  # out_sum is in the row hash
    assert result.n1 == result.n2  # only content changed, no row-count change


# --------------------------------------------------------------------------- #
# Governance fixtures + tests
#
# db-sync fills gov_action_proposal's lifecycle epochs (ratified / enacted /
# dropped / expired) in at a LATER epoch boundary, after the row was inserted.
# So a proposal below the block cutoff can still change once the further-synced
# database crosses another boundary. Here DB1's tip is in epoch 2 and DB2's in
# epoch 3: values up to epoch 2 are known in both and must be compared exactly;
# a value of 3 exists only because DB2 is ahead and must not count as a
# difference.
# --------------------------------------------------------------------------- #

_DDL_GOV = [
    "CREATE TABLE block (id bigint PRIMARY KEY, hash bytea, block_no int, epoch_no int, slot_no bigint)",
    "CREATE TABLE tx (id bigint PRIMARY KEY, hash bytea, block_id bigint)",
    # Spine tables compute_spine_ranges always probes; left empty here.
    'CREATE TABLE tx_out (id bigint PRIMARY KEY, tx_id bigint, "index" int, value numeric, address text)',
    "CREATE TABLE pool_update (id bigint PRIMARY KEY, hash_id bigint, registered_tx_id bigint, cert_index int)",
    "CREATE TABLE gov_action_proposal (id bigint PRIMARY KEY, tx_id bigint, index int, "
    "ratified_epoch int, enacted_epoch int, dropped_epoch int, expired_epoch int)",
    "CREATE TABLE meta (id bigint PRIMARY KEY, version text)",
    # The genesis committee and constitution have no proposal (NULL
    # gov_action_proposal_id); epoch_state records which ones were in force per epoch.
    "CREATE TABLE voting_anchor (id bigint PRIMARY KEY, block_id bigint, data_hash bytea, url text, type text)",
    "CREATE TABLE committee (id bigint PRIMARY KEY, gov_action_proposal_id bigint, "
    "quorum_numerator bigint, quorum_denominator bigint)",
    "CREATE TABLE constitution (id bigint PRIMARY KEY, gov_action_proposal_id bigint, "
    "voting_anchor_id bigint, script_hash bytea)",
    "CREATE TABLE epoch_state (id bigint PRIMARY KEY, committee_id bigint, no_confidence_id bigint, "
    "constitution_id bigint, epoch_no int)",
    "CREATE TABLE committee_hash (id bigint PRIMARY KEY, raw bytea, has_script boolean)",
    "CREATE TABLE committee_member (id bigint PRIMARY KEY, committee_id bigint, committee_hash_id bigint, "
    "expiration_epoch int)",
]

_ALL_TABLES_GOV = (
    "block, tx, tx_out, pool_update, gov_action_proposal, meta, voting_anchor, committee, constitution, "
    "epoch_state, committee_hash, committee_member"
)


def _seed_data_gov(conn, off: int, extra_tip: bool, version: str) -> None:
    """Blocks span epochs (epoch_no = block_no // 2): DB1 block_no 0..5 (tip epoch
    2), DB2 adds block_no 6 (epoch 3). One tx per block 1..5 (+6). Proposals: P0
    in tx 1 with no lifecycle epoch yet, P1 in tx 3 ratified in epoch 1 and
    enacted in epoch 2, and in the ahead DB a P2 in tx 6, beyond the cutoff."""
    max_bn = 7 if extra_tip else 6
    for bn in range(0, max_bn):
        conn.execute(
            "INSERT INTO block VALUES (%s, decode(md5(%s),'hex'), %s, %s, %s)",
            (bn + 1 + off, f"block-{bn}", bn, bn // 2, bn),
        )
    for t in range(1, max_bn):
        conn.execute("INSERT INTO tx VALUES (%s, decode(md5(%s),'hex'), %s)", (t + off, f"tx-{t}", t + 1 + off))
    conn.execute("INSERT INTO gov_action_proposal VALUES (%s, %s, 0, NULL, NULL, NULL, NULL)", (1 + off, 1 + off))
    conn.execute("INSERT INTO gov_action_proposal VALUES (%s, %s, 0, 1, 2, NULL, NULL)", (2 + off, 3 + off))
    if extra_tip:
        conn.execute("INSERT INTO gov_action_proposal VALUES (%s, %s, 0, NULL, NULL, NULL, NULL)", (3 + off, 6 + off))
    conn.execute("INSERT INTO meta VALUES (%s, %s)", (1 + off, version))
    # Genesis committee / constitution (no proposal), then P1's committee and
    # constitution. epoch_state: the genesis ones are in force in epoch 1, P1's
    # from epoch 2 on (up to each DB's tip epoch).
    conn.execute(
        "INSERT INTO voting_anchor VALUES (%s, %s, decode(md5('anchor-g'),'hex'), 'https://g', 'constitution')",
        (1 + off, 2 + off),
    )
    conn.execute(
        "INSERT INTO voting_anchor VALUES (%s, %s, decode(md5('anchor-p1'),'hex'), 'https://p1', 'constitution')",
        (2 + off, 4 + off),
    )
    conn.execute("INSERT INTO committee VALUES (%s, NULL, 2, 3)", (1 + off,))
    conn.execute("INSERT INTO committee VALUES (%s, %s, 3, 4)", (2 + off, 2 + off))
    conn.execute("INSERT INTO constitution VALUES (%s, NULL, %s, decode(md5('const-g'),'hex'))", (1 + off, 1 + off))
    conn.execute(
        "INSERT INTO constitution VALUES (%s, %s, %s, decode(md5('const-p1'),'hex'))", (2 + off, 2 + off, 2 + off)
    )
    # Committee members: cold key A sits on the genesis committee and on P1's, key B
    # only on P1's (ids drift with `off`, contents do not).
    conn.execute("INSERT INTO committee_hash VALUES (%s, decode(md5('cc-a'),'hex'), false)", (1 + off,))
    conn.execute("INSERT INTO committee_hash VALUES (%s, decode(md5('cc-b'),'hex'), true)", (2 + off,))
    conn.execute("INSERT INTO committee_member VALUES (%s, %s, %s, 10)", (1 + off, 1 + off, 1 + off))
    conn.execute("INSERT INTO committee_member VALUES (%s, %s, %s, 20)", (2 + off, 2 + off, 1 + off))
    conn.execute("INSERT INTO committee_member VALUES (%s, %s, %s, 30)", (3 + off, 2 + off, 2 + off))
    for e in range(1, (3 if extra_tip else 2) + 1):
        member = 1 if e < 2 else 2
        conn.execute("INSERT INTO epoch_state VALUES (%s, %s, NULL, %s, %s)", (e + off, member + off, member + off, e))


@pytest.fixture(scope="session")
def _db_dsns_gov(pg_params):
    """Two empty governance-fixture databases with the schema, created once."""
    n1, n2 = "dbsync_cmp_gov_v1", "dbsync_cmp_gov_v2"
    admin = psycopg.connect(_conninfo(pg_params, "postgres"), autocommit=True)
    for n in (n1, n2):
        admin.execute(f'DROP DATABASE IF EXISTS "{n}" WITH (FORCE)')
        admin.execute(f'CREATE DATABASE "{n}"')
    dsn1, dsn2 = _conninfo(pg_params, n1), _conninfo(pg_params, n2)
    for dsn in (dsn1, dsn2):
        with psycopg.connect(dsn, autocommit=True) as c:
            for ddl in _DDL_GOV:
                c.execute(ddl)
    try:
        yield dsn1, dsn2
    finally:
        for n in (n1, n2):
            admin.execute(f'DROP DATABASE IF EXISTS "{n}" WITH (FORCE)')
        admin.close()


@pytest.fixture
def two_dbs_gov(_db_dsns_gov):
    """Reset to a clean, matching governance baseline (DB2 drifted + one epoch
    ahead) and yield two open connections."""
    dsn1, dsn2 = _db_dsns_gov
    c1 = psycopg.connect(dsn1, autocommit=True)
    c2 = psycopg.connect(dsn2, autocommit=True)
    for c in (c1, c2):
        c.execute(f"TRUNCATE {_ALL_TABLES_GOV}")
    _seed_data_gov(c1, off=0, extra_tip=False, version="v1")
    _seed_data_gov(c2, off=100, extra_tip=True, version="v2")
    try:
        yield c1, c2
    finally:
        c1.close()
        c2.close()


def test_gov_proposals_match_despite_id_drift(two_dbs_gov):
    plan, result, _ = _compare_one(*two_dbs_gov, "gov_action_proposal")
    assert plan.epoch_capped_cols == ["dropped_epoch", "enacted_epoch", "expired_epoch", "ratified_epoch"]
    assert result.status == "MATCH", result.note
    assert result.n1 == result.n2 == 2  # P2 is beyond the block cutoff


def test_lifecycle_epoch_set_after_the_cutoff_epoch_is_not_a_difference(two_dbs_gov):
    # DB2 crossed into epoch 3 and dropped/expired P0 there. DB1 cannot know that
    # yet. This is the tip-gap false HASH_DIFF seen on real networks; it must
    # MATCH now - and, as a control, it is still a HASH_DIFF without the cap.
    c1, c2 = two_dbs_gov
    c2.execute("UPDATE gov_action_proposal SET dropped_epoch = 3, expired_epoch = 3 WHERE id = 101")
    _, capped, _ = _compare_one(c1, c2, "gov_action_proposal")
    assert capped.status == "MATCH", capped.note
    _, uncapped, _ = _compare_one(c1, c2, "gov_action_proposal", cap_lifecycle=False)
    assert uncapped.status == "HASH_DIFF"


def test_lifecycle_epoch_difference_at_or_below_the_cutoff_is_still_found(two_dbs_gov):
    # Epoch 2 is known to both databases, so a disagreement there is real.
    from db_sync_comparator.compare import localize

    c1, c2 = two_dbs_gov
    c2.execute("UPDATE gov_action_proposal SET ratified_epoch = 2 WHERE id = 101")
    plan, result, cutoff = _compare_one(c1, c2, "gov_action_proposal")
    assert result.status == "HASH_DIFF"
    windows = localize(plan, c1, c2, 0, cutoff, cutoff, 0, min_width=0).windows
    assert [w.split(":")[0] for w in windows] == ["block_no 1..1"]


def test_cli_run_caps_lifecycle_epochs(two_dbs_gov, _db_dsns_gov, tmp_path):
    # The whole command, not just the planner: a value only the ahead DB has
    # (dropped in epoch 3, after the cutoff epoch 2) must not fail the run.
    import json

    from db_sync_comparator.cli import main

    _, c2 = two_dbs_gov
    c2.execute("UPDATE gov_action_proposal SET dropped_epoch = 3, expired_epoch = 3 WHERE id = 101")
    report = tmp_path / "report.json"
    dsn1, dsn2 = _db_dsns_gov
    argv = ["--db1", dsn1, "--db2", dsn2, "--tables", "gov_action_proposal", "--epoch-margin", "0"]
    rc = main([*argv, "--json", str(report)])
    row = json.loads(report.read_text())["results"][0]
    assert row["table"] == "gov_action_proposal"
    assert row["status"] == "MATCH", row["note"]
    assert row["epoch_capped_cols"] == ["dropped_epoch", "enacted_epoch", "expired_epoch", "ratified_epoch"]
    assert rc == 0


def test_lifecycle_cap_does_not_change_the_hash_when_nothing_is_above_the_cutoff(two_dbs_gov):
    # On the baseline every lifecycle value is <= the cutoff epoch, so the capped
    # projection must hash byte-for-byte like the raw columns did.
    _, capped, _ = _compare_one(*two_dbs_gov, "gov_action_proposal")
    _, uncapped, _ = _compare_one(*two_dbs_gov, "gov_action_proposal", cap_lifecycle=False)
    assert (capped.n1, capped.h1, capped.n2, capped.h2) == (uncapped.n1, uncapped.h1, uncapped.n2, uncapped.h2)


@pytest.mark.parametrize("table", ["committee", "constitution"])
def test_genesis_governance_row_is_compared(two_dbs_gov, table):
    # The genesis committee / constitution have a NULL gov_action_proposal_id, so
    # the id-range bound used to drop them silently: only P1's row was compared.
    _, result, _ = _compare_one(*two_dbs_gov, table)
    assert result.status == "MATCH", result.note
    assert result.n1 == result.n2 == 2


@pytest.mark.parametrize(
    ("table", "fault"),
    [
        ("committee", "UPDATE committee SET quorum_numerator = quorum_numerator + 1 WHERE id = 101"),
        ("constitution", "UPDATE constitution SET script_hash = decode(md5('x'),'hex') WHERE id = 101"),
    ],
)
def test_genesis_governance_row_difference_is_found(two_dbs_gov, table, fault):
    # Before the fix this was a MATCH: the corrupted genesis row was never hashed.
    c1, c2 = two_dbs_gov
    c2.execute(fault)
    _, result, _ = _compare_one(c1, c2, table)
    assert result.status == "HASH_DIFF"
    assert result.n1 == result.n2 == 2


def test_genesis_row_after_the_cutoff_epoch_is_left_out_on_both_sides(two_dbs_gov):
    # The lower DB's tip is before the Conway boundary: it has no genesis committee,
    # and the ahead DB's genesis committee first appears in epoch 3, beyond the
    # cutoff (epoch 2). It must be left out of both sides - no false COUNT_DIFF.
    c1, c2 = two_dbs_gov
    c1.execute("DELETE FROM committee WHERE id = 1")
    c1.execute("UPDATE epoch_state SET committee_id = 2 WHERE committee_id = 1")
    c2.execute("UPDATE epoch_state SET committee_id = 102 WHERE epoch_no < 3")
    c2.execute("UPDATE epoch_state SET committee_id = 101 WHERE epoch_no = 3")
    _, result, _ = _compare_one(c1, c2, "committee")
    assert result.status == "MATCH", result.note
    assert result.n1 == result.n2 == 1


_GENESIS_LINE = (
    "rows with no block (gov_action_proposal_id IS NULL, the genesis row): content differs (db1 n=1, db2 n=1)"
)


@pytest.mark.parametrize("method", ["bisect", "buckets"])
def test_phase_2_reports_a_genesis_only_difference(two_dbs_gov, method):
    # A genesis row has no block, so no block window can contain it. Phase 2 checks
    # those rows as a group of their own and says so, instead of an empty list
    # that reads as "nothing found" - with either localizer.
    from db_sync_comparator.cli import _localize_mismatch

    c1, c2 = two_dbs_gov
    c2.execute("UPDATE committee SET quorum_numerator = quorum_numerator + 1 WHERE id = 101")
    plan, result, cutoff = _compare_one(c1, c2, "committee")
    assert result.status == "HASH_DIFF"
    _localize_mismatch(result, plan, c1, c2, cutoff, 2, method, 1024, 0)
    assert result.localized == []  # the window list stays windows only
    assert result.localized_no_block == _GENESIS_LINE
    assert result.localized_truncated is False
    assert result.localize_error == ""


def test_phase_2_does_not_blame_the_genesis_row_for_a_block_difference(two_dbs_gov):
    # P1's committee (created in block 3) differs; the genesis row does not.
    from db_sync_comparator.cli import _localize_mismatch

    c1, c2 = two_dbs_gov
    c2.execute("UPDATE committee SET quorum_numerator = quorum_numerator + 1 WHERE id = 102")
    plan, result, cutoff = _compare_one(c1, c2, "committee")
    assert result.status == "HASH_DIFF"
    _localize_mismatch(result, plan, c1, c2, cutoff, 2, "bisect", 1024, 0)
    assert result.localized and all(w.startswith("block_no") for w in result.localized)
    assert result.localized_no_block == ""


def test_phase_2_reports_genesis_and_block_differences_apart(two_dbs_gov):
    # Both differ: the windows list only block windows, the genesis row is reported
    # on its own - so a window count is never inflated by the genesis line.
    from db_sync_comparator.cli import _localize_mismatch

    c1, c2 = two_dbs_gov
    c2.execute("UPDATE committee SET quorum_numerator = quorum_numerator + 1 WHERE id IN (101, 102)")
    plan, result, cutoff = _compare_one(c1, c2, "committee")
    _localize_mismatch(result, plan, c1, c2, cutoff, 2, "bisect", 1024, 0)
    assert [w.split(":")[0] for w in result.localized] == ["block_no 0..5"]
    assert result.localized_no_block == _GENESIS_LINE


def test_phase_2_on_an_ordinary_table_is_unaffected(two_dbs):
    # Tables with no NULL-anchored rows must localize exactly as before: windows
    # found, no error, nothing about rows with no block.
    from db_sync_comparator.cli import _localize_mismatch

    c1, c2 = two_dbs
    c2.execute("UPDATE pool_relay SET port = -12900 WHERE id = 101")
    plan, result, cutoff = _compare_one(c1, c2, "pool_relay")
    _localize_mismatch(result, plan, c1, c2, cutoff, 0, "bisect", 1024, 0)
    assert result.localized and all(w.startswith("block_no") for w in result.localized)
    assert result.localize_error == ""
    assert result.localized_no_block == ""


def test_cli_run_counts_the_genesis_rows(two_dbs_gov, _db_dsns_gov, tmp_path):
    # The whole command, not just compare_table: the genesis rows are counted.
    import json

    from db_sync_comparator.cli import main

    report = tmp_path / "report.json"
    dsn1, dsn2 = _db_dsns_gov
    # --epoch-margin 0: the fixture's tips are in epochs 2 and 3 and the genesis rows
    # are first in force in epoch 1, inside the common range only without a margin.
    argv = ["--db1", dsn1, "--db2", dsn2, "--tables", "committee,constitution", "--epoch-margin", "0"]
    rc = main([*argv, "--json", str(report)])
    rows = {r["table"]: r for r in json.loads(report.read_text())["results"]}
    for table in ("committee", "constitution"):
        assert rows[table]["status"] == "MATCH", rows[table]["note"]
        assert rows[table]["n1"] == rows[table]["n2"] == 2
    assert rc == 0


def test_committee_member_is_subset_checked(two_dbs_gov, _db_dsns_gov):
    # committee_member's own columns are only ids, so it used to come back "not
    # verified". Its key is now built through committee (proposal or genesis) and
    # committee_hash. Same members, drifted ids -> identical key sets.
    from db_sync_comparator.verify import verify_accumulator

    v = verify_accumulator(_db_dsns_gov[0], _db_dsns_gov[1], "committee_member")
    assert v["verified"] is True, v
    assert (v["only_db1"], v["only_db2"], v["both"]) == (0, 0, 3)


def test_committee_member_extra_member_is_a_clean_superset(two_dbs_gov, _db_dsns_gov):
    # The tip-gap shape: DB2 has one more member (on P1's committee).
    from db_sync_comparator.verify import verify_accumulator

    _, c2 = two_dbs_gov
    c2.execute("INSERT INTO committee_hash VALUES (103, decode(md5('cc-c'),'hex'), false)")
    c2.execute("INSERT INTO committee_member VALUES (104, 102, 103, 40)")
    v = verify_accumulator(_db_dsns_gov[0], _db_dsns_gov[1], "committee_member")
    assert (v["only_db1"], v["only_db2"]) == (0, 1)
    assert "db1 ⊆ db2" in v["verdict"]


@pytest.mark.parametrize(
    ("fault", "changed"),
    [
        # every part of the key, one at a time
        ("UPDATE committee_member SET expiration_epoch = 11 WHERE id = 101", 1),  # expiration
        ("UPDATE committee_hash SET raw = decode(md5('z'),'hex') WHERE id = 102", 1),  # which member (raw)
        ("UPDATE committee_hash SET has_script = NOT has_script WHERE id = 102", 1),  # which member (script?)
        ("UPDATE committee SET quorum_numerator = quorum_numerator + 1 WHERE id = 101", 1),  # quorum
        ("UPDATE committee SET quorum_denominator = quorum_denominator + 1 WHERE id = 101", 1),
        ("UPDATE gov_action_proposal SET index = 1 WHERE id = 102", 2),  # P1's proposal (index)
        ("UPDATE tx SET hash = decode(md5('other'),'hex') WHERE id = 103", 2),  # P1's proposal (tx hash)
    ],
)
def test_committee_member_changed_member_is_not_a_subset(two_dbs_gov, _db_dsns_gov, fault, changed):
    # A member whose content differs is on one side only in each database ->
    # NEITHER is a subset -> investigate. Each fault touches one part of the key
    # (P1's committee has 2 members, so a change to its proposal moves both).
    from db_sync_comparator.verify import verify_accumulator

    _, c2 = two_dbs_gov
    c2.execute(fault)
    v = verify_accumulator(_db_dsns_gov[0], _db_dsns_gov[1], "committee_member")
    assert (v["only_db1"], v["only_db2"]) == (changed, changed)
    assert v["verdict"].startswith("NEITHER")
