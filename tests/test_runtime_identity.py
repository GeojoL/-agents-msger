#!/usr/bin/env python3
"""Runtime identity: process-tree anchoring, registry handling and webchat detection."""
from __future__ import annotations

import os
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import shim as shim_mod
shim_mod._webchat_anchor = lambda: "example-webchat"  # noqa: E402


PASS = []
FAIL = []


def check(label: str, cond: bool, detail: str = ""):
    (PASS if cond else FAIL).append(label)
    print(("PASS " if cond else "FAIL ") + label + (f"  [{detail}]" if (detail and not cond) else ""))


# ---------------------- runtime walker: nearest-known-runtime wins

#   _proc_info(pid)     -> (ppid, start_epoch, exe_path)   [the ONLY tree read]
#   _reg_json_by_pid()  -> {pid: {startedAt(ms), runtime}} [the registration set]
#   _is_webchat_pid(pid)-> bool                            [verified clw unit]
# A pid anchors only when it is registered AND start <= startedAt/1000 + eps.
_NOW = 1_000_000_000.0           # a plausible epoch (seconds)
_OK_STARTED = int(_NOW * 1000)   # json startedAt (ms) for an anchored pid
_OK_START = _NOW - 1.0           # kernel start just before it → anchored (one-sided)


def _mk_proc(spec):
    """spec: {pid: (ppid, start_epoch, exe_path)}. Patches _proc_info — the single
    walk seam. A pid absent from spec reads as dead (None)."""
    return lambda pid: spec.get(pid)


def _mk_reg(regmap):
    """regmap: {pid: {'startedAt': ms, 'runtime': kind|None}}. Patches
    _reg_json_by_pid (file-only registration set, no process reads)."""
    return lambda: dict(regmap)


def test_codex_under_claude_parent_is_identified_as_codex():
    """Codex under claude parent is identified as codex."""
    # 999 shim → 500 codex (registered) → 300 claude → 1
    proc = _mk_proc({
        999: (500, _OK_START, "/usr/bin/python3"),
        500: (300, _OK_START, "/opt/codex/bin/codex"),
        300: (1,   _OK_START, "/x/.local/share/claude/versions/9.9.9"),
    })
    reg = _mk_reg({500: {"startedAt": _OK_STARTED, "runtime": "codex"},
                   300: {"startedAt": _OK_STARTED, "runtime": None}})
    saved = (shim_mod._proc_info, shim_mod._reg_json_by_pid, shim_mod._is_webchat_pid)
    try:
        shim_mod._proc_info, shim_mod._reg_json_by_pid = proc, reg
        shim_mod._is_webchat_pid = lambda p: False
        pid, kind = shim_mod._find_runtime_pid(start_pid=999)
        check("nearest anchored runtime is codex (not the parent claude)",
              pid == 500 and kind == "codex", f"pid={pid} kind={kind}")
    finally:
        (shim_mod._proc_info, shim_mod._reg_json_by_pid, shim_mod._is_webchat_pid) = saved


def test_pure_claude_tree_is_claude():
    proc = _mk_proc({
        999: (300, _OK_START, "/usr/bin/python3"),
        300: (1,   _OK_START, "/x/.local/share/claude/versions/9.9.9"),
    })
    reg = _mk_reg({300: {"startedAt": _OK_STARTED, "runtime": None}})
    saved = (shim_mod._proc_info, shim_mod._reg_json_by_pid, shim_mod._is_webchat_pid)
    try:
        shim_mod._proc_info, shim_mod._reg_json_by_pid = proc, reg
        shim_mod._is_webchat_pid = lambda p: False
        pid, kind = shim_mod._find_runtime_pid(start_pid=999)
        check("plain claude tree → runtime=claude",
              pid == 300 and kind == "claude", f"pid={pid} kind={kind}")
    finally:
        (shim_mod._proc_info, shim_mod._reg_json_by_pid, shim_mod._is_webchat_pid) = saved


def test_claude_local_registers_as_claude_kind():
    """Claude local registers as claude kind."""
    proc = _mk_proc({
        999: (400, _OK_START, "/usr/bin/python3"),
        400: (1,   _OK_START, "/x/.local/share/claude/versions/9.9.9"),
    })
    reg = _mk_reg({400: {"startedAt": _OK_STARTED, "runtime": None}})
    saved = (shim_mod._proc_info, shim_mod._reg_json_by_pid, shim_mod._is_webchat_pid)
    try:
        shim_mod._proc_info, shim_mod._reg_json_by_pid = proc, reg
        shim_mod._is_webchat_pid = lambda p: False
        pid, kind = shim_mod._find_runtime_pid(start_pid=999)
        check("local harness session → kind=claude (unified at the process layer)",
              pid == 400 and kind == "claude", f"pid={pid} kind={kind}")
    finally:
        (shim_mod._proc_info, shim_mod._reg_json_by_pid, shim_mod._is_webchat_pid) = saved


def test_no_runtime_in_tree_returns_none():
    """No runtime in tree returns none."""
    proc = _mk_proc({
        999: (200, _OK_START, "/bin/bash"),
        200: (1,   _OK_START, "/usr/lib/systemd/systemd"),
    })
    saved = (shim_mod._proc_info, shim_mod._reg_json_by_pid, shim_mod._is_webchat_pid)
    try:
        shim_mod._proc_info = proc
        shim_mod._reg_json_by_pid = _mk_reg({})
        shim_mod._is_webchat_pid = lambda p: False
        pid, kind = shim_mod._find_runtime_pid(start_pid=999)
        check("no anchored runtime found → (None, None)",
              pid is None and kind is None, f"pid={pid} kind={kind}")
    finally:
        (shim_mod._proc_info, shim_mod._reg_json_by_pid, shim_mod._is_webchat_pid) = saved


