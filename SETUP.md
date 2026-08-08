# BlueBubbles bot — setup and operations

A Python service that listens for BlueBubbles webhooks, answers chat commands,
and sends a weekly recap from cron.

```
iMessage ──▶ BlueBubbles Server (Intel MacBook Pro, macOS 14.4)
                      │  POST /webhook  (new-message)
                      ▼
             bot.py serve   (Debian 13 LXC on Proxmox)
                      │  POST /api/v1/message/text  (~800ms, AppleScript)
                      ▼
             BlueBubbles Server ──▶ iMessage
```

Both directions cross the network, so both directions can break independently.
The troubleshooting section below works outward along that path.

## Files

| File | What it is |
| --- | --- |
| `bot.py` | Entry point. `serve`, `summary`, `ping`, `webhooks`, `chats`, `sleeper`, `post`. |
| `app.py` | Flask app, webhook intake, worker queue, the guard order. |
| `registry.py` | Command registry + dispatch + exception isolation. |
| `commands.py` | The general commands, and `!help`. Add yours here. |
| `parsing.py` | Defensive webhook/message parsing. Edit this if fields are missing. |
| `echo_guard.py` | The three loop guards. Read this before changing reply behaviour. |
| `bbclient.py` | BlueBubbles REST client. Rate-limits sends. |
| `summary.py` | Weekly recap aggregation and rendering. |
| `history.py` | Per-chat rolling history — the seam for a future LLM handler. |
| `list_chats.py` | Prints chat GUIDs with participant names. |
| `bluebubbles-bot.service` | systemd unit, `Restart=always`. |
| **Fantasy football** | |
| `sleeper.py` | Sleeper API client. All the caching lives here. |
| `ffleague.py` | The joins (roster→user→matchup) and derived stats. |
| `ffcommands.py` | The fantasy commands. Add yours here. |
| `ffroast.py` | Fact mining and templates for `!roast` / `!cope` / `!curse`. |
| `ffposts.py` | The four scheduled posts. Pure builders, no sending. |
| `ffschedule.py` | When those posts fire. Triggers off game state, not cron. |
| `ffstore.py` | SQLite: nicknames, fines, polls, links, what has been posted. |
| `ffformat.py` | iMessage text helpers. Read the header before formatting anything. |
| `tests/` | `pytest`. Mocked Sleeper responses; never hits the live API. |

---

## 1. Create the LXC (on the Proxmox host)

Debian 13 template, unprivileged, 2 cores / 1 GB is plenty.

```bash
pveam update
pveam available --section system | grep debian-13
pveam download local debian-13-standard_13.0-1_amd64.tar.zst   # match the listed name

pct create 120 local:vztmpl/debian-13-standard_13.0-1_amd64.tar.zst \
  --hostname bbbot \
  --cores 2 --memory 1024 --swap 512 \
  --rootfs local-lvm:8 \
  --net0 name=eth0,bridge=vmbr0,ip=dhcp \
  --unprivileged 1 \
  --features nesting=1 \
  --onboot 1 \
  --password

pct start 120
pct enter 120
```

### Optional but recommended: put the LXC on Tailscale

The config prefers the Mac's Tailscale address because it survives DHCP and
VLAN changes. For that to work the container has to be on the tailnet too — and
as a bonus, the Mac can then reach the bot at a stable Tailscale name for the
webhook, so *neither* direction depends on LAN addressing.

An unprivileged LXC needs `/dev/net/tun` passed through. On the **Proxmox host**:

```bash
pct stop 120
cat >> /etc/pve/lxc/120.conf <<'EOF'
lxc.cgroup2.devices.allow: c 10:200 rwm
lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file
EOF
pct start 120
```

Then inside the container:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up
tailscale status          # note this container's name, e.g. bbbot.your-tailnet.ts.net
```

## 2. Install the bot (inside the LXC)

```bash
apt update && apt install -y python3 python3-venv python3-pip git

