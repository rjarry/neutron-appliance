# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry

"""Stage pre-0 backend: log what would be applied, change nothing."""

from __future__ import annotations

import logging

from neutron_appliance.agent.backends import base
from neutron_appliance.common import schema


LOG = logging.getLogger(__name__)


class NoopBackend(base.Backend):
    """The only backend until stage 0. Deliberately applies nothing."""

    def apply(self, document: schema.ConfigDocument) -> None:
        LOG.info(
            'router %s generation %d: would apply %d interfaces and %d '
            'routes (noop backend, nothing changed)',
            document.router_id,
            document.generation,
            len(document.interfaces),
            len(document.routes),
        )
        for interface in document.interfaces:
            LOG.info(
                'router %s: interface %s role=%s addresses=%s mtu=%s',
                document.router_id,
                interface.mac,
                interface.role,
                interface.addresses,
                interface.mtu,
            )
        for route in document.routes:
            LOG.info(
                'router %s: route %s via %s',
                document.router_id,
                route.destination,
                route.nexthop,
            )
        LOG.debug(
            'router %s generation %d: document %s',
            document.router_id,
            document.generation,
            document,
        )

    def observe(self) -> schema.Observed:
        # pre-0 has no data interfaces to look at
        return schema.Observed()
