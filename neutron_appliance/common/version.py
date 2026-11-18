# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry

"""Protocol and schema versions, shared by both halves.

Every byte of the on-the-wire contract is a multi-release commitment: a new
server must keep working with an agent baked into an old image. Add fields,
never repurpose them.
"""

# bumped when the config document or sync payload gains fields
SCHEMA_VERSION = 1
