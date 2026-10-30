"""Delivery path: deliver() end to end against a fake session socket."""
from __future__ import annotations

import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import shim as shim_mod  # noqa: E402

PASS = []
FAIL = []


def check(label: str, cond: bool, detail: str = ""):
    (PASS if cond else FAIL).append(label)
    print(("PASS " if cond else "FAIL ") + label + (f"  [{detail}]" if (detail and not cond) else ""))


def _run(limited: bool):
    sent, acks = [], []
    sess = {"persona": "test_user_3", "socket": "/tmp/example-socks/1.sock", "name": "test_user_3-1",
            "pid": 1, "limited": limited, "limited_since": None}
    orig = (shim_mod.live_sessions, shim_mod.native_send, shim_mod._ack)
    shim_mod.live_sessions = lambda: [sess]
    shim_mod.native_send = lambda sock, frm, body, window=None, pid=None, human=False, model=None: sent.append((sock, body, human))
    shim_mod._ack = lambda mid, ok, error="": acks.append((mid, ok, error))
    try:
        shim_mod.deliver({"id": "42", "to_persona": "test_user_3", "from_name": "test_agent_6",
                          "message": "hi"})
    finally:
        shim_mod.live_sessions, shim_mod.native_send, shim_mod._ack = orig
    return sent, acks


def test_deliver_live_session_sends_and_acks_ok():
    sent, acks = _run(limited=False)
    check("live: native_send called once", sent == [("/tmp/example-socks/1.sock", "hi", False)], str(sent))
    check("live: _ack(ok=True)", acks == [("42", True, "")], str(acks))


def test_deliver_limited_session_refuses_and_acks_fail():
    sent, acks = _run(limited=True)
    check("limited: no write", sent == [], str(sent))
    check("limited: _ack(ok=False, usage_limit)",
          len(acks) == 1 and acks[0][1] is False and "usage_limit" in acks[0][2], str(acks))


if __name__ == "__main__":
    test_deliver_live_session_sends_and_acks_ok()
    test_deliver_limited_session_refuses_and_acks_fail()
    print(f"\n{len(PASS)} PASS, {len(FAIL)} FAIL")
    sys.exit(0 if not FAIL else 1)
