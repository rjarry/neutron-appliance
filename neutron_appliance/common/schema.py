# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry

"""Desired-state document and sync request/response models.

Rules that must survive refactoring:
  - interfaces are identified by MAC address, never by kernel device name
  - the document is complete desired state, never a delta
  - applying it twice is a no-op

Every model parses with from_dict() and serialises with to_dict(). Parsing is
strict about what it knows and silent about what it does not: unknown fields
are ignored, so that a newer server can add fields without breaking an agent
baked into an image months earlier. An unknown schema_version, on the other
hand, is refused outright.

Secrets are carried in fields excluded from repr(), because these objects end
up in log messages and tracebacks.
"""

from __future__ import annotations

import dataclasses
import datetime
import ipaddress
import re
from typing import Any, Callable
import uuid

from neutron_appliance.common.protocol import (
    AGENT_STATES,
    DEFAULT_POLL_INTERVAL_S,
    MAX_POLL_INTERVAL_S,
    MIN_POLL_INTERVAL_S,
    ApplianceState,
    InterfaceRole,
    UnsupportedSchemaVersion,
    ValidationError,
)
from neutron_appliance.common.version import (
    SCHEMA_VERSION,
    SUPPORTED_SCHEMA_VERSIONS,
)


_MISSING = object()

_MAC_RE = re.compile(r'^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$')

# smallest MTU an IPv4 router is allowed to have, and the largest value the
# kernel will take on a virtio link
_MIN_MTU = 68
_MAX_MTU = 65535

Parser = Callable[[str, Any], Any]


def _take(data: dict, key: str, parse: Parser, default: Any = _MISSING) -> Any:
    """Pull one field out of a decoded JSON object and validate it.

    A field that is present but null is treated as absent: an agent that does
    not know what to put in a field should be able to leave it out either way.
    """
    value = data.get(key, None)
    if value is None:
        if default is _MISSING:
            raise ValidationError(key, 'required field is missing')
        return default
    return parse(key, value)


def _mapping(name: str, value: Any) -> dict:
    if not isinstance(value, dict):
        raise ValidationError(name, 'expected an object')
    return value


def _string(name: str, value: Any) -> str:
    if not isinstance(value, str):
        raise ValidationError(name, 'expected a string')
    if not value:
        raise ValidationError(name, 'expected a non-empty string')
    return value


