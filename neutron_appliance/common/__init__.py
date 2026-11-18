# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry

"""
Code shared by the server and the in-VM agent.

Nothing here may import neutron or neutron-lib: this package is installed
inside the appliance image, which must not carry the Neutron server stack.
The rule is enforced twice, by ruff banned-api in ruff.toml and by
tests/test_import_isolation.py.

See docs/protocol.md for the contract these modules implement.
"""
