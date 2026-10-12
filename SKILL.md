---
name: agents-msger
description: Message a Claude persona session on another machine. Use when you need to reach an agent on a different host, or to deliver an instruction / receipt / question during cross-machine collaboration. The tools relay_send / relay_who / relay_status are provided by the agents-msger MCP server (internal name mcp-msgers); no shell commands are needed.
---

# agents-msger — cross-machine messaging

To reach another Claude persona on another machine, use the MCP tools. The hub
runs on one designated host; each machine runs a shim that automatically reports
its own live sessions. You only call the tools.

## The three tools

- `relay_send(to, message, from_name?)` — send a message.
  - `to = "alice"` sends to **alice on the local machine**.
  - `to = "alice@host-b"` sends to alice on the machine **host-b** (cross-machine).
  - `to = "alice@host-b#alice-c7"` — when a persona has several live sessions,
    use the `#` suffix to pick one (matches the per-session handle from
    `relay_who`: its `name`, `pid`, or a `sessionId` prefix).
  - The sender **only fills `to` and the body**. Your tmux window, agent name,
    and process pid are attached automatically by the shim; the receiver sees
    which window/process the message came from, independent of its content.
- `relay_who()` — list **all live personas** the hub knows, grouped by host, with
  liveness. Call this first to see which personas are online on the target host
  and which `#` handle to use.
- `relay_status()` — per-host shim liveness (how long since the last heartbeat,
  online or not). **If a send fails or you get no reply, call this first** to
  tell "the other machine is offline" from "the target address is wrong".

## Conventions

1. Before sending cross-machine, call `relay_who()` to see which personas are
   addressable — do not guess names.
2. Address with `persona@host`, using the host's name.
3. No reply / `relay_send` reports the target offline → call `relay_status()`:
   - `alive:false` = the other shim is not running (a service problem).
   - `alive:true` but the target persona is not in `relay_who()` = wrong address
     or that session is not open.
4. Replies route automatically: when the other side replies, the message comes
   back to your window — you do not have to find a route.

## Don't

- Don't inject into another machine's window with `tmux send-keys` or similar.
- Don't put content or identity into `to`; identity is automatic, `to` is only
  "who to send to".
- When unsure of the target host name, look it up with `relay_who()` /
  `relay_status()` — don't hardcode an address.
