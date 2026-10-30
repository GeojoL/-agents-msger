#!/usr/bin/env python3
"""mcp-msgers hub — the single source of truth for cross-machine routing.

Runs once, on one machine you choose. Every other machine runs a small shim that (a) heartbeats
its live Claude persona sessions here and (b) long-polls for outbound messages.
The hub holds the only registry and the only routing table; machines carry no
peer config of their own. stdlib only, so it runs on a system python3.

Wire protocol (JSON over HTTP):
  POST /hb    {host, sessions:[...], degraded:[...], facts:{...}, caps:[...]}
               -> registry; sessions are keyed by their UNIQUE pid (a session's
               socket is <runtime socket dir>/<pid>.sock). Several sessions may share a
               persona (soft label) — every pid is tracked, none collapsed.
               degraded = pid-alive-but-socket-missing personas (reboot window),
               surfaced via /who (socket_missing) and /status; facts = bare host
               facts (tmux_active, claude_procs); caps = capability tags the shim
               reports (["ack"] = it acks real delivery outcomes on /ack).
  POST /send  {from_host, from_name, to_host, to_persona, to_match?, message}
               -> resolve the target in the registry and enqueue for to_host.
               to_match is a target's unique handle (name/pid/session_id) to pick
               one of several same-persona sessions; without it the persona's
               newest live session is implied. FAIL-LOUD: if the target cannot be
               resolved to a live session the reply is ok=false (no false
               "queued"). For ack-capable targets the reply also waits (bounded)
               for the shim's /ack and reports the real delivered/error.
  POST /ack   {host, id, ok, error?}  <- a shim reporting an outbox message's
               real delivery outcome (delivered, or no-target / socket-error).
  GET  /poll?host=X        -> long-poll: next message for X, or wait <= --poll-timeout
  GET  /who                -> every live session per host, keyed by persona with
               each session listed individually (unique name + pid), NOT collapsed
  GET  /status             -> per-host liveness (last_seen, alive, persona count)
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import pathlib
import socket
import sys
import threading
import time
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HUB_VERSION = "0.1.18"

# A host is considered alive if it heartbeat within this window.
ALIVE_WINDOW_S = 35
HEARTBEAT_TTL_S = 20
# G3: how long an outbox message may stay with no /ack before the hub gives up
# on it. Longer than ACK_WAIT_S so the sender's /send has already returned a
# clean "unconfirmed" before the hub drops the inflight record.
INFLIGHT_TTL_S = 10.0
# G4: not-alive session rows are kept for this long so operators and /who can
# still see "it was there"; after this they are forgotten so memory does not
# over one day).
DEAD_ROW_TTL_S = 600


def _iso_s(v) -> float | None:
    """ISO 8601 UTC ("...Z", optional fraction) -> epoch seconds; None if absent or unparsable."""
    import datetime as _dt
    if not isinstance(v, str) or not v:
        return None
    try:
        return _dt.datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class Hub:
    def __init__(self, poll_timeout: int):
        self.poll_timeout = poll_timeout
        self.lock = threading.Lock()
        # host -> pid -> session dict {persona, name, socket, started, session_id,
        # last_seen, alive, window, pid, from_pid}. Keyed by UNIQUE pid so that
        # several sessions sharing a persona are each tracked — the old code keyed
        # by persona and silently collapsed same-persona siblings (the C1 bug).
        self.sessions: dict[str, dict[int, dict]] = defaultdict(dict)
        # host -> last heartbeat ts
        self.last_seen: dict[str, float] = {}
        # host -> deque of {id, from_name, to_persona, to_match, message, ts}
        self.outbox: dict[str, deque] = defaultdict(deque)
        # host -> set of capability tags the shim reported (["ack"] = it acks real
        # delivery outcomes on /ack). A hub-only client that never /acks is fine:
        # the send path just doesn't wait for one (stays ok / target_alive).
        self.caps: dict[str, set] = defaultdict(set)
        # host -> persona -> {socket, name, last_seen}: pid alive but messaging
        # socket missing (reboot window). Reported so a broken inbound is a
        # visible alert instead of a quiet 'not alive'.
        self.degraded: dict[str, dict[str, dict]] = defaultdict(dict)
        # host -> [ {pid, persona, name, kind} ]: listed by /who, never routed.
        self.unroutable: dict[str, list[dict]] = {}
        # host -> bare host-level facts reported by the shim (tmux_active,
        # claude_procs). Facts only — the hub is the single place that derives
        # any judgment from them (e.g. sessions_restored in /status).
        self.facts: dict[str, dict] = {}
        # heartbeat. Used by /send to verify a `human: true` claim is
        # coming from from_host's own shim (and not another host on the
        # same network
        # trying to forge). Missing entry (shim hasn't heartbeated yet
        # OR legacy POST handler didn't pass peer_ip) means "no verified
        # IP for this host" and `human` is dropped — fail-closed on the
        # trust claim.
        self.shim_ip: dict[str, str] = {}
        self.conds: dict[str, threading.Condition] = defaultdict(
            lambda: threading.Condition(self.lock))
        # host -> condition a /send wait sleeps on until the target shim /acks.
        # Distinct from self.conds (the poll outbox) so an /ack never wakes a
        # waiting /poll and vice-versa.
        self.ack_conds: dict[str, threading.Condition] = defaultdict(
            lambda: threading.Condition(self.lock))
        # mid -> {ok, error, ts}: the latest recorded /ack, consumed by the
        # matching /send wait. Bounded by ACK_WAIT_S so a slow/absent shim
        # (an old shim that never /acks) never holds /send past that.
        self.ack_results: dict[str, dict] = {}
        self.ACK_WAIT_S = 2.0  # how long /send waits for a shim's /ack
        # message /poll handed out that hasn't been /ack'd yet. ack() removes;
        # _sweep_inflight_locked() drops entries older than INFLIGHT_TTL_S
        # (fail-loud per host via /status.inflight_count / dropped_total).
        self.inflight: dict[str, dict] = {}
        self.inflight_dropped: dict[str, int] = defaultdict(int)
        # host -> target_key -> list[queued_msg]. target_key is (persona, pid)
        # as a tuple-string so one persona's two sessions queue separately. A
        # queued_msg = {ticket, from_name, from_host, to_persona, to_match,
        # message, ts, expires_at, resets_at, limited_reason, extra}.
        # Flushed in order on heartbeat that reports `limited:false` for a
        # rate_limit unknown → 2h; usage_limit unknown → 6h.
        self.limit_queue: dict[str, dict[str, list[dict]]] = defaultdict(
            lambda: defaultdict(list))
        # Cap per target to prevent runaway accumulation during a long outage.
        self.LIMIT_QUEUE_MAX = 100
        # Default TTLs when the shim-reported resets_at is "unknown".
        self.LIMIT_QUEUE_TTL_RATE_S = 2 * 3600
        self.LIMIT_QUEUE_TTL_USAGE_S = 6 * 3600
        # Tracks which pids were limited at the previous heartbeat, so the
        # transition-to-unlimited fires exactly once per flip.
        self.prev_limited: dict[str, set] = defaultdict(set)
        # Tickets sent at the reset instant, waiting to see whether the session took them
        # {"since": send time, "tickets": [...]}. A reply after `since` -> receipt queued_delivered;
        # a new limit sentence after `since` -> back into limit_queue under the new reset.
        self.limit_watch: dict[str, dict[str, dict]] = defaultdict(dict)

    # -- unique-identity resolution -----------------------------------------
    def _matches(self, s: dict, match: str | None) -> bool:
        """Does a session match a `#match` selector (its unique name, pid, or a
        session_id / uuid prefix)? None/empty match means 'any of this persona'."""
        if not match:
            return True
        if s.get("name") == match:
            return True
        if s.get("pid") is not None and str(s.get("pid")) == match:
            return True
        sid = s.get("session_id")
        return bool(sid) and (sid == match or str(sid).startswith(match))

    def _host_heartbeating(self, host: str, now: float | None = None) -> bool:
        """Return True when the host's shim has heartbeated within ALIVE_WINDOW_S.

        A session's alive flag is cleared only when a later heartbeat omits it, so a host that has
        stopped heartbeating would otherwise look alive, and /send would queue messages that are never
        delivered. Resolution and listings therefore also check the hub's own last-seen time."""
        now = now if now is not None else time.time()
        last = self.last_seen.get(host)
        return bool(last and (now - last) < ALIVE_WINDOW_S)

    def _resolve(self, host: str, persona: str,
                 match: str | None) -> tuple[dict | None, list[dict]]:
        """Resolve a target to a concrete session. Returns (winner, candidates).

        - with a `match`: the single live session whose name/pid/session_id
          matches it; (None, candidates) if none match -> fail loud.
        - without: any live session of this persona; if several, the NEWEST is
          the winner. If the persona has no live session at all -> (None, []).
        - If the host itself has stopped heartbeating -> (None, []): the
          per-session alive flag is stale (G4)."""
        if not self._host_heartbeating(host):
            return None, []
        cands = [s for s in self.sessions.get(host, {}).values()
                 if s["alive"] and s.get("persona") == persona]
        if match:
            cands = [s for s in cands if self._matches(s, match)]
        if not cands:
            return None, cands
        winner = max(cands, key=lambda s: (s.get("started") or 0, s["pid"]))
        return winner, cands

    def _registered_from(self, from_host: str, from_name: str, from_pid) -> str:
        """The sender header is the REGISTERED identity of the sending session
        (persona@host#name from this hub's own registry, keyed by the sender's
        pid). A from_name the caller supplied is used only when the sender is not
        registered here; a disagreeing label is ignored and logged: identity is
        detected, never self-declared."""
        try:
            pid = int(from_pid) if from_pid is not None else None
        except (TypeError, ValueError):
            pid = None
        rec = self.sessions.get(from_host, {}).get(pid) if pid is not None else None
        if rec is None:
            return from_name
        canonical = f"{rec.get('persona')}@{from_host}" + (f"#{rec['name']}" if rec.get("name") else "")
        if from_name and from_name != canonical:
            print(json.dumps({"event": "from_name_ignored", "given": from_name,
                              "used": canonical, "host": from_host, "pid": pid}),
                  file=sys.stderr, flush=True)
        return canonical

    def _registered_model(self, from_host: str, from_pid) -> str:
        """The sender's model label from this hub's registry (keyed by pid, like the sender
        header); "unknown" when the sender is not registered or its shim did not report one."""
        try:
            pid = int(from_pid) if from_pid is not None else None
        except (TypeError, ValueError):
            pid = None
        rec = self.sessions.get(from_host, {}).get(pid) if pid is not None else None
        return ((rec or {}).get("model") or {}).get("label") or "unknown"

    def heartbeat(self, host: str, sessions: list[dict],
                  degraded: list[dict] | None = None,
                  facts: dict | None = None,
                  caps: list | None = None,
                  unroutable: list[dict] | None = None,
                  peer_ip: str | None = None,
                  unresponsive_pids: list[int] | None = None) -> dict:
        degraded = degraded or []
        # Non-interactive sessions (background jobs): shown by /who with
        # addressable=False, never used for routing. Older shims omit it.
        self.unroutable[host] = [u for u in (unroutable or []) if u.get("pid")]
        now = time.time()
        with self.lock:
            self.last_seen[host] = now
            if peer_ip:
                # Record the shim's peer IP so /send can verify that a `human: true` claim came
                # from that host's registered shim (not an impersonator
                # on the same network). A quiet drop (not refuse) when
                # a stranger claims human — the message still routes,
                # just without the content-stripping flag.
                self.shim_ip[host] = peer_ip
            # store bare host facts (no judgment here — just what the shim saw)
            self.facts[host] = facts or {}
            self.caps[host] = set(caps or [])
            reg = self.sessions[host]
            seen = set()
            for s in sessions:
                pid = s.get("pid")
                p = s.get("persona")
                if not (pid and p):
                    continue  # pid+persona are the identity; a session with
                seen.add(pid)                       # neither is unaddressable
                reg[pid] = {"pid": pid, "persona": p, "socket": s.get("socket"),
                            "name": s.get("name"), "session_id": s.get("session_id"),
                            "started": s.get("started") or 0, "last_seen": now,
                            "alive": True, "window": s.get("window"),
                            "from_pid": s.get("from_pid"),
                            # Carry the rate-limited state so
                            # the hub can refuse `send` without touching the shim.
                            # resets_at carries the reset instant (ISO 8601 UTC) when
                            # the shim parsed it from the usage-limit notice, or
                            # "unknown" when the notice has no reset hint, or None
                            # for the api_error_rate_limit branch (short backoff
                            # has no absolute reset; the shim's auto-wake schedule
                            # handles it). Older shims omit the field → None.
                            # machine facts ({provider,id,label}); older shims omit it.
                            "model": s.get("model"),
                            "limited": bool(s.get("limited")),
                            "limited_since": s.get("limited_since"),
                            "limited_reason": s.get("limited_reason"),
                            "resets_at": s.get("resets_at"),
                            "last_reply_at": s.get("last_reply_at"),
                            # The shim reports whether the cwd is an agent home; the hub
                            # relays the boolean through /who so a dashboard can
                            # filter real agents from scratch/tmp/webchat rows.
                            # Older shims omit the field — stored as None → /who
                            # returns `agent_home: null` so UI can treat it as
                            # unknown and keep showing the row.
                            "agent_home": s.get("agent_home")}
            # mark sessions not in this heartbeat as not-alive (session ended)
            for pid in list(reg):
                if pid not in seen:
                    reg[pid]["alive"] = False
            # transition and flush that pid's queue. Collected now, flushed
            # after the lock is released (flush path re-acquires the lock on
            # each inner /send call, which does its own routing).
            now_limited: set = set()
            flush_targets: list[tuple[str, str]] = []
            for pid in seen:
                r = reg.get(pid, {})
                if r.get("limited"):
                    now_limited.add(pid)
                elif pid in self.prev_limited.get(host, set()):
                    persona = r.get("persona")
                    if persona:
                        flush_targets.append((persona, str(pid)))
            self.prev_limited[host] = now_limited
            # `persona:<old_pid>` whose <old_pid> is no longer in this
            # heartbeat's `seen` is orphaned by relaunch — the current
            # transition drain never touches it, so tickets outlive the
            # original pid and the sender keeps "queued:N" against a pid
            # queued:4). When the persona has a live pid now, rekey each
            # stranded ticket onto `persona:<live_pid>`: if the live pid
            # is unlimited, the ticket joins flush_targets and drains
            # immediately; if still limited, the ticket queues under the
            # live pid and expires via the normal TTL path (not stuck).
            live_by_persona: dict[str, str] = {}
            for pid in seen:
                r = reg.get(pid, {})
                p = r.get("persona")
                if p:
                    # Prefer the earliest live pid for this persona (same
                    # tie-break the shim uses when there are two): deterministic.
                    live_by_persona.setdefault(p, str(pid))
            if host in self.limit_queue:
                for target_key in list(self.limit_queue[host].keys()):
                    try:
                        persona_part, _, pid_part = target_key.rpartition(":")
                    except ValueError:
                        continue
                    if not persona_part or not pid_part.isdigit():
                        continue
                    if int(pid_part) in seen:
                        continue  # the queue's pid is still live — leave it
                    live_pid = live_by_persona.get(persona_part)
                    if live_pid is None:
                        continue  # no live replacement — ordinary TTL / unresponsive path handles it
                    new_key = f"{persona_part}:{live_pid}"
                    stranded = self.limit_queue[host].pop(target_key, [])
                    if not stranded:
                        continue
                    self.limit_queue[host].setdefault(new_key, []).extend(stranded)
                    # If the live pid is not currently limited, synthesize a
                    # transition so the drain path below delivers the tickets.
                    live_pid_int = int(live_pid)
                    live_r = reg.get(live_pid_int, {})
                    if not live_r.get("limited"):
                        flush_targets.append((persona_part, live_pid))
            # Phase 3b: unresponsive-pid handling. The shim marks any pid
            # whose auto-wake retries exhausted. For each such (host, pid),
            # mint queued_failed_unresponsive receipts to the pending
            # the sender must know the target is unresponsive — only then
            # can they decide to escalate (page a human, give up, etc.).
            if unresponsive_pids:
                for pid in unresponsive_pids:
                    for target_key in list(self.limit_queue.get(host, {}).keys()):
                        if not target_key.endswith(f":{pid}"):
                            continue
                        for q in self.limit_queue[host][target_key]:
                            self._enqueue_system_receipt_locked(
                                q, "queued_failed_unresponsive",
                                detail=f"wake retries exhausted; pid={pid} still limited")
                        del self.limit_queue[host][target_key]
            # Also sweep expired queued tickets (TTL fired without a clear).
            # Each expired ticket mints a system receipt back to its original
            expired_notices: list[tuple[str, dict]] = []
            now_s = time.time()
            if host in self.limit_queue:
                for target_key in list(self.limit_queue[host].keys()):
                    qlist = self.limit_queue[host][target_key]
                    survivors = []
                    for q in qlist:
                        if q.get("expires_at", 0) <= now_s:
                            expired_notices.append((host, q))
                            self._enqueue_system_receipt_locked(
                                q, "limit_queue_expired",
                                detail=f"TTL hit without a clear; {round(now_s - q.get('ts', 0))}s after queue")
                        else:
                            survivors.append(q)
                    if survivors:
                        self.limit_queue[host][target_key] = survivors
                    else:
                        del self.limit_queue[host][target_key]
            # G4: drop rows that have been not-alive for longer than DEAD_ROW_TTL_S.
            # Deletion is bounded by a fixed constant, no judgement; the live row
            # for the same persona (if any) stays.
            for pid in list(reg):
                r = reg[pid]
                if not r["alive"] and (now - r.get("last_seen", 0)) > DEAD_ROW_TTL_S:
                    del reg[pid]
            # replace this host's degraded set (pid alive but socket missing)
            deg = self.degraded[host]
            newp = set()
            for d in degraded:
                dp = d.get("persona")
                if not dp:
                    continue
                newp.add(dp)
                deg[dp] = {"socket": d.get("socket"),
                           "name": d.get("name"), "pid": d.get("pid"),
                           "last_seen": now}
            for dp in list(deg):
                if dp not in newp:
                    del deg[dp]
            snapshot = self._snapshot_locked(host)
            # Drain queued messages for this host whose target just un-limited.
            # Collected here (inside the lock) and flushed below (after release)
            # so each flush can call self.send() which takes the lock itself.
            drain: list[dict] = []
            for persona, pid_s in flush_targets:
                target_key = f"{persona}:{pid_s}"
                qlist = self.limit_queue.get(host, {}).pop(target_key, [])
                for q in qlist:
                    q["_key"] = target_key
                drain.extend(qlist)
            # Tickets sent at an earlier reset: settle them on this heartbeat's facts.
            for target_key in list(self.limit_watch.get(host, {})):
                w = self.limit_watch[host][target_key]
                pid_part = target_key.rpartition(":")[2]
                r = reg.get(int(pid_part)) if pid_part.isdigit() else None
                if r is None or not r.get("alive"):
                    continue
                again = _iso_s(r.get("limited_since"))
                reply = _iso_s(r.get("last_reply_at"))
                if r.get("limited") and again is not None and again >= w["since"]:
                    resets_ts = _iso_s(r.get("resets_at"))
                    for q in w["tickets"]:
                        q["resets_at"] = r.get("resets_at")
                        if resets_ts is not None:
                            q["expires_at"] = resets_ts + 900
                    self.limit_queue[host][target_key][:0] = w["tickets"]
                    del self.limit_watch[host][target_key]
                elif reply is not None and reply >= w["since"]:
                    for q in w["tickets"]:
                        self._enqueue_system_receipt_locked(q, "queued_delivered",
                                                            detail="sent at reset; the session replied")
                    del self.limit_watch[host][target_key]
            for persona, pid_s in flush_targets:
                sent = [q for q in drain if q.get("to_persona") == persona and q.get("_key") == f"{persona}:{pid_s}"]
                if sent:
                    self.limit_watch[host][f"{persona}:{pid_s}"] = {"since": time.time(), "tickets": sent}
            # Keep expired_notices available after lock release for logging.
            expired_for_log = list(expired_notices)
        # Lock released. Flush drained queue in FIFO order.
        for q in drain:
            try:
                self.send(q["from_host"], q["from_name"], host,
                           q["to_persona"], q["message"],
                           extra={**q.get("extra", {}),
                                   "to_match": q.get("to_match")})
            except Exception as _exc:  # noqa: BLE001
                # Flush failures are logged but not surfaced synchronously —
                # the sender already got the queued:true receipt. A persistent
                # flush failure will show as a dropped message in /status.
                print(json.dumps({"event": "limit_queue_flush_failed",
                                   "ticket": q.get("ticket"),
                                   "host": host,
                                   "error": str(_exc)}),
                      file=sys.stderr, flush=True)
        for host_name, q in expired_for_log:
            print(json.dumps({"event": "limit_queue_expired",
                               "ticket": q.get("ticket"),
                               "host": host_name,
                               "to_persona": q.get("to_persona"),
                               "age_s": time.time() - q.get("ts", 0)}),
                  file=sys.stderr, flush=True)
        return snapshot

    def ack(self, host: str, mid: str, ok: bool, error: str = "") -> bool:
        """A shim reports an outbox message's real delivery outcome. Records it,
        clears the inflight entry (G3), and wakes the matching /send wait if any.
        A late/unknown mid is harmless — pop() with default."""
        with self.ack_conds[host]:
            self.ack_results[mid] = {"ok": ok, "error": error, "ts": time.time()}
            self.inflight.pop(mid, None)
            self.ack_conds[host].notify_all()

    def _enqueue_system_receipt_locked(self, original_msg: dict, event: str,
                                        detail: str = "") -> None:
        """Queue a system receipt for the original sender of a dropped or expired message.

        Used for queue-overflow drops and TTL expiry, so the sender learns the outcome rather than only
        seeing a log line. The receipt body is a fixed JSON template with no generated text: event,
        ticket, target persona and host, resets_at, age and detail. It is placed directly in the
        sender's outbox, so the sender's shim picks it up on its next poll.

        The caller must hold self.lock. The receipt goes through the outbox rather than a recursive
        self.send() call, which avoids a deadlock and a routing round trip for a notice-only message."""
        from_host = original_msg.get("from_host")
        from_name = original_msg.get("from_name") or ""
        if not from_host:
            return
        # Parse sender persona from the canonical from_name:
        # "persona@host#name" -> persona.
        sender_persona = from_name.split("@", 1)[0] if "@" in from_name else from_name
        if not sender_persona:
            return
        body = json.dumps({
            "event": event,
            "ticket": original_msg.get("ticket"),
            "to_persona": original_msg.get("to_persona"),
            "to_host": original_msg.get("to_match"),
            "resets_at": original_msg.get("resets_at"),
            "age_s": round(time.time() - original_msg.get("ts", 0), 1),
            "detail": detail,
        }, sort_keys=True)
        mid = "r-%d" % int(time.time() * 1e6)
        self.outbox[from_host].append({
            "id": mid, "from_name": "limit-queue@hub",
            "to_persona": sender_persona, "to_match": None,
            "message": body, "ts": time.time(),
            "from_window": None, "from_pid": None,
            "system_receipt": True,   # tag so a shim can route to a system handler
        })
        self.conds[from_host].notify_all()

    def _sweep_inflight_locked(self, now: float | None = None):
        """Drop in-flight entries older than INFLIGHT_TTL_S. The caller must hold self.lock.

        Dropped messages are counted per host and reported in /status, so a sender can tell "delivered
        but unconfirmed" from "the hub no longer knows". They are not requeued, because a requeue after
        a lost acknowledgement risks duplicate delivery."""
        now = now if now is not None else time.time()
        for mid in list(self.inflight):
            rec = self.inflight[mid]
            if now - rec.get("sent_at", 0) > INFLIGHT_TTL_S:
                self.inflight_dropped[rec["host"]] += 1
                del self.inflight[mid]
                print(json.dumps({"event": "inflight_dropped",
                                  "host": rec["host"], "mid": mid,
                                  "age_s": round(now - rec["sent_at"], 1)}),
                      file=sys.stderr, flush=True)

    def wait_for_ack(self, host: str, mid: str) -> dict | None:
        """Bounded wait (lock released) for the target shim to /ack `mid`. Returns
        the recorded outcome, or None on timeout / no-ack (an old shim)."""
        c = self.ack_conds[host]
        with c:
            t0 = time.time()
            while mid not in self.ack_results:
                remaining = self.ACK_WAIT_S - (time.time() - t0)
                if remaining <= 0:
                    return None
                c.wait(remaining)
            res = self.ack_results.get(mid)
            self.ack_results.pop(mid, None)
            return res

    def send(self, from_host: str, from_name: str, to_host: str,
             to_persona: str, message: str, extra: dict | None = None) -> dict:
        extra = extra or {}
        match = extra.get("to_match") or None
        with self.lock:
            from_name = self._registered_from(from_host, from_name, extra.get("from_pid"))
            winner, cands = self._resolve(to_host, to_persona, match)
            if winner is None:
                # fail loud: no live session to deliver to (persona absent, or a
                # #match that hit nothing). Never a silent ok:true/queued.
                return {"ok": False,
                        "error": (f"no live session matching {match}"
                                  if match else
                                  f"no live session for persona '{to_persona}' "
                                  f"on {to_host}")}
            if winner.get("limited"):
                # Instead of a hard refuse ("ok:false"), enqueue the message
                # and tell the sender when to expect delivery. On the next
                # heartbeat that reports limited:false for this pid, the hub
                # flushes the queue in order via the normal /send path. The
                # sender's receipt is `queued:true, ticket:<id>, resets_at:...`.
                # pid); drop oldest when LIMIT_QUEUE_MAX reached; per-ticket
                # TTL from resets_at + grace, or defaults (rate 2h / usage 6h)
                # when resets_at is 'unknown'.
                target_key = f"{to_persona}:{winner['pid']}"
                qlist = self.limit_queue[to_host][target_key]
                dropped_for_notice: list[dict] = []
                while len(qlist) >= self.LIMIT_QUEUE_MAX:
                    dropped = qlist.pop(0)
                    self.inflight_dropped[to_host] = self.inflight_dropped.get(to_host, 0) + 1
                    dropped_for_notice.append(dropped)
                reason = winner.get("limited_reason") or ""
                resets_at = winner.get("resets_at")
                now_s = time.time()
                if resets_at and resets_at != "unknown":
                    # Parse ISO 8601 UTC; grace = 15 min after reset.
                    try:
                        import datetime as _dt
                        exp_dt = _dt.datetime.strptime(resets_at, "%Y-%m-%dT%H:%M:%SZ")
                        expires_at = exp_dt.replace(tzinfo=_dt.timezone.utc).timestamp() + 900
                    except Exception:  # noqa: BLE001
                        expires_at = now_s + self.LIMIT_QUEUE_TTL_USAGE_S
                else:
                    ttl = (self.LIMIT_QUEUE_TTL_RATE_S if reason == "api_error_rate_limit"
                           else self.LIMIT_QUEUE_TTL_USAGE_S)
                    expires_at = now_s + ttl
                ticket = "q-%d" % int(now_s * 1e6)
                qlist.append({
                    "ticket": ticket, "from_name": from_name,
                    "from_host": from_host, "to_persona": to_persona,
                    "to_match": match, "message": message,
                    "ts": now_s, "expires_at": expires_at,
                    "resets_at": resets_at, "limited_reason": reason,
                    "extra": dict(extra),
                })
                # Overflow notice: each dropped ticket enqueues a system
                # receipt into its original sender's outbox. The sender's
                # shim /poll will deliver it like any other message. This
                # requires only self.outbox access, which is already under
                for d in dropped_for_notice:
                    self._enqueue_system_receipt_locked(d, "queue_overflow_dropped",
                                                        detail="LIMIT_QUEUE_MAX reached, oldest dropped to make room")
                return {"ok": False, "delivered": False,
                        "queued": True, "ticket": ticket,
                        "error": "usage_limit_reached",
                        "to": {"persona": to_persona, "pid": winner["pid"],
                               "model": (winner.get("model") or {}).get("label") or "unknown",
                               "name": winner.get("name")},
                        "limited_since": winner.get("limited_since"),
                        "limited_reason": reason,
                        "resets_at": resets_at}
            q = self.outbox[to_host]
            mid = "%d" % int(time.time() * 1e6)
            outbox_msg = {"id": mid, "from_name": from_name,
                          "to_persona": to_persona, "to_match": match,
                          "message": message, "ts": time.time(),
                          "from_window": extra.get("from_window"),
                          "from_pid": extra.get("from_pid"),
                          "from_model": self._registered_model(from_host, extra.get("from_pid"))}
            # 1. machine level — peer IP must match from_host's shim's
            #    most recent heartbeat IP. Blocks another host on the same
            #    network forging from_host's identity.
            # 2. session level — from_session_id must match an actual
            #    live webchat session (persona=='webchat' row) that
            #    from_host's shim reported in its heartbeat. Blocks a
            #    same-machine process from forging a webchat session id
            #    that was never registered by the shim's _webchat_scan.
            # process running as the SAME user on from_host that targets
            # an ALREADY-REGISTERED webchat session can still forge.
            # Session-level trust is used here, without a per-session token.
            peer_ip = extra.get("peer_ip")
            registered_ip = self.shim_ip.get(from_host)
            session_claimed = extra.get("from_session_id")
            registered_webchat_sids = {
                s.get("session_id")
                for s in self.sessions.get(from_host, {}).values()
                if s.get("persona") == "webchat" and s.get("session_id")
            }
            human_requested = bool(extra.get("human"))
            human_verified = (human_requested
                              and peer_ip is not None
                              and registered_ip is not None
                              and peer_ip == registered_ip
                              and session_claimed is not None
                              and session_claimed in registered_webchat_sids)
            if human_requested and not human_verified:
                # fail-closed on the trust claim, fail-open on routing
                log_fn = globals().get("_log_hub_untrusted_human")
                if callable(log_fn):
                    log_fn(from_host, peer_ip, registered_ip)
            if human_verified:
                outbox_msg["human"] = True
                if extra.get("from_session_id"):
                    outbox_msg["from_session_id"] = extra["from_session_id"]
            q.append(outbox_msg)
            target = {"persona": to_persona, "pid": winner["pid"],
                      "name": winner["name"],
                      "model": (winner.get("model") or {}).get("label") or "unknown"}
            if match:
                target["match"] = match
            queued = len(q)
            can_ack = "ack" in self.caps.get(to_host, set())
            self.conds[to_host].notify_all()
        # wait OUTSIDE the registry lock for the shim's real delivery outcome
        result = None
        if can_ack:
            result = self.wait_for_ack(to_host, mid)
        if result is not None:
            if result.get("ok"):
                return {"ok": True, "delivered": True, "queued": queued,
                        "to": target}
            return {"ok": False, "delivered": False, "queued": queued, "to": target,
                    "error": result.get("error") or "delivery failed"}
        # does not advertise the ack capability. Previously returned ok:true
        # with target_alive, which the sender read as success. The message
        # was already out of the outbox and nothing confirmed delivery.
        # Return ok:false, delivered:false with the cause explicit — the
        # inflight record will time out and the hub drops it (fail-loud).
        reason = ("no delivery ack within %gs" % self.ACK_WAIT_S if can_ack
                  else "target host does not report delivery acks")
        return {"ok": False, "delivered": False, "status": "unconfirmed",
                "queued": queued, "to": target, "error": reason}

    def poll(self, host: str) -> dict | None:
        with self.lock:
            self._sweep_inflight_locked()
            q = self.outbox[host]
            c = self.conds[host]
            t0 = time.time()
            while not q:
                remaining = self.poll_timeout - (time.time() - t0)
                if remaining <= 0:
                    return None
                c.wait(remaining)
            msg = q.popleft()
            # G3: record what we handed out so an un-ack'd message is visible
            # in /status and sweepable, instead of vanishing from the hub.
            self.inflight[msg["id"]] = {"host": host, "msg": msg,
                                         "sent_at": time.time()}
            return msg

    def who(self) -> dict:
        """List every live session, NOT collapsed by persona.

        Shape: host -> persona -> [ {pid, name, alive} ... ]. A persona with
        several live sessions yields several entries, each with its unique pid +
        name so a caller can address one precisely via relay_send's '#' handle.
        Degraded (pid-alive-but-socket-missing) personas that have no live
        session are surfaced too, flagged socket_missing."""
        with self.lock:
            now = time.time()
            out = {}
            for h, sess in self.sessions.items():
                host: dict[str, list] = {}
                host_up = self._host_heartbeating(h, now)
                for s in sess.values():
                    p = s.get("persona")
                    if not p:
                        continue
                    # G4: a session cannot be more alive than its host. If the
                    # host stopped heartbeating, every session flips not-alive
                    # for display + routing, matching _resolve's rule.
                    live = bool(s["alive"] and host_up)
                    host.setdefault(p, []).append(
                        {"pid": s.get("pid"), "name": s.get("name"),
                         "alive": live,
                         # so a caller matching a specific webchat conversation
                         # can address it precisely without a round-trip.
                         "session_id": s.get("session_id"),
                         "agent_home": s.get("agent_home"),
                         "model": (s.get("model") or {}).get("label") or "unknown",
                         # /who now exposes limit state so senders can see a
                         # target's recovery window BEFORE calling relay_send.
                         # Reads: limited (bool), limited_reason, resets_at
                         # (ISO 8601 UTC | "unknown" | None).
                         "limited": bool(s.get("limited")),
                         "limited_reason": s.get("limited_reason"),
                         "resets_at": s.get("resets_at")})
                for plist in host.values():
                    # stable order: newest first so the 'bare alias -> newest'
                    # winner is always the first listed — matches resolution.
                    plist.sort(key=lambda x: (x.get("pid") or 0), reverse=True)
                # surface pid-alive-but-socket-missing personas with no live sess
                for dp, dv in self.degraded.get(h, {}).items():
                    if dp not in host:
                        host[dp] = [{"pid": dv.get("pid"),
                                     "name": dv.get("name"), "alive": False,
                                     "socket_missing": True}]
                for u in self.unroutable.get(h, []):
                    host.setdefault(u.get("persona") or f"pid-{u['pid']}", []).append(
                        {"pid": u["pid"], "name": u.get("name"), "alive": True,
                         "kind": u.get("kind"), "addressable": False,
                         "note": "not addressable: a non-interactive session (e.g. a background job); messages are never delivered here"})
                out[h] = host
            return out

    def status(self) -> dict:
        with self.lock:
            now = time.time()
            out = {}
            for h, seen in self.last_seen.items():
                alive = (now - seen) < ALIVE_WINDOW_S
                sess = self.sessions.get(h, {})
                # a persona is only 'degraded' if it's pid-alive-but-socket-
                # missing AND was not also reported live this heartbeat
                livep = {s.get("persona") for s in sess.values()
                         if s["alive"] and s.get("persona")}
                # The shim reports only bare facts; the hub is the single place
                # that turns them into a judgment. sessions_restored = at least
                # one claude process is running on the host — a live agent
                # session may or may not live inside tmux (e.g. sessions started
                # by a service run outside it), so we key on claude
                # processes, not on the tmux server. tmux_active stays a
                # separate bare fact for context; it is NOT a precondition.
                facts = self.facts.get(h, {})
                restored = int(facts.get("claude_procs", 0) or 0) > 0
                # G3: expose un-ack'd message counts so a sender/operator can
                # see when the hub is holding messages whose delivery nobody
                # confirmed.
                inflight_count = sum(1 for r in self.inflight.values()
                                     if r.get("host") == h)
                out[h] = {"alive": alive, "last_seen": seen,
                          "last_seen_age_s": round(now - seen, 1),
                          "sessions_restored": restored,
                          "tmux_active": facts.get("tmux_active"),
                          "claude_procs": facts.get("claude_procs"),
                          "personas": sorted(livep),
                          "socket_missing": sorted(
                              p for p in self.degraded.get(h, {}) if p not in livep),
                          "inflight_count": inflight_count,
                          "inflight_dropped_total": self.inflight_dropped.get(h, 0)}
            return out

    def _snapshot_locked(self, host: str) -> dict:
        sess = self.sessions.get(host, {})
        return {s["persona"]: {"pid": s.get("pid"), "socket": s.get("socket"),
                               "name": s.get("name"), "alive": s["alive"]}
                for s in sess.values() if s.get("persona")}


