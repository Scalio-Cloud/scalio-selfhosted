#!/usr/bin/env bash
# Scalio Installer installieren: curl -fsSL https://get.scaliocloud.com | sudo bash
#
# Prüft das System (Ubuntu 24.04/26.04, Debian 12/13), installiert Docker aus dem offiziellen Docker-Repository,
# legt /opt/scalio an und startet Caddy und den Scalio Installer. Alles Weitere läuft im Browser. Das Script ändert
# nichts ausser Docker, /opt/scalio, der Firewall (nur wenn ufw aktiv ist: 80 und 443 öffnen) und den automatischen
# Sicherheits-Updates (unattended-upgrades, Neustart nur im Wartungsfenster, das der Installer einstellt; selfhost/H11).
# Es legt keinen Fernzugang für Scalio an.
#
# Variablen (optional): SCALIO_SETUP_IMAGE (Image von Setup), SCALIO_CADDY_IMAGE (Image von Caddy),
# SCALIO_SKIP_DOCKER=1 (Docker nicht installieren), SCALIO_SKIP_UPDATES=1 (keine automatischen Updates), SCALIO_ROOT (Standard /opt/scalio), SCALIO_CHANNEL (public, Standard; dev und rc nur für Scalio-Laborserver: neuster
# Dev-Build oder Release Candidate jeder App, private Images, vorher «docker login ghcr.io»).
set -euo pipefail

CHANNEL="${SCALIO_CHANNEL:-public}"
case "$CHANNEL" in
  public) DEFAULT_IMAGE="ghcr.io/scalio-cloud/scalio-setup:latest"; DEFAULT_CADDY="ghcr.io/scalio-cloud/scalio-setup-caddy:latest" ;;
  dev) DEFAULT_IMAGE="ghcr.io/scalio-cloud/scalio-setup-dev:latest"; DEFAULT_CADDY="ghcr.io/scalio-cloud/scalio-setup-dev-caddy:latest" ;;
  rc) DEFAULT_IMAGE="ghcr.io/scalio-cloud/scalio-setup-dev:rc"; DEFAULT_CADDY="ghcr.io/scalio-cloud/scalio-setup-dev-caddy:rc" ;;
  *) printf 'Fehler: SCALIO_CHANNEL muss public, dev oder rc sein, nicht %s\n' "$CHANNEL" >&2; exit 1 ;;
esac
SETUP_IMAGE="${SCALIO_SETUP_IMAGE:-$DEFAULT_IMAGE}"
# Caddy mit den DNS-Modulen für «Let's Encrypt über DNS»; fehlt es (ältere Version), läuft das normale caddy:2.
CADDY_IMAGE="${SCALIO_CADDY_IMAGE:-$DEFAULT_CADDY}"
ROOT="${SCALIO_ROOT:-/opt/scalio}"
NETWORK="scalio-proxy"

say() { printf '\033[1m%s\033[0m\n' "$*"; }
fail() { printf 'Fehler: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || fail "bitte mit sudo starten: curl -fsSL https://get.scaliocloud.com | sudo bash"
[ "$(uname -s)" = Linux ] || fail "nur Linux wird unterstützt"
case "$(uname -m)" in x86_64|aarch64) ;; *) fail "Prozessor $(uname -m) wird nicht unterstützt (x86_64 oder arm64)" ;; esac

. /etc/os-release
case "${ID}:${VERSION_ID}" in
  ubuntu:24.04|ubuntu:26.04|debian:12|debian:13) ;;
  *) fail "${PRETTY_NAME:-unbekanntes System} wird nicht unterstützt (Ubuntu 24.04/26.04 oder Debian 12/13)" ;;
esac

MEM_MB=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
[ "$MEM_MB" -ge 3500 ] || say "Hinweis: nur ${MEM_MB} MB RAM. Für eine App sind 4 GB empfohlen."

install_docker() {
  say "Docker installieren (offizielles Repository)"
  apt-get update -qq
  apt-get install -y -qq ca-certificates curl >/dev/null
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL "https://download.docker.com/linux/${ID}/gpg" -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/${ID} ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin >/dev/null
  systemctl enable --now docker >/dev/null
}

if [ "${SCALIO_SKIP_DOCKER:-}" != 1 ]; then
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    say "Docker ist schon installiert: $(docker --version)"
  else
    install_docker
  fi
fi

