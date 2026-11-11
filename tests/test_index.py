import base64
import json
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import selfhosted_index as si  # noqa: E402

DATA = Path(__file__).resolve().parent / "data"
DIGEST = "sha256:" + "a" * 64
NEXUS = f"ghcr.io/scalio-cloud/selfhosted-scalionexus-backend@{DIGEST}"
IMAGES = {
    "backend": f"ghcr.io/scalio-cloud/selfhosted-scaliocitadel-backend@{DIGEST}",
    "frontend": f"ghcr.io/scalio-cloud/selfhosted-scaliocitadel-frontend@{DIGEST}",
}


@pytest.fixture
def key():
    return si.private_key(base64.b64encode(bytes(range(32))).decode())


def _add(tmp_path, key, version="0.4.0", images=IMAGES):
    return si.add(tmp_path, "citadel", version, DATA / "citadel-scalio-app.yml", images,
                  "https://example.invalid/notes", key, released="2026-11-11", now="2026-11-11T21:00:00Z")


def test_add_signs_and_verifies(tmp_path, key):
    _add(tmp_path, key)
    pub = si.public_key_b64(key)
    index = si.verify(tmp_path, pub)
    entry = index["apps"]["citadel"][0]
    assert entry["version"] == "0.4.0" and entry["setup"] == "0.1.0"
    assert entry["images"] == IMAGES
    assert (tmp_path / "citadel" / "0.4.0.yml").read_bytes() == (DATA / "citadel-scalio-app.yml").read_bytes()
    assert index["key"] == si.key_id(pub)


def test_newest_first_and_never_twice(tmp_path, key):
    _add(tmp_path, key, "0.4.0")
    _add(tmp_path, key, "0.10.0")
    _add(tmp_path, key, "0.5.1")
    index = si.read(tmp_path)
    assert [v["version"] for v in index["apps"]["citadel"]] == ["0.10.0", "0.5.1", "0.4.0"]
    with pytest.raises(ValueError, match="never changes"):
        _add(tmp_path, key, "0.5.1")


def test_tampered_index_or_other_key_is_refused(tmp_path, key):
    _add(tmp_path, key)
    pub = si.public_key_b64(key)
    raw = json.loads((tmp_path / "index.json").read_text())
    raw["apps"]["citadel"][0]["images"]["backend"] = "ghcr.io/evil/backend@" + DIGEST
    (tmp_path / "index.json").write_bytes(si.dumps(raw))
    with pytest.raises(ValueError, match="signature"):
        si.verify(tmp_path, pub)
    _add(tmp_path / "other", key)
    other = si.public_key_b64(Ed25519PrivateKey.generate())
    with pytest.raises(ValueError, match="signature"):
        si.verify(tmp_path / "other", other)


def test_images_must_be_digests_of_the_app(tmp_path, key):
    with pytest.raises(ValueError, match="sha256"):
        _add(tmp_path, key, images={**IMAGES, "backend": "ghcr.io/scalio-cloud/selfhosted-scaliocitadel-backend:0.4.0"})
    with pytest.raises(ValueError, match="sha256"):
        _add(tmp_path, key, images={**IMAGES, "backend": NEXUS})
    with pytest.raises(ValueError, match="do not match"):
        _add(tmp_path, key, images={"backend": IMAGES["backend"]})
    with pytest.raises(ValueError, match="final versions"):
        _add(tmp_path, key, version="0.4.0rc1")


def test_withdraw(tmp_path, key):
    _add(tmp_path, key)
    si.withdraw(tmp_path, "citadel", "0.4.0", "Fehler beim Update", key)
    index = si.verify(tmp_path, si.public_key_b64(key))
    assert index["apps"]["citadel"][0]["withdrawn"] == "Fehler beim Update"


def test_cli(tmp_path, key, monkeypatch, capsys):
    assert si.main(["keygen"]) == 0
    assert "SELFHOSTED_SIGNING_KEY=" in capsys.readouterr().out
    monkeypatch.delenv("SELFHOSTED_SIGNING_KEY", raising=False)
    args = ["add", "--app", "citadel", "--version", "0.4.0", "--manifest", str(DATA / "citadel-scalio-app.yml"),
            "--index-dir", str(tmp_path)] + [f"--image={k}={v}" for k, v in IMAGES.items()]
    assert si.main(args) == 1
    monkeypatch.setenv("SELFHOSTED_SIGNING_KEY", base64.b64encode(bytes(range(32))).decode())
    assert si.main(args) == 0
    assert si.main(["verify", "--public-key", si.public_key_b64(key), "--index-dir", str(tmp_path)]) == 0