MAX_BODY_BYTES = 1 << 20   # 1 MiB: larger requests are refused before they are read


class BodyTooLarge(Exception):
    pass


class BadContentLength(Exception):
    pass


class Handler(BaseHTTPRequestHandler):
    hub: Hub = None
    token: str | None = None   # shared bearer token; None = no authentication (loopback only)

    def _authorized(self) -> bool:
        if not self.token:
            return True
        got = (self.headers.get("Authorization") or "").encode("utf-8")
        want = ("Bearer " + self.token).encode("utf-8")
        return hmac.compare_digest(got, want)

    def log_message(self, *a):  # quiet
        pass

    def _read_json(self):
        raw = (self.headers.get("Content-Length") or "").strip()
        if raw and not (raw.isascii() and raw.isdigit()):
            raise BadContentLength(raw)
        n = int(raw) if raw else 0
        if not n:
            return {}
        if n > MAX_BODY_BYTES:
            raise BodyTooLarge(n)
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    def _send(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path != "/health" and not self._authorized():
            self._send({"ok": False, "error": "unauthorized"}, 401)
            return
        if u.path == "/who":
            self._send(self.hub.who())
        elif u.path == "/status":
            self._send(self.hub.status())
        elif u.path == "/poll":
            host = (q.get("host") or [""])[0]
            if not host:
                self._send({"ok": False, "error": "missing host"}, 400)
                return
            msg = self.hub.poll(host)
            self._send({"ok": True, "message": msg})
        elif u.path == "/health":
            self._send({"ok": True, "version": HUB_VERSION, "host": self.hub_host})
        else:
            self._send({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        if not self._authorized():
            self.close_connection = True
            self._send({"ok": False, "error": "unauthorized"}, 401)
            return
        try:
            body = self._read_json()
        except BodyTooLarge:
            self.close_connection = True
            self._send({"ok": False, "error": f"request body over {MAX_BODY_BYTES} bytes"}, 413)
            return
        except BadContentLength:
            self.close_connection = True
            self._send({"ok": False, "error": "invalid Content-Length"}, 400)
            return
        if u.path == "/hb":
            host = body.get("host")
            if not host:
                self._send({"ok": False, "error": "missing host"}, 400)
                return
            snap = self.hub.heartbeat(host, body.get("sessions", []),
                                      body.get("degraded", []),
                                      body.get("facts", {}),
                                      body.get("caps", []),
                                      body.get("unroutable", []),
                                      peer_ip=self.client_address[0],
                                      unresponsive_pids=body.get("unresponsive_pids", []))
            self._send({"ok": True, "sessions": snap})
        elif u.path == "/send":
            for k in ("from_host", "to_host", "to_persona", "message"):
                if k not in body:
                    self._send({"ok": False, "error": f"missing {k}"}, 400)
                    return
            r = self.hub.send(body["from_host"], body.get("from_name", ""),
                              body["to_host"], body["to_persona"], body["message"],
                              extra={"from_window": body.get("from_window"),
                                     "from_pid": body.get("from_pid"),
                                     "to_match": body.get("to_match"),
                                     # the sending shim machine-detected
                                     # webchat; pass through to outbox so
                                     # target shim can skip the wrapper.
                                     "human": bool(body.get("human")),
                                     "from_session_id": body.get("from_session_id"),
                                     # peer IP of this /send POST so the hub
                                     # can compare against the registered
                                     # shim IP of body["from_host"]. A
                                     # mismatch silently drops the `human`
                                     # claim (message still routes normally).
                                     "peer_ip": self.client_address[0]})
            self._send(r)
        elif u.path == "/ack":
            host = body.get("host")
            mid = body.get("id")
            if not (host and mid):
                self._send({"ok": False, "error": "missing host or id"}, 400)
                return
            self.hub.ack(host, str(mid), bool(body.get("ok", False)),
                         body.get("error", ""))
            self._send({"ok": True})
        else:
            self._send({"ok": False, "error": "not found"}, 404)


DEFAULT_BIND = "127.0.0.1"   # other machines can reach the hub only after an explicit --bind
LOOPBACK_ADDRS = {"127.0.0.1", "::1", "localhost"}


def main() -> int:
    ap = argparse.ArgumentParser(prog="mcp-msgers-hub")
    ap.add_argument("command", nargs="?", choices=["serve"], default="serve",
                    help="the only command (optional); supervisor units pass it explicitly")
    ap.add_argument("--port", type=int, default=8465)
    ap.add_argument("--poll-timeout", type=int, default=25,
                    help="max seconds a /poll request is held before returning empty")
    ap.add_argument("--bind", default=None,
                    help=f"bind address (default {DEFAULT_BIND}: this machine only). Set a private "
                         "LAN/VPN address to let other machines reach the hub; see SECURITY.md")
    ap.add_argument("--host-name", default=None)
    ap.add_argument("--token-file", default=None,
                    help="file holding the shared bearer token (one line); clients send it in "
                         "the Authorization header. Required for any non-loopback --bind.")
    ap.add_argument("--insecure-no-auth", action="store_true",
                    help="allow a non-loopback bind without a token (not recommended)")
    args = ap.parse_args()

    import socket as _s
    hostname = args.host_name or os.environ.get("HOSTNAME") or _s.gethostname()

    hub = Hub(poll_timeout=args.poll_timeout)
    Handler.hub = hub
    Handler.hub_host = hostname
    addr = args.bind or DEFAULT_BIND
    token = None
    if args.token_file:
        token = pathlib.Path(args.token_file).expanduser().read_text(encoding="utf-8").strip()
        if not token:
            print(f"hub: token file {args.token_file} is empty", file=sys.stderr)
            return 2
    if addr not in LOOPBACK_ADDRS and not token and not args.insecure_no_auth:
        print(f"hub: refusing to listen on {addr} without --token-file "
              "(pass --insecure-no-auth to override)", file=sys.stderr)
        return 2
    Handler.token = token
    server = ThreadingHTTPServer((addr, args.port), Handler)
    server.daemon_threads = True
    print(f"mcp-msgers hub ready host={hostname} listen={addr}:{args.port} "
          f"poll_timeout={args.poll_timeout}s pid={os.getpid()}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
