#!/usr/bin/env python3
"""Agent home detection and persona resolution from the registered agents root."""
from __future__ import annotations

import json
import os
import pathlib
import sys
import time

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import hub as hub_mod   # noqa: E402
import shim as shim_mod
shim_mod._webchat_anchor = lambda: "example-webchat"  # noqa: E402


PASS, FAIL = [], []


def check(label, cond, detail=""):
    (PASS if cond else FAIL).append(label)
    print(("PASS " if cond else "FAIL ") + label + (f"  [{detail}]" if (detail and not cond) else ""))


def _reset_registry_cache():
    shim_mod._AGENTS_ROOT_CACHE["ts"] = 0.0
    shim_mod._AGENTS_ROOT_CACHE["data"] = None
    shim_mod._AGENTS_ROOT_CACHE["last_good"] = None


def _install_fake_root(root_path: pathlib.Path):
    _reset_registry_cache()
    shim_mod._AGENTS_ROOT_CACHE["ts"] = time.monotonic()
    shim_mod._AGENTS_ROOT_CACHE["data"] = root_path.resolve()
    shim_mod._AGENTS_ROOT_CACHE["last_good"] = root_path.resolve()


def test_is_agent_home_cwd_matches_first_level_under_root():
    _install_fake_root(pathlib.Path("/srv/example/agents"))
    check("cwd == <root>/test_agent_1 is agent_home",
          shim_mod._is_agent_home_cwd("/srv/example/agents/test_agent_1") is True)
    check("cwd == <root>/test_agent_1/work is agent_home",
          shim_mod._is_agent_home_cwd("/srv/example/agents/test_agent_1/work") is True)
    check("cwd == <root> itself is NOT agent_home (no first-level name)",
          shim_mod._is_agent_home_cwd("/srv/example/agents") is False)


def test_is_agent_home_cwd_rejects_everything_outside_root():
    _install_fake_root(pathlib.Path("/srv/example/agents"))
    for cwd in [
        "/srv/example-webchat/users/test_user_1/work",   # webchat
        "/tmp/scratchpad",
        "/srv/example/code/foo",
        "",
        None,
    ]:
        check(f"non-agent cwd rejected: {cwd!r}",
              shim_mod._is_agent_home_cwd(cwd) is False)


def test_persona_from_root_overrides_webchat():
    _install_fake_root(pathlib.Path("/srv/example/agents"))
    check("root-match persona wins",
          shim_mod._persona({"cwd": "/srv/example/agents/test_agent_1", "name": "test_agent_1-01"}) == "test_agent_1")


def test_persona_does_not_fallback_to_webchat_for_webchat_cwd():
    """Persona does not fallback to webchat for webchat cwd."""
    _install_fake_root(pathlib.Path("/nonexistent/agents"))
    cwd = "/srv/example/example-webchat/users/test_user_1/work"
    r = {"cwd": cwd, "name": "whatever"}
    check("webchat cwd does NOT become 'webchat' persona via _persona",
          shim_mod._persona(r) is None)
    check("webchat cwd is recognised by _is_webchat_claude_cwd",
          shim_mod._is_webchat_claude_cwd(cwd) is True)
    check("non-webchat cwd is not recognised by _is_webchat_claude_cwd",
          shim_mod._is_webchat_claude_cwd("/srv/example/agents/test_agent_1") is False)


def test_persona_returns_none_when_registry_unknown():
    """Persona returns none when registry unknown."""
    _reset_registry_cache()
    saved = shim_mod._read_agents_root
    try:
        shim_mod._read_agents_root = lambda: None
        r = {"cwd": "/srv/example/agents/test_agent_1", "name": "test_agent_1-xyz-shim"}
        check("unknown registry → persona None (no name-regex guess)",
              shim_mod._persona(r) is None)
    finally:
        shim_mod._read_agents_root = saved
        _reset_registry_cache()


def test_registry_falls_back_to_last_good(tmp_path=None):
    """Registry falls back to last good."""
    good_root = pathlib.Path("/srv/example/agents").resolve()
    _install_fake_root(good_root)
    # Expire ttl so _agents_root re-fetches; keep last_good intact.
    shim_mod._AGENTS_ROOT_CACHE["ts"] = 0.0
    shim_mod._AGENTS_ROOT_CACHE["data"] = None
    saved = shim_mod._read_agents_root
    try:
        shim_mod._read_agents_root = lambda: None
        got = shim_mod._agents_root()
        check("last-good returned on fresh-read failure", got == good_root,
              detail=repr(got))
    finally:
        shim_mod._read_agents_root = saved
        _reset_registry_cache()


