"""The `tbank` profile and the "flat feed with day headers" support.

The fixtures are synthetic (see ``tests/fixtures/tbank_operations.html``):
every merchant, category and amount is invented. What is pinned down here is
the shape of the feed read from the live page on 2026-10-27 — day headers and
operations are sibling ``<li>`` rows of one ``<ul>``, the date lives on the
header and the list is virtualised (old rows leave the DOM while new ones are
appended) — and the fact that reading it writes nothing into the page.
"""

import os
import sys
from datetime import datetime, timezone

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from bank_collector.collect import collect, extract_section, parse_ru_date  # noqa: E402
from bank_collector.profiles import ProfileError, load_profile  # noqa: E402
from fake_page import FakePage  # noqa: E402

FIXTURES = os.path.join(HERE, "fixtures")
TBANK_URL = "https://www.tbank.ru/mybank/operations/"

#: The day the run pretends to happen on: a header without a year is resolved
#: against it, so the tests must not depend on the real clock.
FIXED_NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)

QA = "data-qa-type"
HEADER = "[%s=atom-operations-feed-header]" % QA
OPERATION = "[%s=atom-operations-feed-operation-root]" % QA
TITLE = "[%s=atom-operations-feed-operation-title]" % QA
AMOUNT = "[%s=atom-operations-feed-operation-amount]" % QA
SUBTITLE = "[%s=atom-operations-feed-operation-subtitle]" % QA
DESCRIPTION = "[%s=atom-operations-feed-operation-description]" % QA

ROW = (
    "ul > li:has(%s), ul > li:has(%s)" % (HEADER, OPERATION)
)


def read_fixture(name):
    with open(os.path.join(FIXTURES, name), "r", encoding="utf-8") as handle:
        return handle.read()


def _warnings(result):
    """The warnings of a run, without the 'no balances' noise of the profile."""
    return [text for text in result["warnings"] if text != "no balances extracted"]


def _profile(scroll_overrides=None):
    """The shipped profile, optionally with different scroll settings."""
    profile = load_profile("tbank")
    if scroll_overrides:
        profile["transactions"][0]["scroll"].update(scroll_overrides)
    return profile


# ---------------------------------------------------------------------------
# A virtualised feed: every scroll replaces the rendered window
# ---------------------------------------------------------------------------


class VirtualFeedPage(FakePage):
    """A page whose list re-renders on every scroll, like a virtualised feed.

    Each entry of *windows* is the HTML rendered after the next scroll; when
    the windows are exhausted the last one stays, so further scrolls add
    nothing (the normal "list stopped growing" signal).
    """

    def __init__(self, windows, url=TBANK_URL):
        FakePage.__init__(self, windows[0], url=url)
        self._windows = list(windows[1:])

    def scroll_by(self, dy):
        self.scrolls.append(("scroll", dy))
        if self._windows:
            self._set_root(self._windows.pop(0))
        return None


def _header(text):
    return (
        "<li><div %s='atom-operations-feed-header'>%s"
        "<span %s='atom-operations-feed-header-amount'>0 ₽</span></div></li>"
        % (QA, text, QA)
    )


def _operation(title, amount, category="Прочее", description=None):
    extra = ""
    if description is not None:
        extra = "<div %s='atom-operations-feed-operation-description'>%s</div>" % (
            QA,
            description,
        )
    return (
        "<li><div %s='atom-operations-feed-operation-root' role='button' "
        "aria-label='Детали операции %s'>"
        "<div %s='atom-operations-feed-operation-title'>%s</div>"
        "<div %s='atom-operations-feed-operation-amount'>%s</div>"
        "<div %s='atom-operations-feed-operation-subtitle'>%s</div>%s"
        "</div></li>"
        % (QA, title, QA, title, QA, amount, QA, category, extra)
    )


def _feed(*rows):
    return "<html><body><ul>%s</ul></body></html>" % "".join(rows)


# ---------------------------------------------------------------------------
# The profile
# ---------------------------------------------------------------------------


