# Investigation report — preview LSM backend vs standard backend

**Date:** 2026-06-05 · **Tool:** `db-sync-compare` (tiered, `--workers 3`)

| | DB1 | DB2 |
|---|---|---|
| db-sync version | 13.7.1.0 (**LSM** on-disk UTxO backend) | 13.7.1.0 (**standard** backend) |
| network | preview | preview |
| tip | block 4,321,855 / epoch 1310 | block 4,320,884 / epoch 1310 |
| common cutoff | block ≤ 4,320,884, epoch ≤ 1308 | |

**Why this comparison:** both DBs are the **same db-sync version on the same
chain**, differing only in storage backend — DB1 uses the LSM-backed on-disk UTxO
set, DB2 the standard in-memory ledger. So it answers: *does the LSM backend
produce identical relational data?* (And it's a clean contrast to the mainnet
cross-version run, which surfaced real bugs.)

## Result — clean pass

```
SUMMARY: 57 match, 0 discrepancies, 2 accumulator count-deltas (informational), 0 errors, 16 excluded
FINISHED rc=0  WALL_SECONDS=819 (0h13m39s)
```

- **Every chain-anchored table MATCHES**, including the giants:
  `tx_out` 24,969,565 ✓ · `ma_tx_out` 40,643,204 ✓ · `tx_in` 21,909,503 ✓ ·
  `tx` 6,570,993 ✓ · `redeemer` 9,825,671 ✓.
- **The only two flagged are accumulator tip-gap deltas** (informational), both
  caused by the LSM DB being ~971 blocks ahead:

| accumulator | rows DB1 (LSM) / DB2 (std) | delta |
|---|---|---|
| `multi_asset` | 605,979 / 605,947 | +32 in LSM |
| `stake_address` | 277,721 / 277,711 | +10 in LSM |

## Proof the deltas are pure tip-gap (subset check)

`--verify-accumulators` (and the equivalent manual `psql` + `comm` check) confirm
the smaller side is a **clean subset** of the larger — i.e. the behind DB has
nothing the ahead DB lacks; the delta is only extra tip rows:

```
multi_asset:   only_db1=32 only_db2=0 → db2 ⊆ db1 — db1 is a clean superset (tip-gap-consistent; db1 ahead)
stake_address: only_db1=10 only_db2=0 → db2 ⊆ db1 — db1 is a clean superset (tip-gap-consistent; db1 ahead)
```

`only_db2 = 0` (the behind DB has zero rows the ahead DB is missing) is the
decisive fact; the +32 / +10 are exactly the count deltas. See
[docs/06 — verifying a count-delta](../docs/06-how-each-table-is-compared.md#how-to-verify-an-accumulator-count_diff-tip-gap-or-real).

## Slowest tables

| table | status | rows | seconds |
|---|---|---|---|
| `tx_out` | MATCH | 24,969,565 | 372.5 |
| `ma_tx_out` | MATCH | 40,643,204 | 220.2 |
| `tx_in` | MATCH | 21,909,503 | 154.4 |
| `tx` | MATCH | 6,570,993 | 143.6 |
| `redeemer` | MATCH | 9,825,671 | 142.5 |

## Conclusion

**The LSM on-disk-UTxO backend produces relational data identical to the standard
backend** (db-sync 13.7.1.0, preview). The sole differences are a handful of
accumulator rows from the small tip gap, proven benign by the subset check. No
unexpected differences — exactly what a same-version comparison should look like.
