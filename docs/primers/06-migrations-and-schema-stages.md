# Primer 06 - Migrations and schema stages

> **What's in here:** how db-sync builds and updates the *shape* of its database
> (the tables, the column types, the indexes), and what the mysterious
> `migration-2-0048-...sql` filenames mean. You do not need this to run the
> comparison tool, but it explains a lot of what you see inside a db-sync database.
>
> **Prerequisites:** [primer 01](01-databases-in-2-minutes.md) and
> [primer 04](04-cardano-and-dbsync-for-this-tool.md).

## What is a migration?

Software changes over time, and so does the *shape* of the database it writes to.
Version 1 of db-sync might have a `tx` table with 5 columns; version 2 adds a 6th;
version 3 adds a new table entirely. A **migration** is a small, numbered SQL script
that makes one such change. Think of them as **numbered renovation instructions for a
building**: "step 1, pour the foundation; step 2, build the rooms; step 17, add a new
window." Run the instructions in order on an empty lot and you get the finished
building; run the *new* instructions on an already-built house and you get just the
renovation.

db-sync ships its migrations as plain `.sql` files. When db-sync starts, it applies any
migrations the database has not seen yet, in order, before it begins syncing. That is
how a fresh empty database gets the full schema, and how an existing database picks up
the changes a new db-sync release introduced.

## The filename tells you everything

The migration files live in the **`schema/`** directory of the db-sync repo, and each
is named:

```
migration-STAGE-NNNN-YYYYMMDD.sql
         |     |       |
         |     |       +-- the date it was written (just informational)
         |     +---------- a 4-digit number, its order within the stage
         +---------------- the STAGE (1, 2, 3, or 4) - explained below
```

For example `migration-2-0048-20260520.sql` is stage **2**, the **48th** stage-2
migration. Because the numbers are zero-padded, simply sorting the filenames
alphabetically puts them in the exact order they must run: all of stage 1, then all of
stage 2, and so on. (The parser is in
`cardano-db/src/Cardano/Db/Migration/Version.hs` if you want to see it.)

## The four stages

This is the part worth memorising. db-sync splits its migrations into **four stages**,
and the stage number is not just a label - it controls *when* the migration runs.

| Stage | What it contains | When it runs |
|-------|------------------|--------------|
| **1** | Custom column types (`CREATE DOMAIN ...`) | At startup, on an empty DB |
| **2** | The tables themselves (`CREATE TABLE ...`), plus occasional data repairs | At startup |
| **3** | The few indexes db-sync itself needs *while* it syncs | At startup |
| **4** | All the *other* indexes - the ones query users want | Later, near the chain tip |

Why the split between stage 3 and stage 4 matters is the subject of the next primer
([how db-sync loads fast](07-how-db-sync-loads-fast.md)): building indexes is slow, so
db-sync builds only the essential ones up front (stage 3) and defers the rest (stage 4)
until it has almost caught up with the chain.

### Stage 1 - custom column types

A stage-1 migration creates db-sync's custom column types. Real excerpt from
`schema/migration-1-0001-20190730.sql`:

```sql
CREATE DOMAIN lovelace AS numeric (20, 0) CHECK (VALUE >= 0 AND VALUE <= 18446744073709551615);
CREATE DOMAIN hash32type AS bytea CHECK (octet_length (VALUE) = 32);   -- block and tx hashes
CREATE DOMAIN addr29type AS bytea CHECK (octet_length (VALUE) = 29);   -- stake addresses
```

What a `DOMAIN` is, and why db-sync uses them, is covered in
[primer 08](08-column-types-and-saving-space.md). For now: stage 1 defines the
vocabulary of column types that stage 2 then uses.

### Stage 2 - the tables

A stage-2 migration creates the actual tables. Real excerpt from
`schema/migration-2-0001-20211003.sql`:

```sql
CREATE TABLE "block" ("id" SERIAL8 PRIMARY KEY UNIQUE,
                      "hash" hash32type NOT NULL,
                      "slot_no" word63type NULL,
                      ... );
```

Notice the column types (`hash32type`, `word63type`) are the custom domains from
stage 1. Stage 2 is where `block`, `tx`, `tx_out`, and the ~70 other tables are born.

### Stage 3 - the indexes db-sync needs to sync

A stage-3 migration creates the handful of indexes db-sync relies on internally while
loading the chain (for example, to find a block by its number during a rollback). Real
excerpt from `schema/migration-3-0002-20200521.sql`:

```sql
CREATE INDEX IF NOT EXISTS idx_block_slot_no ON block(slot_no);
CREATE INDEX IF NOT EXISTS idx_tx_block_id ON tx(block_id);
```

### Stage 4 - the indexes everyone else needs

A stage-4 migration creates the indexes that *query users* (explorers, analysts) want
but db-sync does not need to do its own job. Real excerpt from
`schema/migration-4-0002-20200810.sql`:

```sql
CREATE INDEX IF NOT EXISTS idx_block_time ON block(time);
CREATE INDEX IF NOT EXISTS idx_pool_update_hash_id ON pool_update(hash_id);
```

