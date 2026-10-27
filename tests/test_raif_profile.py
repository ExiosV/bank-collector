"""The `raif` profile and the paginated (numbered-page) history mode.

The fixtures reproduce the nesting of the real Raiffeisen cabinet: the home
page holds the product cards, and `/history/common` renders day groups
(`.transaction-group-list__item`) that hold the operations of one day. The
history is NOT scrolled: it shows ten operations per page, the whole list is
replaced when the pagination arrow is pressed, and the URL never changes.
Every name, number and date below is invented.

What is pinned here is the `pages` mode: the one element it may click, when
it stops, and what makes a run incomplete (exit code 5) so that it can never
overwrite a fuller snapshot.
"""

import copy
import io
import os
import sys
from datetime import datetime, timezone

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from bank_collector import cli  # noqa: E402
from bank_collector import report as report_mod  # noqa: E402
from bank_collector.collect import collect  # noqa: E402
from bank_collector.profiles import ProfileError, load_profile  # noqa: E402
from fake_page import FakePage  # noqa: E402

FIXTURES = os.path.join(HERE, "fixtures")

HOME_URL = "https://online.raiffeisen.ru/"
HISTORY_URL = "https://online.raiffeisen.ru/history/common"

NEXT_SELECTOR = (
    "nav.pagination .pagination__pages > button.pagination__item_arrow:last-child"
)
ACTIVE_SELECTOR = "nav.pagination button.pagination__item_active"

#: A fixed run instant: "24 октября" then means 2026-10-24, not a future year.
NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# History markup: day groups, operations and the pagination block
# ---------------------------------------------------------------------------


def _operation(title, amount, cashback=""):
    """One operation, shaped like the live `.transaction-list__item`."""
    success = (
        " class='transaction-list-amount_success'" if amount.startswith("+") else ""
    )
    return (
        "<li class='transaction-list__item'>"
        "<div class='logo-container'><img src='logo.png'></div>"
        "<p class='transaction-list__item-title'>%s</p>"
        "<transaction-list-amount><div class='transaction-list-amount'>"
        "<div%s>%s</div>"
        "<div class='transaction-list-amount__cashback'>%s</div>"
        "</div></transaction-list-amount>"
        "</li>" % (title, success, amount, cashback)
    )


def _group(header, operations):
    """One day: the date header and the operations of that day."""
    return (
        "<li class='transaction-group-list__item'>"
        "<h5 class='transaction-group-list__item__title'>%s</h5>"
        "<ul class='transaction-list'>%s</ul>"
        "</li>" % (header, "".join(operations))
    )


def _pagination(number, last=False, navs=1):
    """The pagination block; the right arrow is the one `pages` clicks.

    *navs* > 1 renders the whole block twice, so the "next" selector finds
    two elements for the "none clicked" test. *last* disables the right
    arrow, which is the list's own definitive end of history.
    """
    left = "<button class='pagination__item pagination__item_arrow' disabled>left</button>"
    active = "<button class='pagination__item pagination__item_active'>%d</button>" % number
    others = "".join(
        "<button class='pagination__item'>%d</button>" % page
        for page in (1, 2, 3, 4)
        if page != number
    )
    right = "<button class='pagination__item pagination__item_arrow'%s>right</button>" % (
        " disabled" if last else ""
    )
    return (
        "<nav class='pagination'><div class='pagination__pages'>%s%s%s%s</div></nav>"
        % (left, active, others, right)
    ) * navs


def _history(number, groups, last=False, navs=1):
    """A whole history page: the day groups and its pagination."""
    return (
        "<html><body><div class='history'>%s%s</div></body></html>"
        % (
            "".join(_group(header, operations) for header, operations in groups),
            _pagination(number, last=last, navs=navs),
        )
    )