adduser --system --group --home /opt/bluebubbles-bot bbbot
install -d -o bbbot -g bbbot /opt/bluebubbles-bot

# copy the repo in (git clone, scp, or pct push from the host)
cd /opt/bluebubbles-bot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
chown -R bbbot:bbbot /opt/bluebubbles-bot
```

## 3. Configure

```bash
cp .env.example .env
chmod 600 .env
chown bbbot:bbbot .env
$EDITOR .env
```

At minimum set `BB_SERVER_URL` and `BB_PASSWORD`. Check the server answers:

```bash
.venv/bin/python bot.py ping
# OK: {'status': 200, 'message': 'Success', 'data': 'Pong!'}
```

A 401 with `Missing server password!` means `BB_PASSWORD` is wrong.

### Fill in the allowlist

**The bot replies nowhere until you do this.** That is deliberate: without an
allowlist it would answer in every conversation on the Apple ID, including
group chats with other people in them.

```bash
.venv/bin/python list_chats.py
```

```
TYPE   NAME                             GUID
------ -------------------------------- ----------------------------------------
group  Family                           iMessage;+;chat483920184756192837
dm     Alex Chen                        iMessage;-;+15551234567
```

Copy the GUIDs you want into `.env`:

```ini
BB_ALLOWED_CHATS=iMessage;+;chat483920184756192837,iMessage;-;+15551234567
BB_SUMMARY_CHAT=iMessage;-;+15551234567
```

Useful variants: `list_chats.py --groups`, `--search family`, and `--env`
(prints a ready-to-paste `BB_ALLOWED_CHATS=` line).

Once the bot is running you can also send `!chatinfo` in any chat and it will
reply with that chat's GUID — but only if that chat is already allowlisted, so
`list_chats.py` is how you bootstrap.

### Try it without sending anything

`BB_DRY_RUN=true` logs each intended send instead of calling `message/text`.
Reads still hit the real server, so the whole path is exercised except delivery
— useful for a first run against your live BlueBubbles server.

```bash
BB_DRY_RUN=true .venv/bin/python bot.py summary
# ... INFO bbclient: DRY RUN: not sending | chat_guid=... message="bloq-bot: Weekly recap ..."
```

## 4. Register the webhook

The URL must be reachable **from the Mac**. Use the container's Tailscale name
if you set that up, otherwise its LAN IP (`ip -4 addr show eth0`).

```bash
.venv/bin/python bot.py webhooks register --url http://bbbot.your-tailnet.ts.net:8080/webhook
.venv/bin/python bot.py webhooks list
```

The path must match `BB_WEBHOOK_PATH` in `.env`. To remove a stale one:

```bash
.venv/bin/python bot.py webhooks delete <id>
```

## 5. Install the service

```bash
cp /opt/bluebubbles-bot/bluebubbles-bot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now bluebubbles-bot
systemctl status bluebubbles-bot
journalctl -u bluebubbles-bot -f
```

Verify locally:

```bash
curl -s localhost:8080/health | python3 -m json.tool
curl -s 'localhost:8080/health?deep=1' | python3 -m json.tool   # also pings the Mac
```

`?deep=1` round-trips to the Mac, so use the plain form for any monitoring
probe you run frequently.

## 6. Weekly summary from cron

Test it first:

```bash
sudo -u bbbot /opt/bluebubbles-bot/.venv/bin/python /opt/bluebubbles-bot/bot.py summary --dry-run
```

Then, Mondays at 9am (`crontab -e -u bbbot`):

```cron
0 9 * * 1 /usr/bin/flock -n /tmp/bb-summary.lock /opt/bluebubbles-bot/.venv/bin/python /opt/bluebubbles-bot/bot.py summary >> /var/log/bb-summary.log 2>&1
```

`flock` keeps a slow run from overlapping the next one. Cron's environment is
nearly empty, but that is fine: config is read from `.env` next to `bot.py`.
(If you keep the env file elsewhere, set `BB_ENV_FILE=/path/to/.env` in the
crontab.)

Useful flags: `--days 14`, `--chat <guid>`, `--dry-run`.

## 7. Confirm it works

In an allowlisted chat — **including from your own number**, see the loop guards
below:

- `!ping` → `bloq-bot: pong` within a couple of seconds
- `!help` → the command list, generated from the registry
- `!chatinfo` → this chat's GUID
- `!weather brooklyn` → current conditions
- `!help` → **three** commands and a pointer at `!help all`. That is deliberate:
  a menu of thirty-eight is a menu nobody reads. Everything still works.
- `!frobnicate` → nothing at all (unknown commands are silently dropped)
- `!help ping` → a reply that itself starts with `!ping`, and does **not**
  trigger another reply. This is the loop hazard worth checking by hand.

---

## 8. Fantasy football (Sleeper)

Sleeper's API is read-only, unauthenticated and needs no key. The only thing to
configure is which league.

### Find your league ID

```bash
sudo -u bbbot /opt/bluebubbles-bot/.venv/bin/python /opt/bluebubbles-bot/bot.py \
    sleeper leagues <your-sleeper-username>
