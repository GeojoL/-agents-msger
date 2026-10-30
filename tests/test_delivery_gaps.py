#!/usr/bin/env python3
"""Delivery gap regression tests for the hub and shim delivery path."""
from __future__ import annotations

import json
import os
import pathlib
import sys
import threading
import time

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

import hub as hub_mod       # noqa: E402
import shim as shim_mod      # noqa: E402

PASS: list = []
FAIL: list = []


def check(label: str, cond: bool, detail: str = ""):
    (PASS if cond else FAIL).append(label)
    print(("PASS " if cond else "FAIL ") + label + (f"  [{detail}]" if (detail and not cond) else ""))


# ---------------------------------------------------------------- G2 ---

def test_g2_no_ack_returns_ok_false_delivered_false():
    """A target that advertises `ack` but never acks must result in
    ok:false delivered:false with the reason explicit — never a bare ok:true."""
    h = hub_mod.Hub(poll_timeout=1)
    h.ACK_WAIT_S = 0.1
    h.heartbeat("test_host_3",
                [{"pid": 5555, "persona": "test_user_3", "name": "test_user_3-dj",
                  "socket": "/tmp/5555.sock", "started": 900,
                  "session_id": "sid-dj"}],
                degraded=[], facts={}, caps=["ack"])
    # NO shim actor — the host just doesn't ack.
    r = h.send("test_agent_2", "test_agent_3", "test_host_3", "test_user_3", "hi")
    check("G2 ok=false when no ack arrives",
          r.get("ok") is False, f"{r}")
    check("G2 delivered=false explicit",
          r.get("delivered") is False, f"{r}")
    check("G2 error names the cause",
          "ack" in (r.get("error") or ""), f"{r}")
    check("G2 target still echoed",
          r.get("to", {}).get("pid") == 5555, f"{r}")


def test_g2_no_cap_host_also_fails_loud():
    """G2 no cap host also fails loud."""
    h = hub_mod.Hub(poll_timeout=1)
    h.heartbeat("legacy",
                [{"pid": 1234, "persona": "test_user_3", "name": "test_user_3-lg",
                  "socket": "/tmp/x", "started": 1, "session_id": "s"}],
                degraded=[], facts={}, caps=[])
    r = h.send("test_agent_2", "test_agent_3", "legacy", "test_user_3", "hi")
    check("G2 non-ack host returns ok=false",
          r.get("ok") is False, f"{r}")
    check("G2 non-ack host status=unconfirmed",
          r.get("status") == "unconfirmed", f"{r}")


# ---------------------------------------------------------------- G3 ---

def test_g3_poll_records_inflight_until_ack():
    """A message /poll handed out is recorded in `inflight` until /ack arrives.
    Exposes to /status.inflight_count. Previously it vanished the moment poll
    returned, and no part of the hub remembered it existed."""
    h = hub_mod.Hub(poll_timeout=1)
    h.ACK_WAIT_S = 0.05  # keep /send fast; we care about the poll side
    h.heartbeat("test_host_1",
                [{"pid": 1, "persona": "test_user_3", "name": "test_user_3-c7",
                  "socket": "/tmp/1", "started": 1, "session_id": "s"}],
                degraded=[], facts={}, caps=["ack"])
    # Send without a shim actor — the message queues, poll pops it, no ack arrives.
    h.send("test_agent_2", "test_agent_3", "test_host_1", "test_user_3", "msg", {"from_pid": 1})
    msg = h.poll("test_host_1")
    check("G3 message was popped by poll", msg is not None, f"{msg}")
    check("G3 inflight_count=1 after poll with no ack",
          h.status()["test_host_1"]["inflight_count"] == 1,
          f"{h.status()['test_host_1']}")
    # Now simulate the shim acking:
    if msg:
        h.ack("test_host_1", msg["id"], True, "")
    check("G3 inflight_count=0 after ack",
          h.status()["test_host_1"]["inflight_count"] == 0,
          f"{h.status()['test_host_1']}")


