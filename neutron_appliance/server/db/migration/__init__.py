# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry

"""Alembic branch, registered through the neutron.db.alembic_migrations entry
point in pyproject.toml.

`neutron-db-manage` turns that entry point into the script location
`neutron_appliance.server.db.migration:alembic_migrations` and runs our
revisions alongside neutron's own, tracked in a separate version table.

    neutron-db-manage --subproject neutron-appliance upgrade heads
"""