```

```
bloqus  user_id=1127649699546255360

Leagues in 2026:
  SLEEPER_LEAGUE_ID=1389694421427036160   WashedUp FF 2025   (12 teams, pre_draft)
```

Put that line in `.env`. This command does not need `BB_SERVER_URL`, so you can
run it before the rest of the bot is configured.

### Map people to Sleeper accounts

`!roast me`, `!record <name>` and `!cope` need to know which Sleeper account
belongs to the person who sent the message. Nothing infers this. Two ways:

```bash
python bot.py sleeper check     # prints every manager with their user_id
```

then either set it centrally —

```ini
SLEEPER_USER_MAP=+15551234567=bloqus,+15559876543=barryyung
```

— or let people do it themselves in the chat, which writes to the database:

```
!link bloqus
```

`sleeper check` also lists everyone *not* yet mapped, so it doubles as the
"who still needs to run `!link`" report. Everything else works unmapped; only
the commands that mean "me" need it.

### The player file, from cron

`/players/nfl` is ~15MB and Sleeper asks that you call it at most once a day.
The bot caches a trimmed copy to disk (1.8MB), loads it at startup, and
refreshes in the background when it goes stale — so cron is belt and braces
rather than required. If you want it deterministic:

```cron
30 4 * * * /usr/bin/flock -n /tmp/bb-players.lock /opt/bluebubbles-bot/.venv/bin/python /opt/bluebubbles-bot/bot.py sleeper refresh-players >> /var/log/bb-players.log 2>&1
```

### Between seasons

A Sleeper league is a **per-season object**. From roughly February to your
draft, the ID in `.env` points at a `pre_draft` league: twelve empty rosters,
no matchups, nothing to say. Rather than have every command answer "nothing
yet" for six months, the bot follows `previous_league_id` and labels the reply:

```
🏈 WashedUp FF 2025 · Standings
(showing 2025 — this season has not drafted)
🥇 Ryanmd211 · 9-5 · 1668.5 PF · W1
```

Set `SLEEPER_FOLLOW_PREVIOUS=false` to turn that off. Two commands deliberately
refuse to answer in this mode rather than mix seasons — `!injuries` and the
per-manager half of `!bye` — because a player's team and injury status are
*today's*, and painting those onto last season's roster produces confident
nonsense. `!roster` drops the team and injury annotations for the same reason.

Update `SLEEPER_LEAGUE_ID` when the new league is created; nothing breaks if
you forget, you just keep seeing last season.

### Try it

```
!week          what the bot thinks the season and week are
!league        which league it is pointed at, and how many people are mapped
!standings     record, points for, streak
!scores        live scoring, with a freshness caveat
!roast bru32   an insult built from real data
!help all      every command, grouped
!help fantasy  the fantasy ones with descriptions
```

### What the bot advertises

`!help` lists three commands — `!scores`, `!standings`, `!roast` — and a line
pointing at `!help all`. The other thirty-five still work and are still
documented; they are just not in the shop window. A short help message people
read beats a complete one they skip.

All three work with no setup: `!roast` with no argument roasts you if your
handle is mapped, and somebody at random if it is not, rather than replying to
a first-time user with configuration instructions.

To change which three, edit `HEADLINE_COMMANDS` in `commands.py`.

### Caching, and the rate limit

Sleeper allows ~1000 requests/minute per IP. In steady state a command costs
**zero to two** requests; ten people spamming `!scores` produce one HTTP call,
because each cache key has its own lock.

| Data | TTL | Notes |
| --- | --- | --- |
| `/state/nfl` | 1 hour | The week ticks over once a week. |
| League, users | 10 min | |
| Rosters | 5 min | Moves after waivers clear. |
| Matchups, current week | 30 s | Scores move. |
| Matchups, finished week | forever | Persisted to disk; a finished week is finished. |
| Transactions | 2 min | |
| Player file | 24 h | Trimmed to 1.8MB, persisted, refreshed in the background. |
| NFL schedule | 3 days | Persisted. Only used for byes. |

The expensive case is the first `!standings` after a cold start, which scans
the season to compute streaks. `SLEEPER_PREWARM=true` (the default) does that
in a background thread at startup, so nobody waits on it.

**When Sleeper is unreachable, the bot serves the last good data and says so**
rather than failing or quietly pretending it is live:

```
⚠️ Sleeper is not responding — scores as of 12 min ago
```

---

## 9. Scheduled posts

The bot mostly speaks without being asked. Four posts a week, to one chat,
off by default until you have looked at them.

```ini
SLEEPER_POSTS_ENABLED=true
SLEEPER_POST_CHAT=iMessage;+;chat1234567890123456789
SLEEPER_POST_TYPES=lineups,sunday,recap,ahead
SLEEPER_TIMEZONE=America/New_York
```

| Post | Fires when | Contains |
| --- | --- | --- |
| `lineups` | The main slate day, before most games kick off | **Only** the managers with an empty slot, a starter on bye, or a starter OUT |
| `sunday` | The afternoon slate is done, the week is not | Closest game, worst beating, who is up, what is left |
| `recap` | Every game in the week has finished | Final scores, high, low, blowout, closest, worst bench call |
| `ahead` | Wednesday morning, once the week has rolled over | Game of the week, matchups, overnight waiver results |

### Why there is no cron entry here

NFL game times move — Thursday, Saturday in December, 9:30am London kickoffs,
Monday doubleheaders, flexed Sunday nights. A fixed "Monday 11pm" recap
eventually fires during the fourth quarter of a Monday nightcap.

So the triggers read game state. The schedule endpoint gives every game a
`status`, and the recap simply waits until all of them are `complete` (plus a
30-minute settle so Sleeper's final scoring lands). A week that ends on a
Saturday recaps on Saturday; nothing is hardcoded to a day. The scheduler polls
every five minutes and consults the clock for exactly two things: not posting
at 4am, and knowing whether it is Wednesday.

### Silence is a feature

The lineup check posts **nothing** when everyone is clean. A weekly
"everything looks fine!" is noise, and noise is how a bot gets muted. Likewise,
if Sleeper is down or the week has no data, a post is skipped rather than sent
half-rendered — and skipping does not consume the week's one allowed fire, so a
transient outage delays a post rather than losing it.

### Preview, before you turn it on

Waiting until Monday night to test a Monday-night post is not workable.

```
!preview recap            in the chat
!preview recap 14         a specific week
```

```bash
python bot.py post render recap --week 14   # print it
python bot.py post check                    # what would fire right now, and why
python bot.py post send recap --chat <guid> # send one by hand
```

`post check` is the one to reach for when a post did not appear:

```
Now: 2025-10-06 23:40 EDT (Monday)
Enabled: True   chat=iMessage;+;chat123
League: WashedUp FF 2025 week 5
Games this week: 14, finished 13

  lineups  no              not the slate day (2025-10-05)
  sunday   no              the week is already over
  recap    no              1 games still to finish
  ahead    no              not Wednesday
