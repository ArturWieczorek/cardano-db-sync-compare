"""Command-line entry point: parse arguments and orchestrate the comparison.

See ``docs/05-running-it.md`` for the full option reference and how to read the
output.
"""

from __future__ import annotations

import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from db_sync_comparator import __version__
from db_sync_comparator.compare import compare_table, localize
from db_sync_comparator.db import connect
from db_sync_comparator.model import TablePlan, TableResult
from db_sync_comparator.planning import plan_table
from db_sync_comparator.ranges import compute_spine_ranges, get_tip
from db_sync_comparator.report import build_json_report, print_summary, write_json_report
from db_sync_comparator.schema import indexed_columns, introspect
from db_sync_comparator.sql import bound_predicate, hash_sql


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="db-sync-compare",
        description="Compare two cardano-db-sync databases for chain-content equivalence.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument(
        "--db1", required=True, help="psycopg/libpq conninfo for DB 1 (e.g. 'dbname=... host=/var/run/postgresql')"
    )
    ap.add_argument("--db2", required=True, help="conninfo for DB 2")
    ap.add_argument("--pgpass", help="path to a pgpass file (sets PGPASSFILE)")
    ap.add_argument("--workers", type=int, default=4, help="parallel tables (each uses 2 connections)")
    ap.add_argument("--full", action="store_true", help="full per-column deep hash on giant tables too")
    ap.add_argument(
        "--giant-fk-depth", type=int, default=1, help="max FK-resolution depth for giant tables in tiered mode"
    )
    ap.add_argument("--cutoff-block", type=int, help="override common boundary block_no")
    ap.add_argument(
        "--block-margin",
        type=int,
        default=0,
        help="blocks to pull the common cutoff back below the lower tip, to stay out of the "
        "volatile near-tip rollback zone (mainnet security parameter k ~= 2160)",
    )
    ap.add_argument(
        "--epoch-margin",
        type=int,
        default=2,
        help="epochs to subtract from the cutoff epoch (avoid the in-progress epoch)",
    )
    ap.add_argument(
        "--block-range", help="LO:HI — compare only this block window (skips epoch tables); great for fast validation"
    )
    ap.add_argument("--tables", help="comma-separated subset of tables to compare")
    ap.add_argument("--no-localize", action="store_true", help="skip Merkle bisection of mismatches")
    ap.add_argument("--statement-timeout", type=int, default=0, help="per-statement timeout in ms (0 = none)")
    ap.add_argument(
        "--work-mem", default="256MB", help="work_mem per session (helps the big FK-translation hash joins)"
    )
    ap.add_argument(
        "--plan",
        action="store_true",
        help="print the comparison plan + generated SQL and exit (no data access for hashing)",
    )
    ap.add_argument("--json", dest="json_path", help="write a structured report to this path")
    return ap


def parse_block_range(spec: str | None) -> tuple[int, int] | None:
    if not spec:
        return None
    lo, hi = spec.split(":")
    return (int(lo), int(hi))


