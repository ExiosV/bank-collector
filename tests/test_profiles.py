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


def test_alfa_profile_is_marked_unverified():
    profile = load_profile("alfa")
    assert profile["bank"] == "alfa"
    assert profile["status"] == "unverified"
    # The selectors are still placeholders — the profile is not usable yet.
    assert "TODO" in profile["transactions"][0]["fields"]["amount"]["selector"]


def test_profiles_are_listed():
    names = [entry["name"] for entry in list_profiles()]
    assert "example" in names
    assert "alfa" in names

    alfa = [entry for entry in list_profiles() if entry["name"] == "alfa"][0]
    assert alfa["status"] == "unverified"


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
