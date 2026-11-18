# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry

"""The wire constants are part of the contract. Pin them down."""

import json

from neutron_appliance.common import protocol
from neutron_appliance.common.version import (
    SCHEMA_VERSION,
    SUPPORTED_SCHEMA_VERSIONS,
)


ROUTER_ID = '7f0e2c3a-1111-4222-8333-444455556666'


def test_paths():
    assert (
        protocol.sync_path(ROUTER_ID)
        == f'/v2.0/appliance/routers/{ROUTER_ID}/sync'
    )
    assert (
        protocol.status_path(ROUTER_ID)
        == f'/v2.0/appliance/routers/{ROUTER_ID}'
    )


def test_headers():
    assert protocol.AUTH_TOKEN_HEADER == 'X-Auth-Token'
    assert protocol.APPLIANCE_TOKEN_HEADER == 'X-Appliance-Token'
    assert protocol.CONTENT_TYPE == 'application/json'


def test_current_schema_version_is_supported():
    assert SCHEMA_VERSION in SUPPORTED_SCHEMA_VERSIONS


def test_states_serialise_as_their_wire_value():
    assert str(protocol.ApplianceState.CONVERGED) == 'converged'
    assert str(protocol.InterfaceRole.EXTERNAL) == 'external'
    # the str mixin is what lets a state go straight into a json payload
    assert json.dumps(protocol.ApplianceState.ERROR) == '"error"'
    assert json.dumps(protocol.InterfaceRole.INTERNAL) == '"internal"'


def test_an_agent_cannot_report_unknown():
    # unknown is derived by the server from the absence of a sync
    assert protocol.ApplianceState.UNKNOWN not in protocol.AGENT_STATES
    assert protocol.AGENT_STATES == {
        protocol.ApplianceState.CONVERGED,
        protocol.ApplianceState.CONVERGING,
        protocol.ApplianceState.ERROR,
    }


def test_validation_error_keeps_the_field_name():
    e = protocol.ValidationError('interfaces[0].mac', 'expected a string')
    assert e.field == 'interfaces[0].mac'
    assert e.reason == 'expected a string'
    assert str(e) == 'interfaces[0].mac: expected a string'
    assert isinstance(e, protocol.ProtocolError)


def test_unsupported_schema_version_names_what_we_speak():
    e = protocol.UnsupportedSchemaVersion(99)
    assert e.version == 99
    assert '99' in str(e)
    assert '1' in str(e)
    assert isinstance(e, protocol.ProtocolError)
    # a message from the future is not malformed, just unknown
    assert not isinstance(e, protocol.ValidationError)


def test_poll_interval_bounds_are_sane():
    assert (
        protocol.MIN_POLL_INTERVAL_S
        <= protocol.DEFAULT_POLL_INTERVAL_S
        <= protocol.MAX_POLL_INTERVAL_S
    )