def test_pid_reuse_is_not_anchored_but_50s_lag_is():
    """Pid reuse is not anchored but 50s lag is."""
    reg = _mk_reg({700: {"startedAt": _OK_STARTED, "runtime": None}})
    saved = (shim_mod._proc_info, shim_mod._reg_json_by_pid, shim_mod._is_webchat_pid)
    try:
        shim_mod._reg_json_by_pid = reg
        shim_mod._is_webchat_pid = lambda p: False
        # reuse: kernel start 100s AFTER startedAt → fails kstart <= startedAt + eps
        shim_mod._proc_info = _mk_proc({
            999: (700, _OK_START, "/usr/bin/python3"),
            700: (1,   _NOW + 100.0, "/x/.local/share/claude/versions/9.9.9")})
        pid, kind = shim_mod._find_runtime_pid(start_pid=999)
        check("reused pid (kstart after startedAt) is NOT anchored → unknown",
              pid is None and kind is None, f"pid={pid} kind={kind}")
        # 50s lag: kernel start 50s BEFORE startedAt → still anchored
        shim_mod._proc_info = _mk_proc({
            999: (700, _OK_START, "/usr/bin/python3"),
            700: (1,   _NOW - 50.0, "/x/.local/share/claude/versions/9.9.9")})
        pid, kind = shim_mod._find_runtime_pid(start_pid=999)
        check("50s-lag session (kstart before startedAt) IS anchored",
              pid == 700 and kind == "claude", f"pid={pid} kind={kind}")
    finally:
        (shim_mod._proc_info, shim_mod._reg_json_by_pid, shim_mod._is_webchat_pid) = saved


def test_unregistered_codex_child_is_unknown_not_parent_claude():
    """Unregistered codex child is unknown not parent claude."""
    proc = _mk_proc({
        999: (500, _OK_START, "/usr/bin/python3"),
        500: (300, _OK_START, "/opt/codex/bin/codex"),     # runtime exe, NOT registered
        300: (1,   _OK_START, "/x/.local/share/claude/versions/9.9.9"),  # registered claude parent
    })
    reg = _mk_reg({300: {"startedAt": _OK_STARTED, "runtime": None}})   # only the parent is registered
    saved = (shim_mod._proc_info, shim_mod._reg_json_by_pid, shim_mod._is_webchat_pid)
    try:
        shim_mod._proc_info, shim_mod._reg_json_by_pid = proc, reg
        shim_mod._is_webchat_pid = lambda p: False
        pid, kind = shim_mod._find_runtime_pid(start_pid=999)
        check("unregistered codex child → unknown, NOT the parent claude",
              pid is None and kind is None, f"pid={pid} kind={kind}")
    finally:
        (shim_mod._proc_info, shim_mod._reg_json_by_pid, shim_mod._is_webchat_pid) = saved


def test_wrapper_argv_mentioning_runtime_does_not_anchor():
    """Wrapper argv mentioning runtime does not anchor."""
    proc = _mk_proc({
        999: (700, _OK_START, "/usr/bin/python3"),          # exe is python; argv irrelevant
        700: (500, _OK_START, "/bin/sh"),                   # /bin/sh run.sh ...
        500: (300, _OK_START, "/bin/zsh"),
        300: (1,   _OK_START, "/x/.local/share/claude/versions/9.9.9"),
    })
    reg = _mk_reg({300: {"startedAt": _OK_STARTED, "runtime": None}})
    saved = (shim_mod._proc_info, shim_mod._reg_json_by_pid, shim_mod._is_webchat_pid)
    try:
        shim_mod._proc_info, shim_mod._reg_json_by_pid = proc, reg
        shim_mod._is_webchat_pid = lambda p: False
        pid, kind = shim_mod._find_runtime_pid(start_pid=999)
        check("wrapper/argv words ignored → walk reaches the registered claude",
              pid == 300 and kind == "claude", f"pid={pid} kind={kind}")
    finally:
        (shim_mod._proc_info, shim_mod._reg_json_by_pid, shim_mod._is_webchat_pid) = saved


# ---------------------- self_identity: refuses when no runtime, ignores env

def test_self_identity_unknown_when_no_runtime():
    saved = (shim_mod._proc_info, shim_mod._reg_json_by_pid, shim_mod._is_webchat_pid,
             shim_mod.host_id, shim_mod.live_sessions)
    try:
        shim_mod._proc_info = _mk_proc({os.getpid(): (1, _OK_START, "/bin/bash")})
        shim_mod._reg_json_by_pid = _mk_reg({})
        shim_mod._is_webchat_pid = lambda p: False
        shim_mod.host_id = lambda: "examplehost"
        shim_mod.live_sessions = lambda: []
        ident = shim_mod.self_identity()
        check("no runtime: agent='unknown' + refuse",
              ident["agent"] == "unknown" and ident["registered"] is False
              and ident["from_name"] == "unknown@examplehost", f"{ident}")
    finally:
        (shim_mod._proc_info, shim_mod._reg_json_by_pid, shim_mod._is_webchat_pid,
         shim_mod.host_id, shim_mod.live_sessions) = saved


def test_self_identity_ignores_mcp_msgers_from_env():
    """Self identity ignores mcp msgers from env."""
    rec = {"persona": "test_agent_1", "name": "test_agent_1-9f", "pid": 777,
           "socket": "/tmp/x", "session_id": "s", "started": 1}
    saved = (shim_mod._find_runtime_pid, shim_mod.host_id, shim_mod._tmux_window,
             shim_mod.live_sessions)
    try:
        shim_mod._find_runtime_pid = lambda start_pid=None: (777, "claude")
        shim_mod.host_id = lambda: "test_host_2"
        shim_mod._tmux_window = lambda pid: None
        shim_mod.live_sessions = lambda: [rec]
        os.environ["MCP_MSGERS_FROM"] = "webchat@test_host_2#conv-xx"
        ident = shim_mod.self_identity()
        check("env set: identity is still the registered one",
              ident["from_name"] == "test_agent_1@test_host_2#test_agent_1-9f"
              and ident["agent"] == "test_agent_1", f"{ident}")
    finally:
        os.environ.pop("MCP_MSGERS_FROM", None)
        (shim_mod._find_runtime_pid, shim_mod.host_id, shim_mod._tmux_window,
         shim_mod.live_sessions) = saved


