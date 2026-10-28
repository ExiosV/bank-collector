"""Shared fixtures: no test is allowed to really sleep.

The collector waits a little before retrying a torn DOM and after every
scroll round. Those waits are real seconds in a browser and wasted time in a
test, so the ``time`` module the collector uses is replaced by a stand-in
whose ``sleep`` returns at once. It is replaced instead of patched globally so
that the real standard library is never touched.
"""

import time

import bank_collector.collect as collect_mod
import pytest


class _NoSleep(object):
    """The parts of ``time`` the collector needs, with an instant ``sleep``.

    ``monotonic`` is the real clock: ``--check-session`` measures its
    deadline with it, and a test that wants a fake clock replaces this whole
    object.
    """

    sleep = staticmethod(lambda *args, **kwargs: None)
    monotonic = staticmethod(time.monotonic)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Make every wait instantaneous."""
    monkeypatch.setattr(collect_mod, "time", _NoSleep)
