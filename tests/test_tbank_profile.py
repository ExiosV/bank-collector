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
from zoneinfo import ZoneInfoNotFoundError

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import bank_collector.collect as collect_module  # noqa: E402
import bank_collector.profiles as profiles_module  # noqa: E402
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


def test_the_profile_is_read_only_and_verified():
    profile = load_profile("tbank")

    assert profile["bank"] == "tbank"
    # Verified live on 2026-10-27: day expense totals match the cabinet's own headers.
    assert profile["status"] == "verified"
    assert profile["url"] == TBANK_URL
    assert profile["wait_for"] == OPERATION
    # Day headers are read in the cabinet's own timezone, not UTC.
    assert profile["timezone"] == "Europe/Moscow"

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


def test_ru_date_is_anchored_on_the_cabinet_local_day():
    """01:30 MSK on 1 January is still 31 December in UTC — the cabinet wins."""
    now = datetime(2026, 12, 31, 22, 30, tzinfo=timezone.utc)
    page = FakePage(
        _feed(
            _header("Сегодня−1 ₽"),
            _operation("Сегодня (тест)", "−1 ₽"),
            _header("1 января"),
            _operation("Первое (тест)", "−2 ₽"),
            _header("31 декабря"),
            _operation("Тридцать первое (тест)", "−3 ₽"),
        ),
        url=TBANK_URL,
    )

    result = collect(
        page,
        _profile(
            {
                "stop_before_date": None,
                "stop_after_empty_rounds": 1,
                "max_rounds": 1,
                "settle_ms": 0,
            }
        ),
        timeout_ms=1,
        now=now,
    )

    # fetched_at stays the UTC instant of the run.
    assert result["fetched_at"] == "2026-12-31T22:30:00Z"
    assert [row["date"]["value"] for row in result["transactions"]] == [
        "2027-01-01",  # Сегодня, on the Moscow calendar
        "2027-01-01",  # 1 января without a year
        "2026-12-31",  # 31 декабря: a year back, it would otherwise be future
    ]


def test_a_profile_without_timezone_uses_moscow_by_default():
    now = datetime(2026, 12, 31, 22, 30, tzinfo=timezone.utc)
    page = FakePage(
        _feed(_header("Сегодня−1 ₽"), _operation("Сегодня (тест)", "−1 ₽")),
        url=TBANK_URL,
    )
    profile = _profile(
        {
            "stop_before_date": None,
            "stop_after_empty_rounds": 1,
            "max_rounds": 1,
            "settle_ms": 0,
        }
    )
    del profile["timezone"]

    result = collect(page, profile, timeout_ms=1, now=now)

    assert result["transactions"][0]["date"]["value"] == "2027-01-01"


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


