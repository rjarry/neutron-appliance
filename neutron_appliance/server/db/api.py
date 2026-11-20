# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry

"""Database access for the appliance table.

Every function takes a neutron context and runs in its transaction when
there already is one. The driver calls these from PRECOMMIT_* callbacks, so
the row and the router it describes are committed together or not at all.
"""

from __future__ import annotations

import dataclasses
import datetime

from neutron_lib.db import api as db_api
from oslo_log import log as logging
from oslo_serialization import jsonutils
from oslo_utils import timeutils

from neutron_appliance.common.protocol import ApplianceState
from neutron_appliance.common.schema import SyncRequest
from neutron_appliance.server import credentials
from neutron_appliance.server.db import models


LOG = logging.getLogger(__name__)

# Generation of the first configuration document of a router. Zero is
# reserved for "the agent has applied nothing yet", so the desired
# generation of a brand new router is already ahead of it.
FIRST_GENERATION = 1

# Which of the two stored hashes a presented token matched.
CURRENT = 'current'
PREVIOUS = 'previous'


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


@dataclasses.dataclass(frozen=True)
class SyncOutcome:
    """What one sync call left behind, for the caller to answer with.

    A plain value rather than the ORM row: the row's attributes expire
    when the transaction closes, and this is read after it has.
    """

    desired_generation: int
    applied_generation: int
    state: str
    # Set only when the answer must carry a rotated token. Clear text,
    # alive for exactly as long as it takes to serialise the response.
    new_token: str | None = dataclasses.field(default=None, repr=False)


def _match_credential(entry, token, now):
    """Which stored hash `token` matches, or None.

    Both hashes are tried, because an agent that crashed between
    receiving a rotated token and persisting it still holds the old one.
    The previous hash stops being accepted once it expires, and the
    comparison itself is constant time.
    """
    if credentials.matches(token, entry.credential_hash):
        return CURRENT
    expires = entry.previous_credential_expires_at
    if expires is not None and expires > now:
        if credentials.matches(token, entry.previous_credential_hash):
            return PREVIOUS
    return None


def _rotate(entry, now, grace_period_s, keep_previous=False):
    """Give a row a new token and return its clear text.

    `keep_previous` is for the one case that is easy to get wrong: the
    agent answered with the *previous* token, which means the token we
    handed it last time never reached its disk. Moving the current hash
    into the previous slot would then throw away the only token the agent
    actually has, and the next crash mid-rotation would lock the appliance
    out for good. So the fallback stays where it is and only its expiry is
    pushed back.
    """
    token = credentials.generate()
    if not keep_previous:
        entry.previous_credential_hash = entry.credential_hash
    if entry.previous_credential_hash:
        entry.previous_credential_expires_at = now + datetime.timedelta(
            seconds=grace_period_s
        )
    entry.credential_hash = credentials.hash_token(token)
    return token


def _report_state(entry, report):
    """The state to record from what the agent says about itself.

    The agent's own word is not taken for `converged`: it reports what it
    has applied, and whether that is current is the server's comparison to
    make. An agent can legitimately be converged on a generation that has
    since been superseded.
    """
    if report.state is ApplianceState.ERROR:
        return ApplianceState.ERROR.value
    if (
        report.state is ApplianceState.CONVERGED
        and report.applied_generation == entry.desired_generation
    ):
        return ApplianceState.CONVERGED.value
    return ApplianceState.CONVERGING.value


@db_api.retry_if_session_inactive()
def issue_credential(context, router_id, grace_period_s=0):
    """Mint a per-router token. Returns the clear text, or None.

    Called before an appliance is built, to put a token on its config
    drive. Any token the router already had keeps working for the grace
    period, so re-issuing one for a router whose appliance is up does not
    cut it off mid-poll.

    The clear text is returned once and never stored. A caller that loses
    it has to issue another.
    """
    now = timeutils.utcnow()
    with db_api.CONTEXT_WRITER.using(context):
        entry = (
            context.session.query(models.ApplianceRouter)
            .filter_by(router_id=router_id)
            .with_for_update()
            .one_or_none()
        )
        if entry is None:
            return None
        token = _rotate(entry, now, grace_period_s)
    LOG.info('router %s: per-router credential issued', router_id)
    return token


@db_api.retry_if_session_inactive()
def sync_router(
    context,
    router_id: str,
    token,
    report: SyncRequest,
    grace_period_s: int,
) -> SyncOutcome | None:
    """Authenticate an agent, record its report, rotate when needed.

    One locked row, one transaction: authentication, the report and any
    rotation have to be atomic, or two polls racing each other can both
    decide to rotate and only one of the two tokens survives.

    Returns None when the router is not ours, which the caller answers
    with a 404. Raises CredentialMismatch for a token that matches
    neither hash, which the caller answers with a 401.
    """
    now = timeutils.utcnow()
    with db_api.CONTEXT_WRITER.using(context):
        entry = (
            context.session.query(models.ApplianceRouter)
            .filter_by(router_id=router_id)
            .with_for_update()
            .one_or_none()
        )
        if entry is None:
            return None

        matched = _match_credential(entry, token, now)
        if matched is None:
            LOG.warning(
                'router %s: sync refused, the credential presented matches '
                'neither the current nor the previous token',
                router_id,
            )
            raise credentials.CredentialMismatch(router_id)

        # A reboot or a rebuild. Also the first call of a fresh appliance,
        # whose token came off a write-once config drive: rotating here is
        # what keeps that copy from being a credential for the life of the
        # router.
        rebooted = entry.boot_id != report.boot_id
        new_token = None

        if matched == CURRENT and entry.previous_credential_hash:
            # the agent is using the token we last gave it, so the
            # rotation landed and the old one can go
            entry.previous_credential_hash = None
            entry.previous_credential_expires_at = None
            LOG.info(
                'router %s: previous credential retired, the agent is on '
                'the current one',
                router_id,
            )

        if matched == PREVIOUS or rebooted:
            new_token = _rotate(
                entry, now, grace_period_s, keep_previous=matched == PREVIOUS
            )
            LOG.info(
                'router %s: credential rotated (%s)',
                router_id,
                'the last rotation never reached the agent'
                if matched == PREVIOUS
                else f'boot {report.boot_id}',
            )

        previous_state = entry.state
        entry.last_seen = now
        entry.boot_id = report.boot_id
        entry.applied_generation = report.applied_generation
        entry.errors = (
            jsonutils.dumps(report.errors) if report.errors else None
        )
        entry.state = _report_state(entry, report)
        outcome = SyncOutcome(
            desired_generation=entry.desired_generation,
            applied_generation=entry.applied_generation,
            state=entry.state,
            new_token=new_token,
        )

    if outcome.state != previous_state:
        LOG.info(
            'router %s: %s -> %s, generation %d/%d',
            router_id,
            previous_state,
            outcome.state,
            outcome.applied_generation,
            outcome.desired_generation,
        )
    return outcome
