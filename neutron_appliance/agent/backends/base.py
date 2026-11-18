# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry

"""Data-plane backend interface.

One implementation today. The abstraction exists so that a DPDK backend stays
possible without reshaping the agent.
"""

# TODO: apply(document) -> applied state; observe() -> actual state
