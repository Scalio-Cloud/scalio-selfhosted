# Scalio Self-hosted

Öffentliches Repo für die Installation der Scalio-Apps auf eigener Infrastruktur (Linux). Hier liegen:

- `get.sh`: das Installationsscript hinter `https://get.scaliocloud.com`. Es installiert Docker und startet den
  **Scalio Installer**; alles Weitere läuft im Browser.
- `docs/`: GitHub Pages hinter `get.scaliocloud.com` (CNAME auf `scalio-cloud.github.io`, Pages aus `main` Ordner
  `/docs`). `index.html` und `get.sh` darin sind Kopien von `get.sh` (nach jeder Änderung: `cp get.sh docs/index.html && cp get.sh docs/get.sh`); `tests/test_site.py` prüft, dass sie gleich sind.
- `index/`: der signierte Release-Index. Der Scalio Installer liest ihn, prüft die Signatur und holt die Images der
  Apps nur über ihren Digest.
- `tools/`: Prüfung des App-Manifests und Werkzeug für den Index (Python 3.12+, `pyyaml`, `cryptography`).
- Releases: `installer-<version>` für den Scalio Installer und `<app>-<version>` für jede App, mit Release Notes.

Hier liegen keine Geheimnisse, keine Kundendaten und keine internen Adressen.

## App-Manifest

Jede App legt `selfhosted/scalio-app.yml` (Format `scalio.app/v1`) in ihr Repo und ihr Image. Der Scalio Installer baut
daraus `docker-compose.yml` und `.env` der App.

```yaml
apiVersion: scalio.app/v1
app: citadel
product: Scalio Citadel
setup: 0.1.0          # kleinste Version des Scalio Installers
memory_mb: 1536       # RAM inkl. Datenbank
database: mongo       # ein mongo:7-Container nur für diese App
public: frontend      # Dienst hinter dem Hostnamen der App (über Caddy)
port: 80
secrets:              # erzeugt der Installer einmal, stehen auf dem Notfallblatt
  - {name: JWT_SECRET, format: hex}   # hex | b64 | fernet, je 32 Byte
license_env: CITADEL_LICENSE_KEY
env:                  # für alle Dienste mit «env: common»
  MONGO_URL: "{{mongo_url}}"
  JWT_SECRET: "{{secret.JWT_SECRET}}"
  APP_PUBLIC_URL: "{{public_url}}"
  SETUP_CODE: "{{setup_code}}"
  SCALIO_APP_LINKS: "{{app_links}}"
services:
  backend:  {image: backend, env: common, healthcheck: /api/health, expose: 8001}
  worker:   {image: backend, env: common, command: [python, -m, worker]}
  frontend: {image: frontend, environment: {BACKEND_URL: "http://backend:8001"}}
volumes:
  - {service: backend, path: /data/storage}
```

Platzhalter: `{{mongo_url}}`, `{{public_url}}`, `{{setup_code}}`, `{{app_links}}`, `{{secret.<NAME>}}`. Keine Ports
auf dem Host, kein Docker-Socket, keine Host-Pfade. Prüfen:

```sh
python tools/selfhosted_manifest.py check <app-repo>
```

Das vergleicht das Manifest zusätzlich mit der SaaS-Vorlage der App (`backend/tenant_template/docker.json`), damit
beide dieselben Dienste und Variablen kennen.

## Release-Index

`index/index.json` (`scalio.selfhosted-index/v1`) listet pro App die Versionen, neueste zuerst, mit Digest jedes
Images, der Prüfsumme des Manifests (`index/<app>/<version>.yml`) und der Mindestversion des Installers.
`index/index.json.sig` ist die Ed25519-Signatur über die Bytes von `index.json`. Eine Version wird nie geändert, nur
zurückgezogen (`withdrawn`).

```sh
python tools/selfhosted_index.py verify --public-key <öffentlicher Schlüssel>
```

Neue Versionen trägt nur der Release-Workflow von Scalio ein.

### Labor-Kanäle

Die Branches `channel-dev` und `channel-rc` enthalten je einen eigenen Index mit `"channel": "dev"` bzw. `"rc"`:
pro App nur den neusten Dev-Build (`dev-<commit>`) oder Release Candidate, signiert mit einem eigenen
Staging-Schlüssel. Sie sind nur für die Laborserver von Scalio (die Images sind privat). Ein Installer im Kanal
`public` nimmt diese Indizes nie an. Start auf einem Laborserver:

```sh
sudo docker login ghcr.io
curl -fsSL https://raw.githubusercontent.com/Scalio-Cloud/scalio-selfhosted/main/get.sh -o get.sh
sudo SCALIO_CHANNEL=dev bash get.sh
```

## ISO (Ubuntu Server mit Scalio)

`image/` baut ein ISO aus dem offiziellen Ubuntu-Server-ISO (Prüfsumme gegen die signierte `SHA256SUMS` von Ubuntu)
mit Autoinstall: Es fragt nur Sprache und Tastatur, Netzwerk, das Administrator-Konto und SSH (kein Standardkonto,
kein Standardpasswort), nimmt die ganze erste Platte, schaltet die Firewall (22, 80, 443) und die automatischen
Sicherheitsupdates ein. SSH: Wird bei der Installation ein Schlüssel importiert (z. B. von GitHub), geht SSH nur noch mit
Schlüssel, sonst bleibt die Anmeldung mit Passwort offen (fail2ban). Beim ersten Start holt `scalio-update` die neuste `get.sh` (ohne Internet die vom ISO) und führt sie aus; danach zeigen
die Konsole vor der Anmeldung und jede Anmeldung (Konsole, SSH) Adresse und Einmal-Code. `sudo scalio-code` gibt einen neuen Anmelde-Code (einmal, 30 Minuten), `sudo scalio-update` aktualisiert den Installer später jederzeit (Apps und Daten
bleiben). Der Kanal steht in `/opt/scalio-image/channel` (Build mit `SCALIO_CHANNEL`, Standard public).

```sh
image/build-iso.sh            # braucht curl, gpg und xorriso
```

Workflow «ISO» baut es von Hand oder bei jedem Release `installer-<version>` und legt es 14 Tage als Artefakt ab.
Ein Release lädt es ausserdem in den öffentlichen Cloudflare-R2-Bucket `scalio-iso`, nach `<version>/` mit `SHA256SUMS`,
dazu `latest.txt`; die zwei neusten Versionen bleiben. Prüfen nach dem Download:

```sh
sha256sum -c SHA256SUMS
```

## Entwicklung

```sh
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest -q && .venv/bin/ruff check tools tests
```
