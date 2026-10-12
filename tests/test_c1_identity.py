#!/usr/bin/env python3
"""C1 identity-addressing local test (no live hub/shim touched).

Covers the locked design end-to-end:
  1. two sessions sharing a persona (two agents/alice, different pids) are BOTH
     reported by live_sessions() (never collapsed) and BOTH show in the hub /who;
  2. each can receive a message, and the two sockets each get their own;
  3. `#match` routes to the correct specific socket (name/pid/session_id);
  4. a dead/absent target -> relay_send's caller sees ok=false, NOT a false
     queued=1 (fail loud);
  5. a bare alias with several sessions resolves to the newest (soft label),
     and still succeeds;
  6. the /ack round-trip: an ack-capable shim posts delivered/error and the
     /send reply reflects it; an old (non-ack) shim still gets ok/target_alive.

Run:  python3 tests/test_c1_identity.py        (prints PASS/FAIL lines, exit 0/1)
"""
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
        sockpath = str(home / "cc-socks" / f"{pid}.sock")
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
# Test 1: two same-persona sessions both reported + both receive + #match
# --------------------------------------------------------------------------
def test_same_persona_reporting_and_routing():
    # Two 'alice' sessions: pids 1111 (alice-c7, older) and 2222 (alice-ea, newer).
    # Plus one 'bob' session pid 3333. Plus a DEAD alice (socket gone) pid 4444.
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

    base = home / "cc-socks"
    base.mkdir()
    s_alice1 = str(base / "1111.sock"); open(s_alice1, "a").close()
    s_alice2 = str(base / "2222.sock"); open(s_alice2, "a").close()
    s_bob = str(base / "3333.sock"); open(s_bob, "a").close()
    # s_alicedead (4444): reg file exists but the socket file is NOT created, so
    # the session is pid-alive-but-socket-missing -> surfaces as degraded.

    reg(1111, "alice", "alice-c7", "sid-alice1", 1000, sock=s_alice1)
    reg(2222, "alice", "alice-ea", "sid-alice2", 2000, sock=s_alice2)   # newer
    reg(3333, "bob", "bob-32", "sid-bob", 1500, sock=s_bob)
    reg(4444, "alice", "alice-dead", "sid-alicedead", 500,
        sock=str(base / "4444.sock"))  # socket path referenced, but never created

    old_home = os.environ.get("HOME")
    os.environ["HOME"] = str(home)
    os.environ.pop("CLAUDE_CONFIG_DIR", None)
    cfg = home / "config.json"; cfg.write_text(json.dumps({"host": "host-a"}))
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
        check("1a two alice sessions both reported (not collapsed)",
              1111 in pids and 2222 in pids, f"pids={pids}")
        check("1b dead alice (socket gone) is NOT live", 4444 not in pids,
              f"pids={pids}")
        check("1c bob reported", 3333 in pids, f"pids={pids}")

        # persona is a soft label: both alice sessions carry persona 'alice'
        alice_sess = [s for s in sess if s["persona"] == "alice"]
        check("1d both alice sessions carry persona 'alice'",
              {s["pid"] for s in alice_sess} == {1111, 2222},
              f"{[ (s['pid'],s['persona']) for s in sess ]}")

        # degraded: the socket-missing alice should surface as degraded (not live)
        check("1e dead alice surfaces as degraded",
              any(d.get("pid") == 4444 for d in deg),
              f"degraded={[d.get('pid') for d in deg]}")

        # routing: bare 'alice' -> newest (2222); #match name -> specific
        shim_mod._alive = lambda pid: pid in alive_pids
        try:
            s_bare = shim_mod.local_socket_for("alice")
            s_c7 = shim_mod.local_socket_for("alice", "alice-c7")
            s_by_pid = shim_mod.local_socket_for("alice", "2222")
            s_by_sid = shim_mod.local_socket_for("alice", "sid-alice2")
            s_bad = shim_mod.local_socket_for("alice", "does-not-exist")
        finally:
            shim_mod._alive = orig_alive

        check("2a bare 'alice' resolves to NEWEST (2222)", s_bare == s_alice2,
              f"got={s_bare}")
        check("3a #match by name routes to alice-c7's socket", s_c7 == s_alice1,
              f"got={s_c7}")
        check("3b #match by pid routes to 2222's socket", s_by_pid == s_alice2,
              f"got={s_by_pid}")
        check("3c #match by session_id prefix routes correctly", s_by_sid == s_alice2,
              f"got={s_by_sid}")
        check("3d #match that hits nothing -> None (fail loud, not a guess)",
              s_bad is None, f"got={s_bad}")

        # delivery: real native_send into two alice sockets -> each gets its own
        f1 = FakeUDS(s_alice1, n=2)
        f2 = FakeUDS(s_alice2, n=2)
        time.sleep(0.2)
        shim_mod.native_send(s_alice1, "bob@host-a", "MSG_TO_C7")
        shim_mod.native_send(s_alice2, "bob@host-a", "MSG_TO_EA")
        time.sleep(0.5)
        check("4a alice-c7 socket received its message",
              any("MSG_TO_C7" in c for c in f1.received),
              f"f1={f1.received}")
        check("4b alice-ea socket received its (distinct) message",
              any("MSG_TO_EA" in c for c in f2.received),
              f"f2={f2.received}")
        check("4c messages did not cross sockets",
              not any("MSG_TO_EA" in c for c in f1.received)
              and not any("MSG_TO_C7" in c for c in f2.received))
    finally:
        os.environ["HOME"] = old_home
        os.environ.pop("MCP_MSGERS_CONFIG", None)


