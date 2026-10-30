#!/usr/bin/env python3
"""Rate-limit detection, queueing, auto-wake and receipt behaviour."""
from __future__ import annotations

import json
import os
import pathlib
import socket
import sys
import tempfile
import threading
import time

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import hub as hub_mod        # noqa: E402
import shim as shim_mod       # noqa: E402


PASS = []
FAIL = []


def check(label: str, cond: bool, detail: str = ""):
    (PASS if cond else FAIL).append(label)
    print(("PASS " if cond else "FAIL ") + label + (f"  [{detail}]" if (detail and not cond) else ""))


def _limit(ts: str, sentence: str) -> dict:
    """Limit."""
    return {"type": "assistant", "timestamp": ts, "isApiErrorMessage": True, "apiErrorStatus": 429,
            "error": "rate_limit", "message": {"content": [{"type": "text", "text": sentence}]}}


def _write_jsonl(root: pathlib.Path, session_id: str, lines: list[dict]) -> pathlib.Path:
    slug_dir = root / "projects" / "fake-slug"
    slug_dir.mkdir(parents=True, exist_ok=True)
    path = slug_dir / f"{session_id}.jsonl"
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------- transcript scanner

def test_session_limited_state_detects_usage_notice():
    root = pathlib.Path(tempfile.mkdtemp(prefix="limited-"))
    sid = "sess-notice"
    _write_jsonl(root, sid, [
        {"type": "assistant", "timestamp": "2026-10-23T00:00:00Z",
         "message": {"content": [{"text": "doing work"}]}},
        _limit("2026-10-23T00:01:00Z", "You've hit your session limit · resets 8:30pm (Asia/Singapore)"),
    ])
    r = shim_mod.session_limited_state(sid, root=root, now=shim_mod._iso_ts("2026-10-23T00:02:00Z"))
    check("session-limit sentence => limited, resets 8:30pm HKT that day",
          r["limited"] is True and r["reason"] == "session_limit" and r["resets_at"] == "2026-10-23T12:30:00Z",
          detail=str(r))


def test_session_limited_state_detects_rate_limit_api_error():
    """Session limited state detects rate limit api error."""
    root = pathlib.Path(tempfile.mkdtemp(prefix="limited-"))
    sid = "sess-429"
    _write_jsonl(root, sid, [
        {"type": "assistant", "timestamp": "2026-10-23T00:00:00Z",
         "message": {"content": [{"text": "ok"}]}},
        {"type": "assistant", "timestamp": "2026-10-23T00:02:00Z",
         "isApiErrorMessage": True, "apiErrorStatus": 429, "error": "rate_limit",
         "message": {"content": [{"text": "You've hit your weekly limit · resets 11pm (Asia/Singapore)"}]}},
    ])
    r = shim_mod.session_limited_state(sid, root=root, now=shim_mod._iso_ts("2026-10-23T00:03:00Z"))
    check("weekly-limit sentence without a date => limited, resets 11pm HKT that day",
          r["limited"] is True and r["reason"] == "weekly_limit" and r["resets_at"] == "2026-10-23T15:00:00Z",
          detail=str(r))


def test_transient_api_errors_are_not_limit():
    """Transient api errors are not limit."""
    root = pathlib.Path(tempfile.mkdtemp(prefix="limited-"))
    for sid, status, err in [
        ("sess-529", 529, "overloaded_error"),
        ("sess-500", 500, "api_error"),
        ("sess-400", 400, "unknown"),
        ("sess-403", 403, "authentication_failed"),
        ("sess-hold", None, "account_on_hold"),
    ]:
        _write_jsonl(root, sid, [
            {"type": "assistant", "timestamp": "2026-10-23T00:02:00Z",
             "isApiErrorMessage": True,
             **({"apiErrorStatus": status} if status else {}),
             "error": err,
             "message": {"content": [{"text": f"API Error: {status or err}"}]}},
        ])
        r = shim_mod.session_limited_state(sid, root=root)
        check(f"transient API error ({sid}) is NOT limited",
              r["limited"] is False, detail=str(r))


def test_session_limited_state_recovers_on_new_assistant_message():
    root = pathlib.Path(tempfile.mkdtemp(prefix="limited-"))
    sid = "sess-recovery"
    _write_jsonl(root, sid, [
        {"type": "system", "level": "notice", "timestamp": "2026-10-23T00:00:00Z",
         "content": "Usage limit reached · continuing automatically at 8:40pm"},
        {"type": "assistant", "timestamp": "2026-10-23T00:10:00Z",
         "message": {"content": [{"text": "back to normal"}]}},
    ])
    r = shim_mod.session_limited_state(sid, root=root)
    check("recovery by positive assistant turn",
          r["limited"] is False, detail=str(r))


def test_session_limited_state_ignores_our_own_tool_output():
    """Session limited state ignores our own tool output."""
    root = pathlib.Path(tempfile.mkdtemp(prefix="limited-"))
    sid = "sess-noise"
    _write_jsonl(root, sid, [
        # A tool-result entry (type:"tool_result") that mentions "usage limit reached" in text.
        {"type": "tool_result", "timestamp": "2026-10-23T00:00:00Z",
         "content": [{"type": "text",
                      "text": "docs say: usage limit reached; retry after N seconds"}]},
        {"type": "assistant", "timestamp": "2026-10-23T00:01:00Z",
         "message": {"content": [{"text": "fine"}]}},
    ])
    r = shim_mod.session_limited_state(sid, root=root)
    check("tool_result containing the phrase is NOT limited",
          r["limited"] is False, detail=str(r))


