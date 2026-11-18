#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry
# Run a command in, or open a shell on, the lab VM.
set -eu
cd "$(dirname "$0")"
# shellcheck disable=SC1091
. ./env.sh
lab_check_ip
# shellcheck disable=SC2086
exec ssh $lab_ssh_opts "$LAB_USER@$LAB_IP" "$@"
