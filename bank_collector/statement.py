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
        "accounts": ["Альфа-Счёт на минимальный остаток", "МИР"],
        "own_transfers": 0
    }

Every transaction also carries a leaf-free boolean ``own_transfer``: two rows
of the same statement on the same operation date with the same absolute
amount, opposite types and different account numbers are a transfer between
the owner's own accounts.  Such rows are kept (never dropped) but should be
excluded from income/expense totals.  The statement block's ``own_transfers``
counts the marked rows (both sides of each pair).

Privacy rules of the import:

* the full account number and the full card number are **never** stored —
  only the last four digits of the account number (``account_last4``) and
  the card *name* (``card``);
* every run of 12 or more digits (spaces and dashes allowed inside; masked
  card runs such as ``220015******1234`` count too) is masked in every stored
  text leaf and in the stored file name as ``***`` plus the last four digits;
* an unreadable row is reported by its line number and the column name,
  never by its values;
* a warning about an unknown operation type names the type only.
"""

import csv
import io
import os
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

__all__ = [
    "StatementError",
    "EmptyStatementError",
    "EMPTY_EXPORT_MESSAGE",
    "BAD_CSV_MESSAGE",
    "STATEMENT_PARSERS",
    "read_statement",
    "read_alfa_statement",
]

#: The message of an empty export; the CLI maps it to ``EXIT_NOTHING``.
EMPTY_EXPORT_MESSAGE = (
    "the statement has no operations - the export is empty; "
    "choose all accounts and export again"
)

#: What a file that is not the bank's UTF-8 CSV export gets.  A cp1251 file,
#: a PDF or an XLSX renamed to ``.csv`` and a file with NUL bytes all land here.
BAD_CSV_MESSAGE = (
    "not a UTF-8 CSV export from the bank — export again as CSV, "
    "or the file is PDF/Excel"
)


class StatementError(Exception):
    """The statement cannot be read: unknown bank, bad file, missing columns.

    ``warnings`` carries the per-row warnings of a parse that read rows but
    could not turn any of them into an operation, so the CLI can print the
    first few of them next to the error.
    """

    def __init__(self, message, warnings=None):
        Exception.__init__(self, message)
        self.warnings = list(warnings or [])


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

#: The statuses the import keeps.  A real export writes an empty status on
#: every incoming row and ``Выполнен`` on completed outgoing ones; anything
#: else (``Отклонён``, ``В обработке``) is not settled money and is skipped.
ACCEPTED_STATUSES = ("", "Выполнен")

#: How many digits of the account number are kept (the rest is never stored).
LAST4 = 4

#: How many per-line warnings the CLI prints next to "none of the N rows".
WARNING_SAMPLE = 5

#: One "number-ish" run: digits with spaces, dashes or asterisks inside.
#: Dots and commas are deliberately absent, so dates and amounts never match.
_NUMBER_RUN_RE = re.compile(r"(?<![\d*])\d[\d\s*\-]*\d(?![\d*])")

#: The shortest plain digit run that is treated as an account/card number.
_LONG_NUMBER_DIGITS = 12


def _mask_number_run(match):
    """Mask one candidate run: ``***`` plus its last four digits.

    A plain run is masked when it holds at least 12 digits (spaces and dashes
    inside count as part of the run).  A masked card run such as
    ``220015******1234`` has only 10 digits but is masked as well.
    """
    run = match.group(0)
    digits = re.sub(r"\D", "", run)
    if len(digits) >= _LONG_NUMBER_DIGITS or ("*" in run and len(digits) >= 8):
        return "***" + digits[-LAST4:]
    return run


def _mask_digits(text):
    """Mask long digit runs (account/card numbers) inside *text*."""
    return _NUMBER_RUN_RE.sub(_mask_number_run, text)


def _text_leaf(raw):
    """A text field in the leaf shape of :func:`collect.normalise_value`.

    Long digit runs are masked in both ``raw`` and ``value``: a comment or a
    merchant that happens to carry an account number must not store it.
    """
    text = _mask_digits("" if raw is None else str(raw))
    return {"raw": text, "value": text.strip(), "currency": None, "money": False}


def _account_leaf(raw):
    """An account label with the NBSP the cabinet writes collapsed to a space."""
    text = _mask_digits("" if raw is None else str(raw))
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

    The file must be the bank's UTF-8 CSV export (with a BOM in the observed
    export, so it is decoded with ``utf-8-sig``).  A cp1251 file, a PDF or
    XLSX renamed to ``.csv`` and a file with NUL bytes are rejected with one
    clear message instead of leaking a ``UnicodeDecodeError`` or a
    ``csv.Error``.  ``reader.line_num`` is the physical line of the row,
    which is what the warnings point at.
    """
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        raise StatementError("cannot read the statement %s: %s" % (path, exc))

    if b"\x00" in raw:
        raise StatementError(BAD_CSV_MESSAGE)
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise StatementError(BAD_CSV_MESSAGE)

    rows = []
    try:
        reader = csv.reader(io.StringIO(text, newline=""))
        for row in reader:
            if any(str(cell).strip() for cell in row):
                rows.append((reader.line_num, row))
    except csv.Error:
        raise StatementError(BAD_CSV_MESSAGE)
    return rows


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


