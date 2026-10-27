"""The one-pass reader of a flat feed (day headers + operations).

``_extract_rows`` used to walk every row element by element
(``query_all`` + ``text_content`` per field), which over a remote CDP relay
took about a minute per scroll round and read a feed that re-renders under
it. The driver now asks a page that offers ``snapshot_rows(spec)`` for the
whole feed in ONE read-only page-side pass and dates every operation by the
day header nearest *above it on screen*. These tests pin that down:

* a page-side pass per extraction, with only selectors/attributes going over;
* the visual order wins over the document order;
* the snapshot path and the element-by-element fallback agree;
* a header whose text loses its day total keeps the same date.
"""

import json
import os
import shutil
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from bank_collector.collect import _extract_rows, extract_section, parse_ru_date  # noqa: E402
from bank_collector.connect import PageAdapter, SNAPSHOT_ROWS_JS  # noqa: E402
from bank_collector.profiles import load_profile  # noqa: E402
from fake_page import FakePage  # noqa: E402
from test_tbank_profile import (  # noqa: E402
    DESCRIPTION,
    FIXED_NOW,
    HEADER,
    QA,
    ROW,
    TBANK_URL,
    VirtualFeedPage,
    _feed,
    _header,
    _operation,
    read_fixture,
)

FIXTURES = os.path.join(HERE, "fixtures")


def _flat_record():
    """The shipped T-Bank record without its scroll block."""
    record = load_profile("tbank")["transactions"][0]
    record.pop("scroll", None)
    return record


class ElementOnlyPage(object):
    """A page without ``snapshot_rows``: the old element-by-element path."""

    def __init__(self, page):
        self._page = page

    @property
    def url(self):
        return self._page.url

    def goto(self, url, **kwargs):
        return self._page.goto(url, **kwargs)

    def wait_for_selector(self, selector, timeout=None):
        return self._page.wait_for_selector(selector, timeout=timeout)

    def query_all(self, selector):
        return self._page.query_all(selector)

    def scroll_by(self, dy):
        return self._page.scroll_by(dy)


# ---------------------------------------------------------------------------
# One pass, and only selectors go over the wire
# ---------------------------------------------------------------------------


def test_the_flat_feed_is_read_in_one_page_side_pass():
    page = FakePage(read_fixture("tbank_operations.html"), url=TBANK_URL)
    record = _flat_record()
    warnings = []

    rows = extract_section(page, record, "transactions", warnings, timeout_ms=1, now=FIXED_NOW)

    assert len(page.snapshot_calls) == 1
    assert [row["date"]["value"] for row in rows] == [
        "2026-10-27",
        "2026-10-27",
        "2026-10-26",
        "2025-12-29",
    ]
    assert warnings == []
    assert page.writes == []

    # Only selectors and attributes cross into the page; regex/group/parse/
    # optional stay in Python.
    spec = page.snapshot_calls[0]
    assert spec["row"] == ROW
    assert spec["header_selector"] == HEADER
    assert spec["header_fields"] == {"date": {"selector": HEADER}}
    assert spec["fields"]["description"] == {"selector": DESCRIPTION}
    for fields in (spec["fields"], spec["header_fields"]):
        for name, field in fields.items():
            assert set(field) <= {"selector", "attr"}, name


def test_the_adapter_makes_exactly_one_evaluate_call():
    spec = {"row": "ul > li", "header_selector": ".day", "header_fields": {}, "fields": {}}

    class RawPage(object):
        def __init__(self):
            self.url = "https://example.invalid/"
            self.calls = []

        def evaluate(self, script, argument):
            self.calls.append((script, argument))
            return [{"index": 0, "top": 0.0, "header": True, "fields": {}}]

    raw = RawPage()
    rows = PageAdapter(raw).snapshot_rows(spec)

    assert rows[0]["header"] is True
    assert len(raw.calls) == 1
    assert raw.calls[0][0] == SNAPSHOT_ROWS_JS
    assert raw.calls[0][1] == spec


# ---------------------------------------------------------------------------
# Visual order, not document order
# ---------------------------------------------------------------------------


