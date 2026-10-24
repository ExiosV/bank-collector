"""Generic, read-only collection driver.

The driver only ever *reads* an already rendered DOM. It never clicks,
never types, never submits a form and never touches a downloading button.

The driver talks to a tiny protocol so that it can be tested without a
browser (see ``tests/fake_page.py``) while still driving real Playwright
objects:

``page`` must provide
    ``goto(url)``                  -- open the cabinet page
    ``wait_for_selector(sel, timeout=...)`` -- wait until the SPA rendered
    ``query_all(selector)``        -- list of elements matching a CSS selector
    ``content()``                  -- optional, the rendered HTML (snapshots)
    ``url``                        -- the URL that is currently open

An *element* must provide
    ``query_all(selector)``        -- descendants matching a CSS selector
    ``text_content()``             -- the visible text of the element
    ``get_attribute(name)``        -- an attribute value or ``None``

Playwright's ``Page`` / ``ElementHandle`` satisfy this protocol as-is.

A *record* (one entry of ``balances`` or ``transactions``) may carry its own
``url`` and ``wait_for``: some banks show the balances on the main page and
the statement on a second one. When the URL of a record differs from the
page that is currently open, the driver opens it (a plain ``goto``), waits
for the record's ``wait_for`` selector and reads the record from there.

A record may also carry ``items``: a group of rows (the operations of one
day) that holds the fields of the group (the date) and an inner set of rows
(the operations themselves). The fields of the group and of the item are
merged into one output row.
"""

import re

__all__ = [
    "collect",
    "extract_section",
    "extract_record",
    "parse_money",
    "normalise_value",
    "DEFAULT_TIMEOUT_MS",
]

DEFAULT_TIMEOUT_MS = 20000

# Money-like text: an optional sign, a number with (optional) thousands
# separators and (optional) a two-digit fraction, then an optional currency
# token. "20.10.2026" must NOT match (it is a date, not an amount).
_MONEY_RE = re.compile(
    r"""^\s*
        (?P<sign>[+\-−–—])?\s*
        (?P<num>
            \d{1,3}(?:[\s'.,]\d{3})+(?:[.,]\d{1,2})?               # 1 234,56 / 1.234,56
          | \d+(?:[.,]\d{1,2})?                                    # 1234.56 / 1234
        )
        \s*
        (?P<cur>₽|руб\.?|рублей|RUB|₸|KZT|BYN|\$|USD|€|EUR|£|GBP|¥|JPY)?
        \s*$""",
    re.VERBOSE | re.IGNORECASE,
)

# Every Unicode space can be a thousands separator in a cabinet: a plain
# space, NBSP (U+00A0), narrow NBSP (U+202F), THIN SPACE (U+2009) and MEDIUM
# MATHEMATICAL SPACE (U+205F) all occur in the wild, sometimes in one number
# (Alfa-Bank writes "+84\u205f000,00\u2009₽"). ``\s`` on a Python 3 ``str``
# covers all of them, so the separators are never listed by hand.

_SEPARATORS = ".,"

# Signs that mean "minus". The Unicode minus sign (U+2212), the en dash and
# the em dash are what banks actually put in front of an amount.
_MINUS_SIGNS = ("-", "−", "–", "—")
_PLUS_SIGNS = ("+",)


def _strip_thousands(text):
    """Drop the thousands separators: any whitespace and the apostrophe."""
    return re.sub(r"[\s']", "", text)


def _decimal_from_number(text):
    """Turn a number-ish string into a ``Decimal`` without losing the sign."""
    from decimal import Decimal, InvalidOperation

    sign = ""
    if text[:1] in _MINUS_SIGNS:
        sign = "-"
        text = text[1:]
    elif text[:1] in _PLUS_SIGNS:
        text = text[1:]

    text = _strip_thousands(text)
    text = text.replace("'", "")

    if "." in text or "," in text:
        last = max(text.rfind("."), text.rfind(","))
        tail = text[last + 1 :]
        if tail.isdigit() and len(tail) in (1, 2):
            head = text[:last]
            fraction = tail
        else:
            head = text
            fraction = ""
        head = re.sub(r"[.,]", "", head)
    else:
        head, fraction = text, ""

    if not head.isdigit():
        return None
    try:
        value = Decimal(head + ("." + fraction if fraction else ""))
    except InvalidOperation:  # pragma: no cover - defensive
        return None
    return -value if sign else value


