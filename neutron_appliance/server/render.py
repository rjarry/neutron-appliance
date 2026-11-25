# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry

"""Server-side state turned into the documents the protocol defines.

The one place where Neutron's vocabulary is translated into the agent's.
Everything downstream of here -- the sync response, the agent, the backend
-- knows nothing about networks, ports or subnets.

The router arrives as a plain dictionary, the shape the L3 plugin returns,
and the appliance row as anything with the right attributes, so that this
stays testable without neutron.
"""

from __future__ import annotations

import datetime

from oslo_serialization import jsonutils

from neutron_appliance.common import protocol, schema
from neutron_appliance.common.protocol import ApplianceState


def bootstrap(
    router_id: str,
    endpoint: str,
    credential: schema.Credential,
    ca_cert: str | None,
    poll_interval_s: int,
) -> schema.Bootstrap:
    """The write-once blob for one router's config drive.

    Pure assembly: gathering the endpoint, the credential and the CA
    certificate is the reconciler's job, because it needs Keystone and
    `[nova]` config to do it. This only knows how to turn the result into
    the document the protocol defines.
    """
    return schema.Bootstrap(
        router_id=router_id,
        endpoint=endpoint,
        sync_path=protocol.sync_path(router_id),
        credential=credential,
        ca_cert=ca_cert,
        poll_interval_s=poll_interval_s,
    )


def config_document(router: dict, generation: int) -> schema.ConfigDocument:
    """Complete desired state for one router, at one generation.

    Stage pre-0 has no data plane: `interfaces` and `routes` are empty by
    construction, not by accident, and the agent's only backend logs what
    it would do and applies nothing. Stage 0 fills them in.
    """
    return schema.ConfigDocument(
        router_id=router['id'],
        generation=generation,
        # a router that is administratively down is still configured, it
        # just does not forward. The agent needs to be told either way
        admin_state_up=bool(router.get('admin_state_up', True)),
        interfaces=[],
        routes=[],
    )


def _rfc3339(when: datetime.datetime | None) -> str | None:
    """A naive UTC column value, as the protocol spells timestamps."""
    if when is None:
        return None
    return when.replace(microsecond=0).isoformat() + 'Z'


def _observed_state(entry, stale_after_s: int, now) -> ApplianceState:
    """The state to report, which is not always the state recorded.

    `unknown` means the appliance has stopped answering, not that anything
    is wrong with it: it is most likely still forwarding, we just cannot
    ask. A router that has *never* synced is a different thing and stays
    `converging` -- the control plane is still building it, which is the
    truth and is far more useful to an operator than `unknown`.
    """
    state = ApplianceState(entry.state)
    if entry.last_seen is None:
        return state
    if (now - entry.last_seen).total_seconds() > stale_after_s:
        return ApplianceState.UNKNOWN
    return state


def router_status(entry, stale_after_s: int, now) -> schema.RouterStatus:
    """The authoritative answer of the read-only status endpoint."""
    return schema.RouterStatus(
        router_id=entry.router_id,
        state=_observed_state(entry, stale_after_s, now),
        desired_generation=entry.desired_generation,
        applied_generation=entry.applied_generation,
        last_seen=_rfc3339(entry.last_seen),
        instance_id=entry.instance_id,
        errors=jsonutils.loads(entry.errors) if entry.errors else [],
    )
