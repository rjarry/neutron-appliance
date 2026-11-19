#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry
#
# Run the unit tests inside the VM, where Neutron exists.
#
# `make tests` on your workstation skips everything that imports Neutron,
# because the appliance image must never carry it and this repository does
# not depend on it. Those tests are not optional, they just need to run
# somewhere else. This is that somewhere else.
#
# Run ./sync.sh first. Any argument is passed to pytest.

set -eu
cd "$(dirname "$0")"
# shellcheck disable=SC1091
. ./env.sh
lab_check_ip

# devstack runs neutron's own tests with stestr, so pytest is usually not
# there. Installing it is a one-off and touches nothing devstack uses.
# shellcheck disable=SC2086,SC2029
exec ssh $lab_ssh_opts "$LAB_USER@$LAB_IP" \
	"$LAB_VENV/bin/python -c 'import pytest' 2>/dev/null ||
	 $LAB_VENV/bin/pip install -q pytest;
	 cd $LAB_SRC && $LAB_VENV/bin/python -m pytest -q tests/ $*"
