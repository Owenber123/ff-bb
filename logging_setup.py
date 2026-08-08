"""Structured logging.

Two formats, chosen with BB_LOG_FORMAT:

  text  (default)  human-readable, one line per event, with key=value extras
  json             one JSON object per line, for shipping somewhere

Any log call may attach structured fields via ``extra={"fields": {...}}``.
Raw webhook payloads are dumped at DEBUG level -- that is the primary tool for
diagnosing payload-shape mismatches, since the BlueBubbles payload schema is
not contractually guaranteed.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


def _extra_fields(record: logging.LogRecord) -> dict[str, Any]:
    fields = getattr(record, "fields", None)
    if isinstance(fields, dict):
        return dict(fields)
    # Also pick up loose extras passed as extra={"chat_guid": ...}
    return {k: v for k, v in record.__dict__.items() if k not in _RESERVED and k != "fields"}


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        fields = _extra_fields(record)
        if fields:
            rendered = " ".join(f"{k}={_compact(v)}" for k, v in sorted(fields.items()))
            base = f"{base} | {rendered}"
        return base


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        payload.update(_extra_fields(record))
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def _compact(value: Any) -> str:
    if isinstance(value, (dict, list)):
        text = json.dumps(value, default=str, ensure_ascii=False)
    else:
        text = str(value)
    if " " in text and not text.startswith(("{", "[", '"')):
        return json.dumps(text, ensure_ascii=False)
    return text


def configure(level: str = "INFO", fmt: str = "text") -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    # stderr, not stdout: `list_chats.py --env` and `bot.py summary --dry-run`
    # are meant to be piped, and log lines must not end up in that stream.
    # systemd captures both, so the journal is unaffected.
    handler = logging.StreamHandler(sys.stderr)
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            TextFormatter(
                fmt="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
    root.addHandler(handler)
    root.setLevel(getattr(logging, level, logging.INFO))

    # These are chatty and rarely useful once things work.
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
