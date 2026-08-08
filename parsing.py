"""Webhook payload parsing.

The BlueBubbles webhook body is documented as roughly::

    {"type": "new-message", "data": {"text": ..., "isFromMe": ..., "chats": [...],
                                     "handle": {"address": ..., "displayName": ...}}}

That is NOT verified against this server. Everything here reads through
``_first`` helpers that accept several plausible key spellings and degrade to
None rather than raising. If a field comes back empty, turn on DEBUG logging:
the raw payload is dumped verbatim on every event, and adjusting this module to
match reality should be a one-line change.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from logging_setup import get_logger

log = get_logger(__name__)

# iMessage uses U+FFFC (object replacement char) as the body of an
# attachment-only message. Treat it as empty text.
OBJECT_REPLACEMENT = "￼"


@dataclass
class IncomingMessage:
    guid: str
    chat_guid: str
    text: str
    is_from_me: bool
    sender_address: str
    sender_name: str
    is_group: bool
    date_created: int | None
    raw: dict[str, Any]

    @property
    def display_sender(self) -> str:
        return self.sender_name or self.sender_address or "unknown"


def _first(source: Any, *keys: str, default: Any = None) -> Any:
    """Return the first present, non-None key from a dict."""
    if not isinstance(source, dict):
        return default
    for key in keys:
        if key in source and source[key] is not None:
            return source[key]
    return default


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes")
    return False


def _clean_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.replace(OBJECT_REPLACEMENT, "").strip()


def extract_chat(data: dict[str, Any]) -> dict[str, Any]:
    """Find the chat object. ``chats`` is an array; the first element is ours."""
    chats = _first(data, "chats", "chat")
    if isinstance(chats, list) and chats:
        return chats[0] if isinstance(chats[0], dict) else {}
    if isinstance(chats, dict):
        return chats
    return {}


def chat_guid_of(data: dict[str, Any]) -> str:
    chat = extract_chat(data)
    guid = _first(chat, "guid", "chatGuid", "chat_guid")
    if not guid:
        guid = _first(data, "chatGuid", "chat_guid")
    return str(guid) if guid else ""


def is_group_chat(chat_guid: str, chat: dict[str, Any] | None = None) -> bool:
    """DMs are ``iMessage;-;+1555...``; groups are ``iMessage;+;chat<digits>``.

    The GUID separator is the most reliable signal. ``style`` (43 = group,
    45 = DM) and participant count are fallbacks.
    """
    if ";+;" in chat_guid:
        return True
    if ";-;" in chat_guid:
        return False
    if isinstance(chat, dict):
        style = _first(chat, "style")
        if style is not None:
            try:
                return int(style) == 43
            except (TypeError, ValueError):
                pass
        participants = _first(chat, "participants", default=[])
        if isinstance(participants, list):
            return len(participants) > 1
    return False


def handle_of(data: dict[str, Any]) -> tuple[str, str]:
    """Return (address, display_name) for the sender."""
    handle = _first(data, "handle", "from", "sender")
    if isinstance(handle, list) and handle:
        handle = handle[0]
    if not isinstance(handle, dict):
        handle = {}

    address = _first(handle, "address", "id", "value", default="")
    name = _first(handle, "displayName", "display_name", "uncanonicalizedId", default="")

    if not address:
        address = _first(data, "handleId", "address", default="")

    return (str(address or ""), str(name or ""))


def parse_new_message(payload: dict[str, Any]) -> IncomingMessage | None:
    """Parse a ``new-message`` webhook body into an IncomingMessage.

    Returns None when the payload is not a usable new-message event. Never
    raises on shape surprises -- it logs and returns None instead.
    """
    if not isinstance(payload, dict):
        log.warning("webhook payload was not an object", extra={"fields": {"type": type(payload).__name__}})
        return None

    event_type = str(_first(payload, "type", "event", default="") or "")
    data = _first(payload, "data", "message", default=None)
    if not isinstance(data, dict):
        # Some deployments post the message object at the top level.
        data = payload if "text" in payload or "isFromMe" in payload else None
    if not isinstance(data, dict):
        log.warning(
            "webhook payload had no usable data object",
            extra={"fields": {"event_type": event_type, "keys": sorted(payload)[:12]}},
        )
        return None

    chat_guid = chat_guid_of(data)
    chat = extract_chat(data)
    address, name = handle_of(data)

    date_created = _first(data, "dateCreated", "date_created")
    try:
        date_created = int(date_created) if date_created is not None else None
    except (TypeError, ValueError):
        date_created = None

    msg = IncomingMessage(
        guid=str(_first(data, "guid", "messageGuid", default="") or ""),
        chat_guid=chat_guid,
        text=_clean_text(_first(data, "text", "body", "message")),
        is_from_me=_as_bool(_first(data, "isFromMe", "is_from_me")),
        sender_address=address,
        sender_name=name,
        is_group=is_group_chat(chat_guid, chat),
        date_created=date_created,
        raw=data,
    )

    if not msg.chat_guid:
        log.warning(
            "could not find a chat GUID in the webhook payload; "
            "run with BB_LOG_LEVEL=DEBUG and compare against parsing.py",
            extra={"fields": {"data_keys": sorted(data)[:20]}},
        )
        return None

    return msg


# --- helpers shared with the summary path ---------------------------------


def message_sender(msg: dict[str, Any], me_label: str = "Me") -> str:
    """Best-effort display name for a message from ``message/query``."""
    if _as_bool(_first(msg, "isFromMe", "is_from_me")):
        return me_label
    address, name = handle_of(msg)
    return name or address or "Unknown"


def message_text(msg: dict[str, Any]) -> str:
    return _clean_text(_first(msg, "text", "body"))


def message_timestamp_ms(msg: dict[str, Any]) -> int | None:
    """Epoch millis for a message, preferring the delivered/created date."""
    for key in ("dateCreated", "date_created", "dateDelivered", "dateRead"):
        value = _first(msg, key)
        if value is None:
            continue
        try:
            value = int(value)
        except (TypeError, ValueError):
            continue
        if value <= 0:
            continue
        # Apple's Core Data epoch (2001-01-01) shows up in some builds as
        # nanoseconds since 2001. Anything that small is not epoch millis.
        if value < 1_000_000_000_000:
            value = value + 978_307_200_000 if value > 1_000_000_000 else value * 1000
        return value
    return None


def is_tapback(msg: dict[str, Any]) -> bool:
    """Reactions arrive as messages associated with another message."""
    if _first(msg, "associatedMessageGuid", "associated_message_guid"):
        return True
    kind = _first(msg, "associatedMessageType", "associated_message_type")
    try:
        return kind is not None and int(kind) != 0
    except (TypeError, ValueError):
        return bool(kind)