def test_two_identical_operations_under_one_header_stay_two_rows():
    """Two equal coffees rendered under their day header are two operations.

    A re-render repeats both rows; the per-round multiset dedup keeps exactly
    the two that were collected the first time.
    """
    window_one = _feed(
        _header("27 октября"),
        _operation("Кофе (тест)", "−100 ₽", "Кафе и рестораны"),
        _operation("Кофе (тест)", "−100 ₽", "Кафе и рестораны"),
    )
    window_two = _feed(
        _header("27 октября"),
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
    # Both coffees sit under their header in every window, so no row is
    # headerless and nothing is counted.
    assert result["scroll"]["headerless_rows"] == 0
    assert page.writes == []


def test_two_identical_operations_never_rendered_together_are_merged():
    """Known limitation: the multiset is counted per rendered window.

    Two identical operations of one day that a virtualised list reveals one
    at a time look like a repeat of the row already collected, so the second
    one is merged. Both rows are dated (the overlap dates the second window),
    so the loss is not visible in ``headerless_rows``.
    """
    window_one = _feed(
        _header("27 октября"),
        _operation("Кофе (тест)", "−100 ₽", "Кафе и рестораны"),
        _operation("Магазин А (тест)", "−10 ₽"),
        _operation("Магазин Б (тест)", "−20 ₽"),
    )
    window_two = _feed(
        _operation("Магазин А (тест)", "−10 ₽"),
        _operation("Магазин Б (тест)", "−20 ₽"),
        # The second, identical coffee: rendered only after the first one
        # left the DOM, so the two are never in one window together.
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

    coffees = [
        row for row in result["transactions"] if row["title"]["value"] == "Кофе (тест)"
    ]
    assert len(coffees) == 1, "the two identical rows are merged (known limitation)"
    assert coffees[0]["date"]["value"] == "2026-10-27"
    assert result["scroll"]["headerless_rows"] == 0
    assert page.writes == []


def test_a_day_longer_than_one_window_keeps_all_its_operations():
    """A virtualised day header leaves the DOM before its last operations.

    The header seen in the first window is carried, so every operation of
    that day — including the ones rendered after the header scrolled out — is
    dated and kept.
    """
    window_one = _feed(
        _header("27 октября"),
        _operation("Магазин А (тест)", "−10 ₽"),
        _operation("Магазин Б (тест)", "−20 ₽"),
    )
    window_two = _feed(
        _operation("Магазин А (тест)", "−10 ₽"),
        _operation("Магазин Б (тест)", "−20 ₽"),
        _operation("Магазин В (тест)", "−30 ₽"),
        _operation("Магазин Г (тест)", "−40 ₽"),
        _header("26 октября"),
    )
    window_three = _feed(
        _operation("Магазин Г (тест)", "−40 ₽"),
        _header("26 октября"),
        _operation("Магазин Д (тест)", "−50 ₽"),
    )
    page = VirtualFeedPage([window_one, window_two, window_three])

    result = collect(
        page,
        _profile({"stop_before_date": None, "stop_after_empty_rounds": 2, "max_rounds": 10}),
        timeout_ms=1,
        now=FIXED_NOW,
    )

    assert [(row["date"]["value"], row["title"]["value"]) for row in result["transactions"]] == [
        ("2026-10-27", "Магазин А (тест)"),
        ("2026-10-27", "Магазин Б (тест)"),
        ("2026-10-27", "Магазин В (тест)"),
        ("2026-10-27", "Магазин Г (тест)"),
        ("2026-10-26", "Магазин Д (тест)"),
    ]
    assert result["scroll"]["headerless_rows"] == 0
    assert _warnings(result) == []
    assert page.writes == []


def test_a_header_that_scrolled_out_still_dates_its_rows():
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
    # The first two rows are rendered before the first header of the second
    # window: the carried 27 October header dates them, so none is skipped.
    assert result["scroll"]["headerless_rows"] == 0
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
    assert result["scroll"]["headerless_rows"] == 0
    assert _warnings(result) == []
    assert page.writes == []


# ---------------------------------------------------------------------------
# Overlap alignment: the leading rows are dated by the round before, not by
# a list of headers that may have skipped a day in between
# ---------------------------------------------------------------------------


def test_a_round_with_no_header_stays_inside_the_day_of_the_overlap():
    """The whole leading block belongs to one day: the overlap proves which.

    The second window renders no header at all (the day is longer than the
    viewport), so only the overlap with the first window can date it. The
    rows appended after that overlap still belong to the same day.
    """
    window_one = _feed(
        _header("27 октября"),
        _operation("Магазин А (тест)", "−10 ₽"),
        _operation("Магазин Б (тест)", "−20 ₽"),
    )
    window_two = _feed(
        _operation("Магазин А (тест)", "−10 ₽"),
        _operation("Магазин Б (тест)", "−20 ₽"),
        _operation("Магазин В (тест)", "−30 ₽"),
        _operation("Магазин Г (тест)", "−40 ₽"),
    )
    window_three = _feed(
        _operation("Магазин В (тест)", "−30 ₽"),
        _operation("Магазин Г (тест)", "−40 ₽"),
        _operation("Магазин Д (тест)", "−50 ₽"),
        _header("26 октября"),
        _operation("Магазин Е (тест)", "−60 ₽"),
    )
    page = VirtualFeedPage([window_one, window_two, window_three])

    result = collect(
        page,
        _profile({"stop_before_date": None, "stop_after_empty_rounds": 2, "max_rounds": 10}),
        timeout_ms=1,
        now=FIXED_NOW,
    )

    assert [(row["date"]["value"], row["title"]["value"]) for row in result["transactions"]] == [
        ("2026-10-27", "Магазин А (тест)"),
        ("2026-10-27", "Магазин Б (тест)"),
        ("2026-10-27", "Магазин В (тест)"),
        ("2026-10-27", "Магазин Г (тест)"),
        ("2026-10-27", "Магазин Д (тест)"),
        ("2026-10-26", "Магазин Е (тест)"),
    ]
    assert result["scroll"]["headerless_rows"] == 0
    assert result["scroll"]["complete"] is True
    assert _warnings(result) == []


def test_an_unparsed_header_between_rounds_skips_rows_instead_of_misdating_them():
    """A header that did not parse must not make the next day look like the last.

    The carried-header list used to skip an unparsed header and date the
    rows after it with the previous day's header. These rows have no overlap
    with the first window, so they are skipped and counted, never guessed.
    """
    window_one = _feed(
        _header("27 октября"),
        _operation("Магазин А (тест)", "−10 ₽"),
        _operation("Магазин Б (тест)", "−20 ₽"),
        _header("скоро"),
    )
    window_two = _feed(
        _operation("Магазин В (тест)", "−30 ₽"),
        _operation("Магазин Г (тест)", "−40 ₽"),
        _header("26 октября"),
        _operation("Магазин Д (тест)", "−50 ₽"),
    )
    page = VirtualFeedPage([window_one, window_two])

    result = collect(
        page,
        _profile({"stop_before_date": None, "stop_after_empty_rounds": 2, "max_rounds": 10}),
        timeout_ms=1,
        now=FIXED_NOW,
    )

    rows = result["transactions"]
    assert [(row["date"]["value"], row["title"]["value"]) for row in rows] == [
        ("2026-10-27", "Магазин А (тест)"),
        ("2026-10-27", "Магазин Б (тест)"),
        ("2026-10-26", "Магазин Д (тест)"),
    ], "the two rows after the unparsed header are not dated 27 October"
    assert result["scroll"]["headerless_rows"] == 2
    assert result["scroll"]["complete"] is False
    assert any(
        "2 operations could not be dated" in text and "and were skipped" in text
        for text in result["warnings"]
    )
    assert any("cannot parse ru_date" in text for text in result["warnings"])


def test_a_header_skipped_between_windows_is_not_inferred():
    """No overlap between two windows: the day stays unknown, not guessed.

    The header of the new day was never rendered together with its rows, and
    none of those rows was rendered before. The previous window's 27 October
    header must not be reused for them.
    """
    window_one = _feed(
        _header("27 октября"),
        _operation("Магазин А (тест)", "−10 ₽"),
        _operation("Магазин Б (тест)", "−20 ₽"),
    )
    window_two = _feed(
        _operation("Магазин В (тест)", "−30 ₽"),
        _operation("Магазин Г (тест)", "−40 ₽"),
    )
    window_three = _feed(
        _operation("Магазин Г (тест)", "−40 ₽"),
        _header("26 октября"),
        _operation("Магазин Д (тест)", "−50 ₽"),
    )
    page = VirtualFeedPage([window_one, window_two, window_three])

    result = collect(
        page,
        _profile({"stop_before_date": None, "stop_after_empty_rounds": 2, "max_rounds": 10}),
        timeout_ms=1,
        now=FIXED_NOW,
    )

    rows = result["transactions"]
    assert [(row["date"]["value"], row["title"]["value"]) for row in rows] == [
        ("2026-10-27", "Магазин А (тест)"),
        ("2026-10-27", "Магазин Б (тест)"),
        ("2026-10-26", "Магазин Д (тест)"),
    ]
    assert [row["title"]["value"] for row in rows].count("Магазин Г (тест)") == 0
    assert result["scroll"]["headerless_rows"] == 2
    assert result["scroll"]["complete"] is False
    assert any("could not be dated" in text for text in result["warnings"])


def test_recurring_identical_content_on_two_days_keeps_the_right_day():
    """Two equal operations on two days: the overlap picks the lower run.

    Both days render the same (title, category, amount) pair. The leading
    block of the next round is the pair of the *lower* day, so the LAST
    contiguous match in the previous window is the one that dates it.
    """
    window_one = _feed(
        _header("27 октября"),
        _operation("Магазин А (тест)", "−10 ₽"),
        _operation("Кофе (тест)", "−100 ₽", "Кафе и рестораны"),
        _operation("Чай (тест)", "−50 ₽", "Кафе и рестораны"),
        _header("26 октября"),
        _operation("Кофе (тест)", "−100 ₽", "Кафе и рестораны"),
        _operation("Чай (тест)", "−50 ₽", "Кафе и рестораны"),
    )
    window_two = _feed(
        _operation("Кофе (тест)", "−100 ₽", "Кафе и рестораны"),
        _operation("Чай (тест)", "−50 ₽", "Кафе и рестораны"),
        _header("25 октября"),
        _operation("Магазин Б (тест)", "−20 ₽"),
    )
    page = VirtualFeedPage([window_one, window_two])

    result = collect(
        page,
        _profile({"stop_before_date": None, "stop_after_empty_rounds": 2, "max_rounds": 10}),
        timeout_ms=1,
        now=FIXED_NOW,
    )

    rows = result["transactions"]
    assert [(row["date"]["value"], row["title"]["value"]) for row in rows] == [
        ("2026-10-27", "Магазин А (тест)"),
        ("2026-10-27", "Кофе (тест)"),
        ("2026-10-27", "Чай (тест)"),
        ("2026-10-26", "Кофе (тест)"),
        ("2026-10-26", "Чай (тест)"),
        ("2026-10-25", "Магазин Б (тест)"),
    ]
    assert result["scroll"]["headerless_rows"] == 0
    assert _warnings(result) == []


# ---------------------------------------------------------------------------
# A missing tz database must not abort the run
# ---------------------------------------------------------------------------


def test_a_missing_tz_database_accepts_the_timezone_name(tmp_path, monkeypatch):
    def missing(name):
        raise ZoneInfoNotFoundError(name)

    monkeypatch.setattr(profiles_module, "ZoneInfo", missing)
    path = tmp_path / "tbank_no_tzdata.yaml"
    path.write_text(
        "bank: tbank_no_tzdata\n"
        "status: unverified\n"
        'url: "https://www.tbank.ru/mybank/operations/"\n'
        'wait_for: "#x"\n'
        "timezone: Europe/Moscow\n"
        "balances: []\n"
        "transactions:\n"
        "  - row: 'li'\n"
        "    fields:\n"
        "      amount:\n"
        "        selector: '.amount'\n",
        encoding="utf-8",
    )

    profile = load_profile("tbank_no_tzdata", directory=str(tmp_path))

    assert profile["timezone"] == "Europe/Moscow"


def test_a_missing_tz_database_resolves_dates_in_utc_with_a_warning(monkeypatch):
    def missing(name):
        raise ZoneInfoNotFoundError(name)

    monkeypatch.setattr(collect_module, "ZoneInfo", missing)
    # 22:30 UTC on 31 December is already 1 January in Moscow.
    now = datetime(2026, 12, 31, 22, 30, tzinfo=timezone.utc)
    page = FakePage(
        _feed(_header("Сегодня−1 ₽"), _operation("Сегодня (тест)", "−1 ₽")),
        url=TBANK_URL,
    )

    result = collect(
        page,
        _profile(
            {
                "stop_before_date": None,
                "stop_after_empty_rounds": 1,
                "max_rounds": 1,
                "settle_ms": 0,
            }
        ),
        timeout_ms=1,
        now=now,
    )

    assert result["transactions"][0]["date"]["value"] == "2026-12-31"
    assert (
        "timezone Europe/Moscow unavailable (no tz database?), dates resolved in UTC"
        in result["warnings"]
    )


# ---------------------------------------------------------------------------
# Profile validation
# ---------------------------------------------------------------------------


def _write_profile(tmp_path, body):
    path = tmp_path / "tbank_broken.yaml"
    path.write_text(
        "bank: tbank_broken\n"
        "status: unverified\n"
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


def test_header_without_row_is_rejected(tmp_path):
    body = (
        "  - fields:\n"
        "      amount:\n"
        "        selector: '.amount'\n"
        "    header:\n"
        "      selector: '.day'\n"
        "      fields:\n"
        "        date:\n"
        "          selector: '.day'\n"
    )
    with pytest.raises(ProfileError, match="needs a 'row'"):
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


def test_a_leading_block_that_repeats_a_later_day_keeps_its_own_day():
    """Live regression (2026-10-24): the same train ticket and phone top-up on two
    days. Looking the leading block up as "the last place it occurs" in the
    previous window dated the newer day's rows by the older day and added them a
    second time. The overlap is a suffix of the previous window and a prefix of
    this one, so the block lands on its own day."""
    ticket = ("Ж/д билет (тест)", "−1 978,5 ₽", "Ж/д билеты")
    phone = ("Связь (тест)", "−320 ₽", "Мобильная связь")
    window_one = _feed(
        _header("25 октября"),
        _operation(*ticket),
        _operation(*phone),
        _header("24 октября"),
        _operation(*ticket),
        _operation(*phone),
    )
    window_two = _feed(
        # The 25th's rows are now at the top, above the first header of this window.
        _operation(*ticket),
        _operation(*phone),
        _header("24 октября"),
        _operation(*ticket),
        _operation(*phone),
        _header("23 октября"),
        _operation("Магазин А (тест)", "−10 ₽"),
    )
    page = VirtualFeedPage([window_one, window_two])

    result = collect(
        page,
        _profile({"stop_before_date": None, "stop_after_empty_rounds": 2, "max_rounds": 10}),
        timeout_ms=1,
        now=FIXED_NOW,
    )

    dated = [(row["date"]["value"], row["title"]["value"]) for row in result["transactions"]]
    assert dated == [
        ("2026-10-25", "Ж/д билет (тест)"),
        ("2026-10-25", "Связь (тест)"),
        ("2026-10-24", "Ж/д билет (тест)"),
        ("2026-10-24", "Связь (тест)"),
        ("2026-10-23", "Магазин А (тест)"),
    ]
    assert result["scroll"]["headerless_rows"] == 0