# ---------------------------------------------------------------- hub /send refusal

def test_hub_refuses_send_when_target_is_limited():
    hub = hub_mod.Hub(poll_timeout=5)
    hub.heartbeat("test_host_1", [
        {"persona": "test_host_5", "pid": 2222, "socket": "/tmp/test_host_5.sock",
         "name": "test_host_5-a1", "session_id": "sid-test_host_5",
         "started": time.time(),
         "limited": True, "limited_since": "2026-10-23T00:01:00Z",
         "limited_reason": "usage_limit_notice"}
    ], caps=[])         # non-ack path so hub.send does not wait on an ack we never post
    r = hub.send("test_host_6", "tool_006", "test_host_1", "test_host_5", "hi")
    check("hub refuses limited target: ok=False delivered=False",
          r.get("ok") is False and r.get("delivered") is False, detail=str(r))
    check("hub refusal error_code is usage_limit_reached",
          r.get("error") == "usage_limit_reached", detail=str(r))
    check("hub refusal names limited_since",
          r.get("limited_since") == "2026-10-23T00:01:00Z", detail=str(r))

    # Follow-up heartbeat with limited=False (recovery) → send goes through.
    hub.heartbeat("test_host_1", [
        {"persona": "test_host_5", "pid": 2222, "socket": "/tmp/test_host_5.sock",
         "name": "test_host_5-a1", "session_id": "sid-test_host_5",
         "started": time.time(),
         "limited": False, "limited_since": None, "limited_reason": None}
    ], caps=[])
    r2 = hub.send("test_host_6", "tool_006", "test_host_1", "test_host_5", "hi again")
    check("after recovery heartbeat, send queues/succeeds (not usage_limit_reached)",
          r2.get("error") != "usage_limit_reached", detail=str(r2))


# ---------------------------------------------------------------- runner

def test_tail_reader_caps_at_fixed_bytes_on_a_large_transcript():
    """Tail reader caps at fixed bytes on a large transcript."""
    root = pathlib.Path(tempfile.mkdtemp(prefix="limited-"))
    sid = "sess-big"
    # 10 MB of noise (one 100 KB JSON line, repeated) then the real anchor at the tail.
    slug_dir = root / "projects" / "fake-slug"
    slug_dir.mkdir(parents=True, exist_ok=True)
    path = slug_dir / f"{sid}.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        noise = {"type": "assistant", "message": {"content": [{"text": "x" * 100_000}]}}
        for _ in range(100):                                       # ~10 MB
            fh.write(json.dumps(noise) + "\n")
        fh.write(json.dumps(_limit("2026-10-23T05:00:00Z",
                                   "You've hit your session limit · resets 8pm (Asia/Singapore)")) + "\n")
    r = shim_mod.session_limited_state(sid, root=root, now=shim_mod._iso_ts("2026-10-23T05:01:00Z"))
    check("large transcript: tail-read still detects the sentence at the end",
          r["limited"] is True and r["reason"] == "session_limit",
          detail=str(r))
    # Also verify the helper did not pull in the whole file.
    tail = shim_mod._tail_lines(path, shim_mod.TRANSCRIPT_SCAN_BYTES,
                                 shim_mod.TRANSCRIPT_SCAN_LINES)
    joined_bytes = sum(len(l.encode("utf-8")) for l in tail)
    check(f"tail bytes read ≤ cap ({joined_bytes} ≤ {shim_mod.TRANSCRIPT_SCAN_BYTES})",
          joined_bytes <= shim_mod.TRANSCRIPT_SCAN_BYTES + 2048,
          detail=f"got {joined_bytes} bytes")



def _iso_ms(iso: str) -> float:
    return shim_mod._iso_ts(iso) * 1000.0


def _rate_limited_tail(root, sid):
    # The real shape: the transcript ends on a 429 rate_limit error; nothing newer.
    _write_jsonl(root, sid, [
        {"type": "assistant", "timestamp": "2026-01-02T01:00:00Z",
         "message": {"content": [{"text": "working"}]}},
        {"type": "assistant", "timestamp": "2026-01-02T01:57:00Z",
         "isApiErrorMessage": True, "apiErrorStatus": 429, "error": "rate_limit",
         "message": {"content": [{"text": "You've hit your session limit · resets 2am (Asia/Singapore)"}]}},
    ])


def test_relaunched_pid_is_not_held_by_the_old_limit():
    """Relaunched pid is not held by the old limit."""
    root = pathlib.Path(tempfile.mkdtemp(prefix="limited-"))
    sid = "sess-relaunched"
    _rate_limited_tail(root, sid)
    r = shim_mod.session_limited_state(sid, root=root, started_ms=_iso_ms("2026-01-02T13:55:00Z"))
    check("new pid started after the limit => not limited",
          r["limited"] is False and r.get("cleared") == "limit_predates_process", detail=str(r))


def test_same_process_that_hit_the_limit_stays_limited():
    root = pathlib.Path(tempfile.mkdtemp(prefix="limited-"))
    sid = "sess-same-pid"
    _rate_limited_tail(root, sid)
    r = shim_mod.session_limited_state(sid, root=root, started_ms=_iso_ms("2026-01-02T00:30:00Z"),
                                       now=shim_mod._iso_ts("2026-01-02T01:58:00Z"))
    check("process started before the limit => still limited",
          r["limited"] is True and r["reason"] == "session_limit", detail=str(r))