def _mark_own_transfers(transactions, meta):
    """Flag transfers between the owner's own accounts.

    A pair is two rows of the same statement on the same operation date with
    the same absolute amount, opposite types (``Списание`` / ``Пополнение``)
    and different account numbers.  Both rows get ``own_transfer = True``;
    nothing is dropped.  The account number is used locally only — the stored
    row keeps only ``account_last4``.
    """
    outgoing = {}
    incoming = {}
    for index, item in enumerate(meta):
        key = (item["date"], item["amount"])
        if item["type"] == OUTGOING:
            outgoing.setdefault(key, []).append(index)
        elif item["type"] == INCOMING:
            incoming.setdefault(key, []).append(index)

    for key, out_indexes in outgoing.items():
        in_indexes = incoming.get(key)
        if not in_indexes:
            continue
        for out_index in out_indexes:
            for in_index in in_indexes:
                if meta[out_index]["account"] != meta[in_index]["account"]:
                    transactions[out_index]["own_transfer"] = True
                    transactions[in_index]["own_transfer"] = True


def read_alfa_statement(path):
    """Parse the Alfa-Bank CSV export.

    Returns ``{"transactions", "warnings", "accounts", "period",
    "own_transfers"}``.  A row with a status other than an empty one or
    ``Выполнен`` is skipped with one warning per distinct status; a row with a
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
    meta = []
    warnings = []
    accounts = set()
    unknown_types = []
    skipped_statuses = {}
    period_start = None
    period_end = None

    for line, row in data:
        record = _row_value(row, header)

        status_raw = record.get("status", "")
        status_text = str(status_raw).strip()
        if status_text not in ACCEPTED_STATUSES:
            skipped_statuses[status_text] = skipped_statuses.get(status_text, 0) + 1
            continue

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
                # Set after the whole file is read (see _mark_own_transfers).
                "own_transfer": False,
            }
        )
        # Kept aside for the own-transfer pairing; never written to the report.
        meta.append(
            {
                "date": operation_iso,
                "amount": abs(amount),
                "type": type_text,
                "account": str(record.get("accountNumber", "")).strip(),
            }
        )

        if period_start is None or operation_iso < period_start:
            period_start = operation_iso
        if period_end is None or operation_iso > period_end:
            period_end = operation_iso

    for status_text, count in skipped_statuses.items():
        warnings.append(
            "%d rows with status %r skipped" % (count, status_text)
        )

    if not transactions:
        error = StatementError(
            "none of the %d rows could be read" % len(data), warnings=warnings
        )
        raise error

    _mark_own_transfers(transactions, meta)

    return {
        "transactions": transactions,
        "warnings": warnings,
        "accounts": sorted(accounts),
        "period": [period_start, period_end],
        "own_transfers": sum(
            1 for transaction in transactions if transaction["own_transfer"]
        ),
    }


#: The statement parsers by bank name.
STATEMENT_PARSERS = {"alfa": read_alfa_statement}


def read_statement(bank, path, now=None):
    """Read *path* as a statement of *bank* and shape it like ``collect()``.

    The returned dict carries ``bank``, ``status`` (``"statement"``),
    ``source_url`` / ``source_urls`` (``file:<basename>``), ``fetched_at``,
    empty ``balances``, the parsed ``transactions``, ``warnings`` and the
    ``statement`` block (file, row count, period, accounts, own transfers).
    Long digit runs in the stored file name are masked just like those in the
    parsed text leaves.
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
    basename = _mask_digits(os.path.basename(path))
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
            "own_transfers": parsed["own_transfers"],
        },
    }
