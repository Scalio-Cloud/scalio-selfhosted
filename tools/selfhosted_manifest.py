#!/usr/bin/env python3
"""Check a self-hosted app manifest selfhosted/scalio-app.yml (format scalio.app/v1).

    python tools/selfhosted_manifest.py check <app-repo>       # schema + comparison with docker.json
    python tools/selfhosted_manifest.py validate <file.yml>    # schema only

1. Schema: apiVersion scalio.app/v1, services, ports, secrets, RAM, healthcheck, volumes, minimum setup version,
   placeholders, no host ports, no Docker socket, no privileges.
2. Comparison with the engine template backend/tenant_template/docker.json (ADR 0067): same services (without mongo),
   same image components and commands, every engine variable in the manifest or SaaS-only, engine license in
   license_env, engine secrets (derive…) under secrets with the same format, public service, port and volumes.

Exit code 0 when everything is fine, 1 with one line per error (in GitHub Actions as ::error:: annotation). Only needs
PyYAML, so every app CI and the shared workflow can run the same file.

The shared release workflow, the app CI and `tools/selfhosted_index.py add` (through `load` and `validate`) all use
this one file; Scalio keeps its copies byte for byte the same, a test checks it.
"""
from __future__ import annotations

import json
import os
import posixpath
import re
import sys
from pathlib import Path

import yaml

API_VERSION = "scalio.app/v1"
MANIFEST_PATH = Path("selfhosted") / "scalio-app.yml"
TEMPLATE_PATH = Path("backend") / "tenant_template" / "docker.json"

TOP_KEYS = {"apiVersion", "app", "product", "setup", "memory_mb", "database", "public", "port", "secrets",
            "license_env", "env", "services", "volumes"}
REQUIRED_TOP = ("apiVersion", "app", "product", "setup", "memory_mb", "database", "public", "port", "services")
SERVICE_KEYS = {"image", "env", "environment", "command", "healthcheck", "expose"}
SECRET_KEYS = {"name", "format"}
VOLUME_KEYS = {"service", "path"}
DATABASES = {"mongo"}  # postgres follows with Kontor
SECRET_FORMATS = {"hex", "b64", "fernet"}
PLACEHOLDERS = {"mongo_url", "public_url", "setup_code", "app_links"}

# Variables only the SaaS engine sets; the installer does not need them (format, section «App-Manifest»).
SAAS_PREFIXES = ("SAAS_", "PROVISION_", "ERROR_REPORT_")
SAAS_NAMES = {"SCALIO_ID_ISSUER", "SCALIO_STAGE", "SUBDOMAIN", "JWT_SECRET_PREVIOUS"}
# Engine template functions and fields that are secrets, with the format the manifest must declare.
ENGINE_SECRETS = (
    (re.compile(r"\{\{\s*deriveFernet\b"), "fernet"),
    (re.compile(r"\{\{\s*deriveB64\b"), "b64"),
    (re.compile(r"\{\{\s*derive\b"), "hex"),
    (re.compile(r"\{\{\s*\.JWTSecret\s*\}\}"), "hex"),
)
ENGINE_LICENSE = re.compile(r"\{\{\s*\.License\s*\}\}")
ENGINE_SERVICE = re.compile(r"^saas_tenant_\{\{\s*\.ID\s*\}\}_(?P<name>[a-z0-9_-]+)$")
ENGINE_UPSTREAM = re.compile(r"\{\{\s*upstreams\s+(?P<port>\d+)\s*\}\}")

APP_NAME = re.compile(r"^[a-z][a-z0-9]*$")
SERVICE_NAME = re.compile(r"^[a-z][a-z0-9_-]*$")
COMPONENT = re.compile(r"^[a-z][a-z0-9-]*$")
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SECRET_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
VERSION = re.compile(r"^\d+\.\d+\.\d+$")
PLACEHOLDER = re.compile(r"\{\{(.*?)\}\}")
DOCKER_SOCKETS = ("/var/run/docker.sock", "/run/docker.sock")


class ManifestError(ValueError):
    """The manifest file cannot be read as a YAML mapping."""


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _mapping_without_duplicates(loader, node, deep=False):
    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise ManifestError(f"duplicate key {key!r} (line {key_node.start_mark.line + 1})")
        seen.add(key)
    return loader.construct_mapping(node, deep=deep)


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping_without_duplicates)


