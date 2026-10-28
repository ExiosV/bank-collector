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
    assert raif["signed_out"] == {
        "url_contains": ["/login"],
        "text": ["Войти в онлайн-банк"],
    }

    tbank = load_profile("tbank")
    assert tbank["signed_out"] == {"url_contains": ["/login"], "text": []}, (
        "only the generic path marker is justified; the exact T-Bank sign-in "
        "URL was not recorded"
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

    result = collect(page, _raif_profile(), timeout_ms=1, now=NOW)

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
        "--format", "json",
        "--timeout", "1",
    )
    out = capsys.readouterr()

    assert code == cli.EXIT_SIGNED_OUT == 6
    assert "SIGNED_OUT: raif\n" in out.out
    assert "warning: signed out: the cabinet shows its sign-in screen" in out.err
    assert "nothing was extracted" not in out.err, (
        "a sign-in screen is not a selector problem"
    )
    assert page.writes == []
    assert page.closed is True


def test_a_signed_out_run_never_overwrites_a_fuller_snapshot(monkeypatch, capsys, tmp_path):
    previous = tmp_path / "raif.json"
    previous.write_text(
        json.dumps(
            {
                "transactions": [{"n": index} for index in range(100)],
                "scroll": {"mode": "pages", "stop": "date", "complete": True},
            }
        ),
        encoding="utf-8",
    )
    page = RoutedPage({HOME_URL: (LOGIN_URL, LOGIN_HTML)}, HOME_URL)

    code = _run(
        monkeypatch,
        page,
        "--bank", "raif",
        "--out", str(tmp_path),
        "--format", "json",
        "--timeout", "1",
    )
    out = capsys.readouterr()

    assert code == cli.EXIT_SIGNED_OUT
    assert "SIGNED_OUT: raif" in out.out
    assert len(json.loads(previous.read_text(encoding="utf-8"))["transactions"]) == 100
    partial = tmp_path / "raif.partial.json"
    assert partial.exists()
    assert json.loads(partial.read_text(encoding="utf-8"))["transactions"] == []


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


def test_check_session_opens_the_profile_url_only():
    page = RoutedPage(
        {HOME_URL: (HOME_URL, _read_fixture("raif_home.html"))}, HOME_URL
    )

    assert check_session(page, load_profile("raif"), timeout_ms=1) == "ok"
    assert page.visited == [HOME_URL]
    assert page.scrolls == []
    assert page.writes == []


def test_check_session_cannot_be_combined_with_the_unlock(monkeypatch):
    with pytest.raises(SystemExit):
        cli.main(["--bank", "alfa", "--check-session", "--enter-code"])
