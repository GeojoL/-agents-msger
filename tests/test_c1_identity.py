#!/usr/bin/env python3
"""Identity addressing: same-persona sessions, #match routing, fail-loud sends and acks."""
from __future__ import annotations

import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import threading
import time

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

import hub as hub_mod        # noqa: E402
import shim as shim_mod       # noqa: E402


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

class _FakeAgentsRoot:
    """Context manager: make the shim see `agents_abs` as the agents root and clear the
    60s root cache so the fake root takes effect at once. Fixture only."""

    def __init__(self, agents_abs: str = "/fake/agents"):
        self.agents_abs = agents_abs

    def __enter__(self):
        self._saved_read = shim_mod._read_agents_root
        self._saved_cache = dict(shim_mod._AGENTS_ROOT_CACHE)
        root = pathlib.Path(self.agents_abs)
        shim_mod._read_agents_root = lambda: root
        shim_mod._AGENTS_ROOT_CACHE.update({"ts": 0.0, "data": None, "last_good": None})
        return self

    def __exit__(self, *exc):
        shim_mod._read_agents_root = self._saved_read
        shim_mod._AGENTS_ROOT_CACHE.clear()
        shim_mod._AGENTS_ROOT_CACHE.update(self._saved_cache)
        return False


class FakeUDS:
    """Bind a UDS at `path`; accept up to `n` native messages, store contents.
    Mimics a live Claude session's messaging socket closely enough for
    native_send (one JSON object + newline)."""

    def __init__(self, path: str, n: int = 4, timeout: float = 15.0):
        self.path = path
        self.n = n
        self.timeout = timeout
        self.received = []
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(path)
        self.srv.listen(4)  # must be in listening state before a peer connect()s
        self.srv.settimeout(timeout)
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        try:
            for _ in range(self.n):
                try:
                    conn, _ = self.srv.accept()
                except socket.timeout:
                    break
                with conn:
                    conn.settimeout(self.timeout)
                    buf = b""
                    try:
                        while b"\n" not in buf:
                            ch = conn.recv(65536)
                            if not ch:
                                break
                            buf += ch
                    except socket.timeout:
                        pass
                    raw = buf.split(b"\n", 1)[0]
                    if raw:
                        try:
                            m = json.loads(raw)
                            self.received.append(m.get("message", {}).get("content", ""))
                        except ValueError:
                            pass
        finally:
            try:
                self.srv.close()
            except OSError:
                pass


def make_shim_home(reg_persona_a: str, reg_persona_b: str, dead_persona: str,
                   live_pids: dict) -> dict:
    """Create an isolated home with a registry dir of fake session files.

    live_pids maps pid -> (persona, name, socket_exists, started_at_ms, sid).
    Returns {"home": Path, "regdir": Path, "socks": {persona_name: path}}.
    """
    home = pathlib.Path(tempfile.mkdtemp(prefix="c1-home-"))
    regdir = home / "claude" / "sessions"
    regdir.mkdir(parents=True)
    socks = {}
    for pid, (persona, name, sock_exists, started, sid) in live_pids.items():
        sockpath = str(home / "example-socks" / f"{pid}.sock")
        pathlib.Path(sockpath).parent.mkdir(parents=True, exist_ok=True)
        # the file existing is what makes the session 'live' for the shim scan
        if sock_exists:
            # create the socket file on disk (listener created separately); we
            # only need the PATH to exist for the shim's liveness gate.
            open(sockpath, "a").close()
        rec = {
            "pid": pid, "sessionId": sid,
            "cwd": f"/fake/agents/{persona}",
            "startedAt": started, "procStart": "", "version": "fake",
            "peerProtocol": 1, "peerFeatures": [], "kind": "interactive",
            "entrypoint": "fakesession", "pidDomain": "host",
            "messagingSocketPath": sockpath, "name": name, "nameSince": 0,
            "nameSource": "custom", "status": "idle",
        }
        # live claude sessions have no runtime field; this one is a 'stale codex'
        if persona == dead_persona:
            rec["runtime"] = "codex"
        (regdir / f"{pid}.json").write_text(json.dumps(rec))
        socks[name] = sockpath
    return {"home": home, "regdir": regdir, "socks": socks}