# ---------------------- webchat cwd rule

def test_webchat_cwd_is_not_a_routable_persona_rev3():
    """Webchat cwd is not a routable persona rev3."""
    rec = {"cwd": "/srv/example-webchat/users/test_user_1/work",
           "sessionId": "1a2b3c4d-5e6f-7890-abcd-ef0123456789", "name": "work-01"}
    check("webchat cwd recognised by _is_webchat_claude_cwd",
          shim_mod._is_webchat_claude_cwd(rec["cwd"]) is True)
    check("webchat cwd does NOT yield a 'webchat' persona through _persona",
          shim_mod._persona(rec) is None,
          detail=str(shim_mod._persona(rec)))


def test_webchat_pattern_requires_full_shape():
    """A cwd that only partially resembles the pattern (missing `work` tail) is
    rejected by the webchat recognizer."""
    partial = "/srv/example-webchat/users/test_user_1"
    check("partial pattern (no /work) is not webchat",
          shim_mod._is_webchat_claude_cwd(partial) is False)


def test_non_webchat_cwd_still_uses_agents_rule():
    """Regression: `agents/<x>` continues to win over everything else."""
    rec = {"cwd": "/srv/example/agents/test_agent_1", "sessionId": "s", "name": "test_agent_1-01"}
    saved_read = shim_mod._read_agents_root
    saved_cache = dict(shim_mod._AGENTS_ROOT_CACHE)
    try:
        root = pathlib.Path("/srv/example/agents").resolve()
        shim_mod._read_agents_root = lambda: root
        shim_mod._AGENTS_ROOT_CACHE.update({"ts": 0.0, "data": None, "last_good": None})
        check("agents/<x> wins", shim_mod._persona(rec) == "test_agent_1")
    finally:
        shim_mod._read_agents_root = saved_read
        shim_mod._AGENTS_ROOT_CACHE.clear()
        shim_mod._AGENTS_ROOT_CACHE.update(saved_cache)


# ---------------------- rev3: _webchat_scan + collision guards ---------------
import json  # noqa: E402
import tempfile  # noqa: E402


def _write_spec(spec_path: pathlib.Path, **fields) -> dict:
    """Build a spec with sensible defaults, write it with mode 0600,
    return the dict."""
    sid = fields.pop("session_id", "aabbccdd-ffff-1111-2222-333344445555")
    user = fields.pop("user", "test_user_1")
    sock = fields.pop("socket", None)
    if sock is None:
        sock_p = spec_path.parent / "s.sock"
        sock_p.touch()
        sock = str(sock_p)
    spec = {"user": user, "session_id": sid, "socket": sock,
            "argv": [], "resume": False, "notify": "",
            "events": "", "idle_seconds": 1800, "stop_grace_seconds": 10}
    spec.update(fields)
    spec_path.write_text(json.dumps(spec))
    os.chmod(spec_path, 0o600)
    return spec


_WEBCHAT_SCRIPT = "/opt/example-webchat/server/session.py"


def _mk_scan_seams(pid: int, spec_path: pathlib.Path, started: int = 1_000_000):
    """Mk scan seams."""
    def list_pids():
        return [pid]
    def read_argv(p):
        return ["/usr/bin/python3", _WEBCHAT_SCRIPT, str(spec_path)] if p == pid else []
    def starttime(p):
        return started if p == pid else 0
    return list_pids, read_argv, starttime


def test_webchat_scan_emits_webchat_row_from_session_py():
    """Webchat scan emits webchat row from session py."""
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="rev3-"))
    spec_path = tmp / "spec.json"
    _write_spec(spec_path, user="test_user_1",
                session_id="aabbccdd-ffff-1111-2222-333344445555")
    list_pids, read_argv, starttime = _mk_scan_seams(9001, spec_path, 1234567)
    saved = shim_mod._alive
    try:
        shim_mod._alive = lambda pid: pid == 9001
        rows = shim_mod._webchat_scan(enforce_uid=True,
                                       _list_pids=list_pids,
                                       _read_argv=read_argv,
                                       _starttime=starttime)
    finally:
        shim_mod._alive = saved
    check("exactly one webchat row produced", len(rows) == 1, detail=str(rows))
    if rows:
        r = rows[0]
        check("row persona = webchat", r["persona"] == "webchat")
        check("row name = sid8 only (no login prefix, test_agent_10 2026-10-24)",
              r["name"] == "aabbccdd", detail=r.get("name"))
        check("row pid = session.py pid", r["pid"] == 9001)
        check("row started comes from /proc/<pid>/stat seam",
              r["started"] == 1234567, detail=str(r["started"]))
        check("delivery target = spec.socket, NOT example-socks",
              r["socket"].endswith("s.sock") and "example-socks" not in r["socket"],
              detail=r["socket"])


