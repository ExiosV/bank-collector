"""Import a statement file that was downloaded by hand.

The web cabinet only shows a limited history, so the owner may export a
full statement from the cabinet (all accounts, CSV) and import that file
instead.  The import is purely local: the file is read from the disk and no
browser is attached, no page is opened and nothing is sent anywhere.

The result has the same shape as :func:`bank_collector.collect.collect`, so
the report builder and the CSV writer accept it unchanged.  One more key is
added::

    "statement": {
        "file": "alfa_statement.csv",
        "rows": 8,
        "period": ["2026-09-28", "2026-10-05"],
        "accounts": ["Альфа-Счёт на минимальный остаток", "МИР"]
    }

Privacy rules of the import:

* the full account number and the full card number are **never** stored —
  only the last four digits of the account number (``account_last4``) and
  the card *name* (``card``);
* an unreadable row is reported by its line number and the column name,
  never by its values;
* a warning about an unknown operation type names the type only.
"""

import csv
import os
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

__all__ = [
    "StatementError",
    "EmptyStatementError",
    "EMPTY_EXPORT_MESSAGE",
    "STATEMENT_PARSERS",
    "read_statement",
    "read_alfa_statement",
]

#: The message of an empty export; the CLI maps it to ``EXIT_NOTHING``.
EMPTY_EXPORT_MESSAGE = (
    "the statement has no operations - the export is empty; "
    "choose all accounts and export again"
)


class StatementError(Exception):
    """The statement cannot be read: unknown bank, bad file, missing columns."""


class EmptyStatementError(StatementError):
    """The export holds no operations at all (an empty statement)."""


#: The columns of the Alfa-Bank CSV export, in their observed order.
ALFA_COLUMNS = (
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
)

#: The columns the parser actually reads.  ``cardNumber``, ``bonusValue`` and
#: ``bonusTitle`` are deliberately absent: they are never needed and the full
#: card number must not even enter the parsed data.
ALFA_REQUIRED_COLUMNS = (
    "operationDate",
    "transactionDate",
    "accountName",
    "accountNumber",
    "cardName",
    "merchant",
    "amount",
    "currency",
    "status",
    "category",
    "mcc",
    "type",
    "comment",
)

#: ``Списание`` is money out, ``Пополнение`` is money in.
OUTGOING = "Списание"
INCOMING = "Пополнение"

#: How many digits of the account number are kept (the rest is never stored).
LAST4 = 4


def _text_leaf(raw):
    """A text field in the leaf shape of :func:`collect.normalise_value`."""
    text = "" if raw is None else str(raw)
    return {"raw": text, "value": text.strip(), "currency": None, "money": False}


def _account_leaf(raw):
    """An account label with the NBSP the cabinet writes collapsed to a space."""
    text = "" if raw is None else str(raw)
    return {
        "raw": text,
        "value": text.replace("\xa0", " ").strip(),
        "currency": None,
        "money": False,
    }


def _date_leaf(raw, iso):
    text = "" if raw is None else str(raw)
    return {"raw": text, "value": iso, "currency": None, "money": False}


def _iso_date(raw):
    """``dd.mm.yyyy`` -> ``YYYY-MM-DD``, or ``None`` when it is not a date."""
    text = "" if raw is None else str(raw).strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%d.%m.%Y").date().isoformat()
    except ValueError:
        return None


def _amount_value(raw):
    """The amount as a ``Decimal`` (always positive in the export), or ``None``."""
    text = "" if raw is None else str(raw).strip()
    if not text:
        return None
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    if not value.is_finite():
        return None
    return value


def _currency_code(raw):
    """``RUR`` is the export's spelling of ``RUB``; every other code is kept."""
    text = "" if raw is None else str(raw).strip().upper()
    if not text:
        return None
    if text == "RUR":
        return "RUB"
    return text


def _account_last4(raw):
    """The last four digits of the account number — never the whole number."""
    digits = re.sub(r"\D", "", "" if raw is None else str(raw))
    return digits[-LAST4:] if digits else ""


def _read_rows(path):
    """Read the CSV as ``[(line_number, row)]``, skipping the blank lines.

    The file is UTF-8 **with a BOM** in the observed export, so it is read
    with ``utf-8-sig``.  ``reader.line_num`` is the physical line of the row,
    which is what the warnings point at.
    """
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            rows = []
            for row in reader:
                if any(str(cell).strip() for cell in row):
                    rows.append((reader.line_num, row))
            return rows
    except OSError as exc:
        raise StatementError("cannot read the statement %s: %s" % (path, exc))


def _split_header(rows):
    """Return ``(header, data_rows)`` or raise when the export is empty."""
    if not rows:
        raise EmptyStatementError(EMPTY_EXPORT_MESSAGE)

    first = rows[0][1]
    cells = [str(cell).strip() for cell in first]
    # An empty export is a BOM and the single line ``operationList`` (plus
    # blank lines).  It is not a header and it is not a success.
    if len(cells) == 1 and cells[0].lower() == "operationlist":
        raise EmptyStatementError(EMPTY_EXPORT_MESSAGE)

    data = rows[1:]
    if not data:
        raise EmptyStatementError(EMPTY_EXPORT_MESSAGE)
    return cells, data


