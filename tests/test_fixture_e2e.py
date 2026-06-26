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


def _compare_one(c1, c2, table: str, full: bool = False):
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
    plan = plan_table(table, s1[table], s2[table], common, full, 1)
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

    bucket_wins = localize_buckets(plan, c1, c2, cutoff, 0, n_buckets=1024)
    assert bucket_wins, "buckets localizer returned no window"
    assert any(_covers(w, 3) for w in bucket_wins), bucket_wins

    # parity: the bisection localizer also brackets block 3
    bisect_wins = localize(plan, c1, c2, 0, cutoff, cutoff, 0)
    assert any(_covers(w, 3) for w in bisect_wins), bisect_wins


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

    windows = localize(plan, c1, c2, 0, cutoff, cutoff, 0)
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
