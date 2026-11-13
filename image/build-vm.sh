#!/usr/bin/env bash
# Builds the Scalio VM images (selfhost/H8): the official Ubuntu cloud image, checked against Ubuntu's signed
# SHA256SUMS, plus get.sh, the first-boot service, the console account wizard and the hardening of the ISO (ufw
# 22/80/443, fail2ban, unattended-upgrades without own restart). No default account or password: cloud-init data
# (Proxmox, VMware vApp, Azure) still works, otherwise scalio-account asks on the console.
#   image/build-vm.sh [out-dir]   UBUNTU_RELEASE (26.04), SCALIO_CHANNEL (public; dev/rc for Scalio lab servers),
#                                 SCALIO_VERSION (name in the files, default dev), DISK_GB (40), VM_CACHE (.vm-cache)
# Writes <name>.qcow2 (for the boot test and Proxmox), <name>.vhdx.zip (Hyper-V Gen 2), <name>.ova (VMware, Proxmox,
# VirtualBox) and a .sha256 for each. Needs curl, gpg, qemu-utils, libguestfs-tools, zip.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
RELEASE="${UBUNTU_RELEASE:-26.04}"
CHANNEL="${SCALIO_CHANNEL:-public}"
VERSION="${SCALIO_VERSION:-dev}"
DISK_GB="${DISK_GB:-40}"
case "$CHANNEL" in public|dev|rc) ;; *) echo "SCALIO_CHANNEL must be public, dev or rc" >&2; exit 1 ;; esac
echo "$VERSION" | grep -Eq '^[0-9A-Za-z.+-]{1,40}$' || { echo "bad SCALIO_VERSION $VERSION" >&2; exit 1; }
CACHE="${VM_CACHE:-$ROOT/.vm-cache}"
OUT="$(mkdir -p "${1:-$ROOT/vm-out}" && cd "${1:-$ROOT/vm-out}" && pwd)"
NAME="scalio-installer-$VERSION-ubuntu-$RELEASE-amd64"
BASE="https://cloud-images.ubuntu.com/releases/$RELEASE/release"
# Ubuntu cloud image signing key (UEC Image Automatic Signing Key, signs SHA256SUMS on cloud-images.ubuntu.com)
CLOUD_KEY="D2EB44626FDDC30B513D5BB71A5D6C4C7DB87C81"

mkdir -p "$CACHE"
cd "$CACHE"
curl -fsSLO "$BASE/SHA256SUMS"
curl -fsSLO "$BASE/SHA256SUMS.gpg"
export GNUPGHOME="$CACHE/gnupg"
install -d -m 700 "$GNUPGHOME"
gpg -q --keyserver hkps://keyserver.ubuntu.com --recv-keys "$CLOUD_KEY"
gpg -q --verify SHA256SUMS.gpg SHA256SUMS
IMG="ubuntu-$RELEASE-server-cloudimg-amd64.img"
grep -q " \*\?$IMG\$" SHA256SUMS || { echo "$IMG not in $BASE/SHA256SUMS" >&2; exit 1; }
if ! grep " \*\?$IMG\$" SHA256SUMS | sha256sum -c --status 2>/dev/null; then
  curl -fL --retry 3 -o "$IMG" "$BASE/$IMG"
  grep " \*\?$IMG\$" SHA256SUMS | sha256sum -c -
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
install -m 0755 "$ROOT/get.sh" "$HERE/scalio-firstboot.sh" "$HERE/scalio-account.sh" "$HERE/scalio-update.sh" \
  "$HERE/scalio-code.sh" "$HERE/scalio-motd.sh" "$WORK/"
