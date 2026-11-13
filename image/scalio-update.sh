#!/usr/bin/env bash
# Scalio Installer aktualisieren: holt die neuste get.sh und führt sie aus (neustes Installer-Image, Caddy, neuer
# Anmelde-Code). Die installierten Apps und ihre Daten bleiben. Ohne Internet läuft die get.sh vom ISO.
#   sudo scalio-update            Kanal aus /opt/scalio-image/channel (vom ISO), sonst public
set -euo pipefail
URL="https://raw.githubusercontent.com/Scalio-Cloud/scalio-selfhosted/main/get.sh"
DIR=/opt/scalio-image
[ "$(id -u)" -eq 0 ] || { echo "Fehler: bitte mit sudo starten: sudo scalio-update" >&2; exit 1; }
CHANNEL="${SCALIO_CHANNEL:-$(cat "$DIR/channel" 2>/dev/null || echo public)}"
NEW="$(mktemp)"
trap 'rm -f "$NEW"' EXIT
if curl -fsSL --retry 3 --max-time 60 "$URL" -o "$NEW" && head -n 1 "$NEW" | grep -q '^#!/usr/bin/env bash' && bash -n "$NEW"; then
  install -m 0755 "$NEW" "$DIR/get.sh"
  echo "Neuste get.sh geladen."
else
  echo "Hinweis: neuste get.sh nicht erreichbar, nehme die vorhandene ($DIR/get.sh)."
fi
SCALIO_CHANNEL="$CHANNEL" bash "$DIR/get.sh"
