# Primer 03 - Hashing and fingerprints

> **What's in here:** how we check that two giant tables hold the same data
> *without* shipping millions of rows across the network - and how to do it so
> the row order doesn't matter.
>
> **Prerequisites:** [primer 01](01-databases-in-2-minutes.md).

## A hash is a fingerprint

A **hash function** takes any text and produces a short, fixed-length
"fingerprint". The one this tool uses is **MD5**, whose fingerprint is 32 hex
characters:

```
md5("relay1.apool.online") = 6f1e9c... (32 hex chars)
md5("relay2.apool.online") = a3b8d4... (completely different)
```

Three properties are all we need:

1. **Deterministic** - the same input *always* gives the same fingerprint.
   Compute it in Warsaw or in Tokyo, today or next year: identical input → identical
   fingerprint.
2. **Tiny change → totally different fingerprint** - flip one character and the
   output looks unrelated. So if two fingerprints match, the inputs are almost
   certainly identical.
3. **Fixed, small size** - no matter how long the input, the fingerprint is the
   same short length.

> We use MD5 because it's fast and built into PostgreSQL. MD5 is "broken" for
> security (an attacker can engineer two different inputs with the same
> fingerprint). That doesn't matter here: nobody is *attacking* the comparison;
> we just need a fast fingerprint that changes when data changes.

## Why fingerprints instead of comparing the data directly

To check that table T is identical in database A and database B, the obvious idea
is to pull all of T out of both databases and compare. For `ma_tx_out` that's
**1.1 billion rows** out of *each* database. You'd move terabytes across the
network and need a giant machine to hold it. Hopeless.

Instead: ask database A to compute a fingerprint of T, ask database B to compute
a fingerprint of T, and **compare the two fingerprints** (64 characters each).
The heavy work happens *inside* each database, right next to the data. Only two
tiny fingerprints travel. If they match, the tables match.

## The catch: a fingerprint of a *set* of rows

We don't want a fingerprint of *one row* - we want one fingerprint for the
*whole table*. And here's the subtlety: the two databases may store the same rows
in a **different physical order** (and we don't care about order - a table is a
*set* of rows). So we need a fingerprint of the set that:

- **ignores order** - rows A,B,C must fingerprint the same as C,A,B, and
- **is not fooled by duplicates** - two different rows that happen to repeat must
  not secretly cancel out.

## The "sum of heights" trick

Picture two rooms full of people. You want to check the two rooms contain the
same group, but you can't line everyone up to compare one by one, and people are
milling around (no fixed order). Simple method: **each person writes their height
on a card; you add up all the heights in each room and compare the two totals.**

- Addition **ignores order**: `170 + 165 + 180` equals `180 + 170 + 165`. It
  doesn't matter who stands where.
- If even one person is a different height, the totals differ.

That's exactly what the tool does, per table:

1. For each row, glue its (meaningful) columns together into a string and take the
   **MD5 fingerprint** - that's the person's "height", but a huge, almost-never-colliding
   number instead of a height in cm.
2. **Add up** all those per-row numbers. The total is the table's set-fingerprint.

Because we add (not concatenate-then-hash), the order rows come out in is
irrelevant - no sorting needed, no holding everything in memory.

### Why add, and not XOR?

A popular alternative is to combine values with XOR (a bitwise exclusive-or)
instead of `+`. XOR has a nasty property here: `x XOR x = 0`. So if a row appears
**twice**, the two copies cancel and vanish from the fingerprint - and a bug that
duplicates or drops a pair of identical rows would go undetected. Plain addition
doesn't cancel, so duplicates are counted. We add.

### Why split it into two numbers?

The tool actually keeps **two** running totals (it splits each MD5 fingerprint
into two halves and sums each half separately). More independent positions means
the chance of two genuinely different tables landing on the same pair of totals
by accident is astronomically small - far smaller than the chance of a hardware
glitch. Two numbers is plenty.

## What it looks like in SQL

This is, essentially verbatim, what the tool sends to each database for a table:

```sql
SELECT
  count(*),                                              -- how many rows
  sum( ('x' || substr(h, 1, 15))::bit(60)::bigint ),     -- running total, first half
  sum( ('x' || substr(h,17, 15))::bit(60)::bigint )      -- running total, second half
FROM (
  SELECT md5( ROW(col1, col2, col3, ...)::text ) AS h    -- fingerprint each row
  FROM the_table
  WHERE ... -- only the rows in the chain window we're comparing
) per_row;
```

In one validation run this fingerprinted the entire 13.3-million-row `block`
table in about **22 seconds**, entirely on the server, sending back just a count
and two numbers.

The columns inside that `ROW(...)` are not simply "every column" - the row's `id`
and its foreign keys are deliberately swapped out first. *Why* is the subject of
[primer 05](05-surrogate-ids-sequences-and-drift.md), and it's the crux of the
whole tool.

**Next:** [Cardano and db-sync, just enough →](04-cardano-and-dbsync-for-this-tool.md)
