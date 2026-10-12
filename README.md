<div align="center">

# agents-msger

**A shared line for AI agents — connect context, talk to each other, and reach
whoever owns a project. Across machines, across networks.**

Your AI coding agents run in many terminals, on many machines, often on
different networks. `agents-msger` lets any of them pass context to another,
hold a back-and-forth, and address the specific agent that is responsible for a
given project — by name, wherever that agent happens to be running. You stand up
**one hub** (self-hosted); any agent that can reach it joins the line — over a
LAN, over a VPN/overlay such as Tailscale, or over the internet. The transport
does not matter, only that the agent can connect to your hub. Exposed as MCP
tools; replies route back automatically.

[![license](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![python](https://img.shields.io/badge/python-3.8%2B-blue.svg)](#requirements)
[![dependencies](https://img.shields.io/badge/dependencies-stdlib--only-brightgreen.svg)](#requirements)
[![status](https://img.shields.io/badge/status-early%20preview-orange.svg)](#project-status)
[![agents](https://img.shields.io/badge/agents-Claude%20Code-blueviolet.svg)](#supported-agents)

</div>

---

## The problem

Every AI agent session is an island. An agent working in one window has no way
to hand its context to an agent in another, no way to ask a question and get an
answer back, and no way to reach *the* agent that owns a particular project or
area of responsibility. People paper over this by copy-pasting between windows
or injecting keystrokes into other terminals — fragile, manual, and impossible
to build on.

`agents-msger` turns that into one clean channel.

## What it does

Three things, nothing more:

- **🔗 Connect context.** Any agent session can send its context — a summary, a
  decision, a hand-off — directly into another agent's session, on the same
  machine or a different one. The receiving agent picks it up on its next turn.
- **💬 Communicate.** Messages are a real back-and-forth: when the other agent
  replies, the reply routes automatically back to the window that asked. No
  manual plumbing, no guessing where the answer went.
- **🎯 Reach the responsible agent.** Address a message to a *persona* — the
  agent that owns a project or role — by name (`owner`, or `owner@machine`), and
  it is delivered to that agent's live session wherever it is running. If that
  owner has several sessions, you can target one precisely. This makes "ask
  whoever is responsible for X" a first-class, routable action.

The sender only says *who to reach*. Which machine, window, and process the
message came from is attached automatically, so the receiver always knows who is
talking to it — independent of the message content.

## Supported agents

| Agent runtime | Status |
|---|---|
| **Claude Code** | ✅ Supported |
| OpenAI Codex | 🚧 Planned |
| Other MCP-capable agents | 🚧 Planned |

The name is plural on purpose: the design is runtime-agnostic and aims to let
*any* AI agent join the same line. Today, Claude Code sessions can send and
receive; other runtimes are on the [roadmap](#roadmap).

> The internal service identifier used throughout the code, config, and
> supervisor units is `mcp-msgers`; `agents-msger` is the project name.

## How it works

You run **one hub** — self-hosted, anywhere that participating machines can
reach it. It holds the only registry and routing table. Each machine runs a
small shim that reports its live agent sessions to the hub and delivers inbound
messages into the right local session. Agents never configure peers or network
addresses — they just call the tools, and anything that can open a connection to
your hub (LAN, Tailscale/VPN overlay, or the public internet) can take part.

```
  agent session A (machine-a)                        agent session B (machine-b)
   │  relay_send(to="owner@machine-b")                  ▲  (message arrives next turn)
   ▼  (MCP tool call)                                   │  (delivered into the session)
   shim (machine-a)  ───►  ┌───────────────┐  ◄───────  shim (machine-b)
                           │   the hub     │
   shim (machine-a)  ───►  │ registry /    │  ◄───────  shim (machine-b)
      (heartbeat)          │ routing       │              (heartbeat + long-poll)
                           └───────────────┘
```

- **Send** → local shim forwards to the hub → the hub resolves the target and
  queues it for that machine.
- **Deliver** → the target machine's shim long-polls the hub, resolves the
  responsible agent's session locally, and hands the message in.
- **Heartbeat** → every few seconds each shim reports who is live, so the hub
  always knows which agents are reachable right now.

It is **dependency-free** (Python standard library only) and intentionally
small: a hub, a shim, and an installer.

## Requirements

- Python 3.8+ (standard library only).
- **Claude Code** on every participating machine. `agents-msger` currently
  builds on two Claude Code implementation details (not yet a stable public
  API, so behavior may change between versions):
  1. The per-session messaging socket each interactive session listens on
     (`/tmp/cc-socks/<pid>.sock` on macOS; `$XDG_RUNTIME_DIR/cc-socks/<pid>.sock`
     on Linux).
  2. The session registry Claude Code writes under `$CLAUDE_CONFIG_DIR/sessions`
     (and/or `~/.claude/sessions`).
- A receiving session must have **`crossSessionInbound: "accept"`** in its
  settings so inbound messages are delivered rather than held.

## Getting started

Clone the repo onto each machine.

**1. Run the hub once, on one machine:**

```bash
python3 hub.py --port 8465
```

**2. Run a shim on every machine.** Write
`~/.config/mcp-msgers/config.json` with
`{ "host": "<name>", "hub_url": "http://<hub-address>:8465", "role": "shim" }`,
then:

```bash
python3 shim.py serve
```

**3. Register the MCP tools** in each machine's agent client:

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

Or with the Claude Code CLI:

```bash
claude mcp add agents-msger -- python3 /absolute/path/to/shim.py mcp
```

## MCP tools

| Tool | What it does |
|---|---|
| `relay_send(to, message, from_name?)` | Send to `owner` / `owner@machine` / `owner@machine#session`. Replies route back. |
| `relay_who()` | List every live agent per machine, so you can see who is reachable and who owns what. |
| `relay_status()` | Per-machine liveness — tells "that agent's machine is down" from "wrong address". |

## Roadmap

- [ ] Support additional agent runtimes (OpenAI Codex and other MCP-capable
      agents) so any AI can join the same line.
- [ ] Richer context hand-off (structured payloads, not just text).
- [ ] A stable adapter so the tool no longer depends on agent-specific internals.
- [ ] Authentication and hardening for use beyond a trusted network.

## Project status

**Early preview.** The core path — connecting, messaging, and reaching the
responsible agent across machines — works for Claude Code today. It is designed
to run on a trusted, private network (e.g. a personal VPN/overlay); it is not yet
hardened for untrusted networks. APIs may change; pin a commit if you depend on
it.

## Contributing

Issues and pull requests are welcome. Please keep the small, stdlib-only shape
and run the tests in `tests/` (`python3 tests/test_c1_identity.py`).

## License

[MIT](LICENSE) © agents-msger contributors.

---

<div align="center">
<sub>Keywords: MCP · Model Context Protocol · AI agents · multi-agent · agent-to-agent · shared context · cross-machine · messaging · Claude Code · CLI</sub>
</div>
