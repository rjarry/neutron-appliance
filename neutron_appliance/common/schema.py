# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry

"""Desired-state document and sync request/response models.

Rules that must survive refactoring:
  - interfaces are identified by MAC address, never by kernel device name
  - the document is complete desired state, never a delta
  - applying it twice is a no-op
"""

# TODO: ConfigDocument, SyncRequest, SyncResponse + validation
