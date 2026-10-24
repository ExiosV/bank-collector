"""Optional, opt-in answer to the cabinet's static "secret code" screen.

Alfa-Bank's web cabinet sometimes asks for a **static** code ("секретный код",
a permanent cabinet code the owner knows) after a long absence.  This is NOT
an SMS code, NOT an e-mail code and NOT a login: the session in the browser
is still alive, the cabinet only re-asks for the code it already knows.

Everything in this module is off by default.  It is reached only through
``bank-collector --enter-code`` and only when a code screen was actually
recognised.  The rules of this module:

* the code is read from a file (mode 0600, outside the repository) or from
  the ``BANK_COLLECTOR_CODE`` environment variable — never from the command
  line, so it never lands in ``ps``, in the shell history or in a log;
* the code is never logged, never printed, never written into a report, a
  JSON file or an exception message — it is masked as ``***`` everywhere;
* this is the ONLY place in the whole package that types or clicks: one
  function, :func:`enter_code`, called only after :func:`detect_code_screen`
  has recognised the screen.  Nothing else in the code base writes into a
  page;
* no JS is evaluated in the page: everything is read through the small
  protocol of ``bank_collector.collect`` (``query_all``, ``get_attribute``,
  ``text_content``, ``inner_text``).

The detection uses the markers, the submit label and (when the profile gives
one) the exact CSS selector of the code field.  When no ``input_selector``
is given, a conservative heuristic is used instead: it looks for three
things at once:

1. one of the marker phrases in the visible page text;
2. exactly one visible input of a code-ish type (``password``, ``text``,
   ``tel``, ``number``) that is not hidden;
3. a visible button (or ``[role=button]``) whose label is one of the known
   "go on" labels.

If any of the three is missing, nothing happens at all — the run continues as
it would without ``--enter-code``.

Two guards stand between the screen and the keyboard:

* the **attempts guard** — the visible text is searched for "осталось N
  попыт"; when fewer than ``min_attempts_left`` tries are left, nothing is
  typed at all and the run says why;
* the **limit of entries** — at most ``max_attempts`` codes (one by
  default) are entered in one run, however often the screen reappears.
"""

import os
import re
import time

__all__ = [
    "DEFAULT_MARKERS",
    "DEFAULT_SUBMIT_TEXTS",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_MIN_ATTEMPTS_LEFT",
    "DEFAULT_UNLOCK_TIMEOUT_MS",
    "ENV_CODE",
    "CodeScreen",
    "UnlockError",
    "CodeFileError",
    "attempts_left",
    "detect_code_screen",
    "probe_code_screen",
    "enter_code",
    "handle_code_screen",
    "read_code",
    "check_code_file_mode",
]

#: Phrases that hint at the static-code screen (matched case-insensitively).
DEFAULT_MARKERS = ("секретный код", "код доступа", "код подтверждения")

#: Labels of the button that submits the code.
DEFAULT_SUBMIT_TEXTS = ("Вперёд", "Продолжить", "Подтвердить", "Войти", "Далее")

#: How many codes may be entered in one run.
DEFAULT_MAX_ATTEMPTS = 1

#: Below this many attempts left on the screen nothing is typed at all.
DEFAULT_MIN_ATTEMPTS_LEFT = 3

#: How long the code screen is waited for (the cabinet is checked as well).
DEFAULT_UNLOCK_TIMEOUT_MS = 10000

#: Environment variable that may hold the code (used only if no file does).
ENV_CODE = "BANK_COLLECTOR_CODE"

#: Input types that may carry the code.
INPUT_TYPES = ("password", "text", "tel", "number")

#: Words that betray an SMS/OTP screen — the static code must never be typed
#: there, so such a screen is refused even if a marker matched.
SMS_HINTS = ("смс", "sms", "одноразов")

#: Markers that are specific to the static cabinet code.
STRONG_MARKERS = ("секретный код",)

#: "Осталось 2 попытки" / "Осталось 3 попытки" / "Осталось 5 попыток" —
#: the prefix "попыт" covers every Russian form of the word.
_ATTEMPTS_RE = re.compile(r"остал[оо]сь\s+(\d+)\s+попыт", re.IGNORECASE)


class UnlockError(Exception):
    """Something went wrong around the cabinet code."""


class CodeFileError(UnlockError):
    """The file with the code is missing, unreadable or too permissive."""


class CodeScreen(object):
    """What was found on the code screen: the input and the submit control."""

    def __init__(self, code_input, submit, marker=None, success_wait=None, attempts=None):
        self.code_input = code_input
        self.submit = submit
        self.marker = marker
        self.success_wait = success_wait
        #: How many tries the screen says are left (``None`` when it does not
        #: say — the only safe reading then is the one of the user).
        self.attempts = attempts

    def __repr__(self):  # pragma: no cover - debugging helper
        return "<CodeScreen marker=%r attempts=%r>" % (self.marker, self.attempts)


