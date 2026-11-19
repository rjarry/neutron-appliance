# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry

"""Alembic environment for the appliance branch.

Run by neutron-db-manage, which fills in the script location and the
database connection before alembic is invoked. Our revisions are recorded
in their own version table, so neutron's timeline and ours never collide.
"""

from alembic import context
from neutron.db.migration import autogen
from neutron.db.migration.connection import DBConnection
from neutron_lib.db import model_base

from neutron_appliance.server.db import models
from neutron_appliance.server.db.migration.alembic_migrations import (
    VERSION_TABLE,
)


config = context.config
neutron_config = config.neutron_config
target_metadata = model_base.BASEV2.metadata

# Importing our models registers them in the shared neutron metadata, which
# also holds every neutron table. Autogenerate must only ever propose
# changes to ours.
OWN_TABLES = frozenset({models.ApplianceRouter.__tablename__})


def include_object(object_, name, type_, reflected, compare_to):
    if type_ == 'table':
        return name in OWN_TABLES
    return True


def run_migrations_offline():
    kwargs = {'version_table': VERSION_TABLE, 'include_object': include_object}
    if neutron_config.database.connection:
        kwargs['url'] = neutron_config.database.connection
    else:
        kwargs['dialect_name'] = neutron_config.database.engine
    context.configure(**kwargs)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    connection = config.attributes.get('connection')
    with DBConnection(neutron_config.database.connection, connection) as conn:
        context.configure(
            connection=conn,
            target_metadata=target_metadata,
            version_table=VERSION_TABLE,
            include_object=include_object,
            process_revision_directives=autogen.process_revision_directives,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
