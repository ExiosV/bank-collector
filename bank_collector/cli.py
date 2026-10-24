"""Command line interface.

Exit codes
----------
0   everything went fine
2   no browser could be attached (browser not started / no debugging port)
3   the profile is missing or invalid
4   the page was read but nothing could be extracted (warning is printed)

The tool never asks for a password, an SMS code or a cookie, and it never
logs in. It only reads the page that is already open in *your* browser.
"""

import argparse
import json
import os
import sys
import time

from . import __version__, report as report_mod
from .collect import DEFAULT_TIMEOUT_MS, collect
from .connect import DEFAULT_CDP_URL, BrowserUnavailable, PageAdapter, connect
from .profiles import ProfileError, list_profiles, load_profile
from .sanitize import sanitize_html, sanitize_text

__all__ = ["main", "build_parser"]

EXIT_OK = 0
EXIT_NO_BROWSER = 2
EXIT_BAD_PROFILE = 3
EXIT_NOTHING = 4


def build_parser():
    parser = argparse.ArgumentParser(
        prog="bank-collector",
        description=(
            "Read your own bank cabinet in a browser you already logged into. "
            "Read-only: no login, no credentials, no clicking on anything."
        ),
        epilog=(
            "Examples:\n"
            "  bank-collector --bank example --cdp-url http://127.0.0.1:9222\n"
            "  bank-collector --bank example --out data --csv\n"
            "  bank-collector --list-profiles\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--bank", metavar="NAME", help="bank profile to use (see --list-profiles)")
    parser.add_argument(
        "--cdp-url",
        default=DEFAULT_CDP_URL,
        metavar="URL",
        help=(
            "CDP endpoint of the browser you started yourself "
            "(default: %(default)s). A playwriter relay URL is accepted too."
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


def _write_outputs(report, out_dir, formats, bank):
    os.makedirs(out_dir, exist_ok=True)
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
    return written


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


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list_profiles:
        _print_profiles()
        return EXIT_OK

    if not args.bank:
        parser.error("--bank is required (or use --list-profiles)")

    try:
        profile = load_profile(args.bank)
    except ProfileError as exc:
        sys.stderr.write("profile error: %s\n" % exc)
        return EXIT_BAD_PROFILE

    try:
        playwright, browser = connect(args.cdp_url, timeout_ms=args.timeout)
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

        collected = collect(page, profile, timeout_ms=args.timeout)
        report = report_mod.build_report(collected)

        if args.snapshot:
            paths, error = _snapshot(page, args.out, profile.get("bank") or args.bank)
            if error:
                sys.stderr.write("warning: %s\n" % error)
            for path in paths:
                sys.stdout.write("snapshot: %s\n" % path)

        written = _write_outputs(report, args.out, args.format, profile.get("bank") or args.bank)
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

        if not report["balances"] and not report["transactions"]:
            sys.stderr.write(
                "nothing was extracted — check the selectors in the profile "
                "(see docs/ADD_A_BANK.md) and try --snapshot\n"
            )
            return EXIT_NOTHING
        return EXIT_OK
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
