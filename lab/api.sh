#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry
#
# The milestone M3 acceptance scenario: the sync and status endpoints,
# exercised the way an agent will exercise them, with curl.
#
# There is no agent yet and no reconciler, so this plays the part of both:
# it mints a per-router token straight into the database and then speaks
# the protocol over HTTP. The token is hashed here with sha256sum rather
# than with our own code, so a bug in the hashing would show up as a 401
# instead of cancelling itself out on both sides.
#
# Every check prints PASS or FAIL and the script keeps going, so one
# failure does not hide the rest.

set -u
cd "$(dirname "$0")"
# shellcheck disable=SC1091
. ./env.sh
lab_check_ip

FLAVOR=${FLAVOR:-appliance-router}
SVC_PROJECT=${SVC_PROJECT:-appliance-service}
SVC_USER=${SVC_USER:-neutron-appliance}
SVC_PASSWORD=${SVC_PASSWORD:-appliance}
ROUTER=${ROUTER:-api-router}

# shellcheck disable=SC2086
ssh $lab_ssh_opts "$LAB_USER@$LAB_IP" \
	FLAVOR="$FLAVOR" SVC_PROJECT="$SVC_PROJECT" SVC_USER="$SVC_USER" \
	SVC_PASSWORD="$SVC_PASSWORD" ROUTER="$ROUTER" bash -s <<'REMOTE'
set -u
set +u
# shellcheck disable=SC1091
. /opt/stack/devstack/openrc admin admin >/dev/null 2>&1
set -u

pass=0; fail=0
ok()  { printf '  PASS  %s\n' "$*"; pass=$((pass+1)); }
ko()  { printf '  FAIL  %s\n' "$*"; fail=$((fail+1)); }
say() { printf '\n== %s\n' "$*"; }

# devstack writes ~/.my.cnf for the stack user, so this needs no password
# and no sudo.
db() { mysql -N neutron -e "$1"; }

# A per-router token, and the row that will recognise it. This is what
# the reconciler will do when it builds an appliance (milestone M4).
mint() {
	local token
	token=$(openssl rand -base64 32 | tr '+/' '-_' | tr -d '=')
	db "update appliance_routers
	    set credential_hash='$(printf '%s' "$token" | sha256sum | cut -d' ' -f1)',
	        previous_credential_hash=null,
	        previous_credential_expires_at=null
	    where router_id='$1';"
	printf '%s' "$token"
}

# POST a sync report. Prints the status code on the first line and the
# body on the second, so a caller reads both from one call.
sync_as() {
	local router=$1 token=$2 boot=$3 applied=$4 state=$5
	local body code
	body=$(printf '{"schema_version":1,"agent_version":"lab",'
		printf '"boot_id":"%s","uptime_s":1,' "$boot"
		printf '"applied_generation":%s,"state":"%s"}' "$applied" "$state")
	code=$(curl -s -o /tmp/api-body -w '%{http_code}' -X POST \
		-H "X-Auth-Token: $SVC_TOKEN" \
		-H "X-Appliance-Token: $token" \
		-H 'Content-Type: application/json' \
		-d "$body" "$NEUTRON/v2.0/appliance/routers/$router/sync")
	printf '%s\n' "$code"
	cat /tmp/api-body
}

field() { python3 -c 'import json,sys; print(json.load(sys.stdin)'"$1"')'; }

say "preconditions"
fid=$(openstack network flavor show "$FLAVOR" -f value -c id 2>/dev/null || true)
if [ -z "$fid" ]; then
	echo "error: flavor $FLAVOR missing, run ./setup.sh" >&2
	exit 1
fi
NEUTRON=$(openstack endpoint list --service network --interface public \
	-f value -c URL | head -1)
NEUTRON=${NEUTRON%/}
if openstack extension list --network -f value -c Alias | grep -qx appliance; then
	ok "the appliance extension is loaded"
else
	echo "error: neutron does not list the 'appliance' extension." >&2
	echo "       api_extensions_path is probably unset: run ./setup.sh," >&2
	echo "       then ./logs.sh to see why the module did not load." >&2
	exit 1
fi

# Exactly what the agent does: authenticate as the shared service user
# and present the resulting token like any other API client.
# devstack's OS_AUTH_URL is unversioned; the token call is a v3 one.
KEYSTONE=${OS_AUTH_URL%/}
case $KEYSTONE in
*/v3) ;;
*) KEYSTONE=$KEYSTONE/v3 ;;
esac
SVC_TOKEN=$(curl -s -i -X POST -H 'Content-Type: application/json' \
	-d "{\"auth\": {\"identity\": {\"methods\": [\"password\"],
	      \"password\": {\"user\": {\"name\": \"$SVC_USER\",
	        \"domain\": {\"name\": \"Default\"},
	        \"password\": \"$SVC_PASSWORD\"}}},
	      \"scope\": {\"project\": {\"name\": \"$SVC_PROJECT\",
	        \"domain\": {\"name\": \"Default\"}}}}}" \
	"$KEYSTONE/auth/tokens" \
	| awk 'tolower($1) == "x-subject-token:" {print $2}' | tr -d '\r')
if [ -n "$SVC_TOKEN" ]; then
	ok "authenticated as $SVC_USER"
else
	echo "error: $SVC_USER could not get a keystone token" >&2
	exit 1
fi

say "cleanup of any previous run"
for name in "$ROUTER" "$ROUTER-other"; do
	for id in $(openstack router list --name "$name" -f value -c ID 2>/dev/null); do
		openstack router delete "$id" >/dev/null 2>&1 || true
	done
