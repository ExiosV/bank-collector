"""A short run must not overwrite a longer history already on disk."""

import json

from bank_collector.cli import _write_outputs


def _report(rows, stop):
    return {
        "bank": "alfa",
        "balances": [],
        "transactions": [{"n": index} for index in range(rows)],
        "warnings": [],
        "scroll": {"rounds": 3, "rows": rows, "stop": stop, "oldest_date": "2026-09-01"},
    }


def _write_previous(tmp_path, rows):
    path = tmp_path / "alfa.json"
    path.write_text(json.dumps({"transactions": [{"n": index} for index in range(rows)]}))
    return path


def test_a_shorter_unfinished_run_goes_to_a_partial_file(tmp_path):
    previous = _write_previous(tmp_path, 300)

    written, kept = _write_outputs(_report(20, "empty"), str(tmp_path), "json", "alfa")

    assert kept == str(previous)
    assert written == [str(tmp_path / "alfa.partial.json")]
    assert len(json.loads(previous.read_text())["transactions"]) == 300
    assert len(json.loads((tmp_path / "alfa.partial.json").read_text())["transactions"]) == 20


def test_a_run_that_reached_the_date_always_replaces_the_file(tmp_path):
    previous = _write_previous(tmp_path, 300)

    written, kept = _write_outputs(_report(20, "date"), str(tmp_path), "json", "alfa")

    assert kept is None
    assert written == [str(previous)]
    assert len(json.loads(previous.read_text())["transactions"]) == 20


def test_a_longer_run_replaces_the_file(tmp_path):
    previous = _write_previous(tmp_path, 20)

    written, kept = _write_outputs(_report(300, "empty"), str(tmp_path), "json", "alfa")

    assert kept is None
    assert len(json.loads(previous.read_text())["transactions"]) == 300


def test_the_first_run_writes_the_file(tmp_path):
    written, kept = _write_outputs(_report(5, "error"), str(tmp_path), "json", "alfa")

    assert kept is None
    assert written == [str(tmp_path / "alfa.json")]
