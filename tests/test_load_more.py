"""The one narrow click of the collector: the list's "load more" button.

Alfa-Bank shows 20 operations per screen and then a «Показать ещё» button.
Scrolling alone never reaches the older rows, so a profile may name that one
button. These tests pin down when it is pressed and — just as important —
when it is not: the button is searched only inside the one list container
named by ``scroll.load_more_scope``, only an allowlisted whole text may be
pressed, and no other control is ever touched.
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

#: The list container the tests scope the button to.
SCOPE = "div.operations"

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


def _group(date, title, amount):
    """One Alfa-like day group: a date element and an operation button."""
    return (
        "<div>"
        "<div data-date='%s'><span>день</span></div>"
        "<button data-test-id='operation-cell'>"
        "<div data-test-id='title-text_content'>%s</div>"
        "<div data-test-id='transaction-category-name'>Прочее</div>"
        "<div data-test-id='transaction-status'>%s ₽</div>"
        "</button></div>" % (DATE_FORMAT % date, title, amount)
    )


def _list(*days):
    """One list container holding the groups of *days*."""
    return "<div class='operations'>%s</div>" % "".join(_group(*day) for day in days)


def _history(days, extra_html="", outside_html=""):
    """An Alfa-like history page: one group per day, one operation per group.

    *extra_html* lands inside the list container (where the cabinet puts its
    own «Показать ещё» button); *outside_html* is a sibling of the list, for
    a same-label control of another widget (an offers block).
    """
    return (
        "<html><body>"
        "<div class='operations'>"
        + "".join(_group(*day) for day in days)
        + extra_html
        + "</div>"
        + outside_html
        + "</body></html>"
    )


def _day(number, title="Оплата", amount="-100,00"):
    return ("2026-03-%02d" % number, title, amount)


def _load_more_button():
    return "<button class='load-more'>%s</button>" % LOAD_MORE


def _append_days(page, days):
    """Append operation groups to the list, like an SPA does after a click."""
    fragment = parse_html("".join(_group(*day) for day in days))
    containers = page.query_all("div.operations")
    target = containers[0] if containers else page.root
    for group in list(fragment.children):
        target.append_child(group)


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
        "load_more_scope": SCOPE,
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


def test_a_label_span_inside_the_button_does_not_look_foreign():
    """A <span> wrapper inside the button is part of it, not a second control."""
    button = "<button class='load-more'><span>%s</span></button>" % LOAD_MORE
    page = FakePage(
        _history([_day(2), _day(1)], button),
        on_click=lambda page, element: _append_days(
            page, [("2025-12-31", "Старая операция", "-1,00")]
        ),
    )

    result = collect(page, _profile(_scroll(stop_before_date="2026-01-01")))

    assert result["scroll"]["clicks"] == 1
    assert result["scroll"]["complete"] is True
    assert _warnings(result) == []


def test_without_the_button_the_list_ends_normally():
    page = FakePage(_history([_day(1), _day(2)]))

    result = collect(page, _profile(_scroll(stop_after_empty_rounds=2)))

    assert result["scroll"]["stop"] == "empty"
    assert result["scroll"]["clicks"] == 0
    assert result["scroll"]["load_more_missing"] >= 1, "the list had no button"
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
        page,
        _profile(
            _scroll(
                load_more_text=None,
                load_more_scope=None,
                stop_after_empty_rounds=2,
            )
        ),
    )

    assert result["scroll"]["stop"] == "empty"
    assert result["scroll"]["clicks"] == 0
    assert result["scroll"]["load_more_missing"] == 0, "the feature is off"
    assert page.writes == [], "without load_more_text the button is just a button"


# ---------------------------------------------------------------------------
# The guards: scope, whole-text equality and "only when scrolling stopped"
# ---------------------------------------------------------------------------


def test_a_superstring_button_inside_the_scope_is_not_clicked():
    """The whole text must be equal — "Показать ещё 5" is a different label."""
    page = FakePage(
        _history([_day(1)], "<button class='load-more'>Показать ещё 5</button>")
    )

    result = collect(page, _profile(_scroll()))

    assert page.writes == [], "a superstring must not be pressed"
    assert result["scroll"]["clicks"] == 0
    assert result["scroll"]["stop"] == "empty"
    assert result["scroll"]["load_more_missing"] >= 1


def test_the_button_is_not_clicked_while_scrolling_still_adds_rows():
    """The click waits until a whole scroll round added nothing."""

    def grow(page):
        if len(page.scrolls) <= 3:
            _append_days(page, [_day(10 + len(page.scrolls))])

    clicks_at = []
    page = FakePage(
        _history([_day(1)], _load_more_button()),
        on_scroll=grow,
        on_click=lambda page, element: clicks_at.append(len(page.scrolls)),
    )

    result = collect(page, _profile(_scroll(stop_after_empty_rounds=1)))

    assert clicks_at == [4], "clicked only after the 4th scroll added no rows"
    assert result["scroll"]["clicks"] == 1
    assert result["scroll"]["rows"] == 4, "the first screen plus three scrolled rows"
    assert result["scroll"]["stop"] == "empty"


def test_a_same_label_button_outside_the_scope_is_never_clicked():
    """An offers block with the same label must not be pressed."""
    offers = "<div class='offers'><button>Показать ещё</button></div>"
    page = FakePage(_history([_day(1)], outside_html=offers))

    result = collect(page, _profile(_scroll()))

    assert page.writes == [], "a button outside the list is never clicked"
    assert result["scroll"]["clicks"] == 0
    assert result["scroll"]["stop"] == "empty"
    assert result["scroll"]["complete"] is False, "the list may be hiding its next page"
    assert _warnings(result) == [
        "transactions: 'Показать ещё' found outside the list or not as a button (1), "
        "not clicked — history may be incomplete"
    ]


def test_a_same_label_anchor_outside_the_scope_is_never_clicked():
    """A link with the same label outside the list forces complete=False."""
    offers = "<div class='offers'><a href='#'>Показать ещё</a></div>"
    page = FakePage(_history([_day(1)], outside_html=offers))

    result = collect(page, _profile(_scroll()))

    assert page.writes == [], "a link outside the list is never clicked"
    assert result["scroll"]["clicks"] == 0
    assert result["scroll"]["stop"] == "empty"
    assert result["scroll"]["complete"] is False
    assert _warnings(result) == [
        "transactions: 'Показать ещё' found outside the list or not as a button (1), "
        "not clicked — history may be incomplete"
    ]


def test_a_div_with_the_label_inside_the_scope_forces_incomplete():
    """A plain div is not a button, but it may be hiding the list's next page."""
    page = FakePage(
        _history([_day(1)], "<div class='load-more-lookalike'>Показать ещё</div>")
    )

    result = collect(page, _profile(_scroll()))

    assert page.writes == [], "a div is never clicked"
    assert result["scroll"]["clicks"] == 0
    assert result["scroll"]["stop"] == "empty"
    assert result["scroll"]["complete"] is False
    assert _warnings(result) == [
        "transactions: 'Показать ещё' found outside the list or not as a button (1), "
        "not clicked — history may be incomplete"
    ]


