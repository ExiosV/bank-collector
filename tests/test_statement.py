"""Importing a hand-downloaded statement: shape, signs, privacy and exit codes."""

import csv
import json
import os
from datetime import datetime, timezone

import pytest

from bank_collector.cli import (
    EXIT_BAD_PROFILE,
    EXIT_NOTHING,
    EXIT_OK,
    main,
)
from bank_collector.statement import (
    EmptyStatementError,
    StatementError,
    read_statement,
)

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
STATEMENT = os.path.join(FIXTURES, "alfa_statement.csv")
EMPTY = os.path.join(FIXTURES, "alfa_statement_empty.csv")

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)

HEADER = [
    "operationDate",
    "transactionDate",
    "accountName",
    "accountNumber",
    "cardName",
    "cardNumber",
    "merchant",
    "amount",
    "currency",
    "status",
    "category",
    "mcc",
    "type",
    "comment",
    "bonusValue",
    "bonusTitle",
]

ACCOUNT = "МИР"
ACCOUNT_NUMBER = "40817810000000000001"
CARD_NUMBER = "220015******1234"


def _write(path, rows, header=HEADER):
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)
    return str(path)


def _row(**overrides):
    row = {
        "operationDate": "05.10.2026",
        "transactionDate": "05.10.2026",
        "accountName": ACCOUNT,
        "accountNumber": ACCOUNT_NUMBER,
        "cardName": "Альфа-Карта МИР",
        "cardNumber": CARD_NUMBER,
        "merchant": "ПЯТЁРОЧКА",
        "amount": "1250.5",
        "currency": "RUR",
        "status": "Выполнен",
        "category": "Супермаркеты",
        "mcc": "5411",
        "type": "Списание",
        "comment": "",
        "bonusValue": "+12",
        "bonusTitle": "Рубли",
    }
    row.update(overrides)
    return [row[name] for name in HEADER]


def _result():
    return read_statement("alfa", STATEMENT, now=NOW)


def test_read_statement_shaped_like_a_collect_run():
    result = _result()

    assert result["bank"] == "alfa"
    assert result["status"] == "statement"
    assert result["source_url"] == "file:alfa_statement.csv"
    assert result["source_urls"] == ["file:alfa_statement.csv"]
    assert result["fetched_at"] == "2026-10-26T12:00:00Z"
    assert result["balances"] == []
    assert len(result["transactions"]) == 8


def test_statement_block_reports_file_rows_period_and_accounts():
    statement = _result()["statement"]

    assert statement["file"] == "alfa_statement.csv"
    assert statement["rows"] == 8
    assert statement["period"] == ["2026-09-28", "2026-10-05"]
    assert statement["accounts"] == [
        "Альфа-Счёт на минимальный остаток",
        "МИР",
    ]


def test_every_field_is_a_leaf():
    for transaction in _result()["transactions"]:
        for name in (
            "date",
            "transaction_date",
            "title",
            "category",
            "type",
            "status",
            "comment",
            "mcc",
            "card",
            "account",
            "account_last4",
            "amount",
        ):
            leaf = transaction[name]
            assert set(leaf) == {"raw", "value", "currency", "money"}

        assert transaction["amount"]["money"] is True
        assert transaction["date"]["money"] is False


def test_dates_are_iso_with_the_original_raw():
    first = _result()["transactions"][0]

    assert first["date"] == {
        "raw": "05.10.2026",
        "value": "2026-10-05",
        "currency": None,
        "money": False,
    }
    assert first["transaction_date"]["raw"] == "05.10.2026"
    assert first["transaction_date"]["value"] == "2026-10-05"


def test_spisanie_is_negative_and_popolnenie_positive():
    amounts = [transaction["amount"]["value"] for transaction in _result()["transactions"]]

    assert amounts == [
        "-1250.5",
        "69803",
        "-20000",
        "-320",
        "1500",
        "-89.99",
        "12000",
        "-550",
    ]
    assert _result()["transactions"][0]["amount"]["raw"] == "1250.5"


def test_rur_becomes_rub():
    for transaction in _result()["transactions"]:
        assert transaction["amount"]["currency"] == "RUB"


def test_full_account_and_card_numbers_are_never_stored():
    result = _result()
    dump = json.dumps(result, ensure_ascii=False)

    for secret in ("40817810000000000001", "40817810000000000002", CARD_NUMBER, "220015"):
        assert secret not in dump

    assert {transaction["account_last4"]["value"] for transaction in result["transactions"]} == {
        "0001",
        "0002",
    }
    # The card column carries the card's name, never its number.
    assert {transaction["card"]["value"] for transaction in result["transactions"]} == {
        "",
        "Альфа-Карта МИР",
    }


def test_account_name_collapses_nbsp():
    result = _result()

    values = [transaction["account"]["value"] for transaction in result["transactions"]]
    assert all("\xa0" not in value for value in values)
    assert "Альфа-Счёт на минимальный остаток" in values
    # The statement block and the normalised value agree.
    assert "Альфа-Счёт на минимальный остаток" in result["statement"]["accounts"]
    assert all("\xa0" not in account for account in result["statement"]["accounts"])


