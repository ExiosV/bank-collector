"""The Playwright adapter: the driver's protocol without importing Playwright.

``bank_collector.collect`` talks to a page through a four-call protocol
(``goto``, ``wait_for_selector``, ``query_all`` and the two snapshot
helpers). Real Playwright pages do not have ``query_all``/``text_content`` —
that mismatch went unnoticed for a while because the tests only ever used the
fake page, and the first live run failed with
``'Page' object has no attribute 'query_all'``. These tests keep the adapter
honest with tiny stand-ins, so no browser is needed here either.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from bank_collector.collect import collect, extract_section  # noqa: E402
from bank_collector.connect import PageAdapter  # noqa: E402


class FakeHandle(object):
    """Looks like a Playwright ElementHandle."""

    def __init__(self, text, attrs=None, children=None):
        self.text = text
        self.attrs = attrs or {}
        self.children = children or {}

    def query_selector_all(self, selector):
        return list(self.children.get(selector, []))

    def text_content(self):
        return self.text

    def get_attribute(self, name):
        return self.attrs.get(name)


class FakePlaywrightPage(object):
    """Looks like a Playwright Page, not like the driver's protocol."""

    def __init__(self, url, html=""):
        self.url = url
        self.html = html
        self.goto_calls = []
        self.waited = []
        self.closed = False

    def goto(self, url, **kwargs):
        self.goto_calls.append((url, kwargs))
        self.url = url

    def wait_for_selector(self, selector, timeout=None):
        self.waited.append(selector)

    def query_selector_all(self, selector):
        if selector == "[data-test-id=transaction-status]":
            return [FakeHandle("-1 250,50 ₽")]
        if selector == "[data-test-id=operation-cell]":
            return [
                FakeHandle(
                    "operation",
                    children={"[data-test-id=transaction-status]": [FakeHandle("-1 250,50 ₽")]},
                )
            ]
        return []

    def content(self):
        return self.html

    def inner_text(self, selector="body"):
        return "text of " + self.html

    def close(self):
        self.closed = True


def test_adapter_exposes_the_driver_protocol():
    page = PageAdapter(FakePlaywrightPage("https://example.invalid/"))
    assert page.url == "https://example.invalid/"
    assert page.query_all("[data-test-id=operation-cell]")[0].text_content() == "operation"
    assert page.query_all("[data-test-id=transaction-status]")[0].text_content() == "-1 250,50 ₽"
    assert page.content() == ""
    assert page.inner_text("body") == "text of "


def test_adapter_navigates_with_a_real_timeout_and_waits():
    raw = FakePlaywrightPage("https://example.invalid/")
    page = PageAdapter(raw, timeout_ms=12345)
    page.goto("https://example.invalid/history/")
    page.wait_for_selector("[data-test-id=operation-cell]")

    url, kwargs = raw.goto_calls[0]
    assert url == "https://example.invalid/history/"
    assert kwargs["wait_until"] == "domcontentloaded"
    assert kwargs["timeout"] == 12345
    assert raw.waited == ["[data-test-id=operation-cell]"]


def test_the_driver_runs_through_the_adapter():
    profile = {
        "bank": "adapter",
        "status": "verified",
        "url": "https://example.invalid/",
        "wait_for": "",
        "balances": [],
        "transactions": [
            {
                "row": "[data-test-id=operation-cell]",
                "fields": {"amount": {"selector": "[data-test-id=transaction-status]"}},
            }
        ],
    }
    warnings = []
    rows = extract_section(
        PageAdapter(FakePlaywrightPage("https://example.invalid/")),
        profile["transactions"][0],
        "transactions",
        warnings,
        timeout_ms=1,
    )

    assert rows[0]["amount"]["value"] == "-1250.50"
    assert warnings == []


def test_collect_over_the_adapter_reports_the_source_url():
    profile = {
        "bank": "adapter",
        "status": "verified",
        "url": "https://example.invalid/",
        "wait_for": "",
        "balances": [],
        "transactions": [],
    }
    result = collect(PageAdapter(FakePlaywrightPage("https://example.invalid/")), profile, timeout_ms=1)

    assert result["source_url"] == "https://example.invalid/"
    assert "no balances extracted" in result["warnings"]