def test_a_div_role_button_with_the_label_counts_once():
    """One element is one control: [role=button] is not also a text lookalike."""
    page = FakePage(
        _history([_day(1)], "<div role='button'>Показать ещё</div>")
    )

    result = collect(page, _profile(_scroll()))

    assert page.writes == [], "a div is never clicked"
    assert result["scroll"]["clicks"] == 0
    assert result["scroll"]["complete"] is False
    assert _warnings(result) == [
        "transactions: 'Показать ещё' found outside the list or not as a button (1), "
        "not clicked — history may be incomplete"
    ]


def test_a_row_less_wrapper_matching_the_scope_is_filtered_out():
    """Only containers that hold rows count: an empty match must not stop the click."""
    page = FakePage(
        _history([_day(2), _day(1)], _load_more_button(), outside_html="<div class='operations'></div>"),
        on_click=lambda page, element: _append_days(
            page, [("2025-12-31", "Старая операция", "-1,00")]
        ),
    )

    result = collect(page, _profile(_scroll(stop_before_date="2026-01-01")))

    assert result["scroll"]["clicks"] == 1
    assert result["scroll"]["rows"] == 3
    assert result["scroll"]["stop"] == "date"
    assert result["scroll"]["complete"] is True
    assert page.writes == [("click", None)]


def test_a_scope_matching_no_container_never_clicks():
    page = FakePage(_history([_day(1)], _load_more_button()))

    result = collect(page, _profile(_scroll(load_more_scope="div.absent")))

    assert page.writes == []
    assert result["scroll"]["clicks"] == 0
    assert result["scroll"]["stop"] == "error"
    assert _warnings(result) == [
        "transactions: load-more scope 'div.absent' matched 0 list containers, "
        "none clicked"
    ]


