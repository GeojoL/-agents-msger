"""Human input delivery and webchat process recognition."""
from __future__ import annotations

import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import shim as shim_mod
shim_mod._webchat_anchor = lambda: "example-webchat"  # noqa: E402


def check(label, cond, detail=""):
    assert cond, f"{label}: {detail}"


# ---- relay_send sets `human` from runtime detection, not caller input ----

def _capture_post():
    """Replace shim._post with a capture that returns a dummy ok."""
    captured = {}
    def fake_post(path, payload, timeout=8):
        captured["path"] = path
        captured["payload"] = payload
        return {"ok": True, "delivered": True}
    orig = shim_mod._post
    shim_mod._post = fake_post
    return captured, orig


def _restore_post(orig):
    shim_mod._post = orig


def _set_identity(**kw):
    """Replace shim.self_identity with a stub."""
    orig = shim_mod.self_identity
    base = {"agent": "webchat", "session": "abcd1234", "window": None,
            "pid": 123, "host": "test_host_1", "from_name": "webchat@test_host_1#abcd1234",
            "registered": True, "runtime": "unknown"}
    base.update(kw)
    shim_mod.self_identity = lambda start_pid=None: dict(base)
    return orig


def test_webchat_sender_marks_human_and_carries_sid():
    cap, orig_post = _capture_post()
    orig_id = _set_identity(runtime="webchat", session="abcd1234",
                             from_name="webchat@test_host_1#abcd1234")
    try:
        shim_mod.relay_send("test_user_3@test_host_1", "hi")
    finally:
        _restore_post(orig_post)
        shim_mod.self_identity = orig_id
    p = cap["payload"]
    check("human=True set", p.get("human") is True, repr(p))
    check("from_session_id set to webchat sid",
          p.get("from_session_id") == "abcd1234", repr(p))


def test_non_webchat_sender_omits_human_and_sid():
    cap, orig_post = _capture_post()
    orig_id = _set_identity(runtime="claude", from_name="test_agent_8#test_agent_9-a1")
    try:
        shim_mod.relay_send("test_user_3@test_host_1", "hi")
    finally:
        _restore_post(orig_post)
        shim_mod.self_identity = orig_id
    p = cap["payload"]
    check("human NOT set", "human" not in p, repr(p))
    check("from_session_id NOT set", "from_session_id" not in p, repr(p))


def test_relay_send_signature_rejects_caller_human_arg():
    """A caller cannot pass `human` through relay_send; the function
    signature does not accept it. If this changes, the check fails."""
    import inspect
    sig = inspect.signature(shim_mod.relay_send)
    check("relay_send has no `human` param", "human" not in sig.parameters,
          repr(list(sig.parameters)))


# ---- native_send body shape differs for human vs non-human ----

def test_native_send_human_sends_plain_body_no_wrapper():
    """When human=True, the content delivered is the raw body — no
    <cross-session-message> wrapper. This is what makes the receiving
    session read the message as if the person had typed it."""
    import json as _json
    import socket as _socket
    import threading as _threading
    import tempfile as _tempfile
    sock_dir = _tempfile.mkdtemp(prefix="hum-", dir="/tmp")
    sock_path = os.path.join(sock_dir, "s.sock")
    srv = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    srv.bind(sock_path)
    srv.listen(1)
    received = []

    def accept_one():
        c, _ = srv.accept()
        with c:
            buf = b""
            while b"\n" not in buf:
                chunk = c.recv(65536)
                if not chunk:
                    break
                buf += chunk
        received.append(_json.loads(buf.split(b"\n", 1)[0].decode()))

    t = _threading.Thread(target=accept_one, daemon=True)
    t.start()
    shim_mod.native_send(sock_path, "webchat@test_host_1#abcd1234",
                          "do the thing", human=True)
    t.join(timeout=3)
    srv.close()
    check("one frame received", len(received) == 1, str(received))
    content = received[0]["message"]["content"]
    check("human body is the plain message", content == "do the thing",
          repr(content))
    check("no cross-session-message wrapper",
          "<cross-session-message" not in content, repr(content))


