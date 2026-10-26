"""Generic, read-only collection driver.

The driver only ever *reads* an already rendered DOM. It never types, never
submits a form and never touches a downloading button. Its one write is an
optional, narrowly scoped click on the list's own "load more" button (see
``scroll.load_more_text`` below): it is made only when scrolling added no new
rows, the ``scroll.load_more_scope`` selector names exactly one list container
that holds rows of the record, and exactly one button inside that container
carries exactly the configured text — it only asks the list to render its next
page.

The driver talks to a tiny protocol so that it can be tested without a
browser (see ``tests/fake_page.py``) while still driving real Playwright
objects:

``page`` must provide
    ``goto(url)``                  -- open the cabinet page
    ``wait_for_selector(sel, timeout=...)`` -- wait until the SPA rendered
    ``query_all(selector)``        -- list of elements matching a CSS selector
    ``content()``                  -- optional, the rendered HTML (snapshots)
    ``url``                        -- the URL that is currently open
    ``scroll_by(dy)``              -- optional, scroll the viewport by *dy*
                                      pixels (read-only: no click, no DOM
                                      mutation, no request of our own)

An *element* must provide
    ``query_all(selector)``        -- descendants matching a CSS selector
    ``text_content()``             -- the visible text of the element
    ``get_attribute(name)``        -- an attribute value or ``None``
    ``click()``                    -- optional, press the element (used only
                                      for the one "load more" button)

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

Finally a record may carry ``scroll``: the driver then scrolls the rendered
list (read-only) and reads the rows that the cabinet appends while the list
is scrolled, until the list stops growing, the date limit of the profile is
reached or the round limit is hit. When scrolling a round added no new rows
and the record names a ``load_more_text``, the driver may press the single
button with exactly that text inside the one container named by
``load_more_scope``: the click only asks the list to render its next page.
"""

import re
import time
from collections import Counter

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
        # A handle obtained from ``query_all`` dies when the SPA re-renders the
        # list (the cabinet navigates after the code screen, and again while a
        # scrolled list appends rows). Reading it then raises
        # "Execution context was destroyed" — one broken element must never
        # abort the whole run, so it becomes a warning and an empty field.
        try:
            if attr:
                text = element.get_attribute(attr)
                if text is None:
                    warnings.append("%s: attribute %r missing" % (where, attr))
                    fields[name] = {"raw": None, "value": None, "currency": None, "money": False}
                    continue
            else:
                text = element.text_content()
        except Exception as exc:
            warnings.append("%s: reading %r failed (%s)" % (where, spec.get("selector"), exc))
            fields[name] = {"raw": None, "value": None, "currency": None, "money": False}
            continue

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


def _extract_rows(page, record_def, section_name, warnings):
    """Read the rows of a record as they are rendered right now.

    A record may hold ``items``: rows *inside* every row of the group. The
    fields of the group and of the item are merged into one output row — the
    item wins when both define the same field.
    """
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


def _extract_rows_stable(page, record_def, section_name, warnings, timeout_ms=DEFAULT_TIMEOUT_MS):
    """Read the rows, retrying once when the DOM was torn by a navigation.

    A single ``query_all`` + ``text_content`` pass can fail while the cabinet
    replaces the rendered list (after the code screen, or while a scrolled
    list appends rows). The pass is then repeated once after a short pause and
    a fresh wait for the record's ``wait_for`` selector. Two failures do not
    raise: they become one warning and an empty result, because a lost section
    is better reported than a lost run.
    """
    attempt_warnings = []
    try:
        rows = _extract_rows(page, record_def, section_name, attempt_warnings)
        warnings.extend(attempt_warnings)
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
    try:
        rows = _extract_rows(page, record_def, section_name, retry_warnings)
    except Exception as exc:
        warnings.append("%s: extraction failed after 2 attempts (%s)" % (section_name, exc))
        return []
    warnings.extend(retry_warnings)
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


def _load_more_buttons(container, load_more_text):
    """The buttons of *container* whose whole text is exactly *load_more_text*.

    The text of a button is compared stripped and whitespace-collapsed, so a
    label that is wrapped over two lines still matches. Only ``<button>``
    elements inside the container are considered; a link or a div that happens
    to carry the text is never pressed.
    """
    matches = []
    for element in container.query_all("button"):
        try:
            text = element.text_content()
        except Exception:  # pragma: no cover - a re-render can kill a handle
            continue
        if _collapsed(text) == load_more_text:
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


def _foreign_load_more_count(page, record_def, load_more_scope, load_more_text):
    """How many same-label controls were not clickable under the rules.

    Counts every ``button``/``[role=button]``/``a`` with the configured whole
    text on the page, then subtracts the ``<button>`` elements that sit inside
    the one valid list container. A positive number means the history may be
    incomplete: the list offered no clickable button, but a same-label control
    existed elsewhere (an offers block) or as a link.
    """
    total = 0
    for element in page.query_all("button, [role=button], a"):
        try:
            text = element.text_content()
        except Exception:  # pragma: no cover - a re-render can kill a handle
            continue
        if _collapsed(text) == load_more_text:
            total += 1
    if not total:
        return 0

    clickable = 0
    for container in _load_more_containers(page, record_def, load_more_scope):
        clickable += len(_load_more_buttons(container, load_more_text))
    return max(total - clickable, 0)


