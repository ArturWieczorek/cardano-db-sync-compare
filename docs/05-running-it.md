# 05 - Running it

> **What's in here:** install, connection setup, every command-line option with
> examples, and how to read the output.
>
> **Prerequisites:** [how it works](03-how-it-works.md).

## Install

Requires Python 3.10+. The only runtime dependency is psycopg 3.

```bash
# with uv (recommended):
make install                 # creates .venv and installs everything

# or with plain pip:
python3 -m venv .venv
. .venv/bin/activate
pip install -e .             # installs psycopg + the db-sync-compare command
```

This puts a `db-sync-compare` command on your PATH. You can equivalently run it
as `python -m db_sync_comparator`. All examples below use `db-sync-compare`.

## Connecting to the two databases

Each database is given as a libpq **connection string** (conninfo). For a local
db-sync database over the Unix socket:

```
"dbname=mainnet_v1 host=/var/run/postgresql"
```

If the database needs a password, point `PGPASSFILE` at a pgpass file (the same
mechanism db-sync uses) with `--pgpass`, or include the password in the conninfo.
The tool opens several connections per database (one per parallel worker), all
read-only.

## The modes

### `--plan` - see the SQL, touch no data

Prints, for every table: its classification, how it's bounded to the chain
window, which columns were dropped or translated, and the exact fingerprint SQL.
**Always start here** to audit what the tool will do.

```bash
db-sync-compare --db1 "dbname=v1 host=/var/run/postgresql" \
                         --db2 "dbname=v2 host=/var/run/postgresql" --plan
```

### `--block-range LO:HI` - fast spot-check

Compare only a window of the chain (by block height). Great for a quick
correctness/performance check before committing to a full run. Epoch-based and
"accumulator" tables are skipped in this mode (they aren't tied to a block
window).

```bash
db-sync-compare --db1 ... --db2 ... --block-range 8000000:8010000
```

### Default (no range) - the full comparison

Compares everything up to the **common boundary** (the lower of the two tips,
minus a small epoch margin). This is the real release gate. Add `--json` to save a
machine-readable report.

```bash
db-sync-compare --db1 ... --db2 ... --json report.json
```

## All options

