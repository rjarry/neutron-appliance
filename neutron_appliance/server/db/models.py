# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry

"""Persisted appliance state. Never keep this in process memory.

One row per flavored router, created in the same transaction as the router
itself and deleted with it. Everything the reconciler (M4) and the sync
endpoint (M3) need to converge a router lives here, so that a restarted
neutron-server picks up exactly where it left off.
"""

from neutron_lib.db import constants as db_const
from neutron_lib.db import model_base
import sqlalchemy as sa

from neutron_appliance.common.protocol import ApplianceState


# sha256, hex encoded
_HASH_FIELD_SIZE = 64
# the service profile metainfo column is String(4096) in neutron
_METAINFO_FIELD_SIZE = 4096


class ApplianceRouter(model_base.BASEV2):
    """Appliance backing one Neutron router.

    `desired_generation` is bumped by the driver, in the transaction that
    changes the router. `applied_generation` is what the agent has reported
    as fully applied. Equal means converged, lower means converging; the
    comparison is the whole state machine.
    """

    __tablename__ = 'appliance_routers'

    router_id = sa.Column(
        sa.String(db_const.UUID_FIELD_SIZE),
        sa.ForeignKey('routers.id', ondelete='CASCADE'),
        primary_key=True,
    )
    # Nova instance, set by the reconciler once it has booted one. NULL
    # means "no appliance yet", which is the normal state right after
    # create.
    instance_id = sa.Column(sa.String(db_const.UUID_FIELD_SIZE))
    # The flavor is create-only, but the service profile behind it is
    # editable at runtime, so snapshot its metainfo here: an operator typo
    # must not retroactively rebuild every router.
    flavor_id = sa.Column(sa.String(db_const.UUID_FIELD_SIZE))
    flavor_snapshot = sa.Column(sa.String(_METAINFO_FIELD_SIZE))
    desired_generation = sa.Column(sa.Integer, nullable=False)
    applied_generation = sa.Column(sa.Integer, nullable=False)
    state = sa.Column(
        sa.String(db_const.STATUS_FIELD_SIZE),
        nullable=False,
        default=ApplianceState.CONVERGING.value,
    )
    # Updated on every sync call. This is the health signal: there is no
    # separate liveness check.
    last_seen = sa.Column(sa.DateTime)
    # Last value reported by the agent. A change means the appliance
    # rebooted or was rebuilt, with no guessing involved.
    boot_id = sa.Column(sa.String(db_const.UUID_FIELD_SIZE))
    # JSON list of human-readable strings from the last sync, surfaced by
    # the status API and by neutron-appliance-debug.
    errors = sa.Column(sa.Text)
    # Per-router secret, stored hashed and never logged. The previous hash
    # is kept with an expiry so that an agent crashing mid-rotation can
    # still authenticate with the token it has on disk.
    credential_hash = sa.Column(sa.String(_HASH_FIELD_SIZE))
    previous_credential_hash = sa.Column(sa.String(_HASH_FIELD_SIZE))
    previous_credential_expires_at = sa.Column(sa.DateTime)
