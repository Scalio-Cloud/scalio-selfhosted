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


LAB = {c: f"ghcr.io/scalio-cloud/scalio-citadel-dev-{c}@{DIGEST}" for c in ("backend", "frontend")}


def _set(tmp_path, key, channel="dev", version="dev-1a2b3c4", images=LAB, now="2026-11-12T18:00:00Z"):
    return si.set_lab(tmp_path, channel, "citadel", version, DATA / "citadel-scalio-app.yml", images, key, now=now)


def test_lab_channel_keeps_one_entry_per_app(tmp_path, key):
    _set(tmp_path, key, version="dev-1a2b3c4")
    _set(tmp_path, key, version="dev-5d6e7f8", now="2026-11-12T19:00:00Z")
    index = si.verify(tmp_path, si.public_key_b64(key))
    assert index["channel"] == "dev"
    assert [v["version"] for v in index["apps"]["citadel"]] == ["dev-5d6e7f8"]
    assert index["apps"]["citadel"][0]["released"] == "2026-11-12T19:00:00Z"
    assert sorted(p.name for p in (tmp_path / "citadel").iterdir()) == ["dev-5d6e7f8.yml"]


def test_lab_channel_checks_versions_and_images(tmp_path, key):
    with pytest.raises(ValueError, match="not a dev build"):
        _set(tmp_path, key, version="0.4.0")
    with pytest.raises(ValueError, match="not a rc build"):
        _set(tmp_path, key, channel="rc", version="dev-1a2b3c4")
    with pytest.raises(ValueError, match="only dev, rc"):
        _set(tmp_path, key, channel="public")
    with pytest.raises(ValueError, match="scalio-citadel-dev-backend"):
        _set(tmp_path, key, images={**LAB, "backend": IMAGES["backend"]})
    _set(tmp_path / "rc", key, channel="rc", version="0.3.3rc1")
    assert si.read(tmp_path / "rc", "rc")["apps"]["citadel"][0]["version"] == "0.3.3rc1"


def test_channels_never_mix(tmp_path, key):
    _set(tmp_path / "dev", key)
    with pytest.raises(ValueError, match="channel dev, not public"):
        si.add(tmp_path / "dev", "citadel", "0.4.0", DATA / "citadel-scalio-app.yml", IMAGES, "", key)
    _add(tmp_path / "pub", key)
    with pytest.raises(ValueError, match="channel public, not dev"):
        _set(tmp_path / "pub", key)


# --- channel prerelease (Vorabversion) -------------------------------------------------------------------------------

def _pre(tmp_path, key, version="0.5.0rc2", images=IMAGES):
    return si.add(tmp_path, "citadel", version, DATA / "citadel-scalio-app.yml", images,
                  "https://example.invalid/notes", key, released="2026-11-12", now="2026-11-12T08:00:00Z",
                  chan="prerelease")


def test_version_order_knows_release_candidates():
    order = ["0.4.0", "0.5.0rc1", "0.5.0rc2", "0.5.0rc10", "0.5.0", "0.5.1rc1", "0.10.0"]
    assert sorted(reversed(order), key=si._version_key) == order
    with pytest.raises(ValueError, match="not like"):
        si._version_key("0.5.0-rc1")


def test_prerelease_entry_carries_the_channel(tmp_path, key):
    _add(tmp_path, key, "0.4.0")
    _pre(tmp_path, key, "0.5.0rc2")
    index = si.verify(tmp_path, si.public_key_b64(key))
    rc, final = index["apps"]["citadel"]
    assert rc["version"] == "0.5.0rc2" and rc["channel"] == "prerelease"
    assert "channel" not in final                      # stable entries stay as before (missing = stable)
    assert si.channel_of(final) == "stable" and si.newest(index["apps"]["citadel"]) == "0.4.0"
    assert si.newest(index["apps"]["citadel"], "prerelease") == "0.5.0rc2"


def test_each_channel_takes_only_its_versions(tmp_path, key):
    with pytest.raises(ValueError, match="only a release candidate"):
        _pre(tmp_path, key, "0.5.0")
    with pytest.raises(ValueError, match="only final versions"):
        _add(tmp_path, key, "0.5.0rc2")
    with pytest.raises(ValueError, match="channel must be"):
        si.add(tmp_path, "citadel", "0.5.0", DATA / "citadel-scalio-app.yml", IMAGES, "", key, chan="beta")


def test_prerelease_must_be_ahead_of_the_release(tmp_path, key):
    _add(tmp_path, key, "0.5.0")
    for old in ("0.5.0rc3", "0.4.1rc1"):
        with pytest.raises(ValueError, match="not newer than the release 0.5.0"):
            _pre(tmp_path, key, old)
    _pre(tmp_path, key, "0.5.1rc1")


