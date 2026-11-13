#!/usr/bin/env bash
# Boot test of a Scalio VM image in QEMU/KVM (selfhost/H8). The image itself stays unchanged (copy-on-write overlay).
#   image/boot-test.sh <disk.qcow2|.vmdk|.vhdx> [bios|uefi]
# A NoCloud seed adds a throwaway test user with a fresh SSH key, as Proxmox or VMware would. Checks: boots, SSH works,
# no account with a password, ufw and fail2ban on, disk grown, first-boot service ran get.sh up to Docker.
# With REGISTRY_TOKEN (read access to ghcr.io/scalio-cloud/scalio-setup-dev) also the whole first boot: Installer
# running and /api/health "ok" on the server's address. Needs qemu-system-x86, qemu-utils, cloud-image-utils, ovmf.
set -euo pipefail
DISK="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
FW="${2:-bios}"
case "$DISK" in *.qcow2) FMT=qcow2 ;; *.vmdk) FMT=vmdk ;; *.vhdx) FMT=vhdx ;; *) echo "unknown disk format: $DISK" >&2; exit 1 ;; esac
WORK="$(mktemp -d)"
PORT=$((20000 + RANDOM % 10000))
cleanup() {
  [ -f "$WORK/pid" ] && kill "$(cat "$WORK/pid")" 2>/dev/null || true
  rm -rf "$WORK"
}
trap cleanup EXIT

ssh-keygen -q -t ed25519 -N '' -f "$WORK/key"
FULL=false
{
  echo "#cloud-config"
  echo "users:"
  echo "  - default"
  echo "  - name: boottest"
  echo "    sudo: ALL=(ALL) NOPASSWD:ALL"
  echo "    shell: /bin/bash"
  echo "    lock_passwd: true"
  echo "    ssh_authorized_keys: [\"$(cat "$WORK/key.pub")\"]"
  if [ -n "${REGISTRY_TOKEN:-}" ]; then
    FULL=true
    AUTH=$(printf 'x:%s' "$REGISTRY_TOKEN" | base64 -w0)
    echo "write_files:"
    echo "  - path: /root/.docker/config.json"
    echo "    permissions: '0600'"
    echo "    content: '{\"auths\": {\"ghcr.io\": {\"auth\": \"$AUTH\"}}}'"
    echo "bootcmd:"
    echo "  - echo dev > /opt/scalio-image/channel"
  fi
} > "$WORK/user-data"
printf 'instance-id: boot-test\nlocal-hostname: scalio-boottest\n' > "$WORK/meta-data"
cloud-localds "$WORK/seed.iso" "$WORK/user-data" "$WORK/meta-data"
qemu-img create -q -f qcow2 -b "$DISK" -F "$FMT" "$WORK/overlay.qcow2"
FIRMWARE=()
if [ "$FW" = uefi ]; then
  OVMF=""   # not `ls a b c | head`: ls exits 2 for a missing path, and pipefail ends the script there
  for f in /usr/share/OVMF/OVMF_CODE_4M.fd /usr/share/OVMF/OVMF_CODE.fd /usr/share/ovmf/OVMF.fd; do
    [ -f "$f" ] && { OVMF=$f; break; }
  done
  [ -n "$OVMF" ] || { echo "no OVMF firmware" >&2; exit 1; }
  FIRMWARE=(-drive "if=pflash,format=raw,readonly=on,file=$OVMF")
  VARS="$(dirname "$OVMF")/OVMF_VARS_4M.fd"; [ -f "$VARS" ] || VARS="$(dirname "$OVMF")/OVMF_VARS.fd"
  if [ -f "$VARS" ]; then cp "$VARS" "$WORK/vars.fd"; FIRMWARE+=(-drive "if=pflash,format=raw,file=$WORK/vars.fd"); fi
fi
ACCEL=(-accel tcg)
[ -w /dev/kvm ] && ACCEL=(-accel kvm -cpu host)
qemu-system-x86_64 "${ACCEL[@]}" -m 4096 -smp 2 -machine q35 "${FIRMWARE[@]}" \
  -drive "file=$WORK/overlay.qcow2,format=qcow2,if=virtio" -drive "file=$WORK/seed.iso,format=raw,media=cdrom" \
  -netdev "user,id=n0,hostfwd=tcp:127.0.0.1:$PORT-:22" -device virtio-net-pci,netdev=n0 \
  -display none -serial "file:$WORK/console.log" -daemonize -pidfile "$WORK/pid"

SSH=(ssh -q -i "$WORK/key" -p "$PORT" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=5
     boottest@127.0.0.1)
fail() {
  echo "BOOT TEST FAILED ($DISK, $FW): $*" >&2
  echo "--- console (last 60 lines)" >&2; tail -n 60 "$WORK/console.log" >&2 || true
  "${SSH[@]}" 'sudo tail -n 60 /var/log/scalio-firstboot.log; systemctl status --no-pager scalio-account scalio-firstboot; sudo journalctl -b --no-pager -u scalio-account | tail -n 30' >&2 2>/dev/null || true
  exit 1
}
wait_for() {   # seconds, description, remote command
  local end=$((SECONDS + $1))
  until "${SSH[@]}" "$3" >/dev/null 2>&1; do
    [ "$SECONDS" -lt "$end" ] || fail "timeout: $2"
    sleep 10
  done
  echo "ok: $2"
}

wait_for 600 "SSH" true
wait_for 300 "cloud-init done" "cloud-init status --wait >/dev/null; test -f /var/lib/cloud/instance/boot-finished"
"${SSH[@]}" 'SCALIO_ACCOUNT_CHECK=1 /opt/scalio-image/scalio-account.sh' || fail "account check"
wait_for 180 "account wizard saw the cloud-init account" 'test -f /var/lib/scalio/account-done'
"${SSH[@]}" "sudo awk -F: '\$2 !~ /^[!*]/ && \$2 != \"\" {print \$1}' /etc/shadow" | grep . && fail "account with a password"
"${SSH[@]}" 'sudo ufw status | grep -q "Status: active"' || fail "ufw not active"
"${SSH[@]}" 'sudo ufw status | grep -q "443/tcp"' || fail "ufw without 443"
"${SSH[@]}" 'systemctl is-active -q fail2ban' || fail "fail2ban not running"
"${SSH[@]}" 'test "$(df --output=size -BG / | tail -n 1 | tr -dc 0-9)" -ge 30' || fail "disk not grown"
"${SSH[@]}" 'systemctl is-enabled -q scalio-firstboot.service || test -f /etc/issue.d/scalio.issue' || fail "first-boot service"
wait_for 900 "Docker installed by get.sh" 'command -v docker'
if [ "$FULL" = true ]; then
  wait_for 1200 "Scalio Installer running" 'grep -q "Installer läuft" /etc/issue.d/scalio.issue || { grep -q fehlgeschlagen /etc/issue.d/scalio.issue && exit 0; exit 1; }'
  "${SSH[@]}" 'grep -q "Installer läuft" /etc/issue.d/scalio.issue' || fail "first boot failed"
  "${SSH[@]}" 'curl -fsk "https://$(hostname -I | cut -d" " -f1)/api/health" | grep -q "\"ok\""' || fail "/api/health"
  echo "ok: /api/health"
else
  echo "note: no REGISTRY_TOKEN, the Installer image itself was not started"
fi
echo "BOOT TEST PASSED ($(basename "$DISK"), $FW)"