| Option | Default | What it does |
|--------|---------|--------------|
| `--db1`, `--db2` | (required) | connection strings for the two databases |
| `--pgpass PATH` | - | set `PGPASSFILE` for password lookup |
| `--plan` | off | print the plan + SQL and exit; no hashing |
| `--block-range LO:HI` | - | compare only this block-height window. **Skips Phase 2 localization** (the window is already narrow - see note below); to localize, use cutoff mode with `--cutoff-block` |
| `--tables a,b,c` | all | compare only these tables |
| `--full` | off | exhaustive every-column fingerprint on the giant tables too |
| `--giant-fk-depth N` | 1 | how deep to translate foreign keys on giants in tiered mode |
| `--cutoff-block N` | lower tip | override the common boundary block height |
| `--block-margin N` | 0 | pull the block cutoff back N blocks below the lower tip, to stay out of the volatile near-tip rollback zone (mainnet `k`≈2160) |
| `--epoch-margin N` | 2 | epochs to hold back from the tip (avoid the in-progress epoch) |
| `--workers N` | 4 | tables compared in parallel (each uses 2 connections) |
| `--work-mem SIZE` | `256MB` | per-session `work_mem` for the translation hash joins |
| `--statement-timeout MS` | 0 (none) | abort any single query after this many ms |
| `--no-localize` | off | skip the Phase-2 zoom-in on mismatches |
| `--localize` | `bisect` | localization algorithm: `bisect` (re-hash halves) or `buckets` (one-pass per-window hash - much faster on giant tables; see [doc 07](07-performance-and-scaling.md#localizing-a-mismatch---localize-bisect-vs---localize-buckets)) |
| `--localize-buckets N` | 1024 | number of chain windows for `--localize buckets` (capped 5000) |
| `--verify-accumulators` | off | for accumulator `COUNT_DIFF`s, subset-check the two key sets to confirm a count delta is purely tip-gap extra rows (see [doc 06](06-how-each-table-is-compared.md#how-to-verify-an-accumulator-count_diff-tip-gap-or-real)) |
| `--json PATH` | - | write a structured JSON report |

## Reading the output

The header names **both databases** so the log is self-identifying - you can tell
which two databases (and which db-sync versions) a run compared without guessing:

```
DB1: mainnet-13.6.0.5-restored-on-13.7.0.4  (tip block 13313031, epoch 626)
DB2: mainnet-dbsync-13.7.1.0-node-11.0.1    (tip block 13488662, epoch 634)
```

The label is the resolved database name (with `@host` appended only for a real
TCP host, never for a local Unix socket); it is read from the live connection, so
a password in the conninfo can never leak into the output. The same names are
written to the `--json` report as top-level `db1` / `db2` fields.

Phase 1 prints one line per table:

```
  OK tx_out            MATCH       n=605637/605637    13.2s
  !! pool_relay        HASH_DIFF   n=72514/72514       0.8s  row counts match but content hash differs
  !! pool_hash         COUNT_DIFF  n=6123/6136         0.0s  accumulator table; count delta usually reflects the tip gap
```

The statuses:

| Status | Meaning |
|--------|---------|
| `MATCH` | same row count **and** same content fingerprint → identical chain data |
| `COUNT_DIFF` | the row counts differ. For an **accumulator** table (see [doc 06](06-how-each-table-is-compared.md)) this is usually just the tip gap and is **informational** - confirm with `--verify-accumulators`; for any other table it's a real discrepancy. A one-sided `0 vs N` means the table was likely disabled in that version's config |
| `HASH_DIFF` | same row count but the content fingerprint differs → real difference in the data |
| `VALUE_DIFF` | the numeric sum/min/max of a giant table's value column differs |
| `ERROR` | a query failed for this table (reported, doesn't stop the run) |
| excluded | a non-chain table the tool doesn't compare (shown in `--plan`) |

For any real mismatch, **Phase 2** then prints the narrow block/epoch windows where
it differs:

```
Phase 2: localizing mismatches ...
  pool_relay: block_no 4490224..4491848: content differs (db1 n=681, db2 n=681)
```

> **`--block-range` skips Phase 2.** Localization exists to *narrow a huge chain
> range down to a small window*. With `--block-range LO:HI` you have already handed
> the tool a small window, so there is nothing left to narrow - Phase 2 is skipped
> and `--localize` / `--localize-buckets` have **no effect**. (The skip is by
> design - see `cli.py`, where Phase 2 runs only when no block range was given.) If
> you want to *see* or *benchmark* localization, run in **cutoff mode** instead,
> optionally bounding the work with `--cutoff-block N` (compares blocks `0..N`, and
> still localizes). This is distinct from the unindexed-column note in
> [doc 09](09-extending-and-limitations.md), which is about *scan cost*, not about
> whether Phase 2 runs.

The final summary counts matches, discrepancies, informational accumulator
count-deltas, errors, and excluded tables.

## Exit codes (for CI)

| Code | Meaning |
|------|---------|
| `0` | content-equivalent over the compared range |
| `1` | one or more real discrepancies (or per-table errors) |
| `2` | could not run (e.g. connection failure) |

## A good workflow

1. `--plan` - sanity-check the plan.
2. `--block-range` on a historical window - confirm it runs clean and fast.
3. Full run with `--json` - the actual gate. Investigate any non-accumulator
   `COUNT_DIFF` / `HASH_DIFF` / `VALUE_DIFF` using the Phase-2 windows (see the
   [case study](08-case-study-pool-relay-port.md)).

**Next:** [How each table is compared →](06-how-each-table-is-compared.md)
