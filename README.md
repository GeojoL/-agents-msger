# -agents-msger

agents-msger lets AI coding-agent sessions (for example Claude Code sessions) on different
machines send messages to each other. A session calls an MCP tool, a central **hub**
routes the message to the target machine, and a small **shim** on that machine delivers
it into the target session. Replies are routed back automatically.

The code keeps the `mcp-msgers` identifiers for its package directory, MCP server name and
environment variables. The examples below use them as written.

The project is pre-1.0 and was extracted from a working deployment. Interfaces may change
between releases. Read the [Security model](#security-model) before running it on any
network you do not fully control.

## How it works

```
  session A (machine 1)                                 session B (machine 2)
    relay_send(to="agent-b@machine-2")                          ^  message as a user turn
        |  MCP tools/call                                  |  (Unix socket of the session)
        v                                                  |
  shim.py mcp  --POST /send-->  +-----------------+  <--GET /poll (long poll)--  shim.py serve
  (machine 1)                   |  hub.py         |                              (machine 2)
  shim.py serve --POST /hb--->  |  registry,      |  <--POST /hb (heartbeat)--
  (machine 1)                   |  routing, queue |
                                +-----------------+
```

| File | Role |
|---|---|
| `hub.py` | The hub. One instance for the whole deployment. Keeps the session registry, the routing table and the outbound queues in memory. Standard library only. |
| `shim.py` | One per machine. `shim.py serve` heartbeats the machine's live sessions to the hub, long-polls for messages and delivers them locally. `shim.py mcp` is the stdio MCP server that agent sessions call. Standard library only. |
| `install` | Idempotent per-machine installer. Copies `shim.py` into a per-user directory and writes the config file. |
| `config.example.json` | Example configuration with placeholder values. |
| `tests/` | Standalone check scripts (see [Testing](#testing)). |

Addressing uses a persona (the name a session is known by), optionally a host and a
session handle: `persona`, `persona@host`, or `persona@host#session`. Sender identity is
never taken from the caller. The shim detects it from the process tree and the session
registry.

## Requirements

- Python 3.10 or newer (tested on 3.10, 3.11 and 3.14; the sources are also syntax-checked for 3.9).
  No third-party packages.
- Linux is the primary platform. macOS has a separate process-introspection path that is less
  exercised. Some features (for example webchat detection) are Linux-only; the code marks them.
- Agent sessions whose runtime records each live session in a registry directory (by default
  `~/.claude/sessions`) with a per-session Unix socket. Delivery writes into that socket.
  Whether your runtime exposes one is a property of the runtime, not of this project. Check
  it before relying on delivery.
- Optional: tmux. The shim reports whether a tmux server is running as a host fact.

## Install

1. **Create a shared token** on the hub machine and on every shim machine (one line, mode 600):

   ```sh
   mkdir -p ~/.config/mcp-msgers && umask 077
   python3 -c "import secrets; print(secrets.token_urlsafe(32))" > ~/.config/mcp-msgers/hub-token
   ```

2. **Start the hub** on one machine. Without `--bind` it listens on `127.0.0.1` only. To let
   other machines reach it, bind a private address and require the token:

   ```sh
   python3 hub.py serve --port 8465 --bind 192.0.2.10 --token-file ~/.config/mcp-msgers/hub-token
   ```

   A non-loopback `--bind` without `--token-file` is refused, and so is an empty token file.

3. **Install the shim** on each machine:

   ```sh
   python3 install --hub-url http://hub.example.invalid:8465 --host <this-machine-name>
   ```

   Then edit `~/.config/mcp-msgers/config.json` (see [Configuration](#configuration)) so it
   contains `hub_token_file` and `agents_root`.

4. **Run the shim as a service.** The installer writes no supervisor unit. Start
   `~/.local/libexec/mcp-msgers/shim.py serve` as a user service (systemd `--user` unit or
   launchd agent) that restarts on failure.

5. **Register the MCP server** with your agent client. For a client that reads an
   `mcpServers` JSON block:

   ```json
   {
     "mcpServers": {
       "mcp-msgers": {
         "command": "python3",
         "args": ["/path/to/mcp-msgers/shim.py", "mcp"]
       }
     }
   }
   ```

## Configuration

The shim reads `~/.config/mcp-msgers/config.json`. Set the path with `MCP_MSGERS_CONFIG`.
The log file defaults to a file next to `shim.py`; set `MCP_MSGERS_LOG` to change it.

| Key | Meaning |
|---|---|
| `host` | Name of this machine as the hub sees it. Default: the short host name. |
| `hub_url` | Base URL of the hub. Required. |
| `hub_token_file` | File containing the shared bearer token. Required when the hub uses `--token-file`. |
| `agents_root` | Directory whose first-level subdirectories are agent homes. A session whose working directory is under it takes the subdirectory name as its persona. Without it, personas are not derived from paths. |
| `registry_dirs` | Extra session registry directories to scan. |
| `extra_runtimes` | Additional runtimes to recognise, as `[needle, [exclusions...]]` pairs matched against process command lines. |
| `webchat_dir` | Directory name that marks an optional webchat install (`<webchat_dir>/users/<login>/work`). Unset disables webchat detection. |
| `wake_command` | Launcher run as `<wake_command> launch <persona>` to restart a persona whose session died while rate-limited. Unset disables auto-wake. |
| `envelope_from_mode` | The `from-mode` attribute written on delivered messages. Default `bypass`. Its meaning is defined by the receiving runtime. |

See `config.example.json` for a complete example with placeholder values.

## Usage

### MCP tools

| Tool | Purpose |
|---|---|
| `relay_send(to, message, from_name?)` | Send a message. `to` is `persona`, `persona@host`, or `persona@host#session`. A target that is absent or dead fails with `ok: false`; nothing is queued silently. |
| `relay_who()` | List live sessions per host. Each session appears on its own with its unique name, so a specific one can be addressed with `#`. |
| `relay_status()` | Liveness of each host's shim (age of the last heartbeat). Use it to tell "the other side is down" from "wrong address". |
| `whoami(pid?)` | The caller's identity as detected from the process tree. Passing a pid describes that process but never changes the caller's identity. |

Replies are routed back to the sender's session without any action by the recipient's
caller.

### Commands

```
shim.py serve     # heartbeat, long poll and local delivery (run as a service)
shim.py mcp       # stdio MCP server (started by the agent client)
shim.py --version
hub.py serve [--port N] [--bind ADDR] [--token-file PATH] [--poll-timeout S] [--insecure-no-auth]
```

## Protocol

JSON over HTTP. The hub is the only component that holds routing state.

| Method and path | Purpose |
|---|---|
| `POST /hb` | Shim heartbeat: its host's live sessions, degraded sessions and host facts. |
| `POST /send` | Enqueue a message for the target host. Replies `ok`, `queued`, or fails loudly. |
| `POST /ack` | Shim reports the real delivery outcome of a polled message. |
| `GET /poll?host=H` | Long poll (up to `--poll-timeout` seconds) for the next message for host `H`. |
| `GET /who` | Registry snapshot, per host and per session. |
| `GET /status` | Per-host liveness. |
| `GET /health` | Version and host name. Does not require the token. |

The hub reads request bodies up to 1 MiB; larger requests get `413` before the body is read.
The full wire description is in the module docstring of `hub.py`.

## Security model

**What the hub enforces**

- **Shared bearer token.** With `--token-file`, every route except `/health` requires
  `Authorization: Bearer <token>`. The comparison is constant-time. Without a token, every
  route is open, so run the hub only on loopback in that case.
- **Loopback by default.** The hub binds `127.0.0.1` unless `--bind` is given. A non-loopback
  bind without a token is refused at startup unless `--insecure-no-auth` is passed.
- **Body size cap.** Requests larger than 1 MiB are refused.
- **Envelope escaping.** Attribute values in the delivered envelope are escaped, and a closing
  tag inside a message body is neutralised, so message content cannot open a second envelope
  or inject attributes.
- **Detected identity.** A sender's identity comes from the sending process tree and the
  session registry, not from arguments or environment variables.

**Residual risks you must accept or mitigate**

- **The token is full trust.** Any holder can register sessions for any host, poll any
  host's queue, post acknowledgements for any host, and send messages as any registered
  session. Treat the token like a password for every machine in the deployment, and rotate it
  if a machine is compromised.
- **Plain HTTP.** The token and message bodies travel unencrypted. Run the hub only on a
  network you trust, such as a VPN or an overlay network with its own encryption, or put it
  behind a TLS-terminating proxy.
- **Heartbeat trust.** The hub accepts heartbeats and their session records from any holder
  of the token. It cannot verify that a reported session really exists on that machine.
- **Delivered text is untrusted input.** A delivered message becomes a user turn in the
  recipient session. Its content can contain instructions. The recipient should treat it as
  data, and deployments should not give delivered messages more authority than a typed message.
- **`envelope_from_mode` is a pass-through.** The default `bypass` is what the originating
  deployment used. Set it to whatever your receiving runtime expects. The project does not
  interpret it.
- **Single hub.** The hub is one process with in-memory state. Its outage stops delivery, and
  a restart loses queued and in-flight messages.

Report security issues privately to the maintainers before public disclosure.

## Limitations

- Delivery depends on the target runtime exposing a per-session Unix socket recorded in its
  registry. Runtimes that do not do so cannot receive messages.
- The rate-limit handling reads the limit notices that Claude Code writes into its transcripts.
  The notices are not checked for other runtimes.
- Linux is the primary platform. The macOS process-introspection path is less exercised, and
  webchat detection is Linux-only.
- No TLS, no per-session credentials, no message persistence, no delivery guarantee beyond
  the outcome the shim acknowledges.

## Testing

Each `tests/test_*.py` is a standalone script that exits 0 on success. Run them in an
isolated home directory so they never read a real configuration:

```sh
for t in tests/test_*.py; do
  HOME="$(mktemp -d)" CLAUDE_CONFIG_DIR="$(mktemp -d)" python3 "$t" || echo "FAILED: $t"
done
```

The tests use temporary directories, fake sockets and loopback servers. They do not contact
any real hub or session.

## Related projects

These four projects were built together and can be used together. Each one also runs on its own.

| Project | What it does | How it connects |
|---|---|---|
| -agents-msger | Messages between agent sessions on different machines. Sender identity is detected from the process tree. | Its hub lists which sessions are alive on each host (`/who`, `/status`). |
| -d-browser | A browser pool for agents. One Chromium per caller, verified by the kernel, closed when the caller exits. Provides the `pbrowse` command. | For a call forwarded from another machine, it asks the agents-msger hub whether that caller is still alive and closes its Chromium when it is gone. |
| -wsearch | Search service. The calling model writes the query in several languages; fixed code runs the searches and merges the results. | Its browser features (page rendering, scripts) use d-browser's `pbrowse`. |
| -verdict | Decision recall. Before an agent decides, a hook gives it similar earlier decisions and the general design rules. Recall is done by code (BM25). | No dependency on the others. |

Using them together, in this order:

1. agents-msger: run one hub, and one shim on every machine (see the agents-msger README, Install). Its installer
   writes `~/.config/mcp-msgers/config.json`, which holds the hub address and the token file.
2. d-browser: install it on the machines that run browsers. It reads that same agents-msger config to find the hub;
   nothing else needs to be set. Its `pbrowse` command is installed with it.
3. wsearch: install it, and point `PBROWSE_BIN` at d-browser's `pbrowse` (or put `pbrowse` on `PATH`).
4. verdict: install the hook in Claude Code (see the verdict README, Hook setup). It needs none of the others.

## License

MIT. See `LICENSE`.
