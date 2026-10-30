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

SHIM_VERSION = "0.1.18"

# Shown to every agent at session start (MCP `initialize` -> `instructions`). The tools are
# lazy-loaded, so without this text an agent never learns the relay exists.
SERVER_INSTRUCTIONS = (
    "mcp-msgers is agent-to-agent messaging across machines. To message any agent, call relay_send "
    "with to=\"persona\" (same host) or to=\"persona@host\" (other machine), e.g. "
    "\"<persona>@<host>\"; add \"#session\" only to pick one of several live sessions. Call relay_who "
    "first to see who is live; a target that is absent fails loudly (ok=false) instead of "
    "silently dropping. Replies route back automatically. Do not use the built-in SendMessage for this: it only reaches "
    "sessions on the same machine."
)
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


# ---------- registry scan: live Claude persona sessions ----

def _reg_dirs() -> list[pathlib.Path]:
    """Every place a live session may register its socket file.

    Claude Code registers a session's messagingSocketPath under
    $CLAUDE_CONFIG_DIR/sessions (default ~/.claude/sessions). Scan the default,
    $CLAUDE_CONFIG_DIR/sessions if set, plus any explicit `registry_dirs` from the
    config (for sessions started with a different CLAUDE_CONFIG_DIR). Dirs that
    don't exist are skipped by the glob, so listing extra ones is harmless."""
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
    # ask whether a session WE registered (our uid) is still running; a pid now
    # owned by another uid (EPERM) can only be a reused pid, i.e. not our session
    # — so EPERM is treated as dead, matching the old `kill -0` result. This keeps
    # a stale json whose socket lingers from re-entering live_sessions.
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):   # ProcessLookupError or PermissionError → not our live session
        return False


_AGENTS_ROOT_CACHE: dict = {"ts": 0.0, "data": None, "last_good": None}
_AGENTS_ROOT_TTL = 60.0


def _read_agents_root():
    """The directory whose first-level subdirectories are agent homes (persona =
    subdirectory name), from the `agents_root` key of this shim's config file.
    Pure file read: no subprocess, no $PATH, so it works under a minimal
    launchd/systemd environment. Returns an absolute Path, or None when the key
    is absent or unreadable."""
    root = load_config().get("agents_root")
    if not root:
        return None
    try:
        return pathlib.Path(root).expanduser().resolve()
    except Exception:  # noqa: BLE001
        return None


def _agents_root():
    """Resolved absolute path of the agents root, with a 60s cache and a
    last-good fallback: a failed read returns the last successful value (and
    logs it); only a root that was never read successfully is None, which
    makes every persona unknown."""
    import time
    now = time.monotonic()
    cached = _AGENTS_ROOT_CACHE
    if cached["data"] is not None and now - cached["ts"] < _AGENTS_ROOT_TTL:
        return cached["data"]
    fresh = _read_agents_root()
    if fresh is not None:
        cached["ts"] = now
        cached["data"] = fresh
        cached["last_good"] = fresh
        return fresh
    if cached["last_good"] is not None:
        try:
            log("registry_stale_fallback",
                config=str(config_path()),
                last_good=str(cached["last_good"]))
        except Exception:  # noqa: BLE001
            pass
        cached["data"] = cached["last_good"]
        return cached["last_good"]
    return None


def _persona_from_registered_home(cwd):
    """Returns the first-level directory name under the agents root iff `cwd`
    is under it; None if not, or if the root is unknown (never a guess).
    Single source of truth: both `_persona()` and `_is_agent_home_cwd()` call it."""
    if not cwd:
        return None
    root = _agents_root()
    if root is None:
        return None
    try:
        p = pathlib.Path(cwd).resolve()
        rel = p.relative_to(root)
    except (ValueError, OSError):
        return None
    name = rel.parts[0] if rel.parts else ""
    return name or None


def _is_agent_home_cwd(cwd) -> bool:
    """A session's cwd is "an agent's home" iff it resolves under the configured
    agents root. Only this boolean leaves the machine; the cwd itself does not."""
    return _persona_from_registered_home(cwd) is not None


def _persona(r: dict):
    name = _persona_from_registered_home(r.get("cwd", ""))
    if name:
        return name
    # Registry unreachable or cwd not under a known home → unknown. We no longer
    # unit must not be addressable, otherwise peers would hit the model through
    return None


def _webchat_anchor() -> str | None:
    """Directory name that marks an optional webchat install (config key `webchat_dir`).
    None means webchat detection is off."""
    return load_config().get("webchat_dir") or None


def _is_webchat_claude_cwd(cwd: str) -> bool:
    """True iff `cwd` matches `<...>/<webchat_dir>/users/<login>/work`."""
    try:
        parts = pathlib.Path(cwd or "").parts
    except Exception:  # noqa: BLE001
        return False
    anchor = _webchat_anchor()
    if not anchor:
        return False
    for i, p in enumerate(parts):
        if (p == anchor and i + 3 < len(parts)
                and parts[i + 1] == "users" and parts[i + 3] == "work"
                and parts[i + 2]):
            return True
    return False


def _is_live_claude(runtime) -> bool:
    """A genuine live Claude session has no `runtime` (or runtime=='claude');
    codex and other tools are other runtimes that historically registered into
    the same ~/.claude/sessions dir and can otherwise shadow a live agent."""
    return runtime in (None, "claude")


TRANSCRIPT_SCAN_BYTES = 256 * 1024    # cap on how far back we read from the tail
TRANSCRIPT_SCAN_LINES = 300           # upper bound on lines examined inside the window
TRANSCRIPTS_ROOT = pathlib.Path("~/.claude/projects").expanduser()
# Claude Code writes exactly two limit sentences, as the text of an assistant API-error
#   "You've hit your session limit · resets 8:30pm (Asia/Singapore)"
#   "You've hit your weekly limit · resets Oct 22 at 11pm (Asia/Singapore)"   (no date when it is today)
# Nothing else means limited: any other API error is an ordinary failure and messages still go.
LIMIT_SENTENCE_RE = __import__("re").compile(
    r"^You've hit your (session|weekly) limit · resets "
    r"(?:([A-Z][a-z]{2}) (\d{1,2}) at )?(\d{1,2})(?::(\d{2}))?(am|pm) \(([^)]+)\)")
_MONTHS = {m: n for n, m in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}


def _transcript_paths(session_id: str) -> list[pathlib.Path]:
    """A session's transcript lives at ~/.claude/projects/<slug>/<session_id>.jsonl.
    The slug encodes the cwd; we glob so the shim doesn't need the project path."""
    if not session_id:
        return []
    return [pathlib.Path(p) for p in glob.glob(str(TRANSCRIPTS_ROOT / "*" / f"{session_id}.jsonl"))]


