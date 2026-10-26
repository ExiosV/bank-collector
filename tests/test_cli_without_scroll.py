"""The ``_without_scroll`` CLI helper strips ``scroll`` from every record."""

import sys

from bank_collector.cli import _without_scroll  # noqa: E402


def test_without_scroll_removes_the_block():
    profile = {
        "bank": "test",
        "status": "verified",
        "url": "https://test.invalid/",
        "wait_for": "#x",
        "balances": [],
        "transactions": [
            {"row": ".row", "fields": {"x": {"selector": ".x"}}, "scroll": {"max_rounds": 10}},
            {"row": ".other", "fields": {"y": {"selector": ".y"}}},
        ],
    }

    result = _without_scroll(profile)

    assert "scroll" not in result["transactions"][0]
    assert "scroll" not in result["transactions"][1]
    # Non-scroll records are untouched.
    assert result["transactions"][1]["row"] == ".other"


def test_without_scroll_does_not_mutate_the_original():
    profile = {
        "bank": "test",
        "status": "verified",
        "url": "https://test.invalid/",
        "wait_for": "#x",
        "balances": [],
        "transactions": [{"row": ".row", "fields": {"x": {"selector": ".x"}}, "scroll": {"max_rounds": 1}}],
    }

    _without_scroll(profile)

    assert "scroll" in profile["transactions"][0]
