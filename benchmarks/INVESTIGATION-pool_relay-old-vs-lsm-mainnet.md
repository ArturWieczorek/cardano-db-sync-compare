# Investigation: `pool_relay.port` — full lifecycle of cardano-db-sync #2135 on mainnet

**Date:** 2026-06-06
**Issue:** [cardano-db-sync #2135](https://github.com/IntersectMBO/cardano-db-sync/issues/2135)
— `pool_relay.port` signed-16-bit overflow: ports > 32767 are stored as negative
numbers (`52636` → `-12900`), root-caused to `Schema/Core/Pool.hs:224`.

## Question

Three things, end to end:

1. Does 13.7.1.0 actually get `pool_relay.port` wrong relative to a known-good DB?
2. Does the repair migration (db-sync stage_two **0049**) restore the correct values?
3. Is the migration's footprint limited to **`pool_relay` + `schema_version`** — i.e.
   did the fix introduce no other data or schema change?

## Databases involved

| Tag | Database | db-sync | Tip | `pool_relay.port` state |
|-----|----------|---------|-----|--------------------------|
| **A** | `mainnet-13.6.0.5-restored-on-13.7.0.4` | 13.6.0.5 (restored on 13.7.0.4) | block 13,313,031 / ep 626 | **correct** (0 neg, max 64,848) — bug predates 13.7.x |
| **B** | `mainnet-dbsync-13.7.1.0-node-11.0.1` | 13.7.1.0 (standard storage) | block 13,488,662 / ep 634 | **buggy** (1,118 neg, min −32,536) |
| **C** | `lsm-mainnet-dbsync-13.7.1.0-node-11.0.1` | 13.7.1.0 (LSM storage) **+ migration 0049** | block ~13,506,710 / ep 635 | **repaired** (0 neg, max 64,848) |

`schema_version` stage_two: A/B = **48**, C = **49** (the repair migration). The `port`
column is `int4` in **all three** — the bug was never the column width; 13.7.x wrote a
signed `Int16` value into the int4 column.

## The three reports, read as one before → bug → after story

All three use the same surrogate-id-free method: drop `pool_relay.id`, translate
`update_id` to the registration natural key (`tx.hash` + `pool_update.cert_index`), hash
`port` together with the address fields, and bound both DBs to the **common chain tip** so
the sync-progress gap can't create false differences. (Surrogate ids drift across the
Persistent→Hasql rewrite between 13.6.0.5 and 13.7.x, so they are never compared.)

### 1. Bug detected — A vs B  (`benchmarks/mainnet-full-2026-06-05.{log,json,SUMMARY.md}`)

Full 13.6.0.5 vs 13.7.1.0 comparison (9h42m, rc=1, 45 match / 7 discrepancies / 6 informational
accumulator deltas / 1 error / 16 excluded). `pool_relay` was a real discrepancy:

```
!! pool_relay   HASH_DIFF   n=72514/72514
   pool_relay: block_no 4490224..4491848 … 4517851..4519475   (8 localized windows)
```

Same row count, different content, localized to the 8 block windows where pools had
registered high-port relays. This is the run that first surfaced #2135. (The other six
differences were independently explained — `tx_out` #2053 pointer-address encoding,
`epoch_stake` zero-amount cleanup / migration 0047, `gov_action_proposal`, the boundary
`epoch` row, `pool_stat` config, etc. — see
`benchmarks/INVESTIGATION-13.6.0.5-vs-13.7.1.0.md`.)

### 2. The two 13.7.1.0 syncs are equivalent — B vs C, pre-migration  (`benchmarks/lsm-vs-standard-mainnet-2026-06-06.{log,json}`)

Full comparison of the standard (B) and LSM (C) 13.7.1.0 syncs, run **before** the repair
migration — so both were still buggy:

```
SUMMARY: 56 match, 0 discrepancies, 3 accumulator count-deltas (informational), 0 errors, 16 excluded
OK pool_relay   MATCH   n=72732/72732
```

Every comparable table matched, including `pool_relay` (both wrong identically: 1,118
negative ports each). This establishes the baseline: **B ≡ C across all data**, so anything
the later migration changes on C is isolated against an otherwise-identical twin.

### 3. Repair verified — A vs C, post-migration

`pool_relay`-scoped comparison of the known-good 13.6.0.5 (A) against the migrated LSM (C):

