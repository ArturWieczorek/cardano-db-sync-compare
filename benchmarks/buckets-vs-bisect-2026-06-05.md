# Localization benchmark - `--localize bisect` vs `--localize buckets`

Measures the two Phase-2 localization algorithms on a real giant table, to confirm
they (a) localize a mismatch to the **same** region and (b) how much faster the
one-pass `buckets` method is than the re-scanning `bisect` default.

## Setup

- DB1: cardano-db-sync **13.6.0.5** mainnet · DB2: **13.7.1.0** mainnet.
- One table, the only differing giant: **`tx_out`** (the pointer-address diffs,
  [#2051]/[#2053]).
- Bounded with `--cutoff-block 6000000` (blocks 0-6,000,000 → 23.16M `tx_out` rows)
  so the run is quick while still containing two real difference regions.
- `--workers 1` so the timing is clean (no parallelism noise).
- **Note:** localization only runs in cutoff mode. `--block-range` deliberately
  **skips Phase 2** (see `cli.py` / `docs/05`), so it cannot be used to benchmark
  this - `--cutoff-block` is the bounded mode that still localizes.

Command (per algorithm):

```bash
python -m db_sync_comparator \
  --db1 "dbname=mainnet-13.6.0.5… host=/var/run/postgresql" \
  --db2 "dbname=mainnet-13.7.1.0… host=/var/run/postgresql" \
  --tables tx_out --cutoff-block 6000000 \
  --localize {bisect|buckets} --workers 1 --json …
```

## Correctness - both localize to the same two regions

| diff region | `bisect` window | `buckets` window (default 1024) | agree? |
|---|---|---|---|
| 1 | 5,566,408 - 5,567,872 (~1.5k blocks) | 5,566,050 - 5,571,909 (~5.9k blocks) | ✅ `bisect` ⊂ `buckets` |
| 2 | 5,685,060 - 5,686,524 (~1.5k blocks) | 5,683,230 - 5,689,089 (~5.9k blocks) | ✅ `bisect` ⊂ `buckets` |

Both methods point at the same two stretches of chain; `buckets` reports a wider
fixed window (~5.9k blocks here, because 1024 windows over a 6M-block range) that
**contains** the tight adaptive `bisect` window. Raise `--localize-buckets` for
finer windows.

## Speed - `buckets` localization ≈ 11× faster

Phase 1 (the full-table hash) is identical work for both; only Phase 2 differs.
Phase 2 = total - Phase 1.

| algorithm | Phase 1 (hash) | **Phase 2 (localize)** | total |
|---|---|---|---|
| `bisect` | 499.7s | **≈ 5,704s (~95 min)** | 6,204s (1h43m) |
| `buckets` | 506.0s | **≈ 503s (~8 min)** | 1,009s (16.8 min) |

- `bisect` Phase-2 localization took **~11× longer** than `buckets`, and **~9× its
  own Phase 1** - the top halving levels each re-scan huge id-ranges (level 0 scans
  all 23M rows, level 1 two ~11.5M halves, …).
- `buckets` does it in **one grouped scan ≈ one Phase-1 pass** (503s ≈ 506s).
- End-to-end the run was **~6× faster** with `buckets`.

## Extrapolation to a full mainnet run

`tx_out` is 346M rows (Phase 1 ≈ 89 min on the full table). So full-table `tx_out`
localization with `buckets` is ≈ **one extra ~89-min scan**, versus `bisect`'s
*many* large scans (the multi-hour tail observed in the 9h42m full run). On a full
comparison, `--localize buckets` should save **a couple of hours** off the
localization tail, at the cost of coarser windows.

**Takeaway:** prefer `--localize buckets` for full mainnet runs; raise
`--localize-buckets` if you need tighter windows. The trade-off is resolution, never
correctness (localization is non-authoritative - it never changes a verdict).

## Artifacts

- `buckets-vs-bisect-2026-06-05.log` - full run log (both algorithms).
- `buckets-vs-bisect-2026-06-05.bisect.json`, `…buckets.json` - structured reports.

[#2051]: https://github.com/IntersectMBO/cardano-db-sync/issues/2051
[#2053]: https://github.com/IntersectMBO/cardano-db-sync/pull/2053
