# 08 - Case study: the pool-relay port bug

> **What's in here:** a real regression this tool found on its first serious run,
> walked through end to end - both as proof the tool works and as a worked example
> of how to read its output and drill into a finding.
>
> **Prerequisites:** [running it](05-running-it.md).

## The setup

Two real **mainnet** databases:

- **DB1:** cardano-db-sync **13.6.0.5** (the trusted prior release).
- **DB2:** cardano-db-sync **13.7.1.0** (the release candidate).

We ran a full comparison up to the common boundary.

## What the tool printed

Almost everything matched - including big, foreign-key-heavy tables across the
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
count, different content - so it's not missing rows; some *values* differ.

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

That -65536 pattern is the signature of a **signed 16-bit overflow**. A 16-bit
*unsigned* integer holds 0-65535 (which is exactly the range of TCP port numbers).
A 16-bit *signed* integer holds -32768-32767. If you take an unsigned port above
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

The column is a 32-bit `integer` in **both** - so the column itself can hold
65535 fine. 13.6.0.5 stores ports up to 64848 correctly; 13.7.1.0 has **zero**
ports above 32767 and **1118 negative** ones. The corruption happens when
13.7.1.0 *encodes* the port, before storing it. The affected scope on mainnet is
**1,118 relay rows across 92 distinct pools** (234 distinct pool+endpoint relays).

## The root cause, confirmed in the db-sync source

The block-window evidence above was enough to file the issue, but we then pinned
the cause in the cardano-db-sync source. The schema record types the port as an
**unsigned** 16-bit value (correct - TCP ports are 0-65535):

```haskell
-- cardano-db/src/Cardano/Db/Schema/Core/Pool.hs:209
, poolRelayPort :: !(Maybe Word16)
```

but the encoder that writes it to PostgreSQL goes through a **signed** 16-bit
encoder:

```haskell
-- cardano-db/src/Cardano/Db/Schema/Core/Pool.hs:224
, poolRelayPort >$< E.param (E.nullable $ fromIntegral >$< E.int2)
```

`E.int2` is the Postgres `int2` encoder, whose Haskell type is `Int16` (signed,
-32768..32767). So `fromIntegral :: Word16 -> Int16` is exactly the
"reinterpret the bits as signed" step we deduced from the data - it wraps any port
above 32767 to `port - 65536` *before* the value reaches the database. The column
is declared `"port" INT4` (`schema/migration-2-0001-20211003.sql:69`), so the
column is not the limit; the value is already wrong by the time it is sent.

This hand-written encoder was introduced by the Persistent→Hasql rewrite in
**13.7.0.1** - which is why 13.6.0.5 (still on Persistent) stores the same ports
correctly. It is the same family of wrong-width codec bugs as the
`epoch.out_sum`/`fees` numeric decoders fixed in issue #2118.

**The fix** is one line - encode via `int4` (so `fromIntegral :: Word16 -> Int32`,
which does not wrap, into the already-`INT4` column) - plus a repair migration:

```haskell
- , poolRelayPort >$< E.param (E.nullable $ fromIntegral >$< E.int2)
+ , poolRelayPort >$< E.param (E.nullable $ fromIntegral >$< E.int4)
```
```sql
UPDATE pool_relay SET port = port + 65536 WHERE port < 0;
```

## It is not mainnet-specific

Because the cause is the encoder rather than anything network-specific, the same
overflow appears on every 13.7.1.0 network that has a relay declaring a port above
32767 - we confirmed it by querying the preprod and preview 13.7.1.0 databases
directly. The count simply scales with how many such relays each network has:

| network (13.7.1.0) | rows with port | min … max | negative | port > 32767 | pools / relays |
|---|---|---|---|---|---|
| mainnet | 72,400 | -32,536 … 31,415 | **1,118** | 0 | 92 / 234 |
| preprod | 1,341 | -25,536 … 31,000 | **17** | 0 | 9 / 9 |
| preview | 1,795 | -32,018 … 31,111 | **28** | 0 | 12 / 21 |

Every one of them shows the same fingerprint: some negative ports, and **zero**
ports above 32767 (they have all wrapped).

Filed upstream as **[cardano-db-sync #2135](https://github.com/IntersectMBO/cardano-db-sync/issues/2135)**.

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
   offending column - just like above.

**Next:** [Extending and limitations →](09-extending-and-limitations.md)
