#!/usr/bin/env bash
# First boot of the Scalio VM image (VHDX/OVA, selfhost/H8): there is no default account. If cloud-init did not create
# a usable one (Proxmox cloud-init, VMware vApp "password"/"public-keys", Azure), this asks on the console for the
# keyboard, an administrator (name and password) and optionally SSH keys from GitHub or Launchpad. Runs once.
#   SCALIO_ACCOUNT_CHECK=1 scalio-account.sh   only answers whether an account is there (exit 0) or not (exit 1)
set -uo pipefail
DONE=/var/lib/scalio/account-done

usable() {
  local u
  while IFS=: read -r u _ uid _ _ home _; do
    [ "$uid" -ge 1000 ] && [ "$uid" -lt 60000 ] || continue
    [ -s "$home/.ssh/authorized_keys" ] && return 0
    [ "$(passwd -S "$u" 2>/dev/null | awk '{print $2}')" = P ] && return 0
  done < /etc/passwd
  return 1
}

if [ "${SCALIO_ACCOUNT_CHECK:-}" = 1 ]; then usable; exit; fi
install -d -m 0755 /var/lib/scalio
# cloud-init (Proxmox, VMware vApp) has created its account by now: the unit runs after its init and config stages.
if usable; then touch "$DONE"; exit 0; fi

printf '\n  Scalio: Einrichtung des Servers\n  ================================\n\n'
printf '  Dieser Server hat noch kein Konto. Legen Sie jetzt das Administrator-Konto an.\n\n'
read -r -p "  Tastatur (ch, de, fr, it, at, us, gb) [ch]: " KB
KB=${KB:-ch}
case "$KB" in ch|de|fr|it|at|us|gb) ;; *) KB=ch ;; esac
sed -i "s/^XKBLAYOUT=.*/XKBLAYOUT=\"$KB\"/" /etc/default/keyboard 2>/dev/null || true
setupcon -k --force >/dev/null 2>&1 || loadkeys "$KB" >/dev/null 2>&1 || true

while :; do
  read -r -p "  Benutzername (Kleinbuchstaben, z. B. admin): " NAME
  if printf '%s' "$NAME" | grep -Eq '^[a-z][a-z0-9_-]{1,31}$' && ! id "$NAME" >/dev/null 2>&1 && [ "$NAME" != root ]; then break; fi
  printf '  Bitte 2 bis 32 Zeichen: Kleinbuchstaben, Ziffern, - oder _, beginnend mit einem Buchstaben (nicht vergeben).\n'
done
while :; do
  read -r -s -p "  Passwort (mindestens 12 Zeichen): " PW1; printf '\n'
  read -r -s -p "  Passwort wiederholen: " PW2; printf '\n'
  if [ "${#PW1}" -ge 12 ] && [ "$PW1" = "$PW2" ]; then break; fi
  printf '  Die Passwörter sind zu kurz oder nicht gleich.\n'
done
useradd -m -s /bin/bash -G sudo "$NAME"
printf '%s:%s\n' "$NAME" "$PW1" | chpasswd
unset PW1 PW2

printf '\n  SSH ist nur mit Schlüssel möglich. Schlüssel von GitHub oder Launchpad übernehmen?\n'
read -r -p "  GitHub-Benutzer (leer lassen zum Überspringen): " GH
if [ -n "$GH" ] && printf '%s' "$GH" | grep -Eq '^[A-Za-z0-9-]{1,39}$'; then
  sudo -u "$NAME" ssh-import-id "gh:$GH" >/dev/null 2>&1 && printf '  SSH-Schlüssel von GitHub übernommen.\n' \
    || printf '  Keine Schlüssel gefunden (später: ssh-import-id gh:<benutzer>).\n'
fi
touch "$DONE"
printf '\n  Konto %s angelegt. Der Scalio Installer wird im Hintergrund eingerichtet;\n' "$NAME"
printf '  Adresse und Einmal-Code erscheinen gleich über der Anmeldung.\n\n'
sleep 3