def test_usage_notice_reset_resolves_against_the_notice_not_now():
    """Usage notice reset resolves against the notice not now."""
    root = pathlib.Path(tempfile.mkdtemp(prefix="limited-"))
    sid = "sess-reset"
    notice_at = "2026-01-02T10:00:00Z"
    _write_jsonl(root, sid, [
        _limit(notice_at, "You've hit your session limit · resets 11:30pm (Asia/Singapore)"),
    ])
    t0 = shim_mod._iso_ts(notice_at)
    during = shim_mod.session_limited_state(sid, root=root, now=t0 + 60)
    check("reset not yet passed => limited", during["limited"] is True, detail=str(during))
    after = shim_mod.session_limited_state(sid, root=root, now=t0 + 3 * 86400)
    check("reset passed (3 days later) => cleared",
          after["limited"] is False and after.get("cleared") == "reset_passed", detail=str(after))


def test_hub_delivers_to_the_relaunched_pid():
    """Hub delivers to the relaunched pid."""
    root = pathlib.Path(tempfile.mkdtemp(prefix="limited-"))
    sid = "sess-e2e"
    _rate_limited_tail(root, sid)
    lim = shim_mod.session_limited_state(sid, root=root, started_ms=_iso_ms("2026-01-02T13:55:00Z"))
    hub = hub_mod.Hub(poll_timeout=5)
    hub.heartbeat("host-a", [
        {"persona": "agent-x", "pid": 9001, "socket": "/tmp/agent-x.sock",
         "name": "agent-x-1", "session_id": sid, "started": time.time(),
         "limited": lim["limited"], "limited_since": lim["limited_since"],
         "limited_reason": lim["reason"], "resets_at": lim.get("resets_at")},
    ], caps=[])
    r = hub.send("host-b", "sender", "host-a", "agent-x", "wake up")
    check("hub: relaunched pid not queued behind usage_limit_reached",
          r.get("error") != "usage_limit_reached" and "ticket" not in r, detail=str(r))
    check("hub: message is in the target host outbox for delivery",
          any(m.get("message") == "wake up" for m in hub.outbox.get("host-a", [])),
          detail=str(hub.outbox.get("host-a")))


if __name__ == "__main__":
    test_session_limited_state_detects_usage_notice()
    test_session_limited_state_detects_rate_limit_api_error()
    test_transient_api_errors_are_not_limit()
    test_session_limited_state_recovers_on_new_assistant_message()
    test_session_limited_state_ignores_our_own_tool_output()
    test_tail_reader_caps_at_fixed_bytes_on_a_large_transcript()
    test_hub_refuses_send_when_target_is_limited()
    # __main__ block at EOF picks them up by scanning globals. Keep both


def test_hub_refusal_now_carries_resets_at():
    hub = hub_mod.Hub(poll_timeout=5)
    hub.heartbeat("test_host_1", [
        {"persona": "test_host_5", "pid": 2222, "socket": "/tmp/test_host_5.sock",
         "name": "test_host_5-a1", "session_id": "sid-test_host_5",
         "started": time.time(),
         "limited": True, "limited_since": "2026-10-28T00:01:00Z",
         "limited_reason": "usage_limit_notice",
         "resets_at": "2026-10-28T12:40:00Z"},
    ], caps=[])
    r = hub.send("test_host_6", "tool_006", "test_host_1", "test_host_5", "hi")
    check("hub refusal carries resets_at",
          r.get("resets_at") == "2026-10-28T12:40:00Z", detail=str(r))

    # Backward-compat: an old shim that doesn't send resets_at → hub reports None.
    hub2 = hub_mod.Hub(poll_timeout=5)
    hub2.heartbeat("test_host_1", [
        {"persona": "test_host_5", "pid": 2222, "socket": "/tmp/test_host_5.sock",
         "name": "test_host_5-a1", "session_id": "sid-test_host_5",
         "started": time.time(),
         "limited": True, "limited_since": "2026-10-28T00:01:00Z",
         "limited_reason": "usage_limit_notice"},  # no resets_at
    ], caps=[])
    r2 = hub2.send("test_host_6", "tool_006", "test_host_1", "test_host_5", "hi")
    check("old-shim refusal reports resets_at=None (not missing)",
          "resets_at" in r2 and r2["resets_at"] is None, detail=str(r2))


def test_who_exposes_limit_state():
    """/who now carries limited, limited_reason, resets_at per-session so a
    sender can gate its own retries without a round-trip."""
    hub = hub_mod.Hub(poll_timeout=5)
    hub.heartbeat("test_host_1", [
        {"persona": "test_host_5", "pid": 2222, "socket": "/tmp/test_host_5.sock",
         "name": "test_host_5-a1", "session_id": "sid-test_host_5",
         "started": time.time(),
         "limited": True, "limited_since": "2026-10-28T00:01:00Z",
         "limited_reason": "usage_limit_notice",
         "resets_at": "2026-10-28T12:40:00Z"},
    ], caps=[])
    w = hub.who()
    row = w["test_host_1"]["test_host_5"][0]
    check("/who row has limited=True",
          row.get("limited") is True, detail=str(row))
    check("/who row has limited_reason=usage_limit_notice",
          row.get("limited_reason") == "usage_limit_notice", detail=str(row))
    check("/who row has resets_at set",
          row.get("resets_at") == "2026-10-28T12:40:00Z", detail=str(row))


