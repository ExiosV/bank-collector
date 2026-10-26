"""Importing a hand-downloaded statement: shape, signs, privacy and exit codes."""

import csv
import json
import os
import re
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
    assert len(result["transactions"]) == 10


def test_statement_block_reports_file_rows_period_and_accounts():
    statement = _result()["statement"]

    assert statement["file"] == "alfa_statement.csv"
    assert statement["rows"] == 10
    assert statement["period"] == ["2026-09-28", "2026-10-05"]
    assert statement["accounts"] == [
        "Альфа-Счёт на минимальный остаток",
        "МИР",
    ]
    assert statement["own_transfers"] == 2


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
        "-5000",
        "5000",
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
    assert report["statement"]["rows"] == 10
    assert len(report["transactions"]) == 10

    out = capsys.readouterr().out
    assert "written: %s" % path in out
    assert "statement: 10 operations, 2026-09-28 .. 2026-10-05" in out


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


# ---------------------------------------------------------------------------
# A file that is not the bank's UTF-8 CSV export
# ---------------------------------------------------------------------------


def test_cp1251_file_is_rejected_as_not_utf8(tmp_path):
    path = tmp_path / "cp1251.csv"
    path.write_bytes(
        "operationDate,merchant\n05.10.2026,Пятёрочка\n".encode("cp1251")
    )

    with pytest.raises(StatementError) as raised:
        read_statement("alfa", str(path), now=NOW)

    assert "not a UTF-8 CSV export from the bank" in str(raised.value)
    assert "PDF/Excel" in str(raised.value)