# --------------------------------------------------------------------------
# Test 2: hub side — fail-loud /send, #match routing, /ack round-trip
# --------------------------------------------------------------------------
def test_hub_fail_loud_and_ack():
    h = hub_mod.Hub(poll_timeout=1)

    # host 'host-a' has two live 'alice' sessions + reports ack cap.
    h.heartbeat("host-a",
                [{"pid": 1111, "persona": "alice", "name": "alice-c7",
                  "socket": "/tmp/1111.sock", "started": 1000,
                  "session_id": "sid-alice1", "window": "w1"},
                 {"pid": 2222, "persona": "alice", "name": "alice-ea",
                  "socket": "/tmp/2222.sock", "started": 2000,
                  "session_id": "sid-alice2", "window": "w2"},
                 {"pid": 3333, "persona": "bob", "name": "bob-32",
                  "socket": "/tmp/3333.sock", "started": 1500,
                  "session_id": "sid-bob", "window": "w3"}],
                degraded=[], facts={"tmux_active": True, "claude_procs": 3},
                caps=["ack"])

    # /who lists both alice sessions individually (not collapsed)
    who = h.who()
    alice_list = who.get("host-a", {}).get("alice", [])
    check("5a /who lists both alice sessions",
          {e["pid"] for e in alice_list} == {1111, 2222},
          f"alice={alice_list}")
    check("5b /who newest-first ordering (2222 before 1111)",
          alice_list and alice_list[0]["pid"] == 2222,
          f"alice={alice_list}")

    # fail loud: send to an absent persona -> ok=false (no false queued)
    r = h.send("bob", "bob@host-a", "host-a", "nobody", "hi")
    check("6a /send to absent persona -> ok=false", r["ok"] is False,
          f"{r}")
    check("6b absent-persona reply has NO queued=1 false success",
          "queued" not in r or r.get("queued", 0) == 0, f"{r}")

    # #match that hits nothing -> ok=false
    r = h.send("bob", "bob@host-a", "host-a", "alice", "hi",
               extra={"to_match": "does-not-exist"})
    check("6c /send #match that hits nothing -> ok=false", r["ok"] is False,
          f"{r}")

    # ack round-trip: sender /send to alice (bare -> newest 2222), the shim /acks
    # delivered. We simulate the shim actor: it /pops the outbox (via poll from
    # the target host), delivers, and posts /ack.
    # start a thread acting as the target shim: poll host-a, then ack the mid.
    got_msg = {}
    def shim_actor():
        # drain the poll (blocking up to poll_timeout) to get the queued msg
        m = None
        deadline = time.time() + 3.0
        while time.time() < deadline and m is None:
            m = h.poll("host-a")
        if m:
            got_msg["m"] = m
            # simulate successful delivery -> ack ok
            h.ack("host-a", m["id"], True, "")
    t = threading.Thread(target=shim_actor)
    t.start()
    r = h.send("bob", "bob@host-a", "host-a", "alice", "hi")  # bare -> newest
    t.join(timeout=5)
    check("6d /send to ack-capable host returns delivered=true",
          r.get("delivered") is True, f"{r}")
    check("6e outbox msg carried the right to_persona",
          got_msg.get("m", {}).get("to_persona") == "alice",
          f"{got_msg}")

    # #match routes the outbox message to the right session (to_match preserved)
    def shim_actor2():
        deadline = time.time() + 3.0
        while time.time() < deadline:
            m = h.poll("host-a")
            if m:
                got_msg["m2"] = m
                h.ack("host-a", m["id"], True, "")
                return
        got_msg["m2"] = None
    t2 = threading.Thread(target=shim_actor2)
    t2.start()
    r2 = h.send("bob", "bob@host-a", "host-a", "alice", "hi",
                extra={"to_match": "alice-c7"})
    t2.join(timeout=5)
    check("6f /send #match ok=true and outbox kept to_match",
          r2.get("ok") is True and got_msg.get("m2", {}).get("to_match") == "alice-c7",
          f"r2={r2} m2={got_msg.get('m2')}")

    # ack ERROR round-trip: target shim reports socket error -> /send ok=false
    def shim_actor3():
        deadline = time.time() + 3.0
        while time.time() < deadline:
            m = h.poll("host-a")
            if m:
                got_msg["m3"] = m
                h.ack("host-a", m["id"], False, "socket error: broken pipe")
                return
    t3 = threading.Thread(target=shim_actor3)
    t3.start()
    r3 = h.send("bob", "bob@host-a", "host-a", "bob", "hi")  # bob pid 3333 live
    t3.join(timeout=5)
    check("6g /send with a shim-reported socket error -> ok=false (fail loud)",
          r3.get("ok") is False and "error" in r3, f"{r3}")

    # old (non-ack) host: no caps -> /send returns ok/target_alive, does not wait
    h.heartbeat("host-b",
                [{"pid": 5555, "persona": "alice", "name": "alice-dj",
                  "socket": "/tmp/5555.sock", "started": 900,
                  "session_id": "sid-dj", "window": None}],
                degraded=[], facts={}, caps=[])
    t0 = time.time()
    r4 = h.send("bob", "bob@host-a", "host-b", "alice", "hi")
    dt = time.time() - t0
    check("6h non-ack host -> ok + target_alive, no long wait",
          r4.get("ok") is True and r4.get("target_alive") is True and dt < 1.0,
          f"{r4} dt={dt:.2f}")

    # absent persona on non-ack host still fails loud (resolution is pre-ack)
    r5 = h.send("bob", "bob@host-a", "host-b", "ghost", "hi")
    check("6i absent persona on non-ack host -> ok=false", r5["ok"] is False,
          f"{r5}")


