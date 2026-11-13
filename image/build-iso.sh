#!/usr/bin/env bash
# Builds the Scalio ISO (selfhost/H23): the official Ubuntu Server ISO, checked against Ubuntu's signed SHA256SUMS,
# plus autoinstall.yaml, get.sh and the first-boot service. Needs curl, gpg and xorriso.
#   image/build-iso.sh [out.iso]        UBUNTU_RELEASE (default 26.04), UBUNTU_ISO_DIR (cache folder, default .iso-cache),
#                                       SCALIO_CHANNEL (public, default; dev/rc for Scalio lab servers)
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
RELEASE="${UBUNTU_RELEASE:-26.04}"
CHANNEL="${SCALIO_CHANNEL:-public}"
case "$CHANNEL" in public|dev|rc) ;; *) echo "SCALIO_CHANNEL must be public, dev or rc" >&2; exit 1 ;; esac
CACHE="${UBUNTU_ISO_DIR:-$ROOT/.iso-cache}"
OUT="${1:-$ROOT/scalio-ubuntu-$RELEASE-amd64.iso}"
BASE="https://releases.ubuntu.com/$RELEASE"
# Ubuntu CD image signing key (https://ubuntu.com/tutorials/how-to-verify-ubuntu)
UBUNTU_KEY="843938DF228D22F7B3742BC0D94AA3F0EFE21092"

mkdir -p "$CACHE"
cd "$CACHE"
curl -fsSLO "$BASE/SHA256SUMS"
curl -fsSLO "$BASE/SHA256SUMS.gpg"
export GNUPGHOME="$CACHE/gnupg"
install -d -m 700 "$GNUPGHOME"
gpg -q --keyserver hkps://keyserver.ubuntu.com --recv-keys "$UBUNTU_KEY"
gpg -q --verify SHA256SUMS.gpg SHA256SUMS
ISO=$(awk '/live-server-amd64\.iso$/ {sub(/^\*/, "", $2); print $2}' SHA256SUMS | tail -n 1)
[ -n "$ISO" ] || { echo "no live-server ISO in $BASE/SHA256SUMS" >&2; exit 1; }
if ! grep " \*\?$ISO\$" SHA256SUMS | sha256sum -c --status 2>/dev/null; then
  curl -fL --retry 3 -o "$ISO" "$BASE/$ISO"
  grep " \*\?$ISO\$" SHA256SUMS | sha256sum -c -
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
install -d "$WORK/scalio"
install -m 0755 "$ROOT/get.sh" "$HERE/scalio-firstboot.sh" "$HERE/scalio-update.sh" "$HERE/scalio-code.sh" "$HERE/scalio-motd.sh" "$WORK/scalio/"
echo "$CHANNEL" > "$WORK/scalio/channel"
install -m 0644 "$HERE/scalio-firstboot.service" "$HERE/50-scalio-unattended" "$WORK/scalio/"
xorriso -osirrox on -indev "$ISO" -extract /boot/grub/grub.cfg "$WORK/grub.cfg" 2>/dev/null
chmod u+w "$WORK/grub.cfg"
# Start the installation right away (autoinstall on the kernel line) and wait 5 seconds in the boot menu.
sed -i -e 's|\(linux[[:space:]]\+/casper/vmlinuz\)[[:space:]]*\(.*\)---|\1 autoinstall \2---|' \
       -e 's|^set timeout=.*|set timeout=5|' \
       -e 's|"Try or Install Ubuntu Server"|"Install Scalio (Ubuntu Server '"$RELEASE"')"|' "$WORK/grub.cfg"
grep -q 'autoinstall' "$WORK/grub.cfg" || { echo "grub.cfg: kernel line not found" >&2; exit 1; }

rm -f "$OUT"
xorriso -indev "$ISO" -outdev "$OUT" \
  -map "$HERE/autoinstall.yaml" /autoinstall.yaml \
  -map "$WORK/scalio" /scalio \
  -map "$WORK/grub.cfg" /boot/grub/grub.cfg \
  -boot_image any replay 2>&1 | grep -v '^xorriso : UPDATE' || true
[ -s "$OUT" ] || { echo "ISO not written" >&2; exit 1; }
sha256sum "$OUT" | sed "s|  .*/|  |" > "$OUT.sha256"
echo "built $OUT ($(du -h "$OUT" | cut -f1))"