```

### Volume

Four posts a week, at most one per post type, at most one per five-minute tick.
Every fire is written to SQLite **before** the send, keyed by
(season, week, kind), so a restart mid-week reposts nothing.

Rate limiting, cooldowns and a quiet-hours mute are deliberately not built.
When you want them, `Scheduler.publish()` is the single place every scheduled
message leaves the process — gate it there rather than in the triggers.

### Tests

```bash
.venv/bin/python -m pytest tests/ -q
```

447 tests, no network. Sleeper responses are stubbed at the `requests.Session`
level, so the real caching, stale-serving and joins are all exercised. Every
command is asserted to reply sensibly in three hostile conditions: Sleeper
down, league empty, and a name that matches nobody.

The scheduler tests cover the two failure modes that matter: firing a recap
while a game is still in progress, and reposting after a restart. Both are
asserted directly, with the clock injected rather than waited for.

---

## Loop guards

Bot replies come from the same Apple ID you type from, so every outgoing message
arrives back as a webhook with `isFromMe: true`. Dropping all of those would
also drop your own commands, so the discriminator is the command prefix, and
three independent layers keep the loop closed. Full rationale is in the
`echo_guard.py` docstring.

**Layer 1 — the bot prefix.** Every outgoing message is stamped with
`BB_BOT_PREFIX` (default `bloq-bot: `) at the send choke point, so summaries and
error replies get it too, once at the top rather than per line. Incoming
messages carrying that prefix are dropped regardless of who sent them. This also
pushes any command prefix into mid-string, where it no longer parses as a
command — which is what makes `!help ping` safe.

**Layer 2 — `isFromMe` plus the command prefix.** A message from your own Apple
ID that starts with the command prefix is you issuing a command, and is
processed (counted as `commands_from_me`). Anything else from your own Apple ID
is ordinary chatter and is dropped.

**Layer 3 — the recent-sends tracker.** Remembers what the bot sent in the last
two minutes, keyed by chat, and rejects an `isFromMe` message that matches. It
compares with the bot prefix normalised *away*, so it still fires if the prefix
is altered or stripped somewhere in transit. That independence is the point —
layer 3 must not assume layer 1 worked. It also matches on message GUID when
one is available.

Layer 1 is a visible, typeable string, so a person *can* type `bloq-bot: !ping`
by hand and have it ignored. That is the trade for making automated messages
legible to everyone in the chat, and it is why layer 3 exists.

Setting `BB_BOT_PREFIX=` (empty) disables layer 1 and the visible marker; layers
2 and 3 still apply and startup warns. A `BB_BOT_PREFIX` that starts with
`BB_COMMAND_PREFIX` is refused at startup, since every bot message would then
look like a command.

If layer 3 ever fires, it logs at WARNING — that means layer 1 did not catch
something it should have, and is worth investigating.

---

## Adding a command

One decorator, one function, in `commands.py`. No change to dispatch.

```python
@command("standup", help="Today's standup order.", usage="standup [team]", aliases=("su",))
def cmd_standup(args: str, ctx: CommandContext) -> str | None:
    # ctx: chat_guid, sender_name, sender_address, text, is_group,
    #      command, message_guid, client, config, history
    return "Alex, then Sam, then you."
