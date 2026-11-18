#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry
#
# Follow the neutron logs, filtered to our code by default.
#
# Usage: ./logs.sh            # follow everything appliance-related
#        ./logs.sh -a         # follow all neutron logs, unfiltered
#        ./logs.sh -n 500     # last 500 lines instead of following

set -eu
cd "$(dirname "$0")"
# shellcheck disable=SC1091
. ./env.sh
lab_check_ip

units="-u devstack@neutron-api -u devstack@neutron-rpc-server"
units="$units -u devstack@neutron-periodic-workers"
args="-f -n 100"
filter=true

while [ $# -gt 0 ]; do
	case "$1" in
	-a) filter=false ;;
	-n) args="-n $2"; shift ;;
	*)  echo "usage: $0 [-a] [-n LINES]" >&2; exit 1 ;;
	esac
	shift
done

if [ "$filter" = true ]; then
	# shellcheck disable=SC2086,SC2029
	exec ssh $lab_ssh_opts -t "$LAB_USER@$LAB_IP" \
		"sudo journalctl $units $args | grep --line-buffered -iE 'appliance|Traceback|ERROR'"
fi
# shellcheck disable=SC2086,SC2029
exec ssh $lab_ssh_opts -t "$LAB_USER@$LAB_IP" "sudo journalctl $units $args"
