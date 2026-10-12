<div align="center">

# agents-msger

**Cross-machine message passing between AI coding-agent sessions.**

A tiny hub + per-machine shim that lets an agent session on one machine send a
message to an agent session on another — exposed as MCP tools, with replies
routed back automatically.

[![license](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![python](https://img.shields.io/badge/python-3.8%2B-blue.svg)](#requirements)
[![dependencies](https://img.shields.io/badge/dependencies-stdlib--only-brightgreen.svg)](#requirements)
[![status](https://img.shields.io/badge/status-early%20preview-orange.svg)](#project-status)
[![agents](https://img.shields.io/badge/agents-Claude%20Code-blueviolet.svg)](#supported-agents)

</div>

---

If you run AI coding agents (such as [Claude Code](https://www.claude.com/product/claude-code))
in more than one terminal — or on more than one machine — they have no built-in
way to talk to each other. `agents-msger` gives them one: a single hub holds the
only routing table, each machine runs a small shim that reports its live agent
sessions and delivers inbound messages, and agents call three MCP tools
(`relay_send` / `relay_who` / `relay_status`). Sender identity (which machine,
which window, which process) is attached automatically; you only say *who to
message*.

It is **dependency-free** (Python standard library only, so it runs on a stock
`python3`) and deliberately small: two scripts plus an installer.

> [!WARNING]
> **`agents-msger` is an early preview and currently supports Claude Code
> sessions only.** It is glue around *internal* Claude Code interfaces (see
> [Requirements](#requirements)) that are **not a stable public API** and may
> change between Claude Code versions. The plural name reflects the roadmap —
> other agent runtimes are planned, not yet supported (see
> [Supported agents](#supported-agents)).

## Table of contents

- [Why](#why) · [Supported agents](#supported-agents) · [Features](#features)
- [Architecture](#architecture) · [Requirements](#requirements)
- [Getting started](#getting-started) · [MCP tools](#mcp-tools)
- [Security](#security) · [Known issues](#known-issues) · [Roadmap](#roadmap)
- [Project status](#project-status) · [Contributing](#contributing) · [License](#license)

## Why

Multi-agent and multi-machine workflows are increasingly common, but each agent
session is an island. Existing hacks (injecting keystrokes into another
terminal, ad-hoc SSH relays) are fragile, need manual steps, and are hard to
discover. `agents-msger` replaces them with one well-defined channel:

- **One source of truth.** Exactly one hub holds the registry and routing;
  machines carry no peer config of their own.
- **Automatic identity.** The sender only supplies the target; the shim attaches
  the sender's agent, machine, window, and pid by walking its own process tree.
- **Fail loud.** A message to an absent or dead target returns an explicit
  error, never a silent "queued".
- **Agent-native.** Agents use MCP tools, not shell commands.

## Supported agents

| Agent runtime | Status |
|---|---|
| **Claude Code** | ✅ Supported |
| OpenAI Codex | 🚧 Planned |
| Other MCP-capable agents | 🚧 Planned |

The name is `agents-msger` (plural) because the design is runtime-agnostic, but
today only Claude Code sessions can send and receive. Support for other runtimes
is on the [roadmap](#roadmap).

> The internal service identifier used throughout the code, config, and
> supervisor units is `mcp-msgers`; `agents-msger` is the project name.

## Features

- 🛰️ **Hub + shim** — one hub, one lightweight worker per machine.
- 🧭 **Automatic sender identity** — agent / machine / tmux window / pid attached
  with zero configuration.
- 🎯 **Precise addressing** — `persona`, `persona@host`, or
  `persona@host#session` to pick one of several same-name sessions.
- 🔁 **Automatic reply routing** — replies come back to the originating window.
- 💓 **Liveness & health** — heartbeats distinguish "the other machine is down"
  from "wrong address", and surface the post-reboot socket-missing window.
- 🐍 **stdlib only** — no third-party dependencies.

## Architecture

```
  agent session A (host-a)                           agent session B (host-b)
   │  relay_send(to="alice@host-b")                     ▲  <cross-session-message …>
   ▼  (MCP tools/call)                                  │  (native UDS: cc-socks/<pid>.sock)
  shim.py mcp (host-a)                              shim.py serve (host-b)
   │  POST /send ──────────►  ┌───────────────┐  ◄────── GET /poll?host=host-b (long-poll)
                              │     hub.py     │
  shim.py serve (host-a) ───► │ registry /    │  ◄────── POST /hb (heartbeat every 8s)
     POST /hb (heartbeat)     │ routing /outbox│
                              └───────────────┘
```

- **Send**: an agent calls `relay_send` → the local shim (`mcp`) `POST /send` to
  the hub → the hub resolves the target and enqueues it for `to_host`.
- **Deliver**: the target machine's shim (`serve`) long-polls `GET /poll`, then
  resolves that session's socket **locally** and writes a
  `<cross-session-message>` envelope into it. The message appears on the target's
  next turn.
- **Heartbeat**: every 8s each shim reports its local live sessions so the hub
  knows who is online.

## Requirements

- Python 3.8+ (standard library only).
- **Claude Code** on every participating machine. `agents-msger` depends on two
  Claude Code implementation details that are **not a stable public API**:
  1. **The messaging socket (`cc-socks`).** Each interactive session listens on a
     Unix-domain socket; the shim delivers by connecting to it.
     - macOS: `/tmp/cc-socks/<pid>.sock`
     - Linux: `$XDG_RUNTIME_DIR/cc-socks/<pid>.sock` (tmpfs, cleared on reboot)
  2. **The session registry.** Claude Code writes `sessions/<pid>.json` under
     `$CLAUDE_CONFIG_DIR/sessions` (and/or `~/.claude/sessions`); the shim reads
     `pid`, `messagingSocketPath`, `cwd`, `name`, `sessionId`, `startedAt`,
     `kind`, and `runtime` from it.
- A receiving session must have **`crossSessionInbound: "accept"`** in its
  settings, or inbound messages are held rather than delivered.

## Getting started

Clone the repo onto each machine. Then:

**1. Run the hub (once, on one machine):**

```bash
python3 hub.py --port 8465
# binds a private overlay-network address (e.g. a Tailscale IP) if available;
# see Security for the bind caveat.
```

**2. Run a shim on every machine** (write `~/.config/mcp-msgers/config.json`
with `{ "host": "<name>", "hub_url": "http://<hub-address>:8465", "role": "shim" }`,
then):

```bash
python3 shim.py serve
```

**3. Register the MCP tools** in each machine's agent client by adding an
`mcpServers` entry that runs the stdio server:

```jsonc
{
  "mcpServers": {
    "agents-msger": {
      "command": "python3",
      "args": ["/absolute/path/to/shim.py", "mcp"]
    }
  }
}
```

Or, with the Claude Code CLI:

```bash
claude mcp add agents-msger -- python3 /absolute/path/to/shim.py mcp
```

> The bundled `install` script can set up the launchd/systemd units and write the
> config, but it does **not** register the MCP server for you — see
> [Known issues](#known-issues).

## MCP tools

| Tool | What it does |
|---|---|
| `relay_send(to, message, from_name?)` | Send to `persona` / `persona@host` / `persona@host#session`. Replies route back. |
| `relay_who()` | List every live session per host (each listed individually) so you can see who is addressable. |
| `relay_status()` | Per-host shim liveness — tells "the other side is down" from "wrong address". |

## Security

> [!CAUTION]
> The hub has **no authentication**. Its only protection is the private network
> it binds to, and the bind can fail *open* to `0.0.0.0`. Combined with
> `crossSessionInbound: "accept"` and a hardcoded `from-mode="bypass"` in the
> delivery envelope, an unauthenticated request can become prompt injection into
> a bypass-mode agent session. **Do not expose the hub on an untrusted network,
> and review the threat model before deploying.** Senders are also trusted
> blindly (spoofable `from_name`/`from_host`), the envelope fields are not
> escaped, and the queues/long-polls are unbounded.

A full threat model and recommended hardening live in the project's security
review; this is a preview, not a hardened service.

## Known issues

1. **`install` does not register the MCP server** despite its wording — add the
   `mcpServers` entry manually (see [Getting started](#getting-started)).
2. **The generated hub supervisor unit runs `hub.py serve`,** but `hub.py` has no
   `serve` subcommand — start the hub with `hub.py --port 8465` and fix the unit.

## Roadmap

- [ ] Authentication (per-host keys) and a fail-closed bind.
- [ ] Escape/structured delivery envelope; drop the hardcoded bypass mode.
- [ ] Bounded queues, connection caps, and `Content-Length` limits.
- [ ] Additional agent runtimes (OpenAI Codex and other MCP-capable agents).
- [ ] Decouple from Claude Code internals behind a stable adapter.

## Project status

**Early preview.** The core cross-machine Claude-to-Claude path works, but the
security items above are unaddressed and the public API may change. Pin a commit
if you depend on it.

## Contributing

Issues and pull requests are welcome. Please keep the two-file, stdlib-only shape
and include/extend the tests in `tests/` (`python3 tests/test_c1_identity.py`).

## License

[MIT](LICENSE) © agents-msger contributors.

---

<div align="center">
<sub>Keywords: MCP · Model Context Protocol · AI agents · multi-agent · cross-machine messaging · Claude Code · agent-to-agent · inter-process · CLI</sub>
</div>