def test_phase2_send_to_limited_target_enqueues_with_ticket():
    """Phase2 send to limited target enqueues with ticket."""
    hub = hub_mod.Hub(poll_timeout=5)
    hub.heartbeat("test_host_1", [
        {"persona": "test_host_5", "pid": 2222, "socket": "/tmp/test_host_5.sock",
         "name": "test_host_5-a1", "session_id": "sid-test_host_5",
         "started": time.time(),
         "limited": True, "limited_since": "2026-10-28T00:01:00Z",
         "limited_reason": "usage_limit_notice",
         "resets_at": "2026-10-28T12:40:00Z"},
    ], caps=[])
    r = hub.send("test_host_6", "tool_006", "test_host_1", "test_host_5", "hi phase2")
    check("send to limited target queues",
          r.get("queued") is True and r.get("ticket", "").startswith("q-"),
          detail=str(r))
    check("send still reports ok:false (not delivered yet)",
          r.get("ok") is False and r.get("delivered") is False, detail=str(r))
    check("limit_queue holds the enqueued message",
          len(hub.limit_queue["test_host_1"].get("test_host_5:2222", [])) == 1,
          detail=str(dict(hub.limit_queue["test_host_1"])))


def test_phase2_heartbeat_clear_flushes_queued_messages():
    """When a limited pid's next heartbeat reports limited:false AND the
    session's inbox socket is live, the hub drains its queue in FIFO order
    via /send. Here we assert the queue empties on the clear-transition;
    the actual delivery into a nonexistent socket is caught in the ack
    path (not tested here since there's no fake shim listener)."""
    hub = hub_mod.Hub(poll_timeout=5)
    hub.heartbeat("test_host_1", [
        {"persona": "test_host_5", "pid": 2222, "socket": "/tmp/test_host_5.sock",
         "name": "test_host_5-a1", "session_id": "sid-test_host_5",
         "started": time.time(),
         "limited": True, "limited_since": "2026-10-28T00:01:00Z",
         "limited_reason": "usage_limit_notice",
         "resets_at": "2026-10-28T12:40:00Z"},
    ], caps=[])
    # Enqueue two messages.
    hub.send("test_host_6", "tool_006", "test_host_1", "test_host_5", "msg one")
    hub.send("test_host_6", "tool_006", "test_host_1", "test_host_5", "msg two")
    check("two messages queued before clear",
          len(hub.limit_queue["test_host_1"].get("test_host_5:2222", [])) == 2)
    # Second heartbeat: same pid, limited:false → flush.
    hub.heartbeat("test_host_1", [
        {"persona": "test_host_5", "pid": 2222, "socket": "/tmp/test_host_5.sock",
         "name": "test_host_5-a1", "session_id": "sid-test_host_5",
         "started": time.time(),
         "limited": False},
    ], caps=[])
    check("queue emptied after clear-transition heartbeat",
          "test_host_5:2222" not in hub.limit_queue.get("test_host_1", {}),
          detail=str(dict(hub.limit_queue.get("test_host_1", {}))))


def test_phase2_queue_cap_drops_oldest_not_newest():
    """When LIMIT_QUEUE_MAX would be exceeded, drop the OLDEST entry
    (FIFO victim) so the sender's most-recent intent is preserved. The
    drop count surfaces via inflight_dropped (reused counter until
    phase-2b adds a dedicated field)."""
    hub = hub_mod.Hub(poll_timeout=5)
    hub.LIMIT_QUEUE_MAX = 3   # tight cap for testing
    hub.heartbeat("test_host_1", [
        {"persona": "test_host_5", "pid": 2222, "socket": "/tmp/test_host_5.sock",
         "name": "test_host_5-a1", "session_id": "sid-test_host_5",
         "started": time.time(),
         "limited": True, "limited_since": "2026-10-28T00:01:00Z",
         "limited_reason": "usage_limit_notice",
         "resets_at": "2026-10-28T12:40:00Z"},
    ], caps=[])
    for i in range(5):
        hub.send("test_host_6", "tool_006", "test_host_1", "test_host_5", f"msg {i}")
    q = hub.limit_queue["test_host_1"]["test_host_5:2222"]
    check("queue capped at LIMIT_QUEUE_MAX",
          len(q) == 3, detail=f"len={len(q)}")
    # The two oldest ("msg 0" and "msg 1") were dropped.
    check("oldest-first drop preserves newest intent",
          q[-1]["message"] == "msg 4" and q[0]["message"] == "msg 2",
          detail=str([m["message"] for m in q]))


