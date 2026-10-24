"""The JSON report and the CSV export."""

import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bank_collector.report import SCHEMA_VERSION, build_report, write_csv, write_json  # noqa: E402


def _collected():
    return {
        "bank": "example",
        "status": "verified",
        "source_url": "https://example.invalid/cabinet",
        "fetched_at": "2026-02-01T10:00:00Z",
        "balances": [
            {
                "amount": {"raw": "1 234,56 ₽", "value": "1234.56", "currency": "RUB", "money": True},
                "account": {"raw": "Основной", "value": "Основной", "currency": None, "money": False},
            }
        ],
        "transactions": [
            {
                "date": {"raw": "01.02.2026", "value": "01.02.2026", "currency": None, "money": False},
                "amount": {"raw": "−1 500,00 ₽", "value": "-1500.00", "currency": "RUB", "money": True},
                "payee": {"raw": "ООО Ромашка", "value": "ООО Ромашка", "currency": None, "money": False},
            }
        ],
        "warnings": [],
    }


def test_build_report_has_the_documented_keys():
    report = build_report(_collected())
    assert report["schema_version"] == SCHEMA_VERSION
    assert report["bank"] == "example"
    assert report["fetched_at"] == "2026-02-01T10:00:00Z"
    assert isinstance(report["balances"], list)
    assert isinstance(report["transactions"], list)
    assert report["warnings"] == []


def test_write_json_roundtrip(tmp_path):
    report = build_report(_collected())
    path = write_json(report, str(tmp_path / "out" / "example.json"))
    assert os.path.isfile(path)

    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)

    assert data["schema_version"] == SCHEMA_VERSION
    assert data["bank"] == "example"
    assert data["balances"][0]["amount"]["value"] == "1234.56"
    assert data["transactions"][0]["amount"]["currency"] == "RUB"


def test_write_json_of_empty_result(tmp_path):
    collected = _collected()
    collected["balances"] = []
    collected["transactions"] = []
    report = build_report(collected)

    path = write_json(report, str(tmp_path / "empty.json"))
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)

    assert data["balances"] == []
    assert data["transactions"] == []
    assert data["schema_version"] == SCHEMA_VERSION


def test_write_csv_flattens_records(tmp_path):
    report = build_report(_collected())
    path = write_csv(report["transactions"], str(tmp_path / "transactions.csv"))

    with open(path, "r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))

    assert len(rows) == 1
    assert rows[0]["date"] == "01.02.2026"
    assert rows[0]["amount"] == "-1500.00"
    assert rows[0]["amount_currency"] == "RUB"
    assert rows[0]["payee"] == "ООО Ромашка"


def test_write_csv_of_empty_list_keeps_the_header(tmp_path):
    report = build_report(_collected())
    report["transactions"] = []
    path = write_csv(report["transactions"], str(tmp_path / "empty.csv"), columns=["date", "amount"])

    with open(path, "r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))

    assert rows == []
    with open(path, "r", encoding="utf-8") as handle:
        assert handle.readline().strip() == "date,amount"