def test_rows_are_dated_in_dom_order_whatever_the_geometry_says():
    """T-Bank's header <li> all report the list's top (the header itself is absolutely
    positioned): ordering by geometry dated whole screens by the wrong day (live run,
    2026-10-27). DOM order is what dates an operation."""
    page = FakePage(
        _feed(
            _header("27 октября"),
            _operation("Кофе (тест)", "−250 ₽", "Кафе и рестораны"),
            _header("26 октября"),
            _operation("Чай (тест)", "−50 ₽", "Кафе и рестораны"),
        )
    )
    tops = [563.0, 607.0, 563.0, 755.0]
    rows = page.query_all(ROW)
    assert len(rows) == 4
    for row, top in zip(rows, tops):
        row.top = top

    warnings = []
    result = extract_section(
        page, _flat_record(), "transactions", warnings, timeout_ms=1, now=FIXED_NOW
    )

    assert [row["title"]["value"] for row in result] == ["Кофе (тест)", "Чай (тест)"]
    assert [row["date"]["value"] for row in result] == ["2026-10-27", "2026-10-26"]
    assert warnings == []
    assert page.writes == []
    assert len(page.snapshot_calls) == 1


def test_rows_above_every_header_are_still_headerless_and_skipped():
    page = FakePage(
        _feed(
            _operation("Ранняя (тест)", "−10 ₽"),
            _header("27 октября"),
            _operation("Поздняя (тест)", "−20 ₽"),
        )
    )
    counter = {"headerless_rows": 0}
    warnings = []

    rows = _extract_rows(
        page, _flat_record(), "transactions", warnings, now=FIXED_NOW, counter=counter
    )

    assert [row["title"]["value"] for row in rows] == ["Поздняя (тест)"]
    assert rows[0]["date"]["value"] == "2026-10-27"
    assert counter["headerless_rows"] == 1
    assert warnings == []


# ---------------------------------------------------------------------------
# The snapshot path and the old element path agree
# ---------------------------------------------------------------------------


def test_the_snapshot_and_the_element_path_produce_the_same_rows_and_warnings():
    page = FakePage(read_fixture("tbank_operations.html"), url=TBANK_URL)
    fallback = ElementOnlyPage(FakePage(read_fixture("tbank_operations.html"), url=TBANK_URL))
    headless_snapshot_warnings = []
    headless_element_warnings = []

    snapshot_rows = extract_section(
        page, _flat_record(), "transactions", headless_snapshot_warnings, timeout_ms=1, now=FIXED_NOW
    )
    element_rows = extract_section(
        fallback, _flat_record(), "transactions", headless_element_warnings, timeout_ms=1, now=FIXED_NOW
    )

    assert snapshot_rows == element_rows
    assert headless_snapshot_warnings == headless_element_warnings == []


def test_a_missing_required_field_warns_once_and_an_optional_one_stays_silent():
    html = (
        "<ul>"
        "<li><div class='day'>27 октября</div></li>"
        "<li><span class='title'>А</span><span class='amount'>−10 ₽</span></li>"
        "<li><span class='title'>Б</span></li>"
        "</ul>"
    )
    record = {
        "row": "ul > li",
        "header": {
            "selector": ".day",
            "fields": {"date": {"selector": ".day", "parse": "ru_date"}},
        },
        "fields": {
            "title": {"selector": ".title"},
            "amount": {"selector": ".amount"},
            "description": {"selector": ".description", "optional": True},
        },
    }
    snapshot_warnings = []
    element_warnings = []

    snapshot_rows = extract_section(
        FakePage(html), record, "transactions", snapshot_warnings, timeout_ms=1, now=FIXED_NOW
    )
    element_rows = extract_section(
        ElementOnlyPage(FakePage(html)), record, "transactions", element_warnings,
        timeout_ms=1, now=FIXED_NOW,
    )

    assert snapshot_rows == element_rows
    assert snapshot_warnings == element_warnings == [
        "transactions[2].amount: selector '.amount' matched nothing"
    ]
    assert snapshot_rows[1]["amount"]["value"] is None
    assert snapshot_rows[1]["description"]["value"] is None
    assert not [text for text in snapshot_warnings if "description" in text]


# ---------------------------------------------------------------------------
# A header that re-renders with and without its day total
# ---------------------------------------------------------------------------


