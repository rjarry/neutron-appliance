# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry

"""Breadcrumbs to /dev/console.

The only diagnostic channel that works with no network at all: these show up
in "openstack console log show".
"""

# TODO: best-effort write to /dev/console, never raise
