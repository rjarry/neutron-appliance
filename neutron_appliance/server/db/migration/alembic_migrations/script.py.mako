## Lines starting with two hashes are mako comments: they document the
## template itself and are not written to the generated revision.
## SPDX-License-Identifier: Apache-2.0
## Copyright (c) 2026 Robin Jarry
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) ${create_date.year} <your name>

"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision}
Create Date: ${create_date}
"""

from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
% if branch_labels:
branch_labels = ${repr(branch_labels)}
% endif


def upgrade():
    ${upgrades if upgrades else "pass"}
