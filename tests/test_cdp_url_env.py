"""The CDP URL may come from the environment, so a relay token stays off the command line."""

from bank_collector.cli import CDP_URL_ENV, _cdp_url, build_parser
from bank_collector.connect import DEFAULT_CDP_URL


def _args(*argv):
    return build_parser().parse_args(["--bank", "alfa", *argv])


def test_flag_wins_over_the_environment(monkeypatch):
    monkeypatch.setenv(CDP_URL_ENV, "ws://env:1/cdp?token=env")
    assert _cdp_url(_args("--cdp-url", "http://flag:9222")) == "http://flag:9222"


def test_environment_is_used_without_the_flag(monkeypatch):
    monkeypatch.setenv(CDP_URL_ENV, "ws://env:1/cdp?token=env")
    assert _cdp_url(_args()) == "ws://env:1/cdp?token=env"


def test_default_without_flag_and_environment(monkeypatch):
    monkeypatch.delenv(CDP_URL_ENV, raising=False)
    assert _cdp_url(_args()) == DEFAULT_CDP_URL


def test_help_names_the_variable():
    assert CDP_URL_ENV in build_parser().format_help()
