"""Self-hosted app manifest selfhosted/scalio-app.yml (format scalio.app/v1, see README.md).

tools/selfhosted_manifest.py checks the schema of the manifest and compares it with the engine template
backend/tenant_template/docker.json (ADR 0067). The example app in tests/fixtures/selfhosted-app passes; every test
breaks one rule on a copy of it.
"""
import copy
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
# Same tests as scalio-infra tests/test_selfhosted_manifest.py (the tool is byte-identical there).
_spec = importlib.util.spec_from_file_location("infra_selfhosted_manifest", ROOT / "tools" / "selfhosted_manifest.py")
sm = importlib.util.module_from_spec(_spec)
sys.modules["infra_selfhosted_manifest"] = sm
_spec.loader.exec_module(sm)

EXAMPLE = ROOT / "tests" / "fixtures" / "selfhosted-app"
MANIFEST = yaml.safe_load((EXAMPLE / "selfhosted" / "scalio-app.yml").read_text(encoding="utf-8"))
TEMPLATE = json.loads((EXAMPLE / "backend" / "tenant_template" / "docker.json").read_text(encoding="utf-8"))


def manifest(**changes):
    m = copy.deepcopy(MANIFEST)
    for key, value in changes.items():
        if value is None:
            m.pop(key, None)
        else:
            m[key] = value
    return m


def template():
    return copy.deepcopy(TEMPLATE)


def engine_service(t, name):
    return next(s for s in t["services"] if s["name"] == f"saas_tenant_{{{{.ID}}}}_{name}")


def schema_errors(m):
    return "\n".join(sm.check_schema(m))


def compare_errors(m=None, t=None):
    return "\n".join(sm.compare(m if m is not None else manifest(), t if t is not None else template()))


# --- the example passes ------------------------------------------------------------------------------------------

def test_example_app_passes_schema_and_comparison():
    assert sm.check_schema(manifest()) == []
    assert sm.compare(manifest(), template()) == []
    assert sm.check_repo(EXAMPLE) == []


def test_cli_check_passes_for_the_example_and_fails_for_a_broken_copy(tmp_path):
    ok = subprocess.run([sys.executable, str(ROOT / "tools" / "selfhosted_manifest.py"), "check", str(EXAMPLE)],  # noqa: S603
                        capture_output=True, text=True)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "OK" in ok.stdout

    repo = tmp_path / "app"
    shutil.copytree(EXAMPLE, repo)
    path = repo / "selfhosted" / "scalio-app.yml"
    path.write_text(path.read_text(encoding="utf-8").replace("memory_mb: 1536", "memory_mb: viel"), encoding="utf-8")
    bad = subprocess.run([sys.executable, str(ROOT / "tools" / "selfhosted_manifest.py"), "check", str(repo)],  # noqa: S603
                         capture_output=True, text=True)
    assert bad.returncode == 1
    assert "memory_mb" in bad.stdout


def test_missing_files_are_reported(tmp_path):
    assert "selfhosted/scalio-app.yml" in "\n".join(sm.check_repo(tmp_path))
    (tmp_path / "selfhosted").mkdir()
    shutil.copy(EXAMPLE / "selfhosted" / "scalio-app.yml", tmp_path / "selfhosted" / "scalio-app.yml")
    assert "tenant_template/docker.json" in "\n".join(sm.check_repo(tmp_path))


def test_duplicate_keys_and_non_mapping_yaml_are_errors(tmp_path):
    path = tmp_path / "scalio-app.yml"
    path.write_text("apiVersion: scalio.app/v1\napp: a\napp: b\n", encoding="utf-8")
    with pytest.raises(sm.ManifestError, match="duplicate key 'app'"):
        sm.load_manifest(path)
    path.write_text("- just\n- a list\n", encoding="utf-8")
    with pytest.raises(sm.ManifestError, match="mapping"):
        sm.load_manifest(path)


# --- schema: top level -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("key", ["apiVersion", "app", "product", "setup", "memory_mb", "database", "public", "port",
                                 "services"])
def test_required_keys(key):
    assert f"'{key}' is missing" in schema_errors(manifest(**{key: None}))


def test_unknown_top_level_key_is_an_error():
    assert "unknown key 'ports'" in schema_errors(manifest(ports=["80:80"]))


