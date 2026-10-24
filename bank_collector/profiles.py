"""Loading and validation of bank profiles.

A profile is a small YAML document. It is the ONLY thing you have to write to
support a new bank — no Python, no code, just selectors.

Canonical form::

    bank: example
    status: verified            # verified | unverified
    url: "https://example.invalid/cabinet"
    wait_for: "#dashboard"      # CSS selector, waits for the SPA to render
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

A record (one entry of ``balances`` or ``transactions``) accepts the keys
``row``, ``fields``, ``url``, ``wait_for`` and ``items``:

``row``
    CSS selector of the repeating container. Optional — without it the whole
    page is treated as a single row.
``fields``
    Mapping "field name → specification" (``selector``, optional ``attr``,
    optional ``regex``/``group``).
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
"""

import os
import re

from . import miniyaml

__all__ = [
    "ProfileError",
    "load_profile",
    "list_profiles",
    "profile_dir",
    "VALID_STATUSES",
]

VALID_STATUSES = ("verified", "unverified")

_REQUIRED_KEYS = ("bank", "status", "url", "wait_for")
_OPTIONAL_KEYS = ("balances", "transactions")
_ALLOWED_TOP_KEYS = _REQUIRED_KEYS + _OPTIONAL_KEYS + ("notes",)
_FIELD_KEYS = ("selector", "attr", "regex", "group")

# The keys a record (one entry of ``balances``/``transactions``) may carry.
_RECORD_KEYS = ("row", "fields", "url", "wait_for", "items")

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


def _normalise_record(index, record, section, where):
    if not isinstance(record, dict):
        raise ProfileError("%s: record #%d must be a mapping" % (where, index))

    url = None
    wait_for = None
    items = None

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
        fields = {}
        for key, value in record.items():
            if key == "row" or key in reserved:
                continue
            fields[key] = value
        url = record.get("url") if "url" in reserved else None
        wait_for = record.get("wait_for") if "wait_for" in reserved else None
        items = record.get("items") if "items" in reserved else None

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

    return {
        "row": row,
        "fields": fields,
        "url": url,
        "wait_for": wait_for,
        "items": normalised_items,
    }


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
