# Primer 07 - How db-sync loads a whole blockchain fast

> **What's in here:** the handful of database tricks db-sync uses to load *billions*
> of rows in a reasonable time, and the flags (`--force-indexes`, `--disable-cache`)
> that turn them on and off. This explains why a fresh sync behaves the way it does.
>
> **Prerequisites:** [primer 02](02-indexes-and-table-scans.md) (indexes) and
> [primer 06](06-migrations-and-schema-stages.md) (stages).

## The problem

A full mainnet database is hundreds of gigabytes and well over a billion rows. If
db-sync inserted those one row at a time, with every index and every constraint fully
in place the whole way, an initial sync would take far longer than it does. So db-sync
leans on a single guiding idea:

> **Defer expensive per-row work, and do it once in bulk instead.**

Almost every trick below is an application of that one idea.

## Trick 1: build the big indexes *last*, not during the load

From [primer 02](02-indexes-and-table-scans.md): an index is a sorted helper structure
the database keeps alongside a table. It makes reads fast - but it makes *writes*
slower, because **every time you insert a row, the database must also update every
index on that table** (slot the new entry into each sorted structure). With a dozen
indexes on a table, one insert becomes a dozen-plus little bookkeeping updates.

Analogy: imagine adding 1,000 new books to a library. If you re-file the catalogue
after *every single book*, you walk to the card cabinet 1,000 times. Far faster to dump
all 1,000 books on the floor, then build the catalogue *once* at the end.

That is exactly what db-sync does. Recall the migration stages from
[primer 06](06-migrations-and-schema-stages.md):

- **Stage 3** (run at startup) creates only the few indexes db-sync itself needs while
  syncing.
- **Stage 4** (the rest - the indexes query users want) is **held back until db-sync is
  about 30 minutes from the chain tip**, then built in one shot over the nearly-full
  tables.

So for almost the entire sync, the big tables are loaded "naked" (few indexes), which
keeps inserts fast; the catalogue is built once at the end.

You can override this with the flag:

```
--force-indexes   Forces the Index creation at the start of db-sync.
                  Normally they're created later.
```

and db-sync warns when it reaches the build step that it "may require an extended period
of time," suggesting a higher Postgres `maintenance_work_mem` to speed the one-shot
build.

## Trick 2: add constraints late too

A **constraint** (for example "this column must be unique" or "this foreign key must
point at a real row") is also checked on every insert. For the same reason as indexes,
db-sync adds several constraints only near the tip rather than enforcing them during the
bulk load (`addConstraintsIfNotExist` in
`cardano-db-sync/src/Cardano/DbSync/Default.hs`). Same idea: do not pay a per-row cost
during the load if you can pay it once at the end.

## Trick 3: insert in bulk with UNNEST, not row by row

Even with few indexes, sending one `INSERT` per row means a network round-trip and a bit
of overhead per row. db-sync instead inserts **many rows in a single statement** using
PostgreSQL's `UNNEST`.

The idea: instead of passing 500 rows, you pass a handful of *arrays* - one array per
column - and ask Postgres to "unzip" them back into 500 rows on its side. One statement,
one round-trip. The generated SQL (from
`cardano-db/src/Cardano/Db/Statement/Function/InsertBulk.hs`) looks like:

```sql
INSERT INTO tx_out (address, value, ...)
  SELECT address, value, ...
  FROM UNNEST ($1::text[], $2::numeric[], ...) AS t(address, value, ...);
```

`$1` is the whole array of addresses, `$2` the whole array of values, and so on. Picture
filling a spreadsheet by pasting one column at a time, rather than typing each cell.

## Trick 4: commit a whole batch of blocks as one transaction

A database **transaction** is a group of changes that either all succeed or all fail
together, finalised by a `COMMIT`. Each commit forces the database to flush to disk
(an `fsync`), which is relatively slow. Committing after every single row, or even every
single block, would mean a flush-to-disk constantly.

db-sync instead pulls a *batch* of blocks off its internal queue and inserts the whole
batch inside **one transaction**, then commits once (see
`cardano-db-sync/src/Cardano/DbSync/Database.hs` and `Default.hs`). Far fewer
flush-to-disk operations, much faster loading. (Once it is caught up near the tip it
commits more often, so the latest data is durable promptly.)

## Trick 5: remember ids in memory instead of asking the database

When db-sync inserts, say, a transaction output at some address, it needs the database
`id` of that address's row. The slow way is to run `SELECT id FROM address WHERE ...`
every single time - millions of tiny lookups.

Instead db-sync keeps **in-memory caches**: small lookup tables in RAM that map a
chain-level identity (an address, a stake credential, a pool hash, a multi-asset, a
transaction id) to the database `id` it was given (see
`cardano-db-sync/src/Cardano/DbSync/Cache/`). The first time it sees an address it does
the lookup and remembers the answer; after that the `id` comes straight from memory.
Like keeping the phone numbers you call often in your contacts instead of looking each
one up in the phone book every time.

These caches are why db-sync uses a fair amount of RAM. The flag:

```
--disable-cache   Disables the db-sync caches. Reduces memory usage but it takes
                  longer to sync.
```

makes the trade-off explicit: less memory, slower sync.

## Trick 6: undo a rollback by deleting, not rebuilding

From [primer 04](04-cardano-and-dbsync-for-this-tool.md): occasionally the chain rolls
back the last few blocks. db-sync does not rebuild anything - it simply **deletes** the
rows for the orphaned blocks. Because the tables are wired with `ON DELETE CASCADE`,
deleting a `block` row automatically deletes its `tx` rows, their `tx_out` rows, and so
on down the chain of references. One delete at the top cleans up everything beneath it.

(This deleting-and-reinserting is also the root of the surrogate-id drift that the whole
comparison tool is built around - see
[primer 05](05-surrogate-ids-sequences-and-drift.md).)

## A note on the engine room: Persistent vs Hasql

You may see the words "Persistent" and "Hasql" in the db-sync code and changelog. They
are just *how* db-sync talks to PostgreSQL from Haskell:

- **Persistent** (older) was a high-level library that generated SQL for you from
  Haskell data definitions - convenient, but with less control over the exact SQL.
- **Hasql** (since db-sync 13.7.0.1) is a thin, low-level library where each statement
  is hand-written SQL paired with explicit value encoders and decoders.

The switch to Hasql is what lets db-sync use precise, Postgres-specific tricks like the
`UNNEST` bulk insert above. You do not need this to use the database - but it explains
why recent db-sync writes SQL by hand, and (as the [case study](../08-case-study-pool-relay-port.md)
shows) why a hand-written value encoder is also where a bug like the `pool_relay.port`
overflow could creep in.

## The one idea, restated

Nearly everything here is the same move: **avoid per-row cost; do the expensive thing
once, in bulk.** Defer indexes and constraints to a single end-of-sync build; insert in
big `UNNEST` batches; commit many blocks per transaction; cache id lookups in memory.
Together they turn "insert a billion rows" from impossible into an overnight job.

**Next:** [Column types and saving space →](08-column-types-and-saving-space.md)
