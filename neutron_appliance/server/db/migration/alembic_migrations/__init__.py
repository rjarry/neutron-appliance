# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry

"""Our own alembic branch, versioned independently of neutron's.

Our revisions must not land in neutron's `alembic_version` table: the two
timelines are unrelated and neutron would consider our head an unknown
revision.

Every revision lives directly in `versions/`, with its branch in the file
name rather than in a `versions/<release>/<branch>/` directory. Neutron only
adds a release directory to `version_locations` when that release is listed
in the `RELEASES` tuple of the *installed* neutron (`db/migration/cli.py`,
code-read and measured): a directory named after a release that neutron has
not heard of yet is skipped, and `neutron-db-manage upgrade heads` then
reports success having created nothing at all. The root directory is always
searched, whatever neutron is installed, and that is worth more here than
matching the layout of in-tree subprojects while the target version is still
open (Q-H). Keep 'expand' or 'contract' in each file name: that is how
`neutron-db-manage check_migration` recognises a branch.
"""

VERSION_TABLE = 'alembic_version_appliance'
