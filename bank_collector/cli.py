"""Command line interface.

Exit codes
----------
0   everything went fine
2   no browser could be attached (browser not started / no debugging port)
3   the profile is missing or invalid, or the code file of ``--enter-code``
    is missing or readable by other users
4   the page was read but nothing could be extracted (warning is printed), or
    an imported statement export holds no operations at all
5   the history is incomplete: the scroll or the numbered pages did not
    reach ``stop_before_date`` (or hit ``max_rounds``/``max_pages``), or a
    fuller snapshot on disk was kept and the new report went to
    ``<bank>.partial.json`` — not a success either way

``--import-statement PATH`` reads a statement the owner downloaded by hand
from the cabinet. It needs ``--bank`` and no browser: the file is read from
the local disk only, and the report is written to
``<out>/<bank>-statement.json`` (never ``<bank>.json``); with ``--format csv``
or ``both`` the transactions also go to
``<out>/<bank>-statement-transactions.csv``. Exit code 3 means an unknown
bank, an unreadable file, a file that is not a UTF-8 CSV export (cp1251,
PDF/Excel), a missing column or rows of which none could be read; 4
means the export is empty.

The tool never asks for a password, an SMS code or a cookie, and it never
logs in. It only reads the page that is already open in *your* browser.

The single exception is ``--enter-code``: it is off by default and answers
the cabinet's static "secret code" screen (not an SMS code) — the permanent
cabinet code, which is read from a file of mode 0600 outside the repository
and is never printed, logged or passed on the command line. Even then the
tool types at most one code per run and refuses to type at all when the
screen says that too few attempts are left. A failed entry leaves a marker
file next to the code; the next run refuses to type anything until the
owner removes it with ``--clear-code-failure``.
"""

import argparse
import copy
import json
import os
import sys
import time

from . import __version__, report as report_mod
from .collect import DEFAULT_TIMEOUT_MS, collect
from .connect import (
    CDP_URL_ENV,
    DEFAULT_CDP_URL,
    BrowserUnavailable,
    PageAdapter,
    connect,
    resolve_cdp_url,
)

from .profiles import ProfileError, list_profiles, load_profile
from .sanitize import sanitize_html, sanitize_text
from .unlock import (
    DEFAULT_UNLOCK_TIMEOUT_MS,
    attempt_marker_path,
    clear_attempt_failure,
    handle_code_screen,
)

__all__ = ["main", "build_parser"]

EXIT_OK = 0
EXIT_NO_BROWSER = 2
EXIT_BAD_PROFILE = 3
EXIT_NOTHING = 4
EXIT_INCOMPLETE = 5

#: The line that is printed on stdout when the code screen was handled.
UNLOCK_OK = "UNLOCK: экран «Секретный код» — код введён"
UNLOCK_FAILED = "UNLOCK: не удалось (%s)"

#: The lines of ``--clear-code-failure``.
UNLOCK_MARKER_CLEARED = "UNLOCK: маркер неудачной попытки снят (%s)"
UNLOCK_NO_MARKER = "UNLOCK: маркера нет (%s)"


