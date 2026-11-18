#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry
# Create the devstack lab VM on the local libvirt (qemu:///system).
#
# Idempotent-ish: refuses to clobber an existing domain unless --recreate.
#
# Usage: ./create-vm.sh [--recreate]

set -eu
cd "$(dirname "$0")"
# shellcheck disable=SC1091
. ./env.sh

NAME=${NAME:-$LAB_NAME}
VCPUS=${VCPUS:-8}
RAM_MB=${RAM_MB:-12288}
DISK_GB=${DISK_GB:-100}
NET=${NET:-default}
BASE_IMG=${BASE_IMG:-$HOME/vm/images/noble-base.img}
DISK=/var/lib/libvirt/images/$NAME.qcow2
URI=qemu:///system

die() { echo "error: $*" >&2; exit 1; }

[ -f "$BASE_IMG" ] || die "base image $BASE_IMG not found"

# Ad-hoc, passphrase-less, git-ignored. See the comment in env.sh.
lab_keygen

if virsh -c $URI dominfo "$NAME" >/dev/null 2>&1; then
	if [ "${1:-}" = "--recreate" ]; then
		echo "removing existing domain $NAME"
		virsh -c $URI destroy "$NAME" >/dev/null 2>&1 || true
		virsh -c $URI undefine "$NAME" --nvram --remove-all-storage >/dev/null 2>&1 || true
	else
		die "domain $NAME already exists (use --recreate to replace it)"
	fi
fi

echo "==> preparing disk ($DISK_GB GB)"
sudo cp --reflink=auto "$BASE_IMG" "$DISK"
sudo qemu-img resize "$DISK" "${DISK_GB}G"
sudo restorecon -F "$DISK" 2>/dev/null || true

USERDATA=$(mktemp /tmp/cloud-init-XXXXXX.yaml)
trap 'rm -f "$USERDATA"' EXIT
cat > "$USERDATA" <<EOF
#cloud-config
hostname: $NAME
preserve_hostname: false
users:
  - name: stack
    gecos: devstack
    sudo: "ALL=(ALL) NOPASSWD:ALL"
    shell: /bin/bash
    lock_passwd: false
    ssh_authorized_keys:
      - $(cat "$LAB_SSH_KEY.pub")
# Console-only password, as the way back in when networking breaks. ssh
# itself is key-only: ssh_pwauth stays false.
chpasswd:
  users:
    - {name: stack, password: stack, type: text}
  expire: false
ssh_pwauth: false
growpart:
  mode: auto
  devices: ["/"]
resize_rootfs: true
package_update: true
packages: [git, jq, net-tools, tcpdump, python3-pip]
write_files:
  - path: /etc/sysctl.d/99-lab.conf
    content: |
      net.ipv4.ip_forward = 1
      fs.inotify.max_user_instances = 1024
runcmd:
  - [ sysctl, --system ]
  - [ install, -d, -o, stack, -g, stack, /opt/stack ]
EOF

echo "==> creating domain $NAME (${VCPUS} vcpu, ${RAM_MB} MB)"
virt-install \
	--connect $URI \
	--name "$NAME" \
	--vcpus "$VCPUS" \
	--memory "$RAM_MB" \
	--cpu host-passthrough \
	--disk path="$DISK",format=qcow2,bus=virtio \
	--network network="$NET",model=virtio \
	--os-variant ubuntu24.04 \
	--graphics none \
	--console pty,target_type=serial \
	--cloud-init user-data="$USERDATA" \
	--import \
	--noautoconsole

echo "==> waiting for an address on the '$NET' network"
for _ in $(seq 1 90); do
	IP=$(virsh -c $URI domifaddr "$NAME" 2>/dev/null \
		| awk '/ipv4/ {split($4,a,"/"); print a[1]; exit}')
	[ -n "${IP:-}" ] && break
	sleep 2
done
[ -n "${IP:-}" ] || die "no address after 180s; check 'virsh console $NAME'"

echo "==> $NAME is at $IP"
echo
echo "    ./ssh.sh                      # once LAB_IP is set"
echo "    virsh -c $URI console $NAME"
echo
echo "Next: put $IP in env.sh, then ./install-devstack.sh"