say "Ordner ${ROOT} anlegen"
install -d -m 0700 "$ROOT" "$ROOT/setup" "$ROOT/apps" "$ROOT/caddy" "$ROOT/caddy/sites" "$ROOT/caddy/data" "$ROOT/caddy/certs"
# Der Installer selbst: https über die IP-Adressen dieses Servers mit Caddys eigener Zertifizierungsstelle. Ein
# Browser schickt bei einer IP keinen Servernamen (SNI), deshalb default_sni. Port 80 leitet auf https um (das
# Anmelde-Cookie gilt nur über https). Die Admin-API hört nur auf einem Socket im Caddy-Container (der Installer lädt
# damit neue Apps per docker exec, ohne Verbindungen zu trennen). Die Datei wird bei jedem Lauf neu geschrieben, die
# Apps liegen in sites/.
IPS=$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9]+(\.[0-9]+){3}$' || true)
FIRST_IP=$(printf '%s\n' "$IPS" | head -n 1)
SITES="https://localhost"
for a in $IPS; do SITES="$SITES, https://$a"; done
{
  echo "# Vom Scalio Installer verwaltet (get.sh schreibt sie bei jedem Lauf neu). Eine Datei pro App in sites/."
  echo "{"
  echo "	admin unix//run/caddy-admin.sock"
  [ -n "$FIRST_IP" ] && echo "	default_sni $FIRST_IP"
  echo "}"
  echo
  echo "http://:80 {"
  echo "	redir https://{host}{uri} 308"
  echo "}"
  echo
  echo "$SITES {"
  echo "	tls internal"
  echo "	reverse_proxy scalio-setup:8080"
  echo "}"
  echo
  echo "import sites/*.caddy"
} > "$ROOT/caddy/Caddyfile"
chmod 600 "$ROOT/caddy/Caddyfile"

if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  say "Firewall: 80 und 443 öffnen"
  ufw allow 80/tcp >/dev/null
  ufw allow 443/tcp >/dev/null
fi

docker network inspect "$NETWORK" >/dev/null 2>&1 || docker network create "$NETWORK" >/dev/null

# Sicherheits-Updates und Wartungsfenster (selfhost/H11): unattended-upgrades installiert die Updates jede Nacht, startet
# aber nie selbst neu. scalio-maintenance läuft stündlich, schreibt den Zustand für den Installer
# ($ROOT/setup/host-status.json) und startet den Server nur neu, wenn ein Update das verlangt und das Fenster aus
# $ROOT/setup/maintenance.json (Standard Sonntag 03:00) gerade offen ist.
if [ "${SCALIO_SKIP_UPDATES:-}" != 1 ]; then
  say "Sicherheits-Updates und Wartungsfenster einrichten"
  if ! dpkg -s unattended-upgrades >/dev/null 2>&1; then
    apt-get update -qq && apt-get install -y -qq unattended-upgrades >/dev/null
  fi
  cat > /etc/apt/apt.conf.d/52scalio-unattended <<'APT'
// Scalio (selfhost/H11): security updates every night; the restart happens in the window of scalio-maintenance.
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
Unattended-Upgrade::Automatic-Reboot "false";
APT
  cat > /usr/local/sbin/scalio-maintenance <<'MAINT'
#!/usr/bin/env bash
# Scalio: state of the OS updates for the Installer and the restart in the maintenance window (selfhost/H11).
set -euo pipefail
ROOT="${SCALIO_ROOT:-/opt/scalio}"
CFG="$ROOT/setup/maintenance.json"
OUT="$ROOT/setup/host-status.json"
DAY=sun
HOUR=3
if [ -f "$CFG" ]; then
  DAY=$(sed -n 's/.*"reboot_day": *"\([a-z]*\)".*/\1/p' "$CFG" | head -n 1)
  HOUR=$(sed -n 's/.*"reboot_hour": *\([0-9]*\).*/\1/p' "$CFG" | head -n 1)
fi
DAY=${DAY:-sun}
HOUR=${HOUR:-3}
NEED=false
[ -f /var/run/reboot-required ] && NEED=true
PENDING=$(apt-get -s -o Debug::NoLocking=1 upgrade 2>/dev/null | grep -c '^Inst ' || true)
SECURITY=$(apt-get -s -o Debug::NoLocking=1 upgrade 2>/dev/null | grep '^Inst ' | grep -ci 'security' || true)
LAST=$(stat -c %Y /var/lib/apt/periodic/unattended-upgrades-stamp 2>/dev/null || echo 0)
. /etc/os-release
OS=$(printf '%s' "${PRETTY_NAME:-unknown}" | tr -d '"\\')
UP=$(cut -d. -f1 /proc/uptime)
printf '{"checked": %s, "reboot_required": %s, "pending": %s, "security": %s, "last_unattended": %s, "os": "%s", "kernel": "%s", "uptime_s": %s, "reboot_day": "%s", "reboot_hour": %s}\n' \
  "$(date +%s)" "$NEED" "${PENDING:-0}" "${SECURITY:-0}" "$LAST" "$OS" "$(uname -r)" "$UP" "$DAY" "$HOUR" > "$OUT.tmp"