def test_the_profile_is_a_read_only_draft():
    profile = load_profile("tbank")

    assert profile["bank"] == "tbank"
    assert profile["status"] == "draft", "the end-to-end numbers are not checked yet"
    assert profile["url"] == TBANK_URL
    assert profile["wait_for"] == OPERATION

    # Balances are a later step: the operations page is all this profile reads.
    assert profile["balances"] == []

    record = profile["transactions"][0]
    assert record["header"]["selector"] == HEADER
    assert record["header"]["fields"]["date"]["parse"] == "ru_date"
    assert record["fields"]["description"]["optional"] is True
    assert "items" not in record or record["items"] is None

    scroll = record["scroll"]
    assert scroll["step_px"] == 800, "smaller than the viewport, so rounds overlap"
    assert scroll["settle_ms"] == 1500
    assert scroll["stop_after_empty_rounds"] == 6
    assert scroll["max_rounds"] == 400
    assert scroll["stop_before_date"] == "2026-01-01"
    assert scroll.get("load_more_text") is None, "no clicks of any kind"


# ---------------------------------------------------------------------------
# Parsing a Russian day header
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("27 октября", "2026-10-27"),
        ("27 ОКТЯБРЯ", "2026-10-27"),
        ("27 октября 2025", "2025-10-27"),
        ("Сегодня", "2026-10-27"),
        ("вчера", "2026-10-26"),
        # No year and the date is still ahead of the run: last year's December.
        ("29 декабря", "2025-12-29"),
        # The day's total is glued to the date, as the live header renders it.
        ("27 октября−1 234 ₽", "2026-10-27"),
        ("27 октября 1 234 ₽", "2026-10-27"),
        ("1 января 2026", "2026-01-01"),
    ],
)
def test_ru_date_forms(text, expected):
    assert parse_ru_date(text, now=FIXED_NOW) == expected


@pytest.mark.parametrize(
    "text",
    [None, "", "не дата", "31 февраля", "27 месяца"],
)
def test_ru_date_garbage_is_not_a_date(text):
    assert parse_ru_date(text, now=FIXED_NOW) is None


def test_an_unparseable_header_date_warns_and_keeps_the_raw_text():
    record = {
        "row": "li",
        "header": {
            "selector": ".day",
            "fields": {"date": {"selector": ".day", "parse": "ru_date"}},
        },
        "fields": {"amount": {"selector": ".amount"}},
    }
    page = FakePage(
        "<ul><li><div class='day'>скоро</div></li>"
        "<li><span class='amount'>−1 ₽</span></li></ul>"
    )
    warnings = []

    rows = extract_section(page, record, "transactions", warnings, timeout_ms=1, now=FIXED_NOW)

    assert len(rows) == 1
    assert rows[0]["date"]["value"] is None
    assert rows[0]["date"]["raw"] == "скоро"
    assert any("cannot parse ru_date" in text for text in warnings)


# ---------------------------------------------------------------------------
# The synthetic fixture of the real structure
# ---------------------------------------------------------------------------