def test_release_of_a_prerelease_is_a_new_stable_entry(tmp_path, key):
    """Build once: 0.5.0 has the digests of 0.5.0rc2; the prerelease entries are never changed."""
    _add(tmp_path, key, "0.4.0")
    _pre(tmp_path, key, "0.5.0rc1")
    _pre(tmp_path, key, "0.5.0rc2")
    before = [dict(v) for v in si.read(tmp_path)["apps"]["citadel"] if v["version"].startswith("0.5.0rc")]
    _add(tmp_path, key, "0.5.0")
    versions = si.verify(tmp_path, si.public_key_b64(key))["apps"]["citadel"]
    assert [v["version"] for v in versions] == ["0.5.0", "0.5.0rc2", "0.5.0rc1", "0.4.0"]
    assert [v for v in versions if v["version"].startswith("0.5.0rc")] == before
    assert "channel" not in versions[0] and versions[0]["images"] == versions[1]["images"]


def test_plan(tmp_path, key):
    index = si.read(tmp_path)
    assert si.plan(index, "citadel", "0.4.0", "release", set()) == {
        "channel": "stable", "latest": "true", "prerelease": "false"}
    _add(tmp_path, key, "0.4.0")
    index = si.read(tmp_path)
    assert si.plan(index, "citadel", "0.3.9", "stable", set())["latest"] == "false"     # fix of an old line
    assert si.plan(index, "citadel", "0.5.0rc2", "prerelease", {"citadel"}) == {
        "channel": "prerelease", "latest": "false", "prerelease": "true"}
    for args, msg in ((("citadel", "0.5.0rc2", "prerelease", {"rosetta"}), "prerelease: true"),
                      (("citadel", "0.4.0", "release", set()), "already in the index"),
                      (("citadel", "0.4.0rc1", "prerelease", {"citadel"}), "not newer than the release"),
                      (("citadel", "0.5.0", "prerelease", {"citadel"}), "only a release candidate"),
                      (("citadel", "0.5.0rc1", "release", set()), "only final versions"),
                      (("citadel", "0.5.0", "beta", set()), "channel must be"),
                      (("../x", "0.5.0", "release", set()), "plain name")):
        with pytest.raises(ValueError, match=msg):
            si.plan(index, *args)


def test_withdrawn_versions_do_not_count(tmp_path, key):
    _add(tmp_path, key, "0.4.0")
    _add(tmp_path, key, "0.5.0")
    si.withdraw(tmp_path, "citadel", "0.5.0", "Fehler beim Update", key)
    index = si.read(tmp_path)
    assert si.newest(index["apps"]["citadel"]) == "0.4.0"
    assert si.plan(index, "citadel", "0.5.1rc1", "prerelease", {"citadel"})["channel"] == "prerelease"
    assert si.plan(index, "citadel", "0.4.1", "release", set())["latest"] == "true"


def test_withdraw_a_prerelease_needs_a_reason_and_happens_once(tmp_path, key):
    _pre(tmp_path, key)
    with pytest.raises(ValueError, match="reason"):
        si.withdraw(tmp_path, "citadel", "0.5.0rc2", "  ", key)
    si.withdraw(tmp_path, "citadel", "0.5.0rc2", "Migration fehlerhaft", key, now="2026-11-13T08:00:00Z")
    entry = si.verify(tmp_path, si.public_key_b64(key))["apps"]["citadel"][0]
    assert entry["withdrawn"] == "Migration fehlerhaft" and entry["channel"] == "prerelease"
    with pytest.raises(ValueError, match="already withdrawn"):
        si.withdraw(tmp_path, "citadel", "0.5.0rc2", "nochmals", key)
    with pytest.raises(ValueError, match="not in the index"):
        si.withdraw(tmp_path, "citadel", "0.5.0rc3", "gibt es nicht", key)


def test_cli_plan_and_prerelease(tmp_path, key, monkeypatch, capsys):
    monkeypatch.delenv("SELFHOSTED_SIGNING_KEY", raising=False)
    plan = ["plan", "--app", "citadel", "--version", "0.5.0rc2", "--channel", "prerelease",
            "--index-dir", str(tmp_path)]
    assert si.main(plan + ["--allowed", "citadel rosetta"]) == 0           # no key needed
    assert capsys.readouterr().out == "channel=prerelease\nlatest=false\nprerelease=true\n"
    assert si.main(plan) == 1
    assert "prerelease: true" in capsys.readouterr().err
    monkeypatch.setenv("SELFHOSTED_SIGNING_KEY", base64.b64encode(bytes(range(32))).decode())
    args = ["add", "--app", "citadel", "--version", "0.5.0rc2", "--channel", "prerelease",
            "--manifest", str(DATA / "citadel-scalio-app.yml"), "--index-dir", str(tmp_path)]
    assert si.main(args + [f"--image={k}={v}" for k, v in IMAGES.items()]) == 0
    assert "added citadel 0.5.0rc2 (prerelease)" in capsys.readouterr().out
    assert si.read(tmp_path)["apps"]["citadel"][0]["channel"] == "prerelease"