install -m 0644 "$HERE/scalio-firstboot.service" "$HERE/scalio-account.service" "$HERE/50-scalio-unattended" "$WORK/"
echo "$CHANNEL" > "$WORK/channel"
DISK="$OUT/$NAME.qcow2"
cp "$IMG" "$DISK"
qemu-img resize -q "$DISK" "${DISK_GB}G"
virt-customize -q -a "$DISK" \
  --update \
  --install ufw,fail2ban,unattended-upgrades,curl,ca-certificates,kbd,console-setup,qemu-guest-agent,open-vm-tools,ssh-import-id \
  --mkdir /opt/scalio-image \
  --upload "$WORK/get.sh:/opt/scalio-image/get.sh" \
  --upload "$WORK/scalio-firstboot.sh:/opt/scalio-image/scalio-firstboot.sh" \
  --upload "$WORK/scalio-account.sh:/opt/scalio-image/scalio-account.sh" \
  --upload "$WORK/channel:/opt/scalio-image/channel" \
  --upload "$WORK/scalio-update.sh:/usr/local/sbin/scalio-update" \
  --upload "$WORK/scalio-code.sh:/usr/local/sbin/scalio-code" \
  --upload "$WORK/scalio-motd.sh:/etc/update-motd.d/60-scalio" \
  --upload "$WORK/scalio-firstboot.service:/etc/systemd/system/scalio-firstboot.service" \
  --upload "$WORK/scalio-account.service:/etc/systemd/system/scalio-account.service" \
  --upload "$WORK/50-scalio-unattended:/etc/apt/apt.conf.d/52scalio-unattended" \
  --run-command 'chmod 0755 /opt/scalio-image/*.sh /usr/local/sbin/scalio-update /usr/local/sbin/scalio-code /etc/update-motd.d/60-scalio' \
  --run-command 'systemctl enable scalio-firstboot.service scalio-account.service fail2ban.service' \
  --run-command 'ufw default deny incoming && ufw allow 22/tcp && ufw allow 80/tcp && ufw allow 443/tcp && sed -i "s/^ENABLED=.*/ENABLED=yes/" /etc/ufw/ufw.conf' \
  --run-command 'sed -i "s/^XKBLAYOUT=.*/XKBLAYOUT=\"ch\"/" /etc/default/keyboard' \
  --run-command 'apt-get clean && rm -rf /var/lib/apt/lists/* && cloud-init clean --logs && truncate -s 0 /etc/machine-id && rm -f /var/lib/dbus/machine-id /etc/ssh/ssh_host_*' \
  --hostname scalio \
  --timezone Europe/Zurich
virt-sparsify -q --in-place "$DISK"

# Hyper-V (Gen 2, UEFI): dynamic VHDX, zipped for the download.
qemu-img convert -q -O vhdx -o subformat=dynamic "$DISK" "$WORK/$NAME.vhdx"
(cd "$WORK" && rm -f "$OUT/$NAME.vhdx.zip" && zip -q -1 "$OUT/$NAME.vhdx.zip" "$NAME.vhdx")
rm -f "$WORK/$NAME.vhdx"

# OVA: OVF (from the Ubuntu cloud image's own), manifest, streamOptimized VMDK; the OVF comes first in the tar.
qemu-img convert -q -O vmdk -o subformat=streamOptimized,adapter_type=lsilogic "$DISK" "$WORK/$NAME.vmdk"
SIZE=$(stat -c %s "$WORK/$NAME.vmdk")
CAPACITY=$(qemu-img info --output=json "$DISK" | python3 -c 'import json,sys; print(json.load(sys.stdin)["virtual-size"])')
sed -e "s|@VMDK@|$NAME.vmdk|" -e "s|@SIZE@|$SIZE|" -e "s|@CAPACITY@|$CAPACITY|" -e "s|@NAME@|$NAME|g" \
    -e "s|@VERSION@|$VERSION|g" -e "s|@RELEASE@|$RELEASE|g" "$HERE/scalio.ovf.in" > "$WORK/$NAME.ovf"
if grep -q '@[A-Z]*@' "$WORK/$NAME.ovf"; then echo "OVF: placeholder left" >&2; exit 1; fi
(cd "$WORK" && for f in "$NAME.vmdk" "$NAME.ovf"; do printf 'SHA256(%s)= %s\n' "$f" "$(sha256sum "$f" | cut -d' ' -f1)"; done > "$NAME.mf")
tar -C "$WORK" --format=ustar -cf "$OUT/$NAME.ova" "$NAME.ovf" "$NAME.mf" "$NAME.vmdk"

cd "$OUT"
for f in "$NAME.qcow2" "$NAME.vhdx.zip" "$NAME.ova"; do sha256sum "$f" > "$f.sha256"; done
ls -lh "$OUT"
