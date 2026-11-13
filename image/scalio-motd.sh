#!/bin/sh
# Nach der Anmeldung (Konsole und SSH): Adresse des Scalio Installers und, solange sich noch niemand angemeldet hat,
# der Einmal-Code. pam_motd führt das als root aus.
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
CODE=$(cat /opt/scalio/setup/first-code 2>/dev/null)
printf '\n  Scalio Installer:   https://%s  (Zertifikatswarnung beim ersten Aufruf bestätigen)\n' "${IP:-<IP>}"
if [ -n "$CODE" ] && ! grep -qs '"claimed": *true' /opt/scalio/setup/state.json; then
  printf '  Einmal-Code:        %s\n' "$CODE"
elif [ -f /opt/scalio/setup/state.json ]; then
  printf '  Neuer Anmelde-Code: sudo scalio-code\n'
else
  printf '  Wird eingerichtet … (Protokoll: /var/log/scalio-firstboot.log)\n'
fi
printf '  Aktualisieren:      sudo scalio-update\n\n'