def test_g3_sweep_drops_expired_inflight():
    """G3 sweep drops expired inflight."""
    orig_ttl = hub_mod.INFLIGHT_TTL_S
    try:
        hub_mod.INFLIGHT_TTL_S = 0.05
        h = hub_mod.Hub(poll_timeout=1)
        h.ACK_WAIT_S = 0.05
        h.heartbeat("test_host_1",
                    [{"pid": 1, "persona": "test_user_3", "name": "test_user_3-c7",
                      "socket": "/tmp/1", "started": 1, "session_id": "s"}],
                    degraded=[], facts={}, caps=["ack"])
        h.send("test_agent_2", "test_agent_3", "test_host_1", "test_user_3", "msg", {"from_pid": 1})
        msg = h.poll("test_host_1")
        check("G3 sweep: message in inflight after poll",
              h.status()["test_host_1"]["inflight_count"] == 1,
              f"{h.status()['test_host_1']}")
        time.sleep(0.2)
        # trigger a sweep via a subsequent poll on any host
        h.poll("test_host_1")
        check("G3 sweep: expired inflight is dropped",
              h.status()["test_host_1"]["inflight_count"] == 0,
              f"{h.status()['test_host_1']}")
        check("G3 sweep: dropped total incremented",
              h.status()["test_host_1"]["inflight_dropped_total"] >= 1,
              f"{h.status()['test_host_1']}")
    finally:
        hub_mod.INFLIGHT_TTL_S = orig_ttl


# ---------------------------------------------------------------- G4 ---

def test_g4_dead_rows_pruned_after_ttl():
    """G4 dead rows pruned after ttl."""
    orig_ttl = hub_mod.DEAD_ROW_TTL_S
    try:
        hub_mod.DEAD_ROW_TTL_S = 0.05
        h = hub_mod.Hub(poll_timeout=1)
        h.heartbeat("test_host_1",
                    [{"pid": 1, "persona": "test_user_3", "name": "test_user_3-a",
                      "socket": "/tmp/1", "started": 1, "session_id": "s1"},
                     {"pid": 2, "persona": "test_user_3", "name": "test_user_3-b",
                      "socket": "/tmp/2", "started": 2, "session_id": "s2"}],
                    degraded=[], facts={}, caps=["ack"])
        h.heartbeat("test_host_1",
                    [{"pid": 2, "persona": "test_user_3", "name": "test_user_3-b",
                      "socket": "/tmp/2", "started": 2, "session_id": "s2"}],
                    degraded=[], facts={}, caps=["ack"])
        pids_now = {s["pid"] for s in h.sessions["test_host_1"].values()}
        check("G4 before TTL: both test_user_3 rows still kept (one alive, one dead)",
              pids_now == {1, 2}, f"{pids_now}")
        time.sleep(0.15)
        # Third heartbeat: triggers prune of the dead row (older than TTL).
        h.heartbeat("test_host_1",
                    [{"pid": 2, "persona": "test_user_3", "name": "test_user_3-b",
                      "socket": "/tmp/2", "started": 2, "session_id": "s2"}],
                    degraded=[], facts={}, caps=["ack"])
        pids_later = {s["pid"] for s in h.sessions["test_host_1"].values()}
        check("G4 after TTL: dead test_user_3-a row pruned, live test_user_3-b kept",
              pids_later == {2}, f"{pids_later}")
    finally:
        hub_mod.DEAD_ROW_TTL_S = orig_ttl


