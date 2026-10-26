"""The one narrow click of the collector: the list's "load more" button.

Alfa-Bank shows 20 operations per screen and then a «Показать ещё» button.
Scrolling alone never reaches the older rows, so a profile may name that one
button. These tests pin down when it is pressed and — just as important —
when it is not, and that no other control is ever touched.
"""

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from bank_collector.collect import collect  # noqa: E402
from bank_collector.profiles import ProfileError, load_profile  # noqa: E402
from fake_page import FakePage, parse_html  # noqa: E402

DATE_FORMAT = "%sT10:00:00.000+0300"
LOAD_MORE = "Показать ещё"

#: The beginning of a profile with a scrollable record; tests append keys.
_PROFILE_PREFIX = (
    "bank: load_more\n"
    "status: verified\n"
    'url: "https://x.invalid/"\n'
    'wait_for: "#x"\n'
    "balances: []\n"
    "transactions:\n"
    "  - row: '.row'\n"
    "    fields:\n"
    "      x:\n"
    '        selector: ".x"\n'
    "    scroll:\n"
    "      max_rounds: 10\n"
)


def _history(days, extra_html=""):
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
    parts.append(extra_html)
    parts.append("</div></body></html>")
    return "".join(parts)


def _day(number, title="Оплата", amount="-100,00"):
    return ("2026-03-%02d" % number, title, amount)


def _load_more_button():
    return "<button class='load-more'>%s</button>" % LOAD_MORE


def _append_days(page, days):
    """Append operation groups to the page, like an SPA does after a click."""
    fragment = parse_html(_history(days))
    for group in fragment.query_all("div:has(> [data-date])"):
        page.root.append_child(group)


def _record(scroll):
    return {
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
        "scroll": scroll,
    }


def _profile(scroll):
    return {
        "bank": "synthetic",
        "status": "verified",
        "url": "https://synthetic.invalid/history/",
        "wait_for": "",
        "balances": [],
        "transactions": [_record(scroll)],
    }


def _scroll(**overrides):
    config = {
        "max_rounds": 20,
        "step_px": 1200,
        "settle_ms": 0,
        "stop_after_empty_rounds": 2,
        "stop_before_date": None,
        "load_more_text": LOAD_MORE,
        "max_clicks": 100,
    }
    config.update(overrides)
    return config


def _dates(result):
    return [row["date"]["value"][:10] for row in result["transactions"]]


def _warnings(result):
    """The warnings of a run, without the 'no balances' noise of the fixture."""
    return [text for text in result["warnings"] if text != "no balances extracted"]


def _load(tmp_path, name, extra):
    path = tmp_path / ("%s.yaml" % name)
    path.write_text(_PROFILE_PREFIX + extra, encoding="utf-8")
    return load_profile(name, directory=str(tmp_path))


# ---------------------------------------------------------------------------
# The click
# ---------------------------------------------------------------------------


def test_a_click_appends_the_older_rows_and_reaches_the_date():
    page = FakePage(
        _history([_day(2), _day(1)], _load_more_button()),
        on_click=lambda page, element: _append_days(
            page, [("2025-12-31", "Старая операция", "-1,00")]
        ),
    )

    result = collect(page, _profile(_scroll(stop_before_date="2026-01-01")))

    assert _dates(result) == ["2026-03-02", "2026-03-01", "2025-12-31"]
    assert result["scroll"]["stop"] == "date"
    assert result["scroll"]["complete"] is True
    assert result["scroll"]["rows"] == 3
    assert result["scroll"]["clicks"] == 1
    assert _warnings(result) == []