def test_phase2b_overflow_drop_enqueues_receipt_to_original_sender():
    """Phase2b overflow drop enqueues receipt to original sender."""
    hub = hub_mod.Hub(poll_timeout=5)
    hub.LIMIT_QUEUE_MAX = 2
    hub.heartbeat("test_host_1", [
        {"persona": "test_host_5", "pid": 2222, "socket": "/tmp/test_host_5.sock",
         "name": "test_host_5-a1", "session_id": "sid-test_host_5",
         "started": time.time(),
         "limited": True, "limited_reason": "usage_limit_notice",
         "resets_at": "2026-10-28T12:40:00Z"},
    ], caps=[])
    hub.heartbeat("test_host_6", [
        {"persona": "tool_006", "pid": 7777, "socket": "/tmp/tool_006.sock",
         "name": "tool_006-7a", "session_id": "sid-tool_006",
         "started": time.time()},
    ], caps=[])
    # Three sends; the third pushes LIMIT_QUEUE_MAX, so the FIRST drops.
    r1 = hub.send("test_host_6", "tool_006", "test_host_1", "test_host_5", "msg one",
                   extra={"from_pid": 7777})
    r2 = hub.send("test_host_6", "tool_006", "test_host_1", "test_host_5", "msg two",
                   extra={"from_pid": 7777})
    r3 = hub.send("test_host_6", "tool_006", "test_host_1", "test_host_5", "msg three",
                   extra={"from_pid": 7777})
    host_039_outbox = list(hub.outbox.get("test_host_6", []))
    receipts = [m for m in host_039_outbox if m.get("system_receipt")]
    check("one receipt enqueued to sender for the dropped oldest",
          len(receipts) == 1, detail=str([m.get("message") for m in host_039_outbox]))
    import json as _json
    body = _json.loads(receipts[0]["message"])
    check("receipt event is queue_overflow_dropped",
          body["event"] == "queue_overflow_dropped", detail=str(body))
    check("receipt carries the dropped ticket id",
          body["ticket"] == r1["ticket"], detail=str(body))
    check("receipt is addressed to the original sender's persona",
          receipts[0]["to_persona"] == "tool_006", detail=str(receipts[0]))


def test_phase2b_ttl_expiry_enqueues_receipt_to_original_sender():
    """Phase2b ttl expiry enqueues receipt to original sender."""
    hub = hub_mod.Hub(poll_timeout=5)
    hub.heartbeat("test_host_1", [
        {"persona": "test_host_5", "pid": 2222, "socket": "/tmp/test_host_5.sock",
         "name": "test_host_5-a1", "session_id": "sid-test_host_5",
         "started": time.time(),
         "limited": True, "limited_reason": "usage_limit_notice",
         "resets_at": "2026-10-28T12:40:00Z"},
    ], caps=[])
    hub.heartbeat("test_host_6", [
        {"persona": "tool_006", "pid": 7777, "socket": "/tmp/tool_006.sock",
         "name": "tool_006-7a", "session_id": "sid-tool_006",
         "started": time.time()},
    ], caps=[])
    r = hub.send("test_host_6", "tool_006", "test_host_1", "test_host_5", "will expire",
                  extra={"from_pid": 7777})
    # Force the ticket into the past.
    qlist = hub.limit_queue["test_host_1"]["test_host_5:2222"]
    qlist[0]["expires_at"] = time.time() - 10
    # Next heartbeat sweeps.
    hub.heartbeat("test_host_1", [
        {"persona": "test_host_5", "pid": 2222, "socket": "/tmp/test_host_5.sock",
         "name": "test_host_5-a1", "session_id": "sid-test_host_5",
         "started": time.time(),
         "limited": True, "limited_reason": "usage_limit_notice",
         "resets_at": "2026-10-28T12:40:00Z"},
    ], caps=[])
    import json as _json
    receipts = [m for m in hub.outbox.get("test_host_6", []) if m.get("system_receipt")]
    expired = [r for r in receipts if _json.loads(r["message"])["event"] == "limit_queue_expired"]
    check("one expired-receipt enqueued to sender",
          len(expired) == 1, detail=str([m.get("message") for m in receipts]))
    body = _json.loads(expired[0]["message"])
    check("expired receipt carries the ticket",
          body["ticket"] == r["ticket"], detail=str(body))


def test_phase3_wake_scheduler_fires_at_resets_at_once_per_interval():
    """Phase3 wake scheduler fires at resets at once per interval."""
    shim_mod._WAKE_STATE.clear()
    s = {"pid": 2222, "persona": "test_host_5", "socket": "/tmp/test_host_5.sock",
         "limited": True, "limited_reason": "usage_limit_notice",
         "resets_at": "2026-10-28T12:40:00Z"}
    import datetime as _dt
    resets_ts = _dt.datetime(2026, 10, 28, 12, 40, 0, tzinfo=_dt.timezone.utc).timestamp()
    # Before resets_at: no fire.
    fires = shim_mod._wake_scheduler_tick([s], now=resets_ts - 10)
    check("no fire before resets_at", len(fires) == 0)
    # At resets_at: 1st fire.
    fires = shim_mod._wake_scheduler_tick([s], now=resets_ts)
    check("one fire at resets_at", len(fires) == 1 and fires[0]["pid"] == 2222)
    # Same instant again: NO re-fire (next_fire_ts advanced by interval).
    fires = shim_mod._wake_scheduler_tick([s], now=resets_ts + 1)
    check("no re-fire within interval",
          len(fires) == 0,
          detail=f"state={shim_mod._WAKE_STATE.get(2222)}")
    # After interval elapsed: 2nd fire.
    fires = shim_mod._wake_scheduler_tick([s], now=resets_ts + shim_mod.WAKE_RETRY_INTERVAL_S + 1)
    check("second fire after interval", len(fires) == 1)
    # Burn through remaining retries.
    for _ in range(shim_mod.WAKE_MAX_RETRIES - 2):
        shim_mod._wake_scheduler_tick(
            [s], now=resets_ts + 10 * shim_mod.WAKE_RETRY_INTERVAL_S)
    check("stops firing after WAKE_MAX_RETRIES",
          shim_mod._WAKE_STATE[2222]["retries"] == shim_mod.WAKE_MAX_RETRIES)
    # One more tick: still no fire (retries cap).
    fires = shim_mod._wake_scheduler_tick(
        [s], now=resets_ts + 100 * shim_mod.WAKE_RETRY_INTERVAL_S)
    check("no fire past WAKE_MAX_RETRIES", len(fires) == 0)
    check("unresponsive helper names the capped pid",
          2222 in shim_mod._wake_unresponsive_pids([s]))


