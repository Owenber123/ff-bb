#!/usr/bin/env bash
#
# Deploy the bot to the LXC.
#
#   ./deploy.sh              deploy
#   ./deploy.sh --dry-run    show what would change, touch nothing
#   ./deploy.sh --rollback   restore the previous release
#   ./deploy.sh --logs       tail the service log
#   ./deploy.sh --status     health check only
#
# Never touches .env, the data directory, or the venv contents. Snapshots the
# running release first and restores it automatically if the new one fails to
# come up healthy.

set -euo pipefail

HOST="${BOT_HOST:-bloq-bot}"
REMOTE_DIR="${BOT_REMOTE_DIR:-/opt/bluebubbles-bot}"
SERVICE="${BOT_SERVICE:-bluebubbles-bot}"
RUN_USER="${BOT_USER:-bbbot}"
LOCAL_DIR="${BOT_LOCAL_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
HEALTH_PORT="${BOT_HEALTH_PORT:-8080}"
PREV_DIR="${REMOTE_DIR}.prev"

# Never sync these. .env holds live credentials; data/ holds the SQLite
# database and the cached Sleeper player file; .venv is built on the target.
EXCLUDES=(
  --exclude '.env'
  --exclude '.venv/'
  --exclude 'venv/'
  --exclude 'data/'
  --exclude '__pycache__/'
  --exclude '*.pyc'
  --exclude '.git/'
  --exclude '.pytest_cache/'
  --exclude '*.log'
  --exclude '.env.backup*'
  --exclude '.claude/'
  --exclude 'deploy.sh'
)

c_red=$'\033[31m'; c_grn=$'\033[32m'; c_ylw=$'\033[33m'; c_dim=$'\033[2m'; c_off=$'\033[0m'
say()  { printf '%s==>%s %s\n' "$c_grn" "$c_off" "$*"; }
warn() { printf '%s==>%s %s\n' "$c_ylw" "$c_off" "$*"; }
die()  { printf '%s==>%s %s\n' "$c_red" "$c_off" "$*" >&2; exit 1; }
run()  { ssh "$HOST" "$@"; }

health() {
  # Returns the health JSON, or empty on failure. Retries because the service
  # takes a moment to bind after a restart.
  local tries="${1:-10}" out
  for _ in $(seq "$tries"); do
    out=$(run "curl -sf --max-time 3 localhost:${HEALTH_PORT}/health" 2>/dev/null || true)
    if [[ -n "$out" ]]; then printf '%s' "$out"; return 0; fi
    sleep 1
  done
  return 1
}

show_health() {
  local json
  if ! json=$(health "${1:-10}"); then
    warn "no response from /health"
    return 1
  fi
  run "python3 -c '
import json,sys
d = json.load(sys.stdin)
s = d.get(\"stats\", {})
print(f\"    status      {d.get(\"status\")}\")
print(f\"    uptime      {d.get(\"uptime_seconds\", 0):.0f}s\")
print(f\"    dry_run     {d.get(\"dry_run\")}\")
print(f\"    allowlist   {d.get(\"allowlisted_chats\")} chat(s)\")
print(f\"    received    {s.get(\"received\", 0)}\")
print(f\"    replies     {s.get(\"replies_sent\", 0)}\")
print(f\"    failures    {s.get(\"send_failures\", 0)}\")
'" <<<"$json"
}

preflight() {
  [[ -f "$LOCAL_DIR/bot.py" ]] || die "no bot.py in $LOCAL_DIR — wrong directory?"
  ssh -o ConnectTimeout=5 "$HOST" true 2>/dev/null || die "cannot ssh to $HOST"
  command -v rsync >/dev/null || die "rsync not installed locally"
  run "command -v rsync >/dev/null" || {
    warn "rsync missing on $HOST, installing"
    run "apt-get update -qq && apt-get install -y -qq rsync" || die "could not install rsync"
  }
}

do_rollback() {
  run "test -d '$PREV_DIR'" || die "no previous release at $PREV_DIR"
  say "restoring previous release"
  # Keep the live .env and data — only the code goes back.
  run "rsync -a --delete ${EXCLUDES[*]} '$PREV_DIR/' '$REMOTE_DIR/' \
       && chown -R $RUN_USER:$RUN_USER '$REMOTE_DIR' \
       && systemctl restart $SERVICE"
  if show_health 15; then say "rolled back and healthy"; else die "rollback did not come up — check journalctl"; fi
}

do_deploy() {
  local dry="$1"

  preflight

  if [[ "$dry" == "yes" ]]; then
    say "changes that would be applied"
    rsync -azn --delete --itemize-changes "${EXCLUDES[@]}" \
      "$LOCAL_DIR/" "$HOST:$REMOTE_DIR/" | grep -v '^$' || echo "    (no changes)"
    return 0
  fi

  say "backing up .env"
  run "cd '$REMOTE_DIR' && cp .env '.env.backup-\$(date +%Y%m%d-%H%M%S)' && ls -t .env.backup-* | tail -n +6 | xargs -r rm --"

  say "snapshotting current release"
  run "rm -rf '$PREV_DIR' && cp -a '$REMOTE_DIR' '$PREV_DIR'"

  say "syncing code"
  rsync -az --delete --info=stats1 "${EXCLUDES[@]}" "$LOCAL_DIR/" "$HOST:$REMOTE_DIR/"

  say "checking dependencies"
  # Rebuild the venv if it is missing or its shebangs point somewhere stale —
  # that breaks pip silently and is easy to miss.
  run "cd '$REMOTE_DIR'
    if [ ! -x .venv/bin/python ] || ! head -1 .venv/bin/pip 2>/dev/null | grep -q '$REMOTE_DIR/.venv'; then
      echo '    rebuilding venv'
      rm -rf .venv && python3 -m venv .venv
    fi
    .venv/bin/pip install -q -r requirements.txt"

  say "clearing stale bytecode"
  run "find '$REMOTE_DIR' -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null; true"

  say "fixing ownership"
  run "chown -R $RUN_USER:$RUN_USER '$REMOTE_DIR' && chmod 600 '$REMOTE_DIR/.env'"

  # A syntax error should fail here, not after the service is already down.
  say "compiling"
  run "cd '$REMOTE_DIR' && .venv/bin/python -m compileall -q *.py" \
    || { warn "compile failed — not restarting"; exit 1; }

  say "restarting"
  run "systemctl restart $SERVICE"

  if show_health 15; then
    say "deployed"
  else
    warn "service did not come up healthy"
    run "journalctl -u $SERVICE -n 30 --no-pager" || true
    printf '\n%srolling back%s\n' "$c_ylw" "$c_off"
    do_rollback
    exit 1
  fi
}

case "${1:-}" in
  --dry-run|-n) do_deploy yes ;;
  --rollback)   do_rollback ;;
  --logs|-f)    run "journalctl -u $SERVICE -f" ;;
  --status|-s)  show_health 3 || exit 1 ;;
  --help|-h)    sed -n '3,12p' "${BASH_SOURCE[0]}" | sed 's/^# \?//' ;;
  "")           do_deploy no ;;
  *)            die "unknown option: $1 (try --help)" ;;
esac
