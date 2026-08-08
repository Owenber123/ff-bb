"""Webhook listener.

The HTTP handler does almost nothing: it logs the raw payload, drops it on a
queue, and returns 200 immediately. All parsing, dispatch, and sending happens
on worker threads, because a send is ~800ms of AppleScript and we do not want
the BlueBubbles server waiting on that (or retrying because we timed out).

Guard order in ``process_event`` is deliberate:

  1. event type is new-message
  2. payload parses
  3. not a duplicate delivery (webhook redelivery)
  4. chat is on the allowlist
  5. **loop guards** -- see echo_guard.py for why there are three:
       a. anything carrying the bot prefix, whoever sent it
       b. isFromMe *without* the command prefix (own chatter, not a command;
          isFromMe *with* the prefix is the operator commanding from their own
          device, and is processed)
       c. text matching something we recently sent
  6. text is actually a command
"""

from __future__ import annotations

import queue
import threading
import time
from collections import OrderedDict
from typing import Any

from flask import Flask, jsonify, request

from bbclient import BlueBubblesClient, BlueBubblesError
from config import Config
from echo_guard import SentRegistry, has_bot_prefix
from history import ChatHistory
from logging_setup import get_logger
from parsing import parse_new_message
from registry import CommandContext, dispatch, parse_command

import commands  # noqa: F401  -- imported for its @command registrations
import ffcommands  # noqa: F401  -- ditto, the Sleeper/fantasy set

log = get_logger(__name__)

QUEUE_MAXSIZE = 500
DEDUPE_SIZE = 1000