def test_phase3_wake_state_clears_on_recovery():
    """When a session's next heartbeat reports limited:false, the shim's
    local wake state for that pid is cleared so a future limit starts
    the retry count at zero."""
    shim_mod._WAKE_STATE.clear()
    import datetime as _dt
    resets_ts = _dt.datetime(2026, 10, 28, 12, 40, 0, tzinfo=_dt.timezone.utc).timestamp()
    s = {"pid": 2222, "persona": "test_host_5", "socket": "/tmp/test_host_5.sock",
         "limited": True, "limited_reason": "usage_limit_notice",
         "resets_at": "2026-10-28T12:40:00Z"}
    shim_mod._wake_scheduler_tick([s], now=resets_ts)
    check("state recorded on first fire", 2222 in shim_mod._WAKE_STATE)
    # Session recovers.
    s2 = {"pid": 2222, "persona": "test_host_5", "socket": "/tmp/test_host_5.sock",
          "limited": False}
    shim_mod._wake_scheduler_tick([s2], now=resets_ts + 1)
    check("state cleared on recovery", 2222 not in shim_mod._WAKE_STATE)


def test_phase3_fire_wake_builds_fixed_template_body():
    """The wake message body is JSON with exactly these keys:
      wake, resets_at, ticket_ids. No prose, no LLM-generated content."""
    captured: dict = {}
    def fake_native_send(sock, from_name, body, window=None, pid=None, human=False):
        captured.update(dict(sock=sock, from_name=from_name, body=body,
                              window=window, pid=pid, human=human))
    import unittest.mock as _mock
    s = {"pid": 2222, "persona": "test_host_5", "socket": "/tmp/test_host_5.sock",
         "limited": True, "resets_at": "2026-10-28T12:40:00Z"}
    with _mock.patch.object(shim_mod, "native_send", fake_native_send):
        shim_mod._fire_wake(s, ticket_ids=["q-11", "q-22"])
    import json as _json
    body = _json.loads(captured["body"])
    check("body is a dict with the fixed keys only",
          set(body.keys()) == {"wake", "resets_at", "ticket_ids"},
          detail=str(body))
    check("wake is true", body["wake"] is True)
    check("ticket_ids survive",
          body["ticket_ids"] == ["q-11", "q-22"])
    check("from_name is the hub system identity",
          captured["from_name"] == "limit-queue@hub")
    check("not routed as human",
          captured["human"] is False)


def test_phase3b_dead_pid_relaunches_via_agents_cli():
    """Phase3b dead pid relaunches via agents cli."""
    shim_mod._WAKE_STATE.clear()
    shim_mod._LAST_SEEN_LIMITED.clear()
    s = {"pid": 4444, "persona": "test_user_7", "socket": "/tmp/test_user_7.sock",
         "limited": True, "limited_reason": "usage_limit_notice",
         "resets_at": "2026-10-28T12:40:00Z"}
    # Tick 1: pid limited and alive — captured in _LAST_SEEN_LIMITED.
    shim_mod._wake_scheduler_tick([s], now=time.time())
    check("tick 1 registers pid in _LAST_SEEN_LIMITED",
          shim_mod._LAST_SEEN_LIMITED.get(4444) == "test_user_7")
    # Tick 2: pid vanished → dead-persona emitted on side-channel.
    shim_mod._wake_scheduler_tick([], now=time.time())
    deads = getattr(shim_mod._wake_scheduler_tick, "last_dead_personas", [])
    check("dead persona emitted on side-channel after disappearance",
          "test_user_7" in deads, detail=str(deads))
    check("state purged after dead-detection",
          4444 not in shim_mod._WAKE_STATE and 4444 not in shim_mod._LAST_SEEN_LIMITED)
    # Launcher argv — override binary with a fake and assert argv.
    import subprocess, tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False) as f:
        f.write("#!/bin/sh\necho \"$@\" > " + f.name + ".log\n")
        fake_path = f.name
    import os as _os
    _os.chmod(fake_path, 0o755)
    old_bin = shim_mod.AGENTS_LAUNCH_BIN
    try:
        shim_mod.AGENTS_LAUNCH_BIN = fake_path
        argv = shim_mod._launch_dead_persona("test_user_7")
    finally:
        shim_mod.AGENTS_LAUNCH_BIN = old_bin
    check("argv structure is [binary, 'launch', persona]",
          argv == [fake_path, "launch", "test_user_7"], detail=str(argv))


def test_phase3b_launcher_missing_logs_but_does_not_crash():
    """Phase3b launcher missing logs but does not crash."""
    old_bin = shim_mod.AGENTS_LAUNCH_BIN
    try:
        shim_mod.AGENTS_LAUNCH_BIN = "/nonexistent/bogus-agents-xyz"
        argv = shim_mod._launch_dead_persona("test_user_7")   # must not raise
    finally:
        shim_mod.AGENTS_LAUNCH_BIN = old_bin
    check("argv still returned when launcher is missing",
          argv == ["/nonexistent/bogus-agents-xyz", "launch", "test_user_7"])