def _normalize(text):
    """Lower-case *text*, collapse whitespace and unify ``ё``/``е``."""
    if not text:
        return ""
    text = str(text).replace("ё", "е").replace("Ё", "Е")
    return " ".join(text.split()).lower()


def _attribute(element, name):
    """Read an attribute, tolerating elements that do not support it."""
    try:
        value = element.get_attribute(name)
    except Exception:  # pragma: no cover - defensive
        return None
    if value is None:
        return None
    return str(value)


def _is_hidden(element):
    """True when the element looks hidden, judged by its attributes only."""
    style = (_attribute(element, "style") or "").lower().replace(" ", "")
    if "display:none" in style or "visibility:hidden" in style:
        return True
    if _attribute(element, "hidden") is not None:
        return True
    if (_attribute(element, "aria-hidden") or "").strip().lower() == "true":
        return True
    return False


def _visible_text(page):
    """The visible text of the page (rendered text first, HTML as a fallback)."""
    for reader in ("inner_text", "content"):
        getter = getattr(page, reader, None)
        if getter is None:
            continue
        try:
            text = getter("body") if reader == "inner_text" else getter()
        except Exception:
            continue
        if text:
            return str(text)
    return ""


def _origin(url):
    """``scheme://host:port`` of *url* (empty when it cannot be parsed)."""
    if not url:
        return ""
    text = str(url)
    for separator in ("://",):
        if separator not in text:
            return ""
        scheme, rest = text.split(separator, 1)
        host = rest.split("/", 1)[0]
        return "%s://%s" % (scheme.lower(), host.lower())
    return ""


def _same_origin(left, right):
    """True when two URLs look same-origin (unknown URLs are assumed same)."""
    left_origin = _origin(left)
    right_origin = _origin(right)
    if not left_origin or not right_origin:
        return True
    return left_origin == right_origin


def _scopes(page):
    """The page itself plus its same-origin child frames (one level deep)."""
    yield page
    frames = getattr(page, "frames", None) or []
    page_url = getattr(page, "url", "") or ""
    for frame in frames:
        frame_url = getattr(frame, "url", "") or ""
        if not _same_origin(page_url, frame_url):
            continue
        yield frame


def attempts_left(text):
    """How many tries the screen says are left (``None`` when it does not say).

    Alfa writes it in Russian: "Осталось 2 попытки", "осталось 5 попыток".
    The prefix ``попыт`` matches every form of the word (попытка, попытки,
    попыток), and the number is the one that follows it.
    """
    if not text:
        return None
    match = _ATTEMPTS_RE.search(str(text))
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:  # pragma: no cover - the regex only matches digits
        return None


def _code_input(scope, input_selector=None):
    """The code field of *scope* (or ``None``).

    When *input_selector* is given (the profile knows the exact field) it
    wins: only the fields it matches are looked at, and the first visible
    one is taken.  Without it a conservative heuristic is used: if several
    inputs qualify, a single ``password`` field wins; if that is ambiguous
    too, nothing is returned — guessing wrong would type the code into
    somebody else's field.
    """
    if input_selector:
        try:
            elements = scope.query_all(input_selector)
        except Exception:  # pragma: no cover - defensive
            return None
        for element in elements:
            if _is_hidden(element):
                continue
            return element
        return None

    try:
        elements = scope.query_all("input")
    except Exception:  # pragma: no cover - defensive
        return None

    candidates = []
    for element in elements:
        kind = (_attribute(element, "type") or "text").strip().lower()
        if kind not in INPUT_TYPES:
            continue
        if _is_hidden(element):
            continue
        candidates.append((kind, element))

    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0][1]

    passwords = [element for kind, element in candidates if kind == "password"]
    if len(passwords) == 1:
        return passwords[0]
    return None


def _submit_control(scope, submit_texts):
    """The visible button whose label matches one of *submit_texts*."""
    wanted = [_normalize(text) for text in submit_texts if _normalize(text)]
    if not wanted:
        return None
    for selector in ("button", "[role=button]"):
        try:
            elements = scope.query_all(selector)
        except Exception:  # pragma: no cover - defensive
            elements = []
        for element in elements:
            if _is_hidden(element):
                continue
            if _attribute(element, "disabled") is not None:
                continue
            label = _normalize(element.text_content())
            if not label:
                continue
            for text in wanted:
                if text in label:
                    return element
    return None


