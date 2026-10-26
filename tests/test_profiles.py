"""Validation of the shipped profiles."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bank_collector.profiles import (  # noqa: E402
    ProfileError,
    list_profiles,
    load_profile,
    profile_dir,
)


def test_example_profile_is_valid():
    profile = load_profile("example")
    assert profile["bank"] == "example"
    assert profile["status"] == "verified"
    assert profile["url"].startswith("https://")
    assert profile["wait_for"]
    assert profile["balances"] and profile["transactions"]


def test_alfa_profile_is_verified_and_split_across_two_pages():
    profile = load_profile("alfa")
    assert profile["bank"] == "alfa"
    assert profile["status"] == "verified"
    # Balances come from the dashboard, the statement lives on /history/.
    assert profile["url"].endswith("/dashboard/")
    record = profile["transactions"][0]
    assert record["url"].endswith("/history/")
    # Operations are grouped by day: the date sits on the group, not on the row.
    assert record["fields"]["date"]["attr"] == "data-date"
    assert record["items"]["row"] == "button[data-test-id=operation-cell]"
    assert set(record["items"]["fields"]) == {"title", "category", "amount"}


def test_profiles_are_listed():
    names = [entry["name"] for entry in list_profiles()]
    assert "example" in names
    assert "alfa" in names

    alfa = [entry for entry in list_profiles() if entry["name"] == "alfa"][0]
    assert alfa["status"] == "verified"


def test_missing_profile_raises():
    with pytest.raises(ProfileError):
        load_profile("there-is-no-such-bank")


def test_profile_name_cannot_escape_the_directory():
    with pytest.raises(ProfileError):
        load_profile("../../etc/passwd")


def test_flat_record_form_is_accepted(tmp_path):
    path = tmp_path / "flat.yaml"
    path.write_text(
        "bank: flat\n"
        "status: verified\n"
        'url: "https://flat.invalid/"\n'
        'wait_for: "#x"\n'
        "balances:\n"
        "  - amount:\n"
        '      selector: ".amount"\n',
        encoding="utf-8",
    )

    profile = load_profile("flat", directory=str(tmp_path))
    assert profile["balances"][0]["fields"]["amount"]["selector"] == ".amount"


def test_missing_required_key_is_rejected(tmp_path):
    path = tmp_path / "broken.yaml"
    path.write_text(
        "bank: broken\nstatus: verified\nurl: \"https://x.invalid/\"\n",
        encoding="utf-8",
    )
    with pytest.raises(ProfileError) as excinfo:
        load_profile("broken", directory=str(tmp_path))
    assert "wait_for" in str(excinfo.value)


def test_unknown_status_is_rejected(tmp_path):
    path = tmp_path / "weird.yaml"
    path.write_text(
        "bank: weird\nstatus: probably\nurl: \"https://x.invalid/\"\n"
        'wait_for: "#x"\nbalances: []\ntransactions: []\n',
        encoding="utf-8",
    )
    with pytest.raises(ProfileError) as excinfo:
        load_profile("weird", directory=str(tmp_path))
    assert "status" in str(excinfo.value)


def test_credential_shaped_keys_are_rejected(tmp_path):
    path = tmp_path / "creds.yaml"
    path.write_text(
        "bank: creds\nstatus: verified\nurl: \"https://x.invalid/\"\n"
        'wait_for: "#x"\nbalances: []\ntransactions:\n'
        "  - password:\n"
        '      selector: ".password"\n',
        encoding="utf-8",
    )
    with pytest.raises(ProfileError):
        load_profile("creds", directory=str(tmp_path))


def test_profile_dir_exists():
    assert os.path.isdir(profile_dir())
    assert os.path.isfile(os.path.join(profile_dir(), "example.yaml"))
    assert os.path.isfile(os.path.join(profile_dir(), "alfa.yaml"))


def test_scroll_accepted_with_defaults_filled(tmp_path):
    """A record with a minimal ``scroll`` block gets all defaults filled in."""
    path = tmp_path / "scroll_defaults.yaml"
    path.write_text(
        "bank: scroll_defaults\n"
        "status: verified\n"
        'url: "https://x.invalid/"\n'
        'wait_for: "#x"\n'
        "balances: []\n"
        "transactions:\n"
        "  - row: '.row'\n"
        "    fields:\n"
        "      x:\n"
        '        selector: ".x"\n'
        "    scroll:\n"
        "      max_rounds: 10\n",
        encoding="utf-8",
    )

    profile = load_profile("scroll_defaults", directory=str(tmp_path))
    scroll = profile["transactions"][0]["scroll"]
    assert isinstance(scroll, dict)
    assert scroll["max_rounds"] == 10
    assert scroll["step_px"] == 1500
    assert scroll["settle_ms"] == 800
    assert scroll["stop_after_empty_rounds"] == 3
    assert scroll["stop_before_date"] is None


def test_scroll_unknown_sub_key_rejected(tmp_path):
    """A scroll block must not carry keys we do not know."""
    path = tmp_path / "unknown_scroll.yaml"
    path.write_text(
        "bank: unknown_scroll\n"
        "status: verified\n"
        'url: "https://x.invalid/"\n'
        'wait_for: "#x"\n'
        "balances: []\n"
        "transactions:\n"
        "  - row: '.row'\n"
        "    fields:\n"
        "      x:\n"
        '        selector: ".x"\n'
        "    scroll:\n"
        "      fake_key: 42\n",
        encoding="utf-8",
    )

    with pytest.raises(ProfileError, match="fake_key"):
        load_profile("unknown_scroll", directory=str(tmp_path))


def test_scroll_bool_rejected_as_int(tmp_path):
    """A boolean value in an integer scroll slot is rejected (bool is int in Python)."""
    path = tmp_path / "bool_scroll.yaml"
    path.write_text(
        "bank: bool_scroll\n"
        "status: verified\n"
        'url: "https://x.invalid/"\n'
        'wait_for: "#x"\n'
        "balances: []\n"
        "transactions:\n"
        "  - row: '.row'\n"
        "    fields:\n"
        "      x:\n"
        '        selector: ".x"\n'
        "    scroll:\n"
        "      max_rounds: true\n",
        encoding="utf-8",
    )

    with pytest.raises(ProfileError, match="must be an integer"):
        load_profile("bool_scroll", directory=str(tmp_path))


def test_scroll_negative_int_rejected(tmp_path):
    """Negative integers for numeric scroll keys are rejected."""
    path = tmp_path / "negative_scroll.yaml"
    path.write_text(
        "bank: negative_scroll\n"
        "status: verified\n"
        'url: "https://x.invalid/"\n'
        'wait_for: "#x"\n'
        "balances: []\n"
        "transactions:\n"
        "  - row: '.row'\n"
        "    fields:\n"
        "      x:\n"
        '        selector: ".x"\n'
        "    scroll:\n"
        "      step_px: -100\n",
        encoding="utf-8",
    )

    with pytest.raises(ProfileError, match="must be >="):
        load_profile("negative_scroll", directory=str(tmp_path))


def test_scroll_stop_before_date_bad_format_rejected(tmp_path):
    """``stop_before_date`` that is not YYYY-MM-DD or is an invalid calendar date is rejected."""
    # Bad format: not YYYY-MM-DD
    path1 = tmp_path / "bad_date_format.yaml"
    path1.write_text(
        "bank: bad_date_format\n"
        "status: verified\n"
        'url: "https://x.invalid/"\n'
        'wait_for: "#x"\n'
        "balances: []\n"
        "transactions:\n"
        "  - row: '.row'\n"
        "    fields:\n"
        "      x:\n"
        '        selector: ".x"\n'
        "    scroll:\n"
        "      stop_before_date: 01-01-2026\n",
        encoding="utf-8",
    )
    with pytest.raises(ProfileError, match="YYYY-MM-DD"):
        load_profile("bad_date_format", directory=str(path1.parent))

    # Invalid calendar date: month 13
    path2 = tmp_path / "bad_date_value.yaml"
    path2.write_text(
        "bank: bad_date_value\n"
        "status: verified\n"
        'url: "https://x.invalid/"\n'
        'wait_for: "#x"\n'
        "balances: []\n"
        "transactions:\n"
        "  - row: '.row'\n"
        "    fields:\n"
        "      x:\n"
        '        selector: ".x"\n'
        "    scroll:\n"
        "      stop_before_date: '2026-13-01'\n",
        encoding="utf-8",
    )
    with pytest.raises(ProfileError, match="not a real date"):
        load_profile("bad_date_value", directory=str(path2.parent))


def test_scroll_without_row_rejected(tmp_path):
    """A ``scroll`` block without a ``row`` on the same record is rejected."""
    path = tmp_path / "no_row_scroll.yaml"
    path.write_text(
        "bank: no_row_scroll\n"
        "status: verified\n"
        'url: "https://x.invalid/"\n'
        'wait_for: "#x"\n'
        "balances:\n"
        "  - fields:\n"
        "      x:\n"
        '        selector: ".x"\n'
        "    scroll:\n"
        "      max_rounds: 10\n"
        'transactions: []\n',
        encoding="utf-8",
    )

    with pytest.raises(ProfileError, match="needs a 'row'"):
        load_profile("no_row_scroll", directory=str(path.parent))
