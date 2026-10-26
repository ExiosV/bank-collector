"""Attaching to a browser you already opened and logged into.

Rules of this module:

* it NEVER opens a login page, never types credentials and never asks for
  them — there is no code path that could;
* it NEVER calls ``browser.close()`` or ``context.close()`` — the browser you
  started stays open and yours;
* the only network traffic is to the local CDP endpoint (or to a relay URL
  you pass explicitly with ``--cdp-url``).

The page may also be *scrolled* (see ``scroll_by``): scrolling moves the
viewport and changes nothing — no DOM mutation, no click, no form submit and
no request of our own. It is used to read the rows a cabinet appends while
the list is scrolled.
"""

__all__ = [
    "DEFAULT_CDP_URL",
    "BrowserUnavailable",
    "PageAdapter",
    "FrameAdapter",
    "ElementAdapter",
    "connect",
    "available_browsers",
]

DEFAULT_CDP_URL = "http://127.0.0.1:9222"

# Navigation is the only slow part: an SPA cabinet can take a while to answer.
GOTO_TIMEOUT_MS = 60000

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


class ElementAdapter(object):
    """Adapt a Playwright element handle to the protocol of ``collect``.

    ``fill`` and ``click`` are the ONLY write primitives of the whole
    package.  They are used by one single place — ``bank_collector.unlock``,
    which types the cabinet's static code on the screen it has recognised —
    and by nothing else: the collector itself never touches them.
    """

    def __init__(self, handle):
        self._handle = handle

    def query_all(self, selector):
        return [ElementAdapter(child) for child in self._handle.query_selector_all(selector)]

    def text_content(self):
        return self._handle.text_content()

    def get_attribute(self, name):
        return self._handle.get_attribute(name)

    # -- the only two writes of the package --------------------------------
    def fill(self, value):
        """Type *value* into the element (used only by ``unlock``)."""
        return self._handle.fill(value)

    def click(self):
        """Click the element (used only by ``unlock``)."""
        return self._handle.click()


class FrameAdapter(object):
    """Adapt a Playwright frame to the read protocol of ``collect``.

    Some banks render the code screen inside an iframe.  A frame is read
    exactly like a page: the same selectors, the same element adapter.
    """

    def __init__(self, frame):
        self._frame = frame

    @property
    def url(self):
        return self._frame.url

    def query_all(self, selector):
        return [ElementAdapter(handle) for handle in self._frame.query_selector_all(selector)]

    def text_content(self):
        try:
            return self._frame.inner_text("body")
        except Exception:  # pragma: no cover - defensive
            return ""

    def scroll_by(self, dy):
        """Scroll the page the frame lives in by *dy* pixels.

        Scrolling is read-only: it moves the viewport, it clicks nothing and
        it sends no request of our own.  A frame without a page (it cannot
        scroll on its own) is silently ignored.
        """
        page = getattr(self._frame, "page", None)
        if page is None:
            return None
        return page.mouse.wheel(0, int(dy))


class PageAdapter(object):
    """Adapt a Playwright page to the protocol of ``collect``.

    The driver only needs ``goto``, ``wait_for_selector``, ``query_all`` and
    the two snapshot helpers; wrapping the page here keeps the driver free of
    any Playwright import, so it stays testable without a browser.
    """

    def __init__(self, page, timeout_ms=GOTO_TIMEOUT_MS):
        self._page = page
        self._timeout_ms = timeout_ms

    # -- what the driver uses -------------------------------------------
    @property
    def url(self):
        return self._page.url

    def goto(self, url):
        return self._page.goto(url, wait_until="domcontentloaded", timeout=self._timeout_ms)

    def wait_for_selector(self, selector, timeout=None):
        return self._page.wait_for_selector(selector, timeout=timeout or self._timeout_ms)

    def query_all(self, selector):
        return [ElementAdapter(handle) for handle in self._page.query_selector_all(selector)]

    def scroll_by(self, dy):
        """Scroll the viewport by *dy* pixels.

        Scrolling is read-only: it moves the viewport, it clicks nothing, it
        mutates nothing and it sends no request of our own.  It is the way
        the driver reads the rows a cabinet appends while the list is
        scrolled.
        """
        return self._page.mouse.wheel(0, int(dy))

    @property
    def frames(self):
        """The child frames of the page (the code screen may live in one)."""
        try:
            frames = self._page.frames
        except Exception:  # pragma: no cover - defensive
            return []
        return [FrameAdapter(frame) for frame in frames or []]

    # -- snapshots -------------------------------------------------------
    def content(self):
        return self._page.content()

    def inner_text(self, selector="body"):
        return self._page.inner_text(selector)