def detect_code_screen(
    page,
    markers=None,
    submit_texts=None,
    success_wait=None,
    input_selector=None,
):
    """Recognise the static-code screen on *page*.

    Returns a :class:`CodeScreen` (the code input and the submit control) or
    ``None`` when the screen was not recognised.  The heuristics are
    deliberately conservative: all three conditions must hold at once.
    """
    markers = tuple(markers or DEFAULT_MARKERS)
    submit_texts = tuple(submit_texts or DEFAULT_SUBMIT_TEXTS)

    visible = _visible_text(page)
    text = _normalize(visible)
    if not text:
        return None

    matched = [marker for marker in markers if _normalize(marker) and _normalize(marker) in text]
    if not matched:
        return None

    # A screen that talks about SMS is not the static-code screen — the code
    # would go into the wrong field.  Only the explicit "секретный код"
    # marker may override that.
    if any(_normalize(hint) in text for hint in SMS_HINTS) and not any(
        _normalize(marker) in text for marker in STRONG_MARKERS
    ):
        return None

    for scope in _scopes(page):
        code_input = _code_input(scope, input_selector=input_selector)
        if code_input is None:
            continue
        submit = _submit_control(scope, submit_texts)
        if submit is None:
            continue
        return CodeScreen(
            code_input,
            submit,
            marker=matched[0],
            success_wait=success_wait,
            attempts=attempts_left(visible),
        )
    return None


def probe_code_screen(
    page,
    markers=None,
    submit_texts=None,
    success_wait=None,
    input_selector=None,
    ready_selector=None,
    timeout_ms=DEFAULT_UNLOCK_TIMEOUT_MS,
    interval=0.25,
):
    """Wait a little for the code screen (or for the cabinet) to show up.

    Returns a :class:`CodeScreen` or ``None``.  The wait ends as soon as
    either the code screen is recognised or *ready_selector* (usually the
    profile's dashboard selector) is on the page — in the latter case there
    is nothing to unlock.
    """
    deadline = time.time() + max(0.0, timeout_ms / 1000.0)
    while True:
        screen = detect_code_screen(
            page,
            markers=markers,
            submit_texts=submit_texts,
            success_wait=success_wait,
            input_selector=input_selector,
        )
        if screen is not None:
            return screen
        if ready_selector:
            try:
                if page.query_all(ready_selector):
                    return None
            except Exception:  # pragma: no cover - defensive
                pass
        if time.time() >= deadline:
            return None
        time.sleep(interval)


def _fill(element, value):
    """Type *value* into *element* (Playwright's ``fill`` first, then type)."""
    filler = getattr(element, "fill", None)
    if filler is not None:
        filler(value)
        return
    typer = getattr(element, "type_text", None) or getattr(element, "type", None)
    if typer is None:
        raise UnlockError("the element does not accept text")
    typer(value)


def _mask(text, code):
    """Replace every occurrence of *code* in *text* with ``***``."""
    if not text:
        return text
    if code:
        text = str(text).replace(code, "***")
    return text


def enter_code(page, code, screen, timeout_ms=10000):
    """Type *code* into the screen found by :func:`detect_code_screen`.

    Returns ``{"entered": bool, "reason": str}``.  The reason never contains
    the code — errors coming from the browser are masked before they are
    handed back.
    """
    if screen is None:
        return {"entered": False, "reason": "экран с кодом не найден"}

    try:
        _fill(screen.code_input, code)
    except Exception as exc:
        return {"entered": False, "reason": _mask("не удалось ввести код: %s" % exc, code)}

    try:
        screen.submit.click()
    except Exception as exc:
        return {"entered": False, "reason": _mask("не удалось нажать кнопку: %s" % exc, code)}

    success_wait = screen.success_wait
    if success_wait:
        try:
            page.wait_for_selector(success_wait, timeout=timeout_ms)
        except Exception as exc:
            return {
                "entered": False,
                "reason": _mask("кабинет не открылся: %s" % exc, code),
            }
    return {"entered": True, "reason": ""}


def check_code_file_mode(path):
    """Refuse a code file that other users can read.

    Raises :class:`CodeFileError` when the file is group- or world-readable;
    the message says exactly how to fix it.
    """
    try:
        mode = os.stat(path).st_mode & 0o777
    except OSError as exc:
        raise CodeFileError("cannot read the code file %s: %s" % (path, exc))
    if mode & 0o077:
        raise CodeFileError(
            "the code file %s is readable by other users (mode %04o) — "
            "fix it with: chmod 600 %s" % (path, mode, path)
        )
    return mode


def _read_code_file(path):
    """Read the code from *path* (permissions are checked first)."""
    check_code_file_mode(path)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError as exc:
        raise CodeFileError("cannot read the code file %s: %s" % (path, exc))


