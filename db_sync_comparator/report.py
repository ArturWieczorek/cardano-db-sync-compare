"""Human-readable summary and machine-readable JSON report."""

from __future__ import annotations

import json

from db_sync_comparator.model import TablePlan, TableResult


def print_summary(results: list[TableResult], excluded: list[TablePlan]) -> tuple[list[TableResult], list[TableResult]]:
    """Print the final summary and return ``(hard_discrepancies, errors)``.

    "Hard" discrepancies are real differences (HASH_DIFF / VALUE_DIFF, or a
    COUNT_DIFF on a non-accumulator table); accumulator count-deltas are
    informational only.
    """
    n_match = sum(1 for r in results if r.status == "MATCH")
    n_acc = sum(1 for r in results if r.status == "COUNT_DIFF" and r.kind == "accumulator")
    hard = [
        r
        for r in results
        if r.status in ("HASH_DIFF", "VALUE_DIFF") or (r.status == "COUNT_DIFF" and r.kind != "accumulator")
    ]
    errors = [r for r in results if r.status == "ERROR"]

    print("\n" + "=" * 78)
    print(
        f"SUMMARY: {n_match} match, {len(hard)} discrepancies, "
        f"{n_acc} accumulator count-deltas (informational), "
        f"{len(errors)} errors, {len(excluded)} excluded"
    )
    if hard:
        print("\nDISCREPANCIES:")
        for r in hard:
            print(f"  {r.name}: {r.status} — {r.note}")
            for line in r.localized:
                print(f"      -> {line}")
            if r.skipped_cols:
                print(f"      (note: columns not hashed: {r.skipped_cols})")
    if errors:
        print("\nERRORS:")
        for r in errors:
            print(f"  {r.name}: {r.note}")
    print("=" * 78)
    return hard, errors


def build_json_report(
    bn1: int,
    en1: int,
    bn2: int,
    en2: int,
    cutoff_block: int,
    cutoff_epoch: int,
    block_range: tuple[int, int] | None,
    only_db1: list[str],
    only_db2: list[str],
    excluded: list[TablePlan],
    results: list[TableResult],
) -> dict:
    """Assemble the structured report written by ``--json``."""
    return {
        "db1_tip": {"block": bn1, "epoch": en1},
        "db2_tip": {"block": bn2, "epoch": en2},
        "cutoff": {"block": cutoff_block, "epoch": cutoff_epoch},
        "block_range": block_range,
        "tables_only_db1": only_db1,
        "tables_only_db2": only_db2,
        "excluded": {p.name: p.reason for p in excluded},
        "results": [
            {
                "table": r.name,
                "kind": r.kind,
                "status": r.status,
                "n1": r.n1,
                "n2": r.n2,
                "hash1": list(r.h1),
                "hash2": list(r.h2),
                "value1": r.value1,
                "value2": r.value2,
                "note": r.note,
                "seconds": round(r.seconds, 2),
                "skipped_cols": r.skipped_cols,
                "schema_drift": r.schema_drift,
                "localized": r.localized,
            }
            for r in sorted(results, key=lambda x: x.name)
        ],
    }


def write_json_report(path: str, report: dict) -> None:
    with open(path, "w") as fh:
        json.dump(report, fh, indent=2, default=str)
    print(f"report written to {path}")