```

`category=` is optional and only affects which `!help` section it appears
under.

### Adding a *fantasy* command

Same thing in `ffcommands.py`, with one extra decorator that hands you a ready
`LeagueView` and absorbs every Sleeper failure:

```python
@command("mypoints", help="Your points for.", category=LEAGUE)
@league_command()
def cmd_mypoints(args: str, ctx: CommandContext, view: LeagueView) -> str:
    manager = _resolve(args, ctx, view)      # name, nickname, team name, or "me"
    return f"{manager.name}: {pts(manager.fpts)}"
```

`@league_command()` handles the league being unreachable, unconfigured, not yet
drafted, and a name that matched nobody or several people. Pass
`needs_games=False` if the command still makes sense before the draft.

Read the header of `ffformat.py` before formatting output. The short version:
iMessage is a **proportional** font, so space-padded columns do not line up —
separate fields with `DOT` and keep lines short instead.

Return a string to reply, `None` to stay silent. Raising is safe — dispatch
logs the traceback and replies with a one-liner; the service stays up and other
commands are unaffected. `!help` picks the new command up automatically.

Do not add `BB_BOT_PREFIX` yourself — the send path stamps it (and would not
double it if you did). A handler is free to return text starting with the
command prefix; the prefix guard makes that safe.

Restart after editing: `systemctl restart bluebubbles-bot`.

## Later: the LLM seam

Deliberately not built yet. When you want it:

`history.ChatHistory` already records every allowlisted message *and* every bot
reply, keyed by chat GUID, as a bounded per-chat deque. A handler gets it as
`ctx.history`:

```python
@command("ask", help="Ask the assistant.")
def cmd_ask(args: str, ctx: CommandContext) -> str:
    turns = ctx.history.recent(ctx.chat_guid, limit=20)   # [Turn(role, sender, text, ts)]
    return call_your_model(args, turns)