def test_native_send_non_human_still_wraps():
    """Regression guard: non-human agent-to-agent messages keep the
    wrapper (preserves provenance for the receiving model)."""
    import json as _json
    import socket as _socket
    import threading as _threading
    import tempfile as _tempfile
    sock_dir = _tempfile.mkdtemp(prefix="hum-", dir="/tmp")
    sock_path = os.path.join(sock_dir, "s.sock")
    srv = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    srv.bind(sock_path)
    srv.listen(1)
    received = []

    def accept_one():
        c, _ = srv.accept()
        with c:
            buf = b""
            while b"\n" not in buf:
                chunk = c.recv(65536)
                if not chunk:
                    break
                buf += chunk
        received.append(_json.loads(buf.split(b"\n", 1)[0].decode()))

    t = _threading.Thread(target=accept_one, daemon=True)
    t.start()
    shim_mod.native_send(sock_path, "test_agent_8", "hi",
                          window="test_agent_9", pid=99)
    t.join(timeout=3)
    srv.close()
    content = received[0]["message"]["content"]
    check("wrapper present", "<cross-session-message" in content, repr(content))
    check("body carried", "hi" in content, repr(content))


# ---- deliver passes human through to native_send + logs with session_id ----

def test_deliver_passes_human_through():
    sent = []
    sess = {"persona": "test_user_3", "socket": "/tmp/x.sock", "name": "test_user_3-1",
            "pid": 1, "limited": False, "limited_since": None}
    orig = (shim_mod.live_sessions, shim_mod.native_send, shim_mod._ack)
    shim_mod.live_sessions = lambda: [sess]
    shim_mod.native_send = lambda sock, frm, body, window=None, pid=None, human=False, model=None: sent.append(
        {"sock": sock, "body": body, "human": human, "frm": frm})
    shim_mod._ack = lambda mid, ok, error="": None
    try:
        shim_mod.deliver({"id": "7", "to_persona": "test_user_3",
                          "from_name": "webchat@test_host_1#abcd1234",
                          "message": "please do X",
                          "human": True,
                          "from_session_id": "abcd1234"})
    finally:
        shim_mod.live_sessions, shim_mod.native_send, shim_mod._ack = orig
    check("deliver propagated human=True", sent and sent[0]["human"] is True,
          str(sent))
    check("deliver propagated plain body", sent[0]["body"] == "please do X",
          str(sent))



def test_hub_drops_human_when_peer_ip_mismatches_registered_shim():
    """Another host on the same network that POSTs /send with human:true but a
    peer IP that doesn't match from_host's registered shim gets its
    `human` flag silently stripped. Message still routes."""
    import sys as _sys, pathlib as _p
    _sys.path.insert(0, str(_p.Path(__file__).resolve().parent.parent))
    from hub import Hub  # noqa: E402

    h = Hub(poll_timeout=1)
    h.heartbeat("test_host_1",
                sessions=[
                    {"pid": 1, "persona": "test_user_3",
                     "socket": "/tmp/x.sock", "name": "test_user_3-1"},
                    {"pid": 2, "persona": "webchat",
                     "socket": "/tmp/clw.sock", "name": "aaaa5678",
                     "session_id": "aaaa5678abcd"},
                ],
                caps=["ack"], peer_ip="198.51.100.1")
    # session): human survives.
    h.send("test_host_1", "webchat@test_host_1#aaaa5678", "test_host_1", "test_user_3", "hello",
           extra={"human": True, "from_session_id": "aaaa5678abcd",
                  "peer_ip": "198.51.100.1"})
    outbox_a = list(h.outbox["test_host_1"])
    check("legit peer IP + registered sid keeps human",
          outbox_a[-1].get("human") is True, str(outbox_a[-1]))

    # Impersonator from a different IP: human is dropped.
    h.send("test_host_1", "webchat@test_host_1#aaaa5678", "test_host_1", "test_user_3", "hi again",
           extra={"human": True, "from_session_id": "aaaa5678abcd",
                  "peer_ip": "198.51.100.99"})
    outbox_b = list(h.outbox["test_host_1"])
    check("impersonator peer IP has human stripped",
          outbox_b[-1].get("human") is None, str(outbox_b[-1]))
    check("impersonator's message still routes",
          outbox_b[-1].get("message") == "hi again", str(outbox_b[-1]))


