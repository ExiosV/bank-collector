"""A short run must not overwrite a longer history already on disk."""

import json

from bank_collector.cli import _incomplete, _write_outputs


def _report(rows, stop=None):
    report = {
        "bank": "alfa",
        "balances": [],
        "transactions": [{"n": index} for index in range(rows)],
        "warnings": [],
    }
    if stop is not None:
        report["scroll"] = {
            "rounds": 3,
            "rows": rows,
            "stop": stop,
            "oldest_date": "2026-09-01",
            "complete": stop == "date",
        }
    return report


def _write_previous(tmp_path, rows, scrolled=True):
    path = tmp_path / "alfa.json"
    previous = {"transactions": [{"n": index} for index in range(rows)]}
    if scrolled:
        previous["scroll"] = {"stop": "date", "complete": True}
    path.write_text(json.dumps(previous))
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


def test_a_no_scroll_run_does_not_replace_a_scrolled_history(tmp_path):
    previous = _write_previous(tmp_path, 300)

    written, kept = _write_outputs(_report(20), str(tmp_path), "json", "alfa")

    assert kept == str(previous)
    assert len(json.loads(previous.read_text())["transactions"]) == 300


def test_an_empty_run_does_not_replace_the_file(tmp_path):
    previous = _write_previous(tmp_path, 5, scrolled=False)

    written, kept = _write_outputs(_report(0), str(tmp_path), "both", "alfa")

    assert kept == str(previous)
    assert written == [str(tmp_path / "alfa.partial.json")]
    assert not (tmp_path / "alfa-transactions.csv").exists()


def test_a_bank_without_scrolling_is_not_held_back(tmp_path):
    """The first screen of a bank without scrolling changes every day."""
    previous = _write_previous(tmp_path, 20, scrolled=False)

    written, kept = _write_outputs(_report(19), str(tmp_path), "json", "alfa")

    assert kept is None
    assert len(json.loads(previous.read_text())["transactions"]) == 19


def test_an_unfinished_scroll_is_never_a_success():
    assert _incomplete(_report(20, "empty")) is True
    assert _incomplete(_report(20, "date")) is False
    assert _incomplete(_report(20)) is False, "no scroll, nothing to judge"


def test_balances_without_transactions_do_not_replace_the_history(tmp_path):
    previous = _write_previous(tmp_path, 20, scrolled=False)
    report = _report(0)
    report["balances"] = [{"amount": 1}]

    written, kept = _write_outputs(report, str(tmp_path), "json", "alfa")

    assert kept == str(previous)
    assert len(json.loads(previous.read_text())["transactions"]) == 20