def test_collect_reads_dates_from_headers_and_signs_from_amounts():
    page = FakePage(read_fixture("tbank_operations.html"), url=TBANK_URL)

    result = collect(page, _profile(), timeout_ms=1, now=FIXED_NOW)

    rows = result["transactions"]
    assert len(rows) == 4

    assert [row["date"]["value"] for row in rows] == [
        "2026-10-27",
        "2026-10-27",
        "2026-10-26",
        "2025-12-29",  # the year is written in the header itself
    ]
    assert [row["title"]["value"] for row in rows] == [
        "Магазин «Пример»",
        "Кофейня «Пример»",
        "Зарплата (тест)",
        "Старый магазин (тест)",
    ]
    assert [row["category"]["value"] for row in rows] == [
        "Супермаркеты",
        "Кафе и рестораны",
        "Переводы",
        "Супермаркеты",
    ]
    # U+2212 is a minus; the plus stays a plus.
    assert [row["amount"]["value"] for row in rows] == [
        "-999",
        "-250",
        "9999",
        "-1000",
    ]
    assert all(row["amount"]["currency"] == "RUB" for row in rows)

    # The header keeps its whole text as ``raw``, the date only as ``value``.
    assert rows[0]["date"]["raw"].startswith("27 октября")

    # An operation without a description keeps an empty field and stays silent.
    assert rows[0]["description"]["value"] == "Кэшбэк 5 ₽"
    assert rows[1]["description"]["value"] is None
    assert not [text for text in result["warnings"] if "description" in text]

    assert _warnings(result) == []
    assert result["scroll"]["stop"] == "date"
    assert result["scroll"]["complete"] is True
    assert result["scroll"]["oldest_date"] == "2025-12-29"
    assert result["scroll"]["headerless_rows"] == 0
    assert result["fetched_at"] == "2026-10-27T12:00:00Z"
    assert page.writes == [], "the run must not click or fill anything"


# ---------------------------------------------------------------------------
# Virtualisation: the rendered window shrinks and grows between rounds
# ---------------------------------------------------------------------------


def test_a_virtualised_window_drops_old_rows_without_duplicating_them():
    """Two equal coffees on one day stay two rows, even across re-renders."""
    window_one = _feed(
        _header("27 октября"),
        _operation("Кофе (тест)", "−100 ₽", "Кафе и рестораны"),
        _operation("Кофе (тест)", "−100 ₽", "Кафе и рестораны"),
    )
    window_two = _feed(
        _operation("Кофе (тест)", "−100 ₽", "Кафе и рестораны"),
        _operation("Кофе (тест)", "−100 ₽", "Кафе и рестораны"),
        _header("26 октября"),
        _operation("Чай (тест)", "−50 ₽", "Кафе и рестораны"),
    )
    page = VirtualFeedPage([window_one, window_two])

    result = collect(
        page,
        _profile({"stop_before_date": None, "stop_after_empty_rounds": 2, "max_rounds": 10}),
        timeout_ms=1,
        now=FIXED_NOW,
    )

    rows = result["transactions"]
    assert [row["date"]["value"] for row in rows] == [
        "2026-10-27",
        "2026-10-27",
        "2026-10-26",
    ]
    assert [row["title"]["value"] for row in rows] == [
        "Кофе (тест)",
        "Кофе (тест)",
        "Чай (тест)",
    ]
    assert result["scroll"]["stop"] == "empty"
    assert result["scroll"]["complete"] is True
    # The two coffees rendered before the first header of the second window:
    # skipped there, but already kept from the first window. The counter adds
    # up the passes, so only its lower bound is pinned.
    assert result["scroll"]["headerless_rows"] >= 2
    assert page.writes == []


def test_a_header_that_scrolled_out_keeps_the_rows_read_earlier():
    window_one = _feed(
        _header("27 октября"),
        _operation("Магазин А (тест)", "−10 ₽"),
        _operation("Магазин Б (тест)", "−20 ₽"),
    )
    window_two = _feed(
        _operation("Магазин А (тест)", "−10 ₽"),
        _operation("Магазин Б (тест)", "−20 ₽"),
        _header("26 октября"),
        _operation("Магазин В (тест)", "−30 ₽"),
    )
    page = VirtualFeedPage([window_one, window_two])

    result = collect(
        page,
        _profile({"stop_before_date": None, "stop_after_empty_rounds": 2, "max_rounds": 10}),
        timeout_ms=1,
        now=FIXED_NOW,
    )

    assert [row["title"]["value"] for row in result["transactions"]] == [
        "Магазин А (тест)",
        "Магазин Б (тест)",
        "Магазин В (тест)",
    ]
    assert [row["date"]["value"] for row in result["transactions"]] == [
        "2026-10-27",
        "2026-10-27",
        "2026-10-26",
    ]
    assert result["scroll"]["headerless_rows"] >= 2
    assert result["scroll"]["stop"] == "empty"
    assert page.writes == []