def _boolean(name: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValidationError(name, 'expected a boolean')
    return value


def _integer(minimum: int | None = None, maximum: int | None = None) -> Parser:
    def parse(name: str, value: Any) -> int:
        # bool is an int in python, and never a valid number here
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValidationError(name, 'expected an integer')
        if minimum is not None and value < minimum:
            raise ValidationError(name, f'must be >= {minimum}')
        if maximum is not None and value > maximum:
            raise ValidationError(name, f'must be <= {maximum}')
        return value

    return parse


def _list_of(parse: Parser) -> Parser:
    def parse_list(name: str, value: Any) -> list:
        if not isinstance(value, list):
            raise ValidationError(name, 'expected a list')
        return [parse(f'{name}[{i}]', v) for i, v in enumerate(value)]

    return parse_list


def _model(cls: type) -> Parser:
    def parse(name: str, value: Any) -> Any:
        try:
            return cls.from_dict(_mapping(name, value))
        except ValidationError as e:
            if e.field == name:
                raise
            # name the element, not just the field: interfaces[1].mac
            raise ValidationError(f'{name}.{e.field}', e.reason) from e

    return parse


def _enum(enum_cls: type, allowed: frozenset | None = None) -> Parser:
    def parse(name: str, value: Any) -> Any:
        try:
            member = enum_cls(_string(name, value))
        except ValueError as e:
            raise ValidationError(name, f'unknown value {value!r}') from e
        if allowed is not None and member not in allowed:
            raise ValidationError(name, f'value {value!r} is not allowed here')
        return member

    return parse


def _uuid(name: str, value: Any) -> str:
    value = _string(name, value)
    try:
        uuid.UUID(value)
    except ValueError as e:
        raise ValidationError(name, 'expected a UUID') from e
    return value


def _mac(name: str, value: Any) -> str:
    value = _string(name, value)
    if not _MAC_RE.match(value):
        raise ValidationError(name, 'expected a MAC address')
    # interfaces are matched by MAC, so matching must not depend on case
    return value.lower()


def _cidr(name: str, value: Any) -> str:
    """An address with its prefix length, host bits kept: 10.0.0.1/24."""
    value = _string(name, value)
    if '/' not in value:
        raise ValidationError(name, 'expected a prefix length')
    try:
        ipaddress.ip_interface(value)
    except ValueError as e:
        raise ValidationError(name, 'expected an IP address and prefix') from e
    return value


def _prefix(name: str, value: Any) -> str:
    value = _string(name, value)
    try:
        ipaddress.ip_network(value)
    except ValueError as e:
        raise ValidationError(name, 'expected an IP prefix') from e
    return value


def _address(name: str, value: Any) -> str:
    value = _string(name, value)
    try:
        ipaddress.ip_address(value)
    except ValueError as e:
        raise ValidationError(name, 'expected an IP address') from e
    return value


def _timestamp(name: str, value: Any) -> str:
    """An RFC 3339 timestamp, kept as the string it arrived as."""
    value = _string(name, value)
    try:
        # fromisoformat() only learned to read a trailing Z in python 3.11
        datetime.datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as e:
        raise ValidationError(name, 'expected an RFC 3339 timestamp') from e
    return value


def _url(name: str, value: Any) -> str:
    value = _string(name, value)
    if not value.startswith(('http://', 'https://')):
        raise ValidationError(name, 'expected an http or https URL')
    return value


def _schema_version(name: str, value: Any) -> int:
    version = _integer(minimum=1)(name, value)
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise UnsupportedSchemaVersion(version)
    return version


@dataclasses.dataclass(frozen=True)
class Route:
    """A static route, in the terms the agent will install it."""

    destination: str
    nexthop: str

    @classmethod
    def from_dict(cls, data: Any) -> Route:
        data = _mapping(cls.__name__, data)
        return cls(
            destination=_take(data, 'destination', _prefix),
            nexthop=_take(data, 'nexthop', _address),
        )

    def to_dict(self) -> dict:
        return {'destination': self.destination, 'nexthop': self.nexthop}


@dataclasses.dataclass(frozen=True)
class Interface:
    """One data-plane interface of the desired state.

    Identified by MAC because kernel device names are not stable across a
    reboot, a rebuild, or a hot attach.
    """

    mac: str
    role: InterfaceRole
    addresses: list = dataclasses.field(default_factory=list)
    mtu: int | None = None

    @classmethod
    def from_dict(cls, data: Any) -> Interface:
        data = _mapping(cls.__name__, data)
        return cls(
            mac=_take(data, 'mac', _mac),
            role=_take(data, 'role', _enum(InterfaceRole)),
            addresses=_take(data, 'addresses', _list_of(_cidr), []),
            mtu=_take(data, 'mtu', _integer(_MIN_MTU, _MAX_MTU), None),
        )

    def to_dict(self) -> dict:
        data = {
            'mac': self.mac,
            'role': self.role.value,
            'addresses': list(self.addresses),
        }
        if self.mtu is not None:
            data['mtu'] = self.mtu
        return data


@dataclasses.dataclass(frozen=True)
class ConfigDocument:
    """Complete desired state for one router. Never a delta.

    `generation` is monotonic per router and bumped by the driver in the same
    transaction as the change. `applied == desired` is converged,
    `applied < desired` is converging. An agent ignores any document whose
    generation is not greater than what it has applied, which makes a stale
    read harmless.
    """

    router_id: str
    generation: int
    admin_state_up: bool = True
    interfaces: list = dataclasses.field(default_factory=list)
    routes: list = dataclasses.field(default_factory=list)
    schema_version: int = SCHEMA_VERSION

    @classmethod
    def from_dict(cls, data: Any) -> ConfigDocument:
        data = _mapping(cls.__name__, data)
        return cls(
            schema_version=_take(data, 'schema_version', _schema_version),
            router_id=_take(data, 'router_id', _uuid),
            generation=_take(data, 'generation', _integer(minimum=0)),
            admin_state_up=_take(data, 'admin_state_up', _boolean, True),
            interfaces=_take(
                data, 'interfaces', _list_of(_model(Interface)), []
            ),
            routes=_take(data, 'routes', _list_of(_model(Route)), []),
        )

    def to_dict(self) -> dict:
        return {
            'schema_version': self.schema_version,
            'router_id': self.router_id,
            'generation': self.generation,
            'admin_state_up': self.admin_state_up,
            'interfaces': [i.to_dict() for i in self.interfaces],
            'routes': [r.to_dict() for r in self.routes],
        }


@dataclasses.dataclass(frozen=True)
class ObservedInterface:
    """An interface as the agent actually sees it, for diagnosis."""

    mac: str
    name: str
    up: bool

    @classmethod
    def from_dict(cls, data: Any) -> ObservedInterface:
        data = _mapping(cls.__name__, data)
        return cls(
            mac=_take(data, 'mac', _mac),
            name=_take(data, 'name', _string),
            up=_take(data, 'up', _boolean),
        )

    def to_dict(self) -> dict:
        return {'mac': self.mac, 'name': self.name, 'up': self.up}


@dataclasses.dataclass(frozen=True)
class Observed:
    """What the agent reports about the world it woke up in.

    An object rather than a bare list, because this is where everything the
    server may want to see about a live appliance will accumulate.
    """

    interfaces: list = dataclasses.field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Any) -> Observed:
        data = _mapping(cls.__name__, data)
        return cls(
            interfaces=_take(
                data, 'interfaces', _list_of(_model(ObservedInterface)), []
            ),
        )

    def to_dict(self) -> dict:
        return {'interfaces': [i.to_dict() for i in self.interfaces]}


