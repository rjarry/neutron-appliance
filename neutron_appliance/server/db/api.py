# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry

"""Database access for the appliance table.

Every function takes a neutron context and runs in its transaction when
there already is one. The driver calls these from PRECOMMIT_* callbacks, so
the row and the router it describes are committed together or not at all.
"""

from neutron_lib.db import api as db_api
from oslo_log import log as logging

from neutron_appliance.common.protocol import ApplianceState
from neutron_appliance.server.db import models


LOG = logging.getLogger(__name__)

# Generation of the first configuration document of a router. Zero is
# reserved for "the agent has applied nothing yet", so the desired
# generation of a brand new router is already ahead of it.
FIRST_GENERATION = 1


@db_api.retry_if_session_inactive()
def create_router(context, router_id, flavor_id=None, flavor_snapshot=None):
    """Record a new appliance router. Returns the created row."""
    entry = models.ApplianceRouter(
        router_id=router_id,
        flavor_id=flavor_id,
        flavor_snapshot=flavor_snapshot,
        desired_generation=FIRST_GENERATION,
        applied_generation=0,
        state=ApplianceState.CONVERGING.value,
    )
    with db_api.CONTEXT_WRITER.using(context):
        context.session.add(entry)
    LOG.info(
        'router %s: appliance record created, generation %d',
        router_id,
        FIRST_GENERATION,
    )
    return entry


@db_api.retry_if_session_inactive()
def delete_router(context, router_id):
    """Forget an appliance router. Returns True if a row was deleted.

    The instance it points at, if any, is not touched here: destroying it
    is the reconciler's job, and the orphan sweep is what guarantees it
    happens even if neutron-server dies in between.
    """
    with db_api.CONTEXT_WRITER.using(context):
        deleted = (
            context.session.query(models.ApplianceRouter)
            .filter_by(router_id=router_id)
            .delete()
        )
    if deleted:
        LOG.info('router %s: appliance record deleted', router_id)
    return bool(deleted)


@db_api.retry_if_session_inactive()
@db_api.CONTEXT_READER
def get_router(context, router_id):
    """Return one appliance row, or None if the router is not ours."""
    return (
        context.session.query(models.ApplianceRouter)
        .filter_by(router_id=router_id)
        .one_or_none()
    )


@db_api.retry_if_session_inactive()
@db_api.CONTEXT_READER
def get_routers(context):
    """Return every appliance row. The reconciler's desired state."""
    return context.session.query(models.ApplianceRouter).all()


@db_api.retry_if_session_inactive()
def bump_generation(context, router_id):
    """Mark a router as changed. Returns the new generation, or None.

    The row is locked for the increment: two updates landing on the same
    router at the same time must not read the same generation and write
    the same new one, which would make the agent apply one of the two
    changes and never hear about the other.
    """
    with db_api.CONTEXT_WRITER.using(context):
        entry = (
            context.session.query(models.ApplianceRouter)
            .filter_by(router_id=router_id)
            .with_for_update()
            .one_or_none()
        )
        if entry is None:
            return None
        entry.desired_generation += 1
        entry.state = ApplianceState.CONVERGING.value
        generation = entry.desired_generation
    LOG.info('router %s: generation bumped to %d', router_id, generation)
    return generation
