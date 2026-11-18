# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry

"""Agent entry point: read bootstrap, apply cached config, then sync."""


def main() -> int:
    # TODO: the agent must apply its cached configuration BEFORE contacting
    # TODO: the control plane. Forwarding never depends on reachability.
    raise NotImplementedError
