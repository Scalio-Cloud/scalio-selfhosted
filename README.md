# Scalio Self-hosted

Öffentliches Repo für die Installation der Scalio-Apps auf eigener Infrastruktur (Linux). Hier liegen:

- `get.sh` (folgt): das Installationsscript hinter `https://get.scaliocloud.com`. Es installiert Docker und startet
  **Scalio Setup**; alles Weitere läuft im Browser.
- `index/`: der signierte Release-Index. Scalio Setup liest ihn, prüft die Signatur und holt die Images der Apps nur
  über ihren Digest.
- `tools/`: Prüfung des App-Manifests und Werkzeug für den Index (Python 3.12+, `pyyaml`, `cryptography`).
- Releases: `setup-<version>` für Scalio Setup und `<app>-<version>` für jede App, mit Release Notes.

Hier liegen keine Geheimnisse, keine Kundendaten und keine internen Adressen.

## App-Manifest

Jede App legt `selfhosted/scalio-app.yml` (Format `scalio.app/v1`) in ihr Repo und ihr Image. Scalio Setup baut
daraus `docker-compose.yml` und `.env` der App.

```yaml
apiVersion: scalio.app/v1
app: citadel
product: Scalio Citadel
setup: 0.1.0          # kleinste Version von Scalio Setup
memory_mb: 1536       # RAM inkl. Datenbank
database: mongo       # ein mongo:7-Container nur für diese App
public: frontend      # Dienst hinter dem Hostnamen der App (über Caddy)
port: 80
secrets:              # erzeugt Setup einmal, stehen auf dem Notfallblatt
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
Images, der Prüfsumme des Manifests (`index/<app>/<version>.yml`) und der Mindestversion von Setup.
`index/index.json.sig` ist die Ed25519-Signatur über die Bytes von `index.json`. Eine Version wird nie geändert, nur
zurückgezogen (`withdrawn`).

```sh
python tools/selfhosted_index.py verify --public-key <öffentlicher Schlüssel>
```

Neue Versionen trägt nur der Release-Workflow von Scalio ein.

## Entwicklung

```sh
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest -q && .venv/bin/ruff check tools tests
```
