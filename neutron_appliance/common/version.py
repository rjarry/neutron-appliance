# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry

"""Protocol and schema versions, shared by both halves.

Every byte of the on-the-wire contract is a multi-release commitment: a new
server must keep working with an agent baked into an old image. Add fields,
never repurpose them.
"""

# bumped when the config document or sync payload gains fields
SCHEMA_VERSION = 1

# Versions this build can parse. The server must keep accepting every version
# it has ever shipped: an agent baked into an old image never changes.
SUPPORTED_SCHEMA_VERSIONS = frozenset({1})

# Version of the HTTP contract itself: paths, headers, status codes. It moves
# independently of SCHEMA_VERSION, which versions the payloads.
AGENT_API_VERSION = '1.0'