class PagedPage(FakePage):
    """A cabinet whose next arrow replaces the whole list, like the real one."""

    def __init__(self, htmls):
        FakePage.__init__(self, htmls[0], url=HISTORY_URL)
        self.htmls = list(htmls)
        self.index = 0
        #: The class attribute of every element that was clicked, in order.
        self.clicks = []
        self._on_click = self._advance

    def _advance(self, page, element):
        self.clicks.append(element.get_attribute("class") or "")
        if self.index + 1 < len(self.htmls):
            self.index += 1
            self._set_root(self.htmls[self.index])


class StuckPage(PagedPage):
    """A page whose arrow is pressed but whose content never changes."""

    def _advance(self, page, element):
        self.clicks.append(element.get_attribute("class") or "")


class LaggingPage(PagedPage):
    """The page number moves at once, the new rows arrive a few reads later.

    Right after the click the list still shows the old rows under the new
    page number, the way an SPA updates its pagination before the data. The
    new rows replace them after a few reads of the page number (the polls).
    """

    def __init__(self, htmls, stale_polls=3):
        PagedPage.__init__(self, htmls)
        self.stale_polls = stale_polls
        self._pending = None

    def _advance(self, page, element):
        self.clicks.append(element.get_attribute("class") or "")
        if self.index + 1 < len(self.htmls):
            self.index += 1
            old_rows = self.htmls[self.index - 1].split("<nav")[0]
            new_nav = "<nav" + self.htmls[self.index].split("<nav", 1)[1]
            self._set_root(old_rows + new_nav)
            self._pending = self.stale_polls

    def query_all(self, selector):
        # Counted on the page-number reads only: the tests never really sleep,
        # so "a few polls later" is the only clock there is.
        if self._pending is not None and selector == ACTIVE_SELECTOR:
            self._pending -= 1
            if self._pending <= 0:
                self._pending = None
                self._set_root(self.htmls[self.index])
        return PagedPage.query_all(self, selector)


class TwoPageCabinet(FakePage):
    """A cabinet that serves a different document for each URL."""

    def __init__(self, pages, url):
        FakePage.__init__(self, pages[url], url=url)
        self.pages_by_url = pages

    def goto(self, url, **kwargs):
        self.visited.append(url)
        for known, html in self.pages_by_url.items():
            if known.rstrip("/") == url.rstrip("/"):
                self.url = url
                self._set_root(html)
                return None
        raise LookupError("no fixture for %r" % url)


def _read_fixture(name):
    with open(os.path.join(FIXTURES, name), "r", encoding="utf-8") as handle:
        return handle.read()


def _profile(**page_overrides):
    """The shipped `raif` history record, with a small page size for the tests."""
    profile = copy.deepcopy(load_profile("raif"))
    profile["balances"] = []
    profile["wait_for"] = ".transaction-list__item"
    profile["transactions"][0]["pages"].update(page_overrides)
    return profile


def _warnings(result):
    """The warnings of a run, without the "no balances" noise of the fixture."""
    return [text for text in result["warnings"] if text != "no balances extracted"]


def _ops(count, title="Магазин", amount="−900 ₽"):
    return [_operation(title, amount) for _ in range(count)]


# ---------------------------------------------------------------------------
# The profile
# ---------------------------------------------------------------------------


def test_raif_profile_loads_and_names_the_pagination():
    profile = load_profile("raif")

    assert profile["status"] == "unverified", "no live run has been made yet"
    assert profile["url"] == HOME_URL
    assert profile["timezone"] == "Europe/Moscow"

    record = profile["transactions"][0]
    assert record["url"] == HISTORY_URL
    assert record["row"] == ".transaction-group-list__item"
    assert record["items"]["row"] == ".transaction-list__item"
    assert record["fields"]["date"]["parse"] == "ru_date"
    assert record["scroll"] is None, "numbered pages and scrolling are exclusive"

    pages = record["pages"]
    assert pages["next_selector"] == NEXT_SELECTOR
    assert pages["active_selector"] == ACTIVE_SELECTOR
    assert pages["page_size"] == 10
    assert pages["settle_ms"] == 800
    assert pages["wait_ms"] == 15000
    assert pages["max_pages"] == 400
    assert pages["stop_before_date"] == "2026-01-01"


