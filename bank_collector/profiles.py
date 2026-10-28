"""Loading and validation of bank profiles.

A profile is a small YAML document. It is the ONLY thing you have to write to
support a new bank — no Python, no code, just selectors.

Canonical form::

    bank: example
    status: verified            # verified | unverified
    url: "https://example.invalid/cabinet"
    wait_for: "#dashboard"      # CSS selector, waits for the SPA to render
    timezone: Europe/Moscow     # optional IANA name; anchors ru_date's "today"
    balances:
      - row: ".balance-card"    # optional: repeating container
        fields:
          account:
            selector: ".balance-card__account"
          amount:
            selector: ".balance-card__amount"
            regex: "([\\d ]+)"  # optional
            group: 1            # optional
    transactions:
      - row: ".tx-row"
        fields:
          date:
            selector: ".tx-row__date"
          amount:
            selector: ".tx-row__amount"

``bank``, ``status``, ``url`` and ``wait_for`` are required. ``balances`` and
``transactions`` are optional, but at least one of them must be there.
``timezone`` is optional: an IANA name (``Europe/Moscow`` by default) of the
cabinet's own timezone. Day headers are rendered there, so ``parse: ru_date``
resolves "Сегодня", "Вчера" and a year-less date on that calendar day, not on
the UTC one. When the tz database is missing altogether (native Windows
without the ``tzdata`` package) every name is accepted and the run resolves
dates in UTC with a warning.

A record (one entry of ``balances`` or ``transactions``) accepts the keys
``row``, ``fields``, ``url``, ``wait_for``, ``items``, ``header``, ``scroll``
and ``pages``:

``row``
    CSS selector of the repeating container. Optional — without it the whole
    page is treated as a single row.
``fields``
    Mapping "field name → specification" (``selector``, optional ``attr``,
    optional ``regex``/``group``, optional ``parse`` and optional
    ``optional``). ``parse: ru_date`` turns a Russian day header
    (``"27 октября"``, ``"27 октября 2025"``, ``"Сегодня"``, ``"Вчера"``,
    with any trailing total ignored) into an ISO ``YYYY-MM-DD`` value; a
    missing year is inferred relative to the run's ``now`` (previous year when
    the date would be in the future). ``optional: true`` keeps a missing
    element silent instead of warning per row.
``url``
    Optional: a page of its own. When the URL differs from the page that is
    currently open, the driver opens it (a plain ``goto``) before reading the
    record. Needed when the balances and the statement live on different
    pages.
``wait_for``
    Optional: a CSS selector the driver waits for after opening ``url``
    (or on the current page when there is no ``url``).
``items``
    Optional: a nested record (only ``row`` and ``fields``) describing rows
    *inside* every ``row`` of the group. The fields of the group and of the
    item are merged into one output row — used when the operations of a day
    are grouped under a shared date. ``items`` inside ``items`` is rejected.
``header``
    Optional: for a *flat* feed where day headers and operations are siblings
    in one list. A mapping with exactly ``selector`` (a CSS selector inside a
    row that marks it as a day header) and ``fields`` (the header's field
    specifications, normally the date). ``row`` (required with ``header``)
    then matches every row of the feed in document order: a header row
    updates the current header values and is not emitted, every other row
    inherits them (the operation wins on a field-name clash). Operations
    rendered before the first header of a pass are dated by the header
    carried over from earlier scroll rounds of the same section; only when
    nothing suitable was carried are they skipped silently. ``header``
    cannot be combined with ``items``.
``scroll``
    Optional: how far and how long to scroll the rendered list, so that the
    rows a cabinet appends while the list is scrolled are read as well.
    Scrolling is read-only — it moves the viewport and sends no request of
    our own.  It is only allowed on a record that has a ``row`` (without rows
    there is nothing to count)::

        scroll:
          max_rounds: 200          # hard cap on scroll steps, int >= 1, default 50
          step_px: 1200            # pixels per step, int >= 100, default 1500
          settle_ms: 900           # wait after each step for the SPA to append rows, int >= 0, default 800
          stop_after_empty_rounds: 4   # stop after N consecutive rounds with no new rows, int >= 1, default 3
          stop_before_date: "2026-01-01"  # optional YYYY-MM-DD: stop once a row dated strictly before it is seen
          load_more_text: "Показать ещё"  # optional: the one "load more" button, default none
          load_more_scope: "div.list"     # REQUIRED with load_more_text: the list container
          max_clicks: 100          # cap on clicks of that button, int >= 1, default 100

    The driver stops when the list stops growing, when a row is dated strictly
    before ``stop_before_date`` or when ``max_rounds`` is reached (the last
    case is reported as a warning).

    ``load_more_text`` is the single exception to "scrolling never clicks":
    when scrolling a round added no new rows, the driver looks for buttons
    whose whole text (stripped, whitespace collapsed) equals the string.  It
    clicks only when exactly one such button exists, and that click merely
    asks the list to render its next page.  None or several matches mean no
    click.  ``max_clicks`` limits how many such clicks one run may make.

    ``load_more_scope`` is a CSS selector of the list container and is
    **required** whenever ``load_more_text`` is set (and forbidden without
    it).  At click time only containers that hold at least one row of the
    record are counted; the scope must match exactly one of them, otherwise
    nothing is clicked and the run stops with an error.  The button is then
    searched only inside that container, so a same-label control of another
    widget (an offers block, a link) can never be pressed.
``pages``
    Optional: a history list cut into numbered pages that the cabinet
    replaces in place — ten operations per page, a "next" arrow, the same URL
    throughout.  It cannot be combined with ``scroll`` (two ways to read one
    list) or with ``header`` (a numbered-page list does not carry day headers
    across page boundaries) and, like ``scroll``, it needs a ``row``.  It
    describes the one arrow the driver may press and the element that shows
    the current page::

        pages:
          next_selector: "nav.pagination .pagination__pages > button.pagination__item_arrow:last-child"
                                      # REQUIRED: the one "next page" arrow; the ONLY click
          active_selector: "nav.pagination button.pagination__item_active"
                                      # REQUIRED: the element whose text is the current page number
          page_size: 10               # optional: expected operations per full page, int >= 1
          settle_ms: 800              # wait after the page changed, int >= 0, default 800
          wait_ms: 15000              # how long the next page may take to render, int >= 1, default 15000
          max_pages: 400              # hard cap on pages read, int >= 1, default 300
          stop_before_date: "2026-01-01"  # optional YYYY-MM-DD: stop once a row dated strictly before it is seen

    The driver never presses anything else in this mode.  An arrow that does
    not resolve to exactly one element is not clicked and stops the run with
    an error; an arrow carrying ``disabled`` (or ``aria-disabled="true"``) is
    the list's definitive end of history.  The resolved arrow's collapsed text
    must contain no digit (a numbered page button is never the arrow).  The
    current page number must be exactly one element whose whole text is an
    integer: the first page must read 1, the number before every click must
    equal the number of pages read so far, and it must move to the next number
    afterwards (``wait_ms``).  The rows of every page are kept as they are:
    the pages are disjoint, so two equal coffees on two pages are two
    operations.  A page that repeats the previous page's rows, or a page that
    yields zero operation rows, is an error.  ``page_size`` and the day order
    (a newer page above an older one would mean a lost page) are checked and
    only ever turn ``complete`` to False.  After a walk that ended with
    ``date`` or ``end`` the record's URL is opened again and the first page is
    re-read: a changed first page means a new operation arrived mid-run and
    the pages may have shifted, so the run is reported incomplete.

The "flat" sugar form is accepted too — a record definition may list the
fields directly instead of nesting them under ``fields``::

    transactions:
      - date:
          selector: ".tx-row__date"
        amount:
          selector: ".tx-row__amount"

A profile NEVER contains credentials: there is no field for a login, a
password, an SMS code or a cookie. The validator rejects any unknown key and
any field whose name looks like a credential, so that such a thing cannot be
smuggled in unnoticed.

An optional ``unlock`` section describes the cabinet's static "secret code"
screen (not an SMS code) so that ``--enter-code`` can recognise it::

    unlock:
      markers: ["секретный код"]   # phrases that betray the screen
      submit_text: ["Вперёд"]      # label of the submit button
      input_selector: 'input[type=password]'  # optional: exact field
      success_wait: "#dashboard"   # appears once the cabinet opened
      code_file: "~/.config/bank-collector/alfa-code"  # where the code lives
      max_attempts: 1              # entries per run (>= 1)
      min_attempts_left: 3         # refuse below this many tries left
      status: unverified           # the screen markup was never observed

The section never holds the code itself — only the way to recognise the
screen and the path of the file the code is read from (mode 0600, outside
the repository).  It is used only when the collector is run with
``--enter-code``.

The keys of the section and what the validator accepts:

``markers``
    list of non-empty strings — the phrases that betray the screen. A plain
    string is accepted as a one-element list.
``submit_text``
    list of non-empty strings — the labels of the submit button.
``input_selector``
    optional CSS selector of the code field. When it is given it wins over
    the heuristic of ``bank_collector.unlock``.
``success_wait``
    optional CSS selector that appears once the cabinet opened.
``code_file``
    optional path of the file the code is read from (``~`` is expanded).
``max_attempts``
    optional positive integer — how many codes may be entered in one run.
``min_attempts_left``
    optional non-negative integer — below this many attempts left on the
    screen nothing is typed at all.
``status``
    optional, ``verified`` or ``unverified`` like the status of the profile.

An optional ``signed_out`` section names what the cabinet shows when nobody is
signed in, so that a run stops at once and says "the session expired" instead
of blaming the selectors::

    signed_out:
      url_contains: ["/login"]        # substrings of the page URL path
      text: ["Войти в онлайн-банк"]   # visible texts of the sign-in screen

At least one of the two lists must be present and non-empty; a plain string is
accepted as a one-element list.  The URL markers are matched as substrings of
the page's URL path, the text markers against the page's body text
(case-folded, whitespace collapsed).  They only describe the sign-in screen —
like the rest of the profile they never carry a credential (the credential
guard still rejects a key that looks like one).
"""