@pytest.mark.parametrize("key,value,needle", [
    ("apiVersion", "scalio.app/v2", "apiVersion"),
    ("app", "Example", "app"),
    ("app", "scalio-example", "app"),
    ("product", "", "product"),
    ("setup", "0.1", "setup"),
    ("setup", "v0.1.0", "setup"),
    ("setup", 1, "setup"),
    ("memory_mb", 0, "memory_mb"),
    ("memory_mb", "1536", "memory_mb"),
    ("memory_mb", True, "memory_mb"),
    ("database", "postgres", "database"),
    ("public", "proxy", "public"),
    ("port", 0, "port"),
    ("port", 70000, "port"),
    ("port", "80", "port"),
    ("license_env", "license key", "license_env"),
])
def test_bad_top_level_values(key, value, needle):
    errors = sm.check_schema(manifest(**{key: value}))
    assert any(needle in e for e in errors), errors


# --- schema: secrets, env, placeholders --------------------------------------------------------------------------

def test_secret_needs_a_known_format_and_a_unique_upper_case_name():
    secrets = MANIFEST["secrets"] + [{"name": "OTHER", "format": "rsa"}]
    assert "format 'rsa'" in schema_errors(manifest(secrets=secrets))
    secrets = MANIFEST["secrets"] + [{"name": "JWT_SECRET", "format": "hex"}]
    assert "JWT_SECRET is declared twice" in schema_errors(manifest(secrets=secrets))
    secrets = MANIFEST["secrets"] + [{"name": "jwt", "format": "hex"}]
    assert "secret name 'jwt'" in schema_errors(manifest(secrets=secrets))
    secrets = MANIFEST["secrets"] + [{"name": "X_KEY", "format": "hex", "length": 64}]
    assert "unknown key 'length'" in schema_errors(manifest(secrets=secrets))


def test_declared_secret_must_be_used():
    secrets = MANIFEST["secrets"] + [{"name": "UNUSED_KEY", "format": "hex"}]
    assert "secret UNUSED_KEY is never used" in schema_errors(manifest(secrets=secrets))


def test_env_values_must_be_strings():
    env = dict(MANIFEST["env"], ALLOW_REGISTRATION=False)
    assert "env.ALLOW_REGISTRATION must be a string" in schema_errors(manifest(env=env))
    env = dict(MANIFEST["env"], PORT=8000)
    assert "env.PORT must be a string" in schema_errors(manifest(env=env))


def test_env_names_must_be_valid():
    env = dict(MANIFEST["env"], **{"bad-name": "x"})
    assert "env name 'bad-name'" in schema_errors(manifest(env=env))


def test_unknown_placeholder_is_an_error():
    env = dict(MANIFEST["env"], X="{{tenant_id}}")
    assert "unknown placeholder {{tenant_id}}" in schema_errors(manifest(env=env))
    env = dict(MANIFEST["env"], X="{{.Subdomain}}")
    assert "unknown placeholder {{.Subdomain}}" in schema_errors(manifest(env=env))


def test_secret_placeholder_must_name_a_declared_secret():
    env = dict(MANIFEST["env"], X="{{secret.NOT_DECLARED}}")
    assert "{{secret.NOT_DECLARED}} is not declared under secrets" in schema_errors(manifest(env=env))


def test_placeholders_are_also_checked_in_service_environment():
    services = copy.deepcopy(MANIFEST["services"])
    services["frontend"]["environment"]["X"] = "{{nope}}"
    assert "services.frontend.environment.X: unknown placeholder {{nope}}" in schema_errors(manifest(services=services))


# --- schema: services --------------------------------------------------------------------------------------------

def test_service_needs_an_image_component():
    services = copy.deepcopy(MANIFEST["services"])
    del services["worker"]["image"]
    assert "services.worker.image is missing" in schema_errors(manifest(services=services))
    services["worker"]["image"] = "ghcr.io/scalio-cloud/scalio-example-backend:1.0"
    assert "services.worker.image" in schema_errors(manifest(services=services))


def test_mongo_service_is_added_by_the_installer():
    services = copy.deepcopy(MANIFEST["services"])
    services["mongo"] = {"image": "mongo"}
    assert "'mongo' is added by the installer" in schema_errors(manifest(services=services))