def _absorb_round(page, record_def, section_name, scrolled_warnings, rows, seen, timeout_ms):
    """Read the list once and keep the rows that are new; return how many.

    *rows* and *seen* are updated in place. A row is kept when its key has
    not been kept that many times yet (two equal coffees are two rows); a row
    with no key at all is always kept.
    """
    added = 0
    in_round = Counter()
    for row in _extract_rows_stable(
        page, record_def, section_name, scrolled_warnings, timeout_ms=timeout_ms
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
    page, record_def, section_name, warnings, rows, scroll_def, timeout_ms=DEFAULT_TIMEOUT_MS
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
    matching button in the list (``load_more_missing``) and why the scrolling
    stopped.
    """
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
            page, record_def, section_name, scrolled_warnings, rows, seen, timeout_ms
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
                    page, record_def, section_name, scrolled_warnings, rows, seen, timeout_ms
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


def _judge_completeness(warnings, section_name, info, stop_before):
    """Set ``info["complete"]`` and say so when the history is short.

    The history is complete when an older row than *stop_before* was seen
    (``date``) or when the list stopped growing at a row of that day or
    older. A list that stopped growing too early (``empty``), a broken
    scroll (``error``) or a page that cannot scroll may simply not have
    rendered the older rows yet; without this flag such a run would look
    like a complete history. Without *stop_before* only a list that stopped
    growing counts as complete. The round limit has a warning of its own.
    """
    stop = info["stop"]
    oldest = info.get("oldest_date")
    if stop == "date":
        info["complete"] = True
        return
    if stop == "limit":
        info["complete"] = False
        return
    if not stop_before:
        info["complete"] = stop == "empty"
        return
    info["complete"] = oldest is not None and oldest <= stop_before
    if not info["complete"]:
        warnings.append(
            "%s: history reaches back only to %s, not to %s (stop: %s, history may be incomplete)"
            % (section_name, oldest or "no dated row", stop_before, stop)
        )


def extract_section(
    page,
    record_def,
    section_name,
    warnings,
    visited=None,
    timeout_ms=DEFAULT_TIMEOUT_MS,
    scroll_info=None,
):
    """Extract one section (``balances`` or ``transactions``) from *page*.

    A record may hold ``items``: rows *inside* every row of the group. The
    fields of the group and of the item are merged into one output row — the
    item wins when both define the same field.

    A record may hold ``scroll``: the driver then scrolls the rendered list
    (read-only) and keeps the rows that appear while the list is scrolled.
    *scroll_info* — when a dictionary is given — receives a small report
    about that run.
    """
    visit_record(page, record_def, warnings, visited=visited, timeout_ms=timeout_ms)

    results = _extract_rows_stable(page, record_def, section_name, warnings, timeout_ms=timeout_ms)

    scroll_def = record_def.get("scroll")
    if scroll_def:
        results, info = _scroll_section(
            page, record_def, section_name, warnings, results, scroll_def, timeout_ms=timeout_ms
        )
        if scroll_info is not None:
            scroll_info.update(info)

    return results


def collect(page, profile, timeout_ms=DEFAULT_TIMEOUT_MS, now=None):
    """Collect balances and transactions for *profile* from an open *page*.

    Returns a dict with the keys ``bank``, ``status``, ``source_url``,
    ``source_urls``, ``fetched_at``, ``balances``, ``transactions`` and
    ``warnings``. ``source_urls`` lists every page that was opened, in order.
    When a record asked for scrolling, the dict also holds a ``scroll`` key
    (``{"rounds": N, "rows": M, "stop": "limit"|"empty"|"date"|"error"|
    "no-scroll", "oldest_date": "YYYY-MM-DD"|None, "clicks": K,
    "load_more_missing": E, "complete": bool}``) that tells how the scroll
    ended, how far back it reached, how many load-more clicks it made, how
    many empty rounds found no matching button in the list container and
    whether that is the whole history the profile asked for.
    """
    warnings = []
    url = profile["url"]
    visited = []
    scroll_sections = {}

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
            )
        )
        if info:
            scroll_sections["balances"] = info

    transactions = []
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
            )
        )
        if info:
            scroll_sections["transactions"] = info

    if not balances:
        warnings.append("no balances extracted")
    if not transactions:
        warnings.append("no transactions extracted")

    if now is None:
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
    fetched_at = now.isoformat().replace("+00:00", "Z")

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

    scroll_info = scroll_sections.get("transactions") or scroll_sections.get("balances")
    if scroll_info:
        result["scroll"] = scroll_info
    return result
