"""Attaching to a browser you already opened and logged into.

Rules of this module:

* it NEVER opens a login page, never types credentials and never asks for
  them — there is no code path that could;
* it NEVER calls ``browser.close()`` or ``context.close()`` — the browser you
  started stays open and yours;
* the only network traffic is to the local CDP endpoint (or to a relay URL
  you pass explicitly with ``--cdp-url``).
"""

__all__ = [
    "DEFAULT_CDP_URL",
    "BrowserUnavailable",
    "connect",
    "available_browsers",
]

DEFAULT_CDP_URL = "http://127.0.0.1:9222"

_HINT = (
    "Start the browser yourself with the debugging port enabled, e.g.\n"
    "  google-chrome --remote-debugging-port=9222\n"
    "  chromium --remote-debugging-port=9222\n"
    "Log in to your bank by hand in that window, then re-run the command.\n"
    "bank-collector never logs in and never asks for a password or a code."
)


class BrowserUnavailable(Exception):
    """Raised when no browser could be attached to."""


def _import_playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise BrowserUnavailable(
            "The 'playwright' package is not installed.\n"
            "Install it with:  pip install playwright && python -m playwright install chromium\n"
            "(Playwright is only needed to talk to your browser; the parsing part is pure Python.)"
        ) from exc
    return sync_playwright


def available_browsers(playwright):
    """Return the names of the browser types Playwright can drive."""
    try:
        return [browser_type.name for browser_type in (playwright.chromium, playwright.firefox, playwright.webkit)]
    except Exception:  # pragma: no cover - defensive
        return []


def connect(cdp_url=DEFAULT_CDP_URL, timeout_ms=15000):
    """Attach over CDP to an already running browser.

    Returns ``(playwright, browser)``. The caller is responsible for
    ``playwright.stop()``; the browser itself is deliberately left open.
    """
    sync_playwright = _import_playwright()
    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.connect_over_cdp(cdp_url, timeout=timeout_ms)
    except Exception as exc:
        try:
            playwright.stop()
        except Exception:  # pragma: no cover - defensive
            pass
        raise BrowserUnavailable(
            "Could not attach to a browser at %s.\n%s\n\nOriginal error: %s" % (cdp_url, _HINT, exc)
        ) from exc
    return playwright, browser