def _raw_header(text):
    return "<li><div %s='atom-operations-feed-header'>%s</div></li>" % (QA, text)


def test_a_header_with_or_without_its_day_total_keeps_the_same_date():
    window_one = _feed(
        _raw_header("27 октября−1 249 ₽"),
        _operation("Перевод (тест)", "+9 000 ₽", "Переводы"),
    )
    window_two = _feed(
        _raw_header("27 октября"),
        _operation("Перевод (тест)", "+9 000 ₽", "Переводы"),
    )
    page = VirtualFeedPage([window_one, window_two])
    profile = load_profile("tbank")
    profile["transactions"][0]["scroll"].update(
        {"stop_before_date": None, "stop_after_empty_rounds": 2, "max_rounds": 10}
    )

    warnings = []
    rows = extract_section(
        page,
        profile["transactions"][0],
        "transactions",
        warnings,
        timeout_ms=1,
        now=FIXED_NOW,
    )

    # The same operation read under both header spellings: one date, one row.
    assert len(rows) == 1
    assert rows[0]["date"]["value"] == "2026-10-27"
    assert rows[0]["date"]["raw"].startswith("27 октября")
    assert warnings == []
    # One page-side pass for the first render and one per scroll round.
    assert len(page.snapshot_calls) == 3
    assert page.writes == []


# ---------------------------------------------------------------------------
# The real JS string, when node with jsdom happens to be installed
# ---------------------------------------------------------------------------

JS_RUNNER = """
const fs = require('fs');
const { JSDOM } = require('jsdom');
const [specPath, jsPath, htmlPath] = process.argv.slice(2);
const spec = JSON.parse(fs.readFileSync(specPath, 'utf8'));
const source = fs.readFileSync(jsPath, 'utf8');
const dom = new JSDOM(fs.readFileSync(htmlPath, 'utf8'));
global.window = dom.window;
global.document = dom.window.document;
const snapshotRows = eval(source);
process.stdout.write(JSON.stringify(snapshotRows(spec)));
"""


def _jsdom_available():
    node = shutil.which("node")
    if not node:
        return None
    try:
        probe = subprocess.run(
            [node, "-e", "require.resolve('jsdom')"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return node if probe.returncode == 0 else None


def test_the_real_snapshot_js_reads_the_tbank_fixture(tmp_path):
    node = _jsdom_available()
    if node is None:
        pytest.skip("node with jsdom is not installed locally")

    spec = {
        # A plain selector: the JS does not need the profile's `:has()`, which
        # older jsdom selector engines do not implement.
        "row": "ul > li",
        "header_selector": HEADER,
        "header_fields": {"date": {"selector": HEADER}},
        "fields": {
            "title": {"selector": "[data-qa-type=atom-operations-feed-operation-title]"},
            "amount": {"selector": "[data-qa-type=atom-operations-feed-operation-amount]"},
            "description": {
                "selector": "[data-qa-type=atom-operations-feed-operation-description]"
            },
        },
    }
    runner = tmp_path / "run.js"
    runner.write_text(JS_RUNNER, encoding="utf-8")
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    js_path = tmp_path / "snapshot.js"
    js_path.write_text(SNAPSHOT_ROWS_JS, encoding="utf-8")

    proc = subprocess.run(
        [node, str(runner), str(spec_path), str(js_path), os.path.join(FIXTURES, "tbank_operations.html")],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
        universal_newlines=True,
    )

    assert proc.returncode == 0, proc.stderr
    snapshot = json.loads(proc.stdout)
    assert [row["header"] for row in snapshot] == [True, False, False, True, False, True, False]
    assert all(isinstance(row["top"], (int, float)) for row in snapshot)
    assert snapshot[0]["fields"]["date"]["found"] is True
    assert parse_ru_date(snapshot[0]["fields"]["date"]["text"], now=FIXED_NOW) == "2026-10-27"
    assert snapshot[1]["fields"]["title"]["text"] == "Магазин «Пример»"
    assert snapshot[1]["fields"]["description"]["text"] == "Кэшбэк 5 ₽"
    assert snapshot[4]["fields"]["description"]["found"] is False
