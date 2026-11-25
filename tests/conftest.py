# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry

"""Fixtures shared by the tests that need a database.

A real sqlite database rather than a mocked session: the generation
counter and the credential state machine are the two places where a mock
would simply agree with whatever the code does.

Session scoped on purpose. The schema is built once, and the stand-in
`routers` table can only be declared once in a process.

Everything here needs neutron-lib, which this repository does not depend
on. The imports are inside the fixtures so that a workstation without the
server dependencies skips the tests that ask for them instead of failing
to collect anything at all.
"""

import pytest


ROUTER_ID = '7f0e2c3a-1111-4000-8000-000000000001'


@pytest.fixture(scope='session')
def conf():
    """An oslo.config that has been parsed, and reads no file.

    oslo.policy and oslo.db both refuse to work against a ConfigOpts that
    was never called, so this is not optional even for a test that only
    looks at rules.
    """
    cfg = pytest.importorskip('oslo_config.cfg')
    db_options = pytest.importorskip('oslo_db.options')
    # importing our own config module is what registers [appliance]
    pytest.importorskip('neutron_appliance.server.config')

    # default_config_files is not optional either: without it oslo.config
    # finds /etc/neutron/neutron.conf wherever neutron is deployed, and the
    # tests quietly run against that cloud's database instead of this one
    # where neutron itself is installed, register its core options too,
    # and before parsing: some are CLI options, which oslo.config refuses
    # to register once arguments have been parsed. Instantiating a neutron
    # worker reads some of these (e.g. 'setproctitle') even though this
    # package never sets any of them
    try:
        import neutron.conf.common as neutron_common_config  # pylint: disable=import-outside-toplevel
    except ImportError:
        pass
    else:
        neutron_common_config.register_core_common_config_opts(cfg.CONF)

    cfg.CONF([], project='neutron', default_config_files=[])
    db_options.set_defaults(cfg.CONF, connection='sqlite://')
    return cfg.CONF


@pytest.fixture(scope='session')
def context(conf):  # pylint: disable=unused-argument
    """An admin context on an empty in-memory database."""
    sa = pytest.importorskip('sqlalchemy')
    n_context = pytest.importorskip('neutron_lib.context')
    db_api = pytest.importorskip('neutron_lib.db.api')
    model_base = pytest.importorskip('neutron_lib.db.model_base')
    # importing the models is what puts our table in the metadata
    pytest.importorskip('neutron_appliance.server.db.models')

    # where neutron itself is installed, register every one of its models
    # up front: some of the modules under test (anything that touches
    # tagging) already pull in a few of them as a side effect of their own
    # imports, and a schema built from that partial set is missing the
    # tables those models' own foreign keys point to
    try:
        import neutron.db.migration.models.head  # noqa: F401 pylint: disable=import-outside-toplevel,unused-import
    except ImportError:
        pass

    # our table has a foreign key to neutron's routers; where neutron is
    # not installed, the import above found nothing and this stand-in is
    # the only definition of that table
    if 'routers' not in model_base.BASEV2.metadata.tables:
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
def make_router(context):
    """Creates neutron router rows, so that foreign keys have a target.

    Both tables are emptied afterwards. The database outlives every
    individual test, so a row left behind is a test that passes or fails
    depending on what ran before it.
    """
    sa = pytest.importorskip('sqlalchemy')
    db_api = pytest.importorskip('neutron_lib.db.api')
    model_base = pytest.importorskip('neutron_lib.db.model_base')
    # true where neutron itself is installed: its router also needs a row
    # in this table, which the stand-in schema has no use for
    has_standard_attr = (
        'standardattributes' in model_base.BASEV2.metadata.tables
    )
    created = []

    def make(router_id=ROUTER_ID):
        with db_api.CONTEXT_WRITER.using(context):
            if has_standard_attr:
                result = context.session.execute(
                    sa.text(
                        'insert into standardattributes (resource_type) '
                        "values ('routers')"
                    )
                )
                context.session.execute(
                    sa.text(
                        'insert into routers (id, standard_attr_id) '
                        'values (:id, :standard_attr_id)'
                    ),
                    {'id': router_id, 'standard_attr_id': result.lastrowid},
                )
            else:
                context.session.execute(
                    sa.text('insert into routers (id) values (:id)'),
                    {'id': router_id},
                )
        created.append(router_id)
        return router_id

    yield make

    for router_id in created:
        with db_api.CONTEXT_WRITER.using(context):
            context.session.execute(
                sa.text('delete from appliance_routers where router_id = :id'),
                {'id': router_id},
            )
            if has_standard_attr:
                context.session.execute(
                    sa.text(
                        'delete from standardattributes where id = ('
                        'select standard_attr_id from routers where id = :id'
                        ')'
                    ),
                    {'id': router_id},
                )
            context.session.execute(
                sa.text('delete from routers where id = :id'),
                {'id': router_id},
            )


@pytest.fixture
def router(make_router):
    """One neutron router row, the usual case."""
    return make_router()