```

Nothing in `app.py`, `registry.py`, or `bbclient.py` needs to change. Swapping
the in-memory store for SQLite is a change to `history.py` alone.

---

## Troubleshooting

Work outward along the path. Each step tells you whether to look further out.

### 0. Is the bot even running?

```bash
systemctl status bluebubbles-bot
curl -s localhost:8080/health
```

`ConfigError` at startup means `.env` is missing or unreadable by `bbbot` —
check ownership and `EnvironmentFile=` in the unit.

### 1. Did the webhook arrive?

Turn on the raw payload dump. This is the main debugging tool.

```bash
# in .env
BB_LOG_LEVEL=DEBUG
systemctl restart bluebubbles-bot
journalctl -u bluebubbles-bot -f
```

Send a message in an allowlisted chat. You want a `raw webhook payload` line.

**Nothing at all?** The event never reached the container:

- Is the webhook registered, with the right URL and path?
  `bot.py webhooks list` — the path must match `BB_WEBHOOK_PATH`.
- Can the Mac reach the container? From the **Mac**:
  ```bash
  curl -v -X POST http://<container-address>:8080/webhook \
       -H 'Content-Type: application/json' -d '{"type":"new-message","data":{}}'
  ```
  A 400 back is a success for this test — it means HTTP got through.
- Firewall between them? Different VLAN? This is exactly what the Tailscale
  setup in step 1 is for.
- **Is the Mac actually up and logged in?** FileVault means nothing runs after a
  reboot until someone logs in at the console. If the bot has gone silent with
  no errors anywhere, check this first. `bot.py ping` from the container tells
  you in one second.

### 2. Did it parse?

With DEBUG on you get a `parsed message` line right after the raw dump, showing
`chat_guid`, `is_from_me`, `sender`, and the text.

**Fields empty or wrong?** The payload shape does not match what `parsing.py`
expects. The raw dump immediately above it is ground truth — compare the two and
adjust `parsing.py`. Every field goes through `_first(data, "camelCase",
"snake_case", ...)`, so accommodating a different spelling is usually adding one
string to one call.

A `could not find a chat GUID` warning means `chats[0].guid` was not where we
looked; that is the one field the bot cannot work without.

### 3. Was it dropped on purpose?

`/health` keeps counters:

```bash
curl -s localhost:8080/health | python3 -m json.tool
```

```json
"stats": {
  "received": 42,
  "dropped_from_me": 12,
  "dropped_bot_prefix": 5,
  "dropped_own_echo": 0,
  "dropped_not_allowed": 8,
  "dropped_duplicate": 0,
  "commands_from_me": 3,
  "commands_run": 5,
  "replies_sent": 5,
  "send_failures": 0
}
```

- `dropped_from_me` — your own Apple ID saying something that is not a command.
  Expected. Your own *commands* are processed and counted as
  `commands_from_me`, so if a command you typed shows up here instead, check
  `BB_COMMAND_PREFIX` and that the message really starts with it.
- `dropped_bot_prefix` — a message carrying `BB_BOT_PREFIX`. Normally the bot's
  own replies coming back. If a human's message lands here, they typed
  something that starts with the bot prefix.
- `dropped_own_echo` — the recent-sends tracker fired. This also logs at
  WARNING, because it means the bot prefix guard missed something: check
  whether the prefix is surviving the round trip.
- `dropped_not_allowed` — chat is not in `BB_ALLOWED_CHATS`. The INFO log line
  prints the GUID; paste it into `.env` and restart.
- `dropped_unparsed` — see step 2.

### 4. Was the command recognised?

Look for `command handled` (with `elapsed_ms` and `replied`) or
`unknown command, dropping`.

- Unknown commands are dropped silently by design. Set
  `BB_REPLY_ON_UNKNOWN_COMMAND=true` while testing if you want an answer.
- Check `BB_COMMAND_PREFIX` matches what you are typing.
- A `command handler raised` line with a traceback means that one command is
  broken — the service and every other command are fine.

### 5. Did the send succeed?

You want `sent message` with `chars` and `elapsed_ms` (expect ~800–1500ms).

**`failed to send reply`?**

- `bot.py ping` — is the server reachable at all?
- Try a send by hand:
  ```bash
  curl -X POST "$BB_SERVER_URL/api/v1/message/text?password=$BB_PASSWORD" \
    -H 'Content-Type: application/json' \
    -d '{"chatGuid":"iMessage;-;+15551234567","message":"test","tempGuid":"manual-1","method":"apple-script"}'
  ```
- Wrong `chatGuid` is the usual cause. Re-check with `list_chats.py`; the format
  is `iMessage;-;+15551234567` for DMs and `iMessage;+;chat<digits>` for groups.
- On the Mac, check that Automation and Full Disk Access are still granted —
  macOS silently revokes these after some updates.

### Known-harmless noise

`iMessage Private API is not enabled!` appears in the **BlueBubbles server log**
before every send. The Private API needs SIP disabled, which was declined. It
costs tapbacks, typing indicators, read receipts, and message editing — plain
text replies work fine. Endpoints that require it (e.g.
`/handle/<address>/focus`) return that string as an error. Ignore it.

### Other symptoms

| Symptom | Cause |
| --- | --- |
| Bot replies to itself in a loop | Stop the service first. All three loop guards failed: check that `BB_BOT_PREFIX` is non-empty and appears on outgoing messages, and that incoming echoes still carry it (`BB_LOG_LEVEL=DEBUG` shows the raw text). `dropped_own_echo` firing at WARNING is the early sign. |
| Your own commands are ignored | Expected only if they do not start with `BB_COMMAND_PREFIX`, or if they start with `BB_BOT_PREFIX`. Check `commands_from_me` on `/health`. |
| Duplicate replies | The same webhook delivered twice; `dropped_duplicate` should absorb it. If it does not, the message `guid` is missing from the payload. |
| Replies are slow / queue backs up | Sends are ~800ms and serialised. Check `queue_depth` on `/health`. Do not lower `BB_SEND_MIN_INTERVAL` below ~0.8. |
| `summary` is empty | `message/query` returned nothing for the window. Check `BB_SUMMARY_SOURCE_CHATS` GUIDs, and try `--days 30 --dry-run`. |
| `summary` is slow | It pages through up to `BB_SUMMARY_MAX_MESSAGES`. Lower it, or narrow the source chats. |
| Everything silent, no errors anywhere | The Mac. FileVault, sleep, or a logged-out console. Check it first. |

### Fantasy symptoms

Note that the Mac is not involved in any of these — Sleeper is a separate
dependency that fails separately. `!ping` working tells you nothing about it;
`!week` is the equivalent check.

| Symptom | Cause |
| --- | --- |
| "No Sleeper league is configured" | `SLEEPER_LEAGUE_ID` is unset. `bot.py sleeper leagues <username>`. |
| "There is no Sleeper league with ID …" | Typo, or you used the league *name*. It is a long numeric string. |
| "Sleeper is not responding right now" | Genuinely Sleeper. `curl -s https://api.sleeper.app/v1/state/nfl`. The bot recovers on its own. |
| "showing 2025 — this season has not drafted" | Working as intended; see *Between seasons*. Update `SLEEPER_LEAGUE_ID` after your draft. |
| Player names show as numbers (`11560`) | The player file has not loaded yet. It downloads in the background on first use; the next command will have names. If it persists, check `SLEEPER_DATA_DIR` is writable — under systemd it must be inside `ReadWritePaths=`. |
| "Player data is still loading" | Same cause. `bot.py sleeper refresh-players` forces it. |
| `!roast` gives generic burns | There is nothing unflattering in the data — a short season, or a manager who is simply fine. It falls back deliberately rather than inventing a fact. |
| `!roast me` says it does not know you | Your handle is not mapped. `!link <sleeper name>`, or fill in `SLEEPER_USER_MAP`. |
| Nicknames vanish on restart | The SQLite file is not writable. Check `SLEEPER_DB_PATH` and `ReadWritePaths=`. |
| `!bye` shows teams but nobody's players | Expected out of season; player→team is current-day data and would be wrong against last year's rosters. |
| Scores look stale mid-game | They can be up to 30s old by design. If the reply carries "⚠️ Sleeper is not responding", that is the real answer. |
| Every Sleeper call takes ~80s, then works | Broken IPv6. `api.sleeper.app` resolves to AAAA records first; if the container has an IPv6 address but no route, each new connection stalls until it falls back to v4. Test with `curl -4` vs plain `curl`. Fix the route, or drop `precedence ::ffff:0:0/96 100` into `/etc/gai.conf` to prefer v4. |

