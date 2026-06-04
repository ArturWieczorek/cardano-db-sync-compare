# Primer 02 — Indexes and table scans

> **What's in here:** why the same question can take a database 2 milliseconds or
> 3 minutes, depending on whether there's an *index*. This explains why parts of
> the tool are fast and parts are slow.
>
> **Prerequisites:** [primer 01](01-databases-in-2-minutes.md).

## The phone book analogy

Imagine a printed phone book with 100 million entries, **sorted by surname**.

- "Find everyone named *Nowak*" → easy. Flip to the **N**s, done in seconds. The
  sort order lets you *jump*.
- "Find everyone whose **phone number** is 555-0100" → painful. The book isn't
  sorted by number, so you must read **every single entry** front to back.

A database table is the same. By default the rows are not sorted in any way
helpful to your question, so to answer "find the rows where column X = ..." the
database may have to read **every row in the table**. That's called a
**sequential scan** (or "full table scan", or "seq scan"). On a 13-million-row
table that's tolerable; on a 1.1-**billion**-row table it's a coffee break.

## An index is the sorted tab

An **index** is an extra, behind-the-scenes structure the database keeps that
*is* sorted by a particular column — like the alphabetical thumb-tabs on a
dictionary, or the index at the back of a textbook. If there's an index on
`block_no`, the database can jump straight to the rows it wants instead of
reading everything.

Two facts that matter for this tool:

1. **The primary key is always indexed.** db-sync's `id` columns are automatically
   fast to look up and to range over.
2. **Other columns are only indexed if someone created an index for them.** Some
   db-sync columns have indexes; many don't. Whether your filter column is
   indexed is the difference between instant and a full scan.

## Indexes also make "smallest / largest / between" fast

Because an index is *sorted*, it doesn't just help with "= this value". It also
makes these fast:

- `min(col)` / `max(col)` — jump to the first / last entry of the sorted index.
- `WHERE col BETWEEN a AND b` — jump to `a`, read until `b`, stop.

This is exactly how the tool restricts a comparison to one stretch of the
blockchain quickly — *if* the right column is indexed.

## The trap that cost us 3 minutes (a real story)

This tool needs to ask things like: *"what is the smallest and largest transaction
`id` among the transactions in blocks 8,000,000–8,010,000?"*

The transactions table (`tx`, 121 million rows) has an index on `block_id` (the
block a transaction belongs to) and, of course, on its own `id`. We first wrote
the question the obvious way:

```sql
SELECT min(id), max(id) FROM tx WHERE block_id BETWEEN 8000177 AND 8010177;
```

This took **over 3 minutes**. Why? The database decided to use the index on `id`
(because we asked for `min(id)`/`max(id)`), walking it from the very top
downwards, checking each row's `block_id` to see if it's in range — and the
matching rows are 113 million entries deep. It used the *wrong* sorted list.

We rephrased it to *seek* using the `block_id` index instead:

```sql
SELECT
  (SELECT id FROM tx WHERE block_id BETWEEN 8000177 AND 8010177
     ORDER BY block_id, id LIMIT 1),                          -- smallest
  (SELECT id FROM tx WHERE block_id BETWEEN 8000177 AND 8010177
     ORDER BY block_id DESC, id DESC LIMIT 1);                -- largest
```

Same answer, but now the database jumps straight to the `block_id` range and
grabs the first/last row. **1.8 seconds.**

The lesson, and a theme you'll see again: *which* index a query can use decides
whether it's instant or unusable. Writing the query so the database can use the
right index is most of the performance work in this tool.

## Vocabulary you'll see in the other docs

- **sequential scan / full table scan** — reading every row (no helpful index).
- **index scan / index seek** — jumping to the rows you want via a sorted index.
- **"the anchor column isn't indexed"** — a warning the tool prints when a table's
  filter column has no index, so narrowing it to a chain window falls back to a
  full scan. (Details in [performance and scaling](../07-performance-and-scaling.md).)

**Next:** [Hashing and fingerprints →](03-hashing-and-fingerprints.md)
