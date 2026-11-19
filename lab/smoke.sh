#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry
#
# The stage pre-0 acceptance scenario, scripted. Every check prints PASS,
# FAIL or SKIP with a reason, and the script keeps going so you see the whole
# picture rather than the first failure.
#
# Nothing here forwards a packet: pre-0 only proves the plumbing.

set -u
cd "$(dirname "$0")"
# shellcheck disable=SC1091
. ./env.sh
lab_check_ip

FLAVOR=${FLAVOR:-appliance-router}
SVC_PROJECT=${SVC_PROJECT:-appliance-service}
ROUTER=${ROUTER:-smoke-router}
TIMEOUT=${TIMEOUT:-180}

# shellcheck disable=SC2086
ssh $lab_ssh_opts "$LAB_USER@$LAB_IP" \
	FLAVOR="$FLAVOR" SVC_PROJECT="$SVC_PROJECT" ROUTER="$ROUTER" \
	TIMEOUT="$TIMEOUT" bash -s <<'REMOTE'
set -u
set +u
# shellcheck disable=SC1091
. /opt/stack/devstack/openrc admin admin >/dev/null 2>&1
set -u

pass=0; fail=0; skip=0
ok()   { printf '  PASS  %s\n' "$*"; pass=$((pass+1)); }
ko()   { printf '  FAIL  %s\n' "$*"; fail=$((fail+1)); }
sk()   { printf '  SKIP  %s\n' "$*"; skip=$((skip+1)); }
say()  { printf '\n== %s\n' "$*"; }

say "preconditions"
fid=$(openstack network flavor show "$FLAVOR" -f value -c id 2>/dev/null || true)
if [ -n "$fid" ]; then ok "flavor $FLAVOR exists"; else
	ko "flavor $FLAVOR missing, run ./setup.sh"; echo; exit 1
fi
# br-ex loses its address across a VM reboot and every data path test then
# fails for a reason that has nothing to do with your code.
if ip -br addr show br-ex 2>/dev/null | grep -q '172\.24\.4\.1/'; then
	ok "br-ex carries 172.24.4.1"
else
	sk "br-ex has no address: sudo ip link set br-ex up &&
          sudo ip addr replace 172.24.4.1/24 dev br-ex"
fi

say "cleanup of any previous run"
for id in $(openstack router list --name "$ROUTER" -f value -c ID 2>/dev/null); do
	openstack router delete "$id" >/dev/null 2>&1 || true
done
sleep 2

say "1. router create returns promptly"
start=$(date +%s)
if openstack router create --flavor-id "$fid" "$ROUTER" >/dev/null 2>&1; then
	took=$(( $(date +%s) - start ))
	rid=$(openstack router show "$ROUTER" -f value -c id)
	if [ "$took" -le 15 ]; then
		ok "created in ${took}s (did not block on the VM)"
	else
		ko "took ${took}s: the API path is blocking on something slow"
	fi
else
	ko "router create failed"; echo; exit 1
fi

say "2. the driver claimed it"
prov=$(sudo mysql -N neutron -e \
	"select provider_name from providerresourceassociations where resource_id='$rid';" \
	2>/dev/null || true)
if [ -n "$prov" ]; then
	ok "provider is '$prov'"
else
	ko "no providerresourceassociations row: the driver did not claim it"
fi

say "3. an appliance VM appears (up to ${TIMEOUT}s)"
vm=""
for _ in $(seq 1 "$TIMEOUT"); do
	vm=$(openstack server list --all-projects -f value -c ID -c Name 2>/dev/null \
		| awk -v r="${rid:0:8}" '$0 ~ r {print $1; exit}')
	[ -n "$vm" ] && break
	sleep 1
done
if [ -n "$vm" ]; then
	ok "instance $vm"
	proj=$(openstack server show "$vm" -f value -c project_id)
	svc=$(openstack project show "$SVC_PROJECT" -f value -c id 2>/dev/null || true)
	if [ "$proj" = "$svc" ]; then
		ok "it is in the service project"
	else
		ko "it is in project $proj, expected the service project $svc"
	fi
else
	sk "no VM yet: the reconciler is milestone M4"
fi

say "4. the agent synced"
state=$(sudo mysql -N neutron -e \
	"select state from appliance_routers where router_id='$rid';" 2>/dev/null || true)
if [ -n "$state" ]; then
	ok "state is '$state'"
else
	sk "no appliance_routers row: the model is milestone M2"
fi

say "5. status is visible to an operator"
tags=$(openstack router show "$ROUTER" -f value -c tags 2>/dev/null || true)
case "$tags" in
*appliance:*) ok "router tags: $tags" ;;
*)            sk "no appliance: tags yet (milestone M3)" ;;
esac

say "6. delete leaves nothing behind"
openstack router delete "$ROUTER" >/dev/null 2>&1 || ko "router delete failed"
sleep 10
left=$(openstack server list --all-projects -f value -c ID 2>/dev/null \
	| grep -c "${vm:-__none__}" || true)
if [ "${left:-0}" -eq 0 ]; then
	ok "no orphan instance"
else
	ko "instance $vm is still there"
fi
if [ -n "$state" ]; then
	row=$(sudo mysql -N neutron -e \
		"select count(*) from appliance_routers where router_id='$rid';" \
		2>/dev/null || true)
	if [ "${row:-1}" = "0" ]; then
		ok "the appliance_routers row went with the router"
	else
		ko "the appliance_routers row for $rid survived the router"
	fi
fi

# Registering our provider as the default would silently take every router
# in the cloud away from OVN, and nothing else would notice until a tenant
# asked for a floating IP.
say "7. plain routers still belong to OVN"
if openstack router create "$ROUTER-plain" >/dev/null 2>&1; then
	pid=$(openstack router show "$ROUTER-plain" -f value -c id)
	prov=$(sudo mysql -N neutron -e \
		"select provider_name from providerresourceassociations where resource_id='$pid';" \
		2>/dev/null || true)
	case "$prov" in
	ovn)  ok "provider is 'ovn'" ;;
	"")   ko "no provider association at all" ;;
	*)    ko "provider is '$prov', expected 'ovn'" ;;
	esac
	row=$(sudo mysql -N neutron -e \
		"select count(*) from appliance_routers where router_id='$pid';" \
		2>/dev/null || true)
	if [ "${row:-0}" = "0" ]; then
		ok "we did not claim it"
	else
		ko "we claimed a router that has no flavor"
	fi
	openstack router delete "$pid" >/dev/null 2>&1 || true
else
	ko "could not create a router without a flavor"
fi

printf '\n%s\n' "pass=$pass fail=$fail skip=$skip"
[ "$fail" -eq 0 ]
REMOTE
