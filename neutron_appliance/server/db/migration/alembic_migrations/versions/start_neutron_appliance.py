# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry

"""start neutron-appliance chain

Root of our migration timeline. It creates nothing: the expand and contract
branches hang off it, and alembic needs a single base to branch from.

Revision ID: start_neutron_appliance
Revises: None
Create Date: 2026-11-18 00:00:00.000000
"""

revision = 'start_neutron_appliance'
down_revision = None


def upgrade():
    pass
