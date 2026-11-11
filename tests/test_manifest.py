import copy
import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import selfhosted_manifest as sm  # noqa: E402

DATA = Path(__file__).resolve().parent / "data"


@pytest.fixture
def manifest():
    return sm.load(DATA / "citadel-scalio-app.yml")


@pytest.fixture
def template():
    return json.loads((DATA / "citadel-docker.json").read_text())


def test_example_is_valid_and_matches_the_engine(manifest, template):
    assert sm.validate(manifest) == []
    assert sm.compare(manifest, template) == []


def test_unknown_keys_and_host_ports_are_refused(manifest):
    m = copy.deepcopy(manifest)
    m["services"]["backend"]["ports"] = ["8001:8001"]
    m["privileged"] = True
    err = sm.validate(m)
    assert "services.backend: key ports is not allowed" in err
    assert "unknown key privileged" in err


def test_placeholders_must_be_known_and_secrets_declared(manifest):
    m = copy.deepcopy(manifest)
    m["env"]["X"] = "{{password}}"
    m["env"]["Y"] = "{{secret.NOPE}}"
    err = sm.validate(m)
    assert "env.X uses unknown placeholder {{password}}" in err
    assert "env.Y uses {{secret.NOPE}}, but NOPE is not under secrets" in err


def test_unused_secret_and_bad_format(manifest):
    m = copy.deepcopy(manifest)
    m["secrets"].append({"name": "OTHER", "format": "pem"})
    err = sm.validate(m)
    assert any("format must be one of" in e for e in err)
    assert "secret OTHER is never used" in err


def test_public_must_be_a_service(manifest):
    m = copy.deepcopy(manifest)
    m["public"] = "web"
    assert "public must name one of the services" in sm.validate(m)


def test_missing_engine_variable_is_reported(manifest, template):
    m = copy.deepcopy(manifest)
    del m["env"]["ALLOW_REGISTRATION"]
    err = sm.compare(m, template)
    assert "backend: ALLOW_REGISTRATION is set by the engine template but missing in the manifest" in err


def test_saas_only_variables_are_ignored(manifest, template):
    t = copy.deepcopy(template)
    t["services"][1]["envs"] += ["SAAS_TENANTID=x", "PROVISION_FOO=y", "SCALIO_STAGE=dev"]
    assert sm.compare(manifest, t) == []


def test_derived_secret_needs_matching_format(manifest, template):
    t = copy.deepcopy(template)
    t["services"][1]["envs"].append("BLOB_MASTER_KEY={{deriveB64 .KeySeed `blob-master`}}")
    m = copy.deepcopy(manifest)
    m["services"]["backend"]["environment"] = {"BLOB_MASTER_KEY": "{{secret.BLOB}}"}
    m["secrets"].append({"name": "BLOB", "format": "hex"})
    err = sm.compare(m, t)
    assert "backend: BLOB_MASTER_KEY is derived by the engine, list it under secrets with format b64" in err


def test_service_and_command_differences(manifest, template):
    m = copy.deepcopy(manifest)
    del m["services"]["worker"]
    m["services"]["frontend"]["image"] = "web"
    err = sm.compare(m, template)
    assert "service worker is in the engine template but not in the manifest" in err
    assert "frontend: image web in the manifest, frontend in the engine template" in err


def test_license_must_be_license_env(manifest, template):
    m = copy.deepcopy(manifest)
    m["license_env"] = "OTHER_KEY"
    err = sm.compare(m, template)
    assert any("CITADEL_LICENSE_KEY" in e for e in err)


def test_check_repo(tmp_path):
    (tmp_path / "selfhosted").mkdir()
    (tmp_path / "backend" / "tenant_template").mkdir(parents=True)
    (tmp_path / "selfhosted" / "scalio-app.yml").write_text((DATA / "citadel-scalio-app.yml").read_text())
    (tmp_path / "backend" / "tenant_template" / "docker.json").write_text((DATA / "citadel-docker.json").read_text())
    assert sm.check_repo(tmp_path) == []
    assert sm.main(["check", str(tmp_path)]) == 0
    assert sm.check_repo(tmp_path / "nothing")[0].endswith("is missing")


def test_broken_yaml(tmp_path):
    f = tmp_path / "x.yml"
    f.write_text(yaml.safe_dump(["a"]))
    assert sm.main(["validate", str(f)]) == 1