def test_title_falls_back_to_comment_then_type():
    titles = [transaction["title"]["value"] for transaction in _result()["transactions"]]

    assert "Зарплата" in titles  # merchant empty, comment used
    assert "Пополнение счёта" in titles  # merchant empty, comment used
    assert titles[2] == "Списание"  # merchant and comment empty, type used


def test_empty_export_is_an_error_not_an_empty_success():
    with pytest.raises(EmptyStatementError) as raised:
        read_statement("alfa", EMPTY, now=NOW)

    assert "no operations" in str(raised.value)
    assert isinstance(raised.value, StatementError)


def test_unknown_bank_names_the_known_ones():
    with pytest.raises(StatementError) as raised:
        read_statement("nope", STATEMENT, now=NOW)

    assert "alfa" in str(raised.value)


def test_missing_columns_are_named(tmp_path):
    path = _write(
        tmp_path / "broken.csv",
        [_row()],
        header=[name for name in HEADER if name != "amount"],
    )

    with pytest.raises(StatementError) as raised:
        read_statement("alfa", path, now=NOW)

    message = str(raised.value)
    assert "missing required columns" in message
    assert "amount" in message


def test_a_malformed_row_is_skipped_with_a_warning_that_keeps_values_out(tmp_path):
    path = _write(
        tmp_path / "broken.csv",
        [
            _row(),
            _row(operationDate="31.02.2026"),
            _row(amount="не-число"),
        ],
    )

    result = read_statement("alfa", path, now=NOW)

    assert len(result["transactions"]) == 1
    warnings = "\n".join(result["warnings"])
    assert "line 3: cannot parse operationDate, row skipped" in warnings
    assert "line 4: cannot parse amount, row skipped" in warnings
    assert "31.02.2026" not in warnings
    assert "не-число" not in warnings


def test_an_unknown_type_warns_once_and_keeps_the_amount_positive(tmp_path):
    path = _write(
        tmp_path / "odd-type.csv",
        [
            _row(type="Хм", amount="100"),
            _row(type="Хм", amount="200"),
        ],
    )

    result = read_statement("alfa", path, now=NOW)

    assert [transaction["amount"]["value"] for transaction in result["transactions"]] == [
        "100",
        "200",
    ]
    unknown = [warning for warning in result["warnings"] if "unknown operation type" in warning]
    assert len(unknown) == 1
    assert "'Хм'" in unknown[0]


def test_cli_writes_the_statement_file_and_exits_zero(tmp_path, capsys):
    code = main(
        ["--bank", "alfa", "--import-statement", STATEMENT, "--out", str(tmp_path)]
    )

    assert code == EXIT_OK
    path = tmp_path / "alfa-statement.json"
    assert path.exists()
    assert not (tmp_path / "alfa.json").exists()

    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["status"] == "statement"
    assert report["statement"]["rows"] == 8
    assert len(report["transactions"]) == 8

    out = capsys.readouterr().out
    assert "written: %s" % path in out
    assert "statement: 8 operations, 2026-09-28 .. 2026-10-05" in out


def test_cli_unknown_bank_exits_3(tmp_path):
    code = main(["--bank", "nope", "--import-statement", STATEMENT, "--out", str(tmp_path)])

    assert code == EXIT_BAD_PROFILE
    assert not (tmp_path / "nope-statement.json").exists()


def test_cli_missing_columns_exits_3(tmp_path):
    path = _write(
        tmp_path / "broken.csv",
        [_row()],
        header=[name for name in HEADER if name != "amount"],
    )

    code = main(["--bank", "alfa", "--import-statement", path, "--out", str(tmp_path)])

    assert code == EXIT_BAD_PROFILE


def test_cli_unreadable_file_exits_3(tmp_path):
    code = main(
        [
            "--bank",
            "alfa",
            "--import-statement",
            str(tmp_path / "missing.csv"),
            "--out",
            str(tmp_path),
        ]
    )

    assert code == EXIT_BAD_PROFILE


def test_cli_empty_export_exits_4(tmp_path):
    code = main(["--bank", "alfa", "--import-statement", EMPTY, "--out", str(tmp_path)])

    assert code == EXIT_NOTHING
    assert not (tmp_path / "alfa-statement.json").exists()


def test_cli_rejects_browser_options():
    with pytest.raises(SystemExit) as raised:
        main(["--bank", "alfa", "--import-statement", STATEMENT, "--enter-code"])
    assert raised.value.code == 2

    with pytest.raises(SystemExit) as raised:
        main(
            [
                "--bank",
                "alfa",
                "--import-statement",
                STATEMENT,
                "--cdp-url",
                "http://127.0.0.1:9999",
            ]
        )
    assert raised.value.code == 2
