#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry
#
# Remove what the lab creates, by ID so duplicates are handled. A leftover
# resource with the same name makes every later 'show <name>' fail with
# "More than one ... exists", which looks exactly like a timeout.

set -eu
cd "$(dirname "$0")"
# shellcheck disable=SC1091
. ./env.sh
lab_check_ip

# shellcheck disable=SC2086
ssh $lab_ssh_opts "$LAB_USER@$LAB_IP" bash -s <<'REMOTE'
set -u
set +u
# shellcheck disable=SC1091
. /opt/stack/devstack/openrc admin admin >/dev/null 2>&1
set -u

say() { printf '\n== %s\n' "$*"; }

say "routers"
for n in smoke-router; do
	for id in $(openstack router list --name "$n" -f value -c ID 2>/dev/null); do
		echo "   delete router $n ($id)"
		openstack router unset --external-gateway "$id" >/dev/null 2>&1 || true
		openstack router delete "$id" || true
	done
done

say "appliance instances"
for id in $(openstack server list --all-projects -f value -c ID -c Name 2>/dev/null \
		| awk '/appliance|smoke/ {print $1}'); do
	echo "   delete server $id"
	openstack server delete "$id" || true
done
for _ in $(seq 1 30); do
	openstack server list --all-projects -f value -c ID 2>/dev/null | grep -q . || break
	sleep 2
done

say "stray appliance ports"
for id in $(openstack port list -f value -c ID -c Name 2>/dev/null \
		| awk '/appliance/ {print $1}'); do
	echo "   delete port $id"
	openstack port delete "$id" || true
done

say "done"
REMOTE
