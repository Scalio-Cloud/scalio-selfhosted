"""Signed release index of the self-hosted Scalio apps (format scalio.selfhosted-index/v1).

Scalio Installer at a customer's site reads index/index.json of this repo, checks index/index.json.sig with the public
key built into the Installer and only then pulls images, by digest. Format: README.md, section «Release-Index».

  python tools/selfhosted_index.py keygen
      new signing key: prints the seed (secret SELFHOSTED_SIGNING_KEY, 1Password) and the public key (for the Installer)
  python tools/selfhosted_index.py plan --app citadel --version 1.3.0rc2 --channel prerelease --allowed "citadel"
      checks a release before anything is copied (no key needed) and prints key=value lines for $GITHUB_OUTPUT:
      channel (stable|prerelease), latest (move the :latest image tag), prerelease (GitHub release as prerelease)
  python tools/selfhosted_index.py add --app citadel --version 0.4.0 --manifest scalio-app.yml \\
      --image backend=ghcr.io/...@sha256:... --image frontend=... [--channel stable|prerelease] [--notes URL]
      [--index-dir index]
      adds the version (refused if it exists), copies the manifest to index/<app>/<version>.yml, signs
      (seed from the environment variable SELFHOSTED_SIGNING_KEY)
  python tools/selfhosted_index.py withdraw --app citadel --version 0.4.0 --reason "…"
  python tools/selfhosted_index.py set --channel dev --app citadel --version dev-1a2b3c4 --manifest scalio-app.yml \
      --image backend=ghcr.io/scalio-cloud/scalio-citadel-dev-backend@sha256:... [--index-dir index]
      lab channels dev / rc only (branches channel-dev / channel-rc, staging key, selfhost/H21): replaces the one
      entry of the app with the newest dev build or release candidate
  python tools/selfhosted_index.py verify --public-key <base64> [--index-dir index]

Channels (prerelease): `stable` (final versions like 1.3.0, no field in the entry) and
`prerelease` (Vorabversion: a release candidate like 1.3.0rc2, entry with "channel": "prerelease"; the Installer
offers it only to apps whose admin switched on «Vorabversionen anzeigen»). Order: 1.3.0rc1 < 1.3.0rc2 < 1.3.0.
The release of the same version is a new stable entry with the same digests (build once); the prerelease entries
stay as they are. A version is never changed, only withdrawn.

The signature covers the exact bytes of index.json; the file is written with sorted keys and a final newline so the
same content always gives the same bytes.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import re
import sys
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

sys.path.insert(0, str(Path(__file__).resolve().parent))
import selfhosted_manifest  # noqa: E402

FORMAT = "scalio.selfhosted-index/v1"
_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
_RC = re.compile(r"^\d+\.\d+\.\d+rc\d+$")
_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:rc(\d+))?$")
_APP = re.compile(r"^[a-z][a-z0-9]*$")
STABLE, PRERELEASE = "stable", "prerelease"
# The workflows say «release» for the stable channel (input channel of app-publish.yml and app-selfhosted.yml).
_CHANNELS = {"stable": STABLE, "release": STABLE, "prerelease": PRERELEASE}
_DIGEST_REF = re.compile(r"^ghcr\.io/scalio-cloud/selfhosted-scalio[a-z0-9]+-[a-z]+@sha256:[0-9a-f]{64}$")
# Lab channels: the private build images of the app, newest dev build (dev-<sha>) or release candidate.
LAB_CHANNELS = {"dev": re.compile(r"^dev-[0-9a-f]{7}$"), "rc": re.compile(r"^\d+\.\d+\.\d+rc\d+$")}
_LAB_REF = re.compile(r"^ghcr\.io/scalio-cloud/scalio-[a-z0-9]+-dev-[a-z]+@sha256:[0-9a-f]{64}$")


def _version_key(v: str) -> tuple[int, ...]:
    """1.3.0rc1 < 1.3.0rc2 < 1.3.0 < 1.3.1rc1 < 1.10.0."""
    m = _VERSION.match(v)
    if not m:
        raise ValueError(f"version {v!r} is not like 1.3.0 or 1.3.0rc2")
    major, minor, patch, rc = m.groups()
    return (int(major), int(minor), int(patch)) + ((0, int(rc)) if rc is not None else (1, 0))


def channel(name: str) -> str:
    """Index channel for a channel name of the tool or the workflows; raises ValueError for others."""
    if name not in _CHANNELS:
        raise ValueError(f"channel must be stable (release) or prerelease, got {name!r}")
    return _CHANNELS[name]


def channel_of(entry: dict) -> str:
    """Channel of an index entry; entries without the field are stable."""
    return entry.get("channel", STABLE)


def newest(versions: list[dict], chan: str = STABLE) -> str | None:
    """Newest version of a channel that is not withdrawn."""
    live = [v["version"] for v in versions if channel_of(v) == chan and not v.get("withdrawn")]
    return max(live, key=_version_key, default=None)


def check_version(version: str, chan: str) -> None:
    if chan == STABLE and not _SEMVER.match(version):
        raise ValueError(f"version {version!r}: only final versions like 0.4.0 (prerelease: --channel prerelease)")
    if chan == PRERELEASE and not _RC.match(version):
        raise ValueError(f"version {version!r}: channel prerelease takes only a release candidate like 0.4.0rc2")


def _check_prerelease_is_ahead(versions: list[dict], app: str, version: str) -> None:
    """A Vorabversion must be newer than the newest release: nobody could update to it (no downgrade, ADR 0115)."""
    best = newest(versions, STABLE)
    if best and _version_key(version) <= _version_key(best):
        raise ValueError(f"{app} {version} is not newer than the release {best} in the index, no prerelease")


def plan(index: dict, app: str, version: str, chan: str, allowed: set[str]) -> dict[str, str]:
    """What «Für Kunden freigeben» does for this version and channel; raises ValueError when it must not run."""
    chan = channel(chan)
    if not _APP.match(app or ""):
        raise ValueError(f"app {app!r} is not a plain name")
    check_version(version, chan)
    versions = index["apps"].get(app, [])
    if any(v["version"] == version for v in versions):
        raise ValueError(f"{app} {version} is already in the index; a version never changes")
    if chan == PRERELEASE:
        if app not in allowed:
            raise ValueError(f"{app} has no 'prerelease: true' in scalio-infra releases/products.yml, no prerelease")
        _check_prerelease_is_ahead(versions, app, version)
        return {"channel": PRERELEASE, "latest": "false", "prerelease": "true"}
    best = newest(versions, STABLE)
    latest = best is None or _version_key(version) > _version_key(best)
    return {"channel": STABLE, "latest": "true" if latest else "false", "prerelease": "false"}


def dumps(index: dict) -> bytes:
    return (json.dumps(index, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def private_key(seed_b64: str) -> Ed25519PrivateKey:
    seed = base64.b64decode(seed_b64.strip(), validate=True)
    if len(seed) != 32:
        raise ValueError("SELFHOSTED_SIGNING_KEY must be 32 bytes in Base64")
    return Ed25519PrivateKey.from_private_bytes(seed)


def public_key_b64(key: Ed25519PrivateKey) -> str:
    return base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()


def key_id(public_b64: str) -> str:
    return hashlib.sha256(base64.b64decode(public_b64)).hexdigest()[:12]


def read(index_dir: Path, channel: str = "public") -> dict:
    path = index_dir / "index.json"
    if not path.is_file():
        return {"format": FORMAT, "apps": {}} | ({} if channel == "public" else {"channel": channel})
    index = json.loads(path.read_text(encoding="utf-8"))
    if index.get("format") != FORMAT:
        raise ValueError(f"{path}: format must be {FORMAT}")
    if index.get("channel", "public") != channel:
        raise ValueError(f"{path} is the index of channel {index.get('channel', 'public')}, not {channel}")
    return index


def write_signed(index_dir: Path, index: dict, key: Ed25519PrivateKey, now: str | None = None) -> None:
    index["updated"] = now or dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    index["key"] = key_id(public_key_b64(key))
    raw = dumps(index)
    index_dir.mkdir(parents=True, exist_ok=True)
    (index_dir / "index.json").write_bytes(raw)
    (index_dir / "index.json.sig").write_text(base64.b64encode(key.sign(raw)).decode() + "\n", encoding="utf-8")


def verify(index_dir: Path, public_b64: str) -> dict:
    """Index after a valid signature check; raises ValueError otherwise."""
    raw = (index_dir / "index.json").read_bytes()
    sig = base64.b64decode((index_dir / "index.json.sig").read_text(encoding="utf-8").strip(), validate=True)
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(public_b64)).verify(sig, raw)
    except InvalidSignature as e:
        raise ValueError("index.json: signature does not match") from e
    index = json.loads(raw)
    if index.get("format") != FORMAT:
        raise ValueError(f"index.json: format must be {FORMAT}")
    return index


def _checked_manifest(manifest_path: Path, app: str, images: dict[str, str]) -> dict:
    manifest = selfhosted_manifest.load(manifest_path)
    errors = selfhosted_manifest.validate(manifest)
    if errors:
        raise ValueError("manifest: " + "; ".join(errors))
    if manifest["app"] != app:
        raise ValueError(f"manifest is for {manifest['app']}, not {app}")
    wanted = {svc["image"] for svc in manifest["services"].values()}
    if set(images) != wanted:
        raise ValueError(f"images {sorted(images)} do not match the manifest components {sorted(wanted)}")
    return manifest


def add(index_dir: Path, app: str, version: str, manifest_path: Path, images: dict[str, str], notes: str,
        key: Ed25519PrivateKey, released: str | None = None, now: str | None = None, chan: str = STABLE) -> dict:
    chan = channel(chan)
    check_version(version, chan)
    manifest = _checked_manifest(manifest_path, app, images)
    for comp, ref in images.items():
        if not _DIGEST_REF.match(ref) or f"selfhosted-scalio{app}-{comp}@" not in ref:
            want = f"ghcr.io/scalio-cloud/selfhosted-scalio{app}-{comp}@sha256:…"
            raise ValueError(f"image {comp}: {ref!r} is not {want}")
    index = read(index_dir)
    versions = index["apps"].setdefault(app, [])
    if any(v["version"] == version for v in versions):
        raise ValueError(f"{app} {version} is already in the index; a version never changes")
    if chan == PRERELEASE:
        _check_prerelease_is_ahead(versions, app, version)
    raw = manifest_path.read_bytes()
    target = index_dir / app / f"{version}.yml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)
    entry = {
        "version": version,
        "released": released or dt.datetime.now(dt.UTC).date().isoformat(),
        "setup": manifest["setup"],
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "images": dict(sorted(images.items())),
        "notes": notes,
    }
    if chan == PRERELEASE:
        entry["channel"] = PRERELEASE
    versions.append(entry)
    versions.sort(key=lambda v: _version_key(v["version"]), reverse=True)
    write_signed(index_dir, index, key, now)
    return index


def set_lab(index_dir: Path, channel: str, app: str, version: str, manifest_path: Path, images: dict[str, str],
            key: Ed25519PrivateKey, now: str | None = None) -> dict:
    """Lab channel dev or rc: the app's one entry becomes this build (older builds and their manifests go away)."""
    if channel not in LAB_CHANNELS:
        raise ValueError(f"channel {channel!r}: only {', '.join(LAB_CHANNELS)} (public versions use add)")
    if not LAB_CHANNELS[channel].match(version):
        want = "dev-<7 hex>" if channel == "dev" else "1.2.3rc1"
        raise ValueError(f"version {version!r} is not a {channel} build ({want})")
    manifest = _checked_manifest(manifest_path, app, images)
    for comp, ref in images.items():
        if not _LAB_REF.match(ref) or f"/scalio-{app}-dev-{comp}@" not in ref:
            raise ValueError(f"image {comp}: {ref!r} is not ghcr.io/scalio-cloud/scalio-{app}-dev-{comp}@sha256:…")
    index = read(index_dir, channel)
    stamp = now or dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    folder = index_dir / app
    if folder.is_dir():
        for old in folder.glob("*.yml"):
            old.unlink()
    raw = manifest_path.read_bytes()
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{version}.yml").write_bytes(raw)
    index["apps"][app] = [{
        "version": version,
        "released": stamp,
        "setup": manifest["setup"],
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "images": dict(sorted(images.items())),
        "notes": "",
    }]
    write_signed(index_dir, index, key, stamp)
    return index


