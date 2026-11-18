# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry

"""The L3ServiceProvider subclass loaded by neutron-server.

On the API path this computes desired state and commits it, and does nothing
else. It never calls Nova and never calls the appliance: callbacks run in the
API worker, and PRECOMMIT_* runs inside the open database transaction.
"""

# TODO: ApplianceL3Driver, ha_support/distributed_support = UNSUPPORTED
# TODO: registry callbacks bumping generation in the same transaction
