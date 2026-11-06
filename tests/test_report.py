"""Tests for the report module: DB-name labelling and JSON assembly."""

from __future__ import annotations

from db_sync_comparator.model import TableResult
from db_sync_comparator.report import build_json_report, format_db_label, print_summary


def test_format_db_label_local_socket_is_just_the_name():
    # A Unix-socket host (path starting with '/') is local; show only the name.
    assert format_db_label("mainnet_v1", "/var/run/postgresql") == "mainnet_v1"
    assert format_db_label("mainnet_v1", None) == "mainnet_v1"
    assert format_db_label("mainnet_v1", "") == "mainnet_v1"


def test_format_db_label_remote_host_is_qualified():
    # A real TCP host disambiguates same-named DBs on different servers.
    assert format_db_label("cexplorer", "10.0.0.5") == "cexplorer@10.0.0.5"


def test_format_db_label_missing_name():
    assert format_db_label(None, None) == "?"


def test_build_json_report_records_db_names():
    report = build_json_report(
        "old_db",
        "new_db",
        100,
        5,
        200,
        6,
        150,
        4,
        None,
        [],
        [],
        [],
        [],
    )
    assert report["db1"] == "old_db"
    assert report["db2"] == "new_db"
    # existing tip metadata still present
    assert report["db1_tip"] == {"block": 100, "epoch": 5}
    assert report["db2_tip"] == {"block": 200, "epoch": 6}


def _report_with(results: list[TableResult]) -> dict:
    return build_json_report("a", "b", 10, 1, 12, 1, 10, 1, None, [], [], [], results)


def test_json_report_records_localization_truncation_and_error():
    stopped = TableResult(
        "pool_relay",
        "normal",
        status="HASH_DIFF",
        localized=["block_no 1..2: content differs (db1 n=1, db2 n=1)"],
        localized_truncated=True,
    )
    failed = TableResult("tx_out", "giant", status="HASH_DIFF", localize_error="QueryCanceled: timeout")
    clean = TableResult("block", "normal", status="MATCH")
    by_table = {r["table"]: r for r in _report_with([stopped, failed, clean])["results"]}
    assert by_table["pool_relay"]["localized_truncated"] is True
    assert by_table["pool_relay"]["localize_error"] is None
    assert by_table["tx_out"]["localized_truncated"] is False
    assert by_table["tx_out"]["localize_error"] == "QueryCanceled: timeout"
    assert by_table["block"]["localized_truncated"] is False
    assert by_table["block"]["localize_error"] is None


def test_summary_says_when_localization_stopped_early(capsys):
    r = TableResult(
        "pool_relay",
        "normal",
        status="HASH_DIFF",
        note="row counts match but content hash differs",
        localized=[f"block_no {i}..{i}: content differs (db1 n=1, db2 n=1)" for i in range(8)],
        localized_truncated=True,
    )
    print_summary([r], [])
    out = capsys.readouterr().out
    assert "stopped after 8 windows; more differing windows may exist" in out


def test_summary_does_not_claim_more_windows_when_search_finished(capsys):
    r = TableResult("pool_relay", "normal", status="HASH_DIFF", localized=["block_no 1..1: content differs"])
    print_summary([r], [])
    assert "more differing windows may exist" not in capsys.readouterr().out


def test_summary_shows_a_failed_localization(capsys):
    r = TableResult("tx_out", "giant", status="HASH_DIFF", localize_error="QueryCanceled: timeout")
    print_summary([r], [])
    assert "-> localize failed: QueryCanceled: timeout" in capsys.readouterr().out


def test_json_report_lists_lifecycle_columns_capped_at_the_cutoff_epoch():
    r = TableResult(
        "gov_action_proposal", "normal", status="MATCH", epoch_capped_cols=["dropped_epoch", "ratified_epoch"]
    )
    row = _report_with([r, TableResult("block", "normal", status="MATCH")])["results"]
    by_table = {x["table"]: x for x in row}
    assert by_table["gov_action_proposal"]["epoch_capped_cols"] == ["dropped_epoch", "ratified_epoch"]
    assert by_table["block"]["epoch_capped_cols"] == []