@dataclasses.dataclass(frozen=True)
class SyncRequest:
    """One request, both jobs: report state, ask for desired state.

    `applied_generation` is what the agent has *fully* applied. Reporting a
    generation that is not applied is the one thing an agent must never do.
    """

    agent_version: str
    boot_id: str
    uptime_s: int
    applied_generation: int
    state: ApplianceState
    errors: list = dataclasses.field(default_factory=list)
    observed: Observed = dataclasses.field(default_factory=Observed)
    schema_version: int = SCHEMA_VERSION

    @classmethod
    def from_dict(cls, data: Any) -> SyncRequest:
        data = _mapping(cls.__name__, data)
        return cls(
            schema_version=_take(data, 'schema_version', _schema_version),
            agent_version=_take(data, 'agent_version', _string),
            boot_id=_take(data, 'boot_id', _uuid),
            uptime_s=_take(data, 'uptime_s', _integer(minimum=0)),
            applied_generation=_take(
                data, 'applied_generation', _integer(minimum=0)
            ),
            state=_take(
                data, 'state', _enum(ApplianceState, allowed=AGENT_STATES)
            ),
            errors=_take(data, 'errors', _list_of(_string), []),
            observed=_take(data, 'observed', _model(Observed), Observed()),
        )

    def to_dict(self) -> dict:
        return {
            'schema_version': self.schema_version,
            'agent_version': self.agent_version,
            'boot_id': self.boot_id,
            'uptime_s': self.uptime_s,
            'applied_generation': self.applied_generation,
            'state': self.state.value,
            'errors': list(self.errors),
            'observed': self.observed.to_dict(),
        }


@dataclasses.dataclass(frozen=True)
class CredentialUpdate:
    """A rotated per-router token, carried in a sync response.

    This is the whole rotation mechanism: there is no inbound channel to the
    appliance. The agent persists the new token atomically before using it,
    and the server keeps accepting the previous one until the new one is.
    """

    router_token: str = dataclasses.field(repr=False)

    @classmethod
    def from_dict(cls, data: Any) -> CredentialUpdate:
        data = _mapping(cls.__name__, data)
        return cls(router_token=_take(data, 'router_token', _string))

    def to_dict(self) -> dict:
        return {'router_token': self.router_token}


@dataclasses.dataclass(frozen=True)
class SyncResponse:
    """The server's answer: how things should be, and how often to ask.

    `config` is omitted when the agent is already at the current generation,
    which makes the common case a small heartbeat. `credential` appears only
    when rotating.
    """

    generation: int
    poll_interval_s: int = DEFAULT_POLL_INTERVAL_S
    config: ConfigDocument | None = None
    credential: CredentialUpdate | None = None
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.config is not None:
            if self.config.generation != self.generation:
                raise ValidationError(
                    'config.generation',
                    'does not match the generation of the response',
                )

    @classmethod
    def from_dict(cls, data: Any) -> SyncResponse:
        data = _mapping(cls.__name__, data)
        return cls(
            schema_version=_take(data, 'schema_version', _schema_version),
            generation=_take(data, 'generation', _integer(minimum=0)),
            poll_interval_s=_take(
                data,
                'poll_interval_s',
                _integer(MIN_POLL_INTERVAL_S, MAX_POLL_INTERVAL_S),
                DEFAULT_POLL_INTERVAL_S,
            ),
            config=_take(data, 'config', _model(ConfigDocument), None),
            credential=_take(
                data, 'credential', _model(CredentialUpdate), None
            ),
        )

    def to_dict(self) -> dict:
        data = {
            'schema_version': self.schema_version,
            'generation': self.generation,
            'poll_interval_s': self.poll_interval_s,
        }
        if self.config is not None:
            data['config'] = self.config.to_dict()
        if self.credential is not None:
            data['credential'] = self.credential.to_dict()
        return data


