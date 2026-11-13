#!/usr/bin/env bash
# Neuer Anmelde-Code für den Scalio Installer (einmal gültig, 30 Minuten): sudo scalio-code
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Fehler: bitte mit sudo starten: sudo scalio-code" >&2; exit 1; }
docker exec scalio-setup python -m scalio_setup code
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo "Im Browser öffnen: https://${IP:-<IP>}"