def read_code(path=None, profile_path=None, env=None):
    """Find the cabinet code.

    The sources are tried in this order:

    1. *path* — an explicit ``--code-file``;
    2. *profile_path* — the ``unlock.code_file`` of the bank profile
       (a leading ``~`` is expanded);
    3. the ``BANK_COLLECTOR_CODE`` environment variable.

    Returns ``(code, source)``; ``(None, None)`` when no code was found.
    *source* describes where the code came from and never contains it.
    """
    env = os.environ if env is None else env

    if path:
        expanded = os.path.expanduser(str(path))
        if not os.path.isfile(expanded):
            raise CodeFileError("the code file %s does not exist" % expanded)
        code = _read_code_file(expanded)
        if code:
            return code, "file:%s" % expanded

    if profile_path:
        expanded = os.path.expanduser(str(profile_path))
        if os.path.isfile(expanded):
            code = _read_code_file(expanded)
            if code:
                return code, "file:%s" % expanded

    value = env.get(ENV_CODE)
    if value and str(value).strip():
        return str(value).strip(), "env:%s" % ENV_CODE

    return None, None


def handle_code_screen(
    page,
    config=None,
    code=None,
    code_path=None,
    env=None,
    timeout_ms=DEFAULT_UNLOCK_TIMEOUT_MS,
    attempts_used=0,
    ready_selector=None,
    interval=0.25,
):
    """The whole flow of one run: detect → guard → read the code → type it.

    *config* is the ``unlock`` section of the profile (its keys are read
    with the same names as in the profile: ``markers``, ``submit_text``,
    ``input_selector``, ``success_wait``, ``code_file``, ``max_attempts``,
    ``min_attempts_left``).  *code* short-circuits the reading of the code
    (the CLI never uses it: the code comes from a file or from the
    environment), *code_path* is the explicit ``--code-file``.

    Returns a dict with the keys:

    ``detected``
        the code screen was recognised;
    ``entered``
        the code was typed and the button was pressed;
    ``reason``
        why nothing was entered (empty when everything went fine) — it
        never contains the code itself;
    ``source``
        where the code came from (``file:…`` or ``env:…``), never the code;
    ``fatal``
        the run must stop: the code file is missing or readable by others;
    ``code_path``
        the file the code was expected in.
    """
    config = dict(config or {})

    markers = config.get("markers")
    submit_texts = config.get("submit_text")
    success_wait = config.get("success_wait")
    input_selector = config.get("input_selector")
    profile_path = config.get("code_file")
    max_attempts = config.get("max_attempts")
    if max_attempts is None:
        max_attempts = DEFAULT_MAX_ATTEMPTS
    min_attempts_left = config.get("min_attempts_left")
    if min_attempts_left is None:
        min_attempts_left = DEFAULT_MIN_ATTEMPTS_LEFT

    result = {
        "detected": False,
        "entered": False,
        "reason": "",
        "source": None,
        "fatal": False,
        "code_path": None,
    }

    screen = probe_code_screen(
        page,
        markers=markers,
        submit_texts=submit_texts,
        success_wait=success_wait,
        input_selector=input_selector,
        ready_selector=ready_selector,
        timeout_ms=timeout_ms,
        interval=interval,
    )
    if screen is None:
        # No code screen: nothing happens at all, the run continues.
        return result

    result["detected"] = True

    # 1. The attempts guard: when too few tries are left, nothing is typed.
    left = screen.attempts
    if left is not None and left < min_attempts_left:
        result["reason"] = "мало попыток ввода: %d" % left
        return result

    # 2. The limit of entries per run: never type a second code.
    if attempts_used >= max_attempts:
        result["reason"] = "лимит ввода кода в этом запуске исчерпан (%d)" % max_attempts
        return result

    # 3. The code itself.
    if code:
        found, source = str(code), "argument"
    else:
        try:
            found, source = read_code(path=code_path, profile_path=profile_path, env=env)
        except CodeFileError as exc:
            result["reason"] = str(exc)
            result["fatal"] = True
            result["code_path"] = _first_existing_path(code_path, profile_path)
            return result
        if not found:
            where = _first_existing_path(code_path, profile_path)
            result["reason"] = (
                "код не найден: %s (или переменная %s)" % (where or "--code-file", ENV_CODE)
            )
            result["fatal"] = True
            result["code_path"] = where
            return result

    result["source"] = source

    # 4. Type it — once.
    outcome = enter_code(page, found, screen, timeout_ms=timeout_ms)
    result["entered"] = outcome["entered"]
    result["reason"] = outcome["reason"]
    return result


def _first_existing_path(*paths):
    """The first path of *paths* that is set (expanded), or ``None``."""
    for path in paths:
        if path:
            return os.path.expanduser(str(path))
    return None
