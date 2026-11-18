#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry
# Install devstack inside the lab VM. Long running (~30-45 min); detaches and
# logs to /opt/stack/logs/stack.sh.log inside the VM.
#
# Usage: ./install-devstack.sh [IP]   (defaults to $LAB_IP or lab/env.sh)

set -eu
cd "$(dirname "$0")"
# shellcheck disable=SC1091
. ./env.sh

lab_check_ip
IP=${1:-$LAB_IP}
BRANCH=${DEVSTACK_BRANCH:-master}
SSH="ssh -i $HOME/.ssh/id_devstack_appliance -o IdentitiesOnly=yes -o BatchMode=yes \
     -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"

echo "==> copying local.conf"
# shellcheck disable=SC2086
scp -i "$HOME/.ssh/id_devstack_appliance" -o IdentitiesOnly=yes -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null -q local.conf "stack@$IP:/tmp/local.conf"

echo "==> cloning devstack ($BRANCH) and starting stack.sh"
# shellcheck disable=SC2086
$SSH "stack@$IP" bash -s <<EOF
set -eu
if [ ! -d /opt/stack/devstack ]; then
	git clone -b $BRANCH https://opendev.org/openstack/devstack /opt/stack/devstack
fi
cp /tmp/local.conf /opt/stack/devstack/local.conf
mkdir -p /opt/stack/logs
cd /opt/stack/devstack
if pgrep -f 'stack.sh' >/dev/null; then
	echo "stack.sh already running"; exit 0
fi
setsid nohup ./stack.sh > /opt/stack/logs/stack-run.log 2>&1 < /dev/null &
echo "stack.sh started (pid \$!)"
EOF

cat <<EOF

stack.sh is running in the background. Follow it with:

    ./ssh.sh tail -f /opt/stack/logs/stack-run.log

Then: ./sync.sh && ./setup.sh

It is done when the log ends with "This is your host IP address".
EOF
