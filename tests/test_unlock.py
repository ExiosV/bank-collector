"""The static cabinet-code screen: detection, the guards and the one write path."""

import os
import stat

import pytest

from bank_collector import cli, miniyaml, unlock
from bank_collector.collect import collect
from tests.fake_page import FakePage

try:  # PyYAML is a dev convenience, the package itself only needs miniyaml
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

CODE = "91827364"

SCREEN_HTML = """
<html><body>
  <button data-test-id="change-user-button">Сменить пользователя</button>
  <div>Привет, Игорь! Войдите в мобильный банк</div>
  <form class="pincode-browser_form__bpRSA">
    <span aria-hidden="true">Секретный код</span>
    <input id="code" type="password" aria-label="Секретный код" maxlength="64">
    <button type="submit">Вперёд</button>
  </form>
  <button>Забыли код?</button>
</body></html>
"""

SCREEN_WITH_ATTEMPTS = SCREEN_HTML.replace(
    "Войдите в мобильный банк",
    "Войдите в мобильный банк. Не тот код. Осталось 2 попытки",
)

DASHBOARD_HTML = """
<html><body>
  <button data-test-id="common-balance-button">Все счета</button>
  <div>Дебетовая карта 12 345 ₽</div>
</body></html>
"""


def screen_page(html=SCREEN_HTML):
    return FakePage(html, url="https://private.auth.alfabank.ru/passport/pincode")


def write_code(tmp_path, code=CODE, mode=0o600):
    path = tmp_path / "alfa-code"
    path.write_text(code + "\n", encoding="utf-8")
    os.chmod(str(path), mode)
    return str(path)


# ── detection ──────────────────────────────────────────────────────────────────────────────
def test_detects_the_code_screen():
    found = unlock.detect_code_screen(screen_page())
    assert found is not None
    assert found.code_input.get_attribute("id") == "code"
    assert found.submit.get_attribute("type") == "submit"
    assert found.attempts is None


def test_no_marker_means_no_detection_and_no_typing(tmp_path):
    page = FakePage("<html><body><div>Привет</div><input type='password'></body></html>")
    assert unlock.detect_code_screen(page) is None

    result = unlock.handle_code_screen(
        page, config={}, code=CODE, timeout_ms=1, interval=0
    )
    assert result["detected"] is False
    assert result["entered"] is False
    assert page.writes == []


def test_hidden_input_is_ignored():
    html = SCREEN_HTML.replace(
        '<input id="code" type="password"',
        '<input id="ghost" type="password" style="display:none">\n'
        '    <input id="code" type="password"',
    )
    found = unlock.detect_code_screen(FakePage(html))
    assert found is not None
    assert found.code_input.get_attribute("id") == "code"


def test_input_selector_wins_over_the_heuristic():
    # a decoy password field outside the code form: two candidates make the
    # heuristic refuse to guess, while the profile's exact selector takes the
    # field inside the pincode form
    html = SCREEN_HTML.replace(
        '<form class="pincode-browser_form__bpRSA">',
        '<input id="decoy" type="password" aria-label="Пароль">\n'
        '  <form class="pincode-browser_form__bpRSA">',
    )
    assert unlock.detect_code_screen(FakePage(html)) is None

    exact = unlock.detect_code_screen(
        FakePage(html),
        input_selector='form[class*="pincode-browser_form"] input[type="password"]',
    )
    assert exact is not None
    assert exact.code_input.get_attribute("id") == "code"


def test_attempts_are_read_from_the_russian_text():
    assert unlock.attempts_left("Не тот код. Осталось 2 попытки") == 2
    assert unlock.attempts_left("осталось 5 попыток") == 5
    assert unlock.attempts_left("Секретный код") is None


# ── guards ────────────────────────────────────────────────────────────────────────────────
def test_too_few_attempts_left_means_nothing_is_typed(tmp_path):
    page = screen_page(SCREEN_WITH_ATTEMPTS)
    result = unlock.handle_code_screen(
        page,
        config={"min_attempts_left": 3},
        code_path=write_code(tmp_path),
        timeout_ms=1,
        interval=0,
    )
    assert result["detected"] is True
    assert result["entered"] is False
    assert "мало попыток" in result["reason"]
    assert page.writes == []


