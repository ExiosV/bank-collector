"""The `signed_out` profile block: "nobody is logged in" is not "selectors broke".

An expired cabinet session shows its sign-in screen. A run that reads that
page gets empty lists and a misleading "nothing was extracted", and a
scheduler cannot tell it from a broken profile. The block names the markers of
that screen, the run stops at once (exit code 6, one `SIGNED_OUT: <bank>`
line), and `--check-session` can ask just that question without reading a
single record.
"""

import copy
import io
import json
import os
import sys
from datetime import datetime, timezone

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from bank_collector import cli  # noqa: E402
import bank_collector.collect as collect_mod  # noqa: E402
from bank_collector.collect import SIGNED_OUT_WARNING, check_session, collect  # noqa: E402
from bank_collector.profiles import ProfileError, load_profile  # noqa: E402
from fake_page import FakePage  # noqa: E402

FIXTURES = os.path.join(HERE, "fixtures")

HOME_URL = "https://online.raiffeisen.ru/"
HISTORY_URL = "https://online.raiffeisen.ru/history/common"
LOGIN_URL = "https://online.raiffeisen.ru/login/main"

LOGIN_HTML = (
    "<html><body><div class='login-screen'>"
    "<button>Войти в онлайн-банк</button>"
    "</div></body></html>"
)

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)


def _read_fixture(name):
    with open(os.path.join(FIXTURES, name), "r", encoding="utf-8") as handle:
        return handle.read()


def _key(url):
    return (url or "").rstrip("/")


class RoutedPage(FakePage):
    """A page that serves a different document (and URL) per requested URL."""

    def __init__(self, routes, url):
        self.routes = {_key(known): value for known, value in routes.items()}
        final, html = self.routes.get(_key(url), (url, ""))
        FakePage.__init__(self, html, url=final)
        #: True once the CLI closed the tab it opened.
        self.closed = False

    def goto(self, url, **kwargs):
        self.visited.append(url)
        final, html = self.routes.get(_key(url), (url, self.html))
        self.url = final
        self._set_root(html)
        return None

    def close(self):
        self.closed = True


# ---------------------------------------------------------------------------
# Profile validation
# ---------------------------------------------------------------------------

_PREFIX = (
    "bank: markers\n"
    "status: unverified\n"
    'url: "https://x.invalid/"\n'
    'wait_for: "#x"\n'
    "balances: []\n"
    "transactions: []\n"
)


def _load(tmp_path, name, block):
    path = tmp_path / ("%s.yaml" % name)
    path.write_text(_PREFIX + block, encoding="utf-8")
    return load_profile(name, directory=str(tmp_path))


def test_signed_out_block_is_normalised(tmp_path):
    profile = _load(
        tmp_path,
        "markers",
        'signed_out:\n  url_contains: ["/login"]\n  text: ["Войти в онлайн-банк"]\n',
    )

    assert profile["signed_out"] == {
        "url_contains": ["/login"],
        "text": ["Войти в онлайн-банк"],
    }


def test_a_plain_string_is_a_one_element_list(tmp_path):
    profile = _load(tmp_path, "plain", 'signed_out:\n  text: "Войти"\n')

    assert profile["signed_out"] == {"url_contains": [], "text": ["Войти"]}


def test_one_of_the_two_lists_is_enough(tmp_path):
    profile = _load(tmp_path, "only_url", 'signed_out:\n  url_contains: ["/login"]\n')

    assert profile["signed_out"]["text"] == []


def test_an_empty_list_for_the_other_key_is_accepted(tmp_path):
    """One non-empty marker is enough; the other list may say "none"."""
    profile = _load(
        tmp_path,
        "one_empty",
        'signed_out:\n  url_contains: ["/login"]\n  text: []\n',
    )

    assert profile["signed_out"] == {"url_contains": ["/login"], "text": []}


def test_an_empty_signed_out_block_is_rejected(tmp_path):
    with pytest.raises(ProfileError, match="at least one of"):
        _load(tmp_path, "empty", "signed_out: {}\n")


def test_an_empty_marker_is_rejected(tmp_path):
    with pytest.raises(ProfileError, match="non-empty"):
        _load(tmp_path, "blank", 'signed_out:\n  url_contains: ["  "]\n')


