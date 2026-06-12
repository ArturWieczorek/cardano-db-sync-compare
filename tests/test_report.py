"""Tests for the report module: DB-name labelling and JSON assembly."""

from __future__ import annotations

from db_sync_comparator.report import build_json_report, format_db_label


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
