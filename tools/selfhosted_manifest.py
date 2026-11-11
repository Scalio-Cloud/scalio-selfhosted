"""App manifest `selfhosted/scalio-app.yml` (scalio.app/v1): schema check and comparison with the engine template.

Every Scalio app describes in one file how it runs at a customer's site. Scalio Setup renders docker-compose.yml and
.env from it, so a new app needs no code in Setup. Format: README.md, section «App-Manifest».

  python tools/selfhosted_manifest.py check <app-repo>      # schema + compare with backend/tenant_template/docker.json
  python tools/selfhosted_manifest.py validate <file.yml>   # schema only

The comparison keeps the manifest and the SaaS template of the engine in step: same services, images and
commands, every environment variable of the template either in the manifest or SaaS-only, and every secret the engine
derives listed under `secrets` with the same format.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import yaml

API_VERSION = "scalio.app/v1"
DATABASES = {"mongo"}
SECRET_FORMATS = {"hex", "b64", "fernet"}
PLACEHOLDERS = {"mongo_url", "public_url", "setup_code", "app_links"}
TOP_KEYS = {"apiVersion", "app", "product", "setup", "memory_mb", "database", "public", "port", "secrets",
            "license_env", "env", "services", "volumes"}
TOP_REQUIRED = {"apiVersion", "app", "product", "setup", "memory_mb", "database", "public", "port", "services"}
SERVICE_KEYS = {"image", "command", "env", "environment", "healthcheck", "expose"}

# Variables of the engine template that only make sense in SaaS; Setup never sets them.
SAAS_ONLY = {"SAAS_MODE", "SCALIO_STAGE", "SUBDOMAIN", "SCALIO_ID_ISSUER", "JWT_SECRET_PREVIOUS"}
SAAS_ONLY_PREFIXES = ("SAAS_", "PROVISION_", "ERROR_REPORT_")
DERIVE_FORMAT = {"derive": "hex", "deriveB64": "b64", "deriveFernet": "fernet"}

_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
_NAME = re.compile(r"^[a-z][a-z0-9]*$")
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
_PLACEHOLDER = re.compile(r"\{\{\s*([^}]*?)\s*\}\}")
_ENGINE_SERVICE = re.compile(r"^saas_tenant_\{\{\.ID\}\}_([a-z0-9]+)$")
_ENGINE_IMAGE = re.compile(r"/(?:scalio-[a-z]+|selfhosted-scalio[a-z]+)-([a-z]+):")


def load(path: str | Path) -> dict:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: not a YAML mapping")
    return data


def _placeholders(value: str) -> list[str]:
    return _PLACEHOLDER.findall(value)


def validate(m: dict) -> list[str]:
    """Schema errors of a manifest (empty list = valid)."""
    err: list[str] = []
    for key in sorted(TOP_REQUIRED - m.keys()):
        err.append(f"missing key {key}")
    for key in sorted(m.keys() - TOP_KEYS):
        err.append(f"unknown key {key}")
    if m.get("apiVersion") != API_VERSION:
        err.append(f"apiVersion must be {API_VERSION}")
    if not isinstance(m.get("app"), str) or not _NAME.match(m.get("app", "")):
        err.append("app must be lower case letters and digits")
    if not isinstance(m.get("product"), str) or not m.get("product"):
        err.append("product must be a text")
    if not isinstance(m.get("setup"), str) or not _SEMVER.match(m.get("setup", "")):
        err.append("setup must be a version like 0.1.0")
    if not isinstance(m.get("memory_mb"), int) or m.get("memory_mb", 0) <= 0:
        err.append("memory_mb must be a positive whole number")
    if m.get("database") not in DATABASES:
        err.append(f"database must be one of {sorted(DATABASES)}")
    if not isinstance(m.get("port"), int) or not 0 < m.get("port", 0) < 65536:
        err.append("port must be a port number")
    if "license_env" in m and not (isinstance(m["license_env"], str) and _ENV_NAME.match(m["license_env"])):
        err.append("license_env must be an environment variable name")

    secrets: dict[str, str] = {}
    for i, s in enumerate(m.get("secrets") or []):
        if not isinstance(s, dict) or set(s) != {"name", "format"}:
            err.append(f"secrets[{i}] needs exactly name and format")
            continue
        if not _ENV_NAME.match(str(s["name"])):
            err.append(f"secrets[{i}].name {s['name']!r} is not an environment variable name")
        if s["format"] not in SECRET_FORMATS:
            err.append(f"secrets[{i}].format must be one of {sorted(SECRET_FORMATS)}")
        if s["name"] in secrets:
            err.append(f"secret {s['name']} listed twice")
        secrets[s["name"]] = s["format"]

    used: set[str] = set()

    def check_env(where: str, env) -> None:
        if env is None:
            return
        if not isinstance(env, dict):
            err.append(f"{where} must be a mapping")
            return
        for name, value in env.items():
            if not _ENV_NAME.match(str(name)):
                err.append(f"{where}.{name} is not an environment variable name")
            if not isinstance(value, (str, int, bool)):
                err.append(f"{where}.{name} must be a text")
                continue
            for p in _placeholders(str(value)):
                if p.startswith("secret."):
                    if p[7:] not in secrets:
                        err.append(f"{where}.{name} uses {{{{{p}}}}}, but {p[7:]} is not under secrets")
                    used.add(p[7:])
                elif p not in PLACEHOLDERS:
                    err.append(f"{where}.{name} uses unknown placeholder {{{{{p}}}}}")

    check_env("env", m.get("env"))
    services = m.get("services")
    if not isinstance(services, dict) or not services:
        err.append("services must list at least one service")
        services = {}
    for name, svc in services.items():
        where = f"services.{name}"
        if not _NAME.match(str(name)):
            err.append(f"{where}: name must be lower case letters and digits")
        if not isinstance(svc, dict):
            err.append(f"{where} must be a mapping")
            continue
        for key in sorted(svc.keys() - SERVICE_KEYS):
            err.append(f"{where}: key {key} is not allowed")
        if not isinstance(svc.get("image"), str) or not _NAME.match(svc.get("image", "")):
            err.append(f"{where}.image must be a component name like backend")
        cmd = svc.get("command")
        if cmd is not None and not (isinstance(cmd, list) and all(isinstance(c, str) for c in cmd)):
            err.append(f"{where}.command must be a list of texts")
        if svc.get("env") not in (None, "common"):
            err.append(f"{where}.env can only be common")
        if svc.get("env") == "common" and not m.get("env"):
            err.append(f"{where}.env is common, but the manifest has no env")
        check_env(f"{where}.environment", svc.get("environment"))
        hc = svc.get("healthcheck")
        if hc is not None and not (isinstance(hc, str) and hc.startswith("/")):
            err.append(f"{where}.healthcheck must be a path like /api/health")
        if hc is not None and not isinstance(svc.get("expose"), int):
            err.append(f"{where}.healthcheck needs expose (the port)")
    if m.get("public") not in services:
        err.append("public must name one of the services")
    for i, v in enumerate(m.get("volumes") or []):
        if not isinstance(v, dict) or set(v) != {"service", "path"}:
            err.append(f"volumes[{i}] needs exactly service and path")
            continue
        if v["service"] not in services:
            err.append(f"volumes[{i}].service {v['service']} is not a service")
        if not str(v["path"]).startswith("/") or ".." in str(v["path"]):
            err.append(f"volumes[{i}].path must be an absolute path in the container")
    for name in sorted(set(secrets) - used):
        err.append(f"secret {name} is never used")
    return err


def service_env(m: dict, service: str) -> dict:
    """Effective environment of a service: env (when env: common) plus its own environment."""
    svc = m["services"][service]
    env = dict(m.get("env") or {}) if svc.get("env") == "common" else {}
    env.update(svc.get("environment") or {})
    return env


def _saas_only(name: str) -> bool:
    return name in SAAS_ONLY or name.startswith(SAAS_ONLY_PREFIXES)


def compare(m: dict, template: dict) -> list[str]:
    """Differences between a (valid) manifest and the engine template backend/tenant_template/docker.json."""
    err: list[str] = []
    secrets = {s["name"]: s["format"] for s in m.get("secrets") or []}
    engine: dict[str, dict] = {}
    for svc in template.get("services", []):
        match = _ENGINE_SERVICE.match(svc.get("name", ""))
        if not match:
            err.append(f"engine service {svc.get('name')!r} has an unexpected name")
            continue
        if match.group(1) != "mongo":
            engine[match.group(1)] = svc
    mine = m.get("services") or {}
    for name in sorted(engine.keys() - mine.keys()):
        err.append(f"service {name} is in the engine template but not in the manifest")
    for name in sorted(mine.keys() - engine.keys()):
        err.append(f"service {name} is in the manifest but not in the engine template")
    for name in sorted(engine.keys() & mine.keys()):
        svc, own = engine[name], mine[name]
        image = _ENGINE_IMAGE.search(svc.get("image", ""))
        if not image:
            err.append(f"{name}: engine image {svc.get('image')!r} has an unexpected name")
        elif image.group(1) != own.get("image"):
            err.append(f"{name}: image {own.get('image')} in the manifest, {image.group(1)} in the engine template")
        if (svc.get("command") or None) != (own.get("command") or None):
            err.append(f"{name}: command differs from the engine template")
        env = service_env(m, name)
        for line in svc.get("envs", []):
            var, _, value = line.partition("=")
            if _saas_only(var):
                continue
            if var == m.get("license_env") and "{{.License}}" in value:
                continue
            if var not in env:
                err.append(f"{name}: {var} is set by the engine template but missing in the manifest")
                continue
            derived = re.search(r"\{\{\s*(derive|deriveB64|deriveFernet)\s", value)
            if derived:
                want = DERIVE_FORMAT[derived.group(1)]
                if secrets.get(var) != want:
                    err.append(f"{name}: {var} is derived by the engine, list it under secrets with format {want}")
            if "{{.JWTSecret}}" in value and var not in secrets:
                err.append(f"{name}: {var} is a secret in the engine template, list it under secrets")
            if "{{.License}}" in value and m.get("license_env") != var:
                err.append(f"{name}: {var} carries the license in the engine template, set license_env: {var}")
    return err


def check_repo(repo: str | Path) -> list[str]:
    repo = Path(repo)
    path = repo / "selfhosted" / "scalio-app.yml"
    if not path.is_file():
        return [f"{path} is missing"]
    try:
        m = load(path)
    except (OSError, ValueError, yaml.YAMLError) as e:
        return [str(e)]
    err = validate(m)
    if err:
        return err
    template = repo / "backend" / "tenant_template" / "docker.json"
    if template.is_file():
        err += compare(m, json.loads(template.read_text(encoding="utf-8")))
    return err


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] not in ("check", "validate"):
        print(__doc__.strip().splitlines()[0])
        print("usage: selfhosted_manifest.py check <app-repo> | validate <file.yml>")
        return 2
    if argv[0] == "check":
        err = check_repo(argv[1])
    else:
        try:
            err = validate(load(argv[1]))
        except (OSError, ValueError, yaml.YAMLError) as e:
            err = [str(e)]
    for e in err:
        print(f"::error::{e}" if "GITHUB_ACTIONS" in os.environ else e)
    if not err:
        print("manifest ok")
    return 1 if err else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
