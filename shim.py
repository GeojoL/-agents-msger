#!/usr/bin/env python3
"""mcp-msgers shim — runs on every machine (host-a, host-b, host-c, ...).

Two modes:
  serve   background worker, started by launchd/systemd:
            - heartbeats this host's live Claude persona sessions to the hub
            - also reports "degraded" personas (pid alive but messaging socket
              missing — the reboot window) so a broken inbound is a visible
              alert, not a quiet 'not alive' (detection + reporting only)
            - long-polls the hub for outbound messages and pushes each one
              into the target local Claude session's UDS socket (native)
  mcp     MCP stdio server exposing relay_send / relay_who / relay_status
            so local agents get clean, self-explanatory tools.

Zero per-machine config: it reads only hub_url from a small config file
(created by the installer); host id is the local hostname. stdlib only.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import pathlib
import socket
import subprocess
import sys
import time
import urllib.request
import urllib.error

SHIM_VERSION = "0.2.0"
# caps this shim reports to the hub. "ack" = it posts the REAL delivery outcome
# of each outbox message on /ack, so the hub can return delivered/error (not a
# silent drop) to a /send whose target is an ack-capable shim.
SHIM_CAPS = ["ack"]
HB_INTERVAL_S = 8          # heartbeat cadence
POLL_TIMEOUT_S = 25        # must be < hub poll-timeout is fine; hub holds up to its own timeout


def config_path() -> pathlib.Path:
    env = os.environ.get("MCP_MSGERS_CONFIG")
    if env:
        return pathlib.Path(env).expanduser()
    return pathlib.Path("~/.config/mcp-msgers/config.json").expanduser()


def load_config() -> dict:
    try:
        return json.loads(config_path().read_text())
    except (OSError, ValueError):
        return {}


def hub_url() -> str:
    return load_config().get("hub_url", "").rstrip("/")


def host_id() -> str:
    cfg = load_config()
    if cfg.get("host"):
        return cfg["host"]
    return socket.gethostname().split(".")[0]


def _log_path() -> pathlib.Path:
    # Default lives next to this file (the tools dir) so code + log stay
    # together; override with MCP_MSGERS_LOG.
    env = os.environ.get("MCP_MSGERS_LOG")
    if env:
        return pathlib.Path(env).expanduser()
    return pathlib.Path(__file__).resolve().parent / "shim.log"


def log(event: str, **fields):
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, **fields}
    try:
        p = _log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


# ---------- registry scan (mirrors the predecessor relay: live Claude persona sessions) ----

def _reg_dirs() -> list[pathlib.Path]:
    """Every place a live session may register its socket file.

    Claude Code registers a session's messagingSocketPath under
    $CLAUDE_CONFIG_DIR/sessions — on these machines that is the *private* config
    dir (~/.config/host-b-local/claude/sessions), NOT ~/.claude/sessions. The
    stale codex/relay peers live under ~/.claude/sessions. We must scan both, or
    we register the dead codex and miss the live agent (this exact miss once broke
    cross-session delivery). So: always include ~/.claude/sessions,
    the known private dir, $CLAUDE_CONFIG_DIR/sessions if set, plus any
    explicit registry_dirs from config. Dirs that don't exist are skipped by the
    glob, so listing extra ones is harmless — no per-machine config needed."""
    home = pathlib.Path.home()
    dirs = [
        home / ".claude" / "sessions",
    ]
    ccd = os.environ.get("CLAUDE_CONFIG_DIR")
    if ccd:
        dirs.append(pathlib.Path(ccd).expanduser() / "sessions")
    for d in load_config().get("registry_dirs", []):
        dirs.append(pathlib.Path(d).expanduser())
    # de-dup by resolved path, preserve order
    out, seen = [], set()
    for d in dirs:
        try:
            rp = d.resolve()
        except OSError:
            continue
        if rp not in seen:
            seen.add(rp)
            out.append(d)
    return out


def _alive(pid) -> bool:
    return bool(pid) and subprocess.run(
        ["kill", "-0", str(pid)], capture_output=True,
        stdin=subprocess.DEVNULL).returncode == 0


def _persona(r: dict):
    parts = pathlib.Path(r.get("cwd", "")).parts
    if "agents" in parts:
        i = parts.index("agents")
        if i + 1 < len(parts):
            return parts[i + 1]
    import re
    n = re.sub(r"-(codex|claude|relay|shim|[0-9a-f]{1,6})$", "", r.get("name", ""))
    return n or r.get("name") or None


def _is_live_claude(runtime) -> bool:
    """A genuine live Claude session has no `runtime` (or runtime=='claude');
    codex / the predecessor relay / etc. are other runtimes that historically registered into
    the same ~/.claude/sessions dir and can otherwise shadow a live agent."""
    return runtime in (None, "claude")


def _addressable(d: dict) -> bool:
    """Only interactive sessions are persona endpoints.

    Claude Code background jobs (kind "bg", hosted by a `claude bg-spare`
    process) run in an agent's cwd and so resolve to that persona, but a message
    delivered to one never reaches the person's interactive session. Records
    without a kind predate the field and are interactive."""
    return (d.get("kind") or "interactive") == "interactive"


def live_sessions() -> list[dict]:
    """Live (non-shim) Claude persona sessions on this host.

    Each session is keyed by its UNIQUE process id (pid): a session's messaging
    socket is /tmp/cc-socks/<pid>.sock and a sender's from_pid is that same pid,
    so pid is the one identity that pins down a single session. The persona (the
    base agent name, e.g. 'alice') is only a SOFT label: several DISTINCT sessions
    can share it (two agents/alice windows) and each must be reported and
    addressable on its own. We therefore NEVER collapse two different pids; we
    only dedupe the SAME session (same pid) when it registers into more than one
    session dir, preferring a live-Claude entry over a stale codex/relay one by
    startedAt. (The old code keyed by persona and kept one per persona, silently
    dropping same-persona siblings before they ever reached the hub — the bug.)
    A session with no resolvable base persona still reports (identity = pid)."""
    out = []
    seen = set()
    for reg in _reg_dirs():
        for f in sorted(glob.glob(str(reg) + "/*.json")):
            if f in seen:
                continue
            seen.add(f)
            try:
                d = json.load(open(f))
            except (OSError, ValueError):
                continue
            rt = d.get("runtime")
            if rt == "mcp-msgers" or not _addressable(d):
                continue
            pid = d.get("pid")
            sock = d.get("messagingSocketPath")
            if not (pid and sock and pathlib.Path(sock).exists()) or not _alive(pid):
                continue
            p = _persona(d) or (d.get("name") or f"pid-{pid}")
            out.append({"persona": p, "socket": sock,
                        "name": d.get("name"), "pid": pid,
                        "session_id": d.get("sessionId"),
                        "started": d.get("startedAt") or 0,
                        "_claude": _is_live_claude(rt)})
    # dedupe ONLY same-pid (a session registering into several dirs); the persona
    # stays a label, never the key, so same-persona siblings each survive.
    best = {}
    for s in out:
        cur = best.get(s["pid"])
        if cur is None:
            best[s["pid"]] = s
            continue
        if (s["_claude"], s["started"]) > (cur["_claude"], cur["started"]):
            best[s["pid"]] = s
    for s in best.values():
        s.pop("_claude", None)
    return list(best.values())


def unroutable_sessions() -> list[dict]:
    """Live non-interactive sessions (e.g. background jobs), for /who only.

    Reported so an operator can see why a name exists, never routed to."""
    out, seen = {}, set()
    for reg in _reg_dirs():
        for f in sorted(glob.glob(str(reg) + "/*.json")):
            if f in seen:
                continue
            seen.add(f)
            try:
                d = json.load(open(f))
            except (OSError, ValueError):
                continue
            if d.get("runtime") == "mcp-msgers" or _addressable(d):
                continue
            pid = d.get("pid")
            if not pid or not _alive(pid):
                continue
            out[pid] = {"persona": _persona(d) or (d.get("name") or f"pid-{pid}"),
                        "name": d.get("name"), "pid": pid, "kind": d.get("kind")}
    return list(out.values())


def degraded_sessions() -> list[dict]:
    """Registrations whose pid is ALIVE but whose messaging socket file is
    MISSING — the process is up yet its inbound socket is absent (the reboot
    window: a stale registration points at a cleared /run/user socket, or a
    process came up without re-running messaging init). These are NOT live
    (delivery would fail) but are more actionable than 'session ended', so we
    report them distinctly so a broken inbound window is a visible alert instead
    of a quiet 'not alive'.

    Pure detection: same scan as live_sessions(); keyed by UNIQUE pid so two
    different pid windows both go degraded without one shadowing the other. A
    registration is dropped here only if that same pid already has a genuinely
    live socket (then it is live, not degraded) — so /who never shows a pid as
    both live and socket_missing."""
    live_pids = {s["pid"] for s in live_sessions()}
    out, seen = [], set()
    for reg in _reg_dirs():
        for f in sorted(glob.glob(str(reg) + "/*.json")):
            if f in seen:
                continue
            seen.add(f)
            try:
                d = json.load(open(f))
            except (OSError, ValueError):
                continue
            rt = d.get("runtime")
            if rt == "mcp-msgers" or not _addressable(d):
                continue
            pid = d.get("pid")
            sock = d.get("messagingSocketPath")
            if not (pid and sock and _alive(pid)):
                continue
            if pathlib.Path(sock).exists():
                continue  # has a live socket -> not degraded
            if pid in live_pids:
                continue  # same pid already reported live (other reg dir)
            p = _persona(d) or (d.get("name") or f"pid-{pid}")
            out.append({"persona": p, "socket": sock,
                        "name": d.get("name"), "pid": pid,
                        "_claude": _is_live_claude(rt),
                        "_started": d.get("startedAt") or 0})
    # dedupe ONLY same-pid (a session registering into several dirs)
    best = {}
    for s in out:
        cur = best.get(s["pid"])
        if cur is None:
            best[s["pid"]] = s
            continue
        if (s["_claude"], s["_started"]) > (cur["_claude"], cur["_started"]):
            best[s["pid"]] = s
    for s in best.values():
        s.pop("_claude", None)
        s.pop("_started", None)
    return list(best.values())


def host_facts() -> dict:
    """Two bare, machine-level facts — the shim stays a dumb sensor: it reports
    WHAT it sees, and lets the hub derive any judgment (e.g. 'did the session
    environment come back after boot'). We do NOT compute a 'sessions_restored'
    boolean here — that is a judgment, and judgments live in the-hub-host (the owner's
    single-computation-point principle).
      tmux_active : is the tmux server process running? (sessions live in tmux)
      claude_procs: how many claude processes are running on this host?
    Both are cheap and OS-agnostic (pgrep/ps exist on mac + linux)."""
    tmux_active = False
    try:
        out = subprocess.run(["pgrep", "-x", "tmux"], capture_output=True,
                             text=True, timeout=5).stdout
        tmux_active = bool(out.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    claude_procs = 0
    try:
        out = subprocess.run(["ps", "-axo", "command="], capture_output=True,
                             text=True, timeout=8).stdout
        for line in out.splitlines():
            low = line.lower()
            if "claude" in low and "mcp-msgers" not in low:
                claude_procs += 1
    except (OSError, subprocess.SubprocessError):
        pass
    return {"tmux_active": tmux_active, "claude_procs": claude_procs}


def _report_degraded(degraded: list[dict], last_seen: set) -> set:
    """Log each degraded persona only on the moment its state changes, so the
    broken window surfaces as a visible alert (socket_missing) rather than
    repeated noise or silence. Returns the new seen-set for the next cycle.
    Reporting only — never touches delivery."""
    cur = {s["persona"]: s for s in degraded}
    for p, s in cur.items():
        if p not in last_seen:
            log("socket_missing", persona=p, pid=s.get("pid"),
                socket=s.get("socket"), name=s.get("name"))
    for p in last_seen:
        if p not in cur:
            log("socket_recovered", persona=p)
    return set(cur)


def _session_matches(s: dict, match: str) -> bool:
    """Does this session match a `#match` selector? `match` is the session's
    UNIQUE handle as shown by relay_who: its per-session `name` (e.g. 'alice-c7'),
    its pid, or a session_id / uuid prefix. Anything else is 'no match' (fail
    loud) rather than a silent guess."""
    if not match:
        return False
    if s.get("name") == match:
        return True
    if s.get("pid") is not None and str(s["pid"]) == match:
        return True
    sid = s.get("session_id")
    return bool(sid) and (sid == match or str(sid).startswith(match))


def local_socket_for(persona: str, match: str | None = None):
    """Resolve the messaging socket for a target session.

    - `match` given  -> the one session whose name/pid/session_id matches it;
                        None if no live session matches (fail loud, never guess).
    - `match` None   -> any live session with this persona. If exactly one, it.
                        If several same-persona sessions exist, the newest one —
                        an alias is a soft label, so 'bare alice' resolves to a
                        concrete session (newest), never to 'not found'. A caller
                        that must pick a specific one uses relay_who + `#match`."""
    cands = [s for s in live_sessions() if s["persona"] == persona]
    if not cands:
        return None
    if match:
        for s in cands:
            if _session_matches(s, match):
                return s["socket"]
        return None
    newest = max(cands, key=lambda s: (s.get("started") or 0, s["pid"]))
    return newest["socket"]


# ---------- self-identity: auto-resolve who I am, regardless of content -----
# The shim runs as a child of the Claude process that called the relay tool,
# so walking up its own process tree recovers the four facts with no config:
# tmux window, agent persona (from the agent's cwd), the process pid, and the
# machine. The sender supplies only the target; everything else is attached.

def _ppid(pid: int) -> int | None:
    out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)],
                         capture_output=True, text=True).stdout.strip()
    return int(out) if out.isdigit() else None


def _proc_cmd(pid: int) -> str:
    return subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                          capture_output=True, text=True).stdout.strip()


def _proc_cwd(pid: int) -> str:
    if os.path.exists(f"/proc/{pid}"):  # Linux: /proc/<pid>/cwd symlink
        try:
            return str(pathlib.Path(f"/proc/{pid}/cwd").resolve())
        except OSError:
            pass
    # macOS: /proc absent, resolve cwd via lsof
    try:
        out = subprocess.run(
            ["lsof", "-a", "-d", "cwd", "-Fn", "-p", str(pid)],
            capture_output=True, text=True, timeout=5).stdout
        for line in out.splitlines():
            if line.startswith("n"):
                return line[1:]
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def _find_claude_pid() -> int:
    cur = os.getpid()
    for _ in range(24):
        cmd = (_proc_cmd(cur) or "").lower()
        if "claude" in cmd and "mcp-msgers" not in cmd and "python" not in cmd:
            return cur
        p = _ppid(cur)
        if not p or p <= 1:
            break
        cur = p
    return os.getpid()


def _tmux_window(pid: int):
    try:
        out = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", "#{pane_pid} #{window_name}"],
            capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    pane = {}
    for line in out.splitlines():
        a = line.split(None, 1)
        if len(a) == 2 and a[0].isdigit():
            pane[int(a[0])] = a[1].strip()
    cur = pid
    for _ in range(24):
        if cur in pane:
            return pane[cur]
        p = _ppid(cur)
        if not p or p <= 1:
            return None
        cur = p
    return None


def self_identity() -> dict:
    """Auto-attach sender facts. Caller may not be under a tmux pane, so the
    window is best-effort; agent comes from the agent process's cwd."""
    claude_pid = _find_claude_pid()
    agent = _persona({"cwd": _proc_cwd(claude_pid), "name": ""})
    me = host_id()
    return {
        "agent": agent or me,          # 2. agent name
        "window": _tmux_window(claude_pid),  # 1. tmux window (may be None)
        "pid": claude_pid,             # 3. which process
        "host": me,                    # 4 is the target, supplied by the sender
        "from_name": f"{agent or me}@{me}",
    }


# ---------- hub client -------------------------------------------------------

def _post(path: str, body: dict, timeout: int = 8) -> dict:
    base = hub_url()
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(base + path, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _get(path: str, timeout: int) -> dict:
    base = hub_url()
    with urllib.request.urlopen(base + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def relay_send(to: str, message: str, from_name: str = "") -> dict:
    # to = "persona" (local) | "persona@host" | "persona@host#match". The #match
    # is the target's unique per-session handle (its name, e.g. 'alice-c7', or its
    # pid / a session_id prefix) as relay_who lists it. It is now ROUTED, not
    # stripped: a same-persona alias with several live sessions can be picked
    # precisely, and a wrong/dead target fails loudly instead of a false
    # "queued=1".
    base, _, match = to.partition("#")
    persona, _, to_host = base.partition("@")
    to_host = to_host or host_id()
    ident = self_identity()
    fn = from_name or ident["from_name"]
    return _post("/send", {"from_host": host_id(), "from_name": fn,
                           "to_host": to_host, "to_persona": persona,
                           "to_match": match or None,
                           "message": message, "from_window": ident.get("window"),
                           "from_pid": ident.get("pid")})


def relay_who() -> dict:
    return _get("/who", timeout=8)


def relay_status() -> dict:
    return _get("/status", timeout=8)


# ---------- native delivery into a local Claude session (predecessor-relay-compatible)

def native_send(dest_socket: str, from_name: str, body: str,
                window: str | None = None, pid: int | None = None):
    import uuid
    mid = str(uuid.uuid4())
    extra = ""
    if window:
        extra += f' from-window="{window}"'
    if pid:
        extra += f' from-pid="{pid}"'
    content = (f'<cross-session-message from="uds:shim-{host_id()}" '
               f'from-name="{from_name}" from-mode="bypass"{extra}>\n{body}\n'
               f'</cross-session-message>')
    payload = {"msgV": 1, "msg_id": mid, "type": "user",
               "message": {"role": "user", "content": content},
               "priority": "next", "from": f"uds:shim-{host_id()}"}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c:
        c.settimeout(5)
        c.connect(dest_socket)
        c.sendall(json.dumps(payload, ensure_ascii=False).encode() + b"\n")


def _ack(mid: str, ok: bool, error: str = "") -> None:
    """Report delivery outcome back to the hub so the sender gets a REAL
    queued/delivered/failed answer instead of a silent drop. Best-effort: if the
    hub is momentarily unreachable we just log — the message already left our
    outbox, so this is a status round-trip, never a re-delivery trigger."""
    try:
        _post("/ack", {"host": host_id(), "id": mid, "ok": ok,
                       "error": error}, timeout=4)
    except Exception as e:  # noqa: BLE001 — never let ack bookkeeping kill the worker
        log("ack_failed", id=mid, ok=ok, error=str(e))


def deliver(msg: dict):
    """Push an outbox message into a local session and report the real result.

    fail-loud: no resolvable local target, or the socket write errors, both
    report ok=False to the hub (so relay_send's caller sees the failure). A
    successful native_send reports ok=True. A bare alias that hit its newest
    sibling is a success by design — that is resolution, not failure."""
    persona = msg.get("to_persona")
    match = msg.get("to_match")
    mid = msg.get("id")
    sock = local_socket_for(persona, match)
    if not sock:
        log("no_local_target", persona=persona, match=match)
        if mid:
            _ack(mid, False, "no local session "
                 + (f"matching {match}" if match else f"for persona '{persona}'"))
        return
    try:
        native_send(sock, msg.get("from_name", ""), msg.get("message", ""),
                    window=msg.get("from_window"), pid=msg.get("from_pid"))
    except OSError as e:
        log("deliver_failed", persona=persona, match=match, error=str(e))
        if mid:
            _ack(mid, False, f"socket error: {e}")
        return
    log("delivered_local", to=f"{persona}@{host_id()}", match=match,
        from_name=msg.get("from_name"), window=msg.get("from_window"),
        pid=msg.get("from_pid"))
    if mid:
        _ack(mid, True)


# ---------- serve: worker loop ----------------------------------------------

def cmd_serve() -> int:
    base = hub_url()
    if not base:
        print(f"mcp-msgers shim: no hub_url in {config_path()}", file=sys.stderr)
        return 1
    me = host_id()
    print(f"mcp-msgers shim ready host={me} hub={base} pid={os.getpid()}",
          flush=True)
    last_hb = 0.0
    degraded_seen: set = set()
    while True:
        try:
            now = time.time()
            if now - last_hb >= HB_INTERVAL_S:
                sessions = live_sessions()
                degraded = degraded_sessions()
                # surface the "pid alive but socket gone" window as a visible alert
                degraded_seen = _report_degraded(degraded, degraded_seen)
                # report only bare host facts; the hub derives judgments (e.g.
                # sessions_restored) — the shim stays a dumb sensor
                _post("/hb", {"host": me, "sessions": sessions,
                              "degraded": degraded,
                              "unroutable": unroutable_sessions(),
                              "facts": host_facts(),
                              "caps": SHIM_CAPS}, timeout=6)
                last_hb = now
            r = _get(f"/poll?host={me}", timeout=POLL_TIMEOUT_S + 6)
            msg = r.get("message")
            if msg:
                deliver(msg)
        except urllib.error.HTTPError as e:
            log("http_error", code=e.code, path="poll/hb")
        except (urllib.error.URLError, OSError) as e:
            log("hub_unreachable", error=str(e))
            time.sleep(3)
        except Exception as e:  # noqa: BLE001 — keep the worker alive
            log("worker_error", error=str(e))
            time.sleep(1)


# ---------- mcp: stdio JSON-RPC server (newline-delimited) -------------------

TOOLS = [
    {"name": "relay_send",
     "description": "Send a message to a Claude persona session. `to` is 'persona' "
                    "for a local persona or 'persona@host' to reach another machine "
                    "(e.g. 'alice@host-b'). A persona may have several live sessions "
                    "on a host; bare 'persona' resolves to the newest one. To target "
                    "a specific one, append '#' + its unique name (see relay_who, "
                    "e.g. 'alice@host-b#alice-c7'). A target that is absent or dead "
                    "fails loudly (ok=false) instead of a silent drop. Reply routing "
                    "is automatic.",
     "inputSchema": {
         "type": "object",
         "properties": {
             "to": {"type": "string"},
             "message": {"type": "string"},
             "from_name": {"type": "string", "description": "optional sender label"}
         },
         "required": ["to", "message"]}},
    {"name": "relay_who",
     "description": "List every live persona session known to the hub, per host, "
                    "NOT collapsed by persona: each session appears once, carrying "
                    "its unique name, pid, and persona so you can address a specific "
                    "one via relay_send's '#' handle. Use to see who you can address.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "relay_status",
     "description": "Liveness of every host's shim (last heartbeat age, alive, "
                    "persona count). Use to tell 'other side is down' from 'wrong address'.",
     "inputSchema": {"type": "object", "properties": {}}},
]


def _rpc(id_, result, error=None):
    msg = {"jsonrpc": "2.0", "id": id_}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    return json.dumps(msg, ensure_ascii=False)


def cmd_mcp() -> int:
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            continue
        method = req.get("method")
        rid = req.get("id")
        if method == "initialize":
            resp = _rpc(rid, {"protocolVersion": "2024-11-05",
                              "capabilities": {"tools": {}},
                              "serverInfo": {"name": "mcp-msgers",
                                             "version": SHIM_VERSION}})
        elif method == "tools/list":
            resp = _rpc(rid, {"tools": TOOLS})
        elif method == "tools/call":
            name = req.get("params", {}).get("name")
            args = req.get("params", {}).get("arguments", {}) or {}
            try:
                if name == "relay_send":
                    r = relay_send(args["to"], args["message"],
                                   args.get("from_name", ""))
                elif name == "relay_who":
                    r = relay_who()
                elif name == "relay_status":
                    r = relay_status()
                else:
                    r = {"ok": False, "error": f"unknown tool {name}"}
                text = json.dumps(r, ensure_ascii=False)
                resp = _rpc(rid, {"content": [{"type": "text", "text": text}],
                                  "isError": not r.get("ok", True)})
            except Exception as e:  # noqa: BLE001
                resp = _rpc(rid, {"content": [{"type": "text",
                          "text": f"error: {e}"}], "isError": True})
        elif method == "notifications/initialized":
            continue  # notification: no response
        elif rid is None:
            continue  # any other notification
        else:
            resp = _rpc(rid, {}, error={"code": -32601, "message": "not found"})
        out.write(resp + "\n")
        out.flush()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="mcp-msgers")
    ap.add_argument("--version", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("serve")
    sub.add_parser("mcp")
    args = ap.parse_args()
    if args.version:
        print(SHIM_VERSION)
        return 0
    if args.cmd == "serve":
        return cmd_serve()
    if args.cmd == "mcp":
        return cmd_mcp()
    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