def test_a_scope_matching_two_containers_never_clicks():
    html = "<html><body>%s%s</body></html>" % (_list(_day(1)), _list(_day(2)))
    page = FakePage(html)

    result = collect(page, _profile(_scroll()))

    assert page.writes == []
    assert result["scroll"]["clicks"] == 0
    assert result["scroll"]["stop"] == "error"
    assert _warnings(result) == [
        "transactions: load-more scope 'div.operations' matched 2 list containers, "
        "none clicked"
    ]


# ---------------------------------------------------------------------------
# Profile validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["42", "''", "'   '"])
def test_load_more_text_must_be_a_non_empty_string(tmp_path, value):
    with pytest.raises(ProfileError, match="load_more_text"):
        _load(tmp_path, "bad_text", "      load_more_text: %s\n" % value)


@pytest.mark.parametrize(
    "label",
    [
        "Скачать выписку",
        "Оплатить",
        "Перевести",
        "Загрузить ещё",
        "Загрузить еще",
        "Ещё",
        "Еще",
        "Show more",
        "Load more",
    ],
)
def test_load_more_text_must_be_an_allowed_label(tmp_path, label):
    with pytest.raises(ProfileError, match="load-more labels"):
        _load(
            tmp_path,
            "bad_label",
            '      load_more_text: "%s"\n      load_more_scope: "%s"\n' % (label, SCOPE),
        )


def test_load_more_text_requires_a_scope(tmp_path):
    with pytest.raises(ProfileError, match="load_more_scope"):
        _load(tmp_path, "no_scope", '      load_more_text: "%s"\n' % LOAD_MORE)


def test_load_more_scope_requires_a_text(tmp_path):
    with pytest.raises(ProfileError, match="load_more_scope"):
        _load(tmp_path, "scope_only", '      load_more_scope: "%s"\n' % SCOPE)


def test_the_allowlisted_label_is_stored_in_canonical_casing(tmp_path):
    profile = _load(
        tmp_path,
        "folded",
        '      load_more_text: "показать ЕЩЁ"\n      load_more_scope: "%s"\n' % SCOPE,
    )

    assert (
        profile["transactions"][0]["scroll"]["load_more_text"] == "Показать ещё"
    ), "the allowlist's own spelling is stored"


def test_a_case_insensitive_profile_label_clicks_the_canonical_button(tmp_path):
    page = FakePage(
        _history([_day(2), _day(1)], _load_more_button()),
        on_click=lambda page, element: _append_days(
            page, [("2025-12-31", "Старая операция", "-1,00")]
        ),
    )

    result = collect(
        page,
        _profile(
            _scroll(load_more_text="показать ЕЩЁ", stop_before_date="2026-01-01")
        ),
    )

    assert result["scroll"]["clicks"] == 1
    assert result["scroll"]["rows"] == 3
    assert result["scroll"]["stop"] == "date"
    assert result["scroll"]["complete"] is True


def test_a_canonical_profile_label_clicks_a_differently_cased_button():
    page = FakePage(
        _history([_day(2), _day(1)], "<button class='load-more'>ПОКАЗАТЬ ЕЩЁ</button>"),
        on_click=lambda page, element: _append_days(
            page, [("2025-12-31", "Старая операция", "-1,00")]
        ),
    )

    result = collect(page, _profile(_scroll(stop_before_date="2026-01-01")))

    assert result["scroll"]["clicks"] == 1
    assert result["scroll"]["stop"] == "date"


@pytest.mark.parametrize("value", ["0", "-3", "true", "'5'"])
def test_max_clicks_must_be_a_positive_integer(tmp_path, value):
    with pytest.raises(ProfileError, match="max_clicks"):
        _load(
            tmp_path,
            "bad_clicks",
            '      load_more_text: "%s"\n      load_more_scope: "%s"\n'
            "      max_clicks: %s\n" % (LOAD_MORE, SCOPE, value),
        )


def test_the_new_keys_have_defaults(tmp_path):
    profile = _load(tmp_path, "defaults", "")
    scroll = profile["transactions"][0]["scroll"]

    assert scroll["load_more_text"] is None, "the click is off unless a profile asks"
    assert scroll["load_more_scope"] is None
    assert scroll["max_clicks"] == 100


def test_the_alfa_profile_names_the_load_more_button():
    scroll = load_profile("alfa")["transactions"][0]["scroll"]

    assert scroll["load_more_text"] == LOAD_MORE
    assert scroll["load_more_scope"], "a scope is required with the label"
    assert scroll["max_clicks"] == 100
