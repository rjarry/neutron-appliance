# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry

"""Database access, against a real sqlite database.

The generation counter and the credential state machine are the whole of
the convergence and authentication logic, so they are worth exercising
through sqlalchemy rather than through a mock that would agree with
whatever the code does.

The `context` and `router` fixtures come from conftest.py. These need
neutron-lib and are skipped where the server dependencies are absent.
"""

import datetime

import pytest


sa = pytest.importorskip('sqlalchemy')
db_api = pytest.importorskip('neutron_lib.db.api')
appliance_db = pytest.importorskip('neutron_appliance.server.db.api')
credentials = pytest.importorskip('neutron_appliance.server.credentials')
protocol = pytest.importorskip('neutron_appliance.common.protocol')
schema = pytest.importorskip('neutron_appliance.common.schema')

BOOT_ID = 'e3b0c442-2222-4000-8000-000000000001'
OTHER_BOOT_ID = 'e3b0c442-2222-4000-8000-000000000002'
OTHER_ROUTER_ID = '7f0e2c3a-1111-4000-8000-000000000002'

# long enough that nothing in these tests expires by accident
GRACE = 3600


def report(
    boot_id=BOOT_ID,
    applied_generation=0,
    state=protocol.ApplianceState.CONVERGING,
    errors=(),
):
    """What an agent says about itself in one sync call."""
    return schema.SyncRequest(
        agent_version='0.0.1',
        boot_id=boot_id,
        uptime_s=10,
        applied_generation=applied_generation,
        state=state,
        errors=list(errors),
    )


def test_a_new_router_is_behind_its_appliance(context, router):
    appliance_db.create_router(
        context, router, flavor_id='flavor', flavor_snapshot='{}'
    )
    entry = appliance_db.get_router(context, router)
    # nothing has been applied yet, so the two must differ
    assert entry.desired_generation == appliance_db.FIRST_GENERATION
    assert not entry.applied_generation
    assert entry.state == protocol.ApplianceState.CONVERGING.value
    assert entry.flavor_id == 'flavor'
    assert entry.flavor_snapshot == '{}'
    assert entry.instance_id is None
    assert entry.last_seen is None
    appliance_db.delete_router(context, router)


def test_an_unknown_router_is_not_ours(context):
    assert appliance_db.get_router(context, 'nobody') is None
    assert appliance_db.delete_router(context, 'nobody') is False
    assert appliance_db.bump_generation(context, 'nobody') is None


def test_every_bump_produces_a_new_generation(context, router):
    appliance_db.create_router(context, router)
    seen = {appliance_db.FIRST_GENERATION}
    for _ in range(3):
        generation = appliance_db.bump_generation(context, router)
        # a repeated generation would let an agent skip a change
        assert generation not in seen
        seen.add(generation)
        assert appliance_db.get_router(context, router).desired_generation == (
            generation
        )
    appliance_db.delete_router(context, router)


def test_a_bump_marks_the_router_as_converging(context, router):
    appliance_db.create_router(context, router)
    with db_api.CONTEXT_WRITER.using(context):
        entry = appliance_db.get_router(context, router)
        entry.state = protocol.ApplianceState.CONVERGED.value
        entry.applied_generation = entry.desired_generation
    appliance_db.bump_generation(context, router)
    entry = appliance_db.get_router(context, router)
    assert entry.state == protocol.ApplianceState.CONVERGING.value
    assert entry.applied_generation < entry.desired_generation
    appliance_db.delete_router(context, router)


def test_listing_returns_every_router(context, router):
    assert appliance_db.get_routers(context) == []
    appliance_db.create_router(context, router)
    entries = appliance_db.get_routers(context)
    assert [entry.router_id for entry in entries] == [router]
    appliance_db.delete_router(context, router)
    assert appliance_db.get_routers(context) == []


def test_deleting_the_router_deletes_the_row(context, router):
    appliance_db.create_router(context, router)
    # the row is gone with its router even when nothing deletes it
    # explicitly, which is what the orphan sweep relies on
    with db_api.CONTEXT_WRITER.using(context):
        context.session.execute(sa.text('pragma foreign_keys = on'))
        context.session.execute(
            sa.text('delete from routers where id = :id'), {'id': router}
        )
    assert appliance_db.get_router(context, router) is None


@pytest.fixture
def appliance(context, router):
    """A recorded router holding a freshly issued credential."""
    appliance_db.create_router(context, router)
    return router, appliance_db.issue_credential(context, router)


def test_only_the_hash_of_a_token_is_stored(context, appliance):
    router, token = appliance
    entry = appliance_db.get_router(context, router)
    assert entry.credential_hash == credentials.hash_token(token)
    assert entry.credential_hash != token
    assert entry.previous_credential_hash is None