def test_hub_drops_human_when_no_shim_ip_registered():
    """A /send that arrives BEFORE from_host's shim has heartbeated
    (no registered IP yet) has `human` stripped — fail-closed on the
    trust claim, fail-open on routing."""
    import sys as _sys, pathlib as _p
    _sys.path.insert(0, str(_p.Path(__file__).resolve().parent.parent))
    from hub import Hub  # noqa: E402
    h = Hub(poll_timeout=1)
    # Only target heartbeat; from_host's shim never checked in.
    h.heartbeat("test_host_1", sessions=[{"pid": 1, "persona": "test_user_3",
                                      "socket": "/tmp/x.sock", "name": "test_user_3-1"}],
                caps=["ack"], peer_ip="198.51.100.1")
    h.send("remote-host-nowhere", "webchat@remote#x", "test_host_1", "test_user_3", "hi",
           extra={"human": True, "peer_ip": "198.51.100.42"})
    outbox = list(h.outbox["test_host_1"])
    check("no shim registration → human stripped",
          outbox[-1].get("human") is None, str(outbox[-1]))



def test_webchat_detection_requires_two_machine_facts():
    """Webchat detection requires two machine facts."""
    import tempfile as _tempfile, pathlib as _pathlib
    import json as _json

    # Fake process readers, configurable per case.
    recorded_pid = 42
    cmd = f"python {_tempfile.gettempdir()}/example-webchat/session.py /nonexistent/spec.json"
    orig_proc = shim_mod._proc_info
    orig_reg = shim_mod._reg_json_by_pid
    orig_argv = shim_mod._read_proc_argv
    # _is_webchat_pid(42) reads this argv → spec path /nonexistent → spec read
    # fails → not webchat. No registered anchor, exe is python → no runtime stop.
    shim_mod._proc_info = lambda p: (1, 1_000_000_000.0, "/usr/bin/python3") if p == recorded_pid else None
    shim_mod._reg_json_by_pid = lambda: {}
    shim_mod._read_proc_argv = lambda p: cmd.split() if p == recorded_pid else []
    try:
        pid, kind = shim_mod._find_runtime_pid(start_pid=recorded_pid)
        # Spec file doesn't exist → webchat rejected → fall through to
        # None (no anchored ancestor matched).
        check("webchat rejected when spec missing",
              kind != "webchat", f"{(pid, kind)}")
    finally:
        shim_mod._proc_info = orig_proc
        shim_mod._reg_json_by_pid = orig_reg
        shim_mod._read_proc_argv = orig_argv


def test_hub_drops_human_when_from_session_id_not_in_registered_webchats():
    """Hub drops human when from session id not in registered webchats."""
    import sys as _sys, pathlib as _p
    _sys.path.insert(0, str(_p.Path(__file__).resolve().parent.parent))
    from hub import Hub  # noqa: E402
    h = Hub(poll_timeout=1)
    # NO webchat session. Any human claim must fail.
    h.heartbeat("test_host_1",
                sessions=[{"pid": 1, "persona": "test_user_3",
                           "socket": "/tmp/x.sock", "name": "test_user_3-1"}],
                caps=["ack"], peer_ip="198.51.100.1")
    h.send("test_host_1", "webchat@test_host_1#forged", "test_host_1", "test_user_3", "hi",
           extra={"human": True, "from_session_id": "ffffffff",
                  "peer_ip": "198.51.100.1"})  # matches shim IP, but sid unregistered
    outbox = list(h.outbox["test_host_1"])
    check("unregistered sid → human stripped",
          outbox[-1].get("human") is None, str(outbox[-1]))

    h.heartbeat("test_host_1",
                sessions=[
                    {"pid": 1, "persona": "test_user_3",
                     "socket": "/tmp/x.sock", "name": "test_user_3-1"},
                    {"pid": 2, "persona": "webchat",
                     "socket": "/tmp/clw.sock", "name": "bbbb",
                     "session_id": "bbbbbbbb-full"},
                ],
                caps=["ack"], peer_ip="198.51.100.1")
    h.send("test_host_1", "webchat@test_host_1#bbbb", "test_host_1", "test_user_3", "hi now",
           extra={"human": True, "from_session_id": "bbbbbbbb-full",
                  "peer_ip": "198.51.100.1"})
    outbox2 = list(h.outbox["test_host_1"])
    check("registered sid → human kept",
          outbox2[-1].get("human") is True, str(outbox2[-1]))


if __name__ == "__main__":
    passed, failed = [], []
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                passed.append(name)
                print("PASS " + name)
            except Exception as e:  # noqa: BLE001
                failed.append(name)
                print("FAIL " + name + ": " + str(e)[:300])
    print(f"{len(passed)} passed, {len(failed)} failed")
    sys.exit(1 if failed else 0)