def test_an_unknown_signed_out_key_is_rejected(tmp_path):
    with pytest.raises(ProfileError, match="fake_key"):
        _load(tmp_path, "unknown", 'signed_out:\n  url_contains: ["/login"]\n  fake_key: 1\n')


def test_a_credential_shaped_signed_out_key_is_rejected(tmp_path):
    with pytest.raises(ProfileError, match="credential"):
        _load(tmp_path, "credential", 'signed_out:\n  session: "/login"\n')


def test_the_shipped_profiles_carry_the_observed_markers():
    raif = load_profile("raif")
    assert raif["signed_out"] == {"url_contains": ["/login"], "text": []}, (
        "only the redirect to /login/main was observed; the «Войти в "
        "онлайн-банк» button text is untested against logged-in pages and a "
        "footer or promo could abort a good run"
    )

    assert "signed_out" not in load_profile("tbank"), (
        "no T-Bank sign-in screen was ever recorded, and docs/ADD_A_BANK.md "
        "requires markers seen on a real one"
    )

    assert "signed_out" not in load_profile("alfa"), (
        "Alfa's secret-code screen must keep working exactly as before"
    )


# ---------------------------------------------------------------------------
# A normal run stops at the sign-in screen
# ---------------------------------------------------------------------------


def _raif_profile():
    profile = copy.deepcopy(load_profile("raif"))
    profile["balances"] = []
    return profile


def _text_marked_profile():
    """The shipped profile with a text marker put back, for the generic path."""
    profile = _raif_profile()
    profile["signed_out"] = {
        "url_contains": ["/login"],
        "text": ["Войти в онлайн-банк"],
    }
    return profile


def test_a_sign_in_url_stops_the_run_before_any_record():
    page = RoutedPage({HOME_URL: (LOGIN_URL, LOGIN_HTML)}, HOME_URL)

    result = collect(page, _raif_profile(), timeout_ms=1, now=NOW)

    assert result["signed_out"] == "url contains '/login'"
    assert result["balances"] == [] and result["transactions"] == []
    assert result["warnings"] == [SIGNED_OUT_WARNING % "url contains '/login'"]
    assert page.writes == [], "nothing is clicked on a sign-in screen"
    assert page.visited == [HOME_URL], "no record page is opened"


def test_a_sign_in_text_stops_the_run_even_when_the_url_looks_right():
    page = RoutedPage({HOME_URL: (HOME_URL, LOGIN_HTML)}, HOME_URL)

    result = collect(page, _text_marked_profile(), timeout_ms=1, now=NOW)

    assert result["signed_out"] == "text 'Войти в онлайн-банк'"
    assert result["warnings"] == [SIGNED_OUT_WARNING % "text 'Войти в онлайн-банк'"]
    assert page.writes == []


def test_the_record_visit_is_checked_too():
    page = RoutedPage(
        {
            HOME_URL: (HOME_URL, _read_fixture("raif_home.html")),
            HISTORY_URL: (LOGIN_URL, LOGIN_HTML),
        },
        HOME_URL,
    )

    result = collect(page, _raif_profile(), timeout_ms=1, now=NOW)

    assert result["signed_out"] == "url contains '/login'"
    assert page.visited == [HOME_URL, HISTORY_URL]
    assert result["transactions"] == []
    assert page.writes == [], "the pagination arrow is never pressed"
    assert "scroll" not in result, "a signed-out run has no page-walk provenance"


def test_the_marker_must_match_the_url_path_not_the_query():
    url = HOME_URL + "?next=/login"
    page = RoutedPage({url: (url, _read_fixture("raif_home.html"))}, url)
    profile = _raif_profile()
    profile["transactions"] = []

    result = collect(page, profile, timeout_ms=1, now=NOW)

    assert "signed_out" not in result
    assert result["balances"] == []


# ---------------------------------------------------------------------------
# The CLI contract: exit 6, one line, no fuller file overwritten
# ---------------------------------------------------------------------------


class _Context(object):
    def __init__(self, page):
        self.pages = [page]
        self._page = page

    def new_page(self):
        return self._page


class _Browser(object):
    def __init__(self, page):
        self.contexts = [_Context(page)]


