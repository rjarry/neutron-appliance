# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry

"""initial contract branch

Nothing to contract yet. The branch exists from the start because tooling
asks both branches for their head, and because the first schema change that
needs a shutdown must not also be the one that creates the branch.

Revision ID: b5b8528aa4e4
Revises: start_neutron_appliance
Create Date: 2026-11-18 00:00:00.000000
"""

from neutron_lib.db import constants as db_const


revision = 'b5b8528aa4e4'
down_revision = 'start_neutron_appliance'
branch_labels = (db_const.CONTRACT_BRANCH,)


def upgrade():
    pass