def test_phase3b_hub_unresponsive_hb_mints_receipt_and_drops_queue():
    """Hub receives `unresponsive_pids` in a heartbeat → for each queued
    sender of that (host, pid), mints a queued_failed_unresponsive
    receipt and drops the queue entries. This closes the loop: the
    sender knows the target is unresponsive, not just quiet."""
    hub = hub_mod.Hub(poll_timeout=5)
    hub.heartbeat("test_host_1", [
        {"persona": "test_host_5", "pid": 2222, "socket": "/tmp/test_host_5.sock",
         "name": "test_host_5-a1", "session_id": "sid-test_host_5",
         "started": time.time(),
         "limited": True, "limited_reason": "usage_limit_notice",
         "resets_at": "2026-10-28T12:40:00Z"},
    ], caps=[])
    hub.heartbeat("test_host_6", [
        {"persona": "tool_006", "pid": 7777, "socket": "/tmp/tool_006.sock",
         "name": "tool_006-7a", "session_id": "sid-tool_006",
         "started": time.time()},
    ], caps=[])
    hub.send("test_host_6", "tool_006", "test_host_1", "test_host_5", "will fail unresponsive",
              extra={"from_pid": 7777})
    check("one entry queued pre-unresponsive",
          len(hub.limit_queue["test_host_1"].get("test_host_5:2222", [])) == 1)
    # Shim reports pid 2222 unresponsive in next heartbeat.
    hub.heartbeat("test_host_1", [
        {"persona": "test_host_5", "pid": 2222, "socket": "/tmp/test_host_5.sock",
         "name": "test_host_5-a1", "session_id": "sid-test_host_5",
         "started": time.time(),
         "limited": True, "limited_reason": "usage_limit_notice",
         "resets_at": "2026-10-28T12:40:00Z"},
    ], caps=[], unresponsive_pids=[2222])
    import json as _json
    receipts = [m for m in hub.outbox.get("test_host_6", []) if m.get("system_receipt")]
    unresponsive_receipts = [r for r in receipts
                             if _json.loads(r["message"])["event"] == "queued_failed_unresponsive"]
    check("one queued_failed_unresponsive receipt minted",
          len(unresponsive_receipts) == 1,
          detail=str([_json.loads(r["message"])["event"] for r in receipts]))
    check("queue entries dropped after unresponsive report",
          "test_host_5:2222" not in hub.limit_queue.get("test_host_1", {}))



# ---------------------------------------------------------------- stranded old-pid queue

def _limited_row(persona, pid, limited):
    row = {"persona": persona, "pid": pid, "socket": f"/tmp/{persona}-{pid}.sock",
           "name": f"{persona}-{pid}", "session_id": f"sid-{persona}", "started": time.time(),
           "limited": limited}
    if limited:
        row.update({"limited_reason": "session_limit", "resets_at": "2099-01-01T00:00:00Z",
                    "limited_since": "2026-01-01T00:00:00Z"})
    return row


def _queue_one(hub, persona, pid):
    hub.heartbeat("host-a", [_limited_row(persona, pid, True)], caps=[])
    hub.heartbeat("host-b", [{"persona": "sender", "pid": 7000, "socket": "/tmp/sender.sock",
                              "name": "sender-1", "session_id": "sid-sender",
                              "started": time.time()}], caps=[])
    hub.send("host-b", "sender", "host-a", persona, "held message", extra={"from_pid": 7000})


def test_stranded_queue_drains_to_relaunched_unlimited_pid():
    hub = hub_mod.Hub(poll_timeout=5)
    _queue_one(hub, "agent-x", 4001)
    check("queued under the old pid", len(hub.limit_queue["host-a"].get("agent-x:4001", [])) == 1)
    hub.outbox["host-a"] = []
    hub.heartbeat("host-a", [_limited_row("agent-x", 4002, False)], caps=[])
    check("old-pid key gone", "agent-x:4001" not in hub.limit_queue.get("host-a", {}))
    check("new-pid key not left holding it", not hub.limit_queue.get("host-a", {}).get("agent-x:4002"))
    check("held message delivered to the relaunched pid",
          any(m.get("message") == "held message" for m in hub.outbox.get("host-a", [])),
          detail=str(hub.outbox.get("host-a")))


def test_stranded_queue_requeues_under_relaunched_pid_that_is_still_limited():
    hub = hub_mod.Hub(poll_timeout=5)
    _queue_one(hub, "agent-y", 5001)
    hub.heartbeat("host-a", [_limited_row("agent-y", 5002, True)], caps=[])
    check("old-pid key gone", "agent-y:5001" not in hub.limit_queue.get("host-a", {}))
    check("ticket now queued under the live pid",
          len(hub.limit_queue["host-a"].get("agent-y:5002", [])) == 1)


def test_live_pid_queue_is_not_touched_by_rekey():
    hub = hub_mod.Hub(poll_timeout=5)
    _queue_one(hub, "agent-z", 6001)
    hub.heartbeat("host-a", [_limited_row("agent-z", 6001, True)], caps=[])
    check("queue stays under the same live pid",
          len(hub.limit_queue["host-a"].get("agent-z:6001", [])) == 1)