import os
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import miniyaml

__all__ = [
    "ProfileError",
    "load_profile",
    "list_profiles",
    "profile_dir",
    "VALID_STATUSES",
]

VALID_STATUSES = ("verified", "unverified")

#: The field options that post-process a value after the regex step.
_PARSE_KINDS = ("ru_date",)

_REQUIRED_KEYS = ("bank", "status", "url", "wait_for")
_OPTIONAL_KEYS = ("balances", "transactions")
_ALLOWED_TOP_KEYS = _REQUIRED_KEYS + _OPTIONAL_KEYS + (
    "notes",
    "unlock",
    "signed_out",
    "timezone",
)
_FIELD_KEYS = ("selector", "attr", "regex", "group", "parse", "optional")

# The keys the optional ``signed_out`` section may carry.  It names what the
# sign-in screen looks like — a substring of the URL path and/or a visible
# text — never a credential.
_SIGNED_OUT_KEYS = ("url_contains", "text")

# The keys the optional ``unlock`` section may carry.  It describes how the
# cabinet's static "secret code" screen is recognised — it never carries the
# code itself, only the markers, the button label and the path of the file
# the code is read from.
_UNLOCK_KEYS = (
    "markers",
    "submit_text",
    "input_selector",
    "success_wait",
    "code_file",
    "max_attempts",
    "min_attempts_left",
    "status",
)