def test_webchat_scan_refuses_uid_mismatch():
    """Spec file owned by another uid → row dropped."""
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="rev3-uid-"))
    spec_path = tmp / "spec.json"
    _write_spec(spec_path, user="test_user_1",
                session_id="aabbccdd-ffff-1111-2222-333344445555")
    saved_stat = os.stat
    def fake_stat(p, *a, **kw):
        s = saved_stat(p, *a, **kw)
        if str(p) == str(spec_path):
            class _S:
                st_uid = s.st_uid + 1
                st_mode = s.st_mode
            return _S()
        return s
    list_pids, read_argv, starttime = _mk_scan_seams(9002, spec_path)
    saved = (shim_mod._alive, shim_mod.os.stat)
    try:
        shim_mod._alive = lambda pid: True
        shim_mod.os.stat = fake_stat
        rows = shim_mod._webchat_scan(enforce_uid=True,
                                       _list_pids=list_pids,
                                       _read_argv=read_argv,
                                       _starttime=starttime)
    finally:
        (shim_mod._alive, shim_mod.os.stat) = saved
    check("uid-mismatched spec → no row", len(rows) == 0, detail=str(rows))


def test_webchat_scan_refuses_malformed_spec():
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="rev3-mal-"))
    spec_path = tmp / "spec.json"
    _write_spec(spec_path, user="")
    list_pids, read_argv, starttime = _mk_scan_seams(9003, spec_path)
    saved = shim_mod._alive
    try:
        shim_mod._alive = lambda pid: True
        rows = shim_mod._webchat_scan(enforce_uid=True,
                                       _list_pids=list_pids,
                                       _read_argv=read_argv,
                                       _starttime=starttime)
    finally:
        shim_mod._alive = saved
    check("empty user → no row", len(rows) == 0, detail=str(rows))


def test_webchat_scan_refuses_short_session_id():
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="rev3-sid-"))
    spec_path = tmp / "spec.json"
    _write_spec(spec_path, session_id="short")
    list_pids, read_argv, starttime = _mk_scan_seams(9004, spec_path)
    saved = shim_mod._alive
    try:
        shim_mod._alive = lambda pid: True
        rows = shim_mod._webchat_scan(enforce_uid=True,
                                       _list_pids=list_pids,
                                       _read_argv=read_argv,
                                       _starttime=starttime)
    finally:
        shim_mod._alive = saved
    check("sid < 8 chars → no row", len(rows) == 0, detail=str(rows))


def test_webchat_scan_pid_listing_failure_returns_no_rows_quietly():
    """Webchat scan pid listing failure returns no rows quietly."""
    saved = shim_mod._alive
    try:
        shim_mod._alive = lambda pid: True
        rows = shim_mod._webchat_scan(enforce_uid=True,
                                       _list_pids=lambda: [],
                                       _read_argv=lambda p: [],
                                       _starttime=lambda p: 0)
    finally:
        shim_mod._alive = saved
    check("empty pid listing → [] cleanly", rows == [], detail=str(rows))


def test_session_py_outside_webchat_install_is_not_webchat():
    """Session py outside webchat install is not webchat."""
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="rev3-anchor-"))
    spec_path = tmp / "spec.json"
    _write_spec(spec_path, user="test_user_1",
                session_id="aabbccdd-ffff-1111-2222-333344445555")
    def list_pids():
        return [9005]
    def read_argv(p):
        return ["/usr/bin/python3", "/opt/other-tool/server/session.py", str(spec_path)]
    saved = shim_mod._alive
    try:
        shim_mod._alive = lambda pid: True
        rows = shim_mod._webchat_scan(enforce_uid=True,
                                       _list_pids=list_pids,
                                       _read_argv=read_argv,
                                       _starttime=lambda p: 0)
    finally:
        shim_mod._alive = saved
    check("session.py outside example-webchat is NOT a webchat row",
          rows == [], detail=str(rows))
    # And the runtime walker agrees:
    check("_match_runtime does not label a non-webchat session.py",
          shim_mod._match_runtime("/usr/bin/python3 /opt/other-tool/server/session.py x") != "webchat")


def test_find_runtime_pid_recognises_webchat():
    """Find runtime pid recognises webchat."""
    proc = _mk_proc({
        9100: (9101, _OK_START, "/usr/bin/python3"),
        9101: (1,    _OK_START, "/usr/bin/python3"),
    })
    saved = (shim_mod._proc_info, shim_mod._reg_json_by_pid, shim_mod._is_webchat_pid)
    try:
        shim_mod._proc_info, shim_mod._reg_json_by_pid = proc, _mk_reg({})
        shim_mod._is_webchat_pid = lambda p: p == 9101
        pid, kind = shim_mod._find_runtime_pid(9100)
    finally:
        (shim_mod._proc_info, shim_mod._reg_json_by_pid, shim_mod._is_webchat_pid) = saved
    check("walks to session.py as webchat runtime",
          (pid, kind) == (9101, "webchat"), detail=f"got={(pid, kind)}")


def test_find_runtime_pid_claude_child_of_session_py_is_webchat_not_claude():
    """Find runtime pid claude child of session py is webchat not claude."""
    proc = _mk_proc({
        9200: (9201, _OK_START, "/usr/bin/python3"),
        9201: (9202, _OK_START, "/x/.local/share/claude/versions/9.9.9"),  # claude child
        9202: (1,    _OK_START, "/usr/bin/python3"),
    })
    saved = (shim_mod._proc_info, shim_mod._reg_json_by_pid, shim_mod._is_webchat_pid)
    try:
        shim_mod._proc_info = proc
        shim_mod._is_webchat_pid = lambda p: p == 9202
        # (a) inner claude registered
        shim_mod._reg_json_by_pid = _mk_reg({9201: {"startedAt": _OK_STARTED, "runtime": None}})
        pid, kind = shim_mod._find_runtime_pid(9200)
        check("registered claude child of session.py → WEBCHAT",
              (pid, kind) == (9202, "webchat"), detail=f"got={(pid, kind)}")
        # (b) inner claude NOT registered (stop rule fires, webchat still wins)
        shim_mod._reg_json_by_pid = _mk_reg({})
        pid, kind = shim_mod._find_runtime_pid(9200)
        check("UNregistered claude child of session.py → still WEBCHAT",
              (pid, kind) == (9202, "webchat"), detail=f"got={(pid, kind)}")
    finally:
        (shim_mod._proc_info, shim_mod._reg_json_by_pid, shim_mod._is_webchat_pid) = saved