@pytest.mark.parametrize("key,value", [
    ("ports", ["8080:80"]),
    ("privileged", True),
    ("network_mode", "host"),
    ("volumes", ["/var/run/docker.sock:/var/run/docker.sock"]),
    ("cap_add", ["SYS_ADMIN"]),
    ("user", "root"),
])
def test_forbidden_and_unknown_service_keys(key, value):
    services = copy.deepcopy(MANIFEST["services"])
    services["backend"][key] = value
    assert f"services.backend: unknown key '{key}'" in schema_errors(manifest(services=services))


def test_env_common_is_the_only_value_and_needs_top_level_env():
    services = copy.deepcopy(MANIFEST["services"])
    services["worker"]["env"] = "all"
    assert "services.worker.env must be 'common'" in schema_errors(manifest(services=services))
    assert "env: common but there is no top-level env" in schema_errors(manifest(env=None))


def test_command_must_be_a_list_of_strings():
    services = copy.deepcopy(MANIFEST["services"])
    services["worker"]["command"] = "python -m worker"
    assert "services.worker.command" in schema_errors(manifest(services=services))


def test_healthcheck_needs_a_path_and_an_expose_port():
    services = copy.deepcopy(MANIFEST["services"])
    services["backend"]["healthcheck"] = "api/health"
    assert "services.backend.healthcheck must be a path" in schema_errors(manifest(services=services))
    services["backend"]["healthcheck"] = "/api/health"
    del services["backend"]["expose"]
    assert "services.backend.healthcheck needs expose" in schema_errors(manifest(services=services))
    services["backend"]["expose"] = 99999
    assert "services.backend.expose" in schema_errors(manifest(services=services))


def test_at_least_one_healthcheck():
    services = copy.deepcopy(MANIFEST["services"])
    del services["backend"]["healthcheck"]
    assert "no service has a healthcheck" in schema_errors(manifest(services=services))


# --- schema: volumes ---------------------------------------------------------------------------------------------

def test_volumes_need_a_known_service_and_an_absolute_path():
    vols = MANIFEST["volumes"] + [{"service": "cache", "path": "/data"}]
    assert "volumes[2].service 'cache' is not a service" in schema_errors(manifest(volumes=vols))
    vols = MANIFEST["volumes"] + [{"service": "backend", "path": "data"}]
    assert "volumes[2].path must be an absolute path" in schema_errors(manifest(volumes=vols))
    vols = MANIFEST["volumes"] + [{"service": "backend", "path": "/data/storage"}]
    assert "volumes[2] is a duplicate" in schema_errors(manifest(volumes=vols))
    vols = MANIFEST["volumes"] + [{"service": "backend", "path": "/x", "host": "/srv"}]
    assert "volumes[2]: unknown key 'host'" in schema_errors(manifest(volumes=vols))


@pytest.mark.parametrize("path", ["/var/run/docker.sock", "/run/docker.sock", "/data/../etc"])
def test_volumes_never_mount_the_docker_socket_or_leave_their_path(path):
    vols = [{"service": "backend", "path": path}]
    assert "volumes[0].path" in schema_errors(manifest(volumes=vols))


# --- comparison with the engine template -------------------------------------------------------------------------

def test_services_must_match_without_mongo():
    t = template()
    t["services"] = [s for s in t["services"] if not s["name"].endswith("_worker")]
    assert "service worker is in the manifest but not in docker.json" in compare_errors(t=t)
    services = copy.deepcopy(MANIFEST["services"])
    del services["worker"]
    vols = [v for v in MANIFEST["volumes"] if v["service"] != "worker"]
    assert "service worker is in docker.json but not in the manifest" in compare_errors(
        manifest(services=services, volumes=vols))


def test_image_component_must_match():
    t = template()
    engine_service(t, "worker")["image"] = "ghcr.io/scalio-cloud/scalio-example-worker:{{.AppVersion}}"
    assert "service worker: image 'backend' in the manifest, 'worker' in docker.json" in compare_errors(t=t)


def test_image_of_another_app_is_an_error():
    t = template()
    engine_service(t, "backend")["image"] = "ghcr.io/scalio-cloud/scalio-citadel-backend:{{.AppVersion}}"
    assert "service backend: image" in compare_errors(t=t)


def test_selfhosted_image_names_are_recognised():
    t = template()
    for s in t["services"][1:]:
        s["image"] = s["image"].replace("scalio-example-", "selfhosted-scalioexample-")
    assert sm.compare(manifest(), t) == []