# and the reset instant the sentence names.
REAL_SAMPLES = [
    ("2026-10-29T10:21:20Z", "You've hit your weekly limit · resets 11pm (Asia/Singapore)", "weekly_limit", "2026-10-29T15:00:00Z"),
    ("2026-10-21T09:00:00Z", "You've hit your weekly limit · resets Oct 22 at 11pm (Asia/Singapore)", "weekly_limit", "2026-10-22T15:00:00Z"),
    ("2026-10-29T08:00:00Z", "You've hit your session limit · resets 8:30pm (Asia/Singapore)", "session_limit", "2026-10-29T12:30:00Z"),
    ("2026-10-29T14:00:00Z", "You've hit your session limit · resets 1am (Asia/Singapore)", "session_limit", "2026-10-29T17:00:00Z"),
    ("2026-10-29T14:00:00Z", "You've hit your session limit · resets 11:10pm (Asia/Singapore)", "session_limit", "2026-10-29T15:10:00Z"),
    ("2026-10-29T01:00:00Z", "You've hit your session limit · resets 11:30am (Asia/Singapore)", "session_limit", "2026-10-29T03:30:00Z"),
    ("2026-10-29T05:00:00Z", "You've hit your session limit · resets 6:30am (Asia/Singapore)", "session_limit", "2026-10-29T22:30:00Z"),
]


def test_every_real_sample_parses_to_its_reset():
    for ts, sentence, reason, resets in REAL_SAMPLES:
        got = shim_mod._limit_sentence(sentence, shim_mod._iso_ts(ts))
        check(f"sample {sentence!r} @ {ts}", got == (reason, resets), detail=str(got))


def test_other_api_errors_are_not_limited_and_do_not_hide_a_reply():
    root = pathlib.Path(tempfile.mkdtemp(prefix="limited-"))
    sid = "sess-plain-429"
    _write_jsonl(root, sid, [
        {"type": "assistant", "timestamp": "2026-10-29T00:00:00Z", "message": {"content": [{"text": "done"}]}},
        {"type": "assistant", "timestamp": "2026-10-29T00:01:00Z", "isApiErrorMessage": True,
         "apiErrorStatus": 429, "error": "rate_limit",
         "message": {"content": [{"text": "API Error: Rate limit reached"}]}},
    ])
    r = shim_mod.session_limited_state(sid, root=root, now=shim_mod._iso_ts("2026-10-29T00:02:00Z"))
    check("429 without a limit sentence => not limited, earlier reply still seen",
          r["limited"] is False and r["last_reply_at"] == "2026-10-29T00:00:00Z", detail=str(r))


def test_a_quoted_sentence_in_a_normal_reply_is_not_a_limit():
    root = pathlib.Path(tempfile.mkdtemp(prefix="limited-"))
    sid = "sess-quote"
    _write_jsonl(root, sid, [{"type": "assistant", "timestamp": "2026-10-29T00:00:00Z", "message": {"content": [
        {"text": "You've hit your session limit · resets 8:30pm (Asia/Singapore)"}]}}])
    r = shim_mod.session_limited_state(sid, root=root, now=shim_mod._iso_ts("2026-10-29T00:01:00Z"))
    check("the sentence outside an API-error record => not limited", r["limited"] is False, detail=str(r))


def test_unknown_zone_is_not_a_guess():
    check("unknown zone => None",
          shim_mod._limit_sentence("You've hit your session limit · resets 8pm (Mars/Olympus)",
                                   shim_mod._iso_ts("2026-10-29T00:00:00Z")) is None)


def _watch_setup():
    hub = hub_mod.Hub(poll_timeout=5)
    _queue_one(hub, "test_user_7", 4100)
    # the reset passed: the shim reports the same pid unlimited -> the queue is sent and watched
    hub.heartbeat("host-a", [_limited_row("test_user_7", 4100, False)], caps=[])
    return hub


def test_reset_send_then_reply_receipts_the_sender():
    hub = _watch_setup()
    check("sent at reset: watched", "test_user_7:4100" in hub.limit_watch["host-a"])
    row = _limited_row("test_user_7", 4100, False) | {"last_reply_at": "2099-01-01T00:00:00Z"}
    hub.heartbeat("host-a", [row], caps=[])
    receipts = [m for m in hub.outbox["host-b"] if "queued_delivered" in m.get("message", "")]
    check("reply after the send => queued_delivered receipt to the sender", len(receipts) == 1,
          detail=str(list(hub.outbox["host-b"])))
    check("watch cleared", "test_user_7:4100" not in hub.limit_watch["host-a"])


def test_reset_send_then_limit_again_requeues_under_the_new_reset():
    hub = _watch_setup()
    row = _limited_row("test_user_7", 4100, True) | {"limited_since": "2099-01-01T00:00:00Z",
                                              "resets_at": "2099-01-02T00:00:00Z"}
    hub.heartbeat("host-a", [row], caps=[])
    q = hub.limit_queue["host-a"].get("test_user_7:4100", [])
    check("limit sentence again => message back in the queue with the new reset",
          len(q) == 1 and q[0]["resets_at"] == "2099-01-02T00:00:00Z", detail=str(q))
    check("watch cleared", "test_user_7:4100" not in hub.limit_watch["host-a"])

if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print(f"\n{len(PASS)} PASS, {len(FAIL)} FAIL")
    sys.exit(0 if not FAIL else 1)