def test_find_runtime_pid_tool_subshell_under_session_py_is_also_webchat():
    """Find runtime pid tool subshell under session py is also webchat."""
    proc = _mk_proc({
        9300: (9301, _OK_START, "/usr/bin/python3"),
        9301: (9302, _OK_START, "/bin/bash"),
        9302: (1,    _OK_START, "/usr/bin/python3"),
    })
    saved = (shim_mod._proc_info, shim_mod._reg_json_by_pid, shim_mod._is_webchat_pid)
    try:
        shim_mod._proc_info, shim_mod._reg_json_by_pid = proc, _mk_reg({})
        shim_mod._is_webchat_pid = lambda p: p == 9302
        pid, kind = shim_mod._find_runtime_pid(9300)
    finally:
        (shim_mod._proc_info, shim_mod._reg_json_by_pid, shim_mod._is_webchat_pid) = saved
    check("tool subshell under session.py resolves to webchat",
          (pid, kind) == (9302, "webchat"), detail=f"got={(pid, kind)}")


def test_find_runtime_pid_claude_without_webchat_still_claude():
    """Regression: a plain claude session (no webchat ancestor) must still
    resolve to its own claude pid, not look for a webchat upstream."""
    proc = _mk_proc({
        9400: (9401, _OK_START, "/usr/bin/python3"),
        9401: (1,    _OK_START, "/x/.local/share/claude/versions/9.9.9"),
    })
    reg = _mk_reg({9401: {"startedAt": _OK_STARTED, "runtime": None}})
    saved = (shim_mod._proc_info, shim_mod._reg_json_by_pid, shim_mod._is_webchat_pid)
    try:
        shim_mod._proc_info, shim_mod._reg_json_by_pid = proc, reg
        shim_mod._is_webchat_pid = lambda p: False
        pid, kind = shim_mod._find_runtime_pid(9400)
    finally:
        (shim_mod._proc_info, shim_mod._reg_json_by_pid, shim_mod._is_webchat_pid) = saved
    check("plain claude (no webchat ancestor) still resolves as claude",
          (pid, kind) == (9401, "claude"), detail=f"got={(pid, kind)}")


def test_from_name_identical_for_model_and_session_py_paths():
    """From name identical for model and session py paths."""
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="paired-"))
    spec_path = tmp / "spec.json"
    _write_spec(spec_path, user="someuser",
                session_id="aabbccdd-ffff-1111-2222-333344445555")
    saved = (shim_mod._proc_info, shim_mod._reg_json_by_pid, shim_mod._alive,
             shim_mod._ps_all_pids, shim_mod._read_proc_argv,
             shim_mod._read_proc_starttime, shim_mod.host_id,
             shim_mod._tmux_window, shim_mod.os.getpid)
    try:
        shim_mod._ps_all_pids = lambda: [9500]
        shim_mod._read_proc_argv = lambda p: (
            ["/usr/bin/python3", _WEBCHAT_SCRIPT, str(spec_path)] if p == 9500 else [])
        shim_mod._read_proc_starttime = lambda p: 1
        shim_mod._alive = lambda pid: pid in (9500, 9501, 9502, 9503)
        shim_mod.host_id = lambda: "host-x"
        shim_mod._tmux_window = lambda pid: None
        # spec file; no runtime is registered (webchat wins regardless).
        shim_mod._reg_json_by_pid = _mk_reg({})
        shim_mod._proc_info = _mk_proc({
            9501: (9500, _OK_START, "/usr/bin/python3"),
            9500: (1,    _OK_START, "/usr/bin/python3"),
        })
        shim_mod.os.getpid = lambda: 9501
        ident_a = shim_mod.self_identity()
        shim_mod._proc_info = _mk_proc({
            9503: (9502, _OK_START, "/usr/bin/python3"),
            9502: (9500, _OK_START, "/x/.local/share/claude/versions/9.9.9"),
            9500: (1,    _OK_START, "/usr/bin/python3"),
        })
        shim_mod.os.getpid = lambda: 9503
        ident_b = shim_mod.self_identity()
    finally:
        (shim_mod._proc_info, shim_mod._reg_json_by_pid, shim_mod._alive,
         shim_mod._ps_all_pids, shim_mod._read_proc_argv,
         shim_mod._read_proc_starttime, shim_mod.host_id,
         shim_mod._tmux_window, shim_mod.os.getpid) = saved
    check("page-path from_name = webchat@host-x#aabbccdd",
          ident_a["from_name"] == "webchat@host-x#aabbccdd",
          detail=ident_a["from_name"])
    check("model-path from_name = webchat@host-x#aabbccdd (same as page)",
          ident_b["from_name"] == "webchat@host-x#aabbccdd",
          detail=ident_b["from_name"])
    check("both paths yield IDENTICAL from_name",
          ident_a["from_name"] == ident_b["from_name"],
          detail=f"a={ident_a['from_name']} b={ident_b['from_name']}")


