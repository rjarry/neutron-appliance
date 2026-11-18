# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry

"""Nova client wrapper: boot, hot-attach, delete, in the service project."""

# TODO: ports are always hot-attached, never supplied at servers.create():
# TODO: Nova rejects ipless ports at boot. See docs/02-hooking-neutron.md 2.9
