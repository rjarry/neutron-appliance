# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry

"""Read the write-once bootstrap blob from the config drive.

Only values that never change belong there. Anything rotatable arrives in the
sync response instead, because the config drive cannot be rewritten.

Nova writes the blob into the config drive's `openstack/latest/user_data`.
An image that carries cloud-init also leaves a copy at
`/var/lib/cloud/instance/user-data.txt`; the appliance image deliberately
does not, so the agent has to find the drive itself and mount it read-only.
"""

from __future__ import annotations

import json
import os
import subprocess

from neutron_appliance.common import protocol, schema


LABEL = 'config-2'
DEVICE = f'/dev/disk/by-label/{LABEL}'
MOUNT_POINT = '/run/neutron-appliance/config-drive'
USER_DATA = os.path.join('openstack', 'latest', 'user_data')

# What cloud-init leaves behind, on images that happen to carry it.
CLOUD_INIT_PATHS = (
    '/var/lib/cloud/instance/user-data.txt',
    '/var/lib/cloud/data/user-data.txt',
)


class BootstrapError(Exception):
    """The config drive is missing, unreadable, or holds no valid blob."""


def _read(path: str) -> str:
    try:
        with open(path, encoding='utf-8') as stream:
            return stream.read()
    except OSError as e:
        raise BootstrapError(f'cannot read {path}: {e}') from e


def _mount_config_drive() -> str:
    """Mount the config drive, read-only, and return its user_data path."""
    path = os.path.join(MOUNT_POINT, USER_DATA)
    if os.path.exists(path):
        return path
    if not os.path.exists(DEVICE):
        raise BootstrapError(f'no config drive: {DEVICE} does not exist')
    try:
        os.makedirs(MOUNT_POINT, mode=0o700, exist_ok=True)
        subprocess.run(
            ['mount', '-o', 'ro', DEVICE, MOUNT_POINT],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        detail = e.stderr.strip() or str(e)
        raise BootstrapError(f'cannot mount {DEVICE}: {detail}') from e
    except OSError as e:
        raise BootstrapError(f'cannot mount {DEVICE}: {e}') from e
    return path


def find() -> str:
    """Path of the bootstrap blob, preferring cloud-init's copy."""
    for path in CLOUD_INIT_PATHS:
        if os.path.exists(path):
            return path
    return _mount_config_drive()


def loads(text: str) -> schema.Bootstrap:
    """Parse one bootstrap blob. Kept free of the filesystem for the tests."""
    try:
        data = json.loads(text)
    except ValueError as e:
        raise BootstrapError(f'bootstrap is not valid JSON: {e}') from e
    try:
        return schema.Bootstrap.from_dict(data)
    except protocol.ProtocolError as e:
        raise BootstrapError(f'invalid bootstrap: {e}') from e


def load(path: str | None = None) -> schema.Bootstrap:
    """Read and parse the bootstrap blob from the config drive."""
    return loads(_read(path or find()))