def build_parser():
    parser = argparse.ArgumentParser(
        prog="bank-collector",
        description=(
            "Read your own bank cabinet in a browser you already logged into. "
            "Read-only: no login and no credentials; the only fill is --enter-code's "
            "answer to the cabinet's static 'secret code' screen (not an SMS code), "
            "from a file of mode 0600. There are exactly three narrow, explicitly "
            "enabled clicks: the history list's own 'load more' button — a profile "
            "may name it (scroll.load_more_text) together with the list container it "
            "lives in (scroll.load_more_scope), and it is pressed only when scrolling "
            "added no rows and exactly one such <button> is inside that container; "
            "the history's own 'next page' arrow (pages.next_selector) of a list cut "
            "into numbered pages — it is the only element clicked in that mode and "
            "only when it resolves to exactly one element; and --enter-code's one "
            "press of the screen's submit button."
        ),
        epilog=(
            "Examples:\n"
            "  bank-collector --bank example --cdp-url http://127.0.0.1:9222\n"
            "  bank-collector --bank example --out data --csv\n"
            "  bank-collector --bank alfa --enter-code --code-file ~/.config/bank-collector/alfa-code\n"
            "  bank-collector --bank alfa --import-statement ~/Downloads/statement.csv\n"
            "  bank-collector --list-profiles\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--bank", metavar="NAME", help="bank profile to use (see --list-profiles)")
    parser.add_argument(
        "--cdp-url",
        default=None,
        metavar="URL",
        help=(
            "CDP endpoint of the browser you started yourself (default: $%s, "
            "else %s). A playwriter relay URL is accepted too; prefer the "
            "environment variable when the URL carries a token, so it does not "
            "show up in argv or in shell history." % (CDP_URL_ENV, DEFAULT_CDP_URL)
        ),
    )
    parser.add_argument(
        "--enter-code",
        action="store_true",
        help=(
            "answer the cabinet's static 'secret code' screen once, if it shows up "
            "(off by default; the code is read from a file of mode 0600)"
        ),
    )
    parser.add_argument(
        "--code-file",
        metavar="PATH",
        default=None,
        help=(
            "file with the cabinet's static code (mode 0600, outside the repository). "
            "Without --enter-code it is ignored (a warning is printed)."
        ),
    )
    parser.add_argument(
        "--clear-code-failure",
        action="store_true",
        help=(
            "remove the marker of a failed code entry (the file next to the code file), "
            "so that the next run may try again; needs no browser and types nothing"
        ),
    )
    parser.add_argument(
        "--import-statement",
        metavar="PATH",
        default=None,
        help=(
            "import a statement CSV you downloaded by hand from the cabinet "
            "(needs --bank, no browser); the report goes to "
            "<out>/<bank>-statement.json"
        ),
    )
    parser.add_argument(
        "--out",
        default="data",
        metavar="DIR",
        help="directory for the result files (default: %(default)s, it is git-ignored)",
    )
    parser.add_argument(
        "--snapshot",
        action="store_true",
        help="also dump a sanitised HTML/text snapshot of the page (for writing selectors)",
    )
    parser.add_argument(
        "--no-scroll",
        action="store_true",
        help=(
            "do not scroll the rendered list, even when the profile asks for it "
            "(the first rendered screen only; does not stop a pages walk; "
            "remove the pages block from the profile to read only the first page)"
        ),
    )
    parser.add_argument(
        "--format",
        choices=("json", "csv", "both"),
        default="both",
        help="output format of the report files (default: %(default)s)",
    )
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="print the report to stdout as JSON",
    )
    parser.add_argument(
        "--csv",
        dest="as_csv",
        action="store_true",
        help="print the transactions to stdout as CSV",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_MS,
        metavar="MS",
        help="how long to wait for the cabinet to render (default: %(default)s)",
    )
    parser.add_argument(
        "--list-profiles",
        action="store_true",
        help="list the available bank profiles and exit",
    )
    parser.add_argument("--version", action="version", version="bank-collector %s" % __version__)
    return parser


def _print_profiles(stream=sys.stdout):
    profiles = list_profiles()
    if not profiles:
        stream.write("no profiles found\n")
        return
    stream.write("%-16s %-12s %s\n" % ("PROFILE", "STATUS", "BANK"))
    for profile in profiles:
        stream.write("%-16s %-12s %s\n" % (profile["name"], profile.get("status") or "?", profile.get("bank")))


def _incomplete(report):
    """True when the report read a list (scroll or pages) but not all of it."""
    scroll = report.get("scroll")
    return bool(scroll) and not scroll.get("complete")


def _would_lose_history(report, path):
    """True when writing *report* to *path* would replace a fuller snapshot.

    A run that is not proven complete must not overwrite yesterday's longer
    history: the daily job would trade a year for a screen. That covers a
    list that stopped short (a scroll or a numbered-page walk), a
    ``--no-scroll`` run over a file that was scrolled, and a run without
    transactions. A complete run always wins,
    and a bank without scrolling (its count of rows changes every day) is
    never held back.
    """
    if (report.get("scroll") or {}).get("complete"):
        return False
    try:
        with open(path, "r", encoding="utf-8") as handle:
            previous = json.load(handle)
    except (OSError, ValueError):
        return False
    if not isinstance(previous, dict) or not isinstance(previous.get("transactions"), list):
        return False
    if len(previous["transactions"]) <= len(report.get("transactions") or []):
        return False
    # A run without transactions never replaces a history that has some,
    # even when it read the balances.
    nothing = not report.get("transactions")
    return _incomplete(report) or bool(previous.get("scroll")) or nothing