```
OK pool_relay   MATCH   n=72514/72514
```

Within the common window every relay row — including `port` — is identical to the
known-good DB. A direct multiset diff over **all** relays (not just the bug class) confirmed
both the **71,066 low ports (≤ 32767)** and the **1,116 high ports (> 32767)** match A
value-for-value: the repair fixed the broken rows **and left the already-correct values
untouched**.

## Direct buggy → fixed proof (B vs C, using the live broken DB)

Because B still holds the pre-repair state, the exact migration transform was verified
directly. Applying `port + 65536 where port < 0` to **B** reproduces **C** as an exact
multiset (72,732 relay rows identical, common boundary block ≤ 13,486,502):

```
buggy(remapped) rows = 72732   fixed rows = 72732
diff  ->  IDENTICAL: (port+65536 where port<0) on the BUGGY db reproduces the FIXED db exactly
rows the remap touched: 1118 (the negatives)   rows untouched: 71282
```

So the migration is a **pure signed→unsigned 16-bit reinterpretation** of exactly the 1,118
overflowed ports, with **zero collateral edits** to any other relay.

> **Method note / gotcha:** this must be checked as a multiset that *includes* `port`. Keying
> on identity-minus-port (`tx.hash`+`cert_index`+addresses) and asserting a single port is
> **wrong** — a pool can register several relays at the same address with different ports, so
> that key is not unique and produces spurious "violations." The full-row multiset diff (and
> the tool's row hash) avoid this.

The 1,118 affected relays were extracted as a small, self-contained CSV
(`reg_tx_hash, cert_index, ipv4, ipv6, dns_name, dns_srv_name, buggy_port, repaired_port`).
The full list is kept locally (not shipped in this repo); a representative sample:

```csv
reg_tx_hash,cert_index,ipv4,ipv6,dns_name,dns_srv_name,buggy_port,repaired_port
0016f71c1f5a6169d949c83239bd6edee368740d33dec9e6c32c37de2270668e,0,54.39.243.118,,,,-15524,50012
0031292d4b393215ef65935774c9e1b0d901e5fc026bc1443c61b4caaa36062d,0,194.233.66.179,,,,-27093,38443
```

Each `buggy_port` is the stored signed-16-bit value; `repaired_port` adds 65536 to the
negative ones (-15524 + 65536 = 50012, -27093 + 65536 = 38443).

## On the "dump `pool_relay` before the fix" suggestion

The original advice was to `\copy pool_relay … TO 'pool_relay_buggy.csv'` before running the
fix, to preserve the pre-repair state for the buggy-vs-fixed scope check. With the current
setup that raw dump is **redundant**:

- the **live broken DB (B)** still holds the full pre-repair state — a queryable snapshot of
  every table, strictly better than a CSV of one table;
- the scope check it was meant to enable is **already done** (reports 1–3 + the remap proof);
- a raw `SELECT *` dump carries surrogate `id`/`update_id` that don't line up across versions,
  so on its own it proves nothing without the natural-key join.

What *is* worth keeping is the **focused** artifact above — the 1,118 affected relays with
buggy and repaired ports side by side, keyed on the registration natural key (112 KB). It is
self-documenting and survives even if the 503 GB broken DB is reclaimed. Keep B around until
this investigation is closed; after that the CSV is sufficient durable proof.

## Conclusion

1. **13.7.1.0 gets `pool_relay.port` wrong** — report 1 (A vs B): `pool_relay` HASH_DIFF,
   1,118 negative ports from the Int16 overflow.
2. **Migration 0049 restores the correct values** — report 3 (A vs C): migrated `pool_relay`
   matches the known-good 13.6.0.5 value-for-value; the buggy→fixed transform is exactly
   `port + 65536` on the 1,118 overflowed rows.
3. **The migration changed only `pool_relay` + `schema_version`** — report 2 (B vs C) shows
   the two 13.7.1.0 syncs were identical across all 56 comparable tables before the fix; the
   migration's footprint is the `pool_relay` port rewrite plus the `schema_version` 48→49
   bump, nothing else. The repair touched zero already-correct ports and no other table.

Residual non-port differences anywhere are the expected **tip gap** (the DBs are synced to
different heights) and the unrelated, separately-explained findings in report 1.
