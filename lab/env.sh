# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry
# Lab environment, sourced by every helper here.

LAB_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
export LAB_DIR

# Fill in after ./create-vm.sh prints the address. It is a DHCP lease, so it
# can change when the VM is recreated.
export LAB_NAME=${LAB_NAME:-devstack-appliance}
export LAB_IP=${LAB_IP:-}
export LAB_USER=${LAB_USER:-stack}

# An ad-hoc, passphrase-less key generated on demand by create-vm.sh and kept
# with the lab rather than in ~/.ssh. It authenticates nothing but a
# throwaway VM, it is git-ignored, and deleting the directory disposes of it.
# A passphrase-less key is deliberate: a smartcard or an agent key would
# demand a physical touch on every scripted ssh, several times per sync.
export LAB_SSH_KEY=${LAB_SSH_KEY:-$LAB_DIR/.ssh/id_devstack_appliance}

# Where this repository is rsynced inside the VM.
export LAB_SRC=${LAB_SRC:-/opt/stack/neutron-appliance}
# The venv devstack runs its services from.
export LAB_VENV=${LAB_VENV:-/opt/stack/data/venv}

lab_ssh_opts="-i $LAB_SSH_KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=no
              -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR
              -o PasswordAuthentication=no"

lab_keygen() {
	if [ ! -f "$LAB_SSH_KEY" ]; then
		mkdir -p "$(dirname "$LAB_SSH_KEY")"
		chmod 700 "$(dirname "$LAB_SSH_KEY")"
		echo "==> generating $LAB_SSH_KEY"
		ssh-keygen -q -t ed25519 -N '' -C "$LAB_NAME" -f "$LAB_SSH_KEY"
	fi
}

lab_check_key() {
	if [ ! -f "$LAB_SSH_KEY" ]; then
		echo "error: no lab ssh key at $LAB_SSH_KEY." >&2
		echo "       It is created by ./create-vm.sh and is not in git." >&2
		echo "       If the VM already exists you will have to recreate it:" >&2
		echo "       ./create-vm.sh --recreate" >&2
		exit 1
	fi
}

lab_check_ip() {
	lab_check_key
	if [ -z "${LAB_IP:-}" ]; then
		echo "error: LAB_IP is not set. Run ./create-vm.sh, then put the" >&2
		echo "       address it prints into lab/env.sh (or export LAB_IP)." >&2
		echo "       Current lease: virsh -c qemu:///system domifaddr $LAB_NAME" >&2
		exit 1
	fi
}
