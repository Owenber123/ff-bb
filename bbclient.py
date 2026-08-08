"""Thin BlueBubbles REST client.

Auth is a query parameter on every request. ``password`` is the canonical
name; ``guid`` and ``token`` are accepted aliases by the server, but we only
ever send ``password``.

Sends go through AppleScript on the Mac and take roughly 800ms each, so
``send_text`` serialises them behind a lock and enforces a minimum interval.
Never fan out bulk sends.
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Callable, Iterable, Iterator

import requests

from echo_guard import apply_bot_prefix
from logging_setup import get_logger

log = get_logger(__name__)

# Truncated defensively: iMessage handles long bodies, but a runaway handler
# should not try to push a megabyte through AppleScript.
MAX_MESSAGE_CHARS = 4000


def _sent_guid(body: Any) -> str | None:
    """Pull the created message's GUID out of a send response, if it has one.

    Unverified shape, so this is best-effort: the echo guard falls back to
    matching on text when no GUID comes back.
    """
    if isinstance(body, dict):
        data = body.get("data")
        if isinstance(data, dict):
            guid = data.get("guid")
            if isinstance(guid, str) and guid:
                return guid
    return None


class BlueBubblesError(RuntimeError):
    """Any non-success response, transport failure, or malformed body."""

    def __init__(self, message: str, status: int | None = None, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


class BlueBubblesClient:
    def __init__(
        self,
        base_url: str,
        password: str,
        timeout: float = 20.0,
        send_min_interval: float = 1.0,
        bot_prefix: str = "",
        dry_run: bool = False,
        on_send: Callable[[str, str, str | None], None] | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.password = password
        self.timeout = timeout
        self.send_min_interval = send_min_interval
        # Stamped on every outgoing message. Marks automated messages visibly
        # for everyone in the chat, and pushes any command prefix into
        # mid-string where parse_command will not match it.
        self.bot_prefix = bot_prefix
        self.dry_run = dry_run
        # Called after every successful send with (chat_guid, sent_text,
        # message_guid). This is the choke point, so anything that sends gets
        # recorded in the echo guard regardless of which caller sent it.
        self.on_send = on_send

        self._session = requests.Session()
        self._send_lock = threading.Lock()
        self._last_send = 0.0

    # -- plumbing ------------------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self.base_url}/api/v1/{path.lstrip('/')}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        url = self._url(path)
        query = {"password": self.password}
        if params:
            query.update(params)

        try:
            resp = self._session.request(
                method,
                url,
                params=query,
                json=json_body,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            # The Mac is the fragile dependency here. FileVault means a reboot
            # leaves the server unreachable until someone logs in.
            raise BlueBubblesError(
                f"{method} {path} failed to reach the BlueBubbles server: {exc}"
            ) from exc

        try:
            body = resp.json()
        except ValueError:
            body = resp.text

        if resp.status_code >= 400:
            detail = body
            if isinstance(body, dict):
                detail = body.get("error") or body.get("message") or body
            raise BlueBubblesError(
                f"{method} {path} -> HTTP {resp.status_code}: {detail}",
                status=resp.status_code,
                body=body,
            )

        log.debug(
            "bluebubbles request ok",
            extra={"fields": {"method": method, "path": path, "status": resp.status_code}},
        )
        return body

    @staticmethod
    def _data(body: Any) -> Any:
        """BlueBubbles wraps responses as {status, message, data}."""
        if isinstance(body, dict) and "data" in body:
            return body["data"]
        return body

    # -- endpoints -----------------------------------------------------------

    def ping(self) -> dict[str, Any]:
        """GET /api/v1/ping -- 200 on success, 401 'Missing server password!'."""
        body = self._request("GET", "ping")
        return body if isinstance(body, dict) else {"data": body}

    def send_text(self, chat_guid: str, message: str) -> dict[str, Any]:
        """POST /api/v1/message/text.

        Serialised and rate limited: each send is ~800ms of AppleScript.
        """
        text = (message or "").strip()
        if not text:
            raise BlueBubblesError("refusing to send an empty message")

        # Once per message, at the top -- not per line. Applied here so that
        # command replies, error replies and the weekly summary all get it.
        text = apply_bot_prefix(text, self.bot_prefix)
        if len(text) > MAX_MESSAGE_CHARS:
            text = text[: MAX_MESSAGE_CHARS - 3] + "..."

        if self.dry_run:
            log.info(
                "DRY RUN: not sending",
                extra={"fields": {"chat_guid": chat_guid, "chars": len(text), "message": text}},
            )
            if self.on_send:
                self.on_send(chat_guid, text, None)
            return {"status": 200, "dryRun": True, "data": {"message": text}}

        payload = {
            "chatGuid": chat_guid,
            "message": text,
            "tempGuid": f"bot-{uuid.uuid4()}",
            "method": "apple-script",
        }

        with self._send_lock:
            wait = self.send_min_interval - (time.monotonic() - self._last_send)
            if wait > 0:
                time.sleep(wait)
            started = time.monotonic()
            try:
                body = self._request("POST", "message/text", json_body=payload)
            finally:
                self._last_send = time.monotonic()

        log.info(
            "sent message",
            extra={
                "fields": {
                    "chat_guid": chat_guid,
                    "chars": len(text),
                    "elapsed_ms": round((time.monotonic() - started) * 1000),
                }
            },
        )

        if self.on_send:
            self.on_send(chat_guid, text, _sent_guid(body))
        return body if isinstance(body, dict) else {"data": body}

    def query_messages(
        self,
        *,
        after: int | None = None,
        before: int | None = None,
        chat_guid: str | None = None,
        limit: int = 500,
        offset: int = 0,
        with_: Iterable[str] = ("chat", "handle"),
        sort: str = "DESC",
    ) -> list[dict[str, Any]]:
        """POST /api/v1/message/query. ``after``/``before`` are epoch millis."""
        body: dict[str, Any] = {
            "limit": limit,
            "offset": offset,
            "with": list(with_),
            "sort": sort,
        }
        if after is not None:
            body["after"] = int(after)
        if before is not None:
            body["before"] = int(before)
        if chat_guid:
            # Not every server build honours this; callers should also filter
            # client-side against the chat embedded in each message.
            body["chatGuid"] = chat_guid

        data = self._data(self._request("POST", "message/query", json_body=body))
        if isinstance(data, list):
            return data
        log.warning(
            "message/query returned an unexpected shape",
            extra={"fields": {"type": type(data).__name__}},
        )
        return []

    def iter_messages(
        self,
        *,
        after: int,
        chat_guid: str | None = None,
        page_size: int = 500,
        max_messages: int = 5000,
    ) -> Iterator[dict[str, Any]]:
        """Page through message/query until exhausted or max_messages reached."""
        offset = 0
        yielded = 0
        while yielded < max_messages:
            page = self.query_messages(
                after=after,
                chat_guid=chat_guid,
                limit=min(page_size, max_messages - yielded),
                offset=offset,
            )
            if not page:
                return
            for msg in page:
                yield msg
                yielded += 1
                if yielded >= max_messages:
                    log.warning(
                        "hit max_messages cap; summary may be incomplete",
                        extra={"fields": {"max_messages": max_messages}},
                    )
                    return
            if len(page) < page_size:
                return
            offset += len(page)

    def query_chats(
        self,
        *,
        limit: int = 200,
        offset: int = 0,
        with_: Iterable[str] = ("participants", "lastMessage"),
        sort: str | None = "lastmessage",
    ) -> list[dict[str, Any]]:
        """POST /api/v1/chat/query."""
        body: dict[str, Any] = {"limit": limit, "offset": offset, "with": list(with_)}
        if sort:
            body["sort"] = sort
        try:
            data = self._data(self._request("POST", "chat/query", json_body=body))
        except BlueBubblesError as exc:
            if exc.status != 400:
                raise
            # Older/newer builds disagree on which `with` and `sort` values are
            # legal. Fall back to the minimal query rather than failing outright.
            log.warning(
                "chat/query rejected the full query; retrying minimally",
                extra={"fields": {"error": str(exc)}},
            )
            data = self._data(
                self._request(
                    "POST", "chat/query", json_body={"limit": limit, "offset": offset}
                )
            )
        return data if isinstance(data, list) else []

    def iter_chats(self, page_size: int = 200, max_chats: int = 2000) -> Iterator[dict]:
        offset = 0
        seen = 0
        while seen < max_chats:
            page = self.query_chats(limit=page_size, offset=offset)
            if not page:
                return
            for chat in page:
                yield chat
                seen += 1
            if len(page) < page_size:
                return
            offset += len(page)

    # -- webhook management --------------------------------------------------

    def list_webhooks(self) -> list[dict[str, Any]]:
        data = self._data(self._request("GET", "webhook"))
        return data if isinstance(data, list) else []

    def create_webhook(self, url: str, events: Iterable[str] = ("new-message",)) -> Any:
        return self._data(
            self._request("POST", "webhook", json_body={"url": url, "events": list(events)})
        )

    def delete_webhook(self, webhook_id: Any) -> Any:
        return self._request("DELETE", f"webhook/{webhook_id}")
