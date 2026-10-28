"""Generic, read-only collection driver.

The driver only ever *reads* an already rendered DOM. It never types, never
submits a form and never touches a downloading button. Its two kinds of write
are optional, narrowly scoped clicks on the history list itself:

* the list's own "load more" button (see ``scroll.load_more_text`` below): it
  is made only when scrolling added no new rows, the
  ``scroll.load_more_scope`` selector names exactly one list container that
  holds rows of the record, and exactly one button inside that container
  carries exactly the configured text — it only asks the list to render its
  next page;
* the history's own "next page" arrow named by ``pages.next_selector`` (see
  ``_paginate_section`` below): the list is cut into numbered pages, the
  content is replaced on every page and the URL never changes. The arrow is
  the only element clicked in that mode, and only when it resolves to exactly
  one element; a disabled arrow is the definitive end of the history.

The driver talks to a tiny protocol so that it can be tested without a
browser (see ``tests/fake_page.py``) while still driving real Playwright
objects:

``page`` must provide
    ``goto(url)``                  -- open the cabinet page
    ``wait_for_selector(sel, timeout=...)`` -- wait until the SPA rendered
    ``query_all(selector)``        -- list of elements matching a CSS selector
    ``content()``                  -- optional, the rendered HTML (snapshots)
    ``inner_text(selector)``       -- optional, the visible text of the page
                                      (used by ``signed_out`` markers and
                                      ``--check-session``)
    ``url``                        -- the URL that is currently open
    ``scroll_by(dy)``              -- optional, scroll the viewport by *dy*
                                      pixels (read-only: no click, no DOM
                                      mutation, no request of our own)

An *element* must provide
    ``query_all(selector)``        -- descendants matching a CSS selector
    ``text_content()``             -- the visible text of the element
    ``get_attribute(name)``        -- an attribute value or ``None``
    ``click()``                    -- optional, press the element (used only
                                      for the one "load more" button and the
                                      one "next page" arrow)

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

A record may carry ``header`` instead when the feed is *flat*: day headers
and operations are siblings in one list. ``row`` then matches every row of
the feed; a row that contains ``header.selector`` is a header, its
``header.fields`` become the current header values, and every other row is an
operation that inherits them. A page that offers ``snapshot_rows(spec)`` is
read in ONE pass over there: every row comes back with its raw field text in
DOM order, so the driver dates each operation by the header that precedes it
in that order. Across the scroll rounds of one section the headers seen so
far are carried along, so an operation whose header already left a
virtualised DOM is still dated by it; only a pass with no suitable header
carried (for instance the very first one) has its leading operations skipped
silently.

Finally a record may carry ``scroll``: the driver then scrolls the rendered
list (read-only) and reads the rows that the cabinet appends while the list
is scrolled, until the list stops growing, the date limit of the profile is
reached or the round limit is hit. When scrolling a round added no new rows
and the record names a ``load_more_text``, the driver may press the single
button with exactly that text inside the one container named by
``load_more_scope``: the click only asks the list to render its next page.

A record may carry ``pages`` instead of ``scroll``: the history is split into
numbered pages, the list replaces its content on every page and the URL never
changes. The driver reads the first page with the ordinary pass and then, while
the date limit is not reached, presses the single element named by
``pages.next_selector`` and reads the page that replaced the previous one. The
current page number is read from ``pages.active_selector`` before the click:
the first page must read 1 and the number must equal the number of pages read
so far, otherwise nothing is clicked and the run stops with an error. The
resolved arrow's text must contain no digit (a numbered page button is never
the arrow). A disabled arrow is the list's definitive end. After a walk that
ended with ``date`` or ``end`` the record's URL is opened again and the first
page is re-read: if it changed, an operation arrived mid-run and the pages
may have shifted, so the run is reported incomplete; if it yields no rows at
all (expired session, login page, failed load) nothing can be proven and the
run is reported incomplete too. The pages are disjoint, so their rows are
appended as they are, without cross-page dedup.
``pages`` cannot be combined with ``header``: a numbered-page list does not
carry day headers across page boundaries.

When the profile has a ``signed_out`` block, every page the driver opens — the
profile's own URL and each record's page — is checked for the cabinet's
sign-in screen: a URL-path substring and/or a visible text (case-folded,
whitespace collapsed).  A match stops the run at once, before any further
navigation, click or read: the result carries no rows and a ``signed_out``
marker, so the caller can tell "the owner is not signed in" from "the
selectors broke".  ``check_session`` uses the same markers to answer that
question without reading a single record.
"""

import re
import time
from collections import Counter
from decimal import Decimal, InvalidOperation
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

__all__ = [
    "collect",
    "check_session",
    "extract_section",
    "extract_record",
    "parse_money",
    "parse_ru_date",
    "normalise_value",
    "signed_out_marker",
    "DEFAULT_TIMEOUT_MS",
]

DEFAULT_TIMEOUT_MS = 20000

#: The cabinet's local timezone when the profile does not name one. Russian
#: cabinets render day headers in Moscow time, so "Сегодня" must be resolved
#: against the Moscow calendar day, not the UTC one.
DEFAULT_TIMEZONE = "Europe/Moscow"

#: The warning a run adds when the cabinet shows its sign-in screen instead of
#: the data.  The marker says WHAT matched, so that a broken marker is visible.
SIGNED_OUT_WARNING = "signed out: the cabinet shows its sign-in screen (%s)"

#: How often ``--check-session`` looks at the page while it waits for either
#: the profile's ``wait_for`` selector or a ``signed_out`` marker.
_SESSION_POLL_MS = 250

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


# ---------------------------------------------------------------------------
# Russian day headers
# ---------------------------------------------------------------------------

#: Russian month names as they appear in cabinet day headers.  Genitive is
#: what banks write ("27 октября"), the nominative forms are accepted too so
#: that a differently worded header does not lose its date.
_RU_MONTHS = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
    "январь": 1,
    "февраль": 2,
    "март": 3,
    "апрель": 4,
    "май": 5,
    "июнь": 6,
    "июль": 7,
    "август": 8,
    "сентябрь": 9,
    "октябрь": 10,
    "ноябрь": 11,
    "декабрь": 12,
}

#: The leading date of a header.  Everything after it — the day's total, an
#: amount, a currency — is ignored, so the pattern is not anchored at the end.
_RU_DATE_RE = re.compile(
    r"^\s*(?P<day>\d{1,2})\s+(?P<month>[а-яё]+)(?:\s+(?P<year>\d{4}))?",
    re.IGNORECASE,
)

#: "Сегодня" / "Вчера" headers.  The word boundary is Unicode-aware, so it
#: also matches when the total is glued to the word ("Сегодня−1 234 ₽").
_RU_RELATIVE_RE = re.compile(r"^\s*(?P<word>сегодня|вчера)\b", re.IGNORECASE)


def _today(now):
    """The calendar day of *now* (a ``datetime``, a ``date`` or ``None``)."""
    if now is None:
        return datetime.now(timezone.utc).date()
    if isinstance(now, datetime):
        return now.date()
    return now


def _local_now(now, profile, warnings=None):
    """The run instant on the cabinet's own calendar day.

    Day headers ("Сегодня", «27 октября» without a year) are rendered in the
    cabinet's local timezone, so they must be resolved against that
    timezone's calendar day, not UTC's: at 01:30 Moscow time on 1 January
    UTC still says 31 December. *now* is a ``datetime`` (naive is read as
    UTC) or a ``date``; a date is already a calendar day and stays as it is.

    A hand-written profile with an unknown name falls back to the default
    timezone. When the tz database itself is missing (native Windows without
    ``tzdata``) even that fails: the dates are then resolved in UTC and a
    warning says so, instead of aborting the run.
    """
    if not isinstance(now, datetime):
        return now
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    timezone_name = profile.get("timezone") or DEFAULT_TIMEZONE
    try:
        zone = ZoneInfo(timezone_name)
    except Exception:
        # The profile validator already rejects an unknown name; a profile
        # built by hand falls back to the default rather than aborting.
        try:
            zone = ZoneInfo(DEFAULT_TIMEZONE)
        except Exception:
            if warnings is not None:
                warnings.append(
                    "timezone %s unavailable (no tz database?), dates resolved in UTC"
                    % timezone_name
                )
            return now.astimezone(timezone.utc)
    return now.astimezone(zone)


