# 08 — Case study: the pool-relay port bug

> **What's in here:** a real regression this tool found on its first serious run,
> walked through end to end — both as proof the tool works and as a worked example
> of how to read its output and drill into a finding.
>
> **Prerequisites:** [running it](05-running-it.md).

## The setup

Two real **mainnet** databases:

- **DB1:** cardano-db-sync **13.6.0.5** (the trusted prior release).
- **DB2:** cardano-db-sync **13.7.1.0** (the release candidate).

We ran a full comparison up to the common boundary.

## What the tool printed

Almost everything matched — including big, foreign-key-heavy tables across the
whole drift region, which is itself the proof that the "compare by meaning"
machinery works:

```
  OK withdrawal           MATCH   n=11596065/11596065   504.6s
  OK stake_registration   MATCH   n=2087047/2087047     293.8s
  OK pool_update          MATCH   n=36739/36739           2.0s
  ...
  !! pool_relay           HASH_DIFF   n=72514/72514        0.8s  row counts match but content hash differs
```

`pool_relay` (stake-pool network addresses) came back **`HASH_DIFF`**: the **same
number of rows** in both databases, but a **different content fingerprint**. Same
count, different content — so it's not missing rows; some *values* differ.

Then Phase 2 binary-searched the chain to pin down where
([how it works, idea 5](03-how-it-works.md)):

```
Phase 2: localizing mismatches ...
  pool_relay: block_no 4490224..4491848: content differs (db1 n=681, db2 n=681)
  pool_relay: block_no 4496724..4498348: content differs (db1 n=143, db2 n=143)
  ...
```

## Drilling in

Armed with a concrete block window, we pulled the actual `pool_relay` rows for
that window from both databases (joining through `pool_update → tx → block` to
filter by block height) and diffed them. The differing column was **`port`**:

| pool relay | 13.6.0.5 | 13.7.1.0 |
|------------|----------|----------|
| `relay1.apool.online` | `52636` | `-12900` |
| `relay1.epicpool.eu`  | `60000` | `-5536`  |
| `relays.can-ada.io`   | `55444` | `-10092` |

The 13.7.1.0 values are **negative**. And they're not random: each is exactly the
correct value **minus 65536**:

```
52636 - 65536 = -12900
60000 - 65536 =  -5536
55444 - 65536 = -10092
```

That −65536 pattern is the signature of a **signed 16-bit overflow**. A 16-bit
*unsigned* integer holds 0–65535 (which is exactly the range of TCP port numbers).
A 16-bit *signed* integer holds −32768–32767. If you take an unsigned port above
32767 and reinterpret the same bits as signed, you get the value minus 65536. So
13.7.1.0 is decoding pool relay ports as signed 16-bit when it shouldn't.

## Confirming it's real, not a tool artifact

We checked the column type and value ranges in both databases directly:

| | 13.6.0.5 | 13.7.1.0 |
|---|---|---|
| `port` column type | `integer` | `integer` |
| min … max port | `1 … 64848` | `-32536 … 31415` |
| rows with port > 32767 | 1116 | 0 |
| rows with port < 0 | 0 | 1118 |

The column is a 32-bit `integer` in **both** — so the column itself can hold
65535 fine. 13.6.0.5 stores ports up to 64848 correctly; 13.7.1.0 has **zero**
ports above 32767 and **1118 negative** ones. The corruption happens when
13.7.1.0 *decodes* the port, before storing it. Roughly 1,100+ mainnet relays are
affected — worth reporting upstream.

## Why this is the whole point of the tool

This difference is invisible to a row-count check (counts matched), would be
drowned out by id-drift noise in a naive comparison, and sits in deep history far
from the tip. The tool surfaced it because it:

1. compared by **meaning**, so id drift didn't bury the signal
   ([primer 05](primers/05-surrogate-ids-sequences-and-drift.md));
2. fingerprinted **content**, so a same-count value change still showed up
   ([primer 03](primers/03-hashing-and-fingerprints.md));
3. **localized** it to a few thousand blocks so a human could pull the rows in
   seconds ([how it works, idea 5](03-how-it-works.md)).

That's exactly the kind of silent, data-level regression a release gate needs to
catch.

## How to read any finding, in general

1. **Status tells you the shape.** `HASH_DIFF` = values changed; `COUNT_DIFF`
   (non-accumulator) = rows added/removed; `VALUE_DIFF` = a numeric total moved.
2. **Phase 2 tells you where.** Use the block/epoch window.
3. **You pull the rows.** Join the table up to `block`, filter by the window,
   `ORDER BY` a natural key, and diff the two databases' output to find the
   offending column — just like above.

**Next:** [Extending and limitations →](09-extending-and-limitations.md)