### Scheduled posts did not appear

`python bot.py post check` answers this in one screen — it prints the current
time in the configured zone, whether each post is due, and the reason. Common
causes:

| Symptom | Cause |
| --- | --- |
| Nothing ever posts | `SLEEPER_POSTS_ENABLED` is false, or `SLEEPER_POST_CHAT` is empty. Both are required; the log warns at startup if only one is set. |
| Nothing posts, config looks right | It is the off-season. The scheduler only runs when `/state/nfl` says `regular` or `post`. |
| No lineup check on Sunday | Working as intended if everyone's lineup is clean — that post is silent by design. `!preview lineups` confirms. |
| The recap never fires | A game is stuck not-`complete` on the schedule endpoint. `post check` prints "N games still to finish". |
| The recap fired but the scores were wrong by a fraction | Stat corrections after the settle window. The 30-minute delay covers the whistle, not Wednesday's revisions. |
| Week-ahead posts on Tuesday evening | `SLEEPER_TIMEZONE` is unset or wrong, so "Wednesday" is being judged in UTC. |
| A post fired twice | Should be impossible — it is recorded in SQLite before sending. Check that `SLEEPER_DB_PATH` is on persistent storage and not being recreated. |
| One post type is annoying | Remove it from `SLEEPER_POST_TYPES`; the others keep running. |
