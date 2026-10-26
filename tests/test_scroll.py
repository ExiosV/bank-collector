"""Scrolling the rendered history list — read-only, without a browser.

The Alfa-Bank cabinet shows 20 operations per screen and appends the next
ones while the list is scrolled.  These tests drive that loop against the
fake page: the rows of every round are collected, the repeats are dropped,
the loop stops for the right reason and — most of all — nothing is ever
written into the page.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from bank_collector.collect import collect  # noqa: E402
from fake_page import FakePage, parse_html  # noqa: E402

DATE_FORMAT = "%sT10:00:00.000+0300"


def _history(days):
    """An Alfa-like history page: one group per day, one operation per group."""
    parts = ["<html><body><div class='operations'>"]
    for date, title, amount in days:
        parts.append(
            "<div>"
            "<div data-date='%s'><span>день</span></div>"
            "<button data-test-id='operation-cell'>"
            "<div data-test-id='title-text_content'>%s</div>"
            "<div data-test-id='transaction-category-name'>Прочее</div>"
            "<div data-test-id='transaction-status'>%s ₽</div>"
            "</button></div>" % (DATE_FORMAT % date, title, amount)
        )
    parts.append("</div></body></html>")
    return "".join(parts)


def _day(number, title="Оплата", amount="-100,00"):
    return ("2026-03-%02d" % number, title, amount)


def _history_without_amount(days):
    """The same history, but the operations have no amount at all."""
    parts = ["<html><body><div class='operations'>"]
    for date, title, _amount in days:
        parts.append(
            "<div>"
            "<div data-date='%s'><span>день</span></div>"
            "<button data-test-id='operation-cell'>"
            "<div data-test-id='title-text_content'>%s</div>"
            "<div data-test-id='transaction-category-name'>Прочее</div>"
            "</button></div>" % (DATE_FORMAT % date, title)
        )
    parts.append("</div></body></html>")
    return "".join(parts)


def _grow(page):
    """Append one more operation group to the page, like an SPA does."""
    day = "2026-03-%02d" % (len(page.scrolls) + 10)
    fragment = parse_html(_history([_day(int(day[-2:]), title="Новая")]))
    for group in fragment.children[0].children:
        page.root.append_child(group)


def _break(page):
    """Replace the page by one whose operation has no amount at all."""
    page.html = _history_without_amount([_day(1)])
    page.root = parse_html(page.html)


def _record(scroll=None):
    record = {
        "row": "div:has(> [data-date]):has(> button[data-test-id=operation-cell])",
        "fields": {"date": {"selector": "[data-date]", "attr": "data-date"}},
        "items": {
            "row": "button[data-test-id=operation-cell]",
            "fields": {
                "title": {"selector": "[data-test-id=title-text_content]"},
                "category": {"selector": "[data-test-id=transaction-category-name]"},
                "amount": {"selector": "[data-test-id=transaction-status]"},
            },
        },
    }
    if scroll is not None:
        record["scroll"] = scroll
    return record


def _profile(record):
    return {
        "bank": "synthetic",
        "status": "verified",
        "url": "https://synthetic.invalid/history/",
        "wait_for": "",
        "balances": [],
        "transactions": [record],
    }


def _scroll(**overrides):
    config = {
        "max_rounds": 20,
        "step_px": 1200,
        "settle_ms": 0,
        "stop_after_empty_rounds": 2,
        "stop_before_date": None,
    }
    config.update(overrides)
    return config


def _dates(result):
    return [row["date"]["value"][:10] for row in result["transactions"]]


def _warnings(result):
    """The warnings of a run, without the 'no balances' noise of the fixture."""
    return [text for text in result["warnings"] if text != "no balances extracted"]


def test_scrolling_collects_the_rows_past_the_first_screen():
    first = _history([_day(1), _day(2)])
    second = _history([_day(1), _day(2), _day(3)])
    third = _history([_day(1), _day(2), _day(3), _day(4)])
    page = FakePage(first, pages=[second, third])

    result = collect(page, _profile(_record(_scroll())))

    assert _dates(result) == ["2026-03-01", "2026-03-02", "2026-03-03", "2026-03-04"]
    assert result["scroll"]["stop"] == "empty"
    assert result["scroll"]["rows"] == 4
    assert len(page.scrolls) == 4, "two rounds with new rows and two empty ones"


def test_rows_repeated_between_the_rounds_are_not_duplicated():
    same = _history([_day(1), _day(2)])
    page = FakePage(same, pages=[same, same])

    result = collect(page, _profile(_record(_scroll())))

    assert len(result["transactions"]) == 2
    assert _dates(result) == ["2026-03-01", "2026-03-02"]
    assert result["scroll"]["stop"] == "empty"


def test_the_scroll_limit_is_reported_with_a_warning():
    page = FakePage(_history([_day(1)]), on_scroll=_grow)

    result = collect(page, _profile(_record(_scroll(max_rounds=2))))

    assert result["scroll"]["stop"] == "limit"
    assert result["scroll"]["rounds"] == 2
    assert result["scroll"]["rows"] == 3, "the first screen plus two rounds"
    assert _warnings(result) == [
        "transactions: scroll limit reached after 2 rounds (3 rows, history may be incomplete)"
    ]


def test_the_scrolling_stops_when_the_list_stops_growing():
    first = _history([_day(1)])
    second = _history([_day(1), _day(2)])
    page = FakePage(first, pages=[second])

    result = collect(page, _profile(_record(_scroll(max_rounds=20, stop_after_empty_rounds=2))))

    assert result["scroll"]["stop"] == "empty"
    assert result["scroll"]["rounds"] == 3, "one round with a new row and two empty ones"
    assert _warnings(result) == [], "a normal stop is not a warning"


def test_the_scrolling_stops_at_the_date_of_the_profile():
    old = _history([_day(1), ("2025-12-31", "Старая операция", "-1,00")])
    page = FakePage(_history([_day(1)]), pages=[old])

    result = collect(page, _profile(_record(_scroll(stop_before_date="2026-01-01"))))

    assert result["scroll"]["stop"] == "date"
    assert result["scroll"]["oldest_date"] == "2025-12-31"
    assert _dates(result) == ["2026-03-01", "2025-12-31"]
    assert _warnings(result) == []


def test_a_row_on_the_date_itself_does_not_stop_the_scrolling():
    """The day of the limit may arrive in two batches: only an older row stops."""
    on_the_day = _history([_day(1), ("2026-01-01", "Первая", "-1,00")])
    more_of_the_day = _history(
        [_day(1), ("2026-01-01", "Первая", "-1,00"), ("2026-01-01", "Вторая", "-2,00")]
    )
    page = FakePage(_history([_day(1)]), pages=[on_the_day, more_of_the_day])

    result = collect(page, _profile(_record(_scroll(stop_before_date="2026-01-01"))))

    assert _dates(result) == ["2026-03-01", "2026-01-01", "2026-01-01"]
    assert result["scroll"]["stop"] == "empty"
    assert _warnings(result) == [], "the history reached the date: it is complete"


def test_a_list_that_stops_short_of_the_date_is_reported():
    page = FakePage(_history([_day(1)]), pages=[_history([_day(1), _day(2)])])

    result = collect(page, _profile(_record(_scroll(stop_before_date="2026-01-01"))))

    assert result["scroll"]["stop"] == "empty"
    assert result["scroll"]["oldest_date"] == "2026-03-01"
    assert _warnings(result) == [
        "transactions: history reaches back only to 2026-03-01, not to 2026-01-01 "
        "(stop: empty, history may be incomplete)"
    ]


def test_equal_operations_of_one_day_are_kept_when_they_appear_in_a_scroll():
    """Two equal coffees on one day are two operations, not a repeat."""
    coffee = _day(2, title="Кофе", amount="-250,00")
    first = _history([_day(1)])
    second = _history([_day(1), coffee, coffee])
    page = FakePage(first, pages=[second, second])

    result = collect(page, _profile(_record(_scroll())))

    assert _dates(result) == ["2026-03-01", "2026-03-02", "2026-03-02"]
    assert result["scroll"]["rows"] == 3


def test_a_broken_scroll_keeps_the_rows_and_warns_once():
    class BrokenScrollPage(FakePage):
        def scroll_by(self, dy):
            raise RuntimeError("the wheel fell off")

    page = BrokenScrollPage(_history([_day(1), _day(2)]))

    result = collect(page, _profile(_record(_scroll())))

    assert len(result["transactions"]) == 2
    assert result["scroll"]["stop"] == "error"
    assert _warnings(result) == ["transactions: scrolling stopped (the wheel fell off)"]


def test_without_a_scroll_block_nothing_is_scrolled():
    page = FakePage(_history([_day(1)]))

    result = collect(page, _profile(_record()))

    assert page.scrolls == []
    assert "scroll" not in result
    assert len(result["transactions"]) == 1


def test_a_scrolled_run_writes_nothing_into_the_page():
    """Scrolling is read-only: no fill, no click (as in test_unlock.py)."""
    page = FakePage(_history([_day(1)]), on_scroll=_grow)

    collect(page, _profile(_record(_scroll(max_rounds=3))))

    assert page.writes == []
    assert page.scrolls, "the page must have been scrolled"


def test_the_warnings_of_the_scrolled_rounds_are_not_repeated():
    """A broken selector must not be reported once per round."""
    page = FakePage(_history([_day(1), _day(2)]), on_scroll=_break)

    result = collect(page, _profile(_record(_scroll(max_rounds=3, stop_after_empty_rounds=1))))

    scrolled = [text for text in _warnings(result) if text.startswith("scrolled row: ")]
    assert scrolled, "the broken row must be reported"
    assert len(scrolled) == len(set(scrolled)), "no message twice"
    assert len(scrolled) <= 5, "at most five messages"


def test_the_profile_keeps_the_order_of_the_first_screen():
    first = _history([_day(3), _day(2), _day(1)])
    page = FakePage(first, pages=[_history([_day(3), _day(2), _day(1), _day(4)])])

    result = collect(page, _profile(_record(_scroll())))

    assert _dates(result) == ["2026-03-03", "2026-03-02", "2026-03-01", "2026-03-04"]