def test_only_one_code_per_run(tmp_path):
    page = screen_page()
    result = unlock.handle_code_screen(
        page,
        config={"max_attempts": 1},
        code=CODE,
        attempts_used=1,
        timeout_ms=1,
        interval=0,
    )
    assert result["entered"] is False
    assert "лимит" in result["reason"]
    assert page.writes == []


def test_missing_code_file_is_fatal_and_names_the_path(tmp_path):
    missing = str(tmp_path / "nope")
    page = screen_page()
    result = unlock.handle_code_screen(
        page, config={}, code_path=missing, timeout_ms=1, interval=0, env={}
    )
    assert result["detected"] is True
    assert result["fatal"] is True
    assert missing in result["reason"]
    assert page.writes == []


def test_world_readable_code_file_is_refused(tmp_path):
    page = screen_page()
    path = write_code(tmp_path, mode=0o644)
    result = unlock.handle_code_screen(
        page, config={}, code_path=path, timeout_ms=1, interval=0, env={}
    )
    assert result["fatal"] is True
    assert "chmod 600" in result["reason"]
    assert page.writes == []


# ── the happy path ────────────────────────────────────────────────────────────────────────
def test_code_is_typed_once_and_the_button_is_pressed(tmp_path):
    page = screen_page(SCREEN_HTML.replace(
        "</body>", '<div data-test-id="common-balance-button">Все счета</div></body>'))
    result = unlock.handle_code_screen(
        page,
        config={"success_wait": "[data-test-id=common-balance-button]"},
        code_path=write_code(tmp_path),
        timeout_ms=1,
        interval=0,
        env={},
    )
    assert result["entered"] is True, result["reason"]
    assert page.writes == [("fill", CODE), ("click", None)]


def test_correct_code_is_read_from_the_profile_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = write_code(tmp_path)
    code, source = unlock.read_code(profile_path="~/alfa-code")
    assert code == CODE
    assert source.startswith("file:")


