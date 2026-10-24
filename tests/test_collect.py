"""Parsing a fixture through the generic driver — without Playwright."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fake_page import FakePage  # noqa: E402

from bank_collector.collect import collect, normalise_value, parse_money  # noqa: E402
from bank_collector.profiles import load_profile  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "example_dashboard.html")


def _fixture_page():
    with open(FIXTURE, "r", encoding="utf-8") as handle:
        return FakePage(handle.read())


def _profile():
    return load_profile("example")


def test_collect_balances_and_transactions():
    page = _fixture_page()
    result = collect(page, _profile())

    assert result["bank"] == "example"
    assert result["source_url"] == "https://example.invalid/cabinet"
    assert result["fetched_at"].endswith("Z")

    assert len(result["balances"]) == 2
    first = result["balances"][0]
    assert first["account"]["value"] == "Основной счёт • 0000"
    assert first["amount"]["value"] == "1234.56"
    assert first["amount"]["currency"] == "RUB"
    assert first["amount"]["raw"].strip() == "1 234,56 ₽"

    second = result["balances"][1]
    assert second["amount"]["value"] == "-500.00"
    assert second["amount"]["money"] is True

    assert len(result["transactions"]) == 3
    assert result["transactions"][0]["date"]["value"] == "01.02.2026"
    assert result["transactions"][0]["amount"]["value"] == "-1500.00"
    assert result["transactions"][1]["amount"]["value"] == "100000.00"
    # "1 500,00" style: no currency invented when the page showed none.
    assert result["transactions"][1]["amount"]["currency"] is None
    assert result["transactions"][2]["amount"]["value"] == "2750.25"


def test_collect_visits_the_profile_url_and_waits_for_the_selector():
    page = _fixture_page()
    collect(page, _profile())
    assert page.visited == ["https://example.invalid/cabinet"]
    assert page.waited_for == ["#dashboard"]


def test_collect_warns_when_nothing_matches():
    page = FakePage("<html><body><div id='dashboard'></div></body></html>")
    result = collect(page, _profile())

    assert result["balances"] == []
    assert result["transactions"] == []
    assert any("no balances extracted" in warning for warning in result["warnings"])
    assert any("no transactions extracted" in warning for warning in result["warnings"])


def test_money_parsing_keeps_sign_and_raw():
    value, currency = parse_money("1 234,56 ₽")
    assert value is not None and str(value) == "1234.56"
    assert currency == "RUB"

    value, currency = parse_money("−1 234,56")
    assert value is not None and str(value) == "-1234.56"
    assert currency is None

    value, currency = parse_money("1 234.56")
    assert value is not None and str(value) == "1234.56"

    # ещё форматы: пробел/NBSP как разделитель тысяч, обе десятичные запятые, знак минус
    for text, expected, cur in (
        ("-1 234,56 ₽", "-1234.56", "RUB"),
        ("1.234,56", "1234.56", None),
        ("1,234.56", "1234.56", None),
        ("1 234.56", "1234.56", None),
        ("−1 234,56", "-1234.56", None),
        ("1\u00a0234,56 ₽", "1234.56", "RUB"),
        ("+1 234,56", "1234.56", None),
    ):
        value, currency = parse_money(text)
        assert value is not None and str(value) == expected, (text, value)
        assert currency == cur, (text, currency)

    value, currency = parse_money("not a number")
    assert value is None and currency is None


def test_dates_are_not_money():
    value, currency = parse_money("01.02.2026")
    # 01.02.2026 must not be read as a decimal number.
    assert value is None or currency is None
    assert normalise_value("01.02.2026")["money"] is False


def test_currency_is_never_guessed():
    result = normalise_value("500,00")
    assert result["money"] is True
    assert result["currency"] is None
    assert result["value"] == "500.00"


def test_raw_text_is_preserved():
    result = normalise_value("  1 234,56 ₽  ")
    assert result["raw"] == "  1 234,56 ₽  "
    assert result["value"] == "1234.56"