def shim_isolated(home: pathlib.Path, cfg: dict):
    """Point shim_mod at an isolated home/config; return a cleanup callable."""
    home.mkdir(parents=True, exist_ok=True)
    (home / ".claude").mkdir(exist_ok=True)
    # point the *known private* dir at our home too, so _reg_dirs() only sees
    # the reg dir we create (it also lists ~/.claude/sessions of the real home,
    # which we override via MCP_MSGERS_CONFIG registry_dirs + a temp HOME).
    cfgpath = home / "config.json"
    cfgpath.write_text(json.dumps(cfg))
    old_home, old_cfg = os.environ.get("HOME"), os.environ.get("MCP_MSGERS_CONFIG")
    os.environ["HOME"] = str(home)
    os.environ["MCP_MSGERS_CONFIG"] = str(cfgpath)
    os.environ.pop("CLAUDE_CONFIG_DIR", None)

    def cleanup():
        for k, v in (("HOME", old_home), ("MCP_MSGERS_CONFIG", old_cfg)):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return cleanup


PASS = []
FAIL = []


def check(label: str, cond: bool, detail: str = ""):
    (PASS if cond else FAIL).append(label)
    print(("PASS " if cond else "FAIL ") + label + (f"  [{detail}]" if (detail and not cond) else ""))


# --------------------------------------------------------------------------
# Case 1: two sessions for one persona are both reported, both receive, and #match selects one
# --------------------------------------------------------------------------
def test_same_persona_reporting_and_routing():
    with _FakeAgentsRoot("/fake/agents"):
        _run_same_persona_body()


def _run_same_persona_body():
    home = pathlib.Path(tempfile.mkdtemp(prefix="c1-home-"))
    # _reg_dirs() on these machines lists <home>/.claude/sessions first — write
    # the fake session files where the shim will actually scan them.
    regdir = home / ".claude" / "sessions"
    regdir.mkdir(parents=True)

    def reg(pid, persona, name, sid, started, runtime=None, sock="x"):
        rec = {"pid": pid, "sessionId": sid, "cwd": f"/fake/agents/{persona}",
               "startedAt": started, "messagingSocketPath": sock, "name": name,
               "nameSince": 0, "nameSource": "custom", "status": "idle"}
        if runtime:
            rec["runtime"] = runtime
        (regdir / f"{pid}.json").write_text(json.dumps(rec))

    base = home / "example-socks"
    base.mkdir()
    s_sock_1 = str(base / "1111.sock"); open(s_sock_1, "a").close()
    s_sock_2 = str(base / "2222.sock"); open(s_sock_2, "a").close()
    s_sock_3 = str(base / "3333.sock"); open(s_sock_3, "a").close()
    # s_freddead (4444): reg file exists but the socket file is NOT created, so
    # the session is pid-alive-but-socket-missing -> surfaces as degraded.

    reg(1111, "test_user_3", "test_user_3-c7", "sid-test_user_4", 1000, sock=s_sock_1)
    reg(2222, "test_user_3", "test_user_3-ea", "sid-test_user_5", 2000, sock=s_sock_2)   # newer
    reg(3333, "test_agent_2", "test_agent_2-32", "sid-test_agent_2", 1500, sock=s_sock_3)
    reg(4444, "test_user_3", "test_user_3-dead", "sid-freddead", 500,
        sock=str(base / "4444.sock"))  # socket path referenced, but never created

    old_home = os.environ.get("HOME")
    os.environ["HOME"] = str(home)
    os.environ.pop("CLAUDE_CONFIG_DIR", None)
    cfg = home / "config.json"; cfg.write_text(json.dumps({"host": "test_host_1"}))
    os.environ["MCP_MSGERS_CONFIG"] = str(cfg)

    try:
        # make all 4 pids look alive to the shim's _alive() (real subprocess)
        alive_pids = [1111, 2222, 3333, 4444]
        # We can't easily fake kill -0 for arbitrary pids, so monkeypatch _alive.
        orig_alive = shim_mod._alive
        shim_mod._alive = lambda pid: pid in alive_pids
        try:
            sess = shim_mod.live_sessions()
            deg = shim_mod.degraded_sessions()
        finally:
            shim_mod._alive = orig_alive

        pids = {s["pid"] for s in sess}
        check("1a two test_user_3 sessions both reported (not collapsed)",
              1111 in pids and 2222 in pids, f"pids={pids}")
        check("1b dead test_user_3 (socket gone) is NOT live", 4444 not in pids,
              f"pids={pids}")
        check("1c test_agent_2 reported", 3333 in pids, f"pids={pids}")

        test_user_3_sess = [s for s in sess if s["persona"] == "test_user_3"]
        check("1d both test_user_3 sessions carry persona 'test_user_3'",
              {s["pid"] for s in test_user_3_sess} == {1111, 2222},
              f"{[ (s['pid'],s['persona']) for s in sess ]}")

        check("1e dead test_user_3 surfaces as degraded",
              any(d.get("pid") == 4444 for d in deg),
              f"degraded={[d.get('pid') for d in deg]}")

        shim_mod._alive = lambda pid: pid in alive_pids
        try:
            s_bare = shim_mod.local_socket_for("test_user_3")
            s_c7 = shim_mod.local_socket_for("test_user_3", "test_user_3-c7")
            s_by_pid = shim_mod.local_socket_for("test_user_3", "2222")
            s_by_sid = shim_mod.local_socket_for("test_user_3", "sid-test_user_5")
            s_bad = shim_mod.local_socket_for("test_user_3", "does-not-exist")
        finally:
            shim_mod._alive = orig_alive

        check("2a bare 'test_user_3' resolves to NEWEST (2222)", s_bare == s_sock_2,
              f"got={s_bare}")
        check("3a #match by name routes to test_user_3-c7's socket", s_c7 == s_sock_1,
              f"got={s_c7}")
        check("3b #match by pid routes to 2222's socket", s_by_pid == s_sock_2,
              f"got={s_by_pid}")
        check("3c #match by session_id prefix routes correctly", s_by_sid == s_sock_2,
              f"got={s_by_sid}")
        check("3d #match that hits nothing -> None (fail loud, not a guess)",
              s_bad is None, f"got={s_bad}")

        f1 = FakeUDS(s_sock_1, n=2)
        f2 = FakeUDS(s_sock_2, n=2)
        time.sleep(0.2)
        shim_mod.native_send(s_sock_1, "test_agent_3", "MSG_TO_C7")
        shim_mod.native_send(s_sock_2, "test_agent_3", "MSG_TO_EA")
        time.sleep(0.5)
        check("4a test_user_3-c7 socket received its message",
              any("MSG_TO_C7" in c for c in f1.received),
              f"f1={f1.received}")
        check("4b test_user_3-ea socket received its (distinct) message",
              any("MSG_TO_EA" in c for c in f2.received),
              f"f2={f2.received}")
        check("4c messages did not cross sockets",
              not any("MSG_TO_EA" in c for c in f1.received)
              and not any("MSG_TO_C7" in c for c in f2.received))
    finally:
        os.environ["HOME"] = old_home
        os.environ.pop("MCP_MSGERS_CONFIG", None)