def parse_money(text):
    """Parse a money-ish string.

    Returns ``(value, currency)`` where *value* is a ``Decimal`` or ``None``
    and *currency* is an ISO-ish code taken **from the string itself**.
    The currency is never guessed: if the text does not carry one, the
    currency is ``None``.

    The sign is part of the value: ``"−1 234,56 ₽"`` is ``Decimal("-1234.56")``
    (U+2212, the en dash and the em dash all mean "minus").
    """
    if text is None:
        return None, None
    text = str(text)
    match = _MONEY_RE.match(text)
    if not match:
        return None, None
    sign = match.group("sign") or ""
    value = _decimal_from_number(sign + match.group("num"))
    if value is None:
        return None, None
    currency = match.group("cur")
    if currency:
        upper = currency.upper()
        currency = {
            "₽": "RUB",
            "РУБ.": "RUB",
            "РУБ": "RUB",
            "РУБЛЕЙ": "RUB",
            "RUB": "RUB",
            "₸": "KZT",
            "KZT": "KZT",
            "BYN": "BYN",
            "$": "USD",
            "USD": "USD",
            "€": "EUR",
            "EUR": "EUR",
            "£": "GBP",
            "GBP": "GBP",
            "¥": "JPY",
            "JPY": "JPY",
        }.get(upper, upper)
    return value, currency


def normalise_value(text):
    """Normalise one extracted string.

    Returns a dict ``{"raw", "value", "currency", "money"}``:

    * ``raw``     -- the string exactly as it was read from the page;
    * ``value``   -- money strings become a plain decimal string
                     (``"1 234,56 ₽"`` -> ``"1234.56"``), everything else
                     keeps its text;
    * ``currency``-- taken from the string, never guessed;
    * ``money``   -- whether the value was recognised as money.
    """
    raw = "" if text is None else str(text)
    stripped = raw.strip()
    if not stripped:
        return {"raw": raw, "value": "", "currency": None, "money": False}

    value, currency = parse_money(stripped)
    if value is None:
        return {"raw": raw, "value": stripped, "currency": None, "money": False}
    if currency is None:
        currency = _currency_in(raw)
    return {
        "raw": raw,
        "value": format(value, "f"),
        "currency": currency,
        "money": True,
    }


_CURRENCY_TOKENS = (
    ("₽", "RUB"),
    ("руб.", "RUB"),
    ("руб", "RUB"),
    ("RUB", "RUB"),
    ("₸", "KZT"),
    ("KZT", "KZT"),
    ("BYN", "BYN"),
    ("$", "USD"),
    ("USD", "USD"),
    ("€", "EUR"),
    ("EUR", "EUR"),
    ("£", "GBP"),
    ("GBP", "GBP"),
    ("¥", "JPY"),
    ("JPY", "JPY"),
)


def _currency_in(text):
    """Find a currency that is literally present in *text* (never guessed)."""
    if not text:
        return None
    upper = str(text).upper()
    for token, code in _CURRENCY_TOKENS:
        if token.upper() in upper:
            return code
    return None


def _apply_regex(text, spec, where, warnings):
    pattern = spec.get("regex")
    if not pattern:
        return text
    match = re.search(pattern, text)
    if not match:
        warnings.append("%s: regex %r did not match %r" % (where, pattern, text))
        return None
    group = spec.get("group")
    if group is None:
        group = 0
    try:
        return match.group(group)
    except (IndexError, re.error):  # pragma: no cover - defensive
        warnings.append("%s: regex group %r missing in %r" % (where, group, text))
        return None


def extract_record(row, record_def, where_prefix, warnings):
    """Read the fields of one record out of *row*.

    *where_prefix* is the human readable position of the row, e.g.
    ``"transactions[0]"``; the warnings are prefixed with it, so a broken
    selector reads ``transactions[0].amount: selector '...' matched nothing``.
    """
    fields = {}
    for name, spec in record_def["fields"].items():
        where = "%s.%s" % (where_prefix, name)
        try:
            matches = row.query_all(spec["selector"])
        except Exception as exc:  # pragma: no cover - defensive
            warnings.append("%s: selector %r failed (%s)" % (where, spec["selector"], exc))
            matches = []
        if not matches:
            warnings.append("%s: selector %r matched nothing" % (where, spec["selector"]))
            fields[name] = {"raw": None, "value": None, "currency": None, "money": False}
            continue

        element = matches[0]
        attr = spec.get("attr")
        if attr:
            text = element.get_attribute(attr)
            if text is None:
                warnings.append("%s: attribute %r missing" % (where, attr))
                fields[name] = {"raw": None, "value": None, "currency": None, "money": False}
                continue
        else:
            text = element.text_content()

        matched = _apply_regex(text, spec, where, warnings)
        if matched is None:
            fields[name] = {"raw": text, "value": None, "currency": None, "money": False}
            continue

        normalised = normalise_value(matched)
        normalised["raw"] = text
        fields[name] = normalised
    return fields