def _tail_lines(path: pathlib.Path, max_bytes: int, max_lines: int) -> list[str]:
    """Reverse-block read: seek from end, step back `BLOCK` bytes at a time until the window
    reaches `max_bytes` or the file start, then split by newline. Avoids loading a multi-MB
    transcript each heartbeat."""
    BLOCK = 32 * 1024
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            end = fh.tell()
            start = max(0, end - max_bytes)
            buf = b""
            pos = end
            # Walk backwards in BLOCK steps until we've grabbed up to `max_bytes` or hit `start`.
            while pos > start:
                step = min(BLOCK, pos - start)
                pos -= step
                fh.seek(pos)
                buf = fh.read(step) + buf
                if buf.count(b"\n") > max_lines + 2:
                    break
    except OSError:
        return []
    # Drop a possibly-truncated first line unless we actually reached file start.
    lines = buf.decode("utf-8", errors="replace").splitlines()
    if pos > start and lines:
        lines = lines[1:]
    return lines[-max_lines:]


def _limit_sentence(text: str, anchor_ts: float | None) -> tuple[str, str] | None:
    """(reason, resets_at as ISO 8601 UTC) when `text` is one of the two limit sentences; None for
    anything else. The clock time is read in the sentence's own time zone and resolved against the
    record's timestamp: no date means the next such time after the record, a date means that day
    (next year if it is already well past). An unknown zone or impossible time is None, not a guess."""
    import datetime as _dt
    import zoneinfo
    m = LIMIT_SENTENCE_RE.match(text or "")
    if not m or anchor_ts is None:
        return None
    kind, mon, day, hour, minute, ampm, tz = m.groups()
    try:
        zone = zoneinfo.ZoneInfo(tz)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return None
    hour, minute = int(hour), int(minute or 0)
    if not (1 <= hour <= 12 and 0 <= minute <= 59):
        return None
    hour = hour % 12 + (12 if ampm == "pm" else 0)
    anchor = _dt.datetime.fromtimestamp(anchor_ts, zone)
    try:
        if mon:
            if mon not in _MONTHS:
                return None
            target = anchor.replace(month=_MONTHS[mon], day=int(day), hour=hour, minute=minute,
                                    second=0, microsecond=0)
            if target < anchor - _dt.timedelta(days=1):
                target = target.replace(year=target.year + 1)
        else:
            target = anchor.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target <= anchor:
                target += _dt.timedelta(days=1)
    except ValueError:
        return None
    return f"{kind}_limit", target.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _scan_limited(session_id: str, root: pathlib.Path | None = None) -> dict:
    """Walk the session's transcript backwards (over a 256 KB window), assistant records only:

      - an API-error record whose text is one of the two limit sentences -> limited, with the
        reset instant from the sentence;
      - any other API-error record -> an ordinary failure: neither a limit nor a reply, keep walking;
      - a regular assistant record with text -> a reply: not limited, and `last_reply_at` is its time.

    Only API-error records count as a limit, so an agent quoting the sentence cannot limit itself."""
    none = {"limited": False, "limited_since": None, "reason": None, "resets_at": None, "last_reply_at": None}
    paths = _transcript_paths(session_id) if root is None else [
        p for p in (root / "projects").glob(f"*/{session_id}.jsonl") if p.is_file()]
    for p in paths:
        lines = _tail_lines(p, TRANSCRIPT_SCAN_BYTES, TRANSCRIPT_SCAN_LINES)
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except (ValueError, json.JSONDecodeError):
                continue
            if d.get("type") != "assistant":
                continue
            content = (d.get("message") or {}).get("content") or []
            texts = [x["text"] for x in content
                     if isinstance(x, dict) and isinstance(x.get("text"), str) and x["text"]] if isinstance(content, list) else []
            if d.get("isApiErrorMessage") is True:
                for t in texts:
                    hit = _limit_sentence(t, _iso_ts(d.get("timestamp")))
                    if hit:
                        return {"limited": True, "limited_since": d.get("timestamp"), "reason": hit[0],
                                "resets_at": hit[1], "last_reply_at": None}
                continue
            if texts:
                return none | {"last_reply_at": d.get("timestamp")}
    return none


def _iso_ts(value) -> float | None:
    """A transcript/notice ISO 8601 timestamp as epoch seconds; None if absent or unparsable."""
    import datetime as _dt
    if not isinstance(value, str) or not value:
        return None
    try:
        t = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (t if t.tzinfo else t.replace(tzinfo=_dt.timezone.utc)).timestamp()


def session_limited_state(session_id: str, root: pathlib.Path | None = None,
                          started_ms: float | None = None, now: float | None = None) -> dict:
    """Return the limit state of a session, read from its transcript and bound to this process and to time.

    A transcript outlives its process: a relaunch resumes the same session id under a new pid, so the
    last line can still be an old limit notice. Two machine facts clear the anchor:
      - the notice is older than this process's registration (startedAt, in milliseconds):
        limit_predates_process;
      - the reset instant named in the notice has passed: reset_passed.
    The hub then sends the queued messages and watches for a reply or a new limit notice."""
    r = _scan_limited(session_id, root)
    if not r["limited"]:
        return r
    since = _iso_ts(r.get("limited_since"))
    cleared = None
    if started_ms and since is not None and since < float(started_ms) / 1000.0:
        cleared = "limit_predates_process"
    else:
        reset = _iso_ts(r.get("resets_at"))
        if reset is not None and reset <= (time.time() if now is None else now):
            cleared = "reset_passed"
    if cleared:
        return {"limited": False, "limited_since": None, "reason": None, "resets_at": None,
                "last_reply_at": None, "cleared": cleared}
    return r


def _session_model_id(session_id: str, roots: list[pathlib.Path] | None = None) -> str | None:
    """Return the model the session is using.

    This is the model field of the newest assistant record in the transcript, so a model switch is
    followed. Synthetic records written by the runtime are not models. Returns None when no record
    names a model."""
    if not session_id:
        return None
    if roots is None:
        roots = [TRANSCRIPTS_ROOT] + [d.parent / "projects" for d in _reg_dirs()]
    paths, seen = [], set()
    for r in roots:
        for f in glob.glob(str(r / "*" / f"{session_id}.jsonl")):
            rp = os.path.realpath(f)
            if rp not in seen:
                seen.add(rp)
                paths.append(pathlib.Path(f))
    best = None   # (timestamp, model): the newest across every copy of the transcript
    for path in paths:
        for line in reversed(_tail_lines(path, TRANSCRIPT_SCAN_BYTES, TRANSCRIPT_SCAN_LINES)):
            try:
                d = json.loads(line)
            except (ValueError, json.JSONDecodeError):
                continue
            if d.get("type") != "assistant":
                continue
            model = (d.get("message") or {}).get("model")
            if not isinstance(model, str) or not model or model == "<synthetic>":
                continue
            ts = d.get("timestamp") or ""
            if best is None or ts > best[0]:
                best = (ts, model)
            break
    return best[1] if best else None


