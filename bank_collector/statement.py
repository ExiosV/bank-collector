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
        "own_transfers": 2,
        "own_transfer_pairs": 1
    }

Every transaction also carries three leaf-free fields of its own:
``own_transfer`` (a boolean), ``own_transfer_reason`` (``"category"``,
``"title"`` or ``""``) and ``own_transfer_pair`` (``"p1"``, ``"p2"`` … or
``""``).  A row is a transfer between the owner's own accounts when the bank
itself says so: the category is ``Между своими счетами``, or the title starts
with ``Перевод себе`` (an own account in another bank).  Such rows are kept
(never dropped) but should be excluded from income/expense totals.  The
statement block's ``own_transfers`` counts the marked rows and
``own_transfer_pairs`` the 1:1 pairs found inside the statement.

A pair is two marked rows of the same statement with the same currency, the
same operation date, the same absolute amount, opposite types and different
account numbers.  Pairing only ever groups rows the bank already marked: a
rent payment of -50000 on one account and a salary of +50000 on another are
not a self-transfer and are not flagged.

Privacy rules of the import:

* the full account number and the full card number are **never** stored —
  only the last four digits of the account number (``account_last4``) and
  the card *name* (``card``);
* every run of 12 or more digits (spaces, NBSP, dashes, dots, slashes and
  asterisks allowed inside, at most two separators in a row) and every
  already-masked card number such as ``220015******1234`` is masked in every
  stored text leaf and in the stored file name as ``***`` plus the last four
  digits;
* every warning and error message (including the path of an unreadable
  file) goes through the same mask;
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
    "NOT_A_STATEMENT_MESSAGE",
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

#: What a file whose first line is not the bank's header gets.  A saved HTML
#: error page (``<html><body>Сессия истекла</body></html>``) is the usual
#: case: it is not an empty statement, it is not a statement at all.
NOT_A_STATEMENT_MESSAGE = (
    "not a bank statement export — the file does not start with the bank's "
    "CSV header; export the statement again as CSV from the cabinet"
)


class StatementError(Exception):
    """The statement cannot be read: unknown bank, bad file, missing columns.

    ``warnings`` carries the per-row warnings of a parse that read rows but
    could not turn any of them into an operation, so the CLI can print the
    first few of them next to the error.

    The message and the warnings are masked here, once, so no error of this
    module can leak a long digit run — the path of an unreadable file and the
    bad value of a row are the usual carriers.
    """

    def __init__(self, message, warnings=None):
        Exception.__init__(self, _mask_digits(str(message)))
        self.warnings = [_mask_digits(str(text)) for text in (warnings or [])]


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

#: The opposite operation type — what the other side of a self-transfer is.
OPPOSITE_TYPES = {OUTGOING: INCOMING, INCOMING: OUTGOING}

#: The categories the bank writes on a transfer between the owner's own
#: accounts.  Compared whole, case-insensitively, with NBSP collapsed.
OWN_TRANSFER_CATEGORIES = ("Между своими счетами",)

#: The beginnings of a title the bank writes on a transfer to an own account
#: in another bank (the real titles look like "Перевод себе в другой банк
#: через СБП"; the category of those rows is the generic "Переводы").
OWN_TRANSFER_TITLE_PREFIXES = ("Перевод себе",)

#: The values of ``own_transfer_reason``: why the row was flagged.
OWN_TRANSFER_BY_CATEGORY = "category"
OWN_TRANSFER_BY_TITLE = "title"

#: The statuses the import keeps.  A real export writes an empty status on
#: every incoming row and ``Выполнен`` on completed outgoing ones; anything
#: else (``Отклонён``, ``В обработке``) is not settled money and is skipped.
ACCEPTED_STATUSES = ("", "Выполнен")

#: How many digits of the account number are kept (the rest is never stored).
LAST4 = 4

#: How many per-line warnings the CLI prints next to "none of the N rows".
WARNING_SAMPLE = 5

#: One "number-ish" run: digits with at most two separators between them.
#: Spaces, NBSP, dashes, dots, slashes and asterisks may sit inside a run, so
#: ``40817.810.1.00001234567`` and ``4081–7810–0000–0000–0099`` are one run.
#: The limit of two separators in a row keeps ordinary prose (and a single
#: dash or dot in it) from being swallowed.
_NUMBER_RUN_RE = re.compile(r"\d(?:[ \u00a0\-–—./*]{0,2}\d)+")