def load_manifest(path: Path) -> dict:
    try:
        data = yaml.load(Path(path).read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
    except yaml.YAMLError as exc:
        raise ManifestError(f"not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ManifestError("the manifest must be a mapping")
    return data


def load(path: str | Path) -> dict:
    """Name used by scalio-selfhosted (selfhosted_index.py): raises ValueError like load_manifest."""
    return load_manifest(Path(path))


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_port(value) -> bool:
    return _is_int(value) and 1 <= value <= 65535


def _secrets(manifest: dict) -> dict[str, str]:
    """Declared secrets {name: format}, ignoring malformed entries (check_schema reports them)."""
    out = {}
    for s in manifest.get("secrets") or []:
        if isinstance(s, dict) and isinstance(s.get("name"), str):
            out.setdefault(s["name"], s.get("format"))
    return out


def _services(manifest: dict) -> dict:
    services = manifest.get("services")
    return services if isinstance(services, dict) else {}


def effective_env(manifest: dict, service: str) -> dict:
    """Environment the installer gives a service: top-level env for «env: common», environment on top."""
    spec = _services(manifest).get(service) or {}
    env = {}
    if spec.get("env") == "common" and isinstance(manifest.get("env"), dict):
        env.update(manifest["env"])
    if isinstance(spec.get("environment"), dict):
        env.update(spec["environment"])
    return env


# --- schema ------------------------------------------------------------------------------------------------------

def _check_env_block(where: str, env, secrets: dict, used: set, errors: list) -> None:
    if not isinstance(env, dict):
        errors.append(f"{where} must be a mapping of NAME: value")
        return
    for name, value in env.items():
        if not isinstance(name, str) or not ENV_NAME.match(name):
            errors.append(f"{where}: env name {name!r} is not a valid variable name")
            continue
        if not isinstance(value, str):
            errors.append(f"{where}.{name} must be a string (quote it: \"{value}\")")
            continue
        for raw in PLACEHOLDER.findall(value):
            ph = raw.strip()
            if ph.startswith("secret."):
                secret = ph[len("secret."):]
                used.add(secret)
                if secret not in secrets:
                    errors.append(f"{where}.{name}: {{{{secret.{secret}}}}} is not declared under secrets")
            elif ph not in PLACEHOLDERS:
                errors.append(f"{where}.{name}: unknown placeholder {{{{{raw}}}}}")


def check_schema(manifest: dict) -> list[str]:
    errors: list[str] = []
    for key in REQUIRED_TOP:
        if key not in manifest:
            errors.append(f"'{key}' is missing")
    for key in manifest:
        if key not in TOP_KEYS:
            errors.append(f"unknown key {key!r}")

    if "apiVersion" in manifest and manifest["apiVersion"] != API_VERSION:
        errors.append(f"apiVersion must be {API_VERSION!r}, not {manifest['apiVersion']!r}")
    if "app" in manifest and not (isinstance(manifest["app"], str) and APP_NAME.match(manifest["app"])):
        errors.append(f"app {manifest['app']!r} must be lower case letters and digits (the name in the images)")
    if "product" in manifest and not (isinstance(manifest["product"], str) and manifest["product"].strip()):
        errors.append("product must be a non-empty name")
    if "setup" in manifest and not (isinstance(manifest["setup"], str) and VERSION.match(manifest["setup"])):
        errors.append(f"setup {manifest['setup']!r} must be a version X.Y.Z of the Scalio installer (quote it)")
    if "memory_mb" in manifest and not (_is_int(manifest["memory_mb"]) and manifest["memory_mb"] > 0):
        errors.append(f"memory_mb {manifest['memory_mb']!r} must be a positive number of MB")
    if "database" in manifest and manifest["database"] not in DATABASES:
        errors.append(f"database {manifest['database']!r} must be one of {', '.join(sorted(DATABASES))}")
    if "port" in manifest and not _is_port(manifest["port"]):
        errors.append(f"port {manifest['port']!r} must be a port number 1-65535")
    if "license_env" in manifest and not (isinstance(manifest["license_env"], str)
                                          and ENV_NAME.match(manifest["license_env"])):
        errors.append(f"license_env {manifest['license_env']!r} must be a variable name")

    # secrets
    secrets: dict[str, str] = {}
    raw_secrets = manifest.get("secrets", [])
    if not isinstance(raw_secrets, list):
        errors.append("secrets must be a list of {name, format}")
        raw_secrets = []
    for i, s in enumerate(raw_secrets):
        if not isinstance(s, dict):
            errors.append(f"secrets[{i}] must be a mapping {{name, format}}")
            continue
        for key in s:
            if key not in SECRET_KEYS:
                errors.append(f"secrets[{i}]: unknown key {key!r}")
        name, fmt = s.get("name"), s.get("format")
        if not (isinstance(name, str) and SECRET_NAME.match(name)):
            errors.append(f"secrets[{i}]: secret name {name!r} must be UPPER_CASE")
            continue
        if fmt not in SECRET_FORMATS:
            errors.append(f"secrets[{i}] {name}: format {fmt!r} must be one of {', '.join(sorted(SECRET_FORMATS))}")
        if name in secrets:
            errors.append(f"secret {name} is declared twice")
        secrets[name] = fmt

    # env and placeholders
    used: set[str] = set()
    if "env" in manifest:
        _check_env_block("env", manifest["env"], secrets, used, errors)

    # services
    services = manifest.get("services")
    if "services" in manifest and not (isinstance(services, dict) and services):
        errors.append("services must be a non-empty mapping")
        services = {}
    services = services or {}
    healthchecks = 0
    for name, spec in services.items():
        where = f"services.{name}"
        if not (isinstance(name, str) and SERVICE_NAME.match(name)):
            errors.append(f"service name {name!r} must be lower case")
            continue
        if name == "mongo":
            errors.append("service 'mongo' is added by the installer (database: mongo), do not list it")
        if not isinstance(spec, dict):
            errors.append(f"{where} must be a mapping")
            continue
        for key in spec:
            if key not in SERVICE_KEYS:
                errors.append(f"{where}: unknown key {key!r} (allowed: {', '.join(sorted(SERVICE_KEYS))}; "
                              "no host ports, privileges, sockets or host paths)")
        if "image" not in spec:
            errors.append(f"{where}.image is missing")
        elif not (isinstance(spec["image"], str) and COMPONENT.match(spec["image"])):
            errors.append(f"{where}.image {spec['image']!r} must be the component only (backend, frontend, …)")
        if "env" in spec:
            if spec["env"] != "common":
                errors.append(f"{where}.env must be 'common'")
            elif "env" not in manifest:
                errors.append(f"{where} has env: common but there is no top-level env")
        if "environment" in spec:
            _check_env_block(f"{where}.environment", spec["environment"], secrets, used, errors)
        if "command" in spec and not (isinstance(spec["command"], list) and spec["command"]
                                      and all(isinstance(c, str) for c in spec["command"])):
            errors.append(f"{where}.command must be a list of strings")
        if "expose" in spec and not _is_port(spec["expose"]):
            errors.append(f"{where}.expose {spec['expose']!r} must be a port number 1-65535")
        if "healthcheck" in spec:
            healthchecks += 1
            hc = spec["healthcheck"]
            if not (isinstance(hc, str) and hc.startswith("/")):
                errors.append(f"{where}.healthcheck must be a path like /api/health")
            if "expose" not in spec:
                errors.append(f"{where}.healthcheck needs expose (the port of the path)")
    if services and not healthchecks:
        errors.append("no service has a healthcheck")
    if "public" in manifest and services and manifest["public"] not in services:
        errors.append(f"public {manifest['public']!r} is not a service")

    # volumes
    raw_volumes = manifest.get("volumes", [])
    if not isinstance(raw_volumes, list):
        errors.append("volumes must be a list of {service, path}")
        raw_volumes = []
    seen_volumes = set()
    for i, v in enumerate(raw_volumes):
        where = f"volumes[{i}]"
        if not isinstance(v, dict):
            errors.append(f"{where} must be a mapping {{service, path}}")
            continue
        for key in v:
            if key not in VOLUME_KEYS:
                errors.append(f"{where}: unknown key {key!r} (only named volumes, no host paths)")
        service, path = v.get("service"), v.get("path")
        if service not in services:
            errors.append(f"{where}.service {service!r} is not a service")
        if not (isinstance(path, str) and path.startswith("/")):
            errors.append(f"{where}.path must be an absolute path in the container")
        elif posixpath.normpath(path) != path.rstrip("/") or ".." in path.split("/"):
            errors.append(f"{where}.path {path!r} must be a plain path without '..'")
        elif path.rstrip("/") in DOCKER_SOCKETS:
            errors.append(f"{where}.path must not be the Docker socket")
        if (service, path) in seen_volumes:
            errors.append(f"{where} is a duplicate")
        seen_volumes.add((service, path))

    for name in sorted(set(secrets) - used):
        errors.append(f"secret {name} is never used in env or environment")
    return errors


# --- comparison with docker.json ---------------------------------------------------------------------------------

def _engine_component(image: str, app: str) -> str | None:
    """Component of an engine image: ghcr.io/scalio-cloud/scalio-<app>-<c>:… or selfhosted-scalio<app>-<c>:…"""
    repo = image.rsplit("/", 1)[-1].split("@", 1)[0].split(":", 1)[0]
    for prefix in (f"scalio-{app}-", f"selfhosted-scalio{app}-", f"scalio{app}-"):
        if repo.startswith(prefix) and len(repo) > len(prefix):
            return repo[len(prefix):]
    return None


def _engine_env(service: dict) -> dict[str, str]:
    env = {}
    for entry in service.get("envs") or []:
        if isinstance(entry, str):
            name, _, value = entry.partition("=")
            env[name] = value
    return env


def _is_saas_only(name: str) -> bool:
    return name.startswith(SAAS_PREFIXES) or name in SAAS_NAMES


def _engine_secret_format(value: str) -> str | None:
    for pattern, fmt in ENGINE_SECRETS:
        if pattern.search(value):
            return fmt
    return None


def compare(manifest: dict, template: dict) -> list[str]:
    errors: list[str] = []
    app = manifest.get("app") if isinstance(manifest.get("app"), str) else ""
    services = _services(manifest)
    secrets = _secrets(manifest)
    license_env = manifest.get("license_env")

    engine: dict[str, dict] = {}
    has_mongo = False
    for s in template.get("services") or []:
        if not isinstance(s, dict):
            continue
        m = ENGINE_SERVICE.match(str(s.get("name", "")))
        if not m:
            errors.append(f"docker.json: service name {s.get('name')!r} is not saas_tenant_{{{{.ID}}}}_<name>")
            continue
        if m["name"] == "mongo":
            has_mongo = True
            continue
        engine[m["name"]] = s
    if not engine:
        return errors + ["docker.json has no services besides mongo" if has_mongo else "docker.json has no services"]

    if manifest.get("database") == "mongo" and not has_mongo:
        errors.append("database is 'mongo', docker.json has no mongo service")

    for name in sorted(set(services) - set(engine)):
        errors.append(f"service {name} is in the manifest but not in docker.json")
    for name in sorted(set(engine) - set(services)):
        errors.append(f"service {name} is in docker.json but not in the manifest")

    volumes = {(v.get("service"), v.get("path")) for v in manifest.get("volumes") or [] if isinstance(v, dict)}
    public_engine, public_port = [], None
    for name in sorted(set(engine) & set(services)):
        es, ms = engine[name], services[name] if isinstance(services[name], dict) else {}
        where = f"service {name}"

        component = _engine_component(str(es.get("image", "")), app)
        if component is None:
            errors.append(f"{where}: image {ms.get('image')!r} in the manifest, docker.json uses "
                          f"{es.get('image')!r}, which is not an image of app {app!r}")
        elif component != ms.get("image"):
            errors.append(f"{where}: image {ms.get('image')!r} in the manifest, {component!r} in docker.json")
        if (es.get("command") or None) != (ms.get("command") or None):
            errors.append(f"{where}: command {ms.get('command')!r} in the manifest, {es.get('command')!r} in docker.json")

        mine = effective_env(manifest, name)
        for var, value in _engine_env(es).items():
            if _is_saas_only(var):
                continue
            if ENGINE_LICENSE.search(value):
                if not license_env:
                    errors.append(f"{where}: {var} gets the license in docker.json, but license_env is missing")
                elif license_env != var:
                    errors.append(f"{where}: {var} gets the license in docker.json, but license_env is {license_env!r}")
                continue
            fmt = _engine_secret_format(value)
            if fmt:
                expected = f"{{{{secret.{var}}}}}"
                if PLACEHOLDER.sub(lambda m: "{{" + m[1].strip() + "}}", str(mine.get(var, ""))) != expected:
                    errors.append(f"{where}: {var} is a secret in docker.json ({fmt}), "
                                  f"the manifest must set it to {expected} and declare it under secrets")
                elif var in secrets and secrets[var] != fmt:
                    errors.append(f"{where}: {var}: format {secrets[var]!r} in the manifest, {fmt!r} in docker.json")
                continue
            if var not in mine:
                errors.append(f"{where}: {var} from docker.json is missing in the manifest")

        for vol in es.get("volumes") or []:
            path = vol.get("container_path") if isinstance(vol, dict) else None
            if path and (name, path) not in volumes:
                errors.append(f"{where}: volume {path} from docker.json is missing in the manifest")

        labels = es.get("labels") or {}
        if isinstance(labels, dict) and "caddy" in labels:
            public_engine.append(name)
            up = ENGINE_UPSTREAM.search(str(labels.get("caddy.reverse_proxy", "")))
            if up:
                public_port = int(up["port"])

    public = manifest.get("public")
    if len(public_engine) == 1 and public != public_engine[0]:
        errors.append(f"public is {public!r}, docker.json routes the hostname to {public_engine[0]!r}")
    if public_port is not None and manifest.get("port") != public_port:
        errors.append(f"port is {manifest.get('port')!r}, docker.json routes to port {public_port}")
    return errors


# --- repo and CLI ------------------------------------------------------------------------------------------------

def check_repo(repo: Path) -> list[str]:
    repo = Path(repo)
    manifest_file, template_file = repo / MANIFEST_PATH, repo / TEMPLATE_PATH
    if not manifest_file.is_file():
        return [f"{MANIFEST_PATH.as_posix()} is missing"]
    try:
        manifest = load_manifest(manifest_file)
    except ManifestError as exc:
        return [f"{MANIFEST_PATH.as_posix()}: {exc}"]
    errors = [f"{MANIFEST_PATH.as_posix()}: {e}" for e in check_schema(manifest)]
    if not template_file.is_file():
        return errors + [f"{TEMPLATE_PATH.as_posix()} is missing (needed for the comparison)"]
    try:
        template = json.loads(template_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return errors + [f"{TEMPLATE_PATH.as_posix()}: not valid JSON: {exc}"]
    if not isinstance(template, dict):
        return errors + [f"{TEMPLATE_PATH.as_posix()}: must be a JSON object"]
    return errors + [f"compare with docker.json: {e}" for e in compare(manifest, template)]


def validate(manifest: dict) -> list[str]:
    """Name used by scalio-selfhosted (selfhosted_index.py): schema errors, empty list = valid."""
    return check_schema(manifest)


def validate_file(path: Path) -> list[str]:
    try:
        return check_schema(load_manifest(path))
    except (OSError, ManifestError) as exc:
        return [f"{Path(path).as_posix()}: {exc}"]


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] not in ("check", "validate"):
        print("usage: selfhosted_manifest.py check <app-repo> | validate <file.yml>", file=sys.stderr)
        return 2
    errors = check_repo(Path(argv[1])) if argv[0] == "check" else validate_file(Path(argv[1]))
    prefix = "::error::" if os.environ.get("GITHUB_ACTIONS") == "true" else "ERROR "
    for e in errors:
        print(f"{prefix}{e}")
    if errors:
        print(f"{len(errors)} error(s) in {argv[1]} (format: README.md of scalio-selfhosted, section «App-Manifest»)")
        return 1
    print(f"OK {argv[1]}: manifest and docker.json match" if argv[0] == "check" else f"OK {argv[1]}: manifest valid")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
