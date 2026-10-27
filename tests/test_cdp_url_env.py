"""The CDP URL may come from the environment, so a relay token stays off argv, and never reaches stderr."""

import os
import sys
import types

import pytest

from bank_collector import cli, connect as connect_mod
from bank_collector.cli import build_parser, main
from bank_collector.connect import (
    CDP_URL_ENV,
    DEFAULT_CDP_URL,
    BrowserUnavailable,
    redact_url,
    resolve_cdp_url,
)

TOKEN = "SECRET-RELAY-TOKEN-123"
RELAY = "ws://relay.example:19988/cdp?token=%s" % TOKEN
STATEMENT = os.path.join(os.path.dirname(__file__), "fixtures", "alfa_statement.csv")


def test_flag_wins_over_the_environment(monkeypatch):
    monkeypatch.setenv(CDP_URL_ENV, RELAY)
    assert resolve_cdp_url("http://flag:9222") == "http://flag:9222"


def test_environment_is_used_without_the_flag(monkeypatch):
    monkeypatch.setenv(CDP_URL_ENV, RELAY)
    assert resolve_cdp_url(None) == RELAY


def test_default_without_flag_and_environment(monkeypatch):
    monkeypatch.delenv(CDP_URL_ENV, raising=False)
    assert resolve_cdp_url(None) == DEFAULT_CDP_URL


def test_an_empty_flag_is_refused_not_ignored(monkeypatch):
    monkeypatch.setenv(CDP_URL_ENV, RELAY)
    with pytest.raises(SystemExit) as raised:
        main(["--bank", "alfa", "--cdp-url", ""])
    assert raised.value.code == 2


def test_help_names_the_variable():
    assert CDP_URL_ENV in build_parser().format_help()


def test_main_hands_the_environment_url_to_connect(monkeypatch, capsys):
    monkeypatch.setenv(CDP_URL_ENV, RELAY)
    seen = []

    def fake_connect(url, timeout_ms=None):
        seen.append(url)
        raise BrowserUnavailable("stub")

    monkeypatch.setattr(cli, "connect", fake_connect)
    assert main(["--bank", "alfa"]) == cli.EXIT_NO_BROWSER
    assert seen == [RELAY]


def test_import_statement_ignores_the_environment_url(monkeypatch, tmp_path):
    monkeypatch.setenv(CDP_URL_ENV, RELAY)
    code = main(["--bank", "alfa", "--import-statement", STATEMENT, "--out", str(tmp_path)])
    assert code == 0


@pytest.mark.parametrize(
    "text",
    [
        RELAY,
        "ws://user:%s@relay.example:19988/cdp" % TOKEN,
        "connect ECONNREFUSED at ws://relay.example:19988/cdp?token=%s&session=1" % TOKEN,
        "WebSocket error: token=%s" % TOKEN,
    ],
)
def test_redact_url_removes_the_token(text):
    assert TOKEN not in redact_url(text)


def test_redact_url_keeps_a_plain_endpoint():
    assert redact_url("http://127.0.0.1:9222") == "http://127.0.0.1:9222"


def test_a_failed_attach_never_prints_the_token(monkeypatch, capsys):
    """Playwright repeats the endpoint in its own error; neither copy may reach stderr."""

    class FakeChromium(object):
        def connect_over_cdp(self, url, timeout=None):
            raise RuntimeError("BrowserType.connect_over_cdp: connect ECONNREFUSED %s" % url)

    class FakePlaywright(object):
        chromium = FakeChromium()

        def stop(self):
            pass

    fake = types.SimpleNamespace(start=lambda: FakePlaywright())
    monkeypatch.setattr(connect_mod, "_import_playwright", lambda: (lambda: fake))
    monkeypatch.setenv(CDP_URL_ENV, RELAY)

    assert main(["--bank", "alfa"]) == cli.EXIT_NO_BROWSER
    err = capsys.readouterr().err
    assert "relay.example:19988" in err
    assert TOKEN not in err


def test_a_blank_environment_variable_is_refused(monkeypatch, capsys):
    monkeypatch.setenv(CDP_URL_ENV, "  ")
    assert main(["--bank", "alfa"]) == cli.EXIT_NO_BROWSER
    assert "is set but empty" in capsys.readouterr().err


def test_a_blank_environment_variable_does_not_block_offline_commands(monkeypatch, tmp_path):
    monkeypatch.setenv(CDP_URL_ENV, " ")
    assert main(["--list-profiles"]) == 0
    code = main(["--bank", "alfa", "--import-statement", STATEMENT, "--out", str(tmp_path)])
    assert code == 0


def test_resolve_refuses_a_blank_environment_variable(monkeypatch):
    monkeypatch.setenv(CDP_URL_ENV, "")
    with pytest.raises(BrowserUnavailable):
        resolve_cdp_url(None)


def test_a_malformed_url_still_gives_browser_unavailable(monkeypatch):
    class FakeChromium(object):
        def connect_over_cdp(self, url, timeout=None):
            raise RuntimeError("bad url %s" % url)

    class FakePlaywright(object):
        chromium = FakeChromium()

        def stop(self):
            pass

    fake = types.SimpleNamespace(start=lambda: FakePlaywright())
    monkeypatch.setattr(connect_mod, "_import_playwright", lambda: (lambda: fake))
    with pytest.raises(BrowserUnavailable) as raised:
        connect_mod.connect("ws://[::1:19988/cdp?token=%s" % TOKEN)
    assert TOKEN not in str(raised.value)


def test_host_and_port_survive_literal_redaction():
    url = "ws://relay.example:19988/cdp?host=relay.example:19988&token=%s" % TOKEN
    text = redact_url("at relay.example:19988 failed: %s" % TOKEN, url)
    assert "relay.example:19988" in text
    assert TOKEN not in text


def test_short_values_are_not_removed_from_ordinary_text():
    assert redact_url("the cat sat", "ws://h:1/cdp?k=cat") == "the cat sat"


def test_redact_url_removes_a_fragment_token():
    assert TOKEN not in redact_url("ws://relay.example:19988/cdp#%s" % TOKEN)


def test_redact_url_removes_a_bare_or_encoded_token_given_the_url():
    url = "ws://relay.example:19988/cdp?token=%s+x" % TOKEN
    text = "handshake failed for %s+x (encoded: %s%%2Bx)" % (TOKEN, TOKEN)
    assert TOKEN not in redact_url(text, url)


def test_connect_with_none_still_raises_browser_unavailable(monkeypatch):
    class FakeChromium(object):
        def connect_over_cdp(self, url, timeout=None):
            raise RuntimeError("no endpoint")

    class FakePlaywright(object):
        chromium = FakeChromium()

        def stop(self):
            pass

    fake = types.SimpleNamespace(start=lambda: FakePlaywright())
    monkeypatch.setattr(connect_mod, "_import_playwright", lambda: (lambda: fake))
    with pytest.raises(BrowserUnavailable):
        connect_mod.connect(None)