def test_balances_drop_the_account_number_and_keep_the_currency():
    history = _history(
        1,
        [
            (
                "24 октября, суббота",
                [_operation("Магазин", "−900 ₽"), _operation("Зарплата", "+13 000 ₽")],
            )
        ],
        last=True,
    )
    page = TwoPageCabinet(
        {HOME_URL: _read_fixture("raif_home.html"), HISTORY_URL: history}, HOME_URL
    )

    result = collect(page, load_profile("raif"), now=NOW)

    balances = result["balances"]
    assert [row["product"]["value"] for row in balances] == [
        "Текущий счёт",
        "Вклад «Пример»",
        "Карта «Пример»",
    ], "the trailing account number is stripped, a name without one stays whole"
    assert [row["amount"]["value"] for row in balances] == [
        "123456.78",
        "50000.00",
        "7777.77",
    ]
    assert [row["amount"]["currency"] for row in balances] == ["RUB", "USD", "JPY"], (
        "¥ is reported as JPY: parse_money does not know the yuan (a known caveat)"
    )
    assert _warnings(result) == []
    # A single page with a disabled arrow is a definitive end.
    assert result["scroll"]["stop"] == "end"
    assert result["scroll"]["complete"] is True


# ---------------------------------------------------------------------------
# Walking the pages
# ---------------------------------------------------------------------------


def test_three_pages_are_kept_whole_and_a_disabled_arrow_ends_the_history():
    page = PagedPage(
        [
            _history(
                1,
                [
                    (
                        "24 октября, суббота",
                        [
                            _operation("Магазин", "−900 ₽"),
                            _operation("Кофе", "−250 ₽"),
                            _operation("Магазин", "−900 ₽"),
                        ],
                    )
                ],
            ),
            _history(
                2,
                [
                    (
                        "24 октября, суббота",
                        [
                            _operation("Магазин", "−900 ₽"),
                            _operation("Аптека", "−400 ₽"),
                            _operation("Метро", "−50 ₽"),
                        ],
                    )
                ],
            ),
            _history(
                3,
                [("24 октября, суббота", [_operation("Магазин", "−900 ₽")])],
                last=True,
            ),
        ]
    )

    result = collect(page, _profile(page_size=3), now=NOW)

    rows = result["transactions"]
    assert [row["title"]["value"] for row in rows] == [
        "Магазин",
        "Кофе",
        "Магазин",
        "Магазин",
        "Аптека",
        "Метро",
        "Магазин",
    ]
    assert [row["amount"]["value"] for row in rows] == [
        "-900",
        "-250",
        "-900",
        "-900",
        "-400",
        "-50",
        "-900",
    ]
    assert [row["amount"]["value"] for row in rows].count("-900") == 4, (
        "the pages are disjoint: equal operations on two pages are two operations"
    )
    assert {row["date"]["value"] for row in rows} == {"2026-10-24"}

    assert result["scroll"] == {
        "mode": "pages",
        "pages": 3,
        "clicks": 2,
        "rows": 7,
        "stop": "end",
        "oldest_date": "2026-10-24",
        "headerless_rows": 0,
        "complete": True,
    }, "a disabled arrow is the definitive end, even above stop_before_date"
    assert _warnings(result) == []
    assert page.clicks == ["pagination__item pagination__item_arrow"] * 2


def test_a_row_older_than_stop_before_date_stops_the_walk():
    page = PagedPage(
        [
            _history(1, [("2 января 2026", _ops(3))]),
            _history(
                2,
                [
                    (
                        "31 декабря 2025",
                        [_operation("Магазин", "−900 ₽"), _operation("Кофе", "−250 ₽")],
                    )
                ],
            ),
        ]
    )

    result = collect(page, _profile(page_size=3), now=NOW)

    assert [row["date"]["value"] for row in result["transactions"]] == [
        "2026-01-02",
        "2026-01-02",
        "2026-01-02",
        "2025-12-31",
        "2025-12-31",
    ]
    assert result["scroll"]["stop"] == "date"
    assert result["scroll"]["pages"] == 2
    assert result["scroll"]["clicks"] == 1
    assert result["scroll"]["complete"] is True
    assert _warnings(result) == [], "the last page may be shorter: it was not followed"