def test_virtualised_scrolling_reaches_the_stop_date_and_is_complete():
    window_one = _feed(
        _header("27 октября"),
        _operation("Магазин А (тест)", "−10 ₽"),
        _operation("Магазин Б (тест)", "−20 ₽"),
    )
    window_two = _feed(
        _operation("Магазин А (тест)", "−10 ₽"),
        _operation("Магазин Б (тест)", "−20 ₽"),
        _header("26 октября"),
        _operation("Магазин В (тест)", "−30 ₽"),
    )
    window_three = _feed(
        _header("26 октября"),
        _operation("Магазин В (тест)", "−30 ₽"),
        _operation("Магазин Г (тест)", "−40 ₽"),
        _header("29 декабря 2025"),
        _operation("Магазин Д (тест)", "−50 ₽"),
    )
    page = VirtualFeedPage([window_one, window_two, window_three])

    result = collect(page, _profile(), timeout_ms=1, now=FIXED_NOW)

    assert [row["title"]["value"] for row in result["transactions"]] == [
        "Магазин А (тест)",
        "Магазин Б (тест)",
        "Магазин В (тест)",
        "Магазин Г (тест)",
        "Магазин Д (тест)",
    ]
    assert result["scroll"]["stop"] == "date"
    assert result["scroll"]["complete"] is True
    assert result["scroll"]["oldest_date"] == "2025-12-29"
    assert result["scroll"]["headerless_rows"] >= 2
    assert _warnings(result) == []
    assert page.writes == []


# ---------------------------------------------------------------------------
# Profile validation
# ---------------------------------------------------------------------------


def _write_profile(tmp_path, body):
    path = tmp_path / "tbank_broken.yaml"
    path.write_text(
        "bank: tbank_broken\n"
        "status: draft\n"
        'url: "https://www.tbank.ru/mybank/operations/"\n'
        'wait_for: "#x"\n'
        "balances: []\n"
        "transactions:\n" + body,
        encoding="utf-8",
    )
    return load_profile("tbank_broken", directory=str(tmp_path))


def test_header_and_items_cannot_be_combined(tmp_path):
    body = (
        "  - row: 'li'\n"
        "    fields:\n"
        "      amount:\n"
        "        selector: '.amount'\n"
        "    header:\n"
        "      selector: '.day'\n"
        "      fields:\n"
        "        date:\n"
        "          selector: '.day'\n"
        "    items:\n"
        "      row: 'span'\n"
        "      fields:\n"
        "        title:\n"
        "          selector: '.title'\n"
    )
    with pytest.raises(ProfileError, match="header"):
        _write_profile(tmp_path, body)


def test_unknown_parse_kind_is_rejected(tmp_path):
    body = (
        "  - row: 'li'\n"
        "    header:\n"
        "      selector: '.day'\n"
        "      fields:\n"
        "        date:\n"
        "          selector: '.day'\n"
        "          parse: en_date\n"
        "    fields:\n"
        "      amount:\n"
        "        selector: '.amount'\n"
    )
    with pytest.raises(ProfileError, match="parse"):
        _write_profile(tmp_path, body)


def test_optional_must_be_a_boolean(tmp_path):
    body = (
        "  - row: 'li'\n"
        "    fields:\n"
        "      description:\n"
        "        selector: '.description'\n"
        "        optional: 'yes'\n"
    )
    with pytest.raises(ProfileError, match="optional"):
        _write_profile(tmp_path, body)


def test_header_selector_must_be_a_non_empty_string(tmp_path):
    body = (
        "  - row: 'li'\n"
        "    header:\n"
        "      selector: ''\n"
        "      fields:\n"
        "        date:\n"
        "          selector: '.day'\n"
        "    fields:\n"
        "      amount:\n"
        "        selector: '.amount'\n"
    )
    with pytest.raises(ProfileError, match="header.selector"):
        _write_profile(tmp_path, body)