chmod 600 "$OUT.tmp"
mv "$OUT.tmp" "$OUT"
[ "$NEED" = true ] || exit 0
[ "$DAY" = off ] && exit 0
case "$(date +%u)" in 1) TODAY=mon ;; 2) TODAY=tue ;; 3) TODAY=wed ;; 4) TODAY=thu ;; 5) TODAY=fri ;; 6) TODAY=sat ;; *) TODAY=sun ;; esac
if { [ "$DAY" = daily ] || [ "$DAY" = "$TODAY" ]; } && [ "$((10#$(date +%H)))" -eq "$((10#$HOUR))" ]; then
  logger -t scalio-maintenance "Neustart im Wartungsfenster (Update verlangt ihn)"
  systemctl reboot
fi
MAINT
  chmod 755 /usr/local/sbin/scalio-maintenance
  cat > /etc/systemd/system/scalio-maintenance.service <<'UNIT'
[Unit]
Description=Scalio: OS-Updates und Wartungsfenster
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/scalio-maintenance
UNIT
  cat > /etc/systemd/system/scalio-maintenance.timer <<'UNIT'
[Unit]
Description=Scalio: Wartungsfenster stündlich prüfen
[Timer]
OnCalendar=*-*-* *:05:00
OnBootSec=5min
[Install]
WantedBy=timers.target
UNIT
  systemctl daemon-reload
  systemctl enable --now scalio-maintenance.timer >/dev/null 2>&1 || true
  /usr/local/sbin/scalio-maintenance || true
fi

say "Scalio Installer starten"
docker pull -q "$SETUP_IMAGE" >/dev/null
if ! docker pull -q "$CADDY_IMAGE" >/dev/null 2>&1; then
  say "Hinweis: ${CADDY_IMAGE} nicht erreichbar, nehme caddy:2 (ohne Let's Encrypt über DNS)"
  CADDY_IMAGE="caddy:2"
  docker pull -q "$CADDY_IMAGE" >/dev/null
fi
docker rm -f scalio-setup scalio-caddy >/dev/null 2>&1 || true
LAB_ARGS=()
if [ "$CHANNEL" != public ]; then
  say "Kanal ${CHANNEL}: nur für Scalio-Laborserver"
  if [ -f /root/.docker/config.json ]; then
    LAB_ARGS=(-v /root/.docker/config.json:/root/.docker/config.json:ro)
  else
    say "Hinweis: kein docker login gefunden. Die Images der Apps sind privat: sudo docker login ghcr.io"
  fi
fi
docker run -d --name scalio-setup --restart unless-stopped --network "$NETWORK" \
  -v /var/run/docker.sock:/var/run/docker.sock -v "$ROOT:$ROOT" -e SCALIO_ROOT="$ROOT" -e SCALIO_CHANNEL="$CHANNEL" -e SCALIO_SERVER_NAME="$(hostname)" \
  "${LAB_ARGS[@]}" --security-opt no-new-privileges:true "$SETUP_IMAGE" >/dev/null
docker run -d --name scalio-caddy --restart unless-stopped --network "$NETWORK" -p 80:80 -p 443:443 \
  -v "$ROOT/caddy/Caddyfile:/etc/caddy/Caddyfile:ro" -v "$ROOT/caddy/sites:/etc/caddy/sites:ro" \
  -v "$ROOT/caddy/certs:/etc/caddy/certs:ro" -v "$ROOT/caddy/data:/data" --security-opt no-new-privileges:true "$CADDY_IMAGE" >/dev/null

CODE=""
CODE_CMD="sudo docker exec scalio-setup python -m scalio_setup code"
if grep -qs '"claimed": *true' "$ROOT/setup/state.json"; then
  # Schon einmal angemeldet: ein neuer Code (einmal, 30 Minuten), z. B. um weitere Apps zu installieren.
  sleep 3
  CODE=$(docker exec scalio-setup python -m scalio_setup code 2>/dev/null | awk '{print $2}' || true)
else
  for _ in $(seq 1 30); do
    [ -s "$ROOT/setup/first-code" ] && CODE=$(cat "$ROOT/setup/first-code") && break
    sleep 2
  done
fi

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
say "Scalio Installer läuft."
echo "  Im Browser öffnen:   https://${IP:-<IP dieses Servers>}  (Zertifikatswarnung beim ersten Aufruf bestätigen)"
if [ -n "$CODE" ]; then
  echo "  Einmal-Code:         $CODE"
else
  echo "  Einmal-Code:         $CODE_CMD"
fi
echo "  Danach führt Sie der Installer durch Apps, Adressen, Lizenz und Sicherung."
