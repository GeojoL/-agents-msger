# Changelog

All notable changes to this project are listed here, newest first. Versions below 0.1.9 were
internal development releases and are not described in this file.

## Unreleased

- `SKILL.md`: the agent-facing usage guide, with placeholder addresses instead of sample names.
- Tests: personal-looking sample names, accounts and passwords are replaced with labelled test placeholders (`test_agent_1`, `test-user`, `test-password`).
- README: a Related projects section describing how agents-msger, d-browser, wsearch and verdict connect, and the order to set them up together.
- Licensed under MIT (`LICENSE`). Hub and shim report version 0.1.18, matching this changelog.

## 0.1.18

- Limit handling is restricted to the two limit notices that Claude Code writes. Any other API
  error is treated as an ordinary failure, and messages continue to be delivered.
- Queued messages are sent at the reset time named in the notice, and the sender is told when
  the delivery was watched through to a reply.
- The backoff wake branch was removed. Every limit now names its reset time.

## 0.1.17

- A limit whose reset time is unknown is cleared after a time-to-live.
- Queue entries that belong to a pid that has since been replaced are re-keyed onto the live
  process for the same persona.

## 0.1.16

- Each session reports the model it actually uses, detected from machine facts. Delivered
  messages carry it as the `from-model` attribute.

## 0.1.15

- Limit state is bound to the process id and to time, so a relaunched process is not held by
  a limit recorded for its predecessor.

## 0.1.14

- End-to-end limit queue: messages sent to a rate-limited session are queued at the hub with a
  ticket, flushed in order when the target is no longer limited, and expire after a
  time-to-live. Overflow and expiry send a receipt to the original sender.
- Shim-side auto-wake for a limited session whose process has exited, with a fixed-template
  wake message, a retry interval and a retry cap. Launching is delegated to the configured
  `wake_command`.

## 0.1.13

- Caller identity is anchored on the process id of a registered session, with a start-time
  check that rejects reused pids. Process argument text no longer determines the runtime.
- A process whose executable is a runtime but which is not registered stops inheriting the
  identity of its parent.

## 0.1.12

- Human input from webchat sessions is delivered as plain text without the message envelope.
- The hub accepts the human flag only when the request comes from the registered shim's peer
  address and the session identifier matches a live webchat session.

## 0.1.11

- Delivery-gap fixes: the shim reports the real delivery outcome of each message on `/ack`, the
  hub waits a bounded time for that acknowledgement, in-flight messages expire from the hub
  after a time-to-live, and a host that has stopped heartbeating is not treated as live.

## 0.1.10

- Webchat sessions use a unified session naming scheme based on a short session identifier.
- A webchat ancestor takes precedence when resolving identity.

## 0.1.9

- Webchat sessions are recognised through their launch script and the spec file it reads.
- Added the `whoami` MCP tool.

## Changes made for the public copy (on top of 0.1.18)

- The hub accepts a shared bearer token (`--token-file`). When it is set, every route except
  `/health` requires it. The shim sends the token from the `hub_token_file` key.
- The hub binds `127.0.0.1` by default. A non-loopback bind without a token is refused at
  startup unless `--insecure-no-auth` is given.
- Request bodies over 1 MiB are refused with `413` before they are read.
- Envelope attribute values are escaped, and a closing tag in a message body is neutralised.
- The `from-mode` attribute is configurable through `envelope_from_mode`; the default is
  unchanged.
- Deployment-specific values are configuration keys (`agents_root`, `webchat_dir`,
  `extra_runtimes`, `wake_command`, `hub_url`, `hub_token_file`) instead of built-in constants.
- The installer no longer queries an external inventory tool for the host name.
