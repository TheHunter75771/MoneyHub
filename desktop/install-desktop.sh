#!/usr/bin/env bash
# Install the Money Hub desktop launcher for the current user (no root).
#
#   desktop/install-desktop.sh http://100.x.y.z:8710
#
# Needs GTK3 + WebKit2GTK 4.1 GObject introspection:
#   Fedora:  sudo dnf install python3-gobject webkit2gtk4.1
#   Ubuntu:  sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-webkit2-4.1
set -euo pipefail

url=${1:-}
[[ -n $url ]] || { echo "usage: $0 http://<tailscale-ip>:8710" >&2; exit 1; }

here=$(cd "$(dirname "$0")" && pwd)
bin=$HOME/.local/bin
apps=$HOME/.local/share/applications
conf=${XDG_CONFIG_HOME:-$HOME/.config}/moneyhub/app.conf

python3 - <<'PY' || { echo "missing GTK/WebKit bindings — see the header of this script" >&2; exit 1; }
import gi
gi.require_version("Gtk", "3.0"); gi.require_version("WebKit2", "4.1")
from gi.repository import Gtk, WebKit2  # noqa
PY

install -d "$bin" "$apps" "$(dirname "$conf")"
install -m 755 "$here/moneyhub-desktop" "$bin/moneyhub-desktop"
install -m 644 "$here/moneyhub.desktop" "$apps/moneyhub.desktop"
if [[ -f $conf ]]; then
  sed -i "s|^url *=.*|url = $url|" "$conf"
else
  printf '[app]\nurl = %s\nwidth = 1280\nheight = 860\n' "$url" > "$conf"
fi
command -v update-desktop-database >/dev/null && update-desktop-database "$apps" || true

case ":$PATH:" in *":$bin:"*) ;; *) echo "note: $bin is not on PATH; the menu entry still works" ;; esac
echo "installed: search your app menu for 'Money Hub' (URL: $url)"