def _same_url(left, right):
    """Compare two URLs ignoring a trailing slash."""
    if not left or not right:
        return left == right
    return left.rstrip("/") == right.rstrip("/")


def visit_record(page, record_def, warnings, visited=None, timeout_ms=DEFAULT_TIMEOUT_MS):
    """Open the page of a record, if the record asks for a page of its own.

    A record with a ``url`` that differs from the page that is currently open
    is read from that page: the driver opens it with a plain ``goto`` and
    waits for the record's ``wait_for`` selector. Visited URLs are appended
    to *visited* (in order, without duplicates).
    """
    url = record_def.get("url")
    navigated = False

    if url:
        current = getattr(page, "url", None)
        if not _same_url(current, url):
            try:
                page.goto(url)
            except Exception as exc:
                raise RuntimeError("cannot open %s: %s" % (url, exc))
            navigated = True
            if visited is not None and not any(_same_url(item, url) for item in visited):
                visited.append(url)

    wait_for = record_def.get("wait_for")
    if wait_for:
        try:
            page.wait_for_selector(wait_for, timeout=timeout_ms)
        except Exception as exc:
            warnings.append("waited for %r but it did not appear (%s)" % (wait_for, exc))
    return navigated


def extract_section(page, record_def, section_name, warnings, visited=None, timeout_ms=DEFAULT_TIMEOUT_MS):
    """Extract one section (``balances`` or ``transactions``) from *page*.

    A record may hold ``items``: rows *inside* every row of the group. The
    fields of the group and of the item are merged into one output row — the
    item wins when both define the same field.
    """
    visit_record(page, record_def, warnings, visited=visited, timeout_ms=timeout_ms)

    rows = page.query_all(record_def["row"]) if record_def.get("row") else [page]
    items_def = record_def.get("items")
    results = []

    for row_index, row in enumerate(rows):
        group_prefix = "%s[%d]" % (section_name, row_index)
        group = extract_record(row, record_def, group_prefix, warnings)

        if not items_def:
            results.append(group)
            continue

        item_rows = row.query_all(items_def["row"]) if items_def.get("row") else [row]
        if not item_rows:
            warnings.append("%s: the group has no item rows" % group_prefix)
            continue

        for item_index, item_row in enumerate(item_rows):
            item = extract_record(
                item_row,
                items_def,
                "%s.items[%d]" % (group_prefix, item_index),
                warnings,
            )
            merged = dict(group)
            for name, value in item.items():
                if name in group:
                    warnings.append(
                        "%s: field %r is defined both in the group and in the item"
                        % (group_prefix, name)
                    )
                merged[name] = value
            results.append(merged)

    return results


def collect(page, profile, timeout_ms=DEFAULT_TIMEOUT_MS, now=None):
    """Collect balances and transactions for *profile* from an open *page*.

    Returns a dict with the keys ``bank``, ``status``, ``source_url``,
    ``source_urls``, ``fetched_at``, ``balances``, ``transactions`` and
    ``warnings``. ``source_urls`` lists every page that was opened, in order.
    """
    warnings = []
    url = profile["url"]
    visited = []

    try:
        page.goto(url)
    except Exception as exc:
        raise RuntimeError("cannot open %s: %s" % (url, exc))
    visited.append(url)

    wait_for = profile.get("wait_for")
    if wait_for:
        try:
            page.wait_for_selector(wait_for, timeout=timeout_ms)
        except Exception as exc:
            warnings.append("waited for %r but it did not appear (%s)" % (wait_for, exc))

    balances = []
    for record_def in profile.get("balances") or []:
        balances.extend(
            extract_section(page, record_def, "balances", warnings, visited=visited, timeout_ms=timeout_ms)
        )

    transactions = []
    for record_def in profile.get("transactions") or []:
        transactions.extend(
            extract_section(
                page, record_def, "transactions", warnings, visited=visited, timeout_ms=timeout_ms
            )
        )

    if not balances:
        warnings.append("no balances extracted")
    if not transactions:
        warnings.append("no transactions extracted")

    if now is None:
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
    fetched_at = now.isoformat().replace("+00:00", "Z")

    return {
        "bank": profile.get("bank"),
        "status": profile.get("status"),
        "source_url": visited[0] if visited else url,
        "source_urls": list(visited),
        "fetched_at": fetched_at,
        "balances": balances,
        "transactions": transactions,
        "warnings": warnings,
    }