def _process_base_url(pid: int) -> tuple[bool, str | None]:
    """(readable, ANTHROPIC_BASE_URL) of a live process, read from the OS: /proc/<pid>/environ on
    Linux, `ps eww` on macOS. Only this one key is extracted; nothing else is kept or logged."""
    key = "ANTHROPIC_BASE_URL="
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            for item in f.read().split(b"\0"):
                if item.startswith(key.encode()):
                    return True, item[len(key):].decode(errors="replace")
            return True, None
    except FileNotFoundError:
        pass
    except OSError:
        return False, None
    try:
        r = subprocess.run(["ps", "eww", "-o", "command=", "-p", str(pid)],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False, None
    if r.returncode != 0 or not r.stdout.strip():
        return False, None
    for tok in r.stdout.split():
        if tok.startswith(key):
            return True, tok[len(key):]
    return True, None


_BASE_URL_CACHE: dict[tuple, tuple[bool, str | None]] = {}   # (pid, startedAt) -> readable result


def session_model(pid, session_id: str, roots=None, base_url_reader=None, started=None) -> dict:
    """Return {provider, id, label} from machine facts only, never from the session's own claim.

    id is the newest assistant record's model. provider is "anthropic" when the process has no
    ANTHROPIC_BASE_URL, and "gateway:<host:port>" of the URL it talks to otherwise. Unknown parts are
    reported as unknown."""
    import urllib.parse
    model_id = _session_model_id(session_id, roots) or "unknown"
    # A process's environment is fixed at exec, so a readable answer is cached per (pid, startedAt);
    # a reused pid has another startedAt and is read afresh.
    key = (pid, started)
    if base_url_reader is None and started and key in _BASE_URL_CACHE:
        readable, base = _BASE_URL_CACHE[key]
    else:
        readable, base = (base_url_reader or _process_base_url)(pid) if pid else (False, None)
        if base_url_reader is None and started and readable:
            _BASE_URL_CACHE[key] = (readable, base)
    if not readable:
        provider = "unknown"
    elif not base:
        provider = "anthropic"
    else:
        provider = "gateway:" + (urllib.parse.urlsplit(base).netloc or base)
    return {"provider": provider, "id": model_id, "label": f"{provider}/{model_id}"}


def _read_proc_argv(pid: int) -> list[str]:
    """Read /proc/<pid>/cmdline (Linux only; webchat is Linux-only). argv is
    NUL-separated, so paths with spaces survive — unlike `ps` output which
    needs shlex and corrupts quoted arguments. Returns [] on any read error."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            raw = f.read()
    except OSError:
        return []
    return [x.decode("utf-8", "replace") for x in raw.split(b"\0") if x]


def _read_proc_starttime(pid: int) -> int:
    """Field 22 of /proc/<pid>/stat (jiffies since boot, monotonic by start
    order). Used so `bare 'webchat' picks newest` is deterministic. Returns 0 on
    any read error — a stable low value puts the row last in newest-first sorts,
    which is the safe default."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return 0
    # /proc/<pid>/stat has `(comm)` possibly containing spaces/parens; split
    # on the LAST ')' so everything after it is space-delimited reliably.
    try:
        tail = data.rsplit(")", 1)[1].split()
        # After the ')' we have: state(0) ppid(1) ... starttime is overall
        # field 22 (1-indexed); comm+pid consumed 2 before ')'; so index here
        # is 22 - 3 = 19.
        return int(tail[19])
    except (IndexError, ValueError):
        return 0


def _ps_all_pids() -> list[int]:
    """Enumerate pids on this host by listing /proc. Linux only (webchat is).
    Returns [] when /proc is absent so callers degrade gracefully — tests
    monkeypatch this seam."""
    try:
        return [int(p) for p in __import__("os").listdir("/proc") if p.isdigit()]
    except (OSError, ValueError):
        return []


def _webchat_spec_from_argv(argv: list[str]) -> pathlib.Path | None:
    """Match `[python...] [<webchat-install>/session.py] [<absolute spec.json>]`.

    The match is anchored: argv[1] must have the basename session.py, and its path must contain the
    configured webchat_dir. With no webchat_dir nothing matches. An unanchored session.py name would
    catch any Python project with a file by that name. argv[2] must be absolute."""
    if len(argv) < 3:
        return None
    script = pathlib.Path(argv[1])
    if script.name != "session.py":
        return None
    anchor = _webchat_anchor()
    if not anchor or anchor not in str(script):
        return None
    spec = pathlib.Path(argv[2])
    if not spec.is_absolute():
        return None
    return spec


_WEBCHAT_REQUIRED_KEYS = ("user", "session_id", "socket")


def _read_webchat_spec(spec_path: pathlib.Path, enforce_uid: bool = True) -> dict | None:
    """Open the spec JSON with a uid guard (owner must be the shim's own uid;
    refuse otherwise). Returns the dict iff all required keys are present and
    non-empty, else None. Reasons logged for operator triage; the caller just
    sees None (fail loud up the stack)."""
    try:
        st = os.stat(spec_path)
    except OSError:
        log("webchat_spec_unreadable", path=str(spec_path))
        return None
    if enforce_uid and st.st_uid != os.geteuid():
        log("webchat_spec_uid_mismatch", path=str(spec_path),
            file_uid=st.st_uid, shim_uid=os.geteuid())
        return None
    try:
        spec = json.loads(spec_path.read_text())
    except (OSError, ValueError):
        log("webchat_spec_malformed", path=str(spec_path), reason="unreadable_or_bad_json")
        return None
    for key in _WEBCHAT_REQUIRED_KEYS:
        v = spec.get(key)
        if not (isinstance(v, str) and v):
            log("webchat_spec_malformed", path=str(spec_path), missing=key)
            return None
    sid = spec["session_id"]
    if len(sid) < 8:
        log("webchat_sid_too_short", path=str(spec_path), sid_len=len(sid))
        return None
    return spec


def _webchat_scan(enforce_uid: bool = True,
                  _list_pids=None, _read_argv=None, _starttime=None) -> list[dict]:
    """Produce webchat rows (persona "webchat") for every live webchat unit on this host.

    Identity comes from the session.py command line and its spec file, not from the runtime's state.
    All system reads (pid listing, argv, start time) go through seams resolved at call time, so tests
    can substitute fakes. A failed read returns no rows from that source and does not stop the scan."""
    list_pids = _list_pids or _ps_all_pids
    read_argv = _read_argv or _read_proc_argv
    starttime = _starttime or _read_proc_starttime
    rows = []
    for pid in list_pids():
        argv = read_argv(pid)
        if not argv:
            continue
        spec_path = _webchat_spec_from_argv(argv)
        if spec_path is None:
            continue
        if not _alive(pid):
            continue
        spec = _read_webchat_spec(spec_path, enforce_uid=enforce_uid)
        if spec is None:
            continue
        sock = spec["socket"]
        if not pathlib.Path(sock).exists():
            log("webchat_socket_missing", pid=pid, socket=sock)
            continue
        sid = spec["session_id"]
        rows.append({
            # sid8 — the conversation number the webchat UI already shows in
            # its session menu. No login prefix (one login can have many
            # concurrent conversations and the login is not an identity peers
            # address). `spec["user"]` stays unread here; its value is in the
            # spec file for operator lookup, not for routing.
            "persona": "webchat",
            "socket": sock,
            "name": sid[:8],
            "pid": pid,
            "session_id": sid,
            "started": starttime(pid),
            "agent_home": False,
            "limited": False,
            "limited_since": 0,
            "limited_reason": "",
            "resets_at": None,
            "webchat": True,  # explicit priority flag used by live_sessions dedup
        })
    return rows


def _addressable(d: dict) -> bool:
    """Only interactive sessions are persona endpoints.

    Claude Code background jobs (kind "bg", hosted by a `claude bg-spare`
    process) run in an agent's cwd and so resolve to that persona, but a message
    delivered to one never reaches the person's interactive session. Records
    without a kind predate the field and are interactive."""
    return (d.get("kind") or "interactive") == "interactive"


def _read_session_json(path, _retry_sleep_s: float = 0.02):
    """Load a session registration file, tolerating a concurrent writer.

    The registrar rewrites these files on each heartbeat, so a read can land mid-write and fail to
    parse. The read is retried once after a short pause, so a transient partial file does not drop a
    live session from the listing. A missing or unreadable file, or JSON that is still invalid on the
    retry, returns None and the row is skipped."""
    for attempt in (0, 1):
        try:
            with open(path) as fh:
                return json.load(fh)
        except ValueError:
            if attempt == 0:
                time.sleep(_retry_sleep_s)
                continue
            return None
        except OSError:
            return None
    return None


def live_sessions() -> list[dict]:
    """Live (non-shim) Claude persona sessions on this host.

    Each session is keyed by its UNIQUE process id (pid): a session's messaging
    socket is <socket dir>/<pid>.sock and a sender's from_pid is that same pid,
    so pid is the one identity that pins down a single session. The persona (the
    base agent name, e.g. 'user-a') is only a SOFT label: several DISTINCT sessions
    can share it (two agents/user-a windows) and each must be reported and
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
            d = _read_session_json(f)
            if d is None:
                continue
            rt = d.get("runtime")
            if rt == "mcp-msgers" or not _addressable(d):
                continue
            pid = d.get("pid")
            sock = d.get("messagingSocketPath")
            if not (pid and sock and pathlib.Path(sock).exists()) or not _alive(pid):
                continue
            # The Claude child of a webchat conversation must never become a
            # routable persona; sending to its name would reach the model. The
            # routable webchat row comes from `_webchat_scan()` below. This record
            # is dropped from both live and unroutable listings.
            if _is_webchat_claude_cwd(d.get("cwd", "")):
                continue
            p = _persona(d) or (d.get("name") or f"pid-{pid}")
            row_name = d.get("name")
            # Mark the row as limited so the hub can refuse `send` to a limited target before anything reaches its socket.
            lim = session_limited_state(d.get("sessionId") or "", started_ms=d.get("startedAt"))
            out.append({"persona": p, "socket": sock,
                        "name": row_name, "pid": pid,
                        "session_id": d.get("sessionId"),
                        "started": d.get("startedAt") or 0,
                        "agent_home": _is_agent_home_cwd(d.get("cwd")),
                        "limited": lim["limited"],
                        "limited_since": lim["limited_since"],
                        "limited_reason": lim["reason"],
                        "resets_at": lim.get("resets_at"),
                        "last_reply_at": lim.get("last_reply_at"),
                        "model": session_model(pid, d.get("sessionId") or "", started=d.get("startedAt")),
                        "_claude": _is_live_claude(rt)})
    # Webchat rows carry the pid of their session.py process, which is disjoint from runtime session pids.
    out.extend(_webchat_scan())
    # dedupe ONLY same-pid (a session registering into several dirs); persona
    # stays a label, never the key, so same-persona siblings each survive.
    # Rank by (is_claude_registry_entry, started): a live Claude record
    # outranks a stale codex/relay record for the same pid.
    best = {}
    for s in out:
        rank = (s.get("_claude", False), s.get("started", 0))
        cur = best.get(s["pid"])
        if cur is None or rank > cur[0]:
            best[s["pid"]] = (rank, s)
    out_rows = []
    for _, s in best.values():
        s.pop("_claude", None)
        s.pop("webchat", None)
        out_rows.append(s)
    return out_rows


def unroutable_sessions() -> list[dict]:
    """Return live non-interactive sessions (for example background jobs) for /who only.

    They are reported so an operator can see why a name exists, but they are never routed to.
    Webchat units' runtime children are not listed: live_sessions already excludes them, and listing
    them would show two rows per conversation."""
    out, seen = {}, set()
    for reg in _reg_dirs():
        for f in sorted(glob.glob(str(reg) + "/*.json")):
            if f in seen:
                continue
            seen.add(f)
            d = _read_session_json(f)
            if d is None:
                continue
            if d.get("runtime") == "mcp-msgers" or _addressable(d):
                continue
            if _is_webchat_claude_cwd(d.get("cwd", "")):
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
            d = _read_session_json(f)
            if d is None:
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
    boolean here — that is a judgment, and judgments live in the hub (a
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
    # Expose whether the agents root is known (config `agents_root`), so a shim
    # with no root configured surfaces loudly instead of letting every session
    # fall silent as persona=unknown.
    root = _agents_root()
    registry_missing = (root is None)
    registry_stale = bool(
        root is not None and _AGENTS_ROOT_CACHE.get("ts", 0.0) == 0.0
        and _AGENTS_ROOT_CACHE.get("last_good") is not None
    )
    return {
        "tmux_active": tmux_active,
        "claude_procs": claude_procs,
        "registry_missing": registry_missing,
        "registry_stale": registry_stale,
    }


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
    UNIQUE handle as shown by relay_who: its per-session `name` (e.g. 'user-a-c7'),
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
                        an alias is a soft label, so 'bare user-a' resolves to a
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


# Identity is not derived from these needles. It is anchored on the registered session pid (see _find_runtime_pid).
# This command-line needle table is kept only as an auxiliary string matcher
# (_match_runtime, tested as a unit); it never determines identity. Earlier the
# walk matched on these needles, which let a `zsh -c` shell-snapshot wrapper or a
# `python -c` whose ARGV merely mentioned a runtime be taken as that runtime
_RUNTIME_NEEDLES = [
    # (needle, kind, exclusions): needle in cmd_lower, no exclusion in cmd_lower.
    ("opencode", "opencode", ("mcp-msgers",)),
    ("codex", "codex", ("mcp-msgers",)),
    # "shell-snapshot": Claude Code runs a tool/MCP command through a wrapper
    # `/bin/zsh -c 'source ~/.claude/shell-snapshots/snapshot-*.sh && …'`. Its
    # command line contains ".claude" (so "claude" matches) but is a shell
    # wrapper, not the runtime. When the wrapped command has no "python" token
    # (for example a launcher script run through `/bin/sh`), the existing
    # "python" exclusion misses it and the wrapper is taken as the nearest claude
    # runtime, shadowing the real claude session pid → self_identity can't match a
    ("claude", "claude", ("mcp-msgers", "python", "shell-snapshot")),
]


def _runtime_needles():
    extra = []
    for item in load_config().get("extra_runtimes") or []:
        try:
            needle, excl = item[0], tuple(item[1]) if len(item) > 1 else ()
        except (TypeError, IndexError):
            continue
        extra.append((str(needle).lower(), ("mcp-msgers",) + tuple(str(x).lower() for x in excl)))
    claude = ("claude", ("mcp-msgers", "python") + tuple(n for n, _ in extra))
    return extra + _BUILTIN_RUNTIMES + [claude]  # "claude" last; the broadest match


def _match_runtime(cmd_lower: str) -> str | None:
    # The webchat match is anchored: the script name AND the configured webchat
    # directory must both appear.
    anchor = _webchat_anchor()
    if (anchor and "session.py" in cmd_lower and anchor.lower() in cmd_lower
            and "mcp-msgers" not in cmd_lower):
        return "webchat"
    for needle, kind, excl in _RUNTIME_NEEDLES:
        if needle in cmd_lower and not any(x in cmd_lower for x in excl):
            return kind
    return None


def _linux_btime() -> int:
    """Seconds since epoch at boot (/proc/stat `btime`), for converting a pid's
    jiffies-since-boot start time to an absolute epoch. 0 if unavailable."""
    try:
        with open("/proc/stat") as f:
            for line in f:
                if line.startswith("btime"):
                    return int(line.split()[1])
    except (OSError, ValueError):
        pass
    return 0


def _proc_info_linux(pid: int):
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            data = f.read().decode("utf-8", "replace")
        tail = data.rsplit(")", 1)[1].split()   # after `(comm)`: state(0) ppid(1) ... starttime(19)
        ppid = int(tail[1])
        ticks = int(tail[19])
        hz = os.sysconf("SC_CLK_TCK") or 100
        try:
            exe = os.readlink(f"/proc/{pid}/exe")
        except OSError:
            exe = ""
        return ppid, _linux_btime() + ticks / hz, exe
    except (OSError, ValueError, IndexError):
        return None


def _proc_info_macos(pid: int):
    """(ppid, epoch start, exe path) via libproc — no subprocess. `pbi_comm` is
    a truncated version string on these machines (e.g. '2.1.308'), so the runtime
    signal is the executable PATH from proc_pidpath, not the name. Returns None
    if the call fails (caller falls back to ps)."""
    try:
        import ctypes
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    except OSError:
        return None

    class _bsdinfo(ctypes.Structure):
        _fields_ = [("pbi_flags", ctypes.c_uint32), ("pbi_status", ctypes.c_uint32),
                    ("pbi_xstatus", ctypes.c_uint32), ("pbi_pid", ctypes.c_uint32),
                    ("pbi_ppid", ctypes.c_uint32), ("pbi_uid", ctypes.c_uint32),
                    ("pbi_gid", ctypes.c_uint32), ("pbi_ruid", ctypes.c_uint32),
                    ("pbi_rgid", ctypes.c_uint32), ("pbi_svuid", ctypes.c_uint32),
                    ("pbi_svgid", ctypes.c_uint32), ("rfu_1", ctypes.c_uint32),
                    ("pbi_comm", ctypes.c_char * 16), ("pbi_name", ctypes.c_char * 32),  # MAXCOMLEN / 2*MAXCOMLEN
                    ("pbi_nfiles", ctypes.c_uint32), ("pbi_pgid", ctypes.c_uint32),
                    ("pbi_pjobc", ctypes.c_uint32), ("e_tdev", ctypes.c_uint32),
                    ("e_tpgid", ctypes.c_uint32), ("pbi_nice", ctypes.c_int32),
                    ("pbi_start_tvsec", ctypes.c_uint64), ("pbi_start_tvusec", ctypes.c_uint64)]
    PROC_PIDTBSDINFO = 3
    libproc.proc_pidinfo.restype = ctypes.c_int
    libproc.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                                     ctypes.c_void_p, ctypes.c_int]
    info = _bsdinfo()
    n = libproc.proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, ctypes.byref(info), ctypes.sizeof(info))
    if n != ctypes.sizeof(info) or info.pbi_pid != pid:
        return None
    exe = ""
    try:
        libproc.proc_pidpath.restype = ctypes.c_int
        libproc.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        buf = ctypes.create_string_buffer(4096)
        if libproc.proc_pidpath(pid, buf, 4096) > 0:
            exe = buf.value.decode("utf-8", "replace")
    except OSError:
        pass
    return int(info.pbi_ppid), info.pbi_start_tvsec + info.pbi_start_tvusec / 1e6, exe


def _proc_info_ps(pid: int):
    """Fallback only: one `ps` for this pid, never per walk level. A failed
    direct read must not be read as 'no parent'. exe is '' (ps gives no kernel
    path), so the runtime-stop rule can't fire on a fallback — a DEGRADED case
    that may identify the parent instead of refusing; direct reads work on every
    deploy target (macOS libproc, Linux /proc), so this path is not expected."""
    r = subprocess.run(["ps", "-o", "ppid=,lstart=", "-p", str(pid)],
                       capture_output=True, text=True, stdin=subprocess.DEVNULL)
    out = r.stdout.strip()
    if not out:
        return None
    parts = out.split(None, 1)
    try:
        return int(parts[0]), time.mktime(time.strptime(parts[1].strip(), "%a %b %d %H:%M:%S %Y")), ""
    except (ValueError, IndexError):
        return None


def _proc_info(pid: int):
    """Return (ppid, start_epoch_seconds, exe_path) for a live pid, or None.

    Process facts are read directly: /proc on Linux, libproc on macOS. The process-tree walk therefore
    does not spawn ps once per level; ps is used only as a fallback when the direct read fails. Each
    walk visits a pid once, so there is no cache. The reuse guard is applied at the anchor."""
    if sys.platform.startswith("linux"):
        return _proc_info_linux(pid) or _proc_info_ps(pid)
    if sys.platform == "darwin":
        return _proc_info_macos(pid) or _proc_info_ps(pid)
    return _proc_info_ps(pid)


# One-sided slack for the start-time check. A process registers after it starts, so the rule
# kstart <= startedAt + epsilon admits every live session; epsilon only absorbs clock skew
# (for example Linux btime+ticks/hz). A reused pid starts long after the stale record's
# startedAt, so it is rejected.
_START_EPS_S = 5.0
# Runtime kinds recognised by executable path components.
_RUNTIME_PATH_TOKENS = ("claude", "codex", "opencode")


def _path_is_runtime(exe_path: str | None) -> bool:
    """Return True when a process's kernel executable path belongs to an agent runtime.

    The path comes from the kernel (proc_pidpath or /proc/<pid>/exe), never from argv. Matching is on
    whole path components rather than substrings: a binary under `.../share/claude/versions/<v>` or
    `.../bin/codex` matches, while a tool or virtualenv that merely has a runtime-like name in one of
    its directories does not."""
    if not exe_path:
        return False
    parts = exe_path.lower().replace("\\", "/").split("/")
    return any(t in parts for t in _RUNTIME_PATH_TOKENS)


def _reg_json_by_pid() -> dict[int, dict]:
    """Return {pid: registration} for every runtime session file in the registry directories.

    Only files are read here; the process walk compares start times separately with _proc_info. The
    shim's own rows are not runtimes. If the same pid appears in more than one directory, the record
    with the newest startedAt wins, so a stale record cannot shadow a live session that reuses its pid."""
    out: dict[int, dict] = {}
    seen = set()
    for reg in _reg_dirs():
        for f in sorted(glob.glob(str(reg) + "/*.json")):
            if f in seen:
                continue
            seen.add(f)
            d = _read_session_json(f)
            if not d:
                continue
            pid = d.get("pid")
            if not pid or d.get("runtime") == "mcp-msgers":
                continue
            pid = int(pid)
            prev = out.get(pid)
            if prev is None or (d.get("startedAt") or 0) > (prev.get("startedAt") or 0):
                out[pid] = d
    return out


def _is_webchat_pid(pid: int) -> bool:
    """Return True iff the pid is a verified webchat unit.

    This is a machine fact, not a name: the argv must have the session.py shape, and the spec file must
    be owned by the current user."""
    spec_path = _webchat_spec_from_argv(_read_proc_argv(pid) or [])
    if spec_path is None:
        return False
    return _read_webchat_spec(spec_path, enforce_uid=True) is not None


def _find_runtime_pid(start_pid: int | None = None) -> tuple[int | None, str | None]:
    """Walk the process tree upward from start_pid. Identity is anchored on the pid.

    An ancestor anchors only when:
      - it is a verified webchat unit. This always wins, because the unit owns the whole conversation; or
      - its pid is a registered session and the live process's kernel start time satisfies
        kstart <= startedAt + epsilon. A reused pid starts after the stale record's startedAt, so it fails.

    A name or argv match never anchors. If the walk meets a process whose kernel executable is a
    runtime but whose pid is not a registered anchor, it stops trusting non-webchat anchors above it
    and returns unknown instead of inheriting the parent's identity. Command-line text that merely
    mentions a runtime never triggers this. With no anchored ancestor up to init the result is
    (None, None), and the caller must refuse."""
    reg = _reg_json_by_pid()
    cur = start_pid if start_pid is not None else os.getpid()
    hits: list[tuple[int, str]] = []
    blocked = False   # passed an unregistered runtime: stop collecting non-webchat anchors
    for _ in range(64):
        info = _proc_info(cur)
        if _is_webchat_pid(cur):
            hits.append((cur, "webchat"))          # webchat always collected (and wins)
        elif info is not None:
            ppid, start, exe = info
            j = reg.get(cur)
            sa = (j or {}).get("startedAt")
            if j is not None and sa and start <= float(sa) / 1000.0 + _START_EPS_S:
                if not blocked:
                    hits.append((cur, j.get("runtime") or "claude"))
            elif _path_is_runtime(exe):
                blocked = True
        if info is None:
            break
        ppid = info[0]
        if not ppid or ppid <= 1:
            break
        cur = ppid
    for pid, kind in hits:
        if kind == "webchat":
            return pid, kind
    return hits[0] if hits else (None, None)


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
        info = _proc_info(cur)
        if info is None or not info[0] or info[0] <= 1:
            return None
        cur = info[0]
    return None


def self_identity(start_pid: int | None = None) -> dict:
    """Return the sender's identity, derived from machine facts.

    Walks the process tree upward from start_pid (default: the current pid) to the nearest ancestor
    anchored on its pid, then looks up that session's row. The identity is the pid, never a
    command-line name. With no anchored ancestor the result is agent "unknown" with registered=False,
    and the caller must refuse rather than guess. Environment variables and the working directory never
    participate; see _find_runtime_pid for the anchoring rule.

    start_pid is a parameter so that whoami(pid=...) can describe another process without duplicating
    the logic."""
    me = host_id()
    reported_pid = start_pid if start_pid is not None else os.getpid()
    runtime_pid, runtime_kind = _find_runtime_pid(start_pid=start_pid)
    if runtime_pid is None:
        log("self_identity_unknown", pid=reported_pid,
            reason="no agent runtime ancestor in process tree")
        return {"agent": "unknown", "session": None, "window": None,
                "pid": reported_pid, "host": me, "from_name": f"unknown@{me}",
                "registered": False}
    record = next((s for s in live_sessions() if s.get("pid") == runtime_pid), None)
    if record is None:
        # A runtime process with no live session row. Identity comes from the
        # session registration (pid → persona), NEVER from the process cwd
        # merely cwd'd under agents/<name> must not inherit that agent's identity
        # or authority; the earlier cwd-derivation reported such a caller as
        # <name>@host registered=False and mislabelled an unregistered caller).
        # Refuse rather than guess; the caller must treat unknown as refuse.
        log("self_identity_unknown", pid=runtime_pid, runtime=runtime_kind,
            reason="runtime process has no live session record")
        return {"agent": "unknown", "session": None, "window": None,
                "pid": runtime_pid, "host": me, "from_name": f"unknown@{me}",
                "registered": False, "runtime": runtime_kind}
    agent, session = record["persona"], record.get("name")
    from_name = f"{agent}@{me}" + (f"#{session}" if session else "")
    return {
        "agent": agent,
        "session": session,
        "window": _tmux_window(runtime_pid),
        "pid": runtime_pid,
        "host": me,
        "from_name": from_name,
        "registered": record is not None,
        "runtime": runtime_kind,
    }


# ---------- hub client -------------------------------------------------------

def _hub_headers(json_body: bool = False) -> dict:
    """Headers for a hub request. When the config names `hub_token_file`, its contents
    (one line) are sent as a bearer token; the hub checks it on every route except /health."""
    headers = {"Content-Type": "application/json"} if json_body else {}
    token_file = load_config().get("hub_token_file")
    if token_file:
        try:
            token = pathlib.Path(os.path.expanduser(token_file)).read_text().strip()
        except OSError:
            token = ""
        if token:
            headers["Authorization"] = "Bearer " + token
    return headers


def _post(path: str, body: dict, timeout: int = 8) -> dict:
    base = hub_url()
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(base + path, data=data, headers=_hub_headers(json_body=True))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _get(path: str, timeout: int) -> dict:
    base = hub_url()
    req = urllib.request.Request(base + path, headers=_hub_headers())
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def relay_send(to: str, message: str, from_name: str = "") -> dict:
    # to = "persona" (local) | "persona@host" | "persona@host#match". The #match
    # is the target's unique per-session handle (its name, e.g. 'user-a-c7', or its
    # pid / a session_id prefix) as relay_who lists it. It is now ROUTED, not
    # stripped: a same-persona alias with several live sessions can be picked
    # precisely, and a wrong/dead target fails loudly instead of a false
    # "queued=1".
    base, _, match = to.partition("#")
    persona, _, to_host = base.partition("@")
    to_host = to_host or host_id()
    ident = self_identity()
    # The caller's from_name is never the sender header: a label that disagrees
    # with the registered identity is ignored and logged (one line, no error).
    fn = ident["from_name"]
    if from_name and from_name != fn:
        log("from_name_ignored", given=from_name, used=fn, pid=ident.get("pid"))
    # A message written by a webchat conversation is a HUMAN message. The flag is machine-detected here from the
    # process tree; it is never trusted from a caller. Target shims deliver
    # human messages as plain body (no cross-session-message wrapper).
    is_human = bool(ident.get("runtime") == "webchat")
    payload = {"from_host": host_id(), "from_name": fn,
               "to_host": to_host, "to_persona": persona,
               "to_match": match or None,
               "message": message, "from_window": ident.get("window"),
               "from_pid": ident.get("pid")}
    if is_human:
        payload["human"] = True
        # The webchat session's short name (sid8) IS the identity row's
        # `name` — carry it so the receiving shim's delivered_local log
        # records which conversation the person's message came from
        # and the webchat sid").
        if ident.get("session"):
            payload["from_session_id"] = ident["session"]
    return _post("/send", payload)


def relay_who() -> dict:
    return _get("/who", timeout=8)


def relay_status() -> dict:
    return _get("/status", timeout=8)


# ---------- native delivery into a local Claude session

# Shim-side auto-wake. Per-pid local state tracks when to deliver a wake message to
# mark unresponsive. Wake message body is a FIXED TEMPLATE (ticket ids +
# resets_at only, no LLM content) per the global "no LLM variables in
WAKE_RETRY_INTERVAL_S = 60
WAKE_MAX_RETRIES = 3
# Wake launcher: a command run as `<wake_command> launch <persona>` to restart a persona
# whose session died while rate-limited. Set `wake_command` in the config file. When it is
# unset no auto-wake is attempted. Tests may override this variable.
AGENTS_LAUNCH_BIN = None
_WAKE_STATE: dict[int, dict] = {}  # pid -> {"next_fire_ts", "retries", "resets_at", "persona"}
# Pids seen limited on the previous tick, so that a disappearance between ticks
# is observable (that's the "dead pid" signal, distinct from an alive-but-
# unresponsive one which the retry cap handles).
_LAST_SEEN_LIMITED: dict[int, str] = {}  # pid -> persona


def _wake_scheduler_tick(sessions: list[dict], now: float | None = None) -> list[dict]:
    """Decide which sessions need a wake fire this tick. Returns a list of
    session dicts to fire against. Side-effects _WAKE_STATE in place.

    Pure scheduling — the fire itself happens in `_fire_wake` below so a
    tests can drive this without sending anything."""
    import datetime as _dt
    now = now if now is not None else time.time()
    to_fire = []
    seen_pids = {s.get("pid") for s in sessions if s.get("pid")}
    for s in sessions:
        pid = s.get("pid")
        if not pid:
            continue
        if not s.get("limited"):
            # Clear state when a session recovers so a future limit starts fresh.
            _WAKE_STATE.pop(pid, None)
            _LAST_SEEN_LIMITED.pop(pid, None)
            continue
        _LAST_SEEN_LIMITED[pid] = s.get("persona") or ""
        resets_at = s.get("resets_at")
        state = _WAKE_STATE.get(pid)
        if resets_at and resets_at != "unknown":
            # Branch A — parseable reset instant: fire at resets_at, then
            # three retries at WAKE_RETRY_INTERVAL_S each.
            try:
                resets_ts = _dt.datetime.strptime(resets_at, "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=_dt.timezone.utc).timestamp()
            except Exception:  # noqa: BLE001
                continue
            if state is None:
                _WAKE_STATE[pid] = {"next_fire_ts": resets_ts, "retries": 0,
                                     "resets_at": resets_at,
                                     "persona": s.get("persona") or "",
                                     "mode": "scheduled"}
                state = _WAKE_STATE[pid]
            if (state.get("mode", "scheduled") == "scheduled"
                    and now >= state["next_fire_ts"]
                    and state["retries"] < WAKE_MAX_RETRIES):
                to_fire.append(s)
                state["retries"] += 1
                state["next_fire_ts"] = now + WAKE_RETRY_INTERVAL_S
        else:
            # No resets_at (older shim) or an unknown reason: nothing to schedule.
            continue
    # Dead-pid detection: pids last seen limited that are no longer in the
    # heartbeat's live sessions. Their persona is captured so the launcher
    # can relaunch; state is purged (no infinite relaunch loop).
    dead_pids = [p for p in list(_LAST_SEEN_LIMITED) if p not in seen_pids]
    dead_personas: list[str] = []
    for p in dead_pids:
        persona = _LAST_SEEN_LIMITED.pop(p, "") or (_WAKE_STATE.get(p, {}).get("persona") or "")
        _WAKE_STATE.pop(p, None)
        if persona:
            dead_personas.append(persona)
    # Stash the dead-persona list for the caller to launch, on the function
    # object, so tests can read it without changing the return
    # type, kept compatible with call sites that expect only the
    # to_fire list).
    _wake_scheduler_tick.last_dead_personas = dead_personas  # type: ignore[attr-defined]
    return to_fire


def _launch_dead_persona(persona: str) -> list[str]:
    """Ask the configured launcher to start a persona whose session died. Returns the
    argv that was run, or an empty list when no launcher is configured."""
    import subprocess, shlex
    launcher = AGENTS_LAUNCH_BIN or load_config().get("wake_command")
    if not launcher:
        return []
    argv = [launcher, "launch", persona]
    try:
        subprocess.Popen(argv, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL,
                          start_new_session=True)
    except FileNotFoundError:
        # Fail loud but non-fatal: the scheduler continues and the next heartbeat retries.
        log("wake_launcher_missing", persona=persona, bin=launcher,
            argv_str=shlex.join(argv))
    return argv


def _wake_unresponsive_pids(sessions: list[dict]) -> list[int]:
    """Return the pids that have exhausted WAKE_MAX_RETRIES and are still
    limited. The hub-side companion sends a `queued_failed_unresponsive`
    receipt to each queued sender via the normal /send machinery; this
    helper is called by the heartbeat loop to drive that reporting."""
    out = []
    for s in sessions:
        pid = s.get("pid")
        if not (pid and s.get("limited")):
            continue
        state = _WAKE_STATE.get(pid)
        # A limit always names its reset, so every wake is scheduled and has a hard retry cap.
        if (state and state.get("mode") == "scheduled"
                and state["retries"] >= WAKE_MAX_RETRIES):
            out.append(pid)
    return out


def _fire_wake(session: dict, ticket_ids: list[str] | None = None) -> None:
    """Deliver a fixed-template wake message to a session's UDS socket.
    Format: a cross-session-message whose body is a bare JSON doc:
      {"wake": true, "resets_at": "...", "ticket_ids": [...]}
    No LLM-generated text, no human prose. The receiving Claude Code
    session reads the cross-session-message as a user turn and resumes."""
    sock = session.get("socket")
    if not sock:
        return
    import json as _json
    body = _json.dumps({
        "wake": True,
        "resets_at": session.get("resets_at"),
        "ticket_ids": ticket_ids or [],
    }, sort_keys=True)
    try:
        native_send(sock, from_name="limit-queue@hub", body=body,
                     window=None, pid=None, human=False)
    except OSError as e:
        log("wake_fire_failed", pid=session.get("pid"), socket=sock,
            error_type=type(e).__name__, error=str(e))


def build_envelope(from_name: str, body: str, window: str | None = None,
                   pid: int | None = None, model: str | None = None,
                   human: bool = False) -> str:
    """The text a receiving session reads as a user turn. Human input is delivered as
    plain text. Every other message is wrapped in a cross-session-message element. Attribute
    values are escaped, and a closing tag inside the body is neutralised, so message content
    cannot open a second envelope or add attributes. The from-mode value comes from config
    (`envelope_from_mode`, default "bypass")."""
    if human:
        return body
    from html import escape as _esc
    extra = ""
    if window:
        extra += f' from-window="{_esc(str(window))}"'
    if pid:
        extra += f' from-pid="{int(pid)}"'
    if model:
        extra += f' from-model="{_esc(str(model))}"'
    from_mode = _esc(str(load_config().get("envelope_from_mode", "bypass")))
    safe_body = body.replace("</cross-session-message", "&lt;/cross-session-message")
    return (f'<cross-session-message from="uds:shim-{_esc(host_id())}" '
            f'from-name="{_esc(str(from_name))}" from-mode="{from_mode}"{extra}>\n{safe_body}\n'
            f'</cross-session-message>')


def native_send(dest_socket: str, from_name: str, body: str,
                window: str | None = None, pid: int | None = None,
                human: bool = False, model: str | None = None):
    import uuid
    mid = str(uuid.uuid4())
    content = build_envelope(from_name, body, window=window, pid=pid, model=model, human=human)
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
    # Double-check limited state right before writing
    # to the UDS, to close the race between heartbeat and send. Find the session dict for
    # this persona/match in the live registry and re-scan its transcript.
    for s in live_sessions():
        if s["persona"] != persona:
            continue
        if match and not _session_matches(s, match):
            continue
        if s.get("socket") != sock:
            continue
        if s.get("limited"):
            log("deliver_refused_limited", persona=persona, match=match,
                since=s.get("limited_since"))
            if mid:
                _ack(mid, False, f"usage_limit_reached (since={s.get('limited_since')})")
            return
        break
    human = bool(msg.get("human"))
    try:
        native_send(sock, msg.get("from_name", ""), msg.get("message", ""),
                    window=msg.get("from_window"), pid=msg.get("from_pid"),
                    human=human, model=msg.get("from_model"))
    except OSError as e:
        log("deliver_failed", persona=persona, match=match, error=str(e))
        if mid:
            _ack(mid, False, f"socket error: {e}")
        return
    log("delivered_local", to=f"{persona}@{host_id()}", match=match,
        from_name=msg.get("from_name"), window=msg.get("from_window"),
        pid=msg.get("from_pid"), human=human, session_id=msg.get("from_session_id"))
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
    # Probe the agents root once at startup. If `agents_root` is not configured,
    # every session
    # on this host will identify as persona=unknown, so we surface it loudly
    # instead of letting the shim quietly drop messages.
    if _agents_root() is None:
        log("registry_missing",
            config=str(config_path()),
            effect="every session will identify as persona=unknown "
                   "(config has no agents_root)")
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
                # Phase 3b: pids that burned WAKE_MAX_RETRIES while still
                # limited. Hub translates each into a `queued_failed_
                # unresponsive` receipt to every queued sender for that
                # (host, pid), then drops those queue entries.
                unresponsive = _wake_unresponsive_pids(sessions)
                _post("/hb", {"host": me, "sessions": sessions,
                              "degraded": degraded,
                              "unroutable": unroutable_sessions(),
                              "facts": host_facts(),
                              "caps": SHIM_CAPS,
                              "unresponsive_pids": unresponsive}, timeout=6)
                # each limited session whose resets_at (parseable) is now
                # past, deliver one fixed-template wake message, up to
                # WAKE_MAX_RETRIES at WAKE_RETRY_INTERVAL_S apart.
                for s in _wake_scheduler_tick(sessions, now=now):
                    _fire_wake(s)
                # Phase 3b: dead-pid relaunch. A pid that was limited last
                # tick and vanished this tick is relaunched through the configured
                # wake launcher. Idempotent; the launcher silently no-ops
                # if the persona is already running.
                for persona in getattr(_wake_scheduler_tick,
                                        "last_dead_personas", []):
                    _launch_dead_persona(persona)
                last_hb = now
            r = _get(f"/poll?host={me}", timeout=POLL_TIMEOUT_S + 6)
            msg = r.get("message")
            if msg:
                # the hub already popped this message from its outbox the moment
                # /poll returned. If deliver() raises any exception that isn't
                # caught inside it (previously: anything other than OSError on
                # the socket write), the hub never learns and the sender gets
                # ok:true/queued:1 — silent loss. Wrap deliver() so EVERY
                # exception produces a loud ack.
                try:
                    deliver(msg)
                except Exception as e:  # noqa: BLE001
                    mid = msg.get("id")
                    log("deliver_crashed", id=mid,
                        error_type=type(e).__name__, error=str(e))
                    if mid:
                        _ack(mid, False,
                             f"deliver crashed: {type(e).__name__}: {e}")
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
                    "(e.g. 'user-a@host-b'). A persona may have several live sessions "
                    "on a host; bare 'persona' resolves to the newest one. To target "
                    "a specific one, append '#' + its unique name (see relay_who, "
                    "e.g. 'user-a@host-b#user-a-c7'). A target that is absent or dead "
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
    {"name": "whoami",
     "description": "Who is calling, as detected from the process tree (never from "
                    "any argument). Returns two fields: `caller` is ALWAYS the "
                    "machine-detected identity of this MCP session (persona@host#name, "
                    "runtime kind, pid); `queried` is populated only when `pid` is "
                    "passed and names the record for THAT pid without changing "
                    "`caller`. Passing someone else's pid cannot change who you are. "
                    "Both the caller's and the queried record's identity come from "
                    "self_identity()'s process-tree walk — env vars and argument-set "
                    "names are ignored.",
     "inputSchema": {
         "type": "object",
         "properties": {
             "pid": {"type": "integer",
                     "description": "optional: a pid to describe in the queried field. "
                                    "Does not change the caller fields."}
         }}},
]


def whoami(pid: int | None = None) -> dict:
    """Two-field identity report. `caller` always comes from THIS MCP session's
    process tree (self_identity with no arg); `queried` is populated iff `pid`
    is given, by calling self_identity(start_pid=pid). A caller cannot change
    its own identity by passing another pid."""
    caller = self_identity()
    if pid is None:
        return {"ok": True, "caller": caller, "queried": None}
    try:
        pid_i = int(pid)
    except (TypeError, ValueError):
        return {"ok": False, "caller": caller, "queried": None,
                "error": "pid must be an integer"}
    queried = self_identity(start_pid=pid_i)
    queried["queried_pid"] = pid_i
    return {"ok": True, "caller": caller, "queried": queried}


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
                                             "version": SHIM_VERSION},
                              "instructions": SERVER_INSTRUCTIONS})
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
                elif name == "whoami":
                    r = whoami(args.get("pid"))
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