def test_only_the_load_more_button_is_clicked():
    """No fill, no click on «Скачать выписку», no click anywhere else."""
    html = _history(
        [_day(2), _day(1)],
        "<button data-test-id='download'>Скачать выписку</button>"
        + _load_more_button(),
    )
    page = FakePage(
        html,
        on_click=lambda page, element: _append_days(
            page, [("2025-12-31", "Старая операция", "-1,00")]
        ),
    )

    collect(page, _profile(_scroll(stop_before_date="2026-01-01")))

    assert page.writes == [("click", None)], "exactly one write, and it is a click"
    assert all(kind != "fill" for kind, _value in page.writes)

    by_text = {element.text_content(): element for element in page.query_all("button")}
    assert by_text[LOAD_MORE].writes == [("click", None)]
    assert by_text["Скачать выписку"].writes == []


def test_without_the_button_the_list_ends_normally():
    page = FakePage(_history([_day(1), _day(2)]))

    result = collect(page, _profile(_scroll(stop_after_empty_rounds=2)))

    assert result["scroll"]["stop"] == "empty"
    assert result["scroll"]["clicks"] == 0
    assert page.writes == [], "there was nothing to click"


def test_two_buttons_with_the_text_are_never_clicked():
    buttons = (
        "<button class='load-more'>%s</button>"
        "<button class='load-more-again'>%s</button>" % (LOAD_MORE, LOAD_MORE)
    )
    page = FakePage(_history([_day(1)], buttons))

    result = collect(page, _profile(_scroll()))

    assert result["scroll"]["stop"] == "error"
    assert result["scroll"]["clicks"] == 0
    assert page.writes == []
    assert _warnings(result) == [
        "transactions: 2 buttons named 'Показать ещё', none clicked"
    ]


def test_the_click_limit_is_reported_with_a_warning():
    appended = []

    def on_click(page, element):
        appended.append(1)
        _append_days(
            page, [("2026-02-%02d" % len(appended), "Догрузка", "-1,00")]
        )

    page = FakePage(_history([_day(2), _day(1)], _load_more_button()), on_click=on_click)

    result = collect(page, _profile(_scroll(max_clicks=2, max_rounds=20)))

    assert result["scroll"]["clicks"] == 2
    assert result["scroll"]["stop"] == "limit"
    assert result["scroll"]["rounds"] == 5, "two scroll+click pairs and one last scroll"
    assert result["scroll"]["rows"] == 4, "the first screen plus two clicked rows"
    assert _warnings(result) == [
        "transactions: scroll limit reached after 5 rounds (4 rows, history may be incomplete)"
    ]
    assert page.writes == [("click", None), ("click", None)]


def test_the_feature_off_never_clicks_the_button():
    page = FakePage(_history([_day(1)], _load_more_button()))

    result = collect(
        page, _profile(_scroll(load_more_text=None, stop_after_empty_rounds=2))
    )

    assert result["scroll"]["stop"] == "empty"
    assert result["scroll"]["clicks"] == 0
    assert page.writes == [], "without load_more_text the button is just a button"


# ---------------------------------------------------------------------------
# Profile validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["42", "''", "'   '"])
def test_load_more_text_must_be_a_non_empty_string(tmp_path, value):
    with pytest.raises(ProfileError, match="load_more_text"):
        _load(tmp_path, "bad_text", "      load_more_text: %s\n" % value)


@pytest.mark.parametrize("value", ["0", "-3", "true", "'5'"])
def test_max_clicks_must_be_a_positive_integer(tmp_path, value):
    with pytest.raises(ProfileError, match="max_clicks"):
        _load(
            tmp_path,
            "bad_clicks",
            '      load_more_text: "%s"\n      max_clicks: %s\n' % (LOAD_MORE, value),
        )


def test_the_new_keys_have_defaults(tmp_path):
    profile = _load(tmp_path, "defaults", "")
    scroll = profile["transactions"][0]["scroll"]

    assert scroll["load_more_text"] is None, "the click is off unless a profile asks"
    assert scroll["max_clicks"] == 100


def test_the_alfa_profile_names_the_load_more_button():
    scroll = load_profile("alfa")["transactions"][0]["scroll"]

    assert scroll["load_more_text"] == LOAD_MORE
    assert scroll["max_clicks"] == 100
