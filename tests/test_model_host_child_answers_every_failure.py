"""The model host answers every request, whatever the call raised.

Windows, 2026-11-18: an image generation raised OSError ("[WinError 10038] ... not a socket") inside
the model host. The request handler took every OSError for a broken connection, answered nothing,
and the gateway waited for that answer for ever (the run stayed `running`, the request never returned).
"""

from __future__ import annotations

import threading

import pytest

from abstractgateway import model_host_child as child

pytestmark = pytest.mark.basic


class _Host:
    def resolve(self, target):
        return target


class _Channel:
    def __init__(self, *, broken: bool = False) -> None:
        self.host = _Host()
        self.closed = threading.Event()
        self.events: dict = {}
        self.sent: list = []
        self._broken = broken

    def send(self, msg) -> None:
        if self._broken:
            raise BrokenPipeError("the gateway is gone")
        self.sent.append(msg)


class _Client:
    def generate(self):
        raise OSError(10038, "An operation was attempted on something that is not a socket")


def test_an_oserror_raised_by_the_call_is_answered_not_taken_for_a_dead_connection() -> None:
    channel = _Channel()
    child._execute(channel, ("call", _Client(), "generate", (), {}))
    assert not channel.closed.is_set()
    assert len(channel.sent) == 1 and channel.sent[0][0] == "err"
    assert "not a socket" in str(channel.sent[0][1])


def test_an_answer_that_cannot_be_sent_closes_the_connection() -> None:
    channel = _Channel(broken=True)
    child._execute(channel, ("call", _Client(), "generate", (), {}))
    assert channel.closed.is_set() and channel.sent == []
