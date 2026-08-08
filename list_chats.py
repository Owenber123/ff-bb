#!/usr/bin/env python3
"""List chat GUIDs with participant names.

This is how you fill in BB_ALLOWED_CHATS and BB_SUMMARY_CHAT. Run it, find the
chats you want, copy their GUIDs.

    python list_chats.py
    python list_chats.py --groups          only group chats
    python list_chats.py --search family   filter by name/participant/GUID
    python list_chats.py --env             emit a ready-to-paste BB_ALLOWED_CHATS line

Equivalent to `python bot.py chats`.
"""

from __future__ import annotations

import argparse
import sys

import logging_setup
from bbclient import BlueBubblesClient, BlueBubblesError
from config import ConfigError, load_config
from summary import describe_chat


def _is_group(chat: dict) -> bool:
    guid = str(chat.get("guid") or "")
    if ";+;" in guid:
        return True
    if ";-;" in guid:
        return False
    participants = chat.get("participants") or []
    return isinstance(participants, list) and len(participants) > 1


def collect_chats(
    client: BlueBubblesClient,
    limit: int = 200,
    only_groups: bool = False,
    search: str = "",
) -> list[dict]:
    rows: list[dict] = []
    needle = search.lower().strip()

    for chat in client.iter_chats(max_chats=limit):
        if not isinstance(chat, dict):
            continue
        guid = str(chat.get("guid") or "")
        if not guid:
            continue
        group = _is_group(chat)
        if only_groups and not group:
            continue

        label = describe_chat(chat)
        if needle and needle not in label.lower() and needle not in guid.lower():
            continue

        rows.append({"guid": guid, "label": label, "group": group})
        if len(rows) >= limit:
            break

    return rows


def print_chats(
    client: BlueBubblesClient,
    limit: int = 200,
    only_groups: bool = False,
    search: str = "",
    as_env: bool = False,
) -> int:
    try:
        rows = collect_chats(client, limit=limit, only_groups=only_groups, search=search)
    except BlueBubblesError as exc:
        print(f"Could not list chats: {exc}", file=sys.stderr)
        return 1

    if not rows:
        print("No chats matched.", file=sys.stderr)
        return 1

    if as_env:
        print("BB_ALLOWED_CHATS=" + ",".join(r["guid"] for r in rows))
        return 0

    width = max(len(r["label"]) for r in rows)
    width = min(max(width, 20), 48)

    print(f"{'TYPE':<6} {'NAME':<{width}} GUID")
    print(f"{'-' * 6} {'-' * width} {'-' * 40}")
    for row in rows:
        label = row["label"]
        if len(label) > width:
            label = label[: width - 1] + "…"
        kind = "group" if row["group"] else "dm"
        print(f"{kind:<6} {label:<{width}} {row['guid']}")

    print(f"\n{len(rows)} chat(s).", file=sys.stderr)
    print(
        "Copy the GUIDs you want into BB_ALLOWED_CHATS (comma separated) "
        "and BB_SUMMARY_CHAT.",
        file=sys.stderr,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=200, help="max chats to show")
    parser.add_argument("--groups", action="store_true", help="only group chats")
    parser.add_argument("--search", default="", help="filter by name, participant, or GUID")
    parser.add_argument("--env", action="store_true", help="print a BB_ALLOWED_CHATS line")
    args = parser.parse_args(argv)

    try:
        cfg = load_config()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    logging_setup.configure(cfg.log_level, cfg.log_format)
    client = BlueBubblesClient(
        base_url=cfg.server_url,
        password=cfg.password,
        timeout=cfg.http_timeout,
        send_min_interval=cfg.send_min_interval,
    )
    return print_chats(
        client,
        limit=args.limit,
        only_groups=args.groups,
        search=args.search,
        as_env=args.env,
    )


if __name__ == "__main__":
    sys.exit(main())