def test_background_jobs_are_never_routed():
    # host-a 2026-01-01: a Claude Code background job (kind "bg", hosted by
    # `claude bg-spare`) ran in agents/dave, registered as a 'dave' session
    # and took messages meant for dave's interactive window (delivered:true,
    # silently to the wrong session).
    home = pathlib.Path(tempfile.mkdtemp(prefix="c1-bg-"))
    regdir = home / ".claude" / "sessions"
    regdir.mkdir(parents=True)
    base = home / "cc-socks"
    base.mkdir()

    def reg(pid, name, started, kind):
        sock = str(base / f"{pid}.sock"); open(sock, "a").close()
        rec = {"pid": pid, "sessionId": f"sid-{pid}", "cwd": "/fake/agents/dave",
               "startedAt": started, "messagingSocketPath": sock, "name": name,
               "status": "idle"}
        if kind is not None:
            rec["kind"] = kind
        (regdir / f"{pid}.json").write_text(json.dumps(rec))
        return sock

    s_live = reg(5211, "dave-71", 1000, "interactive")
    s_bg = reg(8951, "example background job", 2000, "bg")    # NEWER than dave-71
    s_old = reg(4100, "dave-legacy", 500, None)                  # pre-kind record
    old_home = os.environ.get("HOME")
    os.environ["HOME"] = str(home)
    os.environ.pop("CLAUDE_CONFIG_DIR", None)
    cfg = home / "config.json"; cfg.write_text(json.dumps({"host": "host-a"}))
    os.environ["MCP_MSGERS_CONFIG"] = str(cfg)
    orig_alive = shim_mod._alive
    shim_mod._alive = lambda pid: pid in (5211, 8951, 4100)
    try:
        sess = {s["pid"] for s in shim_mod.live_sessions()}
        check("7a background job is not a routable session", 8951 not in sess, f"{sess}")
        check("7b interactive and pre-kind sessions stay routable",
              {5211, 4100} <= sess, f"{sess}")
        check("7c bare persona picks the interactive session even when a bg job is newer",
              shim_mod.local_socket_for("dave") == s_live,
              f"{shim_mod.local_socket_for('dave')}")
        check("7d #match on the bg job's name never resolves to it",
              shim_mod.local_socket_for("dave", "example background job") != s_bg)
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
    h.heartbeat("host-a",
                [{"pid": 5211, "persona": "dave", "name": "dave-71",
                  "socket": s_live, "started": 1000, "session_id": "sid-5211"}],
                degraded=[], facts={}, caps=[],
                unroutable=[{"pid": 8951, "persona": "dave",
                             "name": "example background job", "kind": "bg"}])
    who = h.who()["host-a"]["dave"]
    bg = [e for e in who if e["pid"] == 8951]
    check("7g /who lists the bg job, flagged not addressable",
          len(bg) == 1 and bg[0]["addressable"] is False and bg[0]["kind"] == "bg", f"{who}")
    r = h.send("erin", "erin@host-a", "host-a", "dave", "hi",
               {"to_match": "example background job"})
    check("7h sending to the bg job's name fails loud (ok=false)", r["ok"] is False, f"{r}")
    r = h.send("erin", "erin@host-a", "host-a", "dave", "hi")
    check("7i bare 'dave' still delivers to the interactive session",
          r["ok"] is True and r["target_pid"] == 5211, f"{r}")
    # an old shim that sends no 'unroutable' key keeps working
    h.heartbeat("host-c", [], degraded=[], facts={}, caps=[])
    check("7j heartbeat without 'unroutable' is accepted", "host-c" in h.who())


def main():
    test_same_persona_reporting_and_routing()
    test_hub_fail_loud_and_ack()
    test_background_jobs_are_never_routed()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", *FAIL, sep="\n  ")
        return 1
    print("ALL GREEN")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
