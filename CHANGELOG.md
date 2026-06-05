# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **`new_committee` anchor.** It was registered as epoch-anchored, but the table
  has no `epoch_no` column (its key is `gov_action_proposal_id`), which produced
  a per-table `ERROR` on real databases. Now anchored via `gov_action_proposal_id`
  like `committee`. Added a regression test.

### Changed

- **One-sided-zero tables are flagged, not localized.** When a table has rows in
  one database and **0** in the other, the result now says *"likely disabled in
  config (insert_options) for that version, not a data difference"* and Phase 2
  skips bisecting it. (Real case: `pool_stat` 0 vs 1.13M — the older build ran
  with the `pool_stat` insert option off.)
- **`--block-margin N`** option: pull the block cutoff back below the lower tip
  by N blocks to stay out of the volatile near-tip rollback zone (mainnet
  `k`≈2160). Complements the existing `--epoch-margin`. (Note: this only bounds
  chain-anchored tables; accumulators have no anchor, so use `--verify-accumulators`
  for those.)

### Added

- **`--verify-accumulators`** (opt-in) — for accumulator `COUNT_DIFF`s, stream
  both natural-key sets (server-side, index-ordered, memory-bounded) and
  merge-compare them, reporting `only_db1` / `only_db2`. A clean subset means the
  delta is purely tip-gap extra rows; if neither side is a subset it's a real
  difference. Read-only and off by default. New module `db_sync_comparator/verify.py`
  + `db.stream_keys`; unit tests for the merge + key SQL, and an end-to-end
  fixture test. Validated on preview LSM-vs-standard
  (`benchmarks/INVESTIGATION-preview-lsm-vs-standard.md`).
- **End-to-end fixture tests against a real PostgreSQL** (`tests/test_fixture_e2e.py`,
  marker `fixture`). Two miniature "db-sync-shaped" databases are seeded with
  identical chain content but **drifted surrogate ids** and a **tip gap**, then
  individual tests inject one fault each — a corrupted value, a dropped row, the
  real `pool_relay.port` signed-16-bit overflow, an extra accumulator row — and
  assert the tool returns `MATCH` / `HASH_DIFF` / `COUNT_DIFF` and localizes
  correctly. PostgreSQL (not SQLite) because the generated SQL is Postgres-specific;
  provided by pytest-postgresql locally or a service container in CI
  (`DBSYNC_COMPARE_PG_EXTERNAL`). Run with `make test-db`. The default `make test`
  stays DB-free.
- **README operational guide + hardware/resource notes.** How to run a full
  mainnet comparison in practice: detached from your shell/session, progress
  streamed to files (`-u` + `run.log`, `--json`), no short `--statement-timeout`,
  and `--workers`/`--work-mem` tuning. Plus a hardware section: the run is
  read-only on the data but FK-translation hash joins **spill hundreds of GB of
  PostgreSQL temp files** (measured ~478 GB / ~255 GB cumulative on the two
  mainnet DBs), which is why free disk space fluctuates; client RAM is negligible
  (~10–40 MB). Expanded resource detail in `docs/07-performance-and-scaling.md`.

## [0.1.0] — 2026-06-05

First release: a content-equivalence comparator for two cardano-db-sync
PostgreSQL databases, designed to be a pre-release data-integrity gate on
mainnet-sized (500 GB+) databases. Validated against real mainnet databases for
db-sync 13.6.0.5 vs 13.7.1.0, where it caught a genuine `pool_relay.port`
regression.

### Added

#### The comparator (`db_sync_comparator/`)

- **Compares two databases by chain *meaning*, not by storage.** The surrogate
  `id` of every row, and the foreign keys that reference it, drift between two
  independent syncs (rollbacks burn sequence values). The tool drops the `id`
  and **translates every foreign key to the version-stable natural key** of the
  row it points at (block hash, tx hash, (policy, asset name), …), resolving FK
  chains recursively. db-sync declares no FK constraints in PostgreSQL, so the
  logical foreign keys — including irregular names like `drep_voter`,
  `return_address`, `param_proposal` — are mapped by hand in
  `registries.py`; an unmapped `*_id` is excluded and flagged, never hashed raw.
- **Order-independent, duplicate-safe set hash, computed server-side.** Each row
  is MD5'd over its normalized columns; the digest is split into two 60-bit
  halves and summed as `numeric`. Two tables hash equal iff they are the same
  multiset of rows — no sort, no client-side memory, only a count and two numbers
  cross the wire. (Same family as Percona `pt-table-checksum` / Datafold
  `data-diff`.)
- **Common-tip bounding via indexed id-range windows.** The two databases are
  usually at different tips, so the comparison is bounded to the lower tip. The
  bound is applied per table as an indexed `BETWEEN` on a precomputed
  surrogate-id range (derived by walking `block → tx → tx_out` with index seeks),
  rather than joining each table up to `block` — which avoids whole-table scans.
- **Tiered effort on the giant tables.** `ma_tx_out` (~1.1B rows),
  `epoch_stake`/`reward` (~450M), `tx_out`/`tx_in` (~340M) get row count + a
  numeric sum/min/max proof + a shallow normalized hash by default; `--full`
  forces the exhaustive per-column hash everywhere.
- **Merkle-style localization.** On a mismatch, the chain range is binary-searched
  so the difference is reported as a narrow `block_no`/`epoch_no` window for
  follow-up, instead of just "table X differs".
- **Schema-drift aware.** Both schemas are introspected and only the shared
  columns are compared; columns/tables present in only one database are reported,
  not hashed.
- **Honest defaults.** ~16 network-fetched / per-instance tables (off-chain
  metadata, `meta`, `schema_version`, …) are excluded with a stated reason;
  "accumulator" definition tables with no clean chain anchor are compared whole
  and a count delta is treated as informational (it usually reflects the tip
  gap).
- **CLI** (`db-sync-compare` / `python -m db_sync_comparator`): `--plan` (print
  the generated SQL, touch no data), `--block-range` (fast windowed validation),
  full cutoff mode, `--full`, `--tables`, `--workers`, `--work-mem`,
  `--statement-timeout`, `--json` report, and CI-friendly exit codes
  (0 = equivalent, 1 = discrepancy, 2 = operational error).

#### Tests, tooling, docs

- **pytest suite** covering the pure logic: FK resolution and registry
  invariants, SQL generation and quoting, natural-key expansion with depth
  limiting, plan classification and tiering, the jsonb value-column guard, and
  argument parsing — plus opt-in end-to-end tests gated behind
  `DBSYNC_COMPARE_TEST_DSN1/2`.
- **Quality gate**: ruff (lint + format), mypy, pytest, wired into a `Makefile`,
  `.pre-commit-config.yaml`, and a GitHub Actions matrix (Python 3.10–3.12).
- **Documentation** (`docs/`): a from-zero explanation — primers on indexes,
  hashing, Cardano/db-sync, and surrogate-id drift — followed by the design, a
  per-table reference, performance notes, and a worked case study of the
  `pool_relay.port` finding.

[Unreleased]: https://github.com/ArturWieczorek/cardano-db-sync-compare/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/ArturWieczorek/cardano-db-sync-compare/releases/tag/v0.1.0