def withdraw(index_dir: Path, app: str, version: str, reason: str, key: Ed25519PrivateKey,
             now: str | None = None) -> dict:
    """Marks a version as withdrawn (any channel): the Installer no longer offers it, installed apps stay on it."""
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("a reason is needed to withdraw a version")
    index = read(index_dir)
    for v in index["apps"].get(app, []):
        if v["version"] == version:
            if v.get("withdrawn"):
                raise ValueError(f"{app} {version} is already withdrawn: {v['withdrawn']}")
            v["withdrawn"] = reason
            write_signed(index_dir, index, key, now)
            return index
    raise ValueError(f"{app} {version} is not in the index")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("keygen")
    p = sub.add_parser("plan")
    p.add_argument("--app", required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--channel", default=STABLE)
    p.add_argument("--allowed", default="", help="apps that may publish a prerelease, separated by spaces")
    p.add_argument("--index-dir", default="index", type=Path)
    a = sub.add_parser("add")
    a.add_argument("--app", required=True)
    a.add_argument("--version", required=True)
    a.add_argument("--manifest", required=True, type=Path)
    a.add_argument("--image", action="append", default=[], help="component=ghcr.io/…@sha256:…")
    a.add_argument("--notes", default="")
    a.add_argument("--channel", default=STABLE)
    a.add_argument("--index-dir", default="index", type=Path)
    w = sub.add_parser("withdraw")
    w.add_argument("--app", required=True)
    w.add_argument("--version", required=True)
    w.add_argument("--reason", required=True)
    w.add_argument("--index-dir", default="index", type=Path)
    v = sub.add_parser("verify")
    v.add_argument("--public-key", required=True)
    v.add_argument("--channel", default="public", choices=["public", *LAB_CHANNELS])
    v.add_argument("--index-dir", default="index", type=Path)
    s = sub.add_parser("set")
    s.add_argument("--channel", required=True, choices=list(LAB_CHANNELS))
    s.add_argument("--app", required=True)
    s.add_argument("--version", required=True)
    s.add_argument("--manifest", required=True, type=Path)
    s.add_argument("--image", action="append", default=[], help="component=ghcr.io/…@sha256:…")
    s.add_argument("--index-dir", default="index", type=Path)
    args = ap.parse_args(argv)
    try:
        if args.cmd == "keygen":
            key = Ed25519PrivateKey.generate()
            seed = key.private_bytes_raw()
            pub = public_key_b64(key)
            print(f"SELFHOSTED_SIGNING_KEY={base64.b64encode(seed).decode()}")
            print(f"public key (Scalio Installer): {pub}  kid {key_id(pub)}")
            return 0
        if args.cmd == "plan":
            result = plan(read(args.index_dir), args.app.strip(), args.version.strip(), args.channel.strip(),
                          set(args.allowed.split()))
            for k, val in result.items():
                print(f"{k}={val}")
            return 0
        if args.cmd == "verify":
            index = verify(args.index_dir, args.public_key)
            if index.get("channel", "public") != args.channel:
                raise ValueError(f"index is for channel {index.get('channel', 'public')}, not {args.channel}")
            print(f"index ok, {sum(len(v) for v in index['apps'].values())} versions")
            return 0
        seed = os.environ.get("SELFHOSTED_SIGNING_KEY", "")
        if not seed:
            raise ValueError("environment variable SELFHOSTED_SIGNING_KEY is missing")
        key = private_key(seed)
        images = {}
        for item in getattr(args, "image", []):
            comp, _, ref = item.partition("=")
            images[comp] = ref
        if args.cmd == "set":
            set_lab(args.index_dir, args.channel, args.app, args.version, args.manifest, images, key)
            print(f"{args.channel}: {args.app} {args.version}")
        elif args.cmd == "add":
            add(args.index_dir, args.app, args.version, args.manifest, images, args.notes, key, chan=args.channel)
            print(f"added {args.app} {args.version} ({channel(args.channel)})")
        else:
            withdraw(args.index_dir, args.app, args.version, args.reason, key)
            print(f"withdrew {args.app} {args.version}")
        return 0
    except (OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
