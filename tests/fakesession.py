#!/usr/bin/env python3
"""fakesession — a stand-in Claude messaging socket for deterministic testing.

Binds a UDS at the given path, registers a fake session record so the shim's
live-session scan resolves this persona, then waits for exactly one incoming
native message payload, prints it, and exits 0. If a peer sends nothing within
--timeout, exits 3 (not received).

Usage: fakesession.py --persona probe --socket /tmp/x.sock
             --registry /tmp/reg --timeout 30 [--from-name X]
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import time
import uuid
import pathlib
import uuid as _uuid


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--persona", required=True)
    ap.add_argument("--socket", required=True)
    ap.add_argument("--registry", required=True, help="dir the shim scans for sessions")
    ap.add_argument("--timeout", type=float, default=30)
    args = ap.parse_args()

    reg = pathlib.Path(os.path.expanduser(args.registry))
    reg.mkdir(parents=True, exist_ok=True)
    sockpath = os.path.expanduser(args.socket)
    # clear any stale socket
    try:
        os.unlink(sockpath)
    except FileNotFoundError:
        pass
    pid = os.getpid()

    rec = {"pid": pid, "sessionId": "fake-" + uuid.uuid4().hex,
           "cwd": f"/fake/agents/{args.persona}", "startedAt": int(time.time() * 1000),
           "procStart": "", "version": "fake", "peerProtocol": 1,
           "peerFeatures": [], "kind": "interactive", "entrypoint": "fakesession",
           "pidDomain": "host", "messagingSocketPath": sockpath,
           "name": args.persona + "-fake", "nameSince": 0, "nameSource": "custom",
           "status": "idle", "runtime": "fake"}
    (reg / f"{pid}.json").write_text(json.dumps(rec))

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(sockpath)
    os.chmod(sockpath, 0o600)
    srv.listen(4)
    srv.settimeout(args.timeout)
    print(f"fakesession ready persona={args.persona} sock={sockpath} "
          f"timeout={args.timeout}", flush=True)
    try:
        conn, _ = srv.accept()
    except socket.timeout:
        print("NOT_RECEIVED", flush=True)
        return 3
    except OSError:
        print("LISTEN_CLOSED", flush=True)
        return 3
    with conn:
        conn.settimeout(args.timeout)
        buf = b""
        try:
            while b"\n" not in buf:
                ch = conn.recv(65536)
                if not ch:
                    break
                buf += ch
        except socket.timeout:
            pass
    raw = buf.split(b"\n", 1)[0] if buf else b""
    if not raw:
        print("EMPTY", flush=True)
        return 3
    try:
        msg = json.loads(raw)
        content = msg.get("message", {}).get("content", "")
        print("RECEIVED " + content, flush=True)
        return 0
    except ValueError:
        print("BAD_JSON " + raw.decode("utf-8", "replace")[:300], flush=True)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
