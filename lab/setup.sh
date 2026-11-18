#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry
#
# Configure devstack to use the appliance driver: service project, nova
# flavor, image, neutron service provider, network flavor and profile.
#
# Idempotent: safe to re-run after a re-stack or a driver change.
#
# Run it after ./sync.sh. It refuses to touch neutron.conf until the driver
# class actually imports, because registering a provider that cannot be
# loaded takes neutron-api down and the error is reported nowhere useful.

set -eu
cd "$(dirname "$0")"
# shellcheck disable=SC1091
. ./env.sh
lab_check_ip

DRIVER=${DRIVER:-neutron_appliance.server.driver.ApplianceL3Driver}
PROVIDER=${PROVIDER:-appliance}
FLAVOR=${FLAVOR:-appliance-router}
SVC_PROJECT=${SVC_PROJECT:-appliance-service}
NOVA_FLAVOR=${NOVA_FLAVOR:-appliance.small}
# Until the real image exists (milestone M6), cirros is enough to prove that
# a VM is booted, attached and deleted.
APPLIANCE_IMAGE=${APPLIANCE_IMAGE:-}

# shellcheck disable=SC2086
ssh $lab_ssh_opts "$LAB_USER@$LAB_IP" \
	LAB_VENV="$LAB_VENV" DRIVER="$DRIVER" PROVIDER="$PROVIDER" \
	FLAVOR="$FLAVOR" SVC_PROJECT="$SVC_PROJECT" NOVA_FLAVOR="$NOVA_FLAVOR" \
	APPLIANCE_IMAGE="$APPLIANCE_IMAGE" bash -s <<'REMOTE'
set -eu
set +u
# shellcheck disable=SC1091
. /opt/stack/devstack/openrc admin admin >/dev/null 2>&1
set -u

say() { printf '\n== %s\n' "$*"; }
note() { printf '   %s\n' "$*"; }

say "driver import check"
if "$LAB_VENV/bin/python" -c "
import importlib, sys
mod, _, cls = '$DRIVER'.rpartition('.')
m = importlib.import_module(mod)
getattr(m, cls)
" 2>/dev/null; then
	note "$DRIVER imports: OK"
	driver_ok=true
else
	note "$DRIVER does NOT import yet"
	driver_ok=false
fi

say "service project and role"
openstack project show "$SVC_PROJECT" >/dev/null 2>&1 || \
	openstack project create "$SVC_PROJECT" >/dev/null
openstack role show appliance-agent >/dev/null 2>&1 || \
	openstack role create appliance-agent >/dev/null
openstack role add --project "$SVC_PROJECT" --user admin admin >/dev/null 2>&1 || true
note "project: $(openstack project show "$SVC_PROJECT" -f value -c id)"

say "nova flavor for appliances"
openstack flavor show "$NOVA_FLAVOR" >/dev/null 2>&1 || \
	openstack flavor create --vcpus 1 --ram 512 --disk 2 "$NOVA_FLAVOR" >/dev/null
note "$NOVA_FLAVOR"

say "appliance image"
if [ -n "$APPLIANCE_IMAGE" ]; then
	image=$APPLIANCE_IMAGE
else
	image=$(openstack image list -f value -c Name | grep -i cirros | head -1)
	note "no APPLIANCE_IMAGE set, falling back to '$image' (M6 replaces this)"
fi
note "image: $image"

say "neutron service provider"
if [ "$driver_ok" != true ]; then
	note "SKIPPED: the driver does not import, so registering it would"
	note "         take neutron-api down. Implement milestone M2 first."
	note "         Everything above is still configured."
	exit 0
fi

conf=/etc/neutron/neutron.conf
line="L3_ROUTER_NAT:$PROVIDER:$DRIVER"

# 'flavors' must be in service_plugins or flavor dispatch never happens.
plugins=$(sudo crudini --get $conf DEFAULT service_plugins 2>/dev/null || echo "")
case ",$plugins," in
*,flavors,*) note "service_plugins already has 'flavors'" ;;
*)  sudo crudini --set $conf DEFAULT service_plugins "${plugins:+$plugins,}flavors"
    note "added 'flavors' to service_plugins" ;;
esac

# NOTE: no ':default' suffix. With the OVN L3 plugin that makes the built-in
# 'ovn' provider silently disappear and every unflavored router breaks.
current=$(sudo crudini --get $conf service_providers service_provider 2>/dev/null || echo "")
if echo "$current" | grep -qF "$line"; then
	note "provider already registered"
else
	sudo crudini --set $conf service_providers service_provider "$line"
	note "registered $line"
fi

say "restarting neutron"
sudo systemctl restart devstack@neutron-api devstack@neutron-rpc-server \
	devstack@neutron-periodic-workers
sleep 5
for u in neutron-api neutron-rpc-server neutron-periodic-workers; do
	printf '   %-32s %s\n' "$u" "$(systemctl is-active devstack@$u || true)"
done

say "network flavor and service profile"
prof=$(openstack network flavor profile list -f value -c ID -c Description 2>/dev/null \
	| awk -v d="$DRIVER" '$0 ~ d {print $1; exit}')
if [ -z "$prof" ]; then
	prof=$(openstack network flavor profile create --enable \
		--description "$DRIVER" --driver "$DRIVER" \
		--metainfo "{\"nova_flavor\": \"$NOVA_FLAVOR\", \"image\": \"$image\"}" \
		-f value -c id)
	note "created profile $prof"
else
	note "profile $prof exists"
fi

if ! openstack network flavor show "$FLAVOR" >/dev/null 2>&1; then
	openstack network flavor create --service-type L3_ROUTER_NAT \
		--description "router backed by a service VM" "$FLAVOR" >/dev/null
	note "created flavor $FLAVOR"
fi
fid=$(openstack network flavor show "$FLAVOR" -f value -c id)
openstack network flavor add profile "$fid" "$prof" >/dev/null 2>&1 || true

say "ready"
note "openstack router create --flavor-id $fid r1"
note "providers:"
openstack network agent list >/dev/null 2>&1 || true
sudo crudini --get $conf service_providers service_provider | tr ',' '\n' | sed 's/^/     /'
REMOTE
