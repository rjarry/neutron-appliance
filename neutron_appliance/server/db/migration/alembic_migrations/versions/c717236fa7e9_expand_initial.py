# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry

"""initial appliance router table

Revision ID: c717236fa7e9
Revises: start_neutron_appliance
Create Date: 2026-11-18 00:00:00.000000
"""

from alembic import op
from neutron_lib.db import constants as db_const
import sqlalchemy as sa


revision = 'c717236fa7e9'
down_revision = 'start_neutron_appliance'
branch_labels = (db_const.EXPAND_BRANCH,)

# sha256, hex encoded
HASH_FIELD_SIZE = 64
# same size as neutron's service profile metainfo column
METAINFO_FIELD_SIZE = 4096


def upgrade():
    # The engine is named explicitly rather than taken from the
    # --mysql-engine option: this table is ours alone, and it has to be
    # transactional because rows are written in the same transaction as
    # the router they describe.
    op.create_table(
        'appliance_routers',
        sa.Column(
            'router_id',
            sa.String(db_const.UUID_FIELD_SIZE),
            nullable=False,
        ),
        sa.Column(
            'instance_id', sa.String(db_const.UUID_FIELD_SIZE), nullable=True
        ),
        sa.Column(
            'flavor_id', sa.String(db_const.UUID_FIELD_SIZE), nullable=True
        ),
        sa.Column(
            'flavor_snapshot', sa.String(METAINFO_FIELD_SIZE), nullable=True
        ),
        sa.Column('desired_generation', sa.Integer(), nullable=False),
        sa.Column('applied_generation', sa.Integer(), nullable=False),
        sa.Column(
            'state', sa.String(db_const.STATUS_FIELD_SIZE), nullable=False
        ),
        sa.Column('last_seen', sa.DateTime(), nullable=True),
        sa.Column(
            'boot_id', sa.String(db_const.UUID_FIELD_SIZE), nullable=True
        ),
        sa.Column('errors', sa.Text(), nullable=True),
        sa.Column(
            'credential_hash', sa.String(HASH_FIELD_SIZE), nullable=True
        ),
        sa.Column(
            'previous_credential_hash',
            sa.String(HASH_FIELD_SIZE),
            nullable=True,
        ),
        sa.Column(
            'previous_credential_expires_at', sa.DateTime(), nullable=True
        ),
        sa.ForeignKeyConstraint(
            ['router_id'], ['routers.id'], ondelete='CASCADE'
        ),
        sa.PrimaryKeyConstraint('router_id'),
        mysql_engine='InnoDB',
    )