def test_a_pdf_renamed_to_csv_is_rejected_without_a_traceback(tmp_path, capsys):
    path = tmp_path / "statement.csv"
    path.write_bytes(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n\x00\x00\x00\x00binary\x00")

    code = main(
        ["--bank", "alfa", "--import-statement", str(path), "--out", str(tmp_path / "out")]
    )

    assert code == EXIT_BAD_PROFILE
    err = capsys.readouterr().err
    assert "not a UTF-8 CSV export from the bank" in err
    assert "Traceback" not in err
    assert not (tmp_path / "out" / "alfa-statement.json").exists()


def test_a_csv_error_is_reported_as_a_bad_export(tmp_path):
    path = tmp_path / "huge-field.csv"
    path.write_text(
        "operationDate,transactionDate,accountName,accountNumber,cardName,"
        "merchant,amount,currency,status,category,mcc,type,comment\n"
        + "x" * 200000
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(StatementError) as raised:
        read_statement("alfa", str(path), now=NOW)

    assert "not a UTF-8 CSV export from the bank" in str(raised.value)


# ---------------------------------------------------------------------------
# No data rows vs rows present but none parsed
# ---------------------------------------------------------------------------


def test_a_header_only_export_is_empty(tmp_path):
    path = _write(tmp_path / "header-only.csv", [])

    with pytest.raises(EmptyStatementError):
        read_statement("alfa", path, now=NOW)


def test_rows_present_but_none_parsed_is_a_statement_error(tmp_path, capsys):
    path = _write(
        tmp_path / "unreadable.csv",
        [_row(operationDate="31.02.2026"), _row(amount="не-число")],
    )

    with pytest.raises(StatementError) as raised:
        read_statement("alfa", path, now=NOW)

    message = str(raised.value)
    assert "none of the 2 rows could be read" in message
    assert not isinstance(raised.value, EmptyStatementError)
    assert len(raised.value.warnings) == 2

    code = main(
        ["--bank", "alfa", "--import-statement", path, "--out", str(tmp_path / "out")]
    )
    assert code == EXIT_BAD_PROFILE
    err = capsys.readouterr().err
    assert "none of the 2 rows could be read" in err
    assert "line 2: cannot parse operationDate" in err
    assert "Traceback" not in err


# ---------------------------------------------------------------------------
# Statuses
# ---------------------------------------------------------------------------


def test_empty_and_completed_statuses_are_imported_the_rest_is_skipped(tmp_path):
    path = _write(
        tmp_path / "statuses.csv",
        [
            _row(status="", amount="100", type="Пополнение"),
            _row(status="Выполнен", amount="200", type="Пополнение"),
            _row(status="Отклонён", amount="300"),
            _row(status="Отклонён", amount="400"),
            _row(status="В обработке", amount="500"),
        ],
    )

    result = read_statement("alfa", path, now=NOW)

    assert [transaction["amount"]["value"] for transaction in result["transactions"]] == [
        "100",
        "200",
    ]
    warnings = result["warnings"]
    assert "2 rows with status 'Отклонён' skipped" in warnings
    assert "1 rows with status 'В обработке' skipped" in warnings
    assert len([text for text in warnings if "Отклонён" in text]) == 1


# ---------------------------------------------------------------------------
# Long digit runs are masked everywhere they are stored
# ---------------------------------------------------------------------------


def test_long_digit_runs_are_masked_in_text_and_in_the_file_name(tmp_path):
    name = "выписка 40817810000000000099.csv"
    path = _write(
        tmp_path / name,
        [
            _row(
                merchant="Оплата 220015******1234",
                comment="перевод на 40817810000000000099",
                accountName="Счёт 4081 7810 0000 0000 0099",
            )
        ],
    )

    result = read_statement("alfa", path, now=NOW)
    dump = json.dumps(result, ensure_ascii=False)

    assert "40817810000000000099" not in dump
    assert "220015******1234" not in dump
    assert re.search(r"\d{12,}", dump) is None
    transaction = result["transactions"][0]
    assert "***0099" in transaction["comment"]["value"]
    assert "***1234" in transaction["title"]["value"]
    assert "***0099" in transaction["account"]["value"]
    assert "***0099" in result["statement"]["file"]
    assert "***0099" in result["source_url"]


# ---------------------------------------------------------------------------
# Own transfers
# ---------------------------------------------------------------------------


def test_transfers_between_own_accounts_are_flagged_and_counted():
    result = _result()
    flags = {
        transaction["title"]["value"]: transaction["own_transfer"]
        for transaction in result["transactions"]
    }

    flagged = [
        transaction
        for transaction in result["transactions"]
        if transaction["own_transfer"]
    ]
    assert len(flagged) == 2
    assert {transaction["type"]["value"] for transaction in flagged} == {
        "Списание",
        "Пополнение",
    }
    assert all(transaction["title"]["value"] == "Перевод себе" for transaction in flagged)
    assert result["statement"]["own_transfers"] == 2
    assert flags["ПЯТЁРОЧКА"] is False


def test_a_transfer_needs_a_different_account_and_the_opposite_type(tmp_path):
    path = _write(
        tmp_path / "not-own.csv",
        [
            _row(type="Списание", amount="100", accountNumber=ACCOUNT_NUMBER),
            _row(type="Пополнение", amount="100", accountNumber=ACCOUNT_NUMBER),
        ],
    )

    result = read_statement("alfa", path, now=NOW)

    assert [transaction["own_transfer"] for transaction in result["transactions"]] == [
        False,
        False,
    ]
    assert result["statement"]["own_transfers"] == 0


# ---------------------------------------------------------------------------
# --format on the import path
# ---------------------------------------------------------------------------


def test_import_defaults_to_json_and_csv(tmp_path, capsys):
    out = tmp_path / "out"

    code = main(["--bank", "alfa", "--import-statement", STATEMENT, "--out", str(out)])

    assert code == EXIT_OK
    assert (out / "alfa-statement.json").exists()
    assert (out / "alfa-statement-transactions.csv").exists()
    assert capsys.readouterr().out.count("written:") == 2


def test_import_format_csv_writes_only_the_transactions_csv(tmp_path):
    out = tmp_path / "out"

    code = main(
        [
            "--bank",
            "alfa",
            "--import-statement",
            STATEMENT,
            "--format",
            "csv",
            "--out",
            str(out),
        ]
    )

    assert code == EXIT_OK
    assert not (out / "alfa-statement.json").exists()
    csv_path = out / "alfa-statement-transactions.csv"
    assert csv_path.exists()
    header = csv_path.read_text(encoding="utf-8").splitlines()[0]
    assert "amount" in header
    assert "own_transfer" in header


def test_import_format_json_writes_only_the_json(tmp_path):
    out = tmp_path / "out"

    code = main(
        [
            "--bank",
            "alfa",
            "--import-statement",
            STATEMENT,
            "--format",
            "json",
            "--out",
            str(out),
        ]
    )

    assert code == EXIT_OK
    assert (out / "alfa-statement.json").exists()
    assert not (out / "alfa-statement-transactions.csv").exists()


@pytest.mark.parametrize("flag", ["--json", "--csv"])
def test_stdout_format_flags_are_rejected_with_the_import(flag):
    with pytest.raises(SystemExit) as raised:
        main(["--bank", "alfa", "--import-statement", STATEMENT, flag])

    assert raised.value.code == 2
