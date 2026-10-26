"""Shared fixtures: no test is allowed to really sleep.

The collector waits a little before retrying a torn DOM and after every
scroll round. Those waits are real seconds in a browser and wasted time in a
test, so the ``time`` module the collector uses is replaced by a stand-in
whose ``sleep`` returns at once. It is replaced instead of patched globally so
that the real standard library is never touched.
"""

import bank_collector.collect as collect_mod
import pytest


class _NoSleep(object):
    """The only thing the collector needs from ``time``: ``sleep``."""

    @staticmethod
    def sleep(*args, **kwargs):
        return None


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Make every wait instantaneous."""
    monkeypatch.setattr(collect_mod, "time", _NoSleep)
