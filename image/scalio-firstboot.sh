#!/usr/bin/env bash
# First boot of the Scalio ISO: runs get.sh from the ISO (Docker, /opt/scalio, Caddy, Scalio Installer) and writes
# the address and the one-time code to the login screen of the console. Runs again on the next boot until it worked.
set -euo pipefail
LOG=/var/log/scalio-firstboot.log
ISSUE=/etc/issue.d/scalio.issue
install -d /etc/issue.d
printf '\nScalio wird eingerichtet … (Protokoll: %s)\n\n' "$LOG" > "$ISSUE"
for i in 1 2 3 4 5; do
  if bash /opt/scalio-image/get.sh >>"$LOG" 2>&1; then break; fi
  [ "$i" = 5 ] && { printf '\nScalio: Einrichtung fehlgeschlagen, siehe %s\n\n' "$LOG" > "$ISSUE"; exit 1; }
  sleep $((i * 20))
done
CODE=$(cat /opt/scalio/setup/first-code 2>/dev/null || true)
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
{
  printf '\n  Scalio Installer läuft.\n'
  printf '  Im Browser öffnen:  https://%s  (Zertifikatswarnung beim ersten Aufruf bestätigen)\n' "${IP:-<IP>}"
  [ -n "$CODE" ] && printf '  Einmal-Code:        %s\n' "$CODE"
  printf '\n'
} > "$ISSUE"
systemctl disable scalio-firstboot.service >/dev/null 2>&1 || true
