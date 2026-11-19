# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry

"""The schema the model declares and the schema the migration creates.

A column added to one and forgotten in the other is invisible until a
deployment upgrades its database and neutron-server starts selecting a
column that is not there. Compare the two here instead.

This needs alembic, which comes with neutron; it is skipped where the
server dependencies are absent.
"""

import importlib.util
import pathlib
from unittest import mock

import pytest


sa = pytest.importorskip('sqlalchemy')
pytest.importorskip('alembic')
models = pytest.importorskip('neutron_appliance.server.db.models')

VERSIONS = (
    pathlib.Path(__file__).parent.parent
    / 'neutron_appliance'
    / 'server'
    / 'db'
    / 'migration'
    / 'alembic_migrations'
    / 'versions'
)


def load(name):
    path = VERSIONS / name
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def created_tables():
    """Every table the expand revision creates, as sqlalchemy sees it."""
    module = load('c717236fa7e9_expand_initial.py')
    with mock.patch.object(module, 'op') as op:
        module.upgrade()
    metadata = sa.MetaData()
    return {
        call.args[0]: sa.Table(call.args[0], metadata, *call.args[1:])
        for call in op.create_table.call_args_list
    }


def test_the_branch_heads_are_the_declared_revisions():
    # neutron-db-manage reads these files and compares them to the
    # timeline; a stale one fails check_migration
    for name, head in (
        ('EXPAND_HEAD', 'c717236fa7e9_expand_initial.py'),
        ('CONTRACT_HEAD', 'b5b8528aa4e4_contract_initial.py'),
    ):
        assert (VERSIONS / name).read_text().strip() == load(head).revision


def test_the_revisions_hang_off_the_same_root():
    root = load('start_neutron_appliance.py')
    assert root.down_revision is None
    for name in (
        'c717236fa7e9_expand_initial.py',
        'b5b8528aa4e4_contract_initial.py',
    ):
        assert load(name).down_revision == root.revision


def test_the_migration_creates_the_model_table(created_tables):
    assert set(created_tables) == {models.ApplianceRouter.__tablename__}


def test_the_columns_match_the_model(created_tables):
    expected = models.ApplianceRouter.__table__
    created = created_tables[expected.name]
    assert [column.name for column in created.columns] == [
        column.name for column in expected.columns
    ]
    for column in created.columns:
        reference = expected.columns[column.name]
        assert str(column.type) == str(reference.type), column.name
        assert column.nullable == reference.nullable, column.name


def test_the_router_is_the_primary_key_and_a_foreign_key(created_tables):
    created = created_tables[models.ApplianceRouter.__tablename__]
    assert [column.name for column in created.primary_key] == ['router_id']
    (foreign,) = created.foreign_keys
    assert foreign.parent.name == 'router_id'
    assert foreign.target_fullname == 'routers.id'
    # the row is meaningless without its router, and nothing else deletes it
    assert foreign.ondelete == 'CASCADE'