class Dispatcher:
    """Worker pool that turns webhook payloads into replies."""

    def __init__(
        self,
        cfg: Config,
        client: BlueBubblesClient,
        history: ChatHistory,
        sent: SentRegistry | None = None,
    ):
        self.cfg = cfg
        self.client = client
        self.history = history
        self.sent = sent if sent is not None else SentRegistry(bot_prefix=cfg.bot_prefix)
        self.queue: queue.Queue = queue.Queue(maxsize=QUEUE_MAXSIZE)
        self.started_at = time.time()

        self._seen: OrderedDict[str, float] = OrderedDict()
        self._seen_lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

        self.stats = {
            "received": 0,
            "dropped_from_me": 0,
            "dropped_bot_prefix": 0,
            "dropped_own_echo": 0,
            "dropped_not_allowed": 0,
            "dropped_duplicate": 0,
            "dropped_unparsed": 0,
            "commands_from_me": 0,
            "commands_run": 0,
            "replies_sent": 0,
            "send_failures": 0,
            "queue_overflows": 0,
        }
        self._stats_lock = threading.Lock()

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        for i in range(max(1, self.cfg.worker_threads)):
            thread = threading.Thread(target=self._run, name=f"dispatch-{i}", daemon=True)
            thread.start()
            self._threads.append(thread)
        log.info("dispatcher started", extra={"fields": {"workers": len(self._threads)}})

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                payload = self.queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self.process_event(payload)
            except Exception:
                # Belt and braces: dispatch() already isolates handler errors,
                # but a worker thread must never die.
                log.exception("unhandled error while processing a webhook event")
            finally:
                self.queue.task_done()

    def _bump(self, key: str, amount: int = 1) -> None:
        with self._stats_lock:
            self.stats[key] = self.stats.get(key, 0) + amount

    # -- intake --------------------------------------------------------------

    def enqueue(self, payload: dict[str, Any]) -> bool:
        try:
            self.queue.put_nowait(payload)
            return True
        except queue.Full:
            self._bump("queue_overflows")
            log.error("webhook queue is full; dropping event", extra={"fields": {"size": QUEUE_MAXSIZE}})
            return False

    def _is_duplicate(self, guid: str) -> bool:
        if not guid:
            return False
        with self._seen_lock:
            if guid in self._seen:
                return True
            self._seen[guid] = time.time()
            while len(self._seen) > DEDUPE_SIZE:
                self._seen.popitem(last=False)
        return False

    # -- the actual pipeline -------------------------------------------------

    def process_event(self, payload: dict[str, Any]) -> None:
        self._bump("received")

        event_type = str(payload.get("type") or payload.get("event") or "")
        if event_type and event_type != "new-message":
            log.debug("ignoring event type", extra={"fields": {"event_type": event_type}})
            return

        msg = parse_new_message(payload)
        if msg is None:
            self._bump("dropped_unparsed")
            return

        log.debug(
            "parsed message",
            extra={
                "fields": {
                    "guid": msg.guid,
                    "chat_guid": msg.chat_guid,
                    "is_from_me": msg.is_from_me,
                    "is_group": msg.is_group,
                    "sender": msg.display_sender,
                    "text": msg.text[:200],
                }
            },
        )

        if self._is_duplicate(msg.guid):
            self._bump("dropped_duplicate")
            log.debug("ignoring duplicate delivery", extra={"fields": {"guid": msg.guid}})
            return

        if not self.cfg.is_chat_allowed(msg.chat_guid):
            self._bump("dropped_not_allowed")
            log.info(
                "chat not on the allowlist; ignoring. Send !chatinfo from an "
                "allowlisted chat, or run list_chats.py, to get GUIDs.",
                extra={"fields": {"chat_guid": msg.chat_guid}},
            )
            return

        parsed = parse_command(msg.text, self.cfg.command_prefix)

        # --- Loop guard 1: anything wearing the bot prefix is machine output,
        # whoever appears to have sent it. Cheapest and broadest, so it runs
        # first. Disabled when BB_BOT_PREFIX is empty.
        if has_bot_prefix(msg.text, self.cfg.bot_prefix):
            self._bump("dropped_bot_prefix")
            log.debug(
                "ignoring a message carrying the bot prefix",
                extra={"fields": {"chat_guid": msg.chat_guid, "is_from_me": msg.is_from_me}},
            )
            return

        if msg.is_from_me:
            # --- Loop guard 2: our own Apple ID. A command is the operator
            # typing from their own device and is processed; anything else is
            # ordinary chatter (or a bot reply) and is dropped.
            if parsed is None:
                self._bump("dropped_from_me")
                log.debug(
                    "ignoring our own non-command message",
                    extra={"fields": {"chat_guid": msg.chat_guid}},
                )
                return

            # --- Loop guard 3: independent of the prefix entirely. If we said
            # this recently, it is an echo however it got here.
            if self.sent.matches(msg.chat_guid, msg.text, msg.guid):
                self._bump("dropped_own_echo")
                log.warning(
                    "dropped a message matching one we recently sent; the bot "
                    "prefix guard did not catch it, which is worth a look",
                    extra={"fields": {"chat_guid": msg.chat_guid, "text": msg.text[:120]}},
                )
                return

            self._bump("commands_from_me")
            log.info(
                "processing a command from our own Apple ID",
                extra={"fields": {"chat_guid": msg.chat_guid, "command": parsed[0]}},
            )

        # Recorded for every allowlisted message that survives the guards, not
        # just commands: this is the per-chat context a future AI handler reads.
        self.history.record(msg.chat_guid, "user", msg.display_sender, msg.text)

        if parsed is None:
            return

        name, args = parsed
        ctx = CommandContext(
            chat_guid=msg.chat_guid,
            sender_name=msg.sender_name,
            sender_address=msg.sender_address,
            text=msg.text,
            is_group=msg.is_group,
            command=name,
            message_guid=msg.guid,
            client=self.client,
            config=self.cfg,
            history=self.history,
        )

        self._bump("commands_run")
        reply = dispatch(name, args, ctx)
        if not reply:
            # Unknown commands and silent handlers both land here.
            if self.cfg.reply_on_unknown_command and not _command_exists(name):
                reply = f"Unknown command {self.cfg.command_prefix}{name}. Try {self.cfg.command_prefix}help."
            else:
                return

        try:
            self.client.send_text(msg.chat_guid, reply)
            self._bump("replies_sent")
            self.history.record(msg.chat_guid, "bot", "bot", reply)
        except BlueBubblesError as exc:
            self._bump("send_failures")
            log.error(
                "failed to send reply",
                extra={"fields": {"chat_guid": msg.chat_guid, "error": str(exc)}},
            )