# --------------------------------------------------------------------------
# Case 2: hub side: failing send, #match routing, and the ack round trip
# --------------------------------------------------------------------------
def test_hub_fail_loud_and_ack():
    h = hub_mod.Hub(poll_timeout=1)

    h.heartbeat("test_host_1",
                [{"pid": 1111, "persona": "test_user_3", "name": "test_user_3-c7",
                  "socket": "/tmp/1111.sock", "started": 1000,
                  "session_id": "sid-test_user_4", "window": "w1"},
                 {"pid": 2222, "persona": "test_user_3", "name": "test_user_3-ea",
                  "socket": "/tmp/2222.sock", "started": 2000,
                  "session_id": "sid-test_user_5", "window": "w2"},
                 {"pid": 3333, "persona": "test_agent_2", "name": "test_agent_2-32",
                  "socket": "/tmp/3333.sock", "started": 1500,
                  "session_id": "sid-test_agent_2", "window": "w3"}],
                degraded=[], facts={"tmux_active": True, "claude_procs": 3},
                caps=["ack"])

    who = h.who()
    test_user_3_list = who.get("test_host_1", {}).get("test_user_3", [])
    check("5a /who lists both test_user_3 sessions",
          {e["pid"] for e in test_user_3_list} == {1111, 2222},
          f"test_user_3={test_user_3_list}")
    check("5b /who newest-first ordering (2222 before 1111)",
          test_user_3_list and test_user_3_list[0]["pid"] == 2222,
          f"test_user_3={test_user_3_list}")

    # fail loud: send to an absent persona -> ok=false (no false queued)
    r = h.send("test_agent_2", "test_agent_3", "test_host_1", "nobody", "hi")
    check("6a /send to absent persona -> ok=false", r["ok"] is False,
          f"{r}")
    check("6b absent-persona reply has NO queued=1 false success",
          "queued" not in r or r.get("queued", 0) == 0, f"{r}")

    # #match that hits nothing -> ok=false
    r = h.send("test_agent_2", "test_agent_3", "test_host_1", "test_user_3", "hi",
               extra={"to_match": "does-not-exist"})
    check("6c /send #match that hits nothing -> ok=false", r["ok"] is False,
          f"{r}")

    # delivered. We simulate the shim actor: it /pops the outbox (via poll from
    # the target host), delivers, and posts /ack.
    got_msg = {}
    def shim_actor():
        # drain the poll (blocking up to poll_timeout) to get the queued msg
        m = None
        deadline = time.time() + 3.0
        while time.time() < deadline and m is None:
            m = h.poll("test_host_1")
        if m:
            got_msg["m"] = m
            # simulate successful delivery -> ack ok
            h.ack("test_host_1", m["id"], True, "")
    t = threading.Thread(target=shim_actor)
    t.start()
    r = h.send("test_agent_2", "test_agent_3", "test_host_1", "test_user_3", "hi")  # bare -> newest
    t.join(timeout=5)
    check("6d /send to ack-capable host returns delivered=true",
          r.get("delivered") is True, f"{r}")
    check("6e outbox msg carried the right to_persona",
          got_msg.get("m", {}).get("to_persona") == "test_user_3",
          f"{got_msg}")

    # #match routes the outbox message to the right session (to_match preserved)
    def shim_actor2():
        deadline = time.time() + 3.0
        while time.time() < deadline:
            m = h.poll("test_host_1")
            if m:
                got_msg["m2"] = m
                h.ack("test_host_1", m["id"], True, "")
                return
        got_msg["m2"] = None
    t2 = threading.Thread(target=shim_actor2)
    t2.start()
    r2 = h.send("test_agent_2", "test_agent_3", "test_host_1", "test_user_3", "hi",
                extra={"to_match": "test_user_3-c7"})
    t2.join(timeout=5)
    check("6f /send #match ok=true and outbox kept to_match",
          r2.get("ok") is True and got_msg.get("m2", {}).get("to_match") == "test_user_3-c7",
          f"r2={r2} m2={got_msg.get('m2')}")

    # ack ERROR round-trip: target shim reports socket error -> /send ok=false
    def shim_actor3():
        deadline = time.time() + 3.0
        while time.time() < deadline:
            m = h.poll("test_host_1")
            if m:
                got_msg["m3"] = m
                h.ack("test_host_1", m["id"], False, "socket error: broken pipe")
                return
    t3 = threading.Thread(target=shim_actor3)
    t3.start()
    r3 = h.send("test_agent_2", "test_agent_3", "test_host_1", "test_agent_2", "hi")
    t3.join(timeout=5)
    check("6g /send with a shim-reported socket error -> ok=false (fail loud)",
          r3.get("ok") is False and "error" in r3, f"{r3}")

    # ok:true/target_alive — that was the "silent success" shape that masked
    # 10/23's 04:27:31Z E1 loss. It must come back ok:false + delivered:false,
    # fast (no long wait), with the reason explicit.
    h.heartbeat("test_host_3",
                [{"pid": 5555, "persona": "test_user_3", "name": "test_user_3-dj",
                  "socket": "/tmp/5555.sock", "started": 900,
                  "session_id": "sid-dj", "window": None}],
                degraded=[], facts={}, caps=[])
    t0 = time.time()
    r4 = h.send("test_agent_2", "test_agent_3", "test_host_3", "test_user_3", "hi")
    dt = time.time() - t0
    check("6h non-ack host -> ok:false delivered:false, fast",
          r4.get("ok") is False and r4.get("delivered") is False
          and r4.get("status") == "unconfirmed" and dt < 1.0,
          f"{r4} dt={dt:.2f}")

    # absent persona on non-ack host still fails loud (resolution is pre-ack)
    r5 = h.send("test_agent_2", "test_agent_3", "test_host_3", "ghost", "hi")
    check("6i absent persona on non-ack host -> ok=false", r5["ok"] is False,
          f"{r5}")