def test_registry_reads_agents_root_from_config(tmp_path):
    """The agents root comes from the `agents_root` key of the shim config."""
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"agents_root": str(tmp_path / "agents")}))
    _reset_registry_cache()
    saved_env = os.environ.get("MCP_MSGERS_CONFIG")
    try:
        os.environ["MCP_MSGERS_CONFIG"] = str(cfg)
        got = shim_mod._agents_root()
        expected = (tmp_path / "agents").resolve()
        check("agents_root in config resolves to that directory",
              got == expected, detail=f"{got!r} != {expected!r}")
    finally:
        if saved_env is None:
            os.environ.pop("MCP_MSGERS_CONFIG", None)
        else:
            os.environ["MCP_MSGERS_CONFIG"] = saved_env
        _reset_registry_cache()


def test_registry_missing_without_agents_root(tmp_path):
    """No `agents_root` key in the config -> the root is unknown and facts say so."""
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"hub_url": "http://hub.example.invalid:8465"}))
    _reset_registry_cache()
    saved_env = os.environ.get("MCP_MSGERS_CONFIG")
    try:
        os.environ["MCP_MSGERS_CONFIG"] = str(cfg)
        got = shim_mod._agents_root()
        check("no agents_root key -> _agents_root() returns None", got is None, detail=repr(got))
        facts = shim_mod.host_facts()
        check("facts.registry_missing when agents_root is absent",
              facts.get("registry_missing") is True, detail=str(facts))
    finally:
        if saved_env is None:
            os.environ.pop("MCP_MSGERS_CONFIG", None)
        else:
            os.environ["MCP_MSGERS_CONFIG"] = saved_env
        _reset_registry_cache()


def test_host_facts_registry_missing_when_root_unknown():
    _reset_registry_cache()
    saved = shim_mod._read_agents_root
    try:
        shim_mod._read_agents_root = lambda: None
        facts = shim_mod.host_facts()
        check("facts.registry_missing is True when the root is unknown",
              facts.get("registry_missing") is True, detail=str(facts))
    finally:
        shim_mod._read_agents_root = saved
        _reset_registry_cache()


def test_host_facts_registry_missing_false_when_registry_present():
    _install_fake_root(pathlib.Path("/srv/example/agents"))
    facts = shim_mod.host_facts()
    check("facts.registry_missing is False when AGENTS root known",
          facts.get("registry_missing") is False, detail=str(facts))


def test_who_includes_agent_home_bool_from_heartbeat():
    hub = hub_mod.Hub(poll_timeout=1)
    hub.heartbeat("test_host_1", [
        {"persona": "test_agent_1", "pid": 100, "socket": "/tmp/k.sock",
         "name": "test_agent_1-01", "session_id": "sid-test_agent_1", "started": time.time(),
         "agent_home": True},
        {"persona": "webchat", "pid": 200, "socket": "/tmp/c.sock",
         "name": "test_user_1-1a2b3c4d", "session_id": "sid-c", "started": time.time(),
         "agent_home": False},
    ], caps=[])
    w = hub.who()["test_host_1"]
    test_agent_1 = next(r for r in w["test_agent_1"] if r["pid"] == 100)
    webchat = next(r for r in w["webchat"] if r["pid"] == 200)
    check("agent_home=true on real agent session",
          test_agent_1.get("agent_home") is True, detail=str(test_agent_1))
    check("agent_home=false on webchat session",
          webchat.get("agent_home") is False, detail=str(webchat))


def test_who_leaves_agent_home_null_for_older_shim():
    hub = hub_mod.Hub(poll_timeout=1)
    hub.heartbeat("test_host_2", [
        {"persona": "test_user_2", "pid": 300, "socket": "/tmp/i.sock",
         "name": "test_user_2-01", "session_id": "sid-i", "started": time.time()},
    ], caps=[])
    row = hub.who()["test_host_2"]["test_user_2"][0]
    check("agent_home=None on older shim (not False)",
          row.get("agent_home") is None, detail=str(row))


if __name__ == "__main__":
    test_is_agent_home_cwd_matches_first_level_under_root()
    test_is_agent_home_cwd_rejects_everything_outside_root()
    test_persona_from_root_overrides_webchat()
    test_persona_does_not_fallback_to_webchat_for_webchat_cwd()
    test_persona_returns_none_when_registry_unknown()
    test_registry_falls_back_to_last_good()
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        test_registry_reads_agents_root_from_config(pathlib.Path(td))
    with tempfile.TemporaryDirectory() as td:
        test_registry_missing_without_agents_root(pathlib.Path(td))
    test_host_facts_registry_missing_when_root_unknown()
    test_host_facts_registry_missing_false_when_registry_present()
    test_who_includes_agent_home_bool_from_heartbeat()
    test_who_leaves_agent_home_null_for_older_shim()
    print(f"\n{len(PASS)} PASS, {len(FAIL)} FAIL")
    sys.exit(0 if not FAIL else 1)
