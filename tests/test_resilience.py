"""A DOM torn by a navigation must not abort the run.

The cabinet replaces the rendered list after the code screen and while a
scrolled list appends rows; a handle read at that moment raises "Execution
context was destroyed". These tests break the fake page in the same places
and check that the collector retries once, warns once and keeps what it has.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from bank_collector.collect import collect  # noqa: E402
from fake_page import FakeElement, FakePage  # noqa: E402
from test_scroll import _day, _history, _profile, _record, _scroll, _warnings  # noqa: E402

DESTROYED = "Execution context was destroyed, most likely because of a navigation"


def _dates(result):
    return [row["date"]["value"][:10] for row in result["transactions"]]


def test_a_dead_element_becomes_an_empty_field_and_the_run_goes_on(monkeypatch):
    original = FakeElement.text_content
    calls = {"n": 0}

    def flaky_text_content(self):
        if self.attrs.get("data-test-id") == "title-text_content":
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError(DESTROYED)
        return original(self)

    monkeypatch.setattr(FakeElement, "text_content", flaky_text_content)
    page = FakePage(_history([_day(1, title="Первая"), _day(2, title="Вторая")]))

    result = collect(page, _profile(_record()), timeout_ms=0)

    assert len(result["transactions"]) == 2
    assert result["transactions"][0]["title"]["value"] is None
    assert result["transactions"][1]["title"]["value"] == "Вторая"
    assert len(_warnings(result)) == 1
    assert "reading" in _warnings(result)[0] and DESTROYED in _warnings(result)[0]


def test_a_failed_query_is_retried_once_and_succeeds():
    class OnceBrokenPage(FakePage):
        failures = 1

        def query_all(self, selector):
            if self.failures:
                self.failures -= 1
                raise RuntimeError(DESTROYED)
            return FakePage.query_all(self, selector)

    page = OnceBrokenPage(_history([_day(1), _day(2)]))

    result = collect(page, _profile(_record()), timeout_ms=0)

    assert _dates(result) == ["2026-03-01", "2026-03-02"]
    assert page.failures == 0
    assert _warnings(result) == [], "a successful retry is not a warning"


def test_two_failed_attempts_give_one_warning_and_no_rows():
    class AlwaysBrokenPage(FakePage):
        def query_all(self, selector):
            raise RuntimeError(DESTROYED)

    page = AlwaysBrokenPage(_history([_day(1)]))

    result = collect(page, _profile(_record()), timeout_ms=0)

    assert result["transactions"] == []
    assert _warnings(result) == [
        "transactions: extraction failed after 2 attempts (%s)" % DESTROYED,
        "no transactions extracted",
    ]


def test_a_torn_scroll_round_keeps_the_rows_of_the_earlier_rounds():
    first = _history([_day(1)])
    second = _history([_day(1), _day(2)])

    class TornAfterSecondScrollPage(FakePage):
        def query_all(self, selector):
            if len(self.scrolls) >= 2:
                raise RuntimeError(DESTROYED)
            return FakePage.query_all(self, selector)

    page = TornAfterSecondScrollPage(first, pages=[second])

    result = collect(
        page, _profile(_record(_scroll(stop_after_empty_rounds=2))), timeout_ms=0
    )

    assert _dates(result) == ["2026-03-01", "2026-03-02"]
    assert result["scroll"]["stop"] == "empty"
    assert result["scroll"]["rows"] == 2
    assert all("extraction failed after 2 attempts" in text for text in _warnings(result))
    assert len(_warnings(result)) == 1, "the same failure of every round is reported once"
    assert page.writes == [], "nothing is ever written into the page"