def _write_outputs(report, out_dir, formats, bank):
    """Write the report; return ``(paths, kept)``.

    *kept* is the path of a fuller snapshot that was left in place: the new
    report then goes to ``<bank>.partial.json`` and nothing else is written.
    """
    os.makedirs(out_dir, exist_ok=True)
    if formats in ("json", "both"):
        path = os.path.join(out_dir, "%s.json" % bank)
        if _would_lose_history(report, path):
            partial = os.path.join(out_dir, "%s.partial.json" % bank)
            report_mod.write_json(report, partial)
            return [partial], path
    written = []
    if formats in ("json", "both"):
        path = os.path.join(out_dir, "%s.json" % bank)
        report_mod.write_json(report, path)
        written.append(path)
    if formats in ("csv", "both"):
        path = os.path.join(out_dir, "%s-balances.csv" % bank)
        report_mod.write_csv(report["balances"], path, columns=["amount", "amount_currency"])
        written.append(path)
        path = os.path.join(out_dir, "%s-transactions.csv" % bank)
        report_mod.write_csv(report["transactions"], path)
        written.append(path)
    return written, None


def _snapshot(page, out_dir, bank):
    os.makedirs(out_dir, exist_ok=True)
    try:
        html = page.content()
    except Exception as exc:
        return [], "cannot read the page HTML: %s" % exc
    try:
        text = page.inner_text("body")
    except Exception:
        text = ""

    stamp = time.strftime("%Y%m%d-%H%M%S")
    html_path = os.path.join(out_dir, "snapshot-%s-%s.html" % (bank, stamp))
    text_path = os.path.join(out_dir, "snapshot-%s-%s.txt" % (bank, stamp))
    with open(html_path, "w", encoding="utf-8") as handle:
        handle.write(sanitize_html(html))
    with open(text_path, "w", encoding="utf-8") as handle:
        handle.write(sanitize_text(text))
    return [html_path, text_path], None


def _same_url(left, right):
    """Compare two URLs ignoring a trailing slash."""
    if not left or not right:
        return left == right
    return left.rstrip("/") == right.rstrip("/")


def _without_scroll(profile):
    """Return a copy of *profile* whose records have no ``scroll`` block.

    It powers ``--no-scroll``: the run reads the first rendered screen only,
    exactly as it did before the profiles learned to scroll.
    """
    stripped = copy.deepcopy(profile)
    for section in ("balances", "transactions"):
        for record in stripped.get(section) or []:
            if isinstance(record, dict):
                record.pop("scroll", None)
    return stripped


class _AlreadyOpenPage(object):
    """A page proxy whose ``goto`` of the already open URL does nothing.

    ``collect()`` opens the cabinet itself.  After the unlock the cabinet is
    already there, and a second ``goto`` would reload the page — which would
    throw the freshly unlocked session away.  This proxy swallows exactly
    that one call and passes everything else through.
    """

    def __init__(self, page, url):
        self._page = page
        self._url = url

    @property
    def url(self):
        return getattr(self._page, "url", self._url)

    def goto(self, url, *args, **kwargs):
        if _same_url(url, self._url):
            return None
        return self._page.goto(url, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._page, name)


def _unlock(page, profile, args):
    """Handle the cabinet's static-code screen, if it shows up.

    Returns ``None`` when the run may continue, or the exit code when it
    must stop (the code file is missing or readable by other users).  When
    no code screen is detected nothing is printed at all.
    """
    config = profile.get("unlock") or {}
    url = profile.get("url")
    try:
        page.goto(url)
    except Exception as exc:
        sys.stderr.write("cannot open %s: %s\n" % (url, exc))
        return EXIT_NO_BROWSER

    result = handle_code_screen(
        page,
        config=config,
        code_path=args.code_file,
        timeout_ms=min(args.timeout, DEFAULT_UNLOCK_TIMEOUT_MS),
        ready_selector=profile.get("wait_for"),
    )

    if not result["detected"]:
        # No code screen: the run continues exactly as it would without the
        # flag, and nothing is printed.
        return None

    if result["entered"]:
        sys.stdout.write("%s\n" % UNLOCK_OK)
    else:
        sys.stdout.write("%s\n" % (UNLOCK_FAILED % result["reason"]))
    sys.stdout.flush()

    if result["fatal"]:
        sys.stderr.write("unlock error: %s\n" % result["reason"])
        return EXIT_BAD_PROFILE
    return None