@dataclasses.dataclass(frozen=True)
class Credential:
    """Both secrets an agent needs, written once to the config drive.

    The keystone service user gets the request past keystonemiddleware and
    nothing else; the router token decides which router's configuration may
    be read.
    """

    auth_url: str
    username: str
    password: str = dataclasses.field(repr=False)
    project_name: str
    router_token: str = dataclasses.field(repr=False)
    user_domain_name: str = 'Default'
    project_domain_name: str = 'Default'

    @classmethod
    def from_dict(cls, data: Any) -> Credential:
        data = _mapping(cls.__name__, data)
        return cls(
            auth_url=_take(data, 'auth_url', _url),
            username=_take(data, 'username', _string),
            password=_take(data, 'password', _string),
            project_name=_take(data, 'project_name', _string),
            router_token=_take(data, 'router_token', _string),
            user_domain_name=_take(
                data, 'user_domain_name', _string, 'Default'
            ),
            project_domain_name=_take(
                data, 'project_domain_name', _string, 'Default'
            ),
        )

    def to_dict(self) -> dict:
        return {
            'auth_url': self.auth_url,
            'username': self.username,
            'password': self.password,
            'project_name': self.project_name,
            'router_token': self.router_token,
            'user_domain_name': self.user_domain_name,
            'project_domain_name': self.project_domain_name,
        }


@dataclasses.dataclass(frozen=True)
class Bootstrap:
    """The write-once blob the server puts on the instance's config drive.

    Only values that never change belong here. Anything rotatable arrives in
    a sync response instead, because a config drive cannot be rewritten.
    """

    router_id: str
    endpoint: str
    sync_path: str
    credential: Credential
    ca_cert: str | None = None
    poll_interval_s: int = DEFAULT_POLL_INTERVAL_S
    schema_version: int = SCHEMA_VERSION

    @classmethod
    def from_dict(cls, data: Any) -> Bootstrap:
        data = _mapping(cls.__name__, data)
        return cls(
            schema_version=_take(data, 'schema_version', _schema_version),
            router_id=_take(data, 'router_id', _uuid),
            endpoint=_take(data, 'endpoint', _url),
            sync_path=_take(data, 'sync_path', _string),
            credential=_take(data, 'credential', _model(Credential)),
            ca_cert=_take(data, 'ca_cert', _string, None),
            poll_interval_s=_take(
                data,
                'poll_interval_s',
                _integer(MIN_POLL_INTERVAL_S, MAX_POLL_INTERVAL_S),
                DEFAULT_POLL_INTERVAL_S,
            ),
        )

    def to_dict(self) -> dict:
        data = {
            'schema_version': self.schema_version,
            'router_id': self.router_id,
            'endpoint': self.endpoint,
            'sync_path': self.sync_path,
            'poll_interval_s': self.poll_interval_s,
            'credential': self.credential.to_dict(),
        }
        if self.ca_cert is not None:
            data['ca_cert'] = self.ca_cert
        return data


@dataclasses.dataclass(frozen=True)
class RouterStatus:
    """The authoritative answer of the read-only status endpoint.

    Router tags mirror this for convenience, but they are user-writable and
    therefore not the source of truth.
    """

    router_id: str
    state: ApplianceState
    desired_generation: int
    applied_generation: int
    last_seen: str | None = None
    instance_id: str | None = None
    errors: list = dataclasses.field(default_factory=list)
    schema_version: int = SCHEMA_VERSION

    @classmethod
    def from_dict(cls, data: Any) -> RouterStatus:
        data = _mapping(cls.__name__, data)
        return cls(
            schema_version=_take(data, 'schema_version', _schema_version),
            router_id=_take(data, 'router_id', _uuid),
            state=_take(data, 'state', _enum(ApplianceState)),
            desired_generation=_take(
                data, 'desired_generation', _integer(minimum=0)
            ),
            applied_generation=_take(
                data, 'applied_generation', _integer(minimum=0)
            ),
            last_seen=_take(data, 'last_seen', _timestamp, None),
            instance_id=_take(data, 'instance_id', _uuid, None),
            errors=_take(data, 'errors', _list_of(_string), []),
        )

    def to_dict(self) -> dict:
        return {
            'schema_version': self.schema_version,
            'router_id': self.router_id,
            'state': self.state.value,
            'desired_generation': self.desired_generation,
            'applied_generation': self.applied_generation,
            'last_seen': self.last_seen,
            'instance_id': self.instance_id,
            'errors': list(self.errors),
        }
