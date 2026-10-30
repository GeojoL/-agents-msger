"""Session model label reporting."""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import hub as hub_mod        # noqa: E402
import shim as shim_mod       # noqa: E402


def _transcript(lines):
    root = pathlib.Path(tempfile.mkdtemp(prefix="model-"))
    sid = "sess-model"
    d = root / "projects" / "fake-slug"
    d.mkdir(parents=True)
    (d / f"{sid}.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    return [root / "projects"], sid


def _assistant(ts, model):
    return {"type": "assistant", "timestamp": ts, "message": {"model": model, "content": [{"text": "x"}]}}


DIRECT = lambda pid: (True, None)                                   # noqa: E731
GATEWAY = lambda pid: (True, "http://gw-host:4000")                 # noqa: E731
UNREADABLE = lambda pid: (False, None)                              # noqa: E731


def test_direct_claude_session_is_anthropic_with_the_transcript_model():
    roots, sid = _transcript([_assistant("2026-01-01T00:00:00Z", "claude-model-a")])
    m = shim_mod.session_model(4242, sid, roots=roots, base_url_reader=DIRECT)
    assert m == {"provider": "anthropic", "id": "claude-model-a", "label": "anthropic/claude-model-a"}, m


def test_gateway_session_reports_the_gateway_and_its_route():
    roots, sid = _transcript([_assistant("2026-01-01T00:00:00Z", "local-route")])
    m = shim_mod.session_model(4242, sid, roots=roots, base_url_reader=GATEWAY)
    assert m["provider"] == "gateway:gw-host:4000" and m["id"] == "local-route", m


def test_a_model_switch_follows_the_newest_assistant_record():
    roots, sid = _transcript([_assistant("2026-01-01T00:00:00Z", "model-before"),
                              {"type": "user", "timestamp": "2026-01-01T00:01:00Z", "message": {"content": "/model"}},
                              _assistant("2026-01-01T00:02:00Z", "model-after"),
                              {"type": "assistant", "timestamp": "2026-01-01T00:03:00Z",
                               "message": {"model": "<synthetic>", "content": [{"text": "notice"}]}}])
    m = shim_mod.session_model(4242, sid, roots=roots, base_url_reader=DIRECT)
    assert m["id"] == "model-after", m


def test_nothing_detectable_is_unknown_not_guessed():
    roots, _ = _transcript([{"type": "user", "timestamp": "2026-01-01T00:00:00Z", "message": {"content": "hi"}}])
    m = shim_mod.session_model(4242, "no-such-session", roots=roots, base_url_reader=UNREADABLE)
    assert m == {"provider": "unknown", "id": "unknown", "label": "unknown/unknown"}, m


def _hub_with(rows_by_host):
    hub = hub_mod.Hub(poll_timeout=5)
    for host, rows in rows_by_host.items():
        hub.heartbeat(host, rows, caps=[])
    return hub


def _row(persona, pid, label):
    return {"persona": persona, "pid": pid, "socket": f"/tmp/{persona}.sock", "name": f"{persona}-1",
            "session_id": f"sid-{persona}", "started": 1.0,
            "model": {"provider": label.split("/")[0], "id": label.split("/", 1)[1], "label": label}}


def test_who_receipt_and_header_carry_the_registered_model():
    hub = _hub_with({"host-a": [_row("agent-x", 11, "gateway:gw-host:4000/local-route")],
                     "host-b": [_row("sender", 22, "anthropic/claude-model-a")]})
    who = hub.who()
    assert who["host-a"]["agent-x"][0]["model"] == "gateway:gw-host:4000/local-route", who
    r = hub.send("host-b", "sender", "host-a", "agent-x", "hi", {"from_pid": 22})
    assert r["to"]["model"] == "gateway:gw-host:4000/local-route", r
    msg = [m for m in hub.outbox["host-a"] if m.get("message") == "hi"][0]
    assert msg["from_model"] == "anthropic/claude-model-a", msg


def test_a_sender_the_hub_does_not_know_is_unknown_whatever_it_claims():
    hub = _hub_with({"host-a": [_row("agent-x", 11, "anthropic/claude-model-a")]})
    hub.send("host-b", "sender", "host-a", "agent-x", "hi", {"from_pid": 99, "from_model": "anthropic/claimed"})
    msg = [m for m in hub.outbox["host-a"] if m.get("message") == "hi"][0]
    assert msg["from_model"] == "unknown", msg


def test_the_delivered_header_names_the_sender_model():
    sent = {}

    class FakeSock:
        def __init__(self, *a): pass
        def settimeout(self, t): pass
        def connect(self, path): sent["path"] = path
        def sendall(self, data): sent["data"] = data
        def close(self): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False

    import socket as socket_mod
    orig = socket_mod.socket
    socket_mod.socket = FakeSock
    try:
        shim_mod.native_send("/tmp/none.sock", "sender@host-b#sender-1", "body", pid=22,
                             model="anthropic/claude-model-a")
    finally:
        socket_mod.socket = orig
    content = json.loads(sent["data"])["message"]["content"]
    assert 'from-model="anthropic/claude-model-a"' in content, content


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS", name)
