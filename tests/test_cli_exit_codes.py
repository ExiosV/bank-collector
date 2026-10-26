"""The exit code is the whole contract with the daily job: pin it."""

import io

from bank_collector.cli import EXIT_INCOMPLETE, EXIT_NOTHING, EXIT_OK, _exit_code


def _report(transactions=1, balances=1, scroll=None):
    report = {
        "balances": [{}] * balances,
        "transactions": [{}] * transactions,
        "warnings": [],
    }
    if scroll is not None:
        report["scroll"] = scroll
    return report


COMPLETE = {"stop": "date", "oldest_date": "2025-12-31", "complete": True}
SHORT = {"stop": "empty", "oldest_date": "2026-09-01", "complete": False}


def test_a_complete_run_is_a_success():
    assert _exit_code(_report(scroll=COMPLETE), ["alfa.json"], None, io.StringIO()) == EXIT_OK


def test_a_bank_without_scrolling_is_a_success():
    assert _exit_code(_report(), ["alfa.json"], None, io.StringIO()) == EXIT_OK


def test_a_short_history_is_not_a_success_even_without_a_file_to_protect():
    stream = io.StringIO()

    assert _exit_code(_report(scroll=SHORT), ["alfa.json"], None, stream) == EXIT_INCOMPLETE
    assert "history incomplete: it reaches back only to 2026-09-01" in stream.getvalue()


def test_a_kept_snapshot_is_not_a_success():
    stream = io.StringIO()

    code = _exit_code(_report(scroll=SHORT), ["alfa.partial.json"], "alfa.json", stream)

    assert code == EXIT_INCOMPLETE
    assert "it was kept, the new report is in alfa.partial.json" in stream.getvalue()


def test_an_empty_run_says_that_the_snapshot_was_kept_and_exits_4():
    stream = io.StringIO()

    code = _exit_code(_report(0, 0), ["alfa.partial.json"], "alfa.json", stream)

    assert code == EXIT_NOTHING
    assert "it was kept" in stream.getvalue()
    assert "nothing was extracted" in stream.getvalue()