def _command_exists(name: str) -> bool:
    from registry import get_command

    return get_command(name) is not None


def create_app(
    cfg: Config,
    client: BlueBubblesClient,
    history: ChatHistory,
    sent: SentRegistry | None = None,
) -> tuple[Flask, Dispatcher]:
    app = Flask(__name__)
    dispatcher = Dispatcher(cfg, client, history, sent=sent)

    # Wire the send choke point into the echo guard here rather than at
    # construction time, so every send is recorded no matter which caller made
    # it and regardless of the order the two objects were built in.
    client.on_send = dispatcher.sent.record

    # Layer 1 is only as good as the prefix the client actually stamps, and a
    # caller that built the client without one would silently lose it. The
    # config is authoritative here.
    if client.bot_prefix != cfg.bot_prefix:
        log.warning(
            "client bot_prefix did not match the config; using the config value",
            extra={"fields": {"client": client.bot_prefix, "config": cfg.bot_prefix}},
        )
        client.bot_prefix = cfg.bot_prefix
    if client.dry_run != cfg.dry_run:
        log.warning(
            "client dry_run did not match the config; using the config value",
            extra={"fields": {"client": client.dry_run, "config": cfg.dry_run}},
        )
        client.dry_run = cfg.dry_run

    @app.post(cfg.webhook_path)
    def webhook():  # type: ignore[unused-ignore]
        raw = request.get_data(as_text=True)
        payload = request.get_json(force=True, silent=True)

        # This dump is the main debugging tool for payload-shape mismatches.
        log.debug(
            "raw webhook payload",
            extra={"fields": {"content_type": request.content_type, "body": raw[:8000]}},
        )

        if not isinstance(payload, dict):
            log.warning(
                "webhook body was not a JSON object",
                extra={"fields": {"content_type": request.content_type, "body": raw[:500]}},
            )
            return jsonify({"ok": False, "error": "expected a JSON object"}), 400

        # Return immediately; a send takes ~800ms and the server should not wait.
        dispatcher.enqueue(payload)
        return jsonify({"ok": True}), 200

    @app.get("/health")
    def health():  # type: ignore[unused-ignore]
        body: dict[str, Any] = {
            "status": "ok",
            "uptime_seconds": round(time.time() - dispatcher.started_at, 1),
            "queue_depth": dispatcher.queue.qsize(),
            "allowlisted_chats": "ALL" if cfg.allow_all_chats else len(cfg.allowed_chats),
            "webhook_path": cfg.webhook_path,
            "command_prefix": cfg.command_prefix,
            "bot_prefix": cfg.bot_prefix,
            "dry_run": cfg.dry_run,
            "recent_sends_tracked": dispatcher.sent.size(),
            "stats": dict(dispatcher.stats),
        }

        # ?deep=1 also round-trips to the Mac. Left off by default so that a
        # monitoring probe does not hammer a fragile dependency.
        if request.args.get("deep"):
            started = time.monotonic()
            try:
                client.ping()
                body["bluebubbles"] = {
                    "reachable": True,
                    "latency_ms": round((time.monotonic() - started) * 1000),
                }
            except Exception as exc:
                body["status"] = "degraded"
                body["bluebubbles"] = {"reachable": False, "error": str(exc)}
                return jsonify(body), 503

        return jsonify(body), 200

    @app.get("/")
    def index():  # type: ignore[unused-ignore]
        return jsonify({"service": "bluebubbles-bot", "health": "/health", "webhook": cfg.webhook_path})

    return app, dispatcher