def _row_value(row, header):
    """Map one CSV row to ``{column: text}``; missing trailing cells are empty."""
    return {
        name: (str(row[index]) if index < len(row) else "")
        for index, name in enumerate(header)
    }


def read_alfa_statement(path):
    """Parse the Alfa-Bank CSV export.

    Returns ``{"transactions", "warnings", "accounts"}``.  A row with a
    malformed date or amount is skipped with a warning that names only its
    line number and the column; an unknown operation type keeps the amount
    positive and warns once per distinct type.
    """
    header, data = _split_header(_read_rows(path))

    missing = [name for name in ALFA_REQUIRED_COLUMNS if name not in header]
    if missing:
        raise StatementError(
            "the statement is missing required columns: %s" % ", ".join(missing)
        )

    transactions = []
    warnings = []
    accounts = set()
    unknown_types = []
    period_start = None
    period_end = None

    for line, row in data:
        record = _row_value(row, header)

        operation_raw = record.get("operationDate", "")
        operation_iso = _iso_date(operation_raw)
        if operation_iso is None:
            warnings.append(
                "line %d: cannot parse operationDate, row skipped" % line
            )
            continue

        transaction_raw = record.get("transactionDate", "")
        transaction_iso = _iso_date(transaction_raw)
        if transaction_iso is None and str(transaction_raw).strip():
            warnings.append(
                "line %d: cannot parse transactionDate, row skipped" % line
            )
            continue

        amount_raw = record.get("amount", "")
        amount = _amount_value(amount_raw)
        if amount is None:
            warnings.append("line %d: cannot parse amount, row skipped" % line)
            continue

        type_raw = record.get("type", "")
        type_text = str(type_raw).strip()
        if type_text == OUTGOING:
            signed = -abs(amount)
        elif type_text == INCOMING:
            signed = abs(amount)
        else:
            # The export is always positive; an unknown type must not flip it.
            signed = abs(amount)
            if type_text not in unknown_types:
                unknown_types.append(type_text)
                warnings.append(
                    "line %d: unknown operation type %r, amount kept positive"
                    % (line, type_text)
                )

        merchant_raw = record.get("merchant", "")
        comment_raw = record.get("comment", "")
        if str(merchant_raw).strip():
            title_raw = merchant_raw
        elif str(comment_raw).strip():
            title_raw = comment_raw
        else:
            title_raw = type_raw

        account_raw = record.get("accountName", "")
        account_value = _account_leaf(account_raw)["value"]
        if account_value:
            accounts.add(account_value)

        last4 = _account_last4(record.get("accountNumber", ""))

        currency = _currency_code(record.get("currency", ""))

        transactions.append(
            {
                "date": _date_leaf(operation_raw, operation_iso),
                "transaction_date": _date_leaf(transaction_raw, transaction_iso),
                "title": _text_leaf(title_raw),
                "category": _text_leaf(record.get("category", "")),
                "type": _text_leaf(type_raw),
                "status": _text_leaf(record.get("status", "")),
                "comment": _text_leaf(comment_raw),
                "mcc": _text_leaf(record.get("mcc", "")),
                "card": _text_leaf(record.get("cardName", "")),
                "account": _account_leaf(account_raw),
                # Only the last four digits: the full number is never stored.
                "account_last4": _text_leaf(last4),
                "amount": {
                    "raw": "" if amount_raw is None else str(amount_raw),
                    "value": format(signed, "f"),
                    "currency": currency,
                    "money": True,
                },
            }
        )

        if period_start is None or operation_iso < period_start:
            period_start = operation_iso
        if period_end is None or operation_iso > period_end:
            period_end = operation_iso

    if not transactions:
        raise EmptyStatementError(EMPTY_EXPORT_MESSAGE)

    return {
        "transactions": transactions,
        "warnings": warnings,
        "accounts": sorted(accounts),
        "period": [period_start, period_end],
    }


#: The statement parsers by bank name.
STATEMENT_PARSERS = {"alfa": read_alfa_statement}


def read_statement(bank, path, now=None):
    """Read *path* as a statement of *bank* and shape it like ``collect()``.

    The returned dict carries ``bank``, ``status`` (``"statement"``),
    ``source_url`` / ``source_urls`` (``file:<basename>``), ``fetched_at``,
    empty ``balances``, the parsed ``transactions``, ``warnings`` and the
    ``statement`` block (file, row count, period, accounts).
    """
    parser = STATEMENT_PARSERS.get(bank)
    if parser is None:
        raise StatementError(
            "unknown bank %r for a statement import (known: %s)"
            % (bank, ", ".join(sorted(STATEMENT_PARSERS)))
        )

    if now is None:
        now = datetime.now(timezone.utc)
    fetched_at = now.isoformat().replace("+00:00", "Z")

    parsed = parser(path)
    basename = os.path.basename(path)
    source = "file:%s" % basename

    return {
        "bank": bank,
        "status": "statement",
        "source_url": source,
        "source_urls": [source],
        "fetched_at": fetched_at,
        "balances": [],
        "transactions": parsed["transactions"],
        "warnings": parsed["warnings"],
        "statement": {
            "file": basename,
            "rows": len(parsed["transactions"]),
            "period": parsed["period"],
            "accounts": parsed["accounts"],
        },
    }