def test_background_jobs_are_never_routed():
    with _FakeAgentsRoot("/fake/agents"):
        _run_bg_jobs_body()


def _run_bg_jobs_body():
    # silently to the wrong session).
    home = pathlib.Path(tempfile.mkdtemp(prefix="c1-bg-"))
    regdir = home / ".claude" / "sessions"
    regdir.mkdir(parents=True)
    base = home / "example-socks"
    base.mkdir()

    def reg(pid, name, started, kind):
        sock = str(base / f"{pid}.sock"); open(sock, "a").close()
        rec = {"pid": pid, "sessionId": f"sid-{pid}", "cwd": "/fake/agents/test_agent_4",
               "startedAt": started, "messagingSocketPath": sock, "name": name,
               "status": "idle"}
        if kind is not None:
            rec["kind"] = kind
        (regdir / f"{pid}.json").write_text(json.dumps(rec))
        return sock

    s_live = reg(5211, "test_agent_4-71", 1000, "interactive")
    s_bg = reg(8951, "example session", 2000, "bg")
    s_old = reg(4100, "test_agent_4-legacy", 500, None)                  # pre-kind record
    old_home = os.environ.get("HOME")
    os.environ["HOME"] = str(home)
    os.environ.pop("CLAUDE_CONFIG_DIR", None)
    cfg = home / "config.json"; cfg.write_text(json.dumps({"host": "test_host_1"}))
    os.environ["MCP_MSGERS_CONFIG"] = str(cfg)
    orig_alive = shim_mod._alive
    shim_mod._alive = lambda pid: pid in (5211, 8951, 4100)
    try:
        sess = {s["pid"] for s in shim_mod.live_sessions()}
        check("7a background job is not a routable session", 8951 not in sess, f"{sess}")
        check("7b interactive and pre-kind sessions stay routable",
              {5211, 4100} <= sess, f"{sess}")
        check("7c bare persona picks the interactive session even when a bg job is newer",
              shim_mod.local_socket_for("test_agent_4") == s_live,
              f"{shim_mod.local_socket_for('test_agent_4')}")
        check("7d #match on the bg job's name never resolves to it",
              shim_mod.local_socket_for("test_agent_4", "example session") != s_bg)
        unr = shim_mod.unroutable_sessions()
        check("7e bg job is reported as unroutable with its kind",
              [(u["pid"], u["kind"]) for u in unr] == [(8951, "bg")], f"{unr}")
        check("7f bg job never shows as degraded",
              8951 not in {d["pid"] for d in shim_mod.degraded_sessions()})
    finally:
        shim_mod._alive = orig_alive
        if old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old_home
        os.environ.pop("MCP_MSGERS_CONFIG", None)

    h = hub_mod.Hub(poll_timeout=1)
    h.heartbeat("test_host_1",
                [{"pid": 5211, "persona": "test_agent_4", "name": "test_agent_4-71",
                  "socket": s_live, "started": 1000, "session_id": "sid-5211"}],
                degraded=[], facts={}, caps=[],
                unroutable=[{"pid": 8951, "persona": "test_agent_4",
                             "name": "example session", "kind": "bg"}])
    who = h.who()["test_host_1"]["test_agent_4"]
    bg = [e for e in who if e["pid"] == 8951]
    check("7g /who lists the bg job, flagged not addressable",
          len(bg) == 1 and bg[0]["addressable"] is False and bg[0]["kind"] == "bg", f"{who}")
    r = h.send("test_agent_5", "test_agent_6", "test_host_1", "test_agent_4", "hi",
               {"to_match": "example session"})
    check("7h sending to the bg job's name fails loud (ok=false)", r["ok"] is False, f"{r}")
    r = h.send("test_agent_5", "test_agent_6", "test_host_1", "test_agent_4", "hi")
    # G2: a non-ack-capable host returns ok:false status:unconfirmed, but
    # resolution still picked the right pid — assert the TARGET, not the
    # legacy "ok" shape (every live host now carries the ack cap).
    check("7i bare 'test_agent_4' still resolves to the interactive session",
          r.get("to", {}).get("pid") == 5211, f"{r}")
    # an old shim that sends no 'unroutable' key keeps working
    h.heartbeat("test_host_2", [], degraded=[], facts={}, caps=[])
    check("7j heartbeat without 'unroutable' is accepted", "test_host_2" in h.who())