def test_read_code_precedence(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    explicit = write_code(tmp_path, code="11111111")
    profile = tmp_path / "profile-code"
    profile.write_text("22222222\n", encoding="utf-8")
    os.chmod(str(profile), 0o600)

    code, source = unlock.read_code(
        path=explicit, profile_path=str(profile), env={"BANK_COLLECTOR_CODE": "33333333"}
    )
    assert (code, source) == ("11111111", "file:%s" % explicit)

    code, source = unlock.read_code(
        profile_path=str(profile), env={"BANK_COLLECTOR_CODE": "33333333"}
    )
    assert (code, source) == ("22222222", "file:%s" % profile)

    code, source = unlock.read_code(env={"BANK_COLLECTOR_CODE": "33333333"})
    assert (code, source) == ("33333333", "env:BANK_COLLECTOR_CODE")


# ── the one-strike marker ────────────────────────────────────────────────────────────────
def failing_config():
    """A config whose success selector is not on the screen."""
    return {"success_wait": "[data-test-id=common-balance-button]"}


def test_failed_entry_leaves_a_marker_and_never_the_code(tmp_path):
    code_path = write_code(tmp_path)
    marker = unlock.attempt_marker_path(code_path)
    page = screen_page()

    result = unlock.handle_code_screen(
        page,
        config=failing_config(),
        code_path=code_path,
        timeout_ms=1,
        interval=0,
        env={},
    )
    assert result["detected"] is True
    assert result["entered"] is False
    assert result["fatal"] is False
    assert os.path.exists(marker)

    with open(marker, encoding="utf-8") as handle:
        content = handle.read()
    assert CODE not in content
    assert "ввод не привёл к кабинету" in content
    assert os.stat(marker).st_mode & 0o777 == 0o600


def test_successful_entry_leaves_no_marker(tmp_path):
    code_path = write_code(tmp_path)
    marker = unlock.attempt_marker_path(code_path)
    page = screen_page(SCREEN_HTML.replace(
        "</body>", '<div data-test-id="common-balance-button">Все счета</div></body>'))

    result = unlock.handle_code_screen(
        page,
        config=failing_config(),
        code_path=code_path,
        timeout_ms=1,
        interval=0,
        env={},
    )
    assert result["entered"] is True, result["reason"]
    assert not os.path.exists(marker)


def test_the_marker_stops_the_next_run(tmp_path):
    code_path = write_code(tmp_path)
    marker = unlock.attempt_marker_path(code_path)
    unlock.write_attempt_failure(marker, "проверка")
    page = screen_page()

    result = unlock.handle_code_screen(
        page, config={}, code_path=code_path, timeout_ms=1, interval=0, env={}
    )
    assert result["detected"] is True
    assert result["entered"] is False
    assert result["fatal"] is False
    assert marker in result["reason"]
    assert page.writes == []


def test_the_marker_stops_the_next_run_even_with_the_correct_code(tmp_path):
    # the marker does not care whether the code in the file is right:
    # a human has to look at the screen first
    code_path = write_code(tmp_path)
    marker = unlock.attempt_marker_path(code_path)
    unlock.write_attempt_failure(marker, "проверка")
    page = screen_page(SCREEN_HTML.replace(
        "</body>", '<div data-test-id="common-balance-button">Все счета</div></body>'))

    result = unlock.handle_code_screen(
        page,
        config=failing_config(),
        code_path=code_path,
        timeout_ms=1,
        interval=0,
        env={},
    )
    assert result["entered"] is False
    assert marker in result["reason"]
    assert page.writes == []


def test_a_failed_run_blocks_the_next_run_of_the_same_code(tmp_path):
    code_path = write_code(tmp_path)

    first = screen_page()
    unlock.handle_code_screen(
        first,
        config=failing_config(),
        code_path=code_path,
        timeout_ms=1,
        interval=0,
        env={},
    )
    assert first.writes == [("fill", CODE), ("click", None)]

    second = screen_page()
    result = unlock.handle_code_screen(
        second, config={}, code_path=code_path, timeout_ms=1, interval=0, env={}
    )
    assert result["entered"] is False
    assert second.writes == []


def test_the_code_is_masked_in_the_marker(tmp_path, monkeypatch):
    code_path = write_code(tmp_path)
    marker = unlock.attempt_marker_path(code_path)

    def leaky(page, code, screen, timeout_ms=10000):
        return {"entered": False, "reason": "не удалось ввести код: %s" % code}

    monkeypatch.setattr(unlock, "enter_code", leaky)
    result = unlock.handle_code_screen(
        screen_page(), config={}, code_path=code_path, timeout_ms=1, interval=0, env={}
    )
    with open(marker, encoding="utf-8") as handle:
        content = handle.read()
    assert CODE not in content
    assert CODE not in result["reason"]


def test_the_marker_of_the_environment_code_lives_in_the_default_place(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert unlock.attempt_marker_path(None) == str(tmp_path / ".config" / "bank-collector" / "alfa-code.failed")
    assert unlock.attempt_marker_path("~/alfa-code") == str(tmp_path / "alfa-code.failed")


# ── the CLI ───────────────────────────────────────────────────────────────────────────────
class _Context(object):
    def __init__(self, page):
        self.pages = [page]
        self._page = page

    def new_page(self):
        return self._page


class _Browser(object):
    def __init__(self, page):
        self.contexts = [_Context(page)]


def run_cli(monkeypatch, page, tmp_path, code_path):
    monkeypatch.setattr(cli, "connect", lambda *a, **k: (None, _Browser(page)))
    monkeypatch.setattr(cli, "PageAdapter", lambda page, timeout_ms=None: page)
    return cli.main([
        "--bank", "alfa",
        "--enter-code",
        "--code-file", code_path,
        "--cdp-url", "ws://127.0.0.1:19988/cdp",
        "--out", str(tmp_path),
        "--format", "json",
        "--timeout", "1",
    ])


def test_cli_prints_one_unlock_line_and_never_the_code(tmp_path, monkeypatch, capsys):
    page = screen_page(SCREEN_HTML.replace(
        "</body>", '<div data-test-id="common-balance-button">Все счета</div></body>'))
    code_path = write_code(tmp_path)
    code = run_cli(monkeypatch, page, tmp_path, code_path)
    out = capsys.readouterr()
    assert cli.UNLOCK_OK in out.out
    assert out.out.count("UNLOCK:") == 1
    assert CODE not in out.out and CODE not in out.err
    # the cabinet URL must be opened exactly once: the unlock already did it
    assert page.visited.count("https://web.alfabank.ru/dashboard/") == 1
    assert code in (cli.EXIT_OK, cli.EXIT_NOTHING, cli.EXIT_BAD_PROFILE)


def test_cli_is_silent_without_the_code_screen(tmp_path, monkeypatch, capsys):
    page = FakePage(DASHBOARD_HTML, url="https://web.alfabank.ru/dashboard/")
    code_path = write_code(tmp_path)
    run_cli(monkeypatch, page, tmp_path, code_path)
    out = capsys.readouterr()
    assert "UNLOCK" not in out.out
    assert page.writes == []


def test_cli_clear_code_failure_removes_the_marker(tmp_path, monkeypatch, capsys):
    def boom(*args, **kwargs):
        raise AssertionError("--clear-code-failure must not need a browser")

    monkeypatch.setattr(cli, "connect", boom)
    code_path = write_code(tmp_path)
    marker = unlock.attempt_marker_path(code_path)
    unlock.write_attempt_failure(marker, "проверка")

    code = cli.main(["--clear-code-failure", "--code-file", code_path])
    out = capsys.readouterr()
    assert code == cli.EXIT_OK
    assert out.out.strip() == "UNLOCK: маркер неудачной попытки снят (%s)" % marker
    assert not os.path.exists(marker)


def test_cli_clear_code_failure_without_a_marker(tmp_path, monkeypatch, capsys):
    code_path = write_code(tmp_path)
    marker = unlock.attempt_marker_path(code_path)
    code = cli.main(["--clear-code-failure", "--code-file", code_path])
    out = capsys.readouterr()
    assert code == cli.EXIT_OK
    assert out.out.strip() == "UNLOCK: маркера нет (%s)" % marker


def test_cli_is_silent_and_the_code_stays_out_after_a_failed_entry(tmp_path, monkeypatch, capsys):
    page = screen_page()
    code_path = write_code(tmp_path)
    marker = unlock.attempt_marker_path(code_path)

    run_cli(monkeypatch, page, tmp_path, code_path)
    out = capsys.readouterr()
    assert CODE not in out.out and CODE not in out.err
    assert os.path.exists(marker)
    with open(marker, encoding="utf-8") as handle:
        content = handle.read()
    assert CODE not in content


def test_cli_does_not_type_while_the_marker_is_there(tmp_path, monkeypatch, capsys):
    code_path = write_code(tmp_path)
    marker = unlock.attempt_marker_path(code_path)
    unlock.write_attempt_failure(marker, "проверка")

    page = screen_page()
    run_cli(monkeypatch, page, tmp_path, code_path)
    out = capsys.readouterr()
    assert page.writes == []
    assert marker in out.out
    assert CODE not in out.out and CODE not in out.err


# ── the shipped profile ──────────────────────────────────────────────────────────────────
def test_alfa_profile_parses_with_miniyaml_and_pyyaml():
    path = os.path.join(os.path.dirname(unlock.__file__), "profiles", "alfa.yaml")
    with open(path, encoding="utf-8") as handle:
        raw = handle.read()
    parsed = miniyaml.safe_load(raw)
    assert parsed["unlock"]["status"] == "verified"
    assert parsed["unlock"]["input_selector"] == (
        'form[class*="pincode-browser_form"] input[type="password"]'
    )
    assert parsed["unlock"]["min_attempts_left"] == 3
    if yaml is not None:
        assert parsed == yaml.safe_load(raw)