def parse_ru_date(text, now=None):
    """Parse a Russian day header into an ISO ``YYYY-MM-DD`` string.

    Accepted forms (case-insensitive): ``"27 октября"``, ``"27 октября 2025"``,
    ``"Сегодня"`` and ``"Вчера"``.  Any text after the date — the day's total
    in a header such as ``"27 октября−1 234 ₽"`` — is ignored: only the
    leading date is matched.

    A missing year is inferred relative to *now*: the current year, or the
    previous year when that would put the date in the future (a header
    "29 декабря" read in October belongs to the previous December).  Returns
    ``None`` when the text does not start with a date the parser knows.
    """
    if text is None:
        return None
    text = str(text)
    today = _today(now)

    relative = _RU_RELATIVE_RE.match(text)
    if relative:
        word = relative.group("word").lower()
        if word == "сегодня":
            return today.isoformat()
        return (today - timedelta(days=1)).isoformat()

    match = _RU_DATE_RE.match(text)
    if not match:
        return None
    month = _RU_MONTHS.get(match.group("month").lower())
    if month is None:
        return None
    day = int(match.group("day"))

    year = match.group("year")
    try:
        if year is not None:
            return date(int(year), month, day).isoformat()
        candidate = date(today.year, month, day)
        if candidate > today:
            candidate = date(today.year - 1, month, day)
        return candidate.isoformat()
    except ValueError:
        # 31 февраля and friends: not a date, so the field stays empty.
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


def _empty_field():
    """A field that could not be read at all."""
    return {"raw": None, "value": None, "currency": None, "money": False}


def _process_text(text, spec, where, warnings, now=None):
    """Turn the raw text of one field into its output field.

    This is the second half of ``extract_record``: the regex/group step, the
    money normalisation and the ``parse: ru_date`` step. It is shared with the
    page-side snapshot path (``_records_from_snapshot``), so a field read from
    a snapshot has exactly the shape of a field read element by element.
    """
    matched = _apply_regex(text, spec, where, warnings)
    if matched is None:
        return {"raw": text, "value": None, "currency": None, "money": False}

    if spec.get("parse") == "ru_date":
        iso = parse_ru_date(matched, now)
        if iso is None:
            warnings.append("%s: cannot parse ru_date from %r" % (where, matched))
        return {"raw": text, "value": iso, "currency": None, "money": False}

    normalised = normalise_value(matched)
    normalised["raw"] = text
    return normalised


def extract_record(row, record_def, where_prefix, warnings, now=None):
    """Read the fields of one record out of *row*.

    *where_prefix* is the human readable position of the row, e.g.
    ``"transactions[0]"``; the warnings are prefixed with it, so a broken
    selector reads ``transactions[0].amount: selector '...' matched nothing``.

    A field may say ``optional: true``: a missing element (or a missing
    ``attr``) then yields an empty field silently instead of a warning.  A
    field may also say ``parse: ru_date``: its text is turned into an ISO
    ``YYYY-MM-DD`` date relative to *now*, and an unparseable value becomes
    ``None`` with one warning.
    """
    fields = {}
    for name, spec in record_def["fields"].items():
        where = "%s.%s" % (where_prefix, name)
        optional = bool(spec.get("optional"))
        try:
            matches = row.query_all(spec["selector"])
        except Exception as exc:  # pragma: no cover - defensive
            warnings.append("%s: selector %r failed (%s)" % (where, spec["selector"], exc))
            matches = []
        if not matches:
            if not optional:
                warnings.append("%s: selector %r matched nothing" % (where, spec["selector"]))
            fields[name] = _empty_field()
            continue

        element = matches[0]
        attr = spec.get("attr")
        # A handle obtained from ``query_all`` dies when the SPA re-renders the
        # list (the cabinet navigates after the code screen, and again while a
        # scrolled list appends rows). Reading it then raises
        # "Execution context was destroyed" — one broken element must never
        # abort the whole run, so it becomes a warning and an empty field.
        try:
            if attr:
                text = element.get_attribute(attr)
                if text is None:
                    if not optional:
                        warnings.append("%s: attribute %r missing" % (where, attr))
                    fields[name] = _empty_field()
                    continue
            else:
                text = element.text_content()
        except Exception as exc:
            warnings.append("%s: reading %r failed (%s)" % (where, spec.get("selector"), exc))
            fields[name] = _empty_field()
            continue

        fields[name] = _process_text(text, spec, where, warnings, now=now)
    return fields


def _page_fields(fields):
    """Only the parts of field specs the page itself needs.

    ``regex``, ``group``, ``parse`` and ``optional`` stay in Python: the page
    returns raw text, the driver turns it into the output field.
    """
    spec = {}
    for name, field in fields.items():
        entry = {"selector": field["selector"]}
        attr = field.get("attr")
        if attr:
            entry["attr"] = attr
        spec[name] = entry
    return spec


def _snapshot_spec(record_def, header_def):
    """The selector spec of one page-side pass over a flat feed."""
    return {
        "row": record_def.get("row"),
        "header_selector": header_def["selector"],
        "header_fields": _page_fields(header_def["fields"]),
        "fields": _page_fields(record_def["fields"]),
    }


def _snapshot_field(fields, name, spec, where, warnings, now=None):
    """One output field from the snapshot entry of that field."""
    optional = bool(spec.get("optional"))
    entry = fields.get(name) or {}
    if not entry.get("found"):
        if not optional:
            warnings.append("%s: selector %r matched nothing" % (where, spec["selector"]))
        return _empty_field()

    text = entry.get("text")
    attr = spec.get("attr")
    if attr and text is None:
        if not optional:
            warnings.append("%s: attribute %r missing" % (where, attr))
        return _empty_field()
    if text is None:
        text = ""
    return _process_text(text, spec, where, warnings, now=now)


def _record_from_snapshot(fields, record_def, where_prefix, warnings, now=None):
    """Turn the snapshot fields of one row into an output record."""
    out = {}
    for name, spec in record_def["fields"].items():
        where = "%s.%s" % (where_prefix, name)
        out[name] = _snapshot_field(fields, name, spec, where, warnings, now=now)
    return out


#: A header field with this name is the cabinet's own expense total of the day
#: ("27 октября−1 234 ₽"). It is not copied into the operation rows; after
#: scrolling, every day in the requested range whose collected expenses are
#: SMALLER than that total is reported and makes the history incomplete — a
#: day can only come out short when an operation was lost or dated by another
#: day. (Collected expenses may exceed the total: banks leave transfers to own
#: accounts out of it.)
DAY_TOTAL_FIELD = "day_total"


def _remember_day_total(carry, header):
    """Keep the latest non-empty day total of a header, by its parsed date."""
    if carry is None or not header:
        return
    field = header.get(DAY_TOTAL_FIELD)
    date = _row_date(header)
    if not date or not isinstance(field, dict) or not field.get("money"):
        return
    carry.setdefault("day_totals", {})[date] = field.get("value")


def _short_days(rows, day_totals, stop_before):
    """The days whose collected expenses are below the cabinet's own day total.

    Only days inside the requested range are judged: a day older than
    *stop_before* (or, without it, the oldest day reached) may be cut by the
    stop itself. Totals are whole rubles in the header, so a difference
    under 1 is rounding.
    """
    if not day_totals:
        return []
    spent = {}
    dates = set()
    for row in rows:
        date = _row_date(row)
        if not date:
            continue
        dates.add(date)
        try:
            amount = Decimal(_field_text(row, "amount"))
        except (InvalidOperation, ValueError):
            continue
        if amount < 0:
            spent[date] = spent.get(date, Decimal(0)) - amount
    floor = stop_before or (min(dates) if dates else None)
    short = []
    for date, total in sorted(day_totals.items()):
        if floor and (date < floor or (not stop_before and date == floor)):
            continue
        try:
            expected = Decimal(str(total))
        except (InvalidOperation, ValueError):
            continue
        # The header total is the day's expenses (negative); anything else is not
        # a figure the collected expenses can be held against.
        if expected >= 0:
            continue
        expected = -expected
        got = spent.get(date, Decimal(0))
        if got + 1 <= expected:
            short.append((date, got, expected))
    return short


def _merge_row(header, item_fields, row_prefix, warnings):
    """One output row: the current header values plus the operation's fields.

    The operation wins on a field-name clash (and the clash is reported).
    """
    merged = {name: value for name, value in header.items() if name != DAY_TOTAL_FIELD}
    for name, value in item_fields.items():
        if name in header:
            warnings.append(
                "%s: field %r is defined both in the header and in the item"
                % (row_prefix, name)
            )
        merged[name] = value
    return merged


def _content_key(row, record_def):
    """Identify an operation by its fields, without the day header.

    The key is compared between the rendered windows of two scroll rounds,
    so it must not contain the date: the date is exactly what the alignment
    is trying to recover. Header field names are left out in case a record
    defines one in both places.
    """
    header_names = set((record_def.get("header") or {}).get("fields") or ())
    # Optional fields (a description, cashback text) may appear in a later render
    # of the same row; a key that changes between two windows breaks the overlap.
    return tuple(
        _field_text(row, name)
        for name, spec in (record_def.get("fields") or {}).items()
        if name not in header_names and not (isinstance(spec, dict) and spec.get("optional"))
    )