def test_a_next_selector_matching_two_elements_is_never_clicked():
    page = PagedPage([_history(1, [("24 октября, суббота", _ops(3))], navs=2)])

    result = collect(page, _profile(page_size=3, stop_before_date=None), now=NOW)

    assert page.clicks == [], "not one of the two arrows is pressed"
    assert result["scroll"]["stop"] == "error"
    assert result["scroll"]["pages"] == 1
    assert result["scroll"]["clicks"] == 0
    assert result["scroll"]["complete"] is False
    assert _warnings(result) == [
        "transactions: next-page selector %r matched 2 elements, none clicked"
        % NEXT_SELECTOR
    ]


def test_a_page_that_does_not_advance_stops_with_an_error():
    page = StuckPage([_history(1, [("24 октября, суббота", _ops(3))])])

    result = collect(
        page, _profile(page_size=3, wait_ms=500, stop_before_date=None), now=NOW
    )

    assert page.clicks == ["pagination__item pagination__item_arrow"]
    assert result["scroll"]["stop"] == "error"
    assert result["scroll"]["pages"] == 1
    assert result["scroll"]["clicks"] == 1
    assert result["scroll"]["rows"] == 3
    assert result["scroll"]["complete"] is False
    assert _warnings(result) == ["transactions: page did not advance past 1"]


def test_a_short_intermediate_page_makes_the_run_incomplete():
    page = PagedPage(
        [
            _history(1, [("10 января 2026", _ops(3))]),
            _history(
                2,
                [
                    (
                        "9 января 2026",
                        [_operation("Магазин", "−900 ₽"), _operation("Кофе", "−250 ₽")],
                    )
                ],
            ),
            _history(3, [("8 января 2026", _ops(1))], last=True),
        ]
    )

    result = collect(page, _profile(page_size=3), now=NOW)

    assert result["scroll"]["stop"] == "end"
    assert result["scroll"]["pages"] == 3
    assert result["scroll"]["rows"] == 6
    assert result["scroll"]["complete"] is False
    assert _warnings(result) == [
        "transactions: page 2 showed 2 operations, expected 3"
    ]


def test_a_newer_page_below_an_older_one_makes_the_run_incomplete():
    page = PagedPage(
        [
            _history(1, [("10 января 2026", _ops(3))]),
            _history(2, [("20 января 2026", _ops(3))], last=True),
        ]
    )

    result = collect(page, _profile(page_size=3), now=NOW)

    assert result["scroll"]["stop"] == "end"
    assert result["scroll"]["pages"] == 2
    assert result["scroll"]["complete"] is False
    assert _warnings(result) == [
        "transactions: page 2 starts at 2026-01-20, newer than page 1 ends at "
        "2026-01-10 (history may be incomplete)"
    ]


def test_max_pages_is_reported_as_a_limit():
    page = PagedPage(
        [
            _history(1, [("12 января 2026", _ops(3))]),
            _history(2, [("11 января 2026", _ops(3))]),
            _history(3, [("10 января 2026", _ops(3))]),
        ]
    )

    result = collect(page, _profile(page_size=3, max_pages=2), now=NOW)

    assert result["scroll"]["stop"] == "limit"
    assert result["scroll"]["pages"] == 2
    assert result["scroll"]["clicks"] == 1
    assert result["scroll"]["rows"] == 6
    assert result["scroll"]["complete"] is False
    assert _warnings(result) == [
        "transactions: page limit reached after 2 pages (6 rows, history may be incomplete)"
    ]


# ---------------------------------------------------------------------------
# Profile validation
# ---------------------------------------------------------------------------