# The keys a record (one entry of ``balances``/``transactions``) may carry.
_RECORD_KEYS = ("row", "fields", "url", "wait_for", "items", "header", "scroll", "pages")

# The keys the optional ``scroll`` block of a record may carry.  It says how
# far and how long to scroll the rendered list, and names the one "load more"
# button the driver may press when scrolling added nothing — never any other
# click.
_SCROLL_KEYS = (
    "max_rounds",
    "step_px",
    "settle_ms",
    "stop_after_empty_rounds",
    "stop_before_date",
    "load_more_text",
    "load_more_scope",
    "max_clicks",
)

#: The only labels the driver may ever press.  A profile cannot smuggle in
#: "Скачать выписку", "Оплатить" or any other button by naming it here: the
#: label is compared case-insensitively after whitespace collapsing against
#: this allowlist, and anything else is a profile error.  It is deliberately
#: narrow: the owner-approved label and its orthographic variant only.
_LOAD_MORE_LABELS = (
    "Показать ещё",
    "Показать еще",
)

#: The canonical spelling for every case-folded allowlist label.  A profile
#: may type any casing; the canonical spelling is stored, and the runtime
#: comparison case-folds both sides.
_LOAD_MORE_LABELS_BY_FOLDED = {
    label.casefold(): label for label in _LOAD_MORE_LABELS
}

#: The values used when the profile does not say otherwise.
_SCROLL_DEFAULTS = {
    "max_rounds": 50,
    "step_px": 1500,
    "settle_ms": 800,
    "stop_after_empty_rounds": 3,
    "stop_before_date": None,
    "load_more_text": None,
    "load_more_scope": None,
    "max_clicks": 100,
}

#: The smallest value each numeric key may have.
_SCROLL_MINIMUMS = {
    "max_rounds": 1,
    "step_px": 100,
    "settle_ms": 0,
    "stop_after_empty_rounds": 1,
    "max_clicks": 1,
}

# The keys the optional ``pages`` block of a record may carry.  It describes
# a history list cut into numbered pages: the one "next" arrow the driver may
# press and the element that shows the current page number — never any other
# click.
_PAGES_KEYS = (
    "next_selector",
    "active_selector",
    "page_size",
    "settle_ms",
    "wait_ms",
    "max_pages",
    "stop_before_date",
)

#: The values used when the profile does not say otherwise.
_PAGES_DEFAULTS = {
    "next_selector": None,
    "active_selector": None,
    "page_size": None,
    "settle_ms": 800,
    "wait_ms": 15000,
    "max_pages": 300,
    "stop_before_date": None,
}

#: The smallest value each numeric key may have.
_PAGES_MINIMUMS = {
    "page_size": 1,
    "settle_ms": 0,
    "wait_ms": 1,
    "max_pages": 1,
}

#: The only date format the profile accepts (``stop_before_date``).
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# A profile describes *what to read*, never *how to log in*. Any field whose
# name looks like a credential is rejected outright, so that a login, a
# password, an SMS code or a cookie cannot be smuggled into a profile (and
# from there into the output).
_CREDENTIAL_WORDS = (
    "password",
    "passwd",
    "pwd",
    "passphrase",
    "token",
    "cookie",
    "session",
    "otp",
    "sms",
    "pin",
    "cvc",
    "cvv",
    "secret",
    "login",
    "logon",
    "credential",
    "creds",
    "auth",
    "captcha",
)

# These words are too dangerous to appear even inside a longer word
# ("userPassword", "authToken").
_CREDENTIAL_SUBSTRINGS = (
    "password",
    "passwd",
    "token",
    "cookie",
    "credential",
    "secret",
)


class ProfileError(Exception):
    """Raised when a profile is missing, unreadable or invalid."""


