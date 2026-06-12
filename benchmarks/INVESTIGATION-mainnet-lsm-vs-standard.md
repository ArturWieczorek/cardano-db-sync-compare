# Investigation report - mainnet LSM backend vs standard backend

**Date:** 2026-06-06 · **Tool:** `db-sync-compare` (tiered, `--workers 3`, `--block-margin 2160`, `--localize buckets`, `--verify-accumulators`)

| | DB1 | DB2 |
|---|---|---|
| database name | `mainnet-dbsync-13.7.1.0-node-11.0.1` | `lsm-mainnet-dbsync-13.7.1.0-node-11.0.1` |
| db-sync version | 13.7.1.0 (**standard** in-memory backend) | 13.7.1.0 (**LSM** on-disk UTxO backend) |
| cardano-node | 11.0.1 | 11.0.1 |
| network | mainnet | mainnet |
| tip | block 13,488,662 / epoch 634 | block 13,501,149 / epoch 634 |
| common cutoff | block ≤ 13,486,502, epoch ≤ 632 (lower tip - 2160-block margin) | |

Both DBs reside on the local PostgreSQL (`host=/var/run/postgresql`). Exact invocation:

```bash
python -m db_sync_comparator \
  --db1 "dbname=mainnet-dbsync-13.7.1.0-node-11.0.1 host=/var/run/postgresql" \
  --db2 "dbname=lsm-mainnet-dbsync-13.7.1.0-node-11.0.1 host=/var/run/postgresql" \
  --workers 3 --block-margin 2160 --localize buckets --verify-accumulators \
  --statement-timeout 0 --json benchmarks/lsm-vs-standard-mainnet-2026-06-06.json
```

**Why this comparison:** same db-sync version on the same chain, differing only in
storage backend - does the LSM on-disk UTxO backend produce identical relational
data on **mainnet** (the companion to the clean preview and preprod LSM runs)? This
is the confidence check before deleting the ~503 GB LSM database to reclaim disk.

## Result - clean pass

```
SUMMARY: 56 match, 0 discrepancies, 3 accumulator count-deltas (informational), 0 errors, 16 excluded
comparator rc=0 · wall-clock 3h09m01s (started 23:43:21Z, done 02:52:22Z)
```

- **Every chain-anchored table MATCHES**, including the billion-row giants:
  `ma_tx_out` **1,129,532,871** ✓ · `tx_out` 348,551,532 ✓ · `tx_in` 337,379,612 ✓ ·
  `epoch_stake` 450,952,563 ✓ · `reward` 424,871,147 ✓ · `tx_metadata` 136,867,501 ✓ ·
  `withdrawal` 11,763,985 ✓.
- **The only three flagged are accumulator tip-gap deltas** (informational), the LSM
  DB (DB2) being ~12,487 blocks ahead of the standard DB:

| accumulator | only in DB1 (std) | only in DB2 (LSM) | reading |
|---|---|---|---|
| `drep_hash` | 0 | 3 | db1 ⊆ db2 |
| `multi_asset` | 0 | 1,282 | db1 ⊆ db2 |
| `stake_address` | 0 | 2,400 | db1 ⊆ db2 |

## Proof the deltas are pure tip-gap (auto, via `--verify-accumulators`)

The flag ran the subset check **inline** - no manual step:

```
drep_hash:     only_db1=0 only_db2=3    → db1 ⊆ db2 - db2 is a clean superset; extra rows only in db2 (tip-gap-consistent; db2 ahead)
multi_asset:   only_db1=0 only_db2=1282 → db1 ⊆ db2 - db2 is a clean superset; extra rows only in db2 (tip-gap-consistent; db2 ahead)
stake_address: only_db1=0 only_db2=2400 → db1 ⊆ db2 - db2 is a clean superset; extra rows only in db2 (tip-gap-consistent; db2 ahead)
```

`only_db1 = 0` (the behind DB, standard, has nothing the ahead DB lacks) is
decisive: the standard DB's accumulator rows are a clean subset of the LSM DB's, so
the +3 / +1,282 / +2,400 are exactly the rows the LSM DB picked up by syncing
~12.5k blocks further. No content difference. See
[docs/06 - verifying a count-delta](../docs/06-how-each-table-is-compared.md#how-to-verify-an-accumulator-count_diff-tip-gap-or-real).

## Slowest tables

| table | status | rows | seconds |
|---|---|---|---|
| `tx_out` | MATCH | 348,551,532 | 4,868.5 |
| `ma_tx_out` | MATCH | 1,129,532,871 | 4,055.8 |
| `tx_in` | MATCH | 337,379,612 | 4,050.8 |
| `collateral_tx_in` | MATCH | 29,886,267 | 2,237.2 |
| `epoch_stake` | MATCH | 450,952,563 | 2,156.6 |
| `reward` | MATCH | 424,871,147 | 2,030.8 |
| `tx_metadata` | MATCH | 136,867,501 | 1,246.9 |
| `datum` | MATCH | 34,560,163 | 1,167.7 |

(`--localize buckets` was passed but never used: 0 discrepancies ⇒ Phase 2 never
runs. A clean comparison cannot exercise localization, as expected.)

## Operational notes

- Run detached (`nohup`) with a **disk watchdog** (auto-abort if free space on
  `/mnt/postgres_data` fell below 120 GB). It **never fired**; free disk stayed ~868 GB.
- `--workers 3` (lower than the 13.6-vs-13.7 run's 6) to cap concurrent temp-file
  spill, since this box now has less headroom (the LSM DB occupies ~503 GB). Read-only
  throughout.

## Conclusion

**The LSM on-disk-UTxO backend produces relational data identical to the standard
in-memory backend** on mainnet (db-sync 13.7.1.0) - confirming the preview and
preprod results at full mainnet scale, billion-row tables included. The only
differences are a handful of accumulator rows from the small tip gap, **proven
benign automatically** by `--verify-accumulators`. There is no unexpected
difference, so the mainnet LSM database can be deleted to reclaim disk without
losing any distinct chain data.
