# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry

"""On-disk cache of the last applied configuration.

This is what buys back boot independence: the appliance must come up and
forward with the control plane unreachable. The cache is therefore written
before the new generation is reported, and read before the first sync call.

Writes are atomic (temporary file, fsync, rename) because the file is read
during boot: a crash halfway through must leave either the previous complete
state or the new complete state, never a truncated document.
"""

from __future__ import annotations

import dataclasses
import json
import os
import tempfile

from neutron_appliance.common import protocol, schema


STATE_DIR = '/var/lib/neutron-appliance'
STATE_PATH = os.path.join(STATE_DIR, 'state.json')
CA_FILENAME = 'ca.pem'


class StateError(Exception):
    """The cache cannot be read or does not hold a valid state."""


@dataclasses.dataclass(frozen=True)
class State:
    """What survived the last boot: one document and the credential.

    The token is here rather than on the config drive alone because the
    server rotates it. A reboot that went back to the write-once token would
    use a credential the server has already retired.
    """

    applied_generation: int = 0
    config: schema.ConfigDocument | None = None
    router_token: str | None = dataclasses.field(default=None, repr=False)

    @classmethod
    def from_dict(cls, data: object) -> State:
        if not isinstance(data, dict):
            raise StateError('state is not a JSON object')
        generation = data.get('applied_generation', 0)
        if isinstance(generation, bool) or not isinstance(generation, int):
            raise StateError('applied_generation is not an integer')
        if generation < 0:
            raise StateError('applied_generation is negative')
        token = data.get('router_token')
        if token is not None and not isinstance(token, str):
            raise StateError('router_token is not a string')
        config = data.get('config')
        if config is not None:
            try:
                config = schema.ConfigDocument.from_dict(config)
            except protocol.ProtocolError as e:
                raise StateError(f'invalid cached config: {e}') from e
        return cls(
            applied_generation=generation,
            config=config,
            router_token=token,
        )

    def to_dict(self) -> dict:
        data = {'applied_generation': self.applied_generation}
        if self.config is not None:
            data['config'] = self.config.to_dict()
        if self.router_token is not None:
            data['router_token'] = self.router_token
        return data


def _atomic_write(path: str, text: str) -> None:
    directory = os.path.dirname(path) or '.'
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix='.state-', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        dir_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def save(value: State, path: str = STATE_PATH) -> None:
    """Persist one complete state, atomically."""
    _atomic_write(path, json.dumps(value.to_dict(), sort_keys=True))


def load(path: str = STATE_PATH) -> State:
    """Read the last applied state. Absent is an empty state, not an error."""
    try:
        with open(path, encoding='utf-8') as stream:
            text = stream.read()
    except FileNotFoundError:
        return State()
    except OSError as e:
        raise StateError(f'cannot read {path}: {e}') from e
    try:
        return State.from_dict(json.loads(text))
    except ValueError as e:
        raise StateError(f'invalid state file {path}: {e}') from e


def ensure_ca(ca_cert: str | None, path: str) -> str | None:
    """Write the bootstrap CA bundle so keystoneauth can verify against it."""
    if not ca_cert:
        return None
    _atomic_write(path, ca_cert)
    return path