def test_live_sessions_hides_claude_child_of_webchat_rev3():
    """Live sessions hides claude child of webchat rev3."""
    import glob as glob_mod
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="rev3-live-"))
    regdir = tmp / "sessions"; regdir.mkdir()

    # (a) a Claude session record whose cwd puts it inside a webchat worker
    claude_sock = tmp / "example-socks" / "7777.sock"
    claude_sock.parent.mkdir()
    claude_sock.touch()
    claude_rec = {
        "pid": 7777, "sessionId": "aabbccdd-ffff-1111-2222-333344445555",
        "cwd": "/srv/example-webchat/users/test_user_1/work",
        "startedAt": 1000, "messagingSocketPath": str(claude_sock),
        "name": "work-01", "status": "idle", "kind": "interactive",
    }
    (regdir / "7777.json").write_text(json.dumps(claude_rec))

    spec_path = tmp / "spec.json"
    sock_p = tmp / "s.sock"; sock_p.touch()
    _write_spec(spec_path, user="test_user_1",
                session_id="aabbccdd-ffff-1111-2222-333344445555",
                socket=str(sock_p))

    saved = (shim_mod._reg_dirs, shim_mod._alive, shim_mod._ps_all_pids,
             shim_mod._read_proc_argv, shim_mod._read_proc_starttime,
             shim_mod._agents_root, shim_mod.session_limited_state)
    try:
        shim_mod._reg_dirs = lambda: [regdir]
        shim_mod._alive = lambda pid: pid in (7777, 9200)
        shim_mod._ps_all_pids = lambda: [9200]
        shim_mod._read_proc_argv = lambda p: (
            ["/usr/bin/python3", _WEBCHAT_SCRIPT, str(spec_path)] if p == 9200 else [])
        shim_mod._read_proc_starttime = lambda p: 7777000 if p == 9200 else 0
        shim_mod._agents_root = lambda: None
        shim_mod.session_limited_state = lambda sid, root=None: {
            "limited": False, "limited_since": 0, "reason": ""}
        sess = shim_mod.live_sessions()
        unr = shim_mod.unroutable_sessions()
    finally:
        (shim_mod._reg_dirs, shim_mod._alive, shim_mod._ps_all_pids,
         shim_mod._read_proc_argv, shim_mod._read_proc_starttime,
         shim_mod._agents_root, shim_mod.session_limited_state) = saved

    webchat_rows = [s for s in sess if s.get("persona") == "webchat"]
    check("exactly ONE webchat row for the conversation", len(webchat_rows) == 1,
          detail=f"got {len(webchat_rows)}: {webchat_rows}")
    if webchat_rows:
        r = webchat_rows[0]
        check("webchat row pid = session.py's pid, NOT Claude's",
              r["pid"] == 9200, detail=f"pid={r['pid']}")
        check("webchat row socket = spec.socket, NOT example-socks",
              r["socket"] == str(sock_p) and "example-socks" not in r["socket"],
              detail=r["socket"])
        check("webchat row name = sid8 only (no login prefix)",
              r["name"] == "aabbccdd", detail=r.get("name"))

    claude_live = [s for s in sess if s.get("pid") == 7777]
    check("Claude child of webchat is NOT in live_sessions",
          claude_live == [], detail=str(claude_live))

    # unroutable_sessions either — listing it there with persona="webchat" would
    # mean /who shows two webchat rows per conversation.
    unr_claude = [u for u in unr if u.get("pid") == 7777]
    check("Claude child of webchat is NOT in unroutable_sessions",
          unr_claude == [], detail=str(unr_claude))


# ---------------------- rev3: whoami caller/queried split ---------------------

def test_whoami_caller_matches_self_identity_when_no_pid():
    """whoami.caller == self_identity() verbatim; whoami.queried is None."""
    saved = shim_mod.self_identity
    fake = {"agent": "test_agent_1", "session": "test_agent_1-9f", "pid": 4242, "host": "k",
            "from_name": "test_agent_1@k#test_agent_1-9f", "registered": True, "runtime": "claude",
            "window": None}
    try:
        shim_mod.self_identity = lambda start_pid=None: fake if start_pid is None else {}
        r = shim_mod.whoami()
    finally:
        shim_mod.self_identity = saved
    check("whoami ok=true", r.get("ok") is True)
    check("whoami.caller == self_identity()", r["caller"] == fake)
    check("whoami.queried is None when no pid passed", r.get("queried") is None)


def test_whoami_pid_does_not_change_caller_fields():
    """Whoami pid does not change caller fields."""
    proc = _mk_proc({
        4242: (1, _OK_START, "/x/.local/share/claude/versions/9.9.9"),
        5151: (1, _OK_START, "/x/.local/share/claude/versions/9.9.9"),
    })
    reg = _mk_reg({4242: {"startedAt": _OK_STARTED, "runtime": None},
                   5151: {"startedAt": _OK_STARTED, "runtime": None}})
    test_agent_1_sess = {"persona": "test_agent_1", "name": "test_agent_1-9f", "pid": 4242,
                "socket": "/tmp/test_agent_1.sock", "session_id": "sid-test_agent_1"}
    test_agent_2_sess = {"persona": "test_agent_2", "name": "test_agent_2-32", "pid": 5151,
                 "socket": "/tmp/test_agent_2.sock", "session_id": "sid-test_agent_2"}
    saved = (shim_mod.live_sessions, shim_mod._proc_info, shim_mod._reg_json_by_pid,
             shim_mod._is_webchat_pid, shim_mod.host_id, shim_mod._tmux_window,
             shim_mod.os.getpid)
    try:
        shim_mod.live_sessions = lambda: [test_agent_1_sess, test_agent_2_sess]
        shim_mod._proc_info, shim_mod._reg_json_by_pid = proc, reg
        shim_mod._is_webchat_pid = lambda p: False
        shim_mod.host_id = lambda: "host-x"
        shim_mod._tmux_window = lambda pid: None
        shim_mod.os.getpid = lambda: 4242
        r = shim_mod.whoami(pid=5151)
    finally:
        (shim_mod.live_sessions, shim_mod._proc_info, shim_mod._reg_json_by_pid,
         shim_mod._is_webchat_pid, shim_mod.host_id, shim_mod._tmux_window,
         shim_mod.os.getpid) = saved
    check("whoami.caller.from_name = test_agent_1 (unchanged by pid arg)",
          r["caller"]["from_name"] == "test_agent_1@host-x#test_agent_1-9f",
          detail=r["caller"]["from_name"])
    check("whoami.caller.pid is test_agent_1's pid (4242), not 5151",
          r["caller"]["pid"] == 4242, detail=str(r["caller"]["pid"]))
    check("whoami.queried.agent == test_agent_2",
          r["queried"]["agent"] == "test_agent_2", detail=str(r["queried"]))
    check("whoami.queried.queried_pid == 5151 (round-trip)",
          r["queried"]["queried_pid"] == 5151)
    check("whoami.queried.from_name != caller.from_name",
          r["queried"]["from_name"] != r["caller"]["from_name"])


