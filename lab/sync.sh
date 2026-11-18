#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry
#
# Push this checkout into the lab VM, install it into the venv devstack runs
# its services from, and restart neutron. This is the inner loop: edit here,
# run this, test there. It takes a few seconds, not three weeks.
#
# Usage: ./sync.sh [--no-restart]

set -eu
cd "$(dirname "$0")"
# shellcheck disable=SC1091
. ./env.sh
lab_check_ip

restart=true
[ "${1:-}" = "--no-restart" ] && restart=false

echo "==> rsync $(cd .. && pwd) -> $LAB_USER@$LAB_IP:$LAB_SRC"
# shellcheck disable=SC2086
rsync -a --delete \
	--exclude '.git/' \
	--exclude '.venv/' \
	--exclude '*.egg-info/' \
	--exclude '__pycache__/' \
	--exclude '.ruff_cache/' \
	--exclude '.pytest_cache/' \
	-e "ssh $lab_ssh_opts" \
	../ "$LAB_USER@$LAB_IP:$LAB_SRC/"

# shellcheck disable=SC2086
ssh $lab_ssh_opts "$LAB_USER@$LAB_IP" \
	LAB_SRC="$LAB_SRC" LAB_VENV="$LAB_VENV" restart="$restart" bash -s <<'REMOTE'
set -eu

if [ ! -x "$LAB_VENV/bin/pip" ]; then
	echo "error: no venv at $LAB_VENV. Has stack.sh finished?" >&2
	exit 1
fi

echo "==> installing into $LAB_VENV"
# --no-deps: neutron and its stack are already there, and resolving them
# again against pypi is both slow and a good way to break the deployment.
"$LAB_VENV/bin/pip" install -q --no-deps -e "$LAB_SRC"

echo -n "==> import check: "
if "$LAB_VENV/bin/python" -c 'import neutron_appliance; print(neutron_appliance.__file__)'; then
	:
else
	echo "FAILED" >&2
	exit 1
fi

if [ "$restart" != true ]; then
	echo "==> not restarting (--no-restart)"
	exit 0
fi

# The L3 service provider is loaded by whichever process runs the L3 plugin,
# and the periodic worker by the workers service. Restart all of them rather
# than guessing which one you just changed.
units="devstack@neutron-api devstack@neutron-rpc-server"
units="$units devstack@neutron-periodic-workers"
echo "==> restarting:$units" | tr ' ' '\n' | paste -sd' '
# shellcheck disable=SC2086
sudo systemctl restart $units

sleep 3
fail=false
for u in $units; do
	state=$(systemctl is-active "$u" || true)
	printf '    %-40s %s\n' "$u" "$state"
	[ "$state" = active ] || fail=true
done

if [ "$fail" = true ]; then
	echo
	echo "error: a neutron service did not come back. Most likely your code" >&2
	echo "       raises at import time, or neutron.conf points at a class" >&2
	echo "       that does not exist yet. Look at:" >&2
	echo "       ./logs.sh" >&2
	exit 1
fi
REMOTE

echo "==> synced"
