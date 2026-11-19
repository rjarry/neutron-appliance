# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry

"""Database access, against a real sqlite database.

The generation counter is the whole convergence state machine, so it is
worth exercising through sqlalchemy rather than through a mock that would
agree with whatever the code does.

These need neutron-lib and are skipped where the server dependencies are
absent.
"""

import pytest


cfg = pytest.importorskip('oslo_config.cfg')
db_options = pytest.importorskip('oslo_db.options')
sa = pytest.importorskip('sqlalchemy')
n_context = pytest.importorskip('neutron_lib.context')
db_api = pytest.importorskip('neutron_lib.db.api')
model_base = pytest.importorskip('neutron_lib.db.model_base')
appliance_db = pytest.importorskip('neutron_appliance.server.db.api')
protocol = pytest.importorskip('neutron_appliance.common.protocol')

ROUTER_ID = '7f0e2c3a-1111-4000-8000-000000000001'


@pytest.fixture(scope='module')
def context():
    """An admin context on an empty in-memory database."""
    # default_config_files is not optional: without it oslo.config finds
    # /etc/neutron/neutron.conf wherever neutron is deployed, and the tests
    # quietly run against that cloud's database instead of this one
    cfg.CONF([], project='neutron', default_config_files=[])
    db_options.set_defaults(cfg.CONF, connection='sqlite://')
    # our table has a foreign key to neutron's routers, and neutron's
    # models are not imported here: the one column we point at is enough
    # for sqlite to build the constraint
    sa.Table(
        'routers',
        model_base.BASEV2.metadata,
        sa.Column('id', sa.String(36), primary_key=True),
    )
    ctx = n_context.get_admin_context()
    with db_api.CONTEXT_WRITER.using(ctx):
        model_base.BASEV2.metadata.create_all(ctx.session.get_bind())
    return ctx


@pytest.fixture
def router(context):
    """A neutron router row, so that the foreign key has a target."""
    with db_api.CONTEXT_WRITER.using(context):
        context.session.execute(
            sa.text('insert into routers (id) values (:id)'),
            {'id': ROUTER_ID},
        )
    yield ROUTER_ID
    with db_api.CONTEXT_WRITER.using(context):
        context.session.execute(
            sa.text('delete from routers where id = :id'), {'id': ROUTER_ID}
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