def test_a_router_with_no_appliance_has_no_credential(context):
    assert appliance_db.issue_credential(context, 'nobody') is None
    assert (
        appliance_db.sync_router(context, 'nobody', 'token', report(), GRACE)
        is None
    )


def test_a_sync_records_the_report(context, appliance):
    router, token = appliance
    outcome = appliance_db.sync_router(context, router, token, report(), GRACE)
    assert outcome.desired_generation == appliance_db.FIRST_GENERATION
    assert not outcome.applied_generation
    entry = appliance_db.get_router(context, router)
    # last_seen is the only health signal there is
    assert entry.last_seen is not None
    assert entry.boot_id == BOOT_ID
    assert entry.state == protocol.ApplianceState.CONVERGING.value


def test_converged_is_the_servers_comparison_not_the_agents_word(
    context, appliance
):
    router, token = appliance
    generation = appliance_db.FIRST_GENERATION
    # the agent claims to be converged on a generation that has since
    # been superseded, which is converging from here
    appliance_db.bump_generation(context, router)
    outcome = appliance_db.sync_router(
        context,
        router,
        token,
        report(
            applied_generation=generation,
            state=protocol.ApplianceState.CONVERGED,
        ),
        GRACE,
    )
    assert outcome.state == protocol.ApplianceState.CONVERGING.value

    token = outcome.new_token or token
    outcome = appliance_db.sync_router(
        context,
        router,
        token,
        report(
            applied_generation=outcome.desired_generation,
            state=protocol.ApplianceState.CONVERGED,
        ),
        GRACE,
    )
    assert outcome.state == protocol.ApplianceState.CONVERGED.value


def test_an_error_report_is_recorded_verbatim(context, appliance):
    router, token = appliance
    appliance_db.sync_router(
        context,
        router,
        token,
        report(state=protocol.ApplianceState.ERROR, errors=['no interface']),
        GRACE,
    )
    entry = appliance_db.get_router(context, router)
    assert entry.state == protocol.ApplianceState.ERROR.value
    assert 'no interface' in entry.errors


def test_another_routers_token_is_refused(context, make_router, appliance):
    router, _ = appliance
    other = make_router(OTHER_ROUTER_ID)
    appliance_db.create_router(context, other)
    stolen = appliance_db.issue_credential(context, other)
    # the credential is bound to the router in the URL: a valid token for
    # a different appliance must not open this one
    with pytest.raises(credentials.CredentialMismatch):
        appliance_db.sync_router(context, router, stolen, report(), GRACE)
    with pytest.raises(credentials.CredentialMismatch):
        appliance_db.sync_router(context, router, None, report(), GRACE)


def test_a_reboot_rotates_the_credential(context, appliance):
    router, token = appliance
    # the first sync of a fresh appliance is a boot_id change too, so the
    # token that came off the write-once config drive is used exactly once
    first = appliance_db.sync_router(context, router, token, report(), GRACE)
    assert first.new_token and first.new_token != token

    # steady state: same boot, no rotation
    second = appliance_db.sync_router(
        context, router, first.new_token, report(), GRACE
    )
    assert second.new_token is None

    third = appliance_db.sync_router(
        context,
        router,
        first.new_token,
        report(boot_id=OTHER_BOOT_ID),
        GRACE,
    )
    assert third.new_token and third.new_token != first.new_token


def test_a_rotation_lost_in_flight_leaves_the_old_token_working(
    context, appliance
):
    router, token = appliance
    rotated = appliance_db.sync_router(context, router, token, report(), GRACE)
    # the agent died before persisting rotated.new_token, so it still
    # holds the one it booted with. Without this it would be locked out
    # of its own control plane for good
    recovered = appliance_db.sync_router(
        context, router, token, report(), GRACE
    )
    assert recovered.new_token
    assert recovered.new_token not in (token, rotated.new_token)

    # and it may crash again: the token on its disk still works
    again = appliance_db.sync_router(context, router, token, report(), GRACE)
    assert again.new_token


def test_the_previous_token_is_retired_once_the_new_one_is_used(
    context, appliance
):
    router, token = appliance
    rotated = appliance_db.sync_router(context, router, token, report(), GRACE)
    appliance_db.sync_router(
        context, router, rotated.new_token, report(), GRACE
    )
    assert (
        appliance_db.get_router(context, router).previous_credential_hash
        is None
    )
    with pytest.raises(credentials.CredentialMismatch):
        appliance_db.sync_router(context, router, token, report(), GRACE)


def test_the_grace_window_ends(context, appliance):
    router, token = appliance
    appliance_db.sync_router(context, router, token, report(), GRACE)
    with db_api.CONTEXT_WRITER.using(context):
        entry = appliance_db.get_router(context, router)
        entry.previous_credential_expires_at -= datetime.timedelta(
            seconds=GRACE + 60
        )
    with pytest.raises(credentials.CredentialMismatch):
        appliance_db.sync_router(context, router, token, report(), GRACE)
