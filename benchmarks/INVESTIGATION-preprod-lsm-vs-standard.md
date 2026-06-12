# Investigation report - preprod LSM backend vs standard backend

**Date:** 2026-06-05 · **Tool:** `db-sync-compare` (tiered, `--workers 3`, `--verify-accumulators`)

| | DB1 | DB2 |
|---|---|---|
| database name | `lsm-preprod-dbsync-13.7.1.0-node-11.0.1` | `preprod-dbsync-13.7.1.0-node-11.0.1` |
| db-sync version | 13.7.1.0 (**LSM** on-disk UTxO backend) | 13.7.1.0 (**standard** backend) |
| cardano-node | 11.0.1 | 11.0.1 |
| network | preprod | preprod |
| tip | block 4,756,960 / epoch 291 | block 4,755,862 / epoch 291 |
| common cutoff | block ≤ 4,755,862, epoch ≤ 289 | |

**Why this comparison:** same db-sync version on the same chain, differing only in
storage backend - does the LSM on-disk UTxO backend produce identical relational
data? (Companion to the preview run; same question, larger preprod chain.)

## Result - clean pass

```
SUMMARY: 57 match, 0 discrepancies, 2 accumulator count-deltas (informational), 0 errors, 16 excluded
FINISHED rc=0  WALL_SECONDS=548 (0h09m08s)
```

- **Every chain-anchored table MATCHES**, including the giants:
  `tx_out` 20,214,659 ✓ · `tx_in` 16,115,524 ✓ · `datum` 4,636,725 ✓ ·
  `collateral_tx_out` 3,044,365 ✓ · `block` 4,755,864 ✓ · `tx_metadata` 1,502,118 ✓.
- **The only two flagged are accumulator tip-gap deltas** (informational), the LSM
  DB being ~1,098 blocks ahead:

| accumulator | rows DB1 (LSM) / DB2 (std) | delta |
|---|---|---|
| `multi_asset` | 1,449,115 / 1,448,682 | +433 in LSM |
| `stake_address` | 535,431 / 535,389 | +42 in LSM |

## Proof the deltas are pure tip-gap (auto, via `--verify-accumulators`)

This run used the flag, so the subset check ran **inline** - no manual step:

```
multi_asset:   only_db1=433 only_db2=0 → db2 ⊆ db1 - db1 is a clean superset (tip-gap-consistent; db1 ahead)
stake_address: only_db1=42  only_db2=0 → db2 ⊆ db1 - db1 is a clean superset (tip-gap-consistent; db1 ahead)
```

`only_db2 = 0` (the behind DB has nothing the ahead DB lacks) is decisive; the
+433 / +42 are exactly the count deltas. See
[docs/06 - verifying a count-delta](../docs/06-how-each-table-is-compared.md#how-to-verify-an-accumulator-count_diff-tip-gap-or-real).

## Slowest tables

| table | status | rows | seconds |
|---|---|---|---|
| `tx_out` | MATCH | 20,214,659 | 247.2 |
| `collateral_tx_out` | MATCH | 3,044,365 | 143.1 |
| `tx_in` | MATCH | 16,115,524 | 102.4 |
| `datum` | MATCH | 4,636,725 | 100.9 |
| `tx_metadata` | MATCH | 1,502,118 | 93.3 |
| `block` | MATCH | 4,755,864 | 73.7 |

## Conclusion

**The LSM on-disk-UTxO backend produces relational data identical to the standard
backend** on preprod (db-sync 13.7.1.0) - matching the preview result. The only
differences are a handful of accumulator rows from the small tip gap, **proven
benign automatically** by `--verify-accumulators`. No unexpected differences.