def _count_headerless(counter, leading, carry=None):
    """Count the operation rows a round had to skip for lack of a date.

    A virtualised window keeps rendering the same undated rows round after
    round, so *carry* remembers how many instances of each content key were
    already counted: a row is counted once per rendered instance, not once
    per pass. Without *carry* (a single pass) every row is counted.
    """
    if counter is None:
        return
    seen = None
    if carry is not None:
        seen = carry.get("headerless_seen")
        if seen is None:
            seen = Counter()
            carry["headerless_seen"] = seen
    local = Counter(entry[2] for entry in leading)
    added = 0
    for key, count in local.items():
        known = seen.get(key, 0) if seen is not None else 0
        if count > known:
            added += count - known
            if seen is not None:
                seen[key] = count
    counter["headerless_rows"] = counter.get("headerless_rows", 0) + added


#: How many rows two consecutive windows must share before the top of the
#: newer one is dated from the older one. One shared row could be a
#: coincidence (the same 320 ₽ phone top-up on two days); two in a row, with
#: the headers between them, is the overlap of the scroll step.
_MIN_OVERLAP = 2


def _header_key(header):
    """The sequence key of a day header: its parsed date, else its raw text."""
    return ("H", _row_date(header) or _field_text(header, "date") or repr(sorted(header)))


def _expected_leading_header(current_rows, previous_rows):
    """The day the leading block must belong to, according to the headers alone.

    The leading block sits right above this window's first header. In the
    previous window that place is right before the same header (when it was
    already rendered there), else at the very bottom (the header is new).
    Every entry carries the header that dates it (a header entry carries
    itself), so the entry just above that place names the day.
    """
    first = next((row.get("key") for row in current_rows if (row.get("key") or ("",))[0] == "H"), None)
    position = len(previous_rows)
    if first is not None:
        for index in range(len(previous_rows) - 1, -1, -1):
            if previous_rows[index].get("key") == first:
                position = index
                break
    if position == 0:
        return None
    return previous_rows[position - 1].get("header")


def _align_leading(current_rows, previous_rows, leading_count):
    """Date the leading rows of a round by the overlap with the round before.

    Two consecutive windows of a virtualised feed overlap: the bottom of the
    previous window is the top of this one. The overlap is found as the
    longest run (at least ``_MIN_OVERLAP`` rows) that is a SUFFIX of
    *previous_rows* and a PREFIX of *current_rows* (DOM order, headers
    included, ``{"key", "header"}`` entries). The *leading_count* rows before
    this round's first header all belong to one day: the day of the previous
    row that matches ``current_rows[0]``.

    A run of recurring payments can still match by coincidence when the real
    overlap was broken, so the result must also agree with the headers: the
    day must be the one ``_expected_leading_header`` names, and newer than
    this window's first header (days decrease down the feed). Any
    disagreement, or no overlap at all, returns ``None``: the caller skips the
    rows, counts them and marks the history incomplete.
    """
    if not leading_count or not previous_rows or not current_rows:
        return None
    current = [row.get("key") for row in current_rows]
    previous = [row.get("key") for row in previous_rows]
    expected = _expected_leading_header(current_rows, previous_rows)
    expected_date = _row_date(expected) if expected else None
    first_header_date = next(
        (_row_date(row.get("header")) for row in current_rows
         if (row.get("key") or ("",))[0] == "H" and row.get("header")),
        None,
    )
    for size in range(min(len(current), len(previous)), _MIN_OVERLAP - 1, -1):
        if previous[len(previous) - size:] != current[:size]:
            continue
        header = previous_rows[len(previous) - size].get("header")
        date = _row_date(header) if header else None
        if not date or date != expected_date:
            return None
        if first_header_date and not date > first_header_date:
            return None
        return header
    return None


def _records_from_snapshot(
    snapshot, record_def, section_name, warnings, now=None, counter=None, carry=None
):
    """Build the records of a flat feed from one page-side snapshot.

    Rows are walked in DOM order, so a row is dated by the day header that
    precedes it in the same snapshot. Operations before the first header of
    this pass are aligned with the rows rendered in the previous round (see
    ``_align_leading``); when no overlap can be established they have no
    date: they are skipped silently and counted in
    ``counter["headerless_rows"]`` exactly like the element-by-element pass
    does. Every round stores its operation rows — dated or not — in *carry*
    for the next round.
    """
    header_def = record_def["header"]
    current_header = None
    results = []
    leading = []
    round_rows = []
    seen_header = False

    for position, item in enumerate(snapshot):
        if not isinstance(item, dict):
            item = {}
        index = item.get("index")
        row_index = index if isinstance(index, int) and not isinstance(index, bool) else position
        row_prefix = "%s[%d]" % (section_name, row_index)
        fields = item.get("fields") or {}

        if item.get("header"):
            current_header = _record_from_snapshot(
                fields, header_def, "%s.header" % row_prefix, warnings, now=now
            )
            round_rows.append({"key": _header_key(current_header), "header": current_header})
            _remember_day_total(carry, current_header)
            seen_header = True
            continue
        if not seen_header:
            # Read the fields now — the overlap alignment needs their content
            # key. The warnings are held back: a row that cannot be dated is
            # skipped silently, exactly like before.
            local_warnings = []
            item_fields = _record_from_snapshot(
                fields, record_def, row_prefix, local_warnings, now=now
            )
            key = _content_key(item_fields, record_def)
            leading.append((row_prefix, item_fields, key, local_warnings))
            round_rows.append({"key": ("O",) + tuple(key), "header": None})
            continue

        item_fields = _record_from_snapshot(fields, record_def, row_prefix, warnings, now=now)
        results.append(_merge_row(current_header, item_fields, row_prefix, warnings))
        round_rows.append(
            {"key": ("O",) + tuple(_content_key(item_fields, record_def)), "header": current_header}
        )

    attribution = _align_leading(round_rows, (carry or {}).get("rows") or [], len(leading))
    leading_results = []
    if attribution is not None:
        for row_prefix, item_fields, _key, local_warnings in leading:
            warnings.extend(local_warnings)
            leading_results.append(
                _merge_row(attribution, item_fields, row_prefix, warnings)
            )
        for entry in round_rows[: len(leading)]:
            entry["header"] = attribution
    else:
        _count_headerless(counter, leading, carry)
    if carry is not None:
        carry["rows"] = round_rows
    return leading_results + results


class _SignedOut(Exception):
    """The cabinet showed its sign-in screen: stop before any further read."""

    def __init__(self, marker):
        Exception.__init__(self, marker)
        self.marker = marker


def signed_out_marker(page, config):
    """Describe the sign-in marker the page matches, or return ``None``.

    *config* is the profile's ``signed_out`` block (or ``None``).  A URL
    marker matches when it is a substring of the page's URL **path**; a text
    marker matches when it occurs in the page body after case-folding and
    whitespace collapsing.  The returned string names the marker that
    matched, so the warning tells which clue fired.
    """
    if not config:
        return None

    url = ""
    try:
        url = str(getattr(page, "url", "") or "")
    except Exception:  # pragma: no cover - a dead page is not a marker
        url = ""
    if url:
        try:
            path = urlsplit(url).path or url
        except ValueError:  # pragma: no cover - a malformed URL is not a marker
            path = url
        for needle in config.get("url_contains") or []:
            if needle in path:
                return "url contains %r" % needle

    needles = config.get("text") or []
    if needles:
        try:
            body = page.inner_text("body")
        except Exception:  # pragma: no cover - an unreadable body is not a marker
            body = ""
        if body:
            folded = _collapsed(body).casefold()
            for needle in needles:
                marker = _collapsed(needle)
                if marker and marker.casefold() in folded:
                    return "text %r" % marker

    return None


def _raise_if_signed_out(page, config):
    """Raise :class:`_SignedOut` when the page is the cabinet's sign-in screen."""
    marker = signed_out_marker(page, config)
    if marker:
        raise _SignedOut(marker)


def _selector_present(page, selector):
    """True when *selector* matches at least one element right now."""
    try:
        return bool(page.query_all(selector))
    except Exception:  # pragma: no cover - a closed page answers nothing
        return False