def test_whoami_queried_unknown_pid_reports_unknown():
    proc = _mk_proc({
        42: (1, _OK_START, "/x/.local/share/claude/versions/9.9.9"),
        9999: (1, _OK_START, "/usr/bin/random-tool"),
    })
    reg = _mk_reg({42: {"startedAt": _OK_STARTED, "runtime": None}})
    test_agent_1_sess = {"persona": "test_agent_1", "name": "test_agent_1-9f", "pid": 42,
                "socket": "/tmp/x", "session_id": "s"}
    saved = (shim_mod.live_sessions, shim_mod._proc_info, shim_mod._reg_json_by_pid,
             shim_mod._is_webchat_pid, shim_mod.host_id, shim_mod._tmux_window,
             shim_mod.os.getpid)
    try:
        shim_mod.live_sessions = lambda: [test_agent_1_sess]
        shim_mod._proc_info, shim_mod._reg_json_by_pid = proc, reg
        shim_mod._is_webchat_pid = lambda p: False
        shim_mod.host_id = lambda: "host-x"
        shim_mod._tmux_window = lambda pid: None
        shim_mod.os.getpid = lambda: 42
        r = shim_mod.whoami(pid=9999)
    finally:
        (shim_mod.live_sessions, shim_mod._proc_info, shim_mod._reg_json_by_pid,
         shim_mod._is_webchat_pid, shim_mod.host_id, shim_mod._tmux_window,
         shim_mod.os.getpid) = saved
    check("queried unknown pid → registered=False",
          r["queried"]["registered"] is False)
    check("queried unknown pid → agent=unknown",
          r["queried"]["agent"] == "unknown")
    check("caller still fully set when queried is unknown",
          r["caller"]["agent"] == "test_agent_1")


def test_whoami_pid_non_integer_fails_loud():
    saved = shim_mod.self_identity
    try:
        shim_mod.self_identity = lambda start_pid=None: {
            "agent": "x", "pid": 1, "host": "h",
            "from_name": "x@h", "registered": False}
        r = shim_mod.whoami(pid="not-a-pid")
    finally:
        shim_mod.self_identity = saved
    check("non-integer pid → ok=false", r["ok"] is False)
    check("even on error, caller is reported", r["caller"]["agent"] == "x")


def test_shell_snapshot_wrapper_is_not_a_runtime():
    """Shell snapshot wrapper is not a runtime."""
    # neutral paths; no real host/home
    wrapper = "/bin/zsh -c source /srv/example/.claude/shell-snapshots/snapshot-zsh-1.sh && /bin/sh run.sh /x/front.py mcp"
    check("shell-snapshot wrapper is not matched as a runtime",
          shim_mod._match_runtime(wrapper.lower()) is None, f"got {shim_mod._match_runtime(wrapper.lower())}")
    check("a real claude command still matches", shim_mod._match_runtime("claude --effort low --project x") == "claude")
    check("an unconfigured local harness is not matched as a runtime",
          shim_mod._match_runtime("node /x/local-harness/cli.js") is None)
    # 999 python front → 700 sh launcher → 500 shell-snapshot zsh wrapper → 300 real claude → 1
    # None of 999/700/500 is a registered anchor, and none has a runtime EXE path,
    # so the walk skips them all and anchors on the registered claude (300).
    proc = _mk_proc({
        999: (700, _OK_START, "/usr/bin/python3"),
        700: (500, _OK_START, "/bin/sh"),
        500: (300, _OK_START, "/bin/zsh"),
        300: (1,   _OK_START, "/x/.local/share/claude/versions/9.9.9"),
    })
    reg = _mk_reg({300: {"startedAt": _OK_STARTED, "runtime": None}})
    saved = (shim_mod._proc_info, shim_mod._reg_json_by_pid, shim_mod._is_webchat_pid)
    try:
        shim_mod._proc_info, shim_mod._reg_json_by_pid = proc, reg
        shim_mod._is_webchat_pid = lambda p: False
        pid, kind = shim_mod._find_runtime_pid(start_pid=999)
        check("walk reaches the real claude (300), not the shell-snapshot zsh (500)",
              pid == 300 and kind == "claude", f"pid={pid} kind={kind}")
    finally:
        (shim_mod._proc_info, shim_mod._reg_json_by_pid, shim_mod._is_webchat_pid) = saved


def test_session_json_read_retries_once_on_mid_rewrite():
    """Session json read retries once on mid rewrite."""
    import json as _json
    good = {"persona": "x", "pid": 1}
    calls = {"n": 0}
    real_load = _json.load

    def flaky_load(fh):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("partial json (mid-rewrite)")
        return good
    saved_load, saved_sleep = shim_mod.json.load, shim_mod.time.sleep
    try:
        shim_mod.json.load = flaky_load
        shim_mod.time.sleep = lambda s: None
        got = shim_mod._read_session_json(__file__)   # path only needs to open; loader is faked
        check("mid-rewrite read retried once and succeeded", got == good and calls["n"] == 2, f"got={got} n={calls['n']}")
        # still broken on the retry → None (row skipped), never a crash
        calls["n"] = 0
        shim_mod.json.load = lambda fh: (_ for _ in ()).throw(ValueError("still bad"))
        check("still-broken json → None after the retry", shim_mod._read_session_json(__file__) is None)
    finally:
        shim_mod.json.load, shim_mod.time.sleep = saved_load, saved_sleep


