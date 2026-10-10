# Deploying Money Hub on an always-on Linux box

Target: a Debian/Ubuntu host that is on your Tailscale network. The app has
no auth by design, so the Tailscale boundary **is** the boundary: it binds to
the host's Tailscale IPv4 and nothing else. Never put it on a LAN or public
address.

Everything below is done by `deploy/install.sh`. It is idempotent: the first
run installs, every later run is an upgrade (pull, pip, migrate, restart).

## First install

```bash
curl -fsSL https://raw.githubusercontent.com/TheHunter75771/MoneyHub/main/deploy/install.sh | sudo bash
```

What it does:

| Step | Result |
|---|---|
| packages | `git`, `python3`, `python3-venv` |
| user | `moneyhub` system user, no shell, home `/opt/moneyhub` |
| code | clone of `main` at `/opt/moneyhub`, owned by `moneyhub` |
| venv | `/opt/moneyhub/.venv` built with the host's `python3` |
| config | `/opt/moneyhub/.env` from `deploy/env.example`, mode 600, bind host set to `tailscale ip -4` |
| database | `alembic upgrade head` on a fresh `/opt/moneyhub/data/moneyhub.db` |
| units | `moneyhub-web.service`, `moneyhub-sync.timer` (09:00 Eastern), `moneyhub-backup.timer` (03:00 Eastern) |
| firewall | if ufw is active: allow tcp/8710 **on `tailscale0` only** |

Then from any device on the tailnet: `http://<tailscale-ip>:8710`. On a
phone, Safari/Chrome → Add to Home Screen.

A fresh database has no accounts. Create them in **Data & Rules → Accounts**
in the UI (or `POST /api/accounts`), then either upload bank CSV/PDF exports
there or set up live sync below.

## Desktop app (optional)

`desktop/install-desktop.sh` installs a native window (WebKitGTK, no
browser chrome) and an app-menu entry for the current user. It needs the
GTK3 + WebKit2GTK 4.1 bindings (`python3-gobject webkit2gtk4.1` on Fedora,
`python3-gi gir1.2-gtk-3.0 gir1.2-webkit2-4.1` on Ubuntu):

```bash
git clone https://github.com/TheHunter75771/MoneyHub.git ~/src/MoneyHub
~/src/MoneyHub/desktop/install-desktop.sh http://<tailscale-ip>:8710
```

The URL lives in `~/.config/moneyhub/app.conf`; re-run the installer with a
new URL if the host moves.

## Secrets

`sudoedit /opt/moneyhub/.env`, then `sudo systemctl restart moneyhub-web`.

- **SimpleFIN**: paste the setup token as `SIMPLEFIN_SETUP_TOKEN`, run
  `sudo systemctl start moneyhub-sync`, and the first sync exchanges it for
  `SIMPLEFIN_ACCESS_URL` (appended to `.env`). Delete the setup-token line
  afterwards; it is single-use. Map accounts with
  `sudo -u moneyhub bash -c 'cd /opt/moneyhub && .venv/bin/moneyhub sync --map SFIN_ID="Account Name"'`
  when the sync reports `UNMAPPED` rows.
- **ntfy**: `NTFY_TOPIC` is the secret; generate it, don't pick it.
- **Notion**: `NOTION_TOKEN` + `NOTION_PARENT_PAGE_ID` for the one-way projection.

Never copy the real `.env` anywhere else: not into a backup folder on another
drive, not into chat, not into Notion. The [2026-08 exposure] came from a
well-meant `.env.bak` on an unencrypted volume.

[2026-08 exposure]: GAPS.md

## Day to day

```bash
systemctl status moneyhub-web moneyhub-sync.timer moneyhub-backup.timer
journalctl -u moneyhub-web -f          # app log
journalctl -u moneyhub-sync -n 50      # last sync
sudo systemctl start moneyhub-sync     # sync now
sudo systemctl start moneyhub-backup   # snapshot now
ls -la /var/backups/moneyhub           # newest 14 snapshots
```

## Upgrade

Re-run the installer. It fast-forwards `main`, reinstalls the package,
runs migrations, and restarts the web service.

## Backups and restore

The database is WAL mode. **A plain `cp`/`scp` of `moneyhub.db` omits every
write still in `moneyhub.db-wal`** and produces a valid-looking database
missing the newest data; that only surfaces on restore. The backup timer
uses `moneyhub snapshot`, which goes through SQLite's online backup API and
writes a single integrity-checked file per night.

The snapshots live on the same disk as the database. Copy them off-box
too (another machine on the tailnet, or an encrypted cloud bucket). Pulling
`/var/backups/moneyhub/` with `rsync` over Tailscale from a second machine
is enough; the files are complete and consistent.

Restore:

```bash
sudo systemctl stop moneyhub-web moneyhub-sync.timer
sudo -u moneyhub cp /var/backups/moneyhub/moneyhub-YYYYMMDD-HHMMSS.db /opt/moneyhub/data/moneyhub.db
sudo rm -f /opt/moneyhub/data/moneyhub.db-wal /opt/moneyhub/data/moneyhub.db-shm
sudo systemctl start moneyhub-web moneyhub-sync.timer
```

## Known host gotchas

- **Tailscale boot race.** The bind address does not exist until `tailscaled`
  is up. The web unit orders after it and restarts every 5 s until the bind
  succeeds, so a reboot self-heals.
- **Host clock in UTC.** Both timers carry an explicit `America/New_York`
  zone, so they fire at Eastern wall-clock time regardless.
- **CGNAT-range Wi-Fi.** Some managed networks (Boingo barracks Wi-Fi, for
  one) hand out `100.100.x.x` addresses inside Tailscale's `100.64.0.0/10`.
  Tailnet peers outside that /16 are unaffected; MagicDNS's resolver
  (`100.100.100.100`) is not, so pin a host route
  (`ip route add 100.100.100.100/32 dev tailscale0`) or just use IPs.