These are deliberately held back until db-sync is nearly synced (see
[primer 07](07-how-db-sync-loads-fast.md)).

> **A correction worth flagging:** older notes (including the parent repo's `CLAUDE.md`)
> describe "stage 3 = SQL views." That is out of date. In the current db-sync, stage 3
> is db-sync's own indexes, stage 4 is the rest of the indexes, and there are **no SQL
> views** in the shipped schema at all - downstream tools query the base tables
> directly.

## How db-sync remembers what it has already applied

You do not want a migration to run twice (it might create a table that already exists,
or apply a fix a second time). db-sync tracks progress in a tiny table called
**`schema_version`**, created by the very first migration
(`schema/migration-1-0000-20190730.sql`):

```sql
CREATE TABLE "schema_version" (id SERIAL PRIMARY KEY UNIQUE,
  stage_one INT8 NOT NULL, stage_two INT8 NOT NULL, stage_three INT8 NOT NULL);
INSERT INTO "schema_version" (stage_one, stage_two, stage_three) VALUES (0, 0, 0);
```

It holds one row with a counter per stage. Each stage-1 and stage-2 migration begins
with a guard like this (from the port-repair migration shown later):

```sql
SELECT stage_two + 1 INTO next_version FROM schema_version ;
IF next_version = 49 THEN
    -- ... do the work ...
    UPDATE schema_version SET stage_two = next_version ;
END IF ;
```

So migration number 49 only runs if the recorded `stage_two` is currently 48, and it
bumps the counter to 49 when done. Run it again and the `IF` is false, so it does
nothing. That is how stage 1 and stage 2 run **exactly once**.

Stages 3 and 4 use a simpler trick: they are written to be **idempotent** with
`CREATE INDEX IF NOT EXISTS ...`, which is harmless to re-run. (That is why
`schema_version` only has columns for stages one, two, and three, and the stage-three
counter is effectively ignored.)

## When each stage runs (and the `--force-indexes` flag)

db-sync runs migrations in two phases:

- **At startup** it runs stages 1, 2, and 3 (called "Initial" mode in the code,
  `cardano-db-sync/src/Cardano/DbSync.hs`). Now the schema, the tables, and the
  sync-critical indexes all exist, so loading can begin.
- **Near the chain tip** (about 30 minutes from caught-up) it runs stage 4 - the heavy
  user indexes. Building those on a nearly-full database, once, is far cheaper than
  maintaining them through the whole sync.

If you would rather build everything up front, the `--force-indexes` flag does exactly
that:

```
--force-indexes   Forces the Index creation at the start of db-sync.
                  Normally they're created later.
```

The runtime even warns you when it reaches the stage-4 step: *"Creating Indexes. This
may require an extended period of time ... Setting a higher maintenance_work_mem from
Postgres usually speeds up this process."*

## Two kinds of migration: changing the shape vs fixing the data

Most migrations change the database's *shape* (`CREATE`/`ALTER TABLE`, `CREATE INDEX`).
But a migration is just SQL, so it can also **repair existing data**. db-sync uses this
to fix bugs from older releases without making operators rebuild from scratch. Two real
examples:

- **`migration-2-0048-20260520.sql`** recomputes every epoch's `out_sum` / `fees` /
  `tx_count` / `blk_count` from the underlying `block` and `tx` tables, repairing
  corruption from db-sync 13.7.0.0-13.7.0.4 (issue #2118). It changes no table shape -
  it only rewrites wrong values, and only where they actually disagree.
- **`migration-2-0049-20260605.sql`** repairs `pool_relay.port` corruption (issue
  #2135 - ports above 32767 had been stored as negative numbers). The entire fix is:

  ```sql
  UPDATE pool_relay SET port = port + 65536 WHERE port < 0 ;
  ```

  Both are guarded by the `schema_version` counter, so they run once and are a no-op on
  a clean or freshly synced database. (If that pool_relay bug sounds familiar, it is the
  one written up in [the case study](../08-case-study-pool-relay-port.md).)

## How the files are actually applied (and a safety check)

Each migration is fed to PostgreSQL with `psql --single-transaction` and
`ON_ERROR_STOP=on` (see `cardano-db/src/Cardano/Db/Migration.hs`), so a migration is
**all-or-nothing**: if any statement fails, the whole file rolls back and db-sync stops
rather than leaving a half-applied schema.

db-sync also carries a checksum (a Blake2b hash) of every migration file it expects,
compiled into the binary. At startup it re-hashes the files on disk and complains if
one was altered or is missing - a guard against running against a tampered or mismatched
schema.

## Where to read more (in the db-sync repo)

- `doc/schema-management.md` - the canonical description of the four stages.
- `schema/` - all the real migration files.
- `scripts/` - hand-run versions of the data repairs (e.g. `validate-epoch-table.sql`,
  `fix-epoch-table.sql`) for operators who prefer not to rely on the automatic
  migration.

**Next:** [How db-sync loads a whole blockchain fast →](07-how-db-sync-loads-fast.md)