def profile_dir():
    """Directory that ships with the package and holds the ``*.yaml`` profiles."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "profiles")


def _load_yaml(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        raise ProfileError("cannot read profile %s: %s" % (path, exc))

    try:
        import yaml  # optional: PyYAML is more permissive than our own parser
    except ImportError:
        yaml = None

    if yaml is not None:
        try:
            data = yaml.safe_load(text)
        except Exception as exc:  # pragma: no cover - depends on optional dep
            raise ProfileError("cannot parse profile %s: %s" % (path, exc))
    else:
        try:
            data = miniyaml.safe_load(text)
        except miniyaml.YamlError as exc:
            raise ProfileError("cannot parse profile %s: %s" % (path, exc))

    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ProfileError("profile %s must be a YAML mapping" % path)
    return data


def _looks_like_credential(name):
    """True when a field name looks like a login, a password, a code or a cookie.

    The check is intentionally generous: it splits the name into words
    (camelCase and ``_``/``-`` both count) and compares them against a
    denylist, and it also looks for the strongest words *inside* a word.
    """
    text = str(name)
    # camelCase -> camel_Case, so that "userPassword" is seen as two words.
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", text)
    words = [word for word in re.split(r"[^A-Za-z0-9]+", text) if word]
    for word in words:
        if word.lower() in _CREDENTIAL_WORDS:
            return True
    joined = "".join(words).lower()
    for strong in _CREDENTIAL_SUBSTRINGS:
        if strong in joined:
            return True
    return False


def _check_field(name, spec, where):
    if not isinstance(spec, dict):
        raise ProfileError("%s: field %r must be a mapping" % (where, name))
    if _looks_like_credential(name):
        raise ProfileError(
            "%s: field %r looks like a credential — a profile must never contain "
            "a login, a password, an SMS code or a cookie" % (where, name)
        )
    unknown = sorted(set(spec) - set(_FIELD_KEYS))
    if unknown:
        raise ProfileError(
            "%s: field %r has unknown key(s) %s (allowed: %s)"
            % (where, name, ", ".join(unknown), ", ".join(_FIELD_KEYS))
        )
    selector = spec.get("selector")
    if not isinstance(selector, str) or not selector.strip():
        raise ProfileError("%s: field %r needs a non-empty 'selector'" % (where, name))
    attr = spec.get("attr")
    if attr is not None and not isinstance(attr, str):
        raise ProfileError("%s: field %r: 'attr' must be a string" % (where, name))
    regex = spec.get("regex")
    if regex is not None:
        if not isinstance(regex, str):
            raise ProfileError("%s: field %r: 'regex' must be a string" % (where, name))
        import re

        try:
            re.compile(regex)
        except re.error as exc:
            raise ProfileError("%s: field %r: invalid regex %r (%s)" % (where, name, regex, exc))
    group = spec.get("group")
    if group is not None and (not isinstance(group, int) or isinstance(group, bool)):
        raise ProfileError("%s: field %r: 'group' must be an integer" % (where, name))

    parse = spec.get("parse")
    if parse is not None and parse not in _PARSE_KINDS:
        raise ProfileError(
            "%s: field %r: 'parse' must be one of %s (got %r)"
            % (where, name, ", ".join(_PARSE_KINDS), parse)
        )

    optional = spec.get("optional")
    if optional is not None and not isinstance(optional, bool):
        raise ProfileError(
            "%s: field %r: 'optional' must be a boolean" % (where, name)
        )


def _normalise_items(index, items, where):
    """Validate the nested ``items`` mapping of a record.

    ``items`` may only carry ``row`` and ``fields``: a group of rows never
    navigates on its own and never holds another group (``items`` inside
    ``items`` is rejected).
    """
    if not isinstance(items, dict):
        raise ProfileError("%s: record #%d: 'items' must be a mapping" % (where, index))

    if "items" in items:
        raise ProfileError(
            "%s: record #%d: 'items' must not contain 'items' — a group of a group "
            "is not supported" % (where, index)
        )
    for key in ("url", "wait_for"):
        if key in items:
            raise ProfileError(
                "%s: record #%d: 'items' must not contain %r — the page is opened "
                "once per record, not per group" % (where, index, key)
            )

    unknown = sorted(set(items) - {"row", "fields"})
    if unknown:
        raise ProfileError(
            "%s: record #%d: 'items' has unknown key(s) %s (allowed: row, fields)"
            % (where, index, ", ".join(unknown))
        )

    row = items.get("row")
    if row is not None and (not isinstance(row, str) or not row.strip()):
        raise ProfileError("%s: record #%d: 'items.row' must be a non-empty string" % (where, index))

    fields = items.get("fields")
    if not isinstance(fields, dict) or not fields:
        raise ProfileError(
            "%s: record #%d: 'items.fields' must be a non-empty mapping" % (where, index)
        )

    for name, spec in fields.items():
        _check_field(name, spec, "%s record #%d items" % (where, index))

    return {"row": row, "fields": fields}


def _normalise_header(index, header, where):
    """Validate the optional ``header`` block of a record.

    The block describes a *flat* feed: ``selector`` marks the rows that are
    day headers (the selector is looked up inside every row), and ``fields``
    are the fields such a header carries (normally the date). Everything else
    is the record's own ``fields``.
    """
    if not isinstance(header, dict):
        raise ProfileError("%s: record #%d: 'header' must be a mapping" % (where, index))

    unknown = sorted(set(header) - {"selector", "fields"})
    if unknown:
        raise ProfileError(
            "%s: record #%d: 'header' has unknown key(s) %s (allowed: selector, fields)"
            % (where, index, ", ".join(unknown))
        )

    selector = header.get("selector")
    if not isinstance(selector, str) or not selector.strip():
        raise ProfileError(
            "%s: record #%d: 'header.selector' must be a non-empty CSS selector"
            % (where, index)
        )

    fields = header.get("fields")
    if not isinstance(fields, dict) or not fields:
        raise ProfileError(
            "%s: record #%d: 'header.fields' must be a non-empty mapping" % (where, index)
        )

    for name, spec in fields.items():
        _check_field(name, spec, "%s record #%d header" % (where, index))

    return {"selector": selector.strip(), "fields": fields}


def _normalise_scroll(index, scroll, where):
    """Validate the optional ``scroll`` block of a record.

    The block says how far and how long the driver scrolls the rendered
    list.  ``load_more_text`` names the one button that may be clicked when
    scrolling added nothing: the text must be a non-empty string from the
    fixed allowlist (``_LOAD_MORE_LABELS``), and it is compared with the
    whole text of a button.  ``load_more_scope`` names the list container and
    is required with ``load_more_text`` (and forbidden without it): the
    button is searched only there, so a same-label control elsewhere is never
    pressed.  Everything else stays read-only — scrolling only moves the
    viewport.
    """
    if not isinstance(scroll, dict):
        raise ProfileError("%s: record #%d: 'scroll' must be a mapping" % (where, index))

    unknown = sorted(set(scroll) - set(_SCROLL_KEYS))
    if unknown:
        raise ProfileError(
            "%s: record #%d: 'scroll' has unknown key(s) %s (allowed: %s)"
            % (where, index, ", ".join(unknown), ", ".join(_SCROLL_KEYS))
        )

    result = dict(_SCROLL_DEFAULTS)

    for key, minimum in _SCROLL_MINIMUMS.items():
        value = scroll.get(key)
        if value is None:
            continue
        if not isinstance(value, int) or isinstance(value, bool):
            raise ProfileError(
                "%s: record #%d: 'scroll.%s' must be an integer" % (where, index, key)
            )
        if value < minimum:
            raise ProfileError(
                "%s: record #%d: 'scroll.%s' must be >= %d (got %r)"
                % (where, index, key, minimum, value)
            )
        result[key] = value

    stop_before = scroll.get("stop_before_date")
    if stop_before is not None:
        result["stop_before_date"] = _check_stop_date(index, stop_before, where)

    load_more_text = scroll.get("load_more_text")
    if load_more_text is not None:
        if not isinstance(load_more_text, str) or not load_more_text.strip():
            raise ProfileError(
                "%s: record #%d: 'scroll.load_more_text' must be a non-empty string"
                % (where, index)
            )
        # Compared with the whitespace-collapsed text of a button, and stored
        # in the allowlist's own spelling so the profile reads canonically.
        collapsed = " ".join(load_more_text.split())
        canonical = _LOAD_MORE_LABELS_BY_FOLDED.get(collapsed.casefold())
        if canonical is None:
            raise ProfileError(
                "%s: record #%d: 'scroll.load_more_text' must be one of the known "
                "load-more labels (%s), got %r"
                % (where, index, ", ".join(_LOAD_MORE_LABELS), collapsed)
            )
        result["load_more_text"] = canonical

    load_more_scope = scroll.get("load_more_scope")
    if load_more_scope is not None:
        if not isinstance(load_more_scope, str) or not load_more_scope.strip():
            raise ProfileError(
                "%s: record #%d: 'scroll.load_more_scope' must be a non-empty CSS selector"
                % (where, index)
            )
        result["load_more_scope"] = load_more_scope.strip()

    if result["load_more_text"] is not None and result["load_more_scope"] is None:
        raise ProfileError(
            "%s: record #%d: 'scroll.load_more_text' requires 'scroll.load_more_scope' — "
            "without a list container a same-label button elsewhere could be pressed"
            % (where, index)
        )
    if result["load_more_scope"] is not None and result["load_more_text"] is None:
        raise ProfileError(
            "%s: record #%d: 'scroll.load_more_scope' is only allowed together with "
            "'scroll.load_more_text'" % (where, index)
        )

    return result


def _normalise_pages(index, pages, where):
    """Validate the optional ``pages`` block of a record.

    The block describes a history split into numbered pages: the list
    replaces its content in place and the URL never changes.  It names the
    one "next" arrow the driver may press (``next_selector``, the only click
    of this mode) and the element that carries the current page number
    (``active_selector``).  Every other key is a bound, not an action:
    ``page_size`` says how many operations a full page should hold,
    ``settle_ms``/``wait_ms`` how long the driver waits for the next page and
    ``max_pages`` caps the run.  The block cannot be combined with
    ``scroll`` — both describe one and the same list.
    """
    if not isinstance(pages, dict):
        raise ProfileError("%s: record #%d: 'pages' must be a mapping" % (where, index))

    unknown = sorted(set(pages) - set(_PAGES_KEYS))
    if unknown:
        raise ProfileError(
            "%s: record #%d: 'pages' has unknown key(s) %s (allowed: %s)"
            % (where, index, ", ".join(unknown), ", ".join(_PAGES_KEYS))
        )

    result = dict(_PAGES_DEFAULTS)

    for key, minimum in _PAGES_MINIMUMS.items():
        value = pages.get(key)
        if value is None:
            continue
        if not isinstance(value, int) or isinstance(value, bool):
            raise ProfileError(
                "%s: record #%d: 'pages.%s' must be an integer" % (where, index, key)
            )
        if value < minimum:
            raise ProfileError(
                "%s: record #%d: 'pages.%s' must be >= %d (got %r)"
                % (where, index, key, minimum, value)
            )
        result[key] = value

    for key in ("next_selector", "active_selector"):
        value = pages.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ProfileError(
                "%s: record #%d: 'pages.%s' must be a non-empty CSS selector"
                % (where, index, key)
            )
        result[key] = value.strip()

    stop_before = pages.get("stop_before_date")
    if stop_before is not None:
        result["stop_before_date"] = _check_stop_date(
            index, stop_before, where, key="pages.stop_before_date"
        )

    return result


def _check_stop_date(index, value, where, key="scroll.stop_before_date"):
    """Validate a ``YYYY-MM-DD`` date (and return it untouched).

    *key* is the full dotted name of the option, so the error names the block
    the date came from (``scroll`` or ``pages``).
    """
    if not isinstance(value, str) or not _DATE_RE.match(value.strip()):
        raise ProfileError(
            "%s: record #%d: %r must be a YYYY-MM-DD date (got %r)"
            % (where, index, key, value)
        )
    text = value.strip()
    from datetime import date

    try:
        date(int(text[0:4]), int(text[5:7]), int(text[8:10]))
    except ValueError:
        raise ProfileError(
            "%s: record #%d: %r is not a real date (%r)"
            % (where, index, key, value)
        )
    return text


def _normalise_record(index, record, section, where):
    if not isinstance(record, dict):
        raise ProfileError("%s: record #%d must be a mapping" % (where, index))

    url = None
    wait_for = None
    items = None
    header = None
    scroll = None
    pages = None

    if "fields" in record:
        unknown = sorted(set(record) - set(_RECORD_KEYS))
        if unknown:
            raise ProfileError(
                "%s: record #%d has unknown key(s) %s (allowed: %s)"
                % (where, index, ", ".join(unknown), ", ".join(_RECORD_KEYS))
            )
        fields = record.get("fields")
        if not isinstance(fields, dict) or not fields:
            raise ProfileError("%s: record #%d: 'fields' must be a non-empty mapping" % (where, index))
        url = record.get("url")
        wait_for = record.get("wait_for")
        items = record.get("items")
        header = record.get("header")
        scroll = record.get("scroll")
        pages = record.get("pages")
    else:
        # The "flat" sugar form: the fields sit directly under the record. A
        # field may be named "url"/"wait_for" as long as its value is a
        # mapping (a field specification) and not a string.
        reserved = set()
        for key in ("url", "wait_for"):
            if key in record and isinstance(record[key], str):
                reserved.add(key)
        if "items" in record:
            value = record["items"]
            if isinstance(value, dict) and "selector" not in value:
                reserved.add("items")
        if "header" in record:
            value = record["header"]
            # The header block has its own ``selector``; its ``fields`` key is
            # what tells it apart from a field named "header".
            if isinstance(value, dict) and "fields" in value:
                reserved.add("header")
        if "scroll" in record:
            value = record["scroll"]
            if isinstance(value, dict) and "selector" not in value:
                reserved.add("scroll")
        if "pages" in record:
            value = record["pages"]
            if isinstance(value, dict) and "selector" not in value:
                reserved.add("pages")
        fields = {}
        for key, value in record.items():
            if key == "row" or key in reserved:
                continue
            fields[key] = value
        url = record.get("url") if "url" in reserved else None
        wait_for = record.get("wait_for") if "wait_for" in reserved else None
        items = record.get("items") if "items" in reserved else None
        header = record.get("header") if "header" in reserved else None
        scroll = record.get("scroll") if "scroll" in reserved else None
        pages = record.get("pages") if "pages" in reserved else None

    if not fields:
        raise ProfileError("%s: record #%d has no fields" % (where, index))

    for name, spec in fields.items():
        _check_field(name, spec, "%s record #%d" % (where, index))

    row = record.get("row")
    if row is not None and (not isinstance(row, str) or not row.strip()):
        raise ProfileError("%s: record #%d: 'row' must be a non-empty string" % (where, index))

    if url is not None:
        if not isinstance(url, str) or not url.strip():
            raise ProfileError("%s: record #%d: 'url' must be a non-empty string" % (where, index))
        if not url.startswith(("http://", "https://")):
            raise ProfileError(
                "%s: record #%d: 'url' must start with http:// or https://" % (where, index)
            )

    if wait_for is not None and (not isinstance(wait_for, str) or not wait_for.strip()):
        raise ProfileError(
            "%s: record #%d: 'wait_for' must be a non-empty CSS selector" % (where, index)
        )

    normalised_items = None
    if items is not None:
        normalised_items = _normalise_items(index, items, where)

    normalised_header = None
    if header is not None:
        normalised_header = _normalise_header(index, header, where)

    if normalised_items is not None and normalised_header is not None:
        raise ProfileError(
            "%s: record #%d: 'header' and 'items' cannot be used together — a flat "
            "feed with day headers and a nested group of a day are two ways to say "
            "the same thing" % (where, index)
        )

    if normalised_header is not None and row is None:
        raise ProfileError(
            "%s: record #%d: 'header' needs a 'row' — the header selector is looked "
            "up inside every row of the feed" % (where, index)
        )

    normalised_scroll = None
    if scroll is not None:
        if row is None:
            raise ProfileError(
                "%s: record #%d: 'scroll' needs a 'row' — there must be rows to count"
                % (where, index)
            )
        normalised_scroll = _normalise_scroll(index, scroll, where)

    normalised_pages = None
    if pages is not None:
        if row is None:
            raise ProfileError(
                "%s: record #%d: 'pages' needs a 'row' — there must be rows to count"
                % (where, index)
            )
        normalised_pages = _normalise_pages(index, pages, where)

    # 'pages' and 'header' together are unsafe: when a day header is on the
    # previous page and its operations start on the next page, the header is not
    # carried across the page boundary (the list is replaced in place). The
    # leading operations of that page would then have no date and be silently
    # dropped while complete=true. Reject the combination explicitly.
    if normalised_pages is not None and normalised_header is not None:
        raise ProfileError(
            "%s: record #%d: 'pages' and 'header' cannot be used together — a "
            "numbered-page list does not carry day headers across page "
            "boundaries, so operations whose header was on the previous page "
            "would lose their date and be silently dropped" % (where, index)
        )

    if normalised_scroll is not None and normalised_pages is not None:
        raise ProfileError(
            "%s: record #%d: 'scroll' and 'pages' cannot be used together — scrolling a "
            "list and switching its numbered pages are two ways to read the same history"
            % (where, index)
        )

    return {
        "row": row,
        "fields": fields,
        "url": url,
        "wait_for": wait_for,
        "items": normalised_items,
        "header": normalised_header,
        "scroll": normalised_scroll,
        "pages": normalised_pages,
    }


def _normalise_unlock(section, where):
    """Validate the optional ``unlock`` section of a profile.

    The section only *describes* the cabinet's static "secret code" screen:
    the phrases that betray it, the label of the submit button, the selector
    that shows the cabinet opened and the path of the file the code is read
    from.  The code itself is never part of a profile — it lives in a file
    outside the repository (mode 0600) or in an environment variable.
    """
    if not isinstance(section, dict):
        raise ProfileError("%s: 'unlock' must be a mapping" % where)

    unknown = sorted(set(section) - set(_UNLOCK_KEYS))
    if unknown:
        raise ProfileError(
            "%s: 'unlock' has unknown key(s) %s (allowed: %s)"
            % (where, ", ".join(unknown), ", ".join(_UNLOCK_KEYS))
        )

    def _string_list(key):
        value = section.get(key)
        if value is None:
            return None
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list) or not value:
            raise ProfileError(
                "%s: 'unlock.%s' must be a non-empty list of non-empty strings" % (where, key)
            )
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise ProfileError(
                    "%s: 'unlock.%s' must be a non-empty list of non-empty strings" % (where, key)
                )
        return [item.strip() for item in value]

    markers = _string_list("markers")
    submit_text = _string_list("submit_text")

    input_selector = section.get("input_selector")
    if input_selector is not None and (
        not isinstance(input_selector, str) or not input_selector.strip()
    ):
        raise ProfileError("%s: 'unlock.input_selector' must be a non-empty CSS selector" % where)

    success_wait = section.get("success_wait")
    if success_wait is not None and (
        not isinstance(success_wait, str) or not success_wait.strip()
    ):
        raise ProfileError("%s: 'unlock.success_wait' must be a non-empty CSS selector" % where)

    code_file = section.get("code_file")
    if code_file is not None and (not isinstance(code_file, str) or not code_file.strip()):
        raise ProfileError("%s: 'unlock.code_file' must be a non-empty path" % where)

    max_attempts = section.get("max_attempts")
    if max_attempts is not None:
        if not isinstance(max_attempts, int) or isinstance(max_attempts, bool):
            raise ProfileError("%s: 'unlock.max_attempts' must be an integer" % where)
        if max_attempts < 1:
            raise ProfileError(
                "%s: 'unlock.max_attempts' must be a positive integer (got %r)" % (where, max_attempts)
            )

    min_attempts_left = section.get("min_attempts_left")
    if min_attempts_left is not None:
        if not isinstance(min_attempts_left, int) or isinstance(min_attempts_left, bool):
            raise ProfileError("%s: 'unlock.min_attempts_left' must be an integer" % where)
        if min_attempts_left < 0:
            raise ProfileError(
                "%s: 'unlock.min_attempts_left' must not be negative (got %r)"
                % (where, min_attempts_left)
            )

    status = section.get("status")
    if status is not None and status not in VALID_STATUSES:
        raise ProfileError(
            "%s: 'unlock.status' must be one of %s (got %r)"
            % (where, ", ".join(VALID_STATUSES), status)
        )

    return {
        "markers": markers,
        "submit_text": submit_text,
        "input_selector": input_selector,
        "success_wait": success_wait,
        "code_file": code_file,
        "max_attempts": max_attempts,
        "min_attempts_left": min_attempts_left,
        "status": status,
    }


def _normalise_signed_out(section, where):
    """Validate the optional ``signed_out`` section of a profile.

    The section describes the cabinet's sign-in screen: a substring of the
    URL path and/or a visible text that betrays it.  At least one of the two
    lists must be present and non-empty; a plain string is accepted as a
    one-element list.  It never holds a credential — an unknown key is
    rejected, and a key whose name looks like a credential is named as such.
    """
    if not isinstance(section, dict):
        raise ProfileError("%s: 'signed_out' must be a mapping" % where)

    for key in section:
        if _looks_like_credential(key):
            raise ProfileError(
                "%s: 'signed_out' key %r looks like a credential — the section "
                "describes the sign-in screen, never a login, a password, an SMS "
                "code or a cookie" % (where, key)
            )

    unknown = sorted(set(section) - set(_SIGNED_OUT_KEYS))
    if unknown:
        raise ProfileError(
            "%s: 'signed_out' has unknown key(s) %s (allowed: %s)"
            % (where, ", ".join(unknown), ", ".join(_SIGNED_OUT_KEYS))
        )

    result = {"url_contains": [], "text": []}
    for key in _SIGNED_OUT_KEYS:
        value = section.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list) or not value:
            raise ProfileError(
                "%s: 'signed_out.%s' must be a non-empty list of non-empty strings"
                % (where, key)
            )
        cleaned = []
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise ProfileError(
                    "%s: 'signed_out.%s' must be a non-empty list of non-empty strings"
                    % (where, key)
                )
            cleaned.append(item.strip())
        result[key] = cleaned

    if not result["url_contains"] and not result["text"]:
        raise ProfileError(
            "%s: 'signed_out' needs at least one of 'url_contains' or 'text' — "
            "an empty section could never recognise the sign-in screen" % where
        )

    return result


def _validate(data, path):
    where = os.path.basename(path)

    unknown = sorted(set(data) - set(_ALLOWED_TOP_KEYS))
    if unknown:
        raise ProfileError(
            "%s: unknown top-level key(s) %s (allowed: %s)"
            % (where, ", ".join(unknown), ", ".join(_ALLOWED_TOP_KEYS))
        )

    for key in _REQUIRED_KEYS:
        if key not in data:
            raise ProfileError("%s: missing required key %r" % (where, key))

    bank = data["bank"]
    if not isinstance(bank, str) or not bank.strip():
        raise ProfileError("%s: 'bank' must be a non-empty string" % where)

    status = data["status"]
    if status not in VALID_STATUSES:
        raise ProfileError(
            "%s: 'status' must be one of %s (got %r)" % (where, ", ".join(VALID_STATUSES), status)
        )

    url = data["url"]
    if not isinstance(url, str) or not url.strip():
        raise ProfileError("%s: 'url' must be a non-empty string" % where)
    if not url.startswith(("http://", "https://")):
        raise ProfileError("%s: 'url' must start with http:// or https://" % where)

    wait_for = data["wait_for"]
    if not isinstance(wait_for, str) or not wait_for.strip():
        raise ProfileError("%s: 'wait_for' must be a non-empty CSS selector" % where)

    # Optional: the cabinet's own timezone (an IANA name). Day headers are
    # rendered there, so "Сегодня" must be resolved on that calendar day.
    timezone_name = data.get("timezone")
    if timezone_name is not None:
        if not isinstance(timezone_name, str) or not timezone_name.strip():
            raise ProfileError(
                "%s: 'timezone' must be a non-empty IANA timezone name (e.g. "
                "Europe/Moscow)" % where
            )
        timezone_name = timezone_name.strip()
        try:
            ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            # Either the name is wrong, or the tz database itself is absent
            # (native Windows without the `tzdata` package): there, every
            # name raises. Probe a name no database would lack; if even that
            # fails, accept the name and resolve it at run time.
            try:
                ZoneInfo("UTC")
            except ZoneInfoNotFoundError:
                pass
            else:
                raise ProfileError(
                    "%s: 'timezone' %r is not a known IANA timezone name"
                    % (where, timezone_name)
                )
        except ValueError:
            raise ProfileError(
                "%s: 'timezone' %r is not a known IANA timezone name"
                % (where, timezone_name)
            )
        data["timezone"] = timezone_name

    # At least one of the two sections must be described.
    if "balances" not in data and "transactions" not in data:
        raise ProfileError(
            "%s: a profile must define at least one of 'balances' or 'transactions'" % where
        )

    for section in ("balances", "transactions"):
        if section not in data:
            # A profile may describe only one of the two sections (some banks
            # show the balances and the statement on different pages).
            data[section] = []
            continue
        records = data[section]
        if not isinstance(records, list):
            raise ProfileError("%s: %r must be a list of record definitions" % (where, section))
        normalised = []
        for index, record in enumerate(records):
            normalised.append(_normalise_record(index, record, section, "%s: %s" % (where, section)))
        data[section] = normalised

    notes = data.get("notes")
    if notes is not None and not isinstance(notes, str):
        raise ProfileError("%s: 'notes' must be a string" % where)

    if "unlock" in data:
        data["unlock"] = _normalise_unlock(data["unlock"], where)

    if "signed_out" in data:
        data["signed_out"] = _normalise_signed_out(data["signed_out"], where)

    return data


def load_profile(name, directory=None):
    """Load and validate the profile called *name*.

    Raises :class:`ProfileError` when the profile does not exist or is invalid.
    """
    if not name or not isinstance(name, str):
        raise ProfileError("profile name must be a non-empty string")
    if any(sep in name for sep in ("/", "\\", "..")):
        raise ProfileError("invalid profile name %r" % name)

    directory = directory or profile_dir()
    path = os.path.join(directory, "%s.yaml" % name)
    if not os.path.isfile(path):
        available = ", ".join(p["name"] for p in list_profiles(directory)) or "none"
        raise ProfileError(
            "profile %r not found (available: %s)" % (name, available)
        )

    data = _load_yaml(path)
    return _validate(data, path)


def list_profiles(directory=None):
    """Return the list of available profiles, sorted by name."""
    directory = directory or profile_dir()
    try:
        names = sorted(
            os.path.splitext(entry)[0]
            for entry in os.listdir(directory)
            if entry.endswith(".yaml")
        )
    except OSError:
        return []

    result = []
    for name in names:
        path = os.path.join(directory, "%s.yaml" % name)
        entry = {"name": name, "path": path, "bank": name, "status": None}
        try:
            data = _load_yaml(path)
            entry["bank"] = data.get("bank", name)
            entry["status"] = data.get("status")
        except ProfileError:
            pass
        result.append(entry)
    return result
