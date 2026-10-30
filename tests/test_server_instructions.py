#!/usr/bin/env python3
"""The shim announces itself: MCP `initialize` carries server `instructions`.

Drives the real `shim.py mcp` subprocess over stdin (the transport a client uses), so the
assertion is on the wire reply, not on a constant. Checks the four facts an agent needs:
relay_send with persona / persona@host, relay_who first, replies route back, and that the
built-in SendMessage is not the channel.

Run:  python3 tests/test_server_instructions.py   (PASS/FAIL lines, exit 0/1)
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
PASS, FAIL = [], []


def check(label, cond, detail=""):
    (PASS if cond else FAIL).append(label)
    print(("PASS " if cond else "FAIL ") + label + (f"  [{detail}]" if (detail and not cond) else ""))


def main() -> int:
    req = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
           "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "example-client"}}}
    proc = subprocess.run([sys.executable, str(REPO / "shim.py"), "mcp"], input=json.dumps(req) + "\n",
                          capture_output=True, text=True, timeout=30)
    check("shim mcp exits 0 on initialize", proc.returncode == 0, proc.stderr[-300:])
    lines = [l for l in proc.stdout.splitlines() if l.strip()]
    check("one reply line", len(lines) == 1, repr(proc.stdout[:200]))
    reply = json.loads(lines[0]) if lines else {}
    result = reply.get("result", {})
    text = result.get("instructions", "")
    check("initialize result has instructions", isinstance(text, str) and len(text) > 0)
    check("instructions short (<= 6 sentences)", text.count(". ") + text.count(": ") <= 8, str(text.count(". ")))
    for needle in ("relay_send", 'to="persona"', 'persona@host', "relay_who", "route back",
                   "SendMessage"):
        check(f"instructions mention {needle}", needle in text)
    check("serverInfo still present", result.get("serverInfo", {}).get("name") == "mcp-msgers")
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", *FAIL, sep="\n  ")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
