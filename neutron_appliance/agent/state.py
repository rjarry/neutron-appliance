# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry

"""On-disk cache of the last applied configuration.

This is what buys back boot independence: the appliance must come up and
forward with the control plane unreachable.
"""

# TODO: atomic write (tmp + rename), load on boot
