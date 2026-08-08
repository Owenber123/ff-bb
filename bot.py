#!/usr/bin/env python3
"""BlueBubbles iMessage bot.

Modes:

    python bot.py                      run the webhook listener (default)
    python bot.py serve                same thing, explicitly
    python bot.py summary              build and send the weekly recap, then exit
    python bot.py summary --dry-run    print the recap instead of sending it
    python bot.py ping                 check the BlueBubbles server is reachable
    python bot.py webhooks list        list registered webhooks
    python bot.py webhooks register --url http://<lxc-ip>:8080/webhook
    python bot.py webhooks delete <id>
    python bot.py chats                list chat GUIDs with participant names

    python bot.py sleeper leagues <sleeper-username> [--season 2025]
                                       find your SLEEPER_LEAGUE_ID without
                                       digging through the app
    python bot.py sleeper check        load the configured league and print
                                       what the bot would see
    python bot.py sleeper refresh-players
                                       refresh the cached NFL player file
                                       (run this daily from cron)

    python bot.py post render recap    print a scheduled post without sending
    python bot.py post check           what the scheduler would fire, and why
    python bot.py post send recap      send one now (respects BB_DRY_RUN)
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading

import logging_setup
from bbclient import BlueBubblesClient, BlueBubblesError
from config import Config, ConfigError, load_config
from history import ChatHistory

log = logging_setup.get_logger("bot")


def build_client(cfg: Config) -> BlueBubblesClient:
    return BlueBubblesClient(
        base_url=cfg.server_url,
        password=cfg.password,
        timeout=cfg.http_timeout,
        send_min_interval=cfg.send_min_interval,
        bot_prefix=cfg.bot_prefix,
        dry_run=cfg.dry_run,
    )


def warn_about_dry_run(cfg: Config) -> None:
    if cfg.dry_run:
        log.warning("=" * 72)
        log.warning("BB_DRY_RUN is on. Sends are logged, not delivered.")
        log.warning("Reads still hit the real server, so this exercises the whole")
        log.warning("path except the final message/text call. Nothing reaches a chat.")
        log.warning("=" * 72)


def warn_about_bot_prefix(cfg: Config) -> None:
    if not cfg.bot_prefix.strip():
        log.warning("=" * 72)
        log.warning("BB_BOT_PREFIX is empty, so outgoing messages are not marked and")
        log.warning("the bot-prefix loop guard is disabled. The remaining guards are")
        log.warning("the command-prefix test and the recent-sends tracker.")
        log.warning("=" * 72)
    else:
        log.info("bot prefix", extra={"fields": {"prefix": cfg.bot_prefix}})


def warn_about_allowlist(cfg: Config) -> None:
    """Default-deny, and make the alternative loud."""
    if cfg.allow_all_chats:
        log.warning("=" * 72)
        log.warning("BB_ALLOW_ALL_CHATS is on. The bot will respond in EVERY conversation")
        log.warning("on this Apple ID, including group chats with other people in them.")
        log.warning("Set BB_ALLOWED_CHATS instead once you know which chats you want.")
        log.warning("=" * 72)
    elif not cfg.allowed_chats:
        log.warning("=" * 72)
        log.warning("BB_ALLOWED_CHATS is empty, so the bot will respond NOWHERE.")
        log.warning("Run `python bot.py chats` to list chat GUIDs, then set")
        log.warning("BB_ALLOWED_CHATS=<guid>[,<guid>...] and restart.")
        log.warning("=" * 72)
    else:
        log.info(
            "allowlist loaded",
            extra={"fields": {"chats": len(cfg.allowed_chats)}},
        )


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------


def warn_about_sleeper(cfg: Config) -> None:
    if not cfg.sleeper_league_id:
        log.warning(
            "SLEEPER_LEAGUE_ID is not set; the fantasy commands will say so when used. "
            "Find it with: python bot.py sleeper leagues <your-sleeper-username>"
        )
        return
    log.info(
        "sleeper configured",
        extra={
            "fields": {
                "league_id": cfg.sleeper_league_id,
                "mapped_handles": len(cfg.sleeper_user_map),
                "data_dir": cfg.sleeper_data_dir,
            }
        },
    )
    if not cfg.sleeper_user_map:
        log.info("SLEEPER_USER_MAP is empty; people can run !link <sleeper name> instead")


def prewarm_sleeper(cfg: Config) -> None:
    """Warm the player file and finished weeks in the background.

    Without this the first !standings of the day pays for a 15MB player file
    and a season of matchups while somebody watches nothing happen.

    **Entirely off the startup path**, including the load_view() that works out
    which weeks to fetch. That call is network I/O, and doing it inline meant a
    slow or unreachable Sleeper delayed the webhook listener from binding --
    turning a fantasy-data outage into a bot that answers nothing at all.
    """
    if not cfg.sleeper_prewarm or not cfg.sleeper_league_id:
        return

    def worker() -> None:
        try:
            from ffleague import load_view
            from sleeper import get_client

            client = get_client(cfg.sleeper_data_dir)
            view = load_view(
                client, cfg.sleeper_league_id, follow_previous=cfg.sleeper_follow_previous
            )
            client.prewarm(view.league_id, weeks=view.regular_weeks())
        except Exception as exc:
            # Prewarming is an optimisation. Never let it stop the service.
            log.warning("sleeper prewarm skipped", extra={"fields": {"error": str(exc)}})

    threading.Thread(target=worker, name="sleeper-prewarm-start", daemon=True).start()


def build_scheduler(cfg: Config, client: BlueBubblesClient):
    from ffschedule import Scheduler
    from ffstore import get_store
    from sleeper import get_client

    return Scheduler(cfg, client, get_client(cfg.sleeper_data_dir), get_store(cfg.sleeper_db_path))


def start_scheduler(cfg: Config, client: BlueBubblesClient):
    """Run the scheduled-post loop in the background. Never fatal."""
    try:
        scheduler = build_scheduler(cfg, client)
    except Exception as exc:
        log.error("could not build the scheduler; scheduled posts are off",
                  extra={"fields": {"error": str(exc)}})
        return None

    if not scheduler.enabled:
        if cfg.sleeper_posts_enabled and not cfg.sleeper_post_chat:
            log.warning(
                "SLEEPER_POSTS_ENABLED is on but SLEEPER_POST_CHAT is empty, so "
                "nothing will be posted. Run `python bot.py chats` for the GUID."
            )
        return None

    scheduler.start()
    return scheduler


def cmd_serve(cfg: Config, args: argparse.Namespace) -> int:
    from app import create_app

    client = build_client(cfg)
    history = ChatHistory(max_turns=cfg.history_size)
    app, dispatcher = create_app(cfg, client, history)

    warn_about_allowlist(cfg)
    warn_about_bot_prefix(cfg)
    warn_about_dry_run(cfg)
    warn_about_sleeper(cfg)
    prewarm_sleeper(cfg)
    scheduler = start_scheduler(cfg, client)

    # Non-fatal: the Mac may be locked (FileVault) and come back later.
    try:
        client.ping()
        log.info("BlueBubbles server reachable", extra={"fields": {"url": cfg.server_url}})
    except BlueBubblesError as exc:
        log.error(
            "BlueBubbles server is NOT reachable at startup; serving anyway",
            extra={"fields": {"url": cfg.server_url, "error": str(exc)}},
        )

    dispatcher.start()

    def shutdown(signum, _frame):
        log.info("shutting down", extra={"fields": {"signal": signum}})
        dispatcher.stop()
        if scheduler is not None:
            scheduler.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    log.info(
        "listening",
        extra={
            "fields": {
                "host": cfg.listen_host,
                "port": cfg.listen_port,
                "webhook_path": cfg.webhook_path,
                "prefix": cfg.command_prefix,
            }
        },
    )

    try:
        from waitress import serve as waitress_serve
    except ImportError:
        log.warning("waitress is not installed; falling back to the Flask dev server")
        app.run(host=cfg.listen_host, port=cfg.listen_port, threaded=True)
    else:
        waitress_serve(app, host=cfg.listen_host, port=cfg.listen_port, threads=4)
    return 0


# ---------------------------------------------------------------------------
# summary
# ---------------------------------------------------------------------------


def cmd_summary(cfg: Config, args: argparse.Namespace) -> int:
    from summary import build_summary, chat_name_map

    client = build_client(cfg)
    warn_about_dry_run(cfg)
    days = args.days or cfg.summary_days
    sources = cfg.summary_chats()

    if not sources and not cfg.allow_all_chats:
        log.error(
            "no source chats configured. Set BB_SUMMARY_SOURCE_CHATS (or "
            "BB_ALLOWED_CHATS), or set BB_ALLOW_ALL_CHATS=true to read everything."
        )
        return 2

    text = build_summary(
        client,
        days=days,
        chat_guids=sources,
        top_words=cfg.summary_top_words,
        max_messages=cfg.summary_max_messages,
        command_prefix=cfg.command_prefix,
        chat_names=chat_name_map(client, sources) if len(sources) > 1 else None,
        title=f"Weekly recap ({days} days)" if days == cfg.summary_days else None,
    )

    target = args.chat or cfg.summary_chat
    if args.dry_run or not target:
        if not target and not args.dry_run:
            log.warning("BB_SUMMARY_CHAT is not set; printing instead of sending")
        print(text)
        return 0

    try:
        client.send_text(target, text)
    except BlueBubblesError as exc:
        log.error("failed to send the summary", extra={"fields": {"error": str(exc)}})
        print(text)
        return 1

    log.info("summary sent", extra={"fields": {"chat_guid": target, "days": days}})
    return 0


# ---------------------------------------------------------------------------
# ping / webhooks / chats
# ---------------------------------------------------------------------------


def cmd_ping(cfg: Config, args: argparse.Namespace) -> int:
    client = build_client(cfg)
    try:
        body = client.ping()
    except BlueBubblesError as exc:
        print(f"UNREACHABLE: {exc}")
        return 1
    print(f"OK: {body}")
    return 0


def cmd_webhooks(cfg: Config, args: argparse.Namespace) -> int:
    client = build_client(cfg)

    if args.webhook_action == "list":
        hooks = client.list_webhooks()
        if not hooks:
            print("No webhooks registered.")
            return 0
        for hook in hooks:
            print(f"{hook.get('id')}\t{hook.get('url')}\t{hook.get('events')}")
        return 0

    if args.webhook_action == "register":
        result = client.create_webhook(args.url, events=args.events)
        print(f"Registered: {result}")
        return 0

    if args.webhook_action == "delete":
        client.delete_webhook(args.id)
        print(f"Deleted webhook {args.id}")
        return 0

    print("Unknown webhook action")
    return 2


def cmd_chats(cfg: Config, args: argparse.Namespace) -> int:
    from list_chats import print_chats

    return print_chats(build_client(cfg), limit=args.limit, only_groups=args.groups)


# ---------------------------------------------------------------------------
# sleeper
# ---------------------------------------------------------------------------


def cmd_sleeper(cfg: Config, args: argparse.Namespace) -> int:
    from sleeper import SleeperError, get_client

    client = get_client(cfg.sleeper_data_dir)
    action = args.sleeper_action

    try:
        if action == "leagues":
            user = client.user(args.username)
            if not user:
                # Sleeper answers 200 with a null body for an unknown username,
                # so this is a typo, not an outage.
                print(f"No Sleeper user called {args.username!r}.")
                return 1
            user_id = str(user.get("user_id"))
            print(f"{user.get('display_name')}  user_id={user_id}")

            season = args.season
            if not season:
                state = client.state().value or {}
                season = str(state.get("season") or "")
            leagues = client.user_leagues(user_id, season)

            # In August the current season is often pre-draft, so also offer
            # last year -- that is where the data actually is.
            if not leagues and season.isdigit():
                previous = str(int(season) - 1)
                leagues = client.user_leagues(user_id, previous)
                if leagues:
                    print(f"(nothing in {season}; showing {previous})")
                    season = previous

            if not leagues:
                print(f"No NFL leagues for {args.username} in {season}.")
                return 1
            print(f"\nLeagues in {season}:")
            for league in leagues:
                print(
                    f"  SLEEPER_LEAGUE_ID={league.get('league_id')}"
                    f"   {league.get('name')}"
                    f"   ({league.get('total_rosters')} teams, {league.get('status')})"
                )
            return 0

        if action == "refresh-players":
            count = client.refresh_players_now()
            print(f"Cached {count} players to {cfg.sleeper_data_dir}")
            return 0

        if action == "check":
            from ffleague import load_view

            if not cfg.sleeper_league_id:
                print("SLEEPER_LEAGUE_ID is not set. Run: bot.py sleeper leagues <username>")
                return 2
            view = load_view(
                client,
                cfg.sleeper_league_id,
                follow_previous=cfg.sleeper_follow_previous,
            )
            print(f"League:  {view.name} ({view.season}, {view.status})")
            print(f"Using:   {view.league_id}" + ("  [previous season]" if view.is_previous_season else ""))
            print(f"Week:    {view.week}   playoffs from {view.playoff_week_start}")
            print(f"Teams:   {len(view.managers)}")
            for manager in view.sorted_managers():
                mapped = ""
                for handle, identifier in (cfg.sleeper_user_map or {}).items():
                    if identifier in (manager.user_id, manager.display_name):
                        mapped = f"  <- {handle}"
                print(
                    f"  r{manager.roster_id:<3} {manager.formal_name:<20} "
                    f"{manager.record:<7} {manager.fpts:8.2f} PF"
                    f"  user_id={manager.user_id}{mapped}"
                )
            unmapped = [
                m.formal_name
                for m in view.managers.values()
                if not any(
                    i in (m.user_id, m.display_name) for i in (cfg.sleeper_user_map or {}).values()
                )
            ]
            if unmapped:
                print(f"\nNot in SLEEPER_USER_MAP: {', '.join(unmapped)}")
                print("They can run !link <sleeper name> in the chat instead.")
            print(f"\nSleeper API calls made: {client.request_count}")
            return 0

    except SleeperError as exc:
        print(f"Sleeper error: {exc}")
        return 1

    print("Unknown sleeper action")
    return 2


# ---------------------------------------------------------------------------
# post -- render, test and force the scheduled posts
# ---------------------------------------------------------------------------


def cmd_post(cfg: Config, args: argparse.Namespace) -> int:
    import ffposts
    from ffleague import load_view
    from sleeper import SleeperError, get_client

    client = get_client(cfg.sleeper_data_dir)

    if args.post_action == "check":
        # Show what the scheduler would do right now, and why, without sending.
        scheduler = build_scheduler(cfg, build_client(cfg))
        now = scheduler.now()
        print(f"Now: {now:%Y-%m-%d %H:%M %Z} ({now:%A})")
        print(f"Enabled: {scheduler.enabled}   chat={cfg.sleeper_post_chat or '(unset)'}")
        print(f"Types on: {', '.join(cfg.sleeper_post_types) or '(none)'}")
        try:
            view = load_view(client, cfg.sleeper_league_id, follow_previous=False)
        except SleeperError as exc:
            print(f"League: unavailable ({exc})")
            return 1
        games = client.week_games(view.season, view.week)
        done = sum(1 for g in games if str(g.get("status") or "") in ("complete", "canceled"))
        print(f"League: {view.name} {view.season} week {view.week}")
        print(f"Games this week: {len(games)}, finished {done}")
        print()
        for kind in ffposts.POST_ORDER:
            decision = scheduler.decide(kind, view, games, now)
            posted = scheduler.store.already_posted(view.season, view.week, kind)
            flag = "ALREADY POSTED" if posted else ("WOULD FIRE" if decision.fire else "no")
            print(f"  {kind:<8} {flag:<15} {decision.reason}")
        return 0

    kind = args.kind
    if kind not in ffposts.POST_TYPES:
        print(f"Unknown post type {kind!r}. One of: {', '.join(ffposts.POST_ORDER)}")
        return 2

    try:
        view = load_view(
            client, cfg.sleeper_league_id, follow_previous=cfg.sleeper_follow_previous
        )
    except SleeperError as exc:
        print(f"Sleeper error: {exc}")
        return 1

    week = args.week or view.week
    text = ffposts.build(kind, view, week)
    if not text:
        print(f"[{kind}] would post nothing for week {week} — no data, or nothing worth saying.")
        return 0

    if args.post_action == "render":
        print(text)
        return 0

    # send
    target = args.chat or cfg.sleeper_post_chat
    if not target:
        print("No target chat. Set SLEEPER_POST_CHAT or pass --chat.")
        return 2

    bb = build_client(cfg)
    warn_about_dry_run(cfg)
    try:
        bb.send_text(target, text)
    except BlueBubblesError as exc:
        print(f"Send failed: {exc}")
        return 1

    if args.mark:
        from ffstore import get_store

        get_store(cfg.sleeper_db_path).mark_posted(view.season, week, kind)
    print(f"Sent {kind} for week {week} to {target}")
    return 0


# ---------------------------------------------------------------------------
# entrypoint
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bot.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="mode")

    sub.add_parser("serve", help="run the webhook listener (default)")

    p_summary = sub.add_parser("summary", help="send the weekly recap and exit")
    p_summary.add_argument("--days", type=int, default=None, help="window size (default BB_SUMMARY_DAYS)")
    p_summary.add_argument("--chat", default=None, help="override BB_SUMMARY_CHAT")
    p_summary.add_argument("--dry-run", action="store_true", help="print instead of sending")

    sub.add_parser("ping", help="check the BlueBubbles server")

    p_hooks = sub.add_parser("webhooks", help="manage webhook registrations")
    hooks_sub = p_hooks.add_subparsers(dest="webhook_action", required=True)
    hooks_sub.add_parser("list")
    p_reg = hooks_sub.add_parser("register")
    p_reg.add_argument("--url", required=True, help="e.g. http://192.168.1.50:8080/webhook")
    p_reg.add_argument("--events", nargs="+", default=["new-message"])
    p_del = hooks_sub.add_parser("delete")
    p_del.add_argument("id")

    p_chats = sub.add_parser("chats", help="list chat GUIDs with participant names")
    p_chats.add_argument("--limit", type=int, default=200)
    p_chats.add_argument("--groups", action="store_true", help="only group chats")

    p_sleeper = sub.add_parser("sleeper", help="Sleeper setup helpers")
    sleeper_sub = p_sleeper.add_subparsers(dest="sleeper_action", required=True)
    p_leagues = sleeper_sub.add_parser("leagues", help="find a league ID from a Sleeper username")
    p_leagues.add_argument("username")
    p_leagues.add_argument("--season", default=None, help="defaults to the current NFL season")
    sleeper_sub.add_parser("check", help="load the configured league and print it")
    sleeper_sub.add_parser("refresh-players", help="refresh the cached NFL player file (daily cron)")

    p_post = sub.add_parser("post", help="render, test or send a scheduled post")
    post_sub = p_post.add_subparsers(dest="post_action", required=True)
    p_render = post_sub.add_parser("render", help="print a post without sending it")
    p_render.add_argument("kind", help="lineups | sunday | recap | ahead")
    p_render.add_argument("--week", type=int, default=None)
    p_send = post_sub.add_parser("send", help="send a post now")
    p_send.add_argument("kind", help="lineups | sunday | recap | ahead")
    p_send.add_argument("--week", type=int, default=None)
    p_send.add_argument("--chat", default=None, help="override SLEEPER_POST_CHAT")
    p_send.add_argument("--mark", action="store_true",
                        help="record it, so the scheduler will not repost this week")
    post_sub.add_parser("check", help="show what the scheduler would fire right now, and why")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    mode = args.mode or "serve"

    try:
        # The Sleeper helpers talk to Sleeper, not to the Mac. Requiring
        # BB_SERVER_URL to look up a league ID would make the setup order
        # circular for no reason.
        cfg = load_config(require_server=(mode not in ("sleeper", "post")))
    except ConfigError as exc:
        logging_setup.configure("INFO", "text")
        log.error(str(exc))
        return 2

    logging_setup.configure(cfg.log_level, cfg.log_format)

    handlers = {
        "serve": cmd_serve,
        "summary": cmd_summary,
        "ping": cmd_ping,
        "webhooks": cmd_webhooks,
        "chats": cmd_chats,
        "sleeper": cmd_sleeper,
        "post": cmd_post,
    }
    try:
        return handlers[mode](cfg, args)
    except BlueBubblesError as exc:
        log.error("BlueBubbles request failed", extra={"fields": {"error": str(exc)}})
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