def _print_plan(plans: list[TablePlan], ranges1: dict, cutoff_epoch: int, in_block_range: bool) -> None:
    for p in plans:
        print("-" * 78)
        print(f"{p.name}  [{p.kind}]" + (f"  EXCLUDED: {p.reason}" if p.kind == "excluded" else ""))
        if p.kind == "excluded":
            continue
        if p.anchor_kind == "idrange":
            print(f"  anchor: id-range on t0.{p.anchor_col} via spine '{p.spine}'")
        elif p.anchor_kind == "epoch":
            print(f"  anchor: epoch on {p.epoch_expr}")
        else:
            print("  anchor: none (accumulator — full-table compare)")
        if p.skipped_cols:
            print(f"  columns dropped (FK over depth budget): {p.skipped_cols}")
        if p.extra_cols:
            print(f"  schema drift: {p.extra_cols}")
        pred = bound_predicate(p, ranges1, cutoff_epoch, in_block_range)
        print("  " + hash_sql(p, pred).replace("\n", "\n  "))


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    if args.pgpass:
        os.environ["PGPASSFILE"] = args.pgpass
    block_range = parse_block_range(args.block_range)

    try:
        c1 = connect(args.db1, args.work_mem)
        c2 = connect(args.db2, args.work_mem)
    except Exception as exc:
        print(f"connection failed: {exc}", file=sys.stderr)
        return 2

    schema1 = introspect(c1)
    schema2 = introspect(c2)
    common_cols = {t: set(schema1[t].columns) & set(schema2[t].columns) for t in set(schema1) & set(schema2)}

    common_tables = sorted(set(schema1) & set(schema2))
    only_db1 = sorted(set(schema1) - set(schema2))
    only_db2 = sorted(set(schema2) - set(schema1))
    if args.tables:
        wanted = {t.strip() for t in args.tables.split(",")}
        common_tables = [t for t in common_tables if t in wanted]

    bn1, en1 = get_tip(c1)
    bn2, en2 = get_tip(c2)
    cutoff_block = max(0, (args.cutoff_block if args.cutoff_block else min(bn1, bn2)) - args.block_margin)
    cutoff_epoch = max(0, min(en1, en2) - args.epoch_margin)

    print("=" * 78)
    print("cardano-db-sync database comparison")
    print("=" * 78)
    print(f"DB1 tip: block {bn1}, epoch {en1}")
    print(f"DB2 tip: block {bn2}, epoch {en2}")
    print(
        f"common boundary: block_no <= {cutoff_block}, epoch_no <= {cutoff_epoch}"
        + (f"   (block window {block_range[0]}..{block_range[1]})" if block_range else "")
    )
    if only_db1:
        print(f"tables only in DB1 (not compared): {', '.join(only_db1)}")
    if only_db2:
        print(f"tables only in DB2 (not compared): {', '.join(only_db2)}")

    in_block_range = block_range is not None
    ranges1 = compute_spine_ranges(c1, cutoff_block, block_range)
    ranges2 = compute_spine_ranges(c2, cutoff_block, block_range)
    print(f"DB1 spine id-ranges: {ranges1}")
    print(f"DB2 spine id-ranges: {ranges2}")
    print()

    plans = [plan_table(t, schema1[t], schema2[t], common_cols, args.full, args.giant_fk_depth) for t in common_tables]

    # Warn about id-range tables whose anchor column is not indexed: the window
    # bound degrades to a sequential scan (fine for a full run, slow for a
    # narrow window or Phase-2 bisection).
    idx = indexed_columns(c1)
    unindexed = [
        p.name
        for p in plans
        if p.anchor_kind == "idrange" and p.anchor_col != "id" and (p.name, p.anchor_col) not in idx
    ]
    if unindexed:
        print(f"note: anchor column not indexed (window bound = seq scan): {', '.join(unindexed)}")

    if args.plan:
        _print_plan(plans, ranges1, cutoff_epoch, in_block_range)
        return 0

    to_compare = [p for p in plans if p.kind != "excluded"]
    excluded = [p for p in plans if p.kind == "excluded"]

    results: list[TableResult] = []
    print(f"Phase 1: hashing {len(to_compare)} tables with {args.workers} workers ...\n")

    def work(plan: TablePlan) -> TableResult:
        w1 = connect(args.db1, args.work_mem)
        w2 = connect(args.db2, args.work_mem)
        try:
            return compare_table(plan, w1, w2, ranges1, ranges2, cutoff_epoch, in_block_range, args.statement_timeout)
        finally:
            w1.close()
            w2.close()

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(work, p): p for p in to_compare}
        for fut in as_completed(futs):
            r = fut.result()
            results.append(r)
            flag = "OK " if r.status == "MATCH" else "!! "
            print(
                f"  {flag}{r.name:<28} {r.status:<11} n={r.n1}/{r.n2}  {r.seconds:6.1f}s"
                + (f"  {r.note}" if r.note and r.status != "MATCH" else "")
            )

    # Phase 2: localize chain-anchored mismatches only — skip accumulators and
    # one-sided-zero tables (a feature disabled in config isn't worth bisecting).
    mismatches = [
        r
        for r in results
        if r.status in ("HASH_DIFF", "COUNT_DIFF", "VALUE_DIFF") and r.kind != "accumulator" and r.n1 != 0 and r.n2 != 0
    ]
    if mismatches and not args.no_localize and not block_range:
        print("\nPhase 2: localizing mismatches ...")
        plan_by_name = {p.name: p for p in to_compare}
        lc1, lc2 = connect(args.db1, args.work_mem), connect(args.db2, args.work_mem)
        try:
            for r in mismatches:
                p = plan_by_name[r.name]
                if p.anchor_kind == "idrange":
                    lo, hi = 0, cutoff_block
                elif p.anchor_kind == "epoch":
                    lo, hi = 0, cutoff_epoch
                else:
                    continue
                try:
                    r.localized = localize(p, lc1, lc2, lo, hi, cutoff_block, args.statement_timeout)
                    for line in r.localized:
                        print(f"  {r.name}: {line}")
                except Exception as exc:
                    print(f"  {r.name}: localize failed: {exc}")
        finally:
            lc1.close()
            lc2.close()

    hard, errors = print_summary(results, excluded)

    if args.json_path:
        report = build_json_report(
            bn1, en1, bn2, en2, cutoff_block, cutoff_epoch, block_range, only_db1, only_db2, excluded, results
        )
        write_json_report(args.json_path, report)

    return 1 if (hard or errors) else 0