#: A card number the bank's own export already masks: six digits, four or
#: more asterisks, the last four digits (``220015******1234``).  It has only
#: ten digits, so the long-run rule alone would not catch it.
_MASKED_CARD_RE = re.compile(r"(?<!\d)\d{6}\*{4,}\d{4}(?!\d)")

#: The shortest plain digit run that is treated as an account/card number.
_LONG_NUMBER_DIGITS = 12


def _mask_number_run(match):
    """Mask one candidate run: ``***`` plus its last four digits.

    A run is masked when it holds at least 12 digits; the separators inside
    (spaces, NBSP, dashes, dots, slashes, asterisks) count as part of the run.
    """
    run = match.group(0)
    digits = re.sub(r"\D", "", run)
    if len(digits) >= _LONG_NUMBER_DIGITS:
        return "***" + digits[-LAST4:]
    return run


def _mask_masked_card(match):
    """Mask an already-masked card number as ``***`` plus its last four digits."""
    return "***" + re.sub(r"\D", "", match.group(0))[-LAST4:]


def _mask_digits(text):
    """Mask long digit runs and already-masked card numbers inside *text*."""
    text = _MASKED_CARD_RE.sub(_mask_masked_card, text)
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
    text = _mask_digits("" if raw is None else str(raw))
    return {"raw": text, "value": iso, "currency": None, "money": False}


def _label_text(value):
    """A label for whole-value comparison: NBSP folded, spaces collapsed."""
    text = "" if value is None else str(value)
    return " ".join(text.replace("\xa0", " ").split())


def _own_transfer_reason(category, title):
    """Why the bank says this row is a transfer between own accounts.

    Returns ``"category"`` when the category is one of
    :data:`OWN_TRANSFER_CATEGORIES`, ``"title"`` when the title starts with one
    of :data:`OWN_TRANSFER_TITLE_PREFIXES`, and ``""`` for every other row.
    Both are compared case-insensitively with NBSP collapsed to a space: the
    bank writes "Между своими счетами" as the category of an own-account
    transfer and "Перевод себе …" as the title of a transfer to an own account
    in another bank (whose category is the generic "Переводы").
    """
    category_text = _label_text(category).casefold()
    for name in OWN_TRANSFER_CATEGORIES:
        if category_text == name.casefold():
            return OWN_TRANSFER_BY_CATEGORY
    title_text = _label_text(title).casefold()
    for prefix in OWN_TRANSFER_TITLE_PREFIXES:
        if title_text.startswith(prefix.casefold()):
            return OWN_TRANSFER_BY_TITLE
    return ""


def _pair_own_transfers(meta):
    """Pair the marked self-transfers 1:1 inside one statement, greedy in order.

    Two rows pair when they have the same currency, the same operation date,
    the same absolute amount, opposite types (``Списание`` / ``Пополнение``)
    and different account numbers.  Both rows must already be marked by the
    bank's own signal: a rent payment of -50000 on one account and a salary of
    +50000 on another have the same date and amount but are not a transfer
    between own accounts and stay unflagged.

    Returns ``{row index: "p1"}`` for every matched row (``p2`` for the second
    pair and so on, in file order of the second row of the pair).  The account
    number is used locally only — the stored row keeps just ``account_last4``.
    """
    pairs = {}
    unmatched = {}
    number = 0
    for index, item in enumerate(meta):
        opposite = OPPOSITE_TYPES.get(item["type"])
        if not item["own_transfer_reason"] or opposite is None:
            continue
        key = (item["currency"], item["date"], item["amount"], opposite)
        candidates = unmatched.get(key, [])
        match = None
        for candidate in candidates:
            if meta[candidate]["account"] != item["account"]:
                match = candidate
                break
        if match is not None:
            candidates.remove(match)
            number += 1
            pairs[match] = "p%d" % number
            pairs[index] = "p%d" % number
            continue
        unmatched.setdefault(
            (item["currency"], item["date"], item["amount"], item["type"]), []
        ).append(index)
    return pairs


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
        # The path may carry a long number (a downloaded statement is often
        # named after the account): the message is masked by StatementError.
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


