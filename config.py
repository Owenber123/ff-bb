"""Configuration, loaded entirely from environment variables.

Nothing in here should ever contain a secret literal. See .env.example.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # dotenv is optional; systemd can inject the env directly
    load_dotenv = None


BASE_DIR = Path(__file__).resolve().parent


def _load_dotenv_once() -> None:
    if load_dotenv is None:
        return
    # Explicit path beats CWD, since systemd starts us from an arbitrary place.
    env_path = os.environ.get("BB_ENV_FILE") or str(BASE_DIR / ".env")
    if Path(env_path).is_file():
        load_dotenv(env_path, override=False)


def _str(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _str_raw(name: str, default: str = "") -> str:
    """Like _str but preserves surrounding whitespace.

    Used for BB_BOT_PREFIX, where the trailing space in "bloq-bot: " is
    meaningful. An explicitly empty value stays empty (disabling the feature)
    rather than falling back to the default.
    """
    value = os.environ.get(name)
    return default if value is None else value


def _int(name: str, default: int) -> int:
    raw = _str(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from None


def _float(name: str, default: float) -> float:
    raw = _str(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from None


def _bool(name: str, default: bool = False) -> bool:
    raw = _str(name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _csv(name: str) -> list[str]:
    raw = _str(name)
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _pairs(name: str) -> dict[str, str]:
    """Parse ``+15551234567=bloqus,+15559876543=barryyung`` into a dict.

    Keys are normalised the same way an incoming webhook handle is, so
    "+1 (555) 123-4567" in .env matches "+15551234567" on the wire. Malformed
    entries are logged over rather than failing startup -- a typo in one
    mapping should not stop the bot from serving.
    """
    raw = _str(name)
    if not raw:
        return {}

    from ffformat import normalize_handle

    mapping: dict[str, str] = {}
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        handle, sep, value = chunk.partition("=")
        if not sep or not handle.strip() or not value.strip():
            continue
        key = normalize_handle(handle)
        if key:
            mapping[key] = value.strip()
    return mapping


class ConfigError(RuntimeError):
    """Raised when the environment is missing something we cannot invent."""


@dataclass
class Config:
    # --- BlueBubbles server -------------------------------------------------
    server_url: str
    password: str
    http_timeout: float

    # --- Listener -----------------------------------------------------------
    listen_host: str
    listen_port: int
    webhook_path: str

    # --- Behaviour ----------------------------------------------------------
    command_prefix: str
    bot_prefix: str = ""
    dry_run: bool = False
    allowed_chats: list[str] = field(default_factory=list)
    allow_all_chats: bool = False
    reply_on_unknown_command: bool = False

    # --- Summary ------------------------------------------------------------
    summary_chat: str = ""
    summary_source_chats: list[str] = field(default_factory=list)
    summary_days: int = 7
    summary_top_words: int = 10
    summary_max_messages: int = 5000

    # --- Sleeper / fantasy football -----------------------------------------
    sleeper_league_id: str = ""
    # Chat handle -> Sleeper user_id or display name. Never inferred; !link
    # writes overrides into the database.
    sleeper_user_map: dict[str, str] = field(default_factory=dict)
    # Between seasons the configured league is pre-draft and empty, so fall
    # back to previous_league_id rather than answering "nothing yet" until
    # September. Replies say which season they are showing.
    sleeper_follow_previous: bool = True
    sleeper_data_dir: str = ""
    sleeper_db_path: str = ""
    sleeper_prewarm: bool = True

    # --- Scheduled posts ----------------------------------------------------
    # Where uninvited messages go. Separate from the allowlist on purpose: the
    # bot may answer commands in several chats but should only broadcast to one.
    sleeper_post_chat: str = ""
    sleeper_posts_enabled: bool = False
    # Per-type toggles, so the group can kill the one it finds annoying without
    # losing the rest.
    sleeper_post_types: tuple[str, ...] = ()
    sleeper_timezone: str = "America/New_York"

    # --- Misc ---------------------------------------------------------------
    send_min_interval: float = 1.0
    history_size: int = 50
    worker_threads: int = 2
    default_weather_location: str = ""
    log_level: str = "INFO"
    log_format: str = "text"

    @property
    def api_base(self) -> str:
        return f"{self.server_url.rstrip('/')}/api/v1"

    def is_chat_allowed(self, chat_guid: str) -> bool:
        """Default-deny. An empty allowlist means the bot replies nowhere."""
        if self.allow_all_chats:
            return True
        return chat_guid in self.allowed_chats

    def summary_chats(self) -> list[str]:
        """Which chats the weekly recap reads from. Empty means 'all chats'."""
        if self.summary_source_chats:
            return list(self.summary_source_chats)
        if self.allow_all_chats:
            return []
        return list(self.allowed_chats)


def load_config(require_server: bool = True) -> Config:
    _load_dotenv_once()

    server_url = _str("BB_SERVER_URL")
    password = _str("BB_PASSWORD")

    if require_server:
        if not server_url:
            raise ConfigError(
                "BB_SERVER_URL is not set. Point it at your BlueBubbles server, "
                "e.g. http://macbook.tail1234.ts.net:1234"
            )
        if not password:
            raise ConfigError(
                "BB_PASSWORD is not set. This is the BlueBubbles server password "
                "(sent as the ?password= query parameter)."
            )

    webhook_path = _str("BB_WEBHOOK_PATH", "/webhook")
    if not webhook_path.startswith("/"):
        webhook_path = "/" + webhook_path

    prefix = _str("BB_COMMAND_PREFIX", "!")
    if not prefix:
        raise ConfigError("BB_COMMAND_PREFIX must not be empty")

    bot_prefix = _str_raw("BB_BOT_PREFIX", "bloq-bot: ")
    if bot_prefix.strip().startswith(prefix):
        # Every outgoing message would then begin with the command prefix.
        # The bot-prefix drop layer would still catch it, but the config is
        # wrong and the failure would be baffling, so refuse it outright.
        raise ConfigError(
            f"BB_BOT_PREFIX ({bot_prefix!r}) must not start with BB_COMMAND_PREFIX "
            f"({prefix!r}) -- every bot message would look like a command."
        )

    # Caches and the SQLite file both live here. It must be inside the systemd
    # unit's ReadWritePaths= -- everything else in the container is read-only.
    data_dir = _str("SLEEPER_DATA_DIR") or str(BASE_DIR / "data")

    # Which scheduled posts are on. Unset means all of them; an explicit empty
    # value means none, which is a different thing and worth honouring.
    from ffposts import POST_ORDER

    raw_types = os.environ.get("SLEEPER_POST_TYPES")
    if raw_types is None:
        post_types = tuple(POST_ORDER)
    else:
        wanted = [p.strip().lower() for p in raw_types.split(",") if p.strip()]
        unknown = [p for p in wanted if p not in POST_ORDER]
        if unknown:
            raise ConfigError(
                f"SLEEPER_POST_TYPES has unknown post type(s): {', '.join(unknown)}. "
                f"Valid values are: {', '.join(POST_ORDER)}"
            )
        post_types = tuple(wanted)

    cfg = Config(
        server_url=server_url,
        password=password,
        http_timeout=_float("BB_HTTP_TIMEOUT", 20.0),
        listen_host=_str("BB_LISTEN_HOST", "0.0.0.0"),
        listen_port=_int("BB_LISTEN_PORT", 8080),
        webhook_path=webhook_path,
        command_prefix=prefix,
        bot_prefix=bot_prefix,
        dry_run=_bool("BB_DRY_RUN", False),
        allowed_chats=_csv("BB_ALLOWED_CHATS"),
        allow_all_chats=_bool("BB_ALLOW_ALL_CHATS", False),
        reply_on_unknown_command=_bool("BB_REPLY_ON_UNKNOWN_COMMAND", False),
        summary_chat=_str("BB_SUMMARY_CHAT"),
        summary_source_chats=_csv("BB_SUMMARY_SOURCE_CHATS"),
        summary_days=_int("BB_SUMMARY_DAYS", 7),
        summary_top_words=_int("BB_SUMMARY_TOP_WORDS", 10),
        summary_max_messages=_int("BB_SUMMARY_MAX_MESSAGES", 5000),
        sleeper_league_id=_str("SLEEPER_LEAGUE_ID"),
        sleeper_user_map=_pairs("SLEEPER_USER_MAP"),
        sleeper_follow_previous=_bool("SLEEPER_FOLLOW_PREVIOUS", True),
        sleeper_data_dir=data_dir,
        sleeper_db_path=_str("SLEEPER_DB_PATH") or str(Path(data_dir) / "bot.db"),
        sleeper_prewarm=_bool("SLEEPER_PREWARM", True),
        sleeper_post_chat=_str("SLEEPER_POST_CHAT"),
        sleeper_posts_enabled=_bool("SLEEPER_POSTS_ENABLED", False),
        sleeper_post_types=post_types,
        sleeper_timezone=_str("SLEEPER_TIMEZONE", "America/New_York"),
        send_min_interval=_float("BB_SEND_MIN_INTERVAL", 1.0),
        history_size=_int("BB_HISTORY_SIZE", 50),
        worker_threads=_int("BB_WORKER_THREADS", 2),
        default_weather_location=_str("BB_DEFAULT_WEATHER_LOCATION"),
        log_level=_str("BB_LOG_LEVEL", "INFO").upper(),
        log_format=_str("BB_LOG_FORMAT", "text").lower(),
    )
    return cfg