done

openstack router create --flavor-id "$fid" "$ROUTER" >/dev/null
rid=$(openstack router show "$ROUTER" -f value -c id)
openstack router create --flavor-id "$fid" "$ROUTER-other" >/dev/null
other=$(openstack router show "$ROUTER-other" -f value -c id)
trap 'openstack router delete "$rid" "$other" >/dev/null 2>&1 || true' EXIT

boot=$(cat /proc/sys/kernel/random/uuid)
token=$(mint "$rid")
stolen=$(mint "$other")

say "1. a valid service token and the right per-router token"
answer=$(sync_as "$rid" "$token" "$boot" 0 converging)
code=${answer%%$'\n'*}
body=${answer#*$'\n'}
if [ "$code" = 200 ]; then
	ok "200 OK"
else
	ko "$code, body: $body"
fi
if [ "$(printf '%s' "$body" | field '["generation"]')" = 1 ]; then
	ok "generation 1"
else
	ko "unexpected generation in $body"
fi
if [ "$(printf '%s' "$body" | field '["config"]["interfaces"]')" = "[]" ] &&
   [ "$(printf '%s' "$body" | field '["config"]["routes"]')" = "[]" ]; then
	ok "the configuration document is empty, as pre-0 requires"
else
	ko "the document is not empty: $body"
fi
seen=$(db "select last_seen from appliance_routers where router_id='$rid';")
if [ -n "$seen" ] && [ "$seen" != NULL ]; then
	ok "last_seen recorded: $seen"
else
	ko "last_seen was not recorded"
fi

# The config-drive token is used exactly once: the first sync of a boot
# is a boot_id change, which rotates.
rotated=$(printf '%s' "$body" | field '["credential"]["router_token"]')
if [ -n "$rotated" ] && [ "$rotated" != "$token" ]; then
	ok "the credential was rotated on first contact"
else
	ko "no rotation on first contact"
fi

say "2. another router's token against this router's URL"
answer=$(sync_as "$rid" "$stolen" "$boot" 0 converging)
if [ "${answer%%$'\n'*}" = 401 ]; then
	ok "401, the token is bound to the router in the URL"
else
	ko "${answer%%$'\n'*}, a valid token for $other opened $rid"
fi

say "3. the agent crashed before persisting the rotated token"
# It still holds the one it booted with. Without the grace window this
# is where an appliance is bricked, and only on an unlucky reboot.
answer=$(sync_as "$rid" "$token" "$boot" 0 converging)
code=${answer%%$'\n'*}
body=${answer#*$'\n'}
if [ "$code" = 200 ]; then
	ok "200, the previous token still authenticates"
else
	ko "$code, the appliance is locked out of its own control plane"
fi
current=$(printf '%s' "$body" | field '["credential"]["router_token"]')
if [ -n "$current" ]; then
	ok "and it was handed another new token"
else
	ko "no new token: the agent would stay on the old one for good"
fi

say "4. the previous token is retired once the new one is used"
answer=$(sync_as "$rid" "$current" "$boot" 1 converged)
if [ "${answer%%$'\n'*}" = 200 ]; then
	ok "200 with the current token"
else
	ko "${answer%%$'\n'*} with the token we were just given"
fi
answer=$(sync_as "$rid" "$token" "$boot" 1 converged)
if [ "${answer%%$'\n'*}" = 401 ]; then
	ok "401 with the retired one"
else
	ko "${answer%%$'\n'*}, the old token outlived the rotation"
fi

say "5. the status endpoint"
code=$(curl -s -o /tmp/api-body -w '%{http_code}' \
	-H "X-Auth-Token: $(openstack token issue -f value -c id)" \
	"$NEUTRON/v2.0/appliance/routers/$rid")
body=$(cat /tmp/api-body)
if [ "$code" = 200 ]; then
	ok "200 for an administrator"
else
	ko "$code for an administrator, body: $body"
fi
if [ "$(printf '%s' "$body" | field '["state"]')" = converged ]; then
	ok "state is converged, and the API agrees with the database"
else
	ko "state is $(printf '%s' "$body" | field '["state"]'), expected converged"
fi

code=$(curl -s -o /dev/null -w '%{http_code}' \
	-H "X-Auth-Token: $SVC_TOKEN" "$NEUTRON/v2.0/appliance/routers/$rid")
if [ "$code" = 403 ]; then
	ok "403 for the appliance service user"
else
	ko "$code: an appliance can read the state of every other router"
fi

say "6. the service user can do nothing else"
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
	-H "X-Auth-Token: $SVC_TOKEN" -H 'Content-Type: application/json' \
	-d '{"network": {"name": "should-not-exist"}}' "$NEUTRON/v2.0/networks")
if [ "$code" = 403 ]; then
	ok "403 creating a network"
else
	ko "$code creating a network: the role grants more than the sync endpoint"
fi
# A read it would plausibly want, and the one that matters most: the
# router it syncs for. Nothing about an appliance's job requires it.
# (A list is not worth checking: neutron answers 200 and filters, and
# every project sees the external networks no matter what role it holds.)
code=$(curl -s -o /dev/null -w '%{http_code}' \
	-H "X-Auth-Token: $SVC_TOKEN" "$NEUTRON/v2.0/routers/$rid")
if [ "$code" = 404 ]; then
	ok "404 reading the neutron router itself"
else
	ko "$code reading the router: the service user sees more than it should"
fi

printf '\n%s\n' "pass=$pass fail=$fail"
[ "$fail" -eq 0 ]
REMOTE