def _split_header(rows, required):
    """Return ``(header, data_rows)`` or raise when the file is unusable.

    Only two things are an empty export: the single line ``operationList``
    (what the cabinet writes when the period has no operations) and the
    bank's own header with no data rows under it.  A first line that is not
    that header — a saved HTML error page such as
    ``<html><body>Сессия истекла</body></html>``, a one-line PDF — is not an
    empty statement but not a statement at all, so the required columns are
    checked *before* an empty export is declared.
    """
    if not rows:
        # Not even the ``operationList`` line: a failed download, not an
        # empty statement.  Exit code 3, not 4.
        raise StatementError(NOT_A_STATEMENT_MESSAGE)

    cells = [str(cell).strip() for cell in rows[0][1]]
    # An empty export is a BOM and the single line ``operationList`` (plus
    # blank lines).  It is not a header and it is not a success.
    if len(cells) == 1 and cells[0].lower() == "operationlist":
        raise EmptyStatementError(EMPTY_EXPORT_MESSAGE)

    missing = [name for name in required if name not in cells]
    if missing:
        if len(rows) == 1:
            raise StatementError(NOT_A_STATEMENT_MESSAGE)
        raise StatementError(
            "the statement is missing required columns: %s" % ", ".join(missing)
        )

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

    Returns ``{"transactions", "warnings", "accounts", "period",
    "own_transfers", "own_transfer_pairs"}``.  A row with a status other than
    an empty one or ``Выполнен`` is skipped with one warning per distinct
    status; a row with a malformed operation date or amount is skipped with a
    warning that names only its line number and the column; a row with a
    malformed *transaction* date is kept with an empty transaction date and one
    warning; an unknown operation type keeps the amount positive and warns
    once per distinct type.
    """
    header, data = _split_header(_read_rows(path), ALFA_REQUIRED_COLUMNS)

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

        # Only the operation date is required.  A transaction date the bank
        # wrote in a shape we do not know keeps the row: the operation itself
        # is settled money, the posting date is just missing.
        transaction_raw = record.get("transactionDate", "")
        transaction_iso = _iso_date(transaction_raw)
        if transaction_iso is None and str(transaction_raw).strip():
            warnings.append(
                "line %d: cannot parse transactionDate, kept with an empty value"
                % line
            )

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

        # The bank itself marks a transfer between own accounts: by category
        # or by the title of a transfer to an own account in another bank.
        own_reason = _own_transfer_reason(record.get("category", ""), title_raw)

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
                "own_transfer": bool(own_reason),
                "own_transfer_reason": own_reason,
                # Set after the whole file is read (see _pair_own_transfers).
                "own_transfer_pair": "",
            }
        )
        # Kept aside for the own-transfer pairing; never written to the report.
        meta.append(
            {
                "date": operation_iso,
                "amount": abs(amount),
                "currency": currency,
                "type": type_text,
                "account": str(record.get("accountNumber", "")).strip(),
                "own_transfer_reason": own_reason,
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

    # Warnings carry the status, the type and the line number of the file;
    # none of them may carry a long digit run.
    warnings = [_mask_digits(text) for text in warnings]

    if not transactions:
        error = StatementError(
            "none of the %d rows could be read" % len(data), warnings=warnings
        )
        raise error

    pairs = _pair_own_transfers(meta)
    for index, pair in pairs.items():
        transactions[index]["own_transfer_pair"] = pair

    return {
        "transactions": transactions,
        "warnings": warnings,
        "accounts": sorted(accounts),
        "period": [period_start, period_end],
        "own_transfers": sum(
            1 for transaction in transactions if transaction["own_transfer"]
        ),
        "own_transfer_pairs": len(pairs) // 2,
    }


#: The statement parsers by bank name.
STATEMENT_PARSERS = {"alfa": read_alfa_statement}


def read_statement(bank, path, now=None):
    """Read *path* as a statement of *bank* and shape it like ``collect()``.

    The returned dict carries ``bank``, ``status`` (``"statement"``),
    ``source_url`` / ``source_urls`` (``file:<basename>``), ``fetched_at``,
    empty ``balances``, the parsed ``transactions``, ``warnings`` and the
    ``statement`` block (file, row count, period, accounts, own-transfer row
    count and pair count).  Long digit runs in the stored file name are masked
    just like those in the parsed text leaves.
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
            "own_transfer_pairs": parsed["own_transfer_pairs"],
        },
    }
