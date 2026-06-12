# AGENTS.md

Guidance for AI agents and human contributors working in this repo.

## Writing style: ASCII hyphen only (no fancy dashes)

Use only the **regular ASCII hyphen-minus** (`-`, U+002D) for every dash, range,
or minus. Do **not** use any of these anywhere in this repo (docs, code, comments,
docstrings, help strings, commit messages, CHANGELOG, generated report prose):

- em dash, Unicode **U+2014** (the long dash)
- en dash, Unicode **U+2013** (often used in numeric ranges)
- minus sign, Unicode **U+2212** (often used for negative numbers)

For a parenthetical, use a spaced hyphen (` - `), a comma, a colon, or parentheses.
For a range, use a plain hyphen (`0045-0048`). For a negative number, use a plain
hyphen (`-5535`). The only exception is if a tool genuinely enforces or emits one
(no current tool does). This applies to chat replies to the repo owner too. Other
non-ASCII used as data/notation (for example the subset symbol `⊆`, `≤`, arrows)
is unaffected. To check, this should print nothing:

```bash
grep -rnP '[\x{2012}\x{2013}\x{2014}\x{2015}\x{2212}]' --exclude-dir=.git --exclude-dir=.venv .
```

## What this is

A content-equivalence comparator for two cardano-db-sync PostgreSQL databases.
Read [README.md](README.md) and, for the reasoning, the [docs/](docs/00-start-here.md)
- especially `docs/05-running-it.md` (CLI) and `docs/06-how-each-table-is-compared.md`
(the schema registries).

## Definition of done - every change follows the full cycle

This is not optional and not proportional to change size. Every feature, fix, or
behaviour change goes through **all** of these. The detailed mechanics are in the
sections below; this is the contract.

0. **Plan first.** Use **plan mode** - design before editing. Check the **current
   db-sync schema and authoritative "truth" sources at the matching version tag**
   (see "Source of truth" below); don't trust memory or `master`. Follow the
   repo's **architecture rules and design principles** (inward dependencies, pure
   SQL/logic modules, single responsibility, the registries as the one schema
   mirror) - see "Architecture rules" and "Invariants".
1. **Establish a green baseline.** Run the **existing** tests *before* you change
   anything (`make check`, plus `make test-db` if you'll touch the engine), so a
   later failure is unambiguously yours.
2. **Write the test first (TDD).** Add a **failing** test that captures the new
   behaviour, then write the code to make it pass. Pure logic → unit test; anything
   touching the comparison engine, generated SQL, or registries → also a `fixture`
   end-to-end test against real PostgreSQL. **No behaviour ships untested.**
3. **Update all the docs.** Every user-visible change updates the relevant
   `docs/` page(s) **and** `README.md` **and** `CHANGELOG.md` (`[Unreleased]`).
   Docs are written in **simple language with the why, the what, and worked
   examples** - that is the house style; match it.
4. **Run the formatters and the full gate.** `make check` (ruff format + ruff
   check + mypy + pytest) must be **green**. See "Before you commit".
5. **Decide on CI.** Ask whether `.github/workflows/ci.yml` needs new coverage -
   a new test marker, the DB service for `fixture` tests, a new Python version,
   etc. If the change introduces a category of test the matrix doesn't run, **add
   it to CI**, don't rely on it being run by hand.

Two standing rules that override convenience:

- **New capability is opt-in and defaults to today's behaviour** (a flag, off by
  default). Never silently change what an existing invocation does.
- **Never add load to a running comparison.** A mainnet run takes ~9-10h; if one is
  in flight, defer heavy work (extra scans, new big queries) until it finishes.

## Before you commit

Run the same checks CI runs:

```bash
make check     # ruff check + ruff format --check + mypy + pytest
```

`pre-commit install` wires ruff + mypy to run automatically. Keep changes
formatted and typed; CI is the matrix in `.github/workflows/ci.yml`.

`make test` is DB-free (pure-logic unit tests). `make test-db` (= `pytest -m
fixture`) runs the end-to-end tests against a real PostgreSQL - pytest-postgresql
locally, or set `DBSYNC_COMPARE_PG_EXTERNAL=1` + `PG*` env to use an existing
server. If you change the comparison engine, add or extend a `fixture` test.

## Architecture rules

- Dependencies point **inward** toward `model.py` and `registries.py`. Don't add
  an import that creates a cycle. Only `db.py` imports psycopg, so the pure-logic
  modules (`sql`, `planning`, `registries`, `model`) stay importable - and
  unit-testable - without a database driver.
- SQL is generated as strings in `sql.py`; keep it pure (no DB access there) so
  it can be tested by asserting on the generated SQL.

## Invariants that must hold

- **Never hash a raw surrogate `id` or a raw `*_id` foreign key.** Ids drift
  between databases. `id` is dropped; foreign keys are translated to natural keys.
  An unmapped FK must be **excluded and flagged**, not hashed.
