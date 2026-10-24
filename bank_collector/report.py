"""Building the JSON report and the CSV exports.

The JSON document is the artefact of this tool::

    {
      "schema_version": "1",
      "bank": "example",
      "status": "verified",
      "source_url": "https://example.invalid/cabinet",
      "fetched_at": "2026-01-01T00:00:00Z",
      "balances":    [ { "amount": {"raw": "...", "value": "1234.56", ...} } ],
      "transactions":[ { "date": {...}, "amount": {...} } ],
      "warnings":    [ "..." ]
    }

Everything is written to the local disk by the caller. Nothing is sent
anywhere: this module has no network code at all.
"""

import csv
import json
import os

__all__ = [
    "SCHEMA_VERSION",
    "build_report",
    "write_json",
    "write_csv",
    "flatten",
]

SCHEMA_VERSION = "1"


def build_report(collected, schema_version=SCHEMA_VERSION):
    """Turn the dict returned by :func:`bank_collector.collect.collect` into a report."""
    return {
        "schema_version": schema_version,
        "bank": collected.get("bank"),
        "status": collected.get("status"),
        "source_url": collected.get("source_url"),
        "fetched_at": collected.get("fetched_at"),
        "balances": collected.get("balances", []),
        "transactions": collected.get("transactions", []),
        "warnings": collected.get("warnings", []),
    }


def write_json(report, path):
    """Write *report* as UTF-8 JSON. Returns the path written."""
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=False)
        handle.write("\n")
    return path


def _field_columns(rows):
    columns = []
    for row in rows:
        for name in row:
            if name not in columns:
                columns.append(name)
    return columns


def flatten(rows):
    """Flatten normalised field dicts into plain CSV-friendly strings.

    Each field contributes the normalised ``value``; when the field has a
    currency the column becomes ``<field>_currency``.
    """
    flattened = []
    for row in rows:
        item = {}
        for name, field in row.items():
            if not isinstance(field, dict):
                item[name] = field
                continue
            item[name] = field.get("value")
            if field.get("currency"):
                item["%s_currency" % name] = field["currency"]
        flattened.append(item)
    return flattened


def write_csv(rows, path, columns=None):
    """Write *rows* (normalised field dicts) as a CSV file. Returns the path."""
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)

    flattened = flatten(rows)
    if columns is None:
        columns = _field_columns(flattened)

    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for item in flattened:
            writer.writerow({key: ("" if item.get(key) is None else item.get(key)) for key in columns})
    return path