def test_sender_header_is_the_registered_identity():
    """Sender header is the registered identity."""
    h = hub_mod.Hub(poll_timeout=1)
    # Both hosts advertise ack; the fake actor below drains poll and sends acks.
    h.heartbeat("test_host_2",
                [{"pid": 4242, "persona": "test_agent_1", "name": "test_agent_1-9f",
                  "socket": "/tmp/4242.sock", "started": 1000,
                  "session_id": "sid-test_agent_1", "window": "test_agent_1"},
                 {"pid": 5151, "persona": "test_user_2", "name": "test_user_2-02",
                  "socket": "/tmp/5151.sock", "started": 1100,
                  "session_id": "sid-test_user_2", "window": "test_user_2"}],
                degraded=[], facts={}, caps=["ack"])
    h.heartbeat("test_host_1",
                [{"pid": 7777, "persona": "test_agent_5", "name": "test_user_6",
                  "socket": "/tmp/7777.sock", "started": 1000,
                  "session_id": "sid-test_agent_5", "window": "test_agent_5"}],
                degraded=[], facts={}, caps=[])
    # 8a: a wrong caller label is replaced by the registered identity on the
    # outbox message. The /send return shape under G2 is no-cap-fast-fail;
    # the assertion that matters here is the header on the popped message.
    h.ACK_WAIT_S = 0.05
    h.send("test_host_2", "test_host_2@test_host_2", "test_host_1", "test_agent_5", "hi", {"from_pid": 4242})
    msg = h.poll("test_host_1")
    check("8a header is the registered persona@host#name",
          msg and msg["from_name"] == "test_agent_1@test_host_2#test_agent_1-9f", f"{msg}")
    # 8b: a hand-written signature of another persona is ignored too
    h.send("test_host_2", "test_agent_5@test_host_2", "test_host_1", "test_agent_5", "hi", {"from_pid": 5151})
    msg = h.poll("test_host_1")
    check("8b hand-written signature ignored", msg and msg["from_name"] == "test_user_2@test_host_2#test_user_2-02", f"{msg}")
    # 8c: an unregistered sender (no pid in the registry) keeps the label it gave
    h.send("test_host_4", "pi@test_host_4", "test_host_1", "test_agent_5", "hi", {"from_pid": 1})
    msg = h.poll("test_host_1")
    check("8c unregistered sender keeps its label", msg and msg["from_name"] == "pi@test_host_4", f"{msg}")

    # shim side: self_identity() takes persona and session name from the live
    # tree to the nearest known runtime, not just Claude). Env vars never participate.
    rec = {"persona": "test_agent_1", "name": "test_agent_1-9f", "pid": os.getpid(), "socket": "/tmp/x", "session_id": "s", "started": 1}
    saved = (shim_mod.live_sessions, shim_mod._find_runtime_pid, shim_mod.host_id, shim_mod._tmux_window)
    try:
        shim_mod.live_sessions = lambda: [rec]
        shim_mod._find_runtime_pid = lambda start_pid=None: (os.getpid(), "claude")
        shim_mod.host_id = lambda: "test_host_2"
        shim_mod._tmux_window = lambda pid: None
        ident = shim_mod.self_identity()
        check("8d shim from_name is the registered identity",
              ident["from_name"] == "test_agent_1@test_host_2#test_agent_1-9f" and ident["registered"] is True, f"{ident}")
        shim_mod.live_sessions = lambda: []
        ident = shim_mod.self_identity()
        check("8e unregistered pid without a derivable persona → unknown",
              ident["registered"] is False and ident["agent"] == "unknown"
              and ident["from_name"] == "unknown@test_host_2", f"{ident}")
        # never reads env for identity — removed as part of the launcher-identity revert.
        os.environ["MCP_MSGERS_FROM"] = "webchat@test_host_2#conv-42"
        shim_mod.live_sessions = lambda: [rec]
        ident = shim_mod.self_identity()
        check("8f MCP_MSGERS_FROM is ignored (env never participates)",
              ident["from_name"] == "test_agent_1@test_host_2#test_agent_1-9f"
              and ident["agent"] == "test_agent_1" and ident["session"] == "test_agent_1-9f", f"{ident}")
    finally:
        os.environ.pop("MCP_MSGERS_FROM", None)
        (shim_mod.live_sessions, shim_mod._find_runtime_pid, shim_mod.host_id, shim_mod._tmux_window) = saved


def main():
    test_same_persona_reporting_and_routing()
    test_hub_fail_loud_and_ack()
    test_background_jobs_are_never_routed()
    test_sender_header_is_the_registered_identity()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", *FAIL, sep="\n  ")
        return 1
    print("ALL GREEN")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