- **`db-sync-compare --plan` must report zero `UNMAPPED` columns** on a current
  schema. If you see one, add the column to `GLOBAL_FK`/`FK_MAP` in
  `registries.py` (and the target's natural key to `NATURAL_KEYS`).
- Every `("fk", col, target)` in `NATURAL_KEYS` and every FK-map target must have
  a `NATURAL_KEYS` entry - there are tests asserting this (`test_registries.py`).

## Source of truth for the db-sync schema

The registries in `db_sync_comparator/registries.py` mirror the cardano-db-sync
schema. They are **not** authoritative - db-sync is. Before changing them (or
when a comparison looks wrong), check the upstream schema, **at the git tag that
matches the db-sync version you're comparing** (`master` drifts ahead of releases):

- Schema reference (human-readable): <https://github.com/IntersectMBO/cardano-db-sync/blob/master/doc/schema.md>
- Schema source (authoritative - the Haskell definitions): <https://github.com/IntersectMBO/cardano-db-sync/tree/master/cardano-db/src/Cardano/Db/Schema>
- On-the-wire DDL (migrations): <https://github.com/IntersectMBO/cardano-db-sync/tree/master/cardano-db/test/schema>
- All db-sync docs: <https://github.com/IntersectMBO/cardano-db-sync/tree/master/doc>

## When the db-sync schema changes

Cross-check the upstream sources above, update the registries in
`db_sync_comparator/registries.py` (see `docs/09-extending-and-limitations.md`),
and **add a test** for the new behaviour. Then run `--plan` against two real
databases to confirm classification and zero unmapped columns.

## Running long comparisons (operational playbook)

A full mainnet comparison takes **~9-10 hours** (measured 9h42m). It **must
survive the shell, the SSH session, and the agent session dying.** Treat every
real run as a detached, file-logged job - never as a foreground command you watch.

**Always run detached and log to files:**

```bash
nohup .venv/bin/python -u -m db_sync_comparator \
    --db1 "dbname=<db1> host=/var/run/postgresql" \
    --db2 "dbname=<db2> host=/var/run/postgresql" \
    --workers 6 --statement-timeout 0 \
    --json benchmarks/<run>.json \
    > benchmarks/<run>.log 2>&1 &
```

- `nohup` (or `setsid`) so the process reparents to init and **outlives the
  session**. A bare `&`, or a harness-managed background shell, is **killed when
  the agent session exits or crashes** - you lose a multi-hour run. Learned the
  hard way: detach for real.
- `-u` - **unbuffered** Python output, or `run.log` sits empty for hours and you
  can't see progress (or where it died).
- `--json <path>` - write the structured report too; it's the durable artifact if
  the terminal log is lost.
- **Resume by reading the files**, not by expecting an in-session notification. A
  truly detached process cannot ping you back, and any "waiter" you start dies with
  the session. On reconnect: `tail` the `.log`, check the `.json`, check
  `pg_stat_activity`.
- **If you do add a completion "waiter"** (a harness-tracked loop that watches for a
  `DONE` marker so you get a ping): make its **last command always exit 0** on a
  clean finish. A trailing `grep -c SOMETHING` returns **exit 1 when the count is 0**,
  so a *successful* run reports the waiter as "failed" - a false alarm that looks
  like the comparison broke. Use `grep -q … || true`, or end with `echo done`.
  Learned the hard way. The waiter is best-effort convenience only; the `nohup` job
  + log/JSON are the source of truth regardless of whether the waiter survives.
- **For unattended runs (e.g. overnight), add a disk watchdog.** The hash joins spill
  large PostgreSQL temp files onto the **same filesystem as the data**, so a runaway
  could fill the disk and destabilise *all* databases while no one is watching. Wrap
  the run in a loop that aborts it if free space drops below a hard floor:

  ```bash
  FLOOR_KB=$((120*1024*1024))            # 120 GB
  while kill -0 "$CMP_PID" 2>/dev/null; do
    [ "$(df -P /mnt/postgres_data | awk 'NR==2{print $4}')" -lt "$FLOOR_KB" ] && {
        kill "$CMP_PID"; psql -d postgres -c \
          "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname LIKE '%dbsync%' AND state='active';"; break; }
    sleep 60
  done
  ```

  Also **lower `--workers`** (e.g. 3 instead of 6) when free disk is tight - fewer
  giant tables spill concurrently, so peak temp is lower. Check `df` on the data
  filesystem **before** launching; the prior run that completed had ~1.3 TB free,
  later runs may have much less.

**When you write up a run** (a `benchmarks/INVESTIGATION-*.md`), record the exact
identity of what was compared, or the report is not reproducible: the **full
database names** (e.g. `mainnet-dbsync-13.7.1.0-node-11.0.1` vs
`lsm-mainnet-dbsync-13.7.1.0-node-11.0.1`), the db-sync **and** cardano-node
versions, each tip, the common cutoff, and the **exact command** (flags included).
"mainnet 13.7.1.0 LSM vs standard" alone does not say *which* databases. Learned the
hard way - every investigation report now carries a `database name` row. The tool
itself also stamps **both database names** into the run header (`DB1: <name>` /
`DB2: <name>`) and into the `--json` report (`db1` / `db2` fields), so the raw
`.log` / `.json` artifacts are self-identifying too - don't rely on a wrapper
script to add them.
- `--statement-timeout 0` (no timeout) for full runs - single giant-table queries
  legitimately run **>1 hour**. A short timeout will kill them mid-scan.
- `--workers N`: each worker uses **2 connections**; 6 was comfortable on mainnet.
- `--work-mem` (default 256MB) feeds the FK-translation hash joins.
- `--block-range LO:HI` (note: **colon**, e.g. `5500000:7000000`) bounds a run to a
  block window for fast validation - skips epoch tables.

**If you abort a run:** orphaned PostgreSQL backends keep scanning and **saturate
I/O**, slowing everything (including a fresh run) to a crawl. Find and kill them:

```sql
SELECT pid, state, now()-query_start AS runtime, left(query,60)
FROM pg_stat_activity WHERE datname LIKE '%dbsync%' AND state='active';
SELECT pg_terminate_backend(<pid>);   -- for the stragglers
```

## Resource reality (so you don't fill a disk or chase the wrong knob)

- **Read-only on the data, but it spills hundreds of GB of PostgreSQL temp files.**
  The FK-translation hash joins on giant tables spilled **~478 GB / ~255 GB**
  cumulative across the two mainnet DBs. Free disk fluctuates wildly during a run -
  that's normal, but ensure **hundreds of GB free** for temp.
- **Swap is not the lever.** Postgres does not page `work_mem` to OS swap; oversized
  hashes spill to **temp files on disk**. Tune `work_mem` / free disk, not swap.
- **The client is trivial.** Only a count + two numerics cross the wire per table;
  client RAM is ~10-40 MB and CPU is ~18s over a ~5h run. **The database is the
  bottleneck** - a faster client language (Rust, etc.) would not help. Don't
  optimize the Python.
- `--localize buckets` only changes **Phase 2** (localizing a *mismatched* table).
  It cannot speed up Phase 1 (the bulk of the wall-clock). A clean all-`MATCH` run
  (e.g. **LSM vs standard, same version - the relational data is byte-identical**)
  **never enters Phase 2**, so it won't exercise buckets at all.

## Hard-won correctness lessons (the nitty-gritty)

- **db-sync declares no FK constraints in PostgreSQL.** Foreign keys are logical,
  by column name, and some are **irregular** (`drep_voter`, `return_address`,
  `param_proposal`). Introspecting `information_schema` for FKs returns nothing -
  the registries in `registries.py` are mapped **by hand**, and that's why.
- **Ids and FKs drift** between two independent syncs because **rollbacks burn
  sequence values**. Never compare ids; compare by version-stable natural keys
  (block hash, tx hash, `(policy, asset_name)`, …), resolving FK chains recursively.
- **`min`/`max` with a non-PK filter is catastrophic** (whole-table scan, 3+ min).
  Use `ORDER BY <col> ... LIMIT 1` index seeks instead (3 min → 1.8 s). This is the
  basis of the id-range window bounding.
- **Set hash:** `md5(ROW(...)::text)` split into two 60-bit halves summed as
  `numeric` - order-independent and duplicate-safe, so no `ORDER BY` and no
  client-side memory.
- **Don't sum non-numeric columns.** `jsonb` (e.g. `datum.value`) can't be summed as
  numeric - the value-column proof is restricted to `NUMERIC_TYPES`.
- **Anchoring requires a column that exists.** `new_committee` has no `epoch_no`;
  anchoring it by epoch produced an `ERROR` (it was *never actually compared*). It
  must be anchored by `gov_action_proposal_id`. **A wrong anchor can silently skip a
  table** - when you fix an anchor, **re-run to verify**, because the prior verdict
  is meaningless.
- **One-sided-zero (0 vs N rows)** is almost always a **disabled `insert_option`**
  (config/feature difference, e.g. `pool_stat`), not corruption - flag it, don't
  bisect it.
- **Accumulator tables** (no clean chain anchor: `multi_asset`, `stake_address`,
  `pool_hash`, …) are compared whole; a count delta is usually just the **tip gap**
  (the DBs are at different tips). Use `--verify-accumulators` to confirm one side
  is a clean subset of the other before calling it a difference.
- **Id order only holds for settled history.** Near the tip (rollback zone) it
  wobbles, so stay a safe margin below the lower tip (`--block-margin`,
  `--epoch-margin`; mainnet `k`≈2160).

## Verifying a finding (don't stop at the data)

When the tool flags a real difference, **confirm the root cause in the db-sync
source at the matching version tag**, not just from the rows. Example: the
`pool_relay.port` regression was confirmed at
`cardano-db/src/Cardano/Db/Schema/Core/Pool.hs:224` - `fromIntegral >$< E.int2`
encodes a `Word16` through a signed `Int16`, wrapping ports >32767 to `port-65536`.
And **cross-check across networks** (preprod/preview, not just mainnet) to prove a
regression isn't network-specific. The full worked example is
`docs/08-case-study-pool-relay-port.md` and the upstream issue is
[#2135](https://github.com/IntersectMBO/cardano-db-sync/issues/2135).

## Authorship

Commits are authored by the repo owner. Do not add AI co-author trailers or
"generated by" lines to commits, code, or docs.