def _run(monkeypatch, page, *args):
    monkeypatch.setattr(cli, "connect", lambda *a, **k: (None, _Browser(page)))
    monkeypatch.setattr(cli, "PageAdapter", lambda page, timeout_ms=None: page)
    return cli.main(list(args))


def test_a_signed_out_run_exits_6_and_prints_the_stable_line(monkeypatch, capsys, tmp_path):
    page = RoutedPage({HOME_URL: (LOGIN_URL, LOGIN_HTML)}, HOME_URL)

    code = _run(
        monkeypatch,
        page,
        "--bank", "raif",
        "--out", str(tmp_path),
        "--timeout", "1",
    )
    out = capsys.readouterr()

    assert code == cli.EXIT_SIGNED_OUT == 6
    assert out.out == "SIGNED_OUT: raif\n", "stdout is exactly the one line"
    assert "warning: signed out: the cabinet shows its sign-in screen" in out.err
    assert "nothing was extracted" not in out.err, (
        "a sign-in screen is not a selector problem"
    )
    assert page.writes == []
    assert page.closed is True
    assert list(tmp_path.iterdir()) == [], (
        "a signed-out run writes no report, not even an empty or partial one"
    )


def test_a_signed_out_run_touches_no_existing_file(monkeypatch, capsys, tmp_path):
    """A fuller JSON *and* CSV stay byte-identical; nothing else appears."""
    previous_json = tmp_path / "raif.json"
    previous_json.write_text(
        json.dumps(
            {
                "transactions": [{"n": index} for index in range(100)],
                "scroll": {"mode": "pages", "stop": "date", "complete": True},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    previous_csv = tmp_path / "raif-transactions.csv"
    previous_csv.write_text("date,amount\n2026-10-24,-900\n", encoding="utf-8")
    json_before = previous_json.read_bytes()
    csv_before = previous_csv.read_bytes()
    page = RoutedPage({HOME_URL: (LOGIN_URL, LOGIN_HTML)}, HOME_URL)

    code = _run(
        monkeypatch,
        page,
        "--bank", "raif",
        "--out", str(tmp_path),
        "--snapshot",
        "--json",
        "--csv",
        "--timeout", "1",
    )
    out = capsys.readouterr()

    assert code == cli.EXIT_SIGNED_OUT
    assert out.out == "SIGNED_OUT: raif\n"
    assert previous_json.read_bytes() == json_before
    assert previous_csv.read_bytes() == csv_before
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "raif-transactions.csv",
        "raif.json",
    ], "no partial report, no CSV rewrite and no snapshot of the sign-in screen"
    assert "written:" not in out.out and "snapshot:" not in out.out


def test_exit_code_6_wins_over_the_empty_run_code():
    report = {"balances": [], "transactions": [], "warnings": []}

    code = cli._exit_code(
        report, ["raif.partial.json"], "raif.json", io.StringIO(), signed_out="url '/login'"
    )

    assert code == cli.EXIT_SIGNED_OUT


def test_the_help_lists_exit_code_6_and_the_flag():
    text = cli.build_parser().format_help()

    assert "--check-session" in text
    assert "SIGNED_OUT" in text and "SESSION: signed-out" in text
    assert "6 signed out" in text


# ---------------------------------------------------------------------------
# --check-session
# ---------------------------------------------------------------------------


def _check(monkeypatch, page, bank="raif"):
    return _run(
        monkeypatch,
        page,
        "--bank", bank,
        "--check-session",
        "--cdp-url", "ws://127.0.0.1:19988/cdp",
        "--timeout", "1",
    )


def test_check_session_reports_ok(monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    page = RoutedPage({HOME_URL: (HOME_URL, _read_fixture("raif_home.html"))}, HOME_URL)

    code = _check(monkeypatch, page)
    out = capsys.readouterr()

    assert code == cli.EXIT_OK
    assert out.out == "SESSION: ok raif\n"
    assert out.err == ""
    assert page.closed is True, "the tab it opened is closed again"
    assert page.writes == []
    assert list(tmp_path.iterdir()) == [], "a session check writes no report"


def test_check_session_reports_a_sign_in_screen(monkeypatch, capsys):
    page = RoutedPage({HOME_URL: (LOGIN_URL, LOGIN_HTML)}, HOME_URL)

    code = _check(monkeypatch, page)
    out = capsys.readouterr()

    assert code == cli.EXIT_SIGNED_OUT == 6
    assert out.out == "SESSION: signed-out raif\n"
    assert page.visited == [HOME_URL]
    assert page.writes == []
    assert page.closed is True


def test_check_session_reports_unknown_when_nothing_appeared(monkeypatch, capsys):
    page = RoutedPage(
        {HOME_URL: (HOME_URL, "<html><body>ничего знакомого</body></html>")}, HOME_URL
    )

    code = _check(monkeypatch, page)
    out = capsys.readouterr()

    assert code == cli.EXIT_NOTHING == 4
    assert out.out == "SESSION: unknown raif\n"
    assert "SESSION: unknown: neither wait_for" in out.err
    assert "within 1 ms" in out.err
    assert page.closed is True


def test_a_profile_without_signed_out_can_only_be_ok_or_unknown(monkeypatch, capsys):
    page = RoutedPage(
        {"https://example.invalid/cabinet": ("https://example.invalid/cabinet", "<html></html>")},
        "https://example.invalid/cabinet",
    )

    code = _check(monkeypatch, page, bank="example")
    out = capsys.readouterr()

    assert code == cli.EXIT_NOTHING
    assert out.out == "SESSION: unknown example\n"
    assert "no signed_out marker" in out.err


class _FailingPage(RoutedPage):
    """A page whose navigation fails (a dead tab, a lost connection)."""

    def goto(self, url, **kwargs):
        raise RuntimeError("connection reset by peer")


def test_check_session_says_why_the_navigation_failed(monkeypatch, capsys):
    page = _FailingPage({HOME_URL: (HOME_URL, "")}, HOME_URL)

    code = _check(monkeypatch, page)
    out = capsys.readouterr()

    assert code == cli.EXIT_NOTHING
    assert out.out == "SESSION: unknown raif\n"
    assert "SESSION: unknown: navigation failed: connection reset by peer" in out.err


def test_check_session_opens_the_profile_url_only():
    page = RoutedPage(
        {HOME_URL: (HOME_URL, _read_fixture("raif_home.html"))}, HOME_URL
    )

    assert check_session(page, load_profile("raif"), timeout_ms=1) == "ok"
    assert page.visited == [HOME_URL]
    assert page.scrolls == []
    assert page.writes == []


def test_check_session_cannot_be_combined_with_the_unlock(capsys):
    code = cli.main(["--bank", "alfa", "--check-session", "--enter-code"])
    err = capsys.readouterr().err

    assert code == cli.EXIT_BAD_PROFILE == 3, (
        "exit 2 means 'browser unavailable', not 'bad usage'"
    )
    assert "cannot be combined" in err


@pytest.mark.parametrize("flag", ["--snapshot", "--json", "--csv"])
def test_check_session_rejects_report_output(capsys, flag):
    code = cli.main(["--bank", "raif", "--check-session", flag])
    out = capsys.readouterr()

    assert code == cli.EXIT_BAD_PROFILE == 3
    assert out.out == ""
    assert flag in out.err and "cannot be combined" in out.err


# ---------------------------------------------------------------------------
# --check-session measures a wall-clock deadline, not a poll count
# ---------------------------------------------------------------------------


class _Clock(object):
    """A fake clock: ``sleep`` moves time without waiting for it."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class _CountingPage(RoutedPage):
    def __init__(self, routes, url, clock, body="ничего знакомого", slow_text=0.0):
        RoutedPage.__init__(self, routes, url)
        self.clock = clock
        self.body = body
        self.slow_text = slow_text
        self.text_reads = 0

    def inner_text(self, selector="body"):
        self.text_reads += 1
        self.clock.now += self.slow_text
        return self.body


def _marker_profile(wait_for="#never"):
    return {
        "bank": "synthetic",
        "status": "unverified",
        "url": HOME_URL,
        "wait_for": wait_for,
        "signed_out": {"url_contains": ["/login"], "text": ["Войти в онлайн-банк"]},
    }


def test_a_slow_text_marker_read_cannot_outlive_the_deadline(monkeypatch):
    """The deadline is wall-clock: one slow inner_text ends the wait."""
    clock = _Clock()
    page = _CountingPage(
        {HOME_URL: (HOME_URL, "<html><body>ничего</body></html>")}, HOME_URL,
        clock, slow_text=2.0,
    )
    monkeypatch.setattr(collect_mod, "time", clock)
    reasons = []

    status = check_session(page, _marker_profile(), timeout_ms=1000, reasons=reasons)

    assert status == "unknown"
    assert page.text_reads == 1, "a read slower than --timeout does not loop"
    assert "within 1000 ms" in reasons[0]


def test_the_text_marker_is_read_about_once_a_second(monkeypatch):
    clock = _Clock()
    page = _CountingPage(
        {HOME_URL: (HOME_URL, "<html><body>ничего</body></html>")}, HOME_URL, clock
    )
    monkeypatch.setattr(collect_mod, "time", clock)

    status = check_session(page, _marker_profile(), timeout_ms=2500)

    assert status == "unknown"
    # Once immediately and once per ~1 s: t=0, t=1.0 and t=2.5, not ten times.
    assert page.text_reads == 3


# ---------------------------------------------------------------------------
# A session that expires mid-run ends as "signed out", not as exit 5
# ---------------------------------------------------------------------------


def _history_page(number, last=False, titles=("Магазин", "Кофе", "Метро")):
    """One page of the paginated Raiffeisen list, three operations deep."""
    operations = "".join(
        "<li class='transaction-list__item'>"
        "<p class='transaction-list__item-title'>%s %d</p>"
        "<transaction-list-amount><div class='transaction-list-amount'>"
        "<div>−900 ₽</div>"
        "<div class='transaction-list-amount__cashback'></div>"
        "</div></transaction-list-amount></li>" % (title, number)
        for title in titles
    )
    arrow = (
        "<button class='pagination__item_arrow' data-direction='next'%s></button>"
        % (" disabled" if last else "")
    )
    return (
        "<html><body><div class='history'>"
        "<li class='transaction-group-list__item'>"
        "<h5 class='transaction-group-list__item__title'>24 октября, суббота</h5>"
        "<ul class='transaction-list'>%s</ul></li>"
        "<nav class='pagination'><div class='pagination__pages'>"
        "<button class='pagination__item_arrow' data-direction='prev' disabled></button>"
        "<button class='pagination__item_active'>%d</button>%s"
        "</div></nav></div></body></html>" % (operations, number, arrow)
    )


def _pages_profile(**page_overrides):
    """The shipped `raif` record, small pages, no real waits."""
    profile = copy.deepcopy(load_profile("raif"))
    profile["balances"] = []
    profile["url"] = HISTORY_URL
    profile["wait_for"] = ".transaction-list__item"
    pages = profile["transactions"][0]["pages"]
    pages.update(
        {"page_size": 3, "settle_ms": 0, "wait_ms": 1, "stop_before_date": None}
    )
    pages.update(page_overrides)
    return profile


class _SignOutOnClickPage(FakePage):
    """The next-page click lands on the sign-in screen instead of page 2."""

    def __init__(self, html=_history_page(1)):
        FakePage.__init__(self, html, url=HISTORY_URL)
        self.clicks = 0
        self._on_click = self._sign_out

    def _sign_out(self, page, element):
        self.clicks += 1
        self.url = LOGIN_URL
        self._set_root(LOGIN_HTML)


class _SignOutAfterAdvancePage(FakePage):
    """The pages do advance, but the URL switches to the sign-in screen."""

    def __init__(self, htmls):
        FakePage.__init__(self, htmls[0], url=HISTORY_URL)
        self.htmls = htmls
        self.index = 0
        self.clicks = 0
        self._on_click = self._advance

    def _advance(self, page, element):
        self.clicks += 1
        if self.index + 1 < len(self.htmls):
            self.index += 1
            self._set_root(self.htmls[self.index])
            self.url = LOGIN_URL


class _SignOutOnRecheckPage(FakePage):
    """The walk succeeds; the first-page recheck navigation signs the owner out."""

    def __init__(self, htmls):
        FakePage.__init__(self, htmls[0], url=HISTORY_URL)
        self.htmls = htmls
        self.index = 0
        self.gotos = 0
        self._on_click = self._advance

    def _advance(self, page, element):
        if self.index + 1 < len(self.htmls):
            self.index += 1
            self._set_root(self.htmls[self.index])

    def goto(self, url, **kwargs):
        self.visited.append(url)
        self.gotos += 1
        self.url = url
        if self.gotos == 1:
            self._set_root(self.htmls[0])
        else:
            self.url = LOGIN_URL
            self._set_root(LOGIN_HTML)
        return None


def test_a_sign_out_on_the_next_page_ends_the_run_as_signed_out():
    """The advance wait fails because the click served the sign-in screen."""
    page = _SignOutOnClickPage()

    result = collect(page, _pages_profile(), timeout_ms=1, now=NOW)

    assert result["signed_out"] == "url contains '/login'"
    assert result["balances"] == [] and result["transactions"] == []
    assert "scroll" not in result, "no truncated history is reported as exit 5"
    assert page.clicks == 1


def test_a_sign_out_right_after_the_page_advanced_is_caught_before_extraction():
    page = _SignOutAfterAdvancePage([_history_page(1), _history_page(2, last=True)])

    result = collect(page, _pages_profile(), timeout_ms=1, now=NOW)

    assert result["signed_out"] == "url contains '/login'"
    assert result["transactions"] == []
    assert "scroll" not in result
    assert page.clicks == 1


def test_a_sign_out_on_the_first_page_recheck_ends_as_signed_out():
    page = _SignOutOnRecheckPage([_history_page(1), _history_page(2, last=True)])

    result = collect(page, _pages_profile(), timeout_ms=1, now=NOW)

    assert result["signed_out"] == "url contains '/login'"
    assert "scroll" not in result, "recheck failed (exit 5) is not the answer here"
    assert page.gotos == 2, "the recheck really navigated"


SCROLL_URL = "https://synthetic.invalid/history/"


def _scroll_history(title, day="2026-03-01"):
    return (
        "<html><body><div class='operations'>"
        "<div><div data-date='%sT10:00:00.000+0300'><span>день</span></div>"
        "<button data-test-id='operation-cell'>"
        "<div data-test-id='title-text_content'>%s</div>"
        "</button></div></div></body></html>" % (day, title)
    )


def _scroll_profile():
    return {
        "bank": "synthetic",
        "status": "verified",
        "url": SCROLL_URL,
        "wait_for": "",
        "balances": [],
        "signed_out": {
            "url_contains": ["/login"],
            "text": ["Войти в онлайн-банк"],
        },
        "transactions": [
            {
                "row": "div:has(> [data-date]):has(> button[data-test-id=operation-cell])",
                "fields": {"date": {"selector": "[data-date]", "attr": "data-date"}},
                "items": {
                    "row": "button[data-test-id=operation-cell]",
                    "fields": {
                        "title": {"selector": "[data-test-id=title-text_content]"}
                    },
                },
                "scroll": {
                    "max_rounds": 10,
                    "step_px": 100,
                    "settle_ms": 0,
                    "stop_after_empty_rounds": 2,
                    "stop_before_date": None,
                },
            }
        ],
    }


def test_a_sign_out_while_scrolling_ends_the_run_as_signed_out():
    """The round added rows, so only the cheap URL marker is looked at."""
    page = FakePage(_scroll_history("Первый"), url=SCROLL_URL)

    def sign_out(the_page):
        the_page.url = LOGIN_URL
        the_page._set_root(_scroll_history("Второй"))

    page._on_scroll = sign_out

    result = collect(page, _scroll_profile())

    assert result["signed_out"] == "url contains '/login'"
    assert result["transactions"] == []
    assert "scroll" not in result, "a sign-in screen is not a truncated history"
    assert page.writes == []


def test_a_text_marker_catches_a_sign_out_when_a_round_added_nothing():
    page = FakePage(_scroll_history("Первый"), url=SCROLL_URL)

    def sign_out(the_page):
        the_page._set_root(LOGIN_HTML)

    page._on_scroll = sign_out

    result = collect(page, _scroll_profile())

    assert result["signed_out"] == "text 'Войти в онлайн-банк'"
    assert result["transactions"] == []
    assert "scroll" not in result
