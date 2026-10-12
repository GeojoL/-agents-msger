#!/usr/bin/env python3
"""mcp-msgers hub — the single source of truth for cross-machine routing.

Runs once, on the-hub-host. Every other machine runs a small shim that (a) heartbeats
its live Claude persona sessions here and (b) long-polls for outbound messages.
The hub holds the only registry and the only routing table; machines carry no
peer config of their own. stdlib only, so it runs on the-hub-host's /usr/bin/python3.

Wire protocol (JSON over HTTP):
  POST /hb    {host, sessions:[...], degraded:[...], facts:{...}, caps:[...]}
               -> registry; sessions are keyed by their UNIQUE pid (a session's
               socket is /tmp/cc-socks/<pid>.sock). Several sessions may share a
               persona (soft label) — every pid is tracked, none collapsed.
               degraded = pid-alive-but-socket-missing personas (reboot window),
               surfaced via /who (socket_missing) and /status; facts = bare host
               facts (tmux_active, claude_procs); caps = capability tags the shim
               reports (["ack"] = it acks real delivery outcomes on /ack).
  POST /send  {from_host, from_name, to_host, to_persona, to_match?, message}
               -> resolve the target in the registry and enqueue for to_host.
               to_match is a target's unique handle (name/pid/session_id) to pick
               one of several same-persona sessions; without it the persona's
               newest live session is implied. FAIL-LOUD: if the target cannot be
               resolved to a live session the reply is ok=false (no false
               "queued"). For ack-capable targets the reply also waits (bounded)
               for the shim's /ack and reports the real delivered/error.
  POST /ack   {host, id, ok, error?}  <- a shim reporting an outbox message's
               real delivery outcome (delivered, or no-target / socket-error).
  GET  /poll?host=X        -> long-poll: next message for X, or wait <= --poll-timeout
  GET  /who                -> every live session per host, keyed by persona with
               each session listed individually (unique name + pid), NOT collapsed
  GET  /status             -> per-host liveness (last_seen, alive, persona count)
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import threading
import time
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HUB_VERSION = "0.1.0"

# A host is considered alive if it heartbeat within this window.
ALIVE_WINDOW_S = 35
HEARTBEAT_TTL_S = 20


class Hub:
    def __init__(self, poll_timeout: int):
        self.poll_timeout = poll_timeout
        self.lock = threading.Lock()
        # host -> pid -> session dict {persona, name, socket, started, session_id,
        # last_seen, alive, window, pid, from_pid}. Keyed by UNIQUE pid so that
        # several sessions sharing a persona are each tracked — the old code keyed
        # by persona and silently collapsed same-persona siblings (the C1 bug).
        self.sessions: dict[str, dict[int, dict]] = defaultdict(dict)
        # host -> last heartbeat ts
        self.last_seen: dict[str, float] = {}
        # host -> deque of {id, from_name, to_persona, to_match, message, ts}
        self.outbox: dict[str, deque] = defaultdict(deque)
        # host -> set of capability tags the shim reported (["ack"] = it acks real
        # delivery outcomes on /ack). A hub-only client that never /acks is fine:
        # the send path just doesn't wait for one (stays ok / target_alive).
        self.caps: dict[str, set] = defaultdict(set)
        # host -> persona -> {socket, name, last_seen}: pid alive but messaging
        # socket missing (reboot window). Reported so a broken inbound is a
        # visible alert instead of a quiet 'not alive'.
        self.degraded: dict[str, dict[str, dict]] = defaultdict(dict)
        # host -> [ {pid, persona, name, kind} ]: listed by /who, never routed.
        self.unroutable: dict[str, list[dict]] = {}
        # host -> bare host-level facts reported by the shim (tmux_active,
        # claude_procs). Facts only — the hub is the single place that derives
        # any judgment from them (e.g. sessions_restored in /status).
        self.facts: dict[str, dict] = {}
        self.conds: dict[str, threading.Condition] = defaultdict(
            lambda: threading.Condition(self.lock))
        # host -> condition a /send wait sleeps on until the target shim /acks.
        # Distinct from self.conds (the poll outbox) so an /ack never wakes a
        # waiting /poll and vice-versa.
        self.ack_conds: dict[str, threading.Condition] = defaultdict(
            lambda: threading.Condition(self.lock))
        # mid -> {ok, error, ts}: the latest recorded /ack, consumed by the
        # matching /send wait. Bounded by ACK_WAIT_S so a slow/absent shim
        # (an old shim that never /acks) never holds /send past that.
        self.ack_results: dict[str, dict] = {}
        self.ACK_WAIT_S = 2.0  # how long /send waits for a shim's /ack

    # -- unique-identity resolution -----------------------------------------
    def _matches(self, s: dict, match: str | None) -> bool:
        """Does a session match a `#match` selector (its unique name, pid, or a
        session_id / uuid prefix)? None/empty match means 'any of this persona'."""
        if not match:
            return True
        if s.get("name") == match:
            return True
        if s.get("pid") is not None and str(s.get("pid")) == match:
            return True
        sid = s.get("session_id")
        return bool(sid) and (sid == match or str(sid).startswith(match))

    def _resolve(self, host: str, persona: str,
                 match: str | None) -> tuple[dict | None, list[dict]]:
        """Resolve a target to a concrete session. Returns (winner, candidates).

        - with a `match`: the single live session whose name/pid/session_id
          matches it; (None, candidates) if none match -> fail loud.
        - without: any live session of this persona; if several, the NEWEST is
          the winner (alias resolution by design — a bare alias never means
          'not found'). If the persona has no live session at all -> (None, [])."""
        cands = [s for s in self.sessions.get(host, {}).values()
                 if s["alive"] and s.get("persona") == persona]
        if match:
            cands = [s for s in cands if self._matches(s, match)]
        if not cands:
            return None, cands
        winner = max(cands, key=lambda s: (s.get("started") or 0, s["pid"]))
        return winner, cands

    def heartbeat(self, host: str, sessions: list[dict],
                  degraded: list[dict] | None = None,
                  facts: dict | None = None,
                  caps: list | None = None,
                  unroutable: list[dict] | None = None) -> dict:
        degraded = degraded or []
        # Non-interactive sessions (background jobs): shown by /who with
        # addressable=False, never used for routing. Older shims omit it.
        self.unroutable[host] = [u for u in (unroutable or []) if u.get("pid")]
        now = time.time()
        with self.lock:
            self.last_seen[host] = now
            # store bare host facts (no judgment here — just what the shim saw)
            self.facts[host] = facts or {}
            self.caps[host] = set(caps or [])
            reg = self.sessions[host]
            seen = set()
            for s in sessions:
                pid = s.get("pid")
                p = s.get("persona")
                if not (pid and p):
                    continue  # pid+persona are the identity; a session with
                seen.add(pid)                       # neither is unaddressable
                reg[pid] = {"pid": pid, "persona": p, "socket": s.get("socket"),
                            "name": s.get("name"), "session_id": s.get("session_id"),
                            "started": s.get("started") or 0, "last_seen": now,
                            "alive": True, "window": s.get("window"),
                            "from_pid": s.get("from_pid")}
            # mark sessions not in this heartbeat as not-alive (session ended)
            for pid in list(reg):
                if pid not in seen:
                    reg[pid]["alive"] = False
            # replace this host's degraded set (pid alive but socket missing)
            deg = self.degraded[host]
            newp = set()
            for d in degraded:
                dp = d.get("persona")
                if not dp:
                    continue
                newp.add(dp)
                deg[dp] = {"socket": d.get("socket"),
                           "name": d.get("name"), "pid": d.get("pid"),
                           "last_seen": now}
            for dp in list(deg):
                if dp not in newp:
                    del deg[dp]
            return self._snapshot_locked(host)

    def ack(self, host: str, mid: str, ok: bool, error: str = "") -> bool:
        """A shim reports an outbox message's real delivery outcome. Records it and
        wakes the matching /send wait (if any); a late/unknown mid is harmless."""
        with self.ack_conds[host]:
            self.ack_results[mid] = {"ok": ok, "error": error, "ts": time.time()}
            self.ack_conds[host].notify_all()

    def wait_for_ack(self, host: str, mid: str) -> dict | None:
        """Bounded wait (lock released) for the target shim to /ack `mid`. Returns
        the recorded outcome, or None on timeout / no-ack (an old shim)."""
        c = self.ack_conds[host]
        with c:
            t0 = time.time()
            while mid not in self.ack_results:
                remaining = self.ACK_WAIT_S - (time.time() - t0)
                if remaining <= 0:
                    return None
                c.wait(remaining)
            res = self.ack_results.get(mid)
            self.ack_results.pop(mid, None)
            return res

    def send(self, from_host: str, from_name: str, to_host: str,
             to_persona: str, message: str, extra: dict | None = None) -> dict:
        extra = extra or {}
        match = extra.get("to_match") or None
        with self.lock:
            winner, cands = self._resolve(to_host, to_persona, match)
            if winner is None:
                # fail loud: no live session to deliver to (persona absent, or a
                # #match that hit nothing). Never a silent ok:true/queued.
                return {"ok": False,
                        "error": (f"no live session matching {match}"
                                  if match else
                                  f"no live session for persona '{to_persona}' "
                                  f"on {to_host}")}
            q = self.outbox[to_host]
            mid = "%d" % int(time.time() * 1e6)
            q.append({"id": mid, "from_name": from_name,
                      "to_persona": to_persona, "to_match": match,
                      "message": message, "ts": time.time(),
                      "from_window": extra.get("from_window"),
                      "from_pid": extra.get("from_pid")})
            target = {"persona": to_persona, "pid": winner["pid"],
                      "name": winner["name"]}
            if match:
                target["match"] = match
            queued = len(q)
            can_ack = "ack" in self.caps.get(to_host, set())
            self.conds[to_host].notify_all()
        # wait OUTSIDE the registry lock for the shim's real delivery outcome
        result = None
        if can_ack:
            result = self.wait_for_ack(to_host, mid)
        if result is not None:
            if result.get("ok"):
                return {"ok": True, "delivered": True, "queued": queued,
                        "to": target}
            return {"ok": False, "queued": queued, "to": target,
                    "error": result.get("error") or "delivery failed"}
        # no ack (old shim / timeout): keep the pre-ack answer shape.
        return {"ok": True, "queued": queued, "target_alive": True,
                "to": f"{to_persona}@{to_host}", "target_pid": winner["pid"]}

    def poll(self, host: str) -> dict | None:
        with self.lock:
            q = self.outbox[host]
            c = self.conds[host]
            t0 = time.time()
            while not q:
                remaining = self.poll_timeout - (time.time() - t0)
                if remaining <= 0:
                    return None
                c.wait(remaining)
            return q.popleft()

    def who(self) -> dict:
        """List every live session, NOT collapsed by persona.

        Shape: host -> persona -> [ {pid, name, alive} ... ]. A persona with
        several live sessions yields several entries, each with its unique pid +
        name so a caller can address one precisely via relay_send's '#' handle.
        Degraded (pid-alive-but-socket-missing) personas that have no live
        session are surfaced too, flagged socket_missing."""
        with self.lock:
            out = {}
            for h, sess in self.sessions.items():
                host: dict[str, list] = {}
                for s in sess.values():
                    p = s.get("persona")
                    if not p:
                        continue
                    host.setdefault(p, []).append(
                        {"pid": s.get("pid"), "name": s.get("name"),
                         "alive": s["alive"]})
                for plist in host.values():
                    # stable order: newest first so the 'bare alias -> newest'
                    # winner is always the first listed — matches resolution.
                    plist.sort(key=lambda x: (x.get("pid") or 0), reverse=True)
                # surface pid-alive-but-socket-missing personas with no live sess
                for dp, dv in self.degraded.get(h, {}).items():
                    if dp not in host:
                        host[dp] = [{"pid": dv.get("pid"),
                                     "name": dv.get("name"), "alive": False,
                                     "socket_missing": True}]
                for u in self.unroutable.get(h, []):
                    host.setdefault(u.get("persona") or f"pid-{u['pid']}", []).append(
                        {"pid": u["pid"], "name": u.get("name"), "alive": True,
                         "kind": u.get("kind"), "addressable": False,
                         "note": "not addressable: a non-interactive session (e.g. a background job); messages are not delivered here"})
                out[h] = host
            return out

    def status(self) -> dict:
        with self.lock:
            now = time.time()
            out = {}
            for h, seen in self.last_seen.items():
                alive = (now - seen) < ALIVE_WINDOW_S
                sess = self.sessions.get(h, {})
                # a persona is only 'degraded' if it's pid-alive-but-socket-
                # missing AND was not also reported live this heartbeat
                livep = {s.get("persona") for s in sess.values()
                         if s["alive"] and s.get("persona")}
                # The shim reports only bare facts; the hub is the single place
                # that turns them into a judgment. sessions_restored = at least
                # one claude process is running on the host — a live agent
                # session may or may not live inside tmux (some launchers run a
                # Claude session outside tmux), so we key on claude
                # processes, not on the tmux server. tmux_active stays a
                # separate bare fact for context; it is NOT a precondition.
                facts = self.facts.get(h, {})
                restored = int(facts.get("claude_procs", 0) or 0) > 0
                out[h] = {"alive": alive, "last_seen": seen,
                          "last_seen_age_s": round(now - seen, 1),
                          "sessions_restored": restored,
                          "tmux_active": facts.get("tmux_active"),
                          "claude_procs": facts.get("claude_procs"),
                          "personas": sorted(livep),
                          "socket_missing": sorted(
                              p for p in self.degraded.get(h, {}) if p not in livep)}
            return out

    def _snapshot_locked(self, host: str) -> dict:
        sess = self.sessions.get(host, {})
        return {s["persona"]: {"pid": s.get("pid"), "socket": s.get("socket"),
                               "name": s.get("name"), "alive": s["alive"]}
                for s in sess.values() if s.get("persona")}


class Handler(BaseHTTPRequestHandler):
    hub: Hub = None

    def log_message(self, *a):  # quiet
        pass

    def _read_json(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    def _send(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/who":
            self._send(self.hub.who())
        elif u.path == "/status":
            self._send(self.hub.status())
        elif u.path == "/poll":
            host = (q.get("host") or [""])[0]
            if not host:
                self._send({"ok": False, "error": "missing host"}, 400)
                return
            msg = self.hub.poll(host)
            self._send({"ok": True, "message": msg})
        elif u.path == "/health":
            self._send({"ok": True, "version": HUB_VERSION, "host": self.hub_host})
        else:
            self._send({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        body = self._read_json()
        if u.path == "/hb":
            host = body.get("host")
            if not host:
                self._send({"ok": False, "error": "missing host"}, 400)
                return
            snap = self.hub.heartbeat(host, body.get("sessions", []),
                                      body.get("degraded", []),
                                      body.get("facts", {}),
                                      body.get("caps", []),
                                      body.get("unroutable", []))
            self._send({"ok": True, "sessions": snap})
        elif u.path == "/send":
            for k in ("from_host", "to_host", "to_persona", "message"):
                if k not in body:
                    self._send({"ok": False, "error": f"missing {k}"}, 400)
                    return
            r = self.hub.send(body["from_host"], body.get("from_name", ""),
                              body["to_host"], body["to_persona"], body["message"],
                              extra={"from_window": body.get("from_window"),
                                     "from_pid": body.get("from_pid"),
                                     "to_match": body.get("to_match")})
            self._send(r)
        elif u.path == "/ack":
            host = body.get("host")
            mid = body.get("id")
            if not (host and mid):
                self._send({"ok": False, "error": "missing host or id"}, 400)
                return
            self.hub.ack(host, str(mid), bool(body.get("ok", False)),
                         body.get("error", ""))
            self._send({"ok": True})
        else:
            self._send({"ok": False, "error": "not found"}, 404)


def detect_bind_addr(host: str) -> str:
    # Bind the Tailscale IP only: the trust boundary is the tailnet, so only
    # tailnet members can reach the port (mirrors the predecessor relay's ssh-over-tailnet).
    # Clients connect to this same IP, which every host resolves identically —
    # unlike a host name, which a Docker/VPN resolver can rewrite.
    try:
        import subprocess
        out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True,
                             text=True, timeout=5).stdout.strip()
        if out:
            return out
    except Exception:
        pass
    return "0.0.0.0"


def main() -> int:
    ap = argparse.ArgumentParser(prog="mcp-msgers-hub")
    ap.add_argument("--port", type=int, default=8465)
    ap.add_argument("--poll-timeout", type=int, default=25,
                    help="max seconds a /poll request is held before returning empty")
    ap.add_argument("--bind", default=None,
                    help="bind address (default: auto via tailscale ip, else 0.0.0.0)")
    ap.add_argument("--host-name", default=None)
    args = ap.parse_args()

    import socket as _s
    hostname = args.host_name or os.environ.get("HOSTNAME") or _s.gethostname()

    hub = Hub(poll_timeout=args.poll_timeout)
    Handler.hub = hub
    Handler.hub_host = hostname
    addr = args.bind or detect_bind_addr(hostname)
    server = ThreadingHTTPServer((addr, args.port), Handler)
    server.daemon_threads = True
    print(f"mcp-msgers hub ready host={hostname} listen={addr}:{args.port} "
          f"poll_timeout={args.poll_timeout}s pid={os.getpid()}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