def _marker_path_for(args):
    """The "one strike" marker that belongs to the code file of this run."""
    code_file = args.code_file
    if not code_file and args.bank:
        try:
            profile = load_profile(args.bank)
        except ProfileError:
            profile = None
        if profile:
            code_file = (profile.get("unlock") or {}).get("code_file")
    return attempt_marker_path(code_file)


def _clear_code_failure(args):
    """Remove the marker of a failed code entry.

    Nothing is typed and no browser is needed: the marker file is removed
    and one line is printed.
    """
    marker = _marker_path_for(args)
    try:
        removed = clear_attempt_failure(marker)
    except OSError as exc:
        sys.stderr.write("unlock error: cannot remove the marker %s: %s\n" % (marker, exc))
        return EXIT_BAD_PROFILE
    if removed:
        sys.stdout.write("%s\n" % (UNLOCK_MARKER_CLEARED % marker))
    else:
        sys.stdout.write("%s\n" % (UNLOCK_NO_MARKER % marker))
    return EXIT_OK


def _exit_code(report, written, kept, stream=None):
    """The exit code of a finished run, with the line that explains it.

    The daily job trusts only this code: anything but 0 is retried and then
    reported, and only a 0 is uploaded. The order matters — an empty run is
    ``EXIT_NOTHING`` even when a fuller file was kept.
    """
    stream = stream or sys.stderr
    if kept:
        stream.write(
            "the snapshot already in %s holds a longer history than this run "
            "(%d transactions): it was kept, the new report is in %s\n"
            % (kept, len(report["transactions"]), written[0])
        )
    if not report["balances"] and not report["transactions"]:
        stream.write(
            "nothing was extracted — check the selectors in the profile "
            "(see docs/ADD_A_BANK.md) and try --snapshot\n"
        )
        return EXIT_NOTHING
    if kept:
        return EXIT_INCOMPLETE
    if _incomplete(report):
        scroll = report["scroll"]
        stream.write(
            "history incomplete: it reaches back only to %s (scroll stop: %s); "
            "the report was written, but the run is not a success\n"
            % (scroll.get("oldest_date") or "no dated row", scroll.get("stop"))
        )
        return EXIT_INCOMPLETE
    return EXIT_OK


