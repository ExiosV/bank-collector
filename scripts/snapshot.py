#!/usr/bin/env python3
"""Save a sanitised snapshot of your cabinet page, for writing CSS selectors.

Usage::

    python scripts/snapshot.py --bank example
    python scripts/snapshot.py --bank example --out data/snapshots
    python scripts/snapshot.py --url "https://..." --wait-for ".dashboard"

The script attaches to the browser you already started with
``--remote-debugging-port`` and that you already logged into. It never logs
in, never asks for credentials and never clicks anything.

The written snapshot is sanitised: scripts, style blocks, input values,
e-mail addresses, credentials and long digit runs (card numbers) are removed
or redacted. Review it before sharing it anywhere.
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bank_collector.connect import DEFAULT_CDP_URL, BrowserUnavailable, connect  # noqa: E402
from bank_collector.profiles import ProfileError, load_profile  # noqa: E402
from bank_collector.sanitize import sanitize_html, sanitize_text  # noqa: E402


def build_parser():
    parser = argparse.ArgumentParser(
        prog="snapshot.py",
        description="Dump a sanitised HTML/text snapshot of a bank cabinet page (read-only).",
    )
    parser.add_argument("--bank", metavar="NAME", help="profile whose URL should be opened")
    parser.add_argument("--url", metavar="URL", help="explicit URL to open (overrides the profile)")
    parser.add_argument("--cdp-url", default=DEFAULT_CDP_URL, help="CDP endpoint (default: %(default)s)")
    parser.add_argument("--out", default="data/snapshots", help="output directory (default: %(default)s)")
    parser.add_argument("--wait-for", metavar="CSS", help="CSS selector to wait for before dumping")
    parser.add_argument("--timeout", type=int, default=20000, help="wait timeout in ms (default: %(default)s)")
    parser.add_argument(
        "--name", metavar="NAME", help="base name for the written files (default: <bank> or 'page')"
    )
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)

    url = args.url
    wait_for = args.wait_for
    base_name = args.name

    if url is None:
        if not args.bank:
            sys.stderr.write("error: either --bank or --url is required\n")
            return 3
        try:
            profile = load_profile(args.bank)
        except ProfileError as exc:
            sys.stderr.write("profile error: %s\n" % exc)
            return 3
        url = profile["url"]
        wait_for = wait_for or profile.get("wait_for")
        base_name = base_name or profile.get("bank") or args.bank

    base_name = base_name or "page"
    os.makedirs(args.out, exist_ok=True)

    try:
        playwright, browser = connect(args.cdp_url, timeout_ms=args.timeout)
    except BrowserUnavailable as exc:
        sys.stderr.write("browser error: %s\n" % exc)
        return 2

    try:
        contexts = browser.contexts
        if not contexts:
            sys.stderr.write("browser error: the attached browser has no open context.\n")
            return 2
        context = contexts[0]
        pages = list(context.pages)
        page = pages[0] if pages else context.new_page()

        page.goto(url)
        if wait_for:
            try:
                page.wait_for_selector(wait_for, timeout=args.timeout)
            except Exception as exc:
                sys.stderr.write("warning: selector %r did not appear (%s)\n" % (wait_for, exc))

        stamp = time.strftime("%Y%m%d-%H%M%S")
        html_path = os.path.join(args.out, "%s-%s.html" % (base_name, stamp))
        text_path = os.path.join(args.out, "%s-%s.txt" % (base_name, stamp))

        with open(html_path, "w", encoding="utf-8") as handle:
            handle.write(sanitize_html(page.content()))
        try:
            body_text = page.inner_text("body")
        except Exception:
            body_text = ""
        with open(text_path, "w", encoding="utf-8") as handle:
            handle.write(sanitize_text(body_text))

        sys.stdout.write("url: %s\n" % url)
        sys.stdout.write("html: %s\n" % html_path)
        sys.stdout.write("text: %s\n" % text_path)
        sys.stdout.write(
            "now open the .html file and look for stable selectors "
            "(data-*, id, class) before filling in the profile.\n"
        )
        return 0
    finally:
        try:
            playwright.stop()
        except Exception:  # pragma: no cover - defensive
            pass


if __name__ == "__main__":
    sys.exit(main())