def test_command_must_match():
    t = template()
    engine_service(t, "worker")["command"] = ["python", "-m", "worker", "--fast"]
    assert "service worker: command" in compare_errors(t=t)
    services = copy.deepcopy(MANIFEST["services"])
    services["backend"]["command"] = ["uvicorn", "app:app"]
    assert "service backend: command" in compare_errors(manifest(services=services))


def test_every_engine_env_is_in_the_manifest_or_saas_only():
    t = template()
    engine_service(t, "backend")["envs"].append("NEW_FEATURE_FLAG=true")
    assert "service backend: NEW_FEATURE_FLAG from docker.json is missing in the manifest" in compare_errors(t=t)


def test_env_of_a_service_without_env_common_is_checked_against_its_environment():
    t = template()
    engine_service(t, "frontend")["envs"].append("MONGO_URL=x")
    assert "service frontend: MONGO_URL from docker.json is missing in the manifest" in compare_errors(t=t)


@pytest.mark.parametrize("env", ["SAAS_MODE=true", "SAAS_TENANTID={{.ID}}", "PROVISION_PUBLIC_KEY=x",
                                 "ERROR_REPORT_URL=x", "SCALIO_ID_ISSUER=x", "SCALIO_STAGE=x", "SUBDOMAIN=x",
                                 "JWT_SECRET_PREVIOUS={{.JWTSecretPrevious}}"])
def test_saas_only_variables_are_ignored(env):
    services = copy.deepcopy(MANIFEST["services"])
    t = template()
    engine_service(t, "frontend")["envs"].append(env)
    assert sm.compare(manifest(services=services), t) == []


def test_engine_license_belongs_to_license_env():
    assert "EXAMPLE_LICENSE_KEY gets the license in docker.json, but license_env is missing" in compare_errors(
        manifest(license_env=None, env=dict(MANIFEST["env"])))
    assert "EXAMPLE_LICENSE_KEY gets the license in docker.json, but license_env is 'OTHER_LICENSE'" in compare_errors(
        manifest(license_env="OTHER_LICENSE"))


def test_saas_license_variable_is_fine_with_any_license_env():
    t = template()
    for name in ("backend", "worker"):
        envs = engine_service(t, name)["envs"]
        envs[envs.index("EXAMPLE_LICENSE_KEY={{.License}}")] = "SAAS_LICENSE={{.License}}"
    assert sm.compare(manifest(), t) == []


def test_engine_secrets_must_be_declared_with_the_same_format():
    secrets = [s for s in MANIFEST["secrets"] if s["name"] != "BLOB_MASTER_KEY"]
    env = dict(MANIFEST["env"], BLOB_MASTER_KEY="fixed")
    assert ("service backend: BLOB_MASTER_KEY is a secret in docker.json (b64), "
            "the manifest must set it to {{secret.BLOB_MASTER_KEY}}") in compare_errors(
        manifest(secrets=secrets, env=env))
    secrets = [dict(s, format="hex") if s["name"] == "EXAMPLE_ENCRYPTION_KEY" else s for s in MANIFEST["secrets"]]
    assert "EXAMPLE_ENCRYPTION_KEY: format 'hex' in the manifest, 'fernet' in docker.json" in compare_errors(
        manifest(secrets=secrets))


def test_jwt_secret_counts_as_hex_secret():
    secrets = [dict(s, format="b64") if s["name"] == "JWT_SECRET" else s for s in MANIFEST["secrets"]]
    assert "JWT_SECRET: format 'b64' in the manifest, 'hex' in docker.json" in compare_errors(manifest(secrets=secrets))


def test_public_service_and_port_match_the_caddy_label():
    assert "public is 'backend', docker.json routes the hostname to 'frontend'" in compare_errors(
        manifest(public="backend"))
    assert "port is 8080, docker.json routes to port 80" in compare_errors(manifest(port=8080))


def test_engine_volumes_must_be_in_the_manifest():
    vols = [v for v in MANIFEST["volumes"] if v["service"] != "worker"]
    assert "service worker: volume /data/storage from docker.json is missing in the manifest" in compare_errors(
        manifest(volumes=vols))


def test_mongo_database_matches_the_engine():
    t = template()
    t["services"] = [s for s in t["services"] if not s["name"].endswith("_mongo")]
    assert "database is 'mongo', docker.json has no mongo service" in compare_errors(t=t)


def test_template_without_services_is_an_error():
    assert "docker.json has no services" in compare_errors(t={"services": []})
