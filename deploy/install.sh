#!/usr/bin/env bash
# Money Hub installer / upgrader for Ubuntu or Debian hosts.
#
#   curl -fsSL https://raw.githubusercontent.com/TheHunter75771/MoneyHub/main/deploy/install.sh | sudo bash
#   # or, from a checkout:  sudo deploy/install.sh
#
# Idempotent: first run installs, later runs upgrade in place (git pull,
# pip install, migrate, restart). Nothing here touches the database except
# `alembic upgrade head`, and the nightly snapshot timer is installed before
# the web service is started.
#
# Layout it produces:
#   /opt/moneyhub            git checkout, owned by the `moneyhub` system user
#   /opt/moneyhub/.venv      virtualenv built with the host python3
#   /opt/moneyhub/data/      SQLite database (gitignored)
#   /opt/moneyhub/.env       runtime config + secrets, mode 600
#   /var/backups/moneyhub/   nightly WAL-safe snapshots, newest 14 kept
#   /etc/systemd/system/     moneyhub-web.service, -sync.timer, -backup.timer
set -euo pipefail

APP_DIR=${MONEYHUB_DIR:-/opt/moneyhub}
APP_USER=${MONEYHUB_USER:-moneyhub}
REPO=${MONEYHUB_REPO:-https://github.com/TheHunter75771/MoneyHub.git}
BRANCH=${MONEYHUB_BRANCH:-main}
BACKUP_DIR=${MONEYHUB_BACKUP_DIR:-/var/backups/moneyhub}
PORT=${MONEYHUB_WEB_PORT:-8710}

log() { printf '\n==> %s\n' "$*"; }
die() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run as root (sudo)"
command -v apt-get >/dev/null || die "this installer targets apt-based hosts"

log "Packages"
if command -v git >/dev/null && python3 -c 'import venv, ensurepip' 2>/dev/null; then
  echo "   git + python3-venv already present"
else
  export DEBIAN_FRONTEND=noninteractive
  apt-get install -y -qq --no-install-recommends git python3 python3-venv ca-certificates >/dev/null
fi

log "System user $APP_USER"
if ! id -u "$APP_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
fi
as_app() { sudo -u "$APP_USER" -H "$@"; }

log "Code at $APP_DIR (branch $BRANCH)"
if [[ -d $APP_DIR/.git ]]; then
  as_app git -C "$APP_DIR" fetch --quiet origin "$BRANCH"
  as_app git -C "$APP_DIR" checkout --quiet "$BRANCH"
  as_app git -C "$APP_DIR" merge --quiet --ff-only "origin/$BRANCH"
else
  install -d -o "$APP_USER" -g "$APP_USER" -m 750 "$APP_DIR"
  as_app git clone --quiet --branch "$BRANCH" "$REPO" "$APP_DIR"
fi
as_app git -C "$APP_DIR" log -1 --format='   at %h %s'

log "Virtualenv"
[[ -x $APP_DIR/.venv/bin/python ]] || as_app python3 -m venv "$APP_DIR/.venv"
as_app "$APP_DIR/.venv/bin/pip" install --quiet --no-cache-dir --upgrade pip
as_app "$APP_DIR/.venv/bin/pip" install --quiet --no-cache-dir -e "$APP_DIR"

log "Data, backups, config"
install -d -o "$APP_USER" -g "$APP_USER" -m 750 "$APP_DIR/data" "$BACKUP_DIR"
if [[ ! -f $APP_DIR/.env ]]; then
  ts_ip=$(tailscale ip -4 2>/dev/null | head -1 || true)
  if [[ -z $ts_ip ]]; then
    echo "   WARNING: no Tailscale IPv4 found; binding to 127.0.0.1 only." >&2
    echo "   Install Tailscale, then set MONEYHUB_WEB_HOST in $APP_DIR/.env." >&2
    ts_ip=127.0.0.1
  fi
  sed -e "s/^MONEYHUB_WEB_HOST=.*/MONEYHUB_WEB_HOST=$ts_ip/" \
      -e "s/^MONEYHUB_WEB_PORT=.*/MONEYHUB_WEB_PORT=$PORT/" \
      "$APP_DIR/deploy/env.example" > "$APP_DIR/.env"
  chown "$APP_USER:$APP_USER" "$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"
  echo "   wrote $APP_DIR/.env (bind $ts_ip:$PORT) — add secrets there later"
else
  echo "   keeping existing $APP_DIR/.env"
fi

log "Database migrations"
(cd "$APP_DIR" && as_app "$APP_DIR/.venv/bin/alembic" upgrade head 2>&1 | sed 's/^/   /')

if [[ ! -d /run/systemd/system ]]; then
  echo "   systemd not running; skipping unit install (container or chroot?)"
  exit 0
fi

log "systemd units"
install -m 644 "$APP_DIR"/deploy/moneyhub-*.service "$APP_DIR"/deploy/moneyhub-*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --quiet moneyhub-backup.timer moneyhub-sync.timer moneyhub-web.service
systemctl restart moneyhub-backup.timer moneyhub-sync.timer moneyhub-web.service

if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q '^Status: active'; then
  log "Firewall"
  ufw allow in on tailscale0 to any port "$PORT" proto tcp comment 'Money Hub (tailnet only)' >/dev/null
  echo "   ufw: allowed tcp/$PORT on tailscale0 only"
fi

log "Done"
host=$(sed -n 's/^MONEYHUB_WEB_HOST=//p' "$APP_DIR/.env")
cat <<MSG
   web:     http://$host:$PORT      (tailnet devices only)
   status:  systemctl status moneyhub-web moneyhub-sync.timer moneyhub-backup.timer
   logs:    journalctl -u moneyhub-web -f
   secrets: sudoedit $APP_DIR/.env   then   systemctl restart moneyhub-web
   sync now: systemctl start moneyhub-sync.service   (then journalctl -u moneyhub-sync)
   snapshot: systemctl start moneyhub-backup.service  (writes to $BACKUP_DIR)
MSG
