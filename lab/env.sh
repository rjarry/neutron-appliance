# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry
# Lab environment, sourced by every helper here.

# Fill in after ./create-vm.sh prints the address. It is a DHCP lease, so it
# can change when the VM is recreated.
export LAB_NAME=${LAB_NAME:-devstack-appliance}
export LAB_IP=${LAB_IP:-}
export LAB_USER=${LAB_USER:-stack}
export LAB_SSH_KEY=${LAB_SSH_KEY:-$HOME/.ssh/id_devstack_appliance}

# Where this repository is rsynced inside the VM.
export LAB_SRC=${LAB_SRC:-/opt/stack/neutron-appliance}
# The venv devstack runs its services from.
export LAB_VENV=${LAB_VENV:-/opt/stack/data/venv}

lab_ssh_opts="-i $LAB_SSH_KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=no
              -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR"

lab_check_ip() {
	if [ -z "${LAB_IP:-}" ]; then
		echo "error: LAB_IP is not set. Run ./create-vm.sh, then put the" >&2
		echo "       address it prints into lab/env.sh (or export LAB_IP)." >&2
		echo "       Current lease: virsh -c qemu:///system domifaddr $LAB_NAME" >&2
		exit 1
	fi
}
