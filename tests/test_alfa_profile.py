"""The `alfa` profile against synthetic fixtures.

The fixtures reproduce the nesting of the real cabinet (see
``tests/fixtures/alfa_dashboard.html`` and ``alfa_history.html``) but every
name and amount in them is invented. What is tested here is the profile, not
a bank: the balances of the dashboard, and the operations of ``/history/``
where the date belongs to a group of operations and not to the operation
itself.
"""

import os
import sys

import pytest

from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from bank_collector.collect import collect, extract_section  # noqa: E402
from bank_collector.profiles import ProfileError, load_profile  # noqa: E402
from bank_collector.profiles import _SCROLL_DEFAULTS, _SCROLL_KEYS, _SCROLL_MINIMUMS  # noqa: E402
from fake_page import FakePage, parse_html  # noqa: E402

FIXTURES = os.path.join(HERE, "fixtures")

DASHBOARD_URL = "https://web.alfabank.ru/dashboard/"
HISTORY_URL = "https://web.alfabank.ru/history/"


def read_fixture(name):
    with open(os.path.join(FIXTURES, name), "r", encoding="utf-8") as handle:
        return handle.read()


class TwoPageBrowser(FakePage):
    """A fake browser that serves a different fixture per URL."""

    def __init__(self, pages):
        first = next(iter(pages))
        FakePage.__init__(self, pages[first], url=first)
        self.pages = pages

    def goto(self, url, **kwargs):
        self.visited.append(url)
        for known, html in self.pages.items():
            if known.rstrip("/") == url.rstrip("/"):
                self.url = url
                self.root = parse_html(html)
                return None
        raise LookupError("no fixture for %r" % url)


def browser():
    return TwoPageBrowser(
        {
            DASHBOARD_URL: read_fixture("alfa_dashboard.html"),
            HISTORY_URL: read_fixture("alfa_history.html"),
        }
    )


# ---------------------------------------------------------------------------
# Every test that calls `collect` on a scrolled profile must not wait for the
# settle delay (900 ms by default for Alfa-Bank).  Patch it away so that
# these tests run fast — and so that the fixture can be autoused without
# changing existing tests.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def no_sleep():
    """Suppress the settle delay of scrolled runs so tests finish fast."""
    with patch("bank_collector.collect.time.sleep", lambda *a: None):
        yield


def test_alfa_profile_asks_for_two_pages():
    profile = load_profile("alfa")
    assert profile["status"] == "verified"
    assert profile["url"] == DASHBOARD_URL
    assert profile["transactions"][0]["url"] == HISTORY_URL


def test_balances_skip_the_advertising_block_and_keep_the_currency():
    profile = load_profile("alfa")
    result = collect(browser(), profile=profile, timeout_ms=1)

    balances = result["balances"]
    assert len(balances) == 4, "the advertising block has no balance and must be skipped"

    first = balances[0]
    assert first["product"]["value"] == "Карта «Пример»"
    assert first["amount"]["value"] == "12000.00"
    assert first["amount"]["currency"] == "RUB"

    byn = [row for row in balances if row["amount"]["currency"] == "BYN"]
    assert len(byn) == 1 and byn[0]["amount"]["value"] == "1.00"

    # The currency is taken from the page, never guessed.
    assert balances[1]["amount"]["currency"] == "RUB"


def test_operations_inherit_the_date_of_their_group():
    profile = load_profile("alfa")
    result = collect(browser(), profile=profile, timeout_ms=1)

    rows = result["transactions"]
    assert len(rows) == 3, "two groups hold 1 + 2 operations"

    # First group: one operation on 15 January.
    assert rows[0]["date"]["value"] == "2026-01-15T10:00:00.000+0300"
    assert rows[0]["title"]["value"] == "Магазин «Пример»"
    assert rows[0]["category"]["value"] == "Супермаркеты"
    assert rows[0]["amount"]["value"] == "-1250.50"

    # Second group: two operations that share the date of the group.
    assert rows[1]["date"]["value"] == "2026-01-14T09:30:00.000+0300"
    assert rows[2]["date"]["value"] == "2026-01-14T09:30:00.000+0300"
    assert rows[1]["title"]["value"] == "Иван И."
    assert rows[1]["amount"]["value"] == "30000.00"
    assert rows[2]["amount"]["value"] == "-99.00"

    for row in rows:
        assert row["amount"]["currency"] == "RUB"


def test_both_pages_are_reported_as_sources():
    profile = load_profile("alfa")
    result = collect(browser(), profile=profile, timeout_ms=1)

    assert result["source_urls"] == [DASHBOARD_URL, HISTORY_URL]
    assert result["source_url"] == DASHBOARD_URL
    assert result["warnings"] == []


def test_a_field_defined_in_group_and_item_warns_and_the_item_wins():
    profile = {
        "bank": "synthetic",
        "status": "verified",
        "url": "https://synthetic.invalid/",
        "wait_for": "",
        "balances": [],
        "transactions": [
            {
                "row": "[data-date]",
                "fields": {"amount": {"selector": "span"}},
                "items": {"row": "button", "fields": {"amount": {"selector": "b"}}},
            }
        ],
    }
    page = FakePage(
        '<div data-date="2026-01-01"><span>0,00 ₽</span>'
        "<button><b>1,00 ₽</b></button></div>"
    )
    warnings = []
    rows = extract_section(page, profile["transactions"][0], "transactions", warnings, timeout_ms=1)

    assert len(rows) == 1
    assert rows[0]["amount"]["value"] == "1.00", "the item must win over the group"
    assert any("defined both in the group and in the item" in text for text in warnings)


def test_items_cannot_be_nested(tmp_path):
    path = tmp_path / "nested.yaml"
    path.write_text(
        "bank: nested\n"
        "status: unverified\n"
        'url: "https://nested.invalid/"\n'
        'wait_for: "#x"\n'
        "transactions:\n"
        '  - row: "#group"\n'
        "    fields:\n"
        "      date:\n"
        '        selector: "[data-date]"\n'
        "    items:\n"
        '      row: "button"\n'
        "      fields:\n"
        "        amount:\n"
        '          selector: "b"\n'
        "      items:\n"
        '        row: "span"\n'
        "        fields:\n"
        "          x:\n"
        '            selector: "i"\n',
        encoding="utf-8",
    )
    from bank_collector.profiles import load_profile as load_from

    with pytest.raises(ProfileError, match="items"):
        load_from("nested", directory=str(tmp_path))


def test_alfa_scroll_block_has_expected_values():
    """The Alfa-Bank profile must spell out the scroll limits it uses."""
    profile = load_profile("alfa")
    scroll = profile["transactions"][0]["scroll"]

    assert scroll["max_rounds"] == 200
    assert scroll["step_px"] == 1200
    assert scroll["settle_ms"] == 900
    assert scroll["stop_after_empty_rounds"] == 4
    assert scroll["stop_before_date"] == "2026-01-01"
