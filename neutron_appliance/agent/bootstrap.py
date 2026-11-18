# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry

"""Read the write-once bootstrap blob from the config drive.

Only values that never change belong there. Anything rotatable arrives in the
sync response instead.
"""

# TODO: locate and parse the config drive payload