def test_identity_never_derived_from_cwd_when_unregistered():
    """Identity never derived from cwd when unregistered."""
    saved = (shim_mod.live_sessions, shim_mod._find_runtime_pid, shim_mod.host_id,
             shim_mod._tmux_window, shim_mod._proc_cwd, shim_mod._persona)
    try:
        shim_mod._find_runtime_pid = lambda start_pid=None: (4242, "claude")
        shim_mod.live_sessions = lambda: []                         # runtime pid NOT registered
        shim_mod._proc_cwd = lambda pid: "/x/agents/test_agent_1"            # cwd under an agent home
        shim_mod._persona = lambda r: "test_agent_1"
        shim_mod.host_id = lambda: "examplehost"
        shim_mod._tmux_window = lambda pid: None
        ident = shim_mod.self_identity()
        check("unregistered runtime is unknown, not the cwd persona",
              ident["agent"] == "unknown" and ident["registered"] is False
              and ident["from_name"] == "unknown@examplehost", f"{ident}")
    finally:
        (shim_mod.live_sessions, shim_mod._find_runtime_pid, shim_mod.host_id,
         shim_mod._tmux_window, shim_mod._proc_cwd, shim_mod._persona) = saved


def test_reg_json_newest_startedAt_wins_across_dirs():
    """Reg json newest startedAt wins across dirs."""
    a = pathlib.Path(tempfile.mkdtemp(prefix="regA-"))
    b = pathlib.Path(tempfile.mkdtemp(prefix="regB-"))
    (a / "555.json").write_text(json.dumps({"pid": 555, "startedAt": 1000, "name": "stale"}))
    (b / "555.json").write_text(json.dumps({"pid": 555, "startedAt": 9_000_000_000_000, "name": "live"}))
    saved = shim_mod._reg_dirs
    try:
        shim_mod._reg_dirs = lambda: [a, b]   # A (stale) scanned first, like ~/.claude/sessions
        m = shim_mod._reg_json_by_pid()
    finally:
        shim_mod._reg_dirs = saved
    check("newest startedAt wins across dirs (live json, not the stale one)",
          m.get(555, {}).get("name") == "live", detail=str(m.get(555)))


def test_macos_proc_info_matches_ps_and_is_not_none():
    """Macos proc info matches ps and is not none."""
    if sys.platform != "darwin":
        check("macos proc_info verification (skipped: not darwin)", True)
        return
    pid = os.getpid()
    direct = shim_mod._proc_info_macos(pid)
    check("proc_info_macos returns non-None (ctypes struct size correct)",
          direct is not None, detail=str(direct))
    if direct:
        ppid, start, exe = direct
        check("proc_info_macos ppid == os.getppid()",
              ppid == os.getppid(), detail=f"{ppid} vs {os.getppid()}")
        psinfo = shim_mod._proc_info_ps(pid)
        if psinfo:
            check("proc_info_macos start within 2s of ps lstart",
                  abs(start - psinfo[1]) <= 2.0, detail=f"direct={start} ps={psinfo[1]}")


if __name__ == "__main__":
    test_shell_snapshot_wrapper_is_not_a_runtime()
    test_session_json_read_retries_once_on_mid_rewrite()
    test_identity_never_derived_from_cwd_when_unregistered()
    test_codex_under_claude_parent_is_identified_as_codex()
    test_pure_claude_tree_is_claude()
    test_claude_local_registers_as_claude_kind()
    test_pid_reuse_is_not_anchored_but_50s_lag_is()
    test_unregistered_codex_child_is_unknown_not_parent_claude()
    test_wrapper_argv_mentioning_runtime_does_not_anchor()
    test_reg_json_newest_startedAt_wins_across_dirs()
    test_macos_proc_info_matches_ps_and_is_not_none()
    test_no_runtime_in_tree_returns_none()
    test_self_identity_unknown_when_no_runtime()
    test_self_identity_ignores_mcp_msgers_from_env()
    test_webchat_cwd_is_not_a_routable_persona_rev3()
    test_webchat_pattern_requires_full_shape()
    test_non_webchat_cwd_still_uses_agents_rule()
    test_webchat_scan_emits_webchat_row_from_session_py()
    test_webchat_scan_refuses_uid_mismatch()
    test_webchat_scan_refuses_malformed_spec()
    test_webchat_scan_refuses_short_session_id()
    test_webchat_scan_pid_listing_failure_returns_no_rows_quietly()
    test_session_py_outside_webchat_install_is_not_webchat()
    test_find_runtime_pid_recognises_webchat()
    test_find_runtime_pid_claude_child_of_session_py_is_webchat_not_claude()
    test_find_runtime_pid_tool_subshell_under_session_py_is_also_webchat()
    test_find_runtime_pid_claude_without_webchat_still_claude()
    test_from_name_identical_for_model_and_session_py_paths()
    test_live_sessions_hides_claude_child_of_webchat_rev3()
    test_whoami_caller_matches_self_identity_when_no_pid()
    test_whoami_pid_does_not_change_caller_fields()
    test_whoami_queried_unknown_pid_reports_unknown()
    test_whoami_pid_non_integer_fails_loud()
    print(f"\n{len(PASS)} PASS, {len(FAIL)} FAIL")
    sys.exit(0 if not FAIL else 1)
