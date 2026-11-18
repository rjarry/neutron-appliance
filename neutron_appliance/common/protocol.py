# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry

"""Wire constants: paths, headers, states. See docs/protocol.md."""

from __future__ import annotations

import enum

from neutron_appliance.common.version import SUPPORTED_SCHEMA_VERSIONS


# The appliance endpoints live under the neutron API tree so that requests
# traverse the normal pipeline: no api-paste.ini change, no exempted path.
API_PREFIX = '/v2.0/appliance'
ROUTERS_PATH = f'{API_PREFIX}/routers'

CONTENT_TYPE = 'application/json'

# Keystone token obtained as the shared service user. It gets the request
# past keystonemiddleware and grants nothing else.
AUTH_TOKEN_HEADER = 'X-Auth-Token'
# Per-router secret. This is the real authorisation. Never log its value.
APPLIANCE_TOKEN_HEADER = 'X-Appliance-Token'

# Delay between two sync calls, unless the server says otherwise.
DEFAULT_POLL_INTERVAL_S = 30
# Bounds accepted for a server-provided interval, so that a bogus value
# cannot stop an appliance from ever calling home again.
MIN_POLL_INTERVAL_S = 1
MAX_POLL_INTERVAL_S = 3600
# Cap of the agent's exponential backoff after a failed sync.
MAX_BACKOFF_S = 300


def sync_path(router_id: str) -> str:
    """Path of the sync endpoint, relative to the neutron API root."""
    return f'{ROUTERS_PATH}/{router_id}/sync'


def status_path(router_id: str) -> str:
    """Path of the read-only status endpoint."""
    return f'{ROUTERS_PATH}/{router_id}'


class ApplianceState(str, enum.Enum):
    """Where a router is in its convergence cycle.

    Operators see three of these: `converged`, `converging` and `unknown`.
    `unknown` means no sync within N intervals; it is not an error, the
    appliance is most likely forwarding happily. `error` is reported by the
    agent and comes with human-readable strings in `errors[]`.
    """

    CONVERGED = 'converged'
    CONVERGING = 'converging'
    ERROR = 'error'
    UNKNOWN = 'unknown'

    def __str__(self) -> str:
        return self.value


# What an agent may report about itself. It cannot know that it is unknown:
# that state is derived by the server from the absence of a sync.
AGENT_STATES = frozenset(
    {
        ApplianceState.CONVERGED,
        ApplianceState.CONVERGING,
        ApplianceState.ERROR,
    }
)


class InterfaceRole(str, enum.Enum):
    """What an interface is for, in the agent's own terms.

    The agent knows nothing about Neutron: no network IDs, no port IDs, no
    subnet semantics. Only what it has to do with the interface.
    """

    MANAGEMENT = 'management'
    INTERNAL = 'internal'
    EXTERNAL = 'external'

    def __str__(self) -> str:
        return self.value


class ProtocolError(Exception):
    """A message that cannot be understood."""


class ValidationError(ProtocolError):
    """A field is missing, of the wrong type, or out of range."""

    def __init__(self, field: str, reason: str):
        self.field = field
        self.reason = reason
        super().__init__(f'{field}: {reason}')


class UnsupportedSchemaVersion(ProtocolError):
    """A peer speaks a version of the payload format that we do not.

    Deliberately not a ValidationError: the message may be perfectly well
    formed, it just belongs to a contract this build does not know.
    """

    def __init__(self, version: object):
        self.version = version
        known = sorted(SUPPORTED_SCHEMA_VERSIONS)
        supported = ', '.join(str(v) for v in known)
        super().__init__(
            f'unsupported schema_version {version!r}, this build speaks '
            f'{supported}'
        )