_PROFILE_PREFIX = (
    "bank: paginated\n"
    "status: verified\n"
    'url: "https://x.invalid/"\n'
    'wait_for: "#x"\n'
    "balances: []\n"
    "transactions:\n"
    "  - row: '.row'\n"
    "    fields:\n"
    "      x:\n"
    '        selector: ".x"\n'
)

_PAGES = "    pages:\n      next_selector: '.next'\n      active_selector: '.active'\n"


def _load(tmp_path, name, extra):
    path = tmp_path / ("%s.yaml" % name)
    path.write_text(_PROFILE_PREFIX + extra, encoding="utf-8")
    return load_profile(name, directory=str(tmp_path))


def test_pages_gets_the_documented_defaults(tmp_path):
    profile = _load(tmp_path, "pages_defaults", _PAGES)
    pages = profile["transactions"][0]["pages"]

    assert pages["next_selector"] == ".next"
    assert pages["active_selector"] == ".active"
    assert pages["page_size"] is None
    assert pages["settle_ms"] == 800
    assert pages["wait_ms"] == 15000
    assert pages["max_pages"] == 300
    assert pages["stop_before_date"] is None


def test_pages_with_scroll_is_rejected(tmp_path):
    with pytest.raises(ProfileError, match="cannot be used together"):
        _load(
            tmp_path,
            "pages_and_scroll",
            _PAGES + "    scroll:\n      max_rounds: 2\n",
        )


def test_pages_unknown_key_is_rejected(tmp_path):
    with pytest.raises(ProfileError, match="fake_key"):
        _load(tmp_path, "pages_unknown", _PAGES + "      fake_key: 1\n")


def test_pages_requires_both_selectors(tmp_path):
    with pytest.raises(ProfileError, match="active_selector"):
        _load(
            tmp_path,
            "pages_no_active",
            "    pages:\n      next_selector: '.next'\n",
        )


@pytest.mark.parametrize("value", ["0", "-1", "true", "'5'"])
def test_pages_page_size_must_be_a_positive_integer(tmp_path, value):
    with pytest.raises(ProfileError, match="page_size"):
        _load(
            tmp_path,
            "pages_bad_size",
            _PAGES + "      page_size: %s\n" % value,
        )


def test_pages_stop_before_date_is_validated_like_scroll(tmp_path):
    with pytest.raises(ProfileError, match="YYYY-MM-DD"):
        _load(
            tmp_path,
            "pages_bad_date",
            _PAGES + "      stop_before_date: 01-01-2026\n",
        )


# ---------------------------------------------------------------------------
# The CLI contract
# ---------------------------------------------------------------------------


def test_an_incomplete_paginated_run_exits_5():
    page = PagedPage([_history(1, [("24 октября, суббота", _ops(3))], navs=2)])

    collected = collect(page, _profile(page_size=3, stop_before_date=None), now=NOW)
    report = report_mod.build_report(collected)

    assert report["scroll"]["mode"] == "pages"
    assert cli._incomplete(report) is True

    stream = io.StringIO()
    code = cli._exit_code(report, ["raif.json"], None, stream)

    assert code == cli.EXIT_INCOMPLETE
    assert "history incomplete" in stream.getvalue()


def test_rows_that_arrive_after_the_page_number_are_still_the_new_page():
    page = LaggingPage(
        [
            _history(1, [("24 октября, суббота", _ops(3))]),
            _history(2, [("23 октября, пятница", _ops(3, title="Кофе", amount="−250 ₽"))], last=True),
        ]
    )

    result = collect(
        page, _profile(page_size=3, wait_ms=2000, settle_ms=0, stop_before_date=None), now=NOW
    )

    assert result["scroll"]["stop"] == "end"
    assert result["scroll"]["pages"] == 2
    assert result["scroll"]["complete"] is True
    assert [row["title"]["value"] for row in result["transactions"]] == ["Магазин"] * 3 + ["Кофе"] * 3
    assert [row["date"]["value"] for row in result["transactions"]] == ["2026-10-24"] * 3 + ["2026-10-23"] * 3
    assert _warnings(result) == []