def test_g4_silent_host_fails_loud_in_send():
    """A host whose shim stopped heartbeating has sessions whose `alive` flag
    is stale. _resolve must treat the host as gone and fail-loud on /send,
    so a message does not sit in the outbox for a host that will never poll."""
    orig = hub_mod.ALIVE_WINDOW_S
    try:
        hub_mod.ALIVE_WINDOW_S = 0.05
        h = hub_mod.Hub(poll_timeout=1)
        h.heartbeat("test_host_2",
                    [{"pid": 1, "persona": "test_agent_7", "name": "test_agent_7-a",
                      "socket": "/tmp/1", "started": 1, "session_id": "s"}],
                    degraded=[], facts={}, caps=["ack"])
        time.sleep(0.1)
        r = h.send("test_agent_2", "test_agent_3", "test_host_2", "test_agent_7", "hi")
        check("G4 silent host: send returns ok=false",
              r.get("ok") is False, f"{r}")
    finally:
        hub_mod.ALIVE_WINDOW_S = orig


def test_g4_silent_host_flips_who_to_not_alive():
    """/who shows every session of a silent host as alive=False, consistent
    with /status's host-level liveness. A UI showing /who no longer displays
    a dead-silent session as alive."""
    orig = hub_mod.ALIVE_WINDOW_S
    try:
        hub_mod.ALIVE_WINDOW_S = 0.05
        h = hub_mod.Hub(poll_timeout=1)
        h.heartbeat("test_host_2",
                    [{"pid": 1, "persona": "test_agent_7", "name": "test_agent_7-a",
                      "socket": "/tmp/1", "started": 1, "session_id": "s"}],
                    degraded=[], facts={}, caps=["ack"])
        time.sleep(0.1)
        who = h.who()
        test_agent_7 = who.get("test_host_2", {}).get("test_agent_7", [])
        check("G4 silent host: /who marks sessions alive=False",
              test_agent_7 and all(e["alive"] is False for e in test_agent_7), f"{test_agent_7}")
    finally:
        hub_mod.ALIVE_WINDOW_S = orig


# ---------------------------------------------------------------- G1 ---

def test_g1_deliver_crash_acks_failure():
    """G1 deliver crash acks failure."""
    # We don't need a real hub here; patch _ack to capture arguments, then
    # feed a message into deliver via the serve loop's path.
    captured = []
    saved_ack = shim_mod._ack
    saved_local = shim_mod.local_socket_for
    try:
        shim_mod._ack = lambda mid, ok, error="": captured.append(
            {"id": mid, "ok": ok, "error": error})

        def boom(persona, match=None):
            raise RuntimeError("simulated boom in local_socket_for")

        shim_mod.local_socket_for = boom
        # Simulate the serve-loop wrapper (shim.py:1130-1143) directly.
        msg = {"id": "mid-g1", "to_persona": "test_user_3", "message": "hi"}
        try:
            shim_mod.deliver(msg)
        except Exception as e:  # noqa: BLE001 — this is what the serve loop would catch
            mid = msg.get("id")
            shim_mod.log("deliver_crashed", id=mid,
                         error_type=type(e).__name__, error=str(e))
            if mid:
                shim_mod._ack(mid, False,
                              f"deliver crashed: {type(e).__name__}: {e}")
    finally:
        shim_mod._ack = saved_ack
        shim_mod.local_socket_for = saved_local
    check("G1 deliver crash produced an /ack call",
          len(captured) == 1, f"{captured}")
    if captured:
        rec = captured[0]
        check("G1 ack had ok=False", rec["ok"] is False, f"{rec}")
        check("G1 ack carried the exception type",
              "RuntimeError" in rec["error"], f"{rec}")
        check("G1 ack has the mid round-trip",
              rec["id"] == "mid-g1", f"{rec}")


def main():
    test_g2_no_ack_returns_ok_false_delivered_false()
    test_g2_no_cap_host_also_fails_loud()
    test_g3_poll_records_inflight_until_ack()
    test_g3_sweep_drops_expired_inflight()
    test_g4_dead_rows_pruned_after_ttl()
    test_g4_silent_host_fails_loud_in_send()
    test_g4_silent_host_flips_who_to_not_alive()
    test_g1_deliver_crash_acks_failure()
    print(f"\n{len(PASS)} PASS, {len(FAIL)} FAIL")
    if FAIL:
        print("FAILED:", *FAIL, sep="\n  ")
        return 1
    print("ALL GREEN")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