def _import_statement(args):
    """Import a hand-downloaded statement: no browser, no CDP, local file only.

    The report goes to ``<out>/<bank>-statement.json`` — never to
    ``<bank>.json``, so a statement can never be mistaken for a cabinet run
    (and never replaces the fuller snapshot protection of one).  With
    ``--format csv`` or ``both`` the transactions are also written to
    ``<bank>-statement-transactions.csv``; the JSON is written for ``json``
    and ``both`` (the default).
    """
    # Imported here so that the browser path stays free of the CSV parser.
    from .statement import (
        WARNING_SAMPLE,
        EmptyStatementError,
        StatementError,
        read_statement,
    )

    try:
        collected = read_statement(args.bank, args.import_statement)
    except EmptyStatementError as exc:
        sys.stderr.write("statement error: %s\n" % exc)
        return EXIT_NOTHING
    except StatementError as exc:
        sys.stderr.write("statement error: %s\n" % exc)
        # When rows were present but none could be read, say which lines and
        # columns failed — without the values themselves.
        for warning in getattr(exc, "warnings", [])[:WARNING_SAMPLE]:
            sys.stderr.write("warning: %s\n" % warning)
        return EXIT_BAD_PROFILE

    report = report_mod.build_report(collected)
    report["statement"] = collected["statement"]

    os.makedirs(args.out, exist_ok=True)
    written = []
    if args.format in ("json", "both"):
        path = os.path.join(args.out, "%s-statement.json" % args.bank)
        report_mod.write_json(report, path)
        written.append(path)
    if args.format in ("csv", "both"):
        path = os.path.join(args.out, "%s-statement-transactions.csv" % args.bank)
        report_mod.write_csv(report["transactions"], path)
        written.append(path)
    for path in written:
        sys.stdout.write("written: %s\n" % path)

    statement = report["statement"]
    period = statement["period"]
    sys.stdout.write(
        "statement: %d operations, %s .. %s\n"
        % (statement["rows"], period[0], period[1])
    )
    for warning in report["warnings"]:
        sys.stderr.write("warning: %s\n" % warning)
    return EXIT_OK


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cdp_url is not None and not args.cdp_url.strip():
        # An empty --cdp-url "$CDP" in a script must not fall through silently.
        parser.error("--cdp-url is empty")

    if args.list_profiles:
        _print_profiles()
        return EXIT_OK

    if args.clear_code_failure:
        # Works without a browser, without a bank and without typing anything.
        return _clear_code_failure(args)

    if args.import_statement:
        # A statement import is a local file read: a browser cannot help it.
        if not args.bank:
            parser.error("--bank is required with --import-statement")
        if args.enter_code:
            parser.error("--enter-code cannot be used with --import-statement")
        if args.cdp_url is not None:
            parser.error("--cdp-url cannot be used with --import-statement")
        if args.as_json or args.as_csv:
            parser.error("--json/--csv cannot be used with --import-statement")
        return _import_statement(args)

    if not args.bank:
        parser.error("--bank is required (or use --list-profiles)")

    if args.code_file and not args.enter_code:
        sys.stderr.write("warning: --code-file is ignored without --enter-code\n")

    try:
        profile = load_profile(args.bank)
    except ProfileError as exc:
        sys.stderr.write("profile error: %s\n" % exc)
        return EXIT_BAD_PROFILE

    if args.no_scroll:
        # The defensive switch: read the first rendered screen only.
        profile = _without_scroll(profile)

    try:
        playwright, browser = connect(resolve_cdp_url(args.cdp_url), timeout_ms=args.timeout)
    except BrowserUnavailable as exc:
        sys.stderr.write("browser error: %s\n" % exc)
        return EXIT_NO_BROWSER

    own_page = None
    try:
        contexts = browser.contexts
        if not contexts:
            sys.stderr.write("browser error: the attached browser has no open context.\n")
            return EXIT_NO_BROWSER
        context = contexts[0]

        # Prefer a tab of our own: the tabs the person already has open are
        # theirs, and navigating one of them would throw away their page state.
        try:
            own_page = context.new_page()
        except Exception:
            own_page = None
        raw_page = own_page or (list(context.pages) or [None])[0]
        if raw_page is None:
            sys.stderr.write("browser error: the attached browser has no tab to read.\n")
            return EXIT_NO_BROWSER
        page = PageAdapter(raw_page, timeout_ms=args.timeout)

        collect_target = page
        if args.enter_code:
            fatal = _unlock(page, profile, args)
            if fatal is not None:
                return fatal
            collect_target = _AlreadyOpenPage(page, profile.get("url"))

        collected = collect(collect_target, profile, timeout_ms=args.timeout)
        report = report_mod.build_report(collected)

        if args.snapshot:
            paths, error = _snapshot(page, args.out, profile.get("bank") or args.bank)
            if error:
                sys.stderr.write("warning: %s\n" % error)
            for path in paths:
                sys.stdout.write("snapshot: %s\n" % path)

        written, kept = _write_outputs(report, args.out, args.format, profile.get("bank") or args.bank)
        for path in written:
            sys.stdout.write("written: %s\n" % path)

        if args.as_json:
            json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
            sys.stdout.write("\n")
        if args.as_csv:
            import csv as csv_mod

            writer = csv_mod.writer(sys.stdout)
            writer.writerow(["date", "amount", "amount_currency", "raw"])
            for row in report["transactions"]:
                fields = row if isinstance(row, dict) else {}
                amount = fields.get("amount") or {}
                writer.writerow(
                    [
                        fields.get("date", {}).get("value", "") if isinstance(fields.get("date"), dict) else "",
                        amount.get("value", "") if isinstance(amount, dict) else "",
                        amount.get("currency", "") if isinstance(amount, dict) else "",
                        amount.get("raw", "") if isinstance(amount, dict) else "",
                    ]
                )

        for warning in report["warnings"]:
            sys.stderr.write("warning: %s\n" % warning)

        return _exit_code(report, written, kept)
    finally:
        # Close the tab we opened ourselves, and never the person's browser.
        if own_page is not None:
            try:
                own_page.close()
            except Exception:  # pragma: no cover - defensive
                pass
        try:
            playwright.stop()
        except Exception:  # pragma: no cover - defensive
            pass


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
