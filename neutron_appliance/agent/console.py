# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry

"""Breadcrumbs to /dev/console.

The only diagnostic channel that works with no network at all: these show up
in "openstack console log show". Writing them must never be a reason for the
agent to stop, so every failure is swallowed on purpose.
"""

from __future__ import annotations

import os
import time


CONSOLE = '/dev/console'

PREFIX = 'neutron-appliance-agent'


def say(message: str) -> None:
    """Write one timestamped line to the console, best effort."""
    stamp = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    line = f'{stamp} {PREFIX}: {message}\n'.encode()
    try:
        fd = os.open(CONSOLE, os.O_WRONLY | os.O_NOCTTY)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except OSError:
        pass