def check_session(page, profile, timeout_ms=DEFAULT_TIMEOUT_MS):
    """Tell whether the attached browser is still signed in to the cabinet.

    Only the profile's own URL is opened: no unlock code, no record page, no
    click and no scroll.  For up to *timeout_ms* the page is polled for the
    profile's ``wait_for`` selector and for its ``signed_out`` markers.

    Returns ``"ok"`` (the cabinet rendered), ``"signed-out"`` (a sign-in
    marker matched) or ``"unknown"`` (neither appeared, or the page could not
    be opened).  A profile without a ``signed_out`` block can only ever give
    ``ok`` or ``unknown``.
    """
    try:
        page.goto(profile["url"])
    except Exception:
        return "unknown"

    config = profile.get("signed_out")
    wait_for = profile.get("wait_for")
    polls = max(1, (timeout_ms + _SESSION_POLL_MS - 1) // _SESSION_POLL_MS)
    for attempt in range(polls):
        if signed_out_marker(page, config):
            return "signed-out"
        if wait_for and _selector_present(page, wait_for):
            return "ok"
        if attempt + 1 < polls:
            time.sleep(_SESSION_POLL_MS / 1000.0)
    return "unknown"


def _same_url(left, right):
    """Compare two URLs ignoring a trailing slash."""
    if not left or not right:
        return left == right
    return left.rstrip("/") == right.rstrip("/")


def visit_record(
    page, record_def, warnings, visited=None, timeout_ms=DEFAULT_TIMEOUT_MS, signed_out=None
):
    """Open the page of a record, if the record asks for a page of its own.

    A record with a ``url`` that differs from the page that is currently open
    is read from that page: the driver opens it with a plain ``goto`` and
    waits for the record's ``wait_for`` selector. Visited URLs are appended
    to *visited* (in order, without duplicates).

    When *signed_out* is given (the profile's ``signed_out`` block), the page
    is checked for its sign-in screen right after the navigation and again
    after the wait: a run must stop there at once, without reading a record
    off the sign-in page.
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
            _raise_if_signed_out(page, signed_out)

    wait_for = record_def.get("wait_for")
    if wait_for:
        try:
            page.wait_for_selector(wait_for, timeout=timeout_ms)
        except Exception as exc:
            warnings.append("waited for %r but it did not appear (%s)" % (wait_for, exc))
    _raise_if_signed_out(page, signed_out)
    return navigated


def _extract_rows(
    page, record_def, section_name, warnings, now=None, counter=None, carry=None
):
    """Read the rows of a record as they are rendered right now.

    A record may hold ``items``: rows *inside* every row of the group. The
    fields of the group and of the item are merged into one output row — the
    item wins when both define the same field.

    A record may hold ``header`` instead: the feed is flat, so ``row`` matches
    both the day headers and the operations. A header row updates the current
    header values and is not emitted; an operation row inherits them (the
    operation wins on a clash). Operations before the first header of a pass
    are aligned with the rows rendered in the previous round (see
    ``_align_leading``); only when no overlap can be established do they have
    no date — then they are skipped silently and counted in
    ``counter["headerless_rows"]``.

    When the page offers ``snapshot_rows`` and the record has both a ``row``
    and a ``header``, the whole feed is read in one page-side pass in DOM
    order. The element-by-element pass below stays as the fallback for a page
    without that method; records without ``header`` always use it.
    """
    items_def = record_def.get("items")
    header_def = record_def.get("header")

    if header_def and record_def.get("row"):
        snapshot_rows = getattr(page, "snapshot_rows", None)
        if snapshot_rows is not None:
            snapshot = snapshot_rows(_snapshot_spec(record_def, header_def))
            return _records_from_snapshot(
                snapshot, record_def, section_name, warnings, now=now,
                counter=counter, carry=carry,
            )

    rows = page.query_all(record_def["row"]) if record_def.get("row") else [page]
    results = []

    if header_def:
        current_header = None
        seen_header = False
        leading = []
        round_rows = []
        for row_index, row in enumerate(rows):
            row_prefix = "%s[%d]" % (section_name, row_index)
            try:
                is_header = bool(row.query_all(header_def["selector"]))
            except Exception as exc:  # pragma: no cover - defensive
                warnings.append(
                    "%s: header selector %r failed (%s)"
                    % (row_prefix, header_def["selector"], exc)
                )
                is_header = False
            if is_header:
                current_header = extract_record(
                    row, header_def, "%s.header" % row_prefix, warnings, now=now
                )
                round_rows.append({"key": _header_key(current_header), "header": current_header})
                _remember_day_total(carry, current_header)
                seen_header = True
                continue
            if not seen_header:
                # Extracted here, not after the loop: the overlap alignment
                # needs the content key of every leading row. The warnings
                # are held back, so a row that cannot be dated stays silent.
                local_warnings = []
                item = extract_record(
                    row, record_def, row_prefix, local_warnings, now=now
                )
                key = _content_key(item, record_def)
                leading.append((row_prefix, item, key, local_warnings))
                round_rows.append({"key": ("O",) + tuple(key), "header": None})
                continue
            item = extract_record(row, record_def, row_prefix, warnings, now=now)
            results.append(_merge_row(current_header, item, row_prefix, warnings))
            round_rows.append(
                {"key": ("O",) + tuple(_content_key(item, record_def)), "header": current_header}
            )

        attribution = _align_leading(round_rows, (carry or {}).get("rows") or [], len(leading))
        leading_results = []
        if attribution is not None:
            for row_prefix, item, _key, local_warnings in leading:
                warnings.extend(local_warnings)
                leading_results.append(_merge_row(attribution, item, row_prefix, warnings))
            for entry in round_rows[: len(leading)]:
                entry["header"] = attribution
        else:
            _count_headerless(counter, leading, carry)
        if carry is not None:
            carry["rows"] = round_rows
        return leading_results + results

    for row_index, row in enumerate(rows):
        group_prefix = "%s[%d]" % (section_name, row_index)
        group = extract_record(row, record_def, group_prefix, warnings, now=now)

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
                now=now,
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


def _merge_counter(counter, attempt):
    """Add the counts of a successful extraction attempt to *counter*."""
    if counter is None:
        return
    for name, value in attempt.items():
        counter[name] = counter.get(name, 0) + value


def _extract_rows_stable(
    page, record_def, section_name, warnings, timeout_ms=DEFAULT_TIMEOUT_MS, now=None,
    counter=None, carry=None,
):
    """Read the rows, retrying once when the DOM was torn by a navigation.

    A single ``query_all`` + ``text_content`` pass can fail while the cabinet
    replaces the rendered list (after the code screen, or while a scrolled
    list appends rows). The pass is then repeated once after a short pause and
    a fresh wait for the record's ``wait_for`` selector. Two failures do not
    raise: they become one warning and an empty result, because a lost section
    is better reported than a lost run.
    """
    attempt_warnings = []
    attempt_counter = {}
    try:
        rows = _extract_rows(
            page, record_def, section_name, attempt_warnings, now=now,
            counter=attempt_counter, carry=carry,
        )
        warnings.extend(attempt_warnings)
        _merge_counter(counter, attempt_counter)
        return rows
    except Exception:
        pass

    if timeout_ms:
        time.sleep(0.6)
    try:
        wait_for = record_def.get("wait_for")
        if wait_for:
            page.wait_for_selector(wait_for, timeout=timeout_ms)
    except Exception:  # pragma: no cover - best effort only
        pass

    retry_warnings = []
    retry_counter = {}
    try:
        rows = _extract_rows(
            page, record_def, section_name, retry_warnings, now=now,
            counter=retry_counter, carry=carry,
        )
    except Exception as exc:
        warnings.append("%s: extraction failed after 2 attempts (%s)" % (section_name, exc))
        return []
    warnings.extend(retry_warnings)
    _merge_counter(counter, retry_counter)
    return rows


#: The fields that identify one operation: two rows with the same values are
#: the same operation, even when the page repeats it after a scroll.
_ROW_KEY_FIELDS = ("date", "title", "category", "amount")

#: How many warnings of the scrolled rounds are reported at most (they would
#: otherwise repeat themselves once per round).
_SCROLL_WARNING_LIMIT = 5

#: The date of a row (``YYYY-MM-DD`` prefix of the extracted value).
_DATE_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _field_text(row, name):
    """The text of one field of a row: the normalised value, else the raw one."""
    field = row.get(name)
    if not isinstance(field, dict):
        return ""
    for key in ("value", "raw"):
        text = field.get(key)
        if text:
            return str(text)
    return ""


def _row_key(row):
    """The key that identifies a row: its date, title, category and amount."""
    return tuple(_field_text(row, name) for name in _ROW_KEY_FIELDS)


def _row_date(row):
    """The ``YYYY-MM-DD`` date of a row, or ``None`` when it has none."""
    text = _field_text(row, "date")[:10]
    if len(text) == 10 and _DATE_PREFIX_RE.match(text):
        return text
    return None


def _oldest_date(rows):
    """The oldest ``YYYY-MM-DD`` date among *rows*, or ``None``."""
    dates = [date for date in (_row_date(row) for row in rows) if date]
    return min(dates) if dates else None


def _newest_date(rows):
    """The newest ``YYYY-MM-DD`` date among *rows*, or ``None``."""
    dates = [date for date in (_row_date(row) for row in rows) if date]
    return max(dates) if dates else None


def _reached_date(rows, stop_before):
    """True when a row is dated strictly before *stop_before* (a ``YYYY-MM-DD``).

    Strictly before, not on: the operations of one day may arrive in two
    batches, so the day of *stop_before* itself is complete only once an
    older row has been rendered.
    """
    if not stop_before:
        return False
    oldest = _oldest_date(rows)
    return oldest is not None and oldest < stop_before


def _append_scroll_warnings(warnings, scrolled_warnings):
    """Add a few warnings of the scrolled rounds to the report.

    The same broken selector would otherwise be reported once per round, so
    only the first few different messages are kept.
    """
    seen = set()
    for message in scrolled_warnings:
        if message in seen:
            continue
        seen.add(message)
        warnings.append("scrolled row: %s" % message)
        if len(seen) >= _SCROLL_WARNING_LIMIT:
            break


def _collapsed(text):
    """Strip *text* and collapse every run of whitespace into one space."""
    return " ".join(str(text or "").split())


def _same_label(element, label_folded):
    """True when the whole text of *element* equals the label case-insensitively."""
    try:
        text = element.text_content()
    except Exception:  # pragma: no cover - a re-render can kill a handle
        return False
    return _collapsed(text).casefold() == label_folded


def _load_more_buttons(container, load_more_text):
    """The buttons of *container* whose whole text is *load_more_text*.

    The text of a button is compared stripped, whitespace-collapsed and
    case-folded, so a label that is wrapped over two lines or typed in a
    different case still matches. Only ``<button>`` elements inside the
    container are considered; a link or a div that happens to carry the text
    is never pressed.
    """
    folded = _collapsed(load_more_text).casefold()
    matches = []
    for element in container.query_all("button"):
        if _same_label(element, folded):
            matches.append(element)
    return matches


def _load_more_containers(page, record_def, load_more_scope):
    """The containers matching *load_more_scope* that hold rows of the record.

    A scope that also matches a wrapper without rows (or the whole page) is
    therefore not counted: only a real list container is.
    """
    return [
        container
        for container in page.query_all(load_more_scope)
        if container.query_all(record_def["row"])
    ]


def _text_only_label_count(scope, label_folded):
    """How many text-only ``div``/``span`` elements under *scope* carry the label.

    Only an element with no element children counts: a wrapper whose text is
    the label plus its children is the container of a control, not an inert
    lookalike.  Counting without identity comparisons also lets the caller
    subtract the ones that live inside a matching control.

    A ``div``/``span`` that itself carries ``role="button"`` is skipped: it is
    already counted by the control query, and counting it here too would
    report one element as two.
    """
    count = 0
    for element in scope.query_all(
        "div:not([role=button]), span:not([role=button])"
    ):
        try:
            if element.query_all("*"):
                continue
            text = element.text_content()
        except Exception:  # pragma: no cover - a re-render can kill a handle
            continue
        if _collapsed(text).casefold() == label_folded:
            count += 1
    return count


def _foreign_load_more_count(page, record_def, load_more_scope, load_more_text):
    """How many same-label controls were not clickable under the rules.

    Counts every same-label control on the page — ``button``/``[role=button]``
    /``a`` and also a plain text-only ``div`` or ``span`` (an inert widget can
    look exactly like a button), case-insensitively — then subtracts the
    ``<button>`` elements that sit inside the one valid list container. A
    positive number means the history may be incomplete: the list offered no
    clickable button, but a same-label control existed elsewhere (an offers
    block) or as a link/div.

    Each element is counted once: a ``<div role="button">`` is a control, not
    a control plus a text-only lookalike, and a label span inside a control is
    part of that control.
    """
    folded = _collapsed(load_more_text).casefold()
    controls = [
        element
        for element in page.query_all("button, [role=button], a")
        if _same_label(element, folded)
    ]
    total = len(controls)

    # A plain div/span can carry the label without being a button.  A span
    # inside a matching control is part of that control, not a second one,
    # so those are subtracted; a div/span that is itself a control was never
    # added to the text-only count in the first place.
    text_only = _text_only_label_count(page, folded)
    for control in controls:
        text_only -= _text_only_label_count(control, folded)
    total += max(text_only, 0)
    if not total:
        return 0

    clickable = 0
    for container in _load_more_containers(page, record_def, load_more_scope):
        clickable += len(_load_more_buttons(container, load_more_text))
    return max(total - clickable, 0)


def _absorb_round(
    page, record_def, section_name, scrolled_warnings, rows, seen, timeout_ms,
    now=None, counter=None, carry=None,
):
    """Read the list once and keep the rows that are new; return how many.

    *rows* and *seen* are updated in place. A row is kept when its key has
    not been kept that many times yet (two equal coffees are two rows); a row
    with no key at all is always kept.

    A virtualised list drops rows from the DOM while it is scrolled: a round
    may therefore see fewer rows than the previous one. Counting the rows of
    *this round* against *seen* keeps that correct — a row that is still
    rendered is simply skipped, and only the newly rendered ones are kept.
    """
    added = 0
    in_round = Counter()
    for row in _extract_rows_stable(
        page,
        record_def,
        section_name,
        scrolled_warnings,
        timeout_ms=timeout_ms,
        now=now,
        counter=counter,
        carry=carry,
    ):
        key = _row_key(row)
        if any(key):
            in_round[key] += 1
            if in_round[key] <= seen[key]:
                continue
            seen[key] += 1
        # A row without a date, a title, a category and an amount cannot
        # be recognised as a duplicate — it is always kept.
        rows.append(row)
        added += 1
    return added


def _scroll_limit_warning(warnings, section_name, rounds, rows):
    """Report that a scroll or a click hit its limit."""
    warnings.append(
        "%s: scroll limit reached after %d rounds (%d rows, history may be incomplete)"
        % (section_name, rounds, len(rows))
    )


def _scroll_section(
    page, record_def, section_name, warnings, rows, scroll_def,
    timeout_ms=DEFAULT_TIMEOUT_MS, now=None, headerless=None, carry=None,
):
    """Scroll the rendered list and append the rows it newly shows.

    Scrolling itself is read-only: the viewport moves and no request of our
    own is sent. When a round added no new rows and the record names a
    ``load_more_text``, the driver first resolves ``load_more_scope`` to the
    one list container that holds rows of the record; only buttons inside
    that container whose whole text equals the configured string are
    considered. Exactly one match is pressed (the click only asks the list to
    render its next page); no match is a normal empty round; several matches
    — or zero/several containers — are never pressed and stop the run with an
    error.

    Returns ``(rows, info)``, where *info* says how many rounds were made,
    how many rows were collected in total, how many load-more clicks were
    made (always 0 when the feature is off), how many empty rounds found no
    matching button in the list (``load_more_missing``), how many operation
    rows could not be dated at all and were therefore skipped
    (``headerless_rows``) and why the scrolling stopped.

    *carry* holds the operation rows of the previous round, with their dates,
    so that the leading rows of the next round can be aligned with them (see
    ``_align_leading``). It is created here when the caller did not provide
    one and the record is a flat feed.
    """
    counter = headerless if headerless is not None else {"headerless_rows": 0}
    if carry is None and record_def.get("header"):
        carry = {"rows": []}
    scroll_by = getattr(page, "scroll_by", None)
    if scroll_by is None:
        # The page cannot scroll — nothing is done, and it is said so.
        info = {
            "rounds": 0,
            "rows": len(rows),
            "stop": "no-scroll",
            "oldest_date": _oldest_date(rows),
            "clicks": 0,
            "load_more_missing": 0,
            "headerless_rows": counter["headerless_rows"],
        }
        _judge_completeness(warnings, section_name, info, scroll_def.get("stop_before_date"))
        return rows, info

    max_rounds = scroll_def["max_rounds"]
    step_px = scroll_def["step_px"]
    settle_ms = scroll_def["settle_ms"]
    stop_after_empty = scroll_def["stop_after_empty_rounds"]
    stop_before = scroll_def["stop_before_date"]
    load_more_text = scroll_def.get("load_more_text")
    load_more_scope = scroll_def.get("load_more_scope")
    max_clicks = scroll_def.get("max_clicks", 100)

    # How many times each operation has been kept so far. A key may repeat:
    # two equal coffees on one day are two operations, so a round adds a row
    # only when it shows that key more often than the rows already kept.
    seen = Counter(_row_key(row) for row in rows)

    rounds = 0
    clicks = 0
    empty_rounds = 0
    load_more_missing = 0
    stop = "empty"
    scrolled_warnings = []

    while rounds < max_rounds:
        try:
            scroll_by(step_px)
        except Exception as exc:
            warnings.append("%s: scrolling stopped (%s)" % (section_name, exc))
            stop = "error"
            break
        rounds += 1
        if settle_ms > 0:
            time.sleep(settle_ms / 1000.0)

        added = _absorb_round(
            page, record_def, section_name, scrolled_warnings, rows, seen, timeout_ms,
            now=now, counter=counter, carry=carry,
        )

        if _reached_date(rows, stop_before):
            stop = "date"
            break

        if added:
            empty_rounds = 0
            continue

        # Scrolling added nothing: the list may end here or offer one button
        # that renders its next page. The button is the only thing ever
        # clicked by the driver, and it is searched only inside the one list
        # container named by ``load_more_scope``.
        if load_more_text:
            containers = _load_more_containers(page, record_def, load_more_scope)
            if len(containers) != 1:
                warnings.append(
                    "%s: load-more scope %r matched %d list containers, none clicked"
                    % (section_name, load_more_scope, len(containers))
                )
                stop = "error"
                break
            buttons = _load_more_buttons(containers[0], load_more_text)
            if len(buttons) > 1:
                warnings.append(
                    "%s: %d buttons named %r, none clicked"
                    % (section_name, len(buttons), load_more_text)
                )
                stop = "error"
                break
            if buttons:
                if clicks >= max_clicks:
                    _scroll_limit_warning(warnings, section_name, rounds, rows)
                    stop = "limit"
                    break
                try:
                    buttons[0].click()
                except Exception as exc:
                    warnings.append(
                        "%s: load-more click failed (%s)" % (section_name, exc)
                    )
                    stop = "error"
                    break
                clicks += 1
                # A click is a round of its own: it walks towards max_rounds
                # exactly like a scroll step does.
                rounds += 1
                if settle_ms > 0:
                    time.sleep(settle_ms / 1000.0)
                added = _absorb_round(
                    page, record_def, section_name, scrolled_warnings, rows, seen, timeout_ms,
                    now=now, counter=counter, carry=carry,
                )
                if _reached_date(rows, stop_before):
                    stop = "date"
                    break
                if added:
                    empty_rounds = 0
                    continue
                # A click that added no rows falls through to an empty round.
            else:
                # The list showed no matching button this round. Counted so
                # the report can say how often the click was unavailable.
                load_more_missing += 1

        empty_rounds += 1
        if empty_rounds >= stop_after_empty:
            stop = "empty"
            break
    else:
        stop = "limit"
        _scroll_limit_warning(warnings, section_name, rounds, rows)

    _append_scroll_warnings(warnings, scrolled_warnings)
    info = {
        "rounds": rounds,
        "rows": len(rows),
        "stop": stop,
        "oldest_date": _oldest_date(rows),
        "clicks": clicks,
        "load_more_missing": load_more_missing,
        "headerless_rows": counter["headerless_rows"],
    }
    _judge_completeness(warnings, section_name, info, stop_before)

    # A list that ended without a clickable button but does show the same
    # label elsewhere (an offers block, a link) may be hiding its next page.
    # This only ever turns ``complete`` from True to False.
    if load_more_text and info["stop"] == "empty":
        foreign = _foreign_load_more_count(
            page, record_def, load_more_scope, load_more_text
        )
        if foreign:
            warnings.append(
                "%s: %r found outside the list or not as a button (%d), not clicked "
                "— history may be incomplete"
                % (section_name, load_more_text, foreign)
            )
            info["complete"] = False

    return rows, info


#: How often the driver looks at the page number after the "next page" click.
_PAGE_POLL_MS = 250


def _element_disabled(element):
    """True when a control is disabled: ``disabled`` or ``aria-disabled="true"``.

    A disabled "next page" arrow is the list's own way of saying that the
    history ends here — it is never pressed.
    """
    try:
        if element.get_attribute("disabled") is not None:
            return True
        aria = element.get_attribute("aria-disabled")
    except Exception:  # pragma: no cover - a re-render can kill a handle
        return False
    return _collapsed(aria or "").casefold() == "true"


def _read_active_page_number(page, selector):
    """Read the current page number and, when it fails, say why.

    Returns ``(number, None)`` when exactly one element matches *selector* and
    its whole text is an integer, and ``(None, reason)`` otherwise.  One read
    answers both, so the reason describes the DOM that actually failed instead
    of a second query that may see a different one.
    """
    try:
        matches = page.query_all(selector)
    except Exception as exc:
        return None, "active-page selector %r failed (%s)" % (selector, exc)
    if len(matches) != 1:
        return None, (
            "active-page selector %r matched %d elements, not one page number"
            % (selector, len(matches))
        )
    try:
        text = _collapsed(matches[0].text_content())
    except Exception as exc:  # pragma: no cover - a re-render can kill a handle
        return None, "reading %r failed (%s)" % (selector, exc)
    if not text.isdigit():
        return None, "active-page selector %r holds %r, not a page number" % (selector, text)
    return int(text), None


def _active_page_number(page, selector):
    """The current page number, or ``None`` when it cannot be read.

    Exactly one element must match *selector* and its whole text must be an
    integer. More or fewer matches, or any other text, give ``None``: the
    driver then never clicks, because it cannot tell which page it is on.
    """
    return _read_active_page_number(page, selector)[0]


def _pages_limit_warning(warnings, section_name, pages, rows):
    """Report that the pagination hit its page limit."""
    warnings.append(
        "%s: page limit reached after %d pages (%d rows, history may be incomplete)"
        % (section_name, pages, len(rows))
    )


def _row_key_counts(rows):
    """The multiset of operation keys of one page."""
    return Counter(_row_key(row) for row in rows)


def _rows_signature(page, row_selector, items_selector=None):
    """The texts of the rendered rows, as one tuple (empty when none or unreadable).

    A cheap fingerprint of the list: it tells the old page from the new one
    while the cabinet is still swapping the content in.

    When *items_selector* is given, the signature is built from the texts of
    the item rows inside every group row, not from the group rows themselves.
    An operation list detects a page change by its operations: a record with
    ``items`` must not let a shared day header (the group row) mask the fact
    that the operations below it were replaced.
    """
    try:
        if not items_selector:
            return tuple(
                _collapsed(row.text_content()) for row in page.query_all(row_selector)
            )
        texts = []
        for group in page.query_all(row_selector):
            texts.extend(
                _collapsed(item.text_content())
                for item in group.query_all(items_selector)
            )
        return tuple(texts)
    except Exception:  # pragma: no cover - a re-render can kill a handle
        return ()


def _paginate_section(
    page, record_def, section_name, warnings, rows, pages_def,
    timeout_ms=DEFAULT_TIMEOUT_MS, now=None,
):
    """Walk a history split into numbered pages and append their rows.

    The first page is already read by the caller; this function presses the
    list's own "next page" arrow (``pages.next_selector``) and reads every
    page that replaces the previous one. The arrow is the only element ever
    clicked in this mode. It must resolve to exactly one element; an arrow
    carrying ``disabled`` (or ``aria-disabled="true"``) ends the history. The
    current page number is read from ``pages.active_selector`` before the
    click and must equal the number of pages read so far; the first page must
    read 1. An unreadable number is retried for up to ``pages.wait_ms`` (the
    pagination may render a beat after the rows) and is then an error, never
    an assumption that the list opened where it should. The page number must
    move to the next integer afterwards, together with at least one rendered
    row.

    The pages are disjoint, so all their rows are kept without cross-page
    dedup: two equal coffees on two pages are two operations. A page whose
    rows are the same multiset as the page before did not switch and is an
    error. Every page followed by another one must show exactly
    ``pages.page_size`` operations (the last page may be shorter), and the
    newest date of a page must not be newer than the oldest date of the page
    before — otherwise the run is reported incomplete.

    When the record has ``items``, the signature uses the item row selector
    (rows inside the record row) to detect page changes. A page that after
    the switch yields zero rows from ``_extract_rows_stable`` is an error.

    Before clicking, the resolved next element's collapsed text must contain
    no digit (a numbered page button is never the arrow).

    After the walk ends with stop "date" or "end", the record's URL is
    revisited and the first page is re-read to detect history shifts
    (an operation arriving mid-run shifts every page by one). The multiset of
    row keys is compared with the first page read at the start. A recheck that
    cannot even open the page again, or that yields no rows at all (expired
    session, a login page, a failed load), is reported as ``"failed"`` and
    makes the run incomplete, because nothing can be proven about the start of
    the history. ``"skipped"`` means only that no recheck was attempted: the
    walk stopped with ``error``/``limit`` or the record has no ``url``.

    Returns ``(rows, info)`` where *info* mirrors the scroll block: ``mode``
    (``"pages"``), ``pages`` (how many pages were read), ``clicks``, ``rows``,
    ``stop``, ``oldest_date``, ``headerless_rows`` (always 0 here), ``complete``
    and ``recheck`` (``"same"``, ``"changed"``, ``"failed"`` or ``"skipped"``).
    """
    next_selector = pages_def["next_selector"]
    active_selector = pages_def["active_selector"]
    page_size = pages_def.get("page_size")
    settle_ms = pages_def["settle_ms"]
    wait_ms = pages_def["wait_ms"]
    max_pages = pages_def["max_pages"]
    stop_before = pages_def.get("stop_before_date")

    pages_read = 1
    clicks = 0
    short_page = False
    stop = "error"
    previous_rows = list(rows)
    polls = max(1, (wait_ms + _PAGE_POLL_MS - 1) // _PAGE_POLL_MS)

    # Capture the first page keys for the recheck at the end.
    # The `rows` parameter contains only the first page's operations at this point.
    first_page_keys_at_start = Counter(_row_key(row) for row in rows)

    # Determine the row selector for the signature: when the record has items,
    # the signature must look at the item rows, not the group rows.
    items_def = record_def.get("items")
    signature_row_selector = record_def["row"]
    signature_items_selector = items_def["row"] if items_def else None

    # Check that the first page reads as page 1. An unreadable number is not
    # "probably page 1": a walk that then stops at the date limit or on a
    # disabled arrow could otherwise look complete without any anchor at all.
    # The pagination may render a beat after the rows, so an unreadable number
    # is retried for up to ``wait_ms`` before the run gives up on it.
    first_number = None
    first_error = None
    for attempt in range(polls):
        first_number, first_error = _read_active_page_number(page, active_selector)
        if first_number is not None:
            break
        if attempt + 1 < polls:
            time.sleep(_PAGE_POLL_MS / 1000.0)
    if first_number != 1:
        if first_number is None:
            message = first_error or (
                "active-page selector %r could not be read" % active_selector
            )
        else:
            message = "list did not open on page 1 (active page is %d)" % first_number
        warnings.append("%s: %s" % (section_name, message))
        stop = "error"
        info = {
            "mode": "pages",
            "pages": pages_read,
            "clicks": clicks,
            "rows": len(rows),
            "stop": stop,
            "oldest_date": _oldest_date(rows),
            "headerless_rows": 0,
            "recheck": "skipped",
        }
        _judge_completeness(warnings, section_name, info, stop_before)
        info["complete"] = False
        return rows, info

    while True:
        if _reached_date(rows, stop_before):
            stop = "date"
            break

        try:
            arrows = page.query_all(next_selector)
        except Exception as exc:  # pragma: no cover - defensive
            warnings.append(
                "%s: next-page selector %r failed (%s)" % (section_name, next_selector, exc)
            )
            stop = "error"
            break
        if len(arrows) != 1:
            warnings.append(
                "%s: next-page selector %r matched %d elements, none clicked"
                % (section_name, next_selector, len(arrows))
            )
            stop = "error"
            break
        arrow = arrows[0]

        if _element_disabled(arrow):
            stop = "end"
            break

        if pages_read >= max_pages:
            _pages_limit_warning(warnings, section_name, pages_read, rows)
            stop = "limit"
            break

        previous_number, previous_error = _read_active_page_number(page, active_selector)
        if previous_number is None:
            warnings.append(
                "%s: %s"
                % (
                    section_name,
                    previous_error
                    or ("active-page selector %r could not be read" % active_selector),
                )
            )
            stop = "error"
            break

        # Anchor the page number: before clicking, the active number must equal
        # the number of pages read so far.
        if previous_number != pages_read:
            warnings.append(
                "%s: page number %d does not match pages read %d"
                % (section_name, previous_number, pages_read)
            )
            stop = "error"
            break

        # Click hardening: the resolved arrow's collapsed text must contain no
        # digit. A numbered page button is never the arrow; if the selector
        # matched a page number button instead, we must not click it.
        try:
            arrow_text = _collapsed(arrow.text_content())
        except Exception:
            arrow_text = ""
        if any(ch.isdigit() for ch in arrow_text):
            warnings.append(
                "%s: next-page selector matched an element with digits in its text (%r) — "
                "this is a page number button, not the arrow; no click" % (section_name, arrow_text)
            )
            stop = "error"
            break

        before = _rows_signature(page, signature_row_selector, signature_items_selector)
        try:
            arrow.click()
        except Exception as exc:
            warnings.append("%s: next-page click failed (%s)" % (section_name, exc))
            stop = "error"
            break
        clicks += 1

        # The cabinet replaces the list in place, and the page number may move
        # before the new rows arrive: wait until the number moved on AND the
        # rendered rows are there and differ from the rows before the click.
        # A page whose number moved but whose list stays empty (a header-only
        # or truly empty page) is not "not advanced": the extraction below
        # reports the empty page as an error.
        number_moved = False
        advanced = False
        for attempt in range(polls):
            if _active_page_number(page, active_selector) == previous_number + 1:
                number_moved = True
                current = _rows_signature(page, signature_row_selector, signature_items_selector)
                if current and current != before:
                    advanced = True
                    break
            if attempt + 1 < polls:
                time.sleep(_PAGE_POLL_MS / 1000.0)
        if not advanced and not number_moved:
            warnings.append(
                "%s: page did not advance past %d" % (section_name, previous_number)
            )
            stop = "error"
            break

        if settle_ms > 0:
            time.sleep(settle_ms / 1000.0)

        next_rows = _extract_rows_stable(
            page, record_def, section_name, warnings, timeout_ms=timeout_ms, now=now
        )

        # A page that yields zero rows after the click means the list did not
        # actually switch to a page with operations (e.g. a header-only page or
        # an empty page). This is an error.
        if not next_rows:
            warnings.append(
                "%s: page %d yielded zero rows after the click (the list did not switch "
                "to a page with operations)" % (section_name, pages_read + 1)
            )
            stop = "error"
            break

        next_number = pages_read + 1

        if _row_key_counts(next_rows) == _row_key_counts(previous_rows):
            warnings.append(
                "%s: page %d content did not change after the click "
                "(the list did not switch pages)" % (section_name, next_number)
            )
            stop = "error"
            break

        # The page before was followed by another one, so it must have been
        # full. The final page (a disabled arrow) may be shorter.
        if page_size is not None and len(previous_rows) != page_size:
            warnings.append(
                "%s: page %d showed %d operations, expected %d"
                % (section_name, pages_read, len(previous_rows), page_size)
            )
            short_page = True

        newest = _newest_date(next_rows)
        oldest_before = _oldest_date(previous_rows)
        if newest is not None and oldest_before is not None and newest > oldest_before:
            warnings.append(
                "%s: page %d starts at %s, newer than page %d ends at %s "
                "(history may be incomplete)"
                % (section_name, next_number, newest, pages_read, oldest_before)
            )
            short_page = True

        rows.extend(next_rows)
        previous_rows = list(next_rows)
        pages_read = next_number

    # After the walk ends with stop "date" or "end", revisit the record's URL
    # and re-read the first page to detect history shifts (an operation arriving
    # mid-run shifts every page by one). Skip when stop is error/limit or the
    # record has no url.
    recheck = "skipped"
    if stop in ("date", "end") and record_def.get("url"):
        record_url = record_def["url"]
        try:
            page.goto(record_url)
        except Exception as exc:
            # The page could not be opened again: the recheck cannot be made.
            # Never claim completeness on an unverified first page, and say
            # that the recheck itself failed rather than that it was skipped.
            warnings.append("%s: first page recheck failed (%s)" % (section_name, exc))
            recheck = "failed"
            short_page = True
        else:
            wait_for = record_def.get("wait_for")
            if wait_for:
                try:
                    page.wait_for_selector(wait_for, timeout=timeout_ms)
                except Exception as exc:
                    # A missing selector is reported but not fatal, exactly like
                    # ``visit_record``: the extraction below decides.
                    warnings.append(
                        "waited for %r but it did not appear (%s)" % (wait_for, exc)
                    )
            try:
                recheck_rows = _extract_rows_stable(
                    page, record_def, section_name, warnings, timeout_ms=timeout_ms, now=now
                )
            except Exception as exc:  # pragma: no cover - defensive
                warnings.append("%s: first page recheck failed (%s)" % (section_name, exc))
                recheck_rows = []
            first_page_keys_at_end = _row_key_counts(recheck_rows)
            if not recheck_rows:
                # Zero rows after a real navigation is not a smaller first
                # page: the session expired, the cabinet served its login
                # page or the load failed. Nothing can be proven about the
                # start of the history, so the run is incomplete.
                warnings.append(
                    "%s: the first page could not be re-read at the end "
                    "(session expired or page not loaded) — the run cannot be "
                    "proven complete" % section_name
                )
                recheck = "failed"
                short_page = True  # force complete to False
            elif first_page_keys_at_start != first_page_keys_at_end:
                warnings.append(
                    "%s: the first page changed during the run (a new operation arrived?) "
                    "— pages may have shifted, history may be incomplete" % section_name
                )
                recheck = "changed"
                short_page = True  # force complete to False
            else:
                recheck = "same"

    info = {
        "mode": "pages",
        "pages": pages_read,
        "clicks": clicks,
        "rows": len(rows),
        "stop": stop,
        "oldest_date": _oldest_date(rows),
        "headerless_rows": 0,
        "recheck": recheck,
    }
    _judge_completeness(warnings, section_name, info, stop_before)
    # A short intermediate page or a newer page above an older one can only
    # turn a complete run into an incomplete one, never the other way round.
    if short_page:
        info["complete"] = False
    return rows, info


def _judge_completeness(warnings, section_name, info, stop_before):
    """Set ``info["complete"]`` and say so when the history is short.

    The history is complete when an older row than *stop_before* was seen
    (``date``), when the list's own "next page" arrow is disabled (``end``:
    a definitive end of history, whatever date the oldest row has) or when
    the scrolled list stopped growing at a row of that day or older. A list
    that stopped growing too early (``empty``), a broken scroll or page
    switch (``error``) or a page that cannot scroll may simply not have
    rendered the older rows yet; without this flag such a run would look
    like a complete history. Without *stop_before* only a list that stopped
    growing counts as complete. The round and page limits have a warning of
    their own.

    Rows that could not be dated at all (``headerless_rows``) were silently
    dropped from the output, so a run with any of them is never complete:
    the warning says how many operations are missing and why.
    """
    stop = info["stop"]
    oldest = info.get("oldest_date")
    if stop in ("date", "end"):
        complete = True
    elif stop == "limit":
        complete = False
    elif not stop_before:
        complete = stop == "empty"
    else:
        complete = oldest is not None and oldest <= stop_before
        if not complete:
            warnings.append(
                "%s: history reaches back only to %s, not to %s (stop: %s, history may be incomplete)"
                % (section_name, oldest or "no dated row", stop_before, stop)
            )

    headerless = info.get("headerless_rows") or 0
    if headerless:
        warnings.append(
            "%s: at least %d operations could not be dated (their day header was not "
            "rendered together with them) and were skipped"
            % (section_name, headerless)
        )
        complete = False
    info["complete"] = complete


def extract_section(
    page,
    record_def,
    section_name,
    warnings,
    visited=None,
    timeout_ms=DEFAULT_TIMEOUT_MS,
    scroll_info=None,
    now=None,
    signed_out=None,
):
    """Extract one section (``balances`` or ``transactions``) from *page*.

    A record may hold ``items``: rows *inside* every row of the group. The
    fields of the group and of the item are merged into one output row — the
    item wins when both define the same field. A record may carry ``header``
    instead (a flat feed of sibling day headers and operations).

    A record may hold ``scroll``: the driver then scrolls the rendered list
    (read-only) and keeps the rows that appear while the list is scrolled.
    A record may hold ``pages`` instead: the list is cut into numbered pages
    and the driver presses the list's own "next page" arrow, reading every
    page that replaces the previous one. *scroll_info* — when a dictionary is
    given — receives a small report about that run (the same ``scroll`` block
    of the report, with ``mode: "pages"`` for a paginated one).
    """
    visit_record(
        page,
        record_def,
        warnings,
        visited=visited,
        timeout_ms=timeout_ms,
        signed_out=signed_out,
    )

    headerless = {"headerless_rows": 0}
    scroll_def = record_def.get("scroll")
    pages_def = record_def.get("pages")
    # The operation rows (with their dates) travel from one pass into the
    # next, so an operation whose header already left a virtualised DOM can
    # still be dated by its overlap with the previous rendered window.
    # Without scrolling there is no later round to carry them into, and the
    # single pass behaves as it always did.
    carry = {"rows": []} if scroll_def and record_def.get("header") else None
    results = _extract_rows_stable(
        page, record_def, section_name, warnings, timeout_ms=timeout_ms,
        now=now, counter=headerless, carry=carry,
    )

    if scroll_def:
        results, info = _scroll_section(
            page, record_def, section_name, warnings, results, scroll_def,
            timeout_ms=timeout_ms, now=now, headerless=headerless, carry=carry,
        )
        short = _short_days(
            results, (carry or {}).get("day_totals"), scroll_def.get("stop_before_date")
        )
        if short:
            info["short_days"] = len(short)
            info["complete"] = False
            for date, got, expected in short[:_SCROLL_WARNING_LIMIT]:
                warnings.append(
                    "%s: %s: collected expenses %s are below the cabinet's day total %s "
                    "(an operation was lost or dated by another day)"
                    % (section_name, date, got, expected)
                )
            if len(short) > _SCROLL_WARNING_LIMIT:
                warnings.append(
                    "%s: %d more days are short of their day total"
                    % (section_name, len(short) - _SCROLL_WARNING_LIMIT)
                )
        if scroll_info is not None:
            scroll_info.update(info)

    if pages_def:
        results, info = _paginate_section(
            page, record_def, section_name, warnings, results, pages_def,
            timeout_ms=timeout_ms, now=now,
        )
        if scroll_info is not None:
            scroll_info.update(info)

    return results


def collect(page, profile, timeout_ms=DEFAULT_TIMEOUT_MS, now=None):
    """Collect balances and transactions for *profile* from an open *page*.

    Returns a dict with the keys ``bank``, ``status``, ``source_url``,
    ``source_urls``, ``fetched_at``, ``balances``, ``transactions`` and
    ``warnings``. ``source_urls`` lists every page that was opened, in order.
    When a record asked for scrolling or numbered pages, the dict also holds
    a ``scroll`` key that tells how the run ended, how far back it reached and
    whether that is the whole history the profile asked for. A scrolled run
    reports ``{"rounds": N, "rows": M, "stop": "limit"|"empty"|"date"|
    "error"|"no-scroll", "oldest_date": "YYYY-MM-DD"|None, "clicks": K,
    "load_more_missing": E, "headerless_rows": H, "complete": bool}``: how
    many load-more clicks it made, how many empty rounds found no matching
    button in the list container, how many operation rows could not be dated
    at all and were therefore skipped (``headerless_rows``, a virtualised flat
    feed with no reliable overlap between rounds). A paginated run reports
    ``{"mode": "pages", "pages": P, "clicks": K, "rows": M, "stop": "date"|
    "end"|"limit"|"error", "oldest_date": ..., "headerless_rows": 0,
    "recheck": "same"|"changed"|"failed"|"skipped", "complete": bool}``: how many
    pages were read, how many times the "next page" arrow was pressed, where the
    walk stopped (``end`` is a disabled arrow — the list's definitive end of
    history) and whether the re-read first page matched the one read at the
    start (``recheck``: ``same``; ``changed`` when a non-empty page differs;
    ``failed`` when the page could not be opened again or it yielded no rows
    at all — an expired session or a failed load, which also leaves the run
    incomplete; ``skipped`` only when the walk stopped with ``error``/``limit``
    or the record has no ``url``).

    When the profile has a ``signed_out`` block and the page matches it (the
    cabinet redirected to its sign-in screen) the run stops at once, before
    any further navigation or click: the result then holds no balances and no
    transactions, the ``signed_out`` marker that matched, and a warning
    ``signed out: the cabinet shows its sign-in screen (...)``.

    *now* anchors the run's ``fetched_at`` (kept in UTC) and the year and
    relative day ("Сегодня"/"Вчера") inferred by the ``ru_date`` field
    option. The latter are resolved on the cabinet's calendar day: the
    profile's optional ``timezone`` (an IANA name, ``Europe/Moscow`` by
    default), not UTC.
    """
    warnings = []
    url = profile["url"]
    visited = []
    scroll_sections = {}
    signed_out_config = profile.get("signed_out")
    signed_out = None
    balances = []
    transactions = []

    if now is None:
        now = datetime.now(timezone.utc)
    if isinstance(now, datetime) and now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    local_now = _local_now(now, profile, warnings)

    try:
        try:
            page.goto(url)
        except Exception as exc:
            raise RuntimeError("cannot open %s: %s" % (url, exc))
        visited.append(url)

        # The sign-in screen is checked right after the URL was opened and
        # again after the wait: reading a sign-in page as if it were the
        # cabinet would only produce empty rows and misleading warnings.
        _raise_if_signed_out(page, signed_out_config)

        wait_for = profile.get("wait_for")
        if wait_for:
            try:
                page.wait_for_selector(wait_for, timeout=timeout_ms)
            except Exception as exc:
                warnings.append("waited for %r but it did not appear (%s)" % (wait_for, exc))
        _raise_if_signed_out(page, signed_out_config)

        for record_def in profile.get("balances") or []:
            info = {}
            balances.extend(
                extract_section(
                    page,
                    record_def,
                    "balances",
                    warnings,
                    visited=visited,
                    timeout_ms=timeout_ms,
                    scroll_info=info,
                    now=local_now,
                    signed_out=signed_out_config,
                )
            )
            if info:
                scroll_sections["balances"] = info

        for record_def in profile.get("transactions") or []:
            info = {}
            transactions.extend(
                extract_section(
                    page,
                    record_def,
                    "transactions",
                    warnings,
                    visited=visited,
                    timeout_ms=timeout_ms,
                    scroll_info=info,
                    now=local_now,
                    signed_out=signed_out_config,
                )
            )
            if info:
                scroll_sections["transactions"] = info
    except _SignedOut as exc:
        # Nothing extracted: a partial screen of a sign-in page is not the
        # cabinet.  The caller reports the marker and exits with its own code.
        signed_out = exc.marker
        balances = []
        transactions = []
        scroll_sections = {}

    if signed_out:
        warnings.append(SIGNED_OUT_WARNING % signed_out)
    else:
        if not balances:
            warnings.append("no balances extracted")
        if not transactions:
            warnings.append("no transactions extracted")

    # ``fetched_at`` stays the UTC instant whatever the cabinet's timezone is.
    if isinstance(now, datetime):
        instant = now.astimezone(timezone.utc)
    else:
        instant = now
    fetched_at = instant.isoformat().replace("+00:00", "Z")

    result = {
        "bank": profile.get("bank"),
        "status": profile.get("status"),
        "source_url": visited[0] if visited else url,
        "source_urls": list(visited),
        "fetched_at": fetched_at,
        "balances": balances,
        "transactions": transactions,
        "warnings": warnings,
    }

    if signed_out:
        result["signed_out"] = signed_out
        return result

    scroll_info = scroll_sections.get("transactions") or scroll_sections.get("balances")
    if scroll_info:
        result["scroll"] = scroll_info
    return result
