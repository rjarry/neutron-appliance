# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry

"""Both halves of the project meet here, so this is where drift is caught.

The two properties that matter: what we serialise we can parse back, and a
payload from a newer peer is tolerated as long as it speaks our schema
version.
"""

import json

import pytest

from neutron_appliance.common import schema
from neutron_appliance.common.protocol import (
    DEFAULT_POLL_INTERVAL_S,
    ApplianceState,
    InterfaceRole,
    UnsupportedSchemaVersion,
    ValidationError,
)
from neutron_appliance.common.version import SCHEMA_VERSION


ROUTER_ID = '7f0e2c3a-1111-4222-8333-444455556666'
BOOT_ID = 'e3b0c442-2222-4333-8444-555566667777'
INSTANCE_ID = 'c1a2b3d4-3333-4444-8555-666677778888'


def roundtrip(obj):
    """Parse back what we serialised, through real json."""
    cls = type(obj)
    return cls.from_dict(json.loads(json.dumps(obj.to_dict())))


#
# the configuration document
#


def test_empty_document_is_the_pre_0_payload():
    doc = schema.ConfigDocument(router_id=ROUTER_ID, generation=1)
    assert doc.to_dict() == {
        'schema_version': SCHEMA_VERSION,
        'router_id': ROUTER_ID,
        'generation': 1,
        'admin_state_up': True,
        'interfaces': [],
        'routes': [],
    }
    assert roundtrip(doc) == doc


def test_full_document_roundtrip():
    doc = schema.ConfigDocument(
        router_id=ROUTER_ID,
        generation=42,
        admin_state_up=False,
        interfaces=[
            schema.Interface(
                mac='fa:16:3e:11:22:33',
                role=InterfaceRole.INTERNAL,
                addresses=['10.0.0.1/24', 'fd00::1/64'],
                mtu=1442,
            ),
            schema.Interface(
                mac='fa:16:3e:44:55:66',
                role=InterfaceRole.EXTERNAL,
                addresses=['172.24.4.9/24'],
            ),
        ],
        routes=[
            schema.Route(destination='0.0.0.0/0', nexthop='172.24.4.1'),
            schema.Route(destination='192.168.0.0/24', nexthop='10.0.0.5'),
        ],
    )
    assert roundtrip(doc) == doc
    # an interface without an MTU does not claim one
    assert 'mtu' not in doc.to_dict()['interfaces'][1]


def test_macs_are_matched_case_insensitively():
    iface = schema.Interface.from_dict(
        {'mac': 'FA:16:3E:11:22:33', 'role': 'internal'}
    )
    assert iface.mac == 'fa:16:3e:11:22:33'


def test_unknown_fields_are_ignored():
    # a newer server may add fields; an old agent must not choke on them
    doc = schema.ConfigDocument.from_dict(
        {
            'schema_version': SCHEMA_VERSION,
            'router_id': ROUTER_ID,
            'generation': 7,
            'ipsec': [{'peer': '198.51.100.1'}],
            'interfaces': [
                {
                    'mac': 'fa:16:3e:11:22:33',
                    'role': 'internal',
                    'vrf': 'blue',
                }
            ],
        }
    )
    assert doc.generation == 7
    assert doc.interfaces[0].mac == 'fa:16:3e:11:22:33'


def test_unknown_schema_version_is_refused():
    with pytest.raises(UnsupportedSchemaVersion):
        schema.ConfigDocument.from_dict(
            {
                'schema_version': SCHEMA_VERSION + 1,
                'router_id': ROUTER_ID,
                'generation': 1,
            }
        )


def test_schema_version_must_be_an_integer():
    with pytest.raises(ValidationError) as exc:
        schema.ConfigDocument.from_dict(
            {'schema_version': '1', 'router_id': ROUTER_ID, 'generation': 1}
        )
    assert exc.value.field == 'schema_version'


@pytest.mark.parametrize(
    'data,field',
    [
        ({}, 'schema_version'),
        ({'schema_version': SCHEMA_VERSION}, 'router_id'),
        ({'schema_version': SCHEMA_VERSION, 'router_id': 'r1'}, 'router_id'),
        (
            {'schema_version': SCHEMA_VERSION, 'router_id': ROUTER_ID},
            'generation',
        ),
        (
            {
                'schema_version': SCHEMA_VERSION,
                'router_id': ROUTER_ID,
                'generation': -1,
            },
            'generation',
        ),
        (
            {
                'schema_version': SCHEMA_VERSION,
                'router_id': ROUTER_ID,
                'generation': 1,
                'admin_state_up': 'yes',
            },
            'admin_state_up',
        ),
        (
            {
                'schema_version': SCHEMA_VERSION,
                'router_id': ROUTER_ID,
                'generation': 1,
                'interfaces': {},
            },
            'interfaces',
        ),
    ],
)
def test_document_rejects(data, field):
    with pytest.raises(ValidationError) as exc:
        schema.ConfigDocument.from_dict(data)
    assert exc.value.field == field


def test_nested_errors_name_the_offending_element():
    with pytest.raises(ValidationError) as exc:
        schema.ConfigDocument.from_dict(
            {
                'schema_version': SCHEMA_VERSION,
                'router_id': ROUTER_ID,
                'generation': 1,
                'interfaces': [
                    {'mac': 'fa:16:3e:11:22:33', 'role': 'internal'},
                    {'mac': 'nope', 'role': 'internal'},
                ],
            }
        )
    assert exc.value.field == 'interfaces[1].mac'


def test_document_must_be_an_object():
    with pytest.raises(ValidationError) as exc:
        schema.ConfigDocument.from_dict([])
    assert exc.value.field == 'ConfigDocument'


@pytest.mark.parametrize(
    'data,field',
    [
        ({'role': 'internal'}, 'mac'),
        ({'mac': 'fa:16:3e:11:22:33'}, 'role'),
        ({'mac': 'fa:16:3e:11:22:33', 'role': 'uplink'}, 'role'),
        ({'mac': 12, 'role': 'internal'}, 'mac'),
        ({'mac': '', 'role': 'internal'}, 'mac'),
        (
            {
                'mac': 'fa:16:3e:11:22:33',
                'role': 'internal',
                'addresses': ['10.0.0.1'],
            },
            'addresses[0]',
        ),
        (
            {
                'mac': 'fa:16:3e:11:22:33',
                'role': 'internal',
                'addresses': ['10.0.0.300/24'],
            },
            'addresses[0]',
        ),
        (
            {'mac': 'fa:16:3e:11:22:33', 'role': 'internal', 'mtu': 0},
            'mtu',
        ),
        (
            {'mac': 'fa:16:3e:11:22:33', 'role': 'internal', 'mtu': 1 << 20},
            'mtu',
        ),
        (
            {'mac': 'fa:16:3e:11:22:33', 'role': 'internal', 'mtu': True},
            'mtu',
        ),
    ],
)
def test_interface_rejects(data, field):
    with pytest.raises(ValidationError) as exc:
        schema.Interface.from_dict(data)
    assert exc.value.field == field


@pytest.mark.parametrize(
    'data,field',
    [
        ({'nexthop': '10.0.0.1'}, 'destination'),
        ({'destination': '0.0.0.0/0'}, 'nexthop'),
        ({'destination': 'default', 'nexthop': '10.0.0.1'}, 'destination'),
        ({'destination': '10.0.0.1/24', 'nexthop': '10.0.0.1'}, 'destination'),
        ({'destination': '0.0.0.0/0', 'nexthop': '10.0.0.1/24'}, 'nexthop'),
    ],
)
def test_route_rejects(data, field):
    with pytest.raises(ValidationError) as exc:
        schema.Route.from_dict(data)
    assert exc.value.field == field


#
# the sync call
#


def minimal_request(**kwargs):
    args = {
        'agent_version': '0.0.1',
        'boot_id': BOOT_ID,
        'uptime_s': 1234,
        'applied_generation': 41,
        'state': ApplianceState.CONVERGED,
    }
    args.update(kwargs)
    return schema.SyncRequest(**args)


def test_sync_request_roundtrip():
    req = minimal_request(
        state=ApplianceState.ERROR,
        errors=['interface fa:16:3e:11:22:33 not present after 90s'],
        observed=schema.Observed(
            interfaces=[
                schema.ObservedInterface(
                    mac='fa:16:3e:11:22:33', name='eth1', up=True
                )
            ]
        ),
    )
    assert roundtrip(req) == req
    assert req.to_dict()['state'] == 'error'


def test_sync_request_defaults():
    req = minimal_request()
    assert not req.errors
    assert req.observed == schema.Observed(interfaces=[])
    assert req.schema_version == SCHEMA_VERSION
    assert roundtrip(req) == req


def test_sync_request_rejects_a_server_only_state():
    # an agent cannot know that it is unknown
    with pytest.raises(ValidationError) as exc:
        schema.SyncRequest.from_dict(
            dict(minimal_request().to_dict(), state='unknown')
        )
    assert exc.value.field == 'state'


@pytest.mark.parametrize(
    'override,field',
    [
        ({'agent_version': 7}, 'agent_version'),
        ({'boot_id': 'not-a-uuid'}, 'boot_id'),
        ({'uptime_s': -1}, 'uptime_s'),
        ({'applied_generation': 'one'}, 'applied_generation'),
        ({'state': 'wedged'}, 'state'),
        ({'errors': 'it broke'}, 'errors'),
        ({'errors': [None]}, 'errors[0]'),
        ({'observed': []}, 'observed'),
        (
            {'observed': {'interfaces': [{'mac': 'x'}]}},
            'observed.interfaces[0].mac',
        ),
    ],
)
def test_sync_request_rejects(override, field):
    with pytest.raises(ValidationError) as exc:
        schema.SyncRequest.from_dict(
            dict(minimal_request().to_dict(), **override)
        )
    assert exc.value.field == field


def test_observed_interface_roundtrip():
    iface = schema.ObservedInterface(
        mac='FA:16:3E:11:22:33', name='eth1', up=False
    )
    parsed = roundtrip(iface)
    assert parsed.mac == 'fa:16:3e:11:22:33'
    assert parsed.name == 'eth1'
    assert parsed.up is False


def test_observed_interface_rejects_a_missing_link_state():
    with pytest.raises(ValidationError) as exc:
        schema.ObservedInterface.from_dict(
            {'mac': 'fa:16:3e:11:22:33', 'name': 'eth1'}
        )
    assert exc.value.field == 'up'


def test_heartbeat_response_carries_no_config():
    resp = schema.SyncResponse(generation=41)
    assert resp.to_dict() == {
        'schema_version': SCHEMA_VERSION,
        'generation': 41,
        'poll_interval_s': DEFAULT_POLL_INTERVAL_S,
    }
    assert roundtrip(resp) == resp


def test_response_with_config_and_rotation():
    resp = schema.SyncResponse(
        generation=42,
        poll_interval_s=60,
        config=schema.ConfigDocument(router_id=ROUTER_ID, generation=42),
        credential=schema.CredentialUpdate(router_token='s3cr3t'),
    )
    assert roundtrip(resp) == resp
    assert resp.to_dict()['credential'] == {'router_token': 's3cr3t'}


def test_response_config_generation_must_match():
    # a document that does not match the generation it is announced under
    # would make the agent report a generation it never applied
    with pytest.raises(ValidationError) as exc:
        schema.SyncResponse(
            generation=42,
            config=schema.ConfigDocument(router_id=ROUTER_ID, generation=41),
        )
    assert exc.value.field == 'config.generation'


@pytest.mark.parametrize(
    'override,field',
    [
        ({'generation': None}, 'generation'),
        ({'poll_interval_s': 0}, 'poll_interval_s'),
        ({'poll_interval_s': 86400}, 'poll_interval_s'),
        ({'config': 'none'}, 'config'),
        ({'credential': {}}, 'credential.router_token'),
    ],
)
def test_sync_response_rejects(override, field):
    with pytest.raises(ValidationError) as exc:
        schema.SyncResponse.from_dict(
            dict(schema.SyncResponse(generation=1).to_dict(), **override)
        )
    assert exc.value.field == field


def test_rotated_token_stays_out_of_repr():
    update = schema.CredentialUpdate(router_token='s3cr3t')
    assert 's3cr3t' not in repr(update)


#
# bootstrap
#


def credential(**kwargs):
    args = {
        'auth_url': 'https://keystone.example.com:5000/v3',
        'username': 'neutron-appliance',
        'password': 'service-user-secret',
        'project_name': 'service',
        'router_token': 'per-router-secret',
    }
    args.update(kwargs)
    return schema.Credential(**args)


def bootstrap(**kwargs):
    args = {
        'router_id': ROUTER_ID,
        'endpoint': 'https://neutron.example.com:9696',
        'sync_path': f'/v2.0/appliance/routers/{ROUTER_ID}/sync',
        'credential': credential(),
    }
    args.update(kwargs)
    return schema.Bootstrap(**args)


def test_bootstrap_roundtrip():
    blob = bootstrap(
        ca_cert='-----BEGIN CERTIFICATE-----\nMIIB\n',
        poll_interval_s=15,
    )
    assert roundtrip(blob) == blob


def test_bootstrap_defaults():
    blob = bootstrap()
    assert blob.poll_interval_s == DEFAULT_POLL_INTERVAL_S
    assert blob.ca_cert is None
    # a well-known CA means no certificate to ship
    assert 'ca_cert' not in blob.to_dict()
    assert blob.credential.user_domain_name == 'Default'
    assert roundtrip(blob) == blob


def test_bootstrap_secrets_stay_out_of_repr():
    blob = bootstrap()
    assert 'service-user-secret' not in repr(blob)
    assert 'per-router-secret' not in repr(blob)
    assert 'neutron-appliance' in repr(blob)


@pytest.mark.parametrize(
    'override,field',
    [
        ({'router_id': 'r1'}, 'router_id'),
        ({'endpoint': 'neutron.example.com'}, 'endpoint'),
        ({'sync_path': ''}, 'sync_path'),
        ({'credential': None}, 'credential'),
        ({'ca_cert': 42}, 'ca_cert'),
        ({'poll_interval_s': -5}, 'poll_interval_s'),
    ],
)
def test_bootstrap_rejects(override, field):
    with pytest.raises(ValidationError) as exc:
        schema.Bootstrap.from_dict(dict(bootstrap().to_dict(), **override))
    assert exc.value.field == field


@pytest.mark.parametrize(
    'override,field',
    [
        ({'auth_url': 'keystone.example.com'}, 'auth_url'),
        ({'username': ''}, 'username'),
        ({'password': None}, 'password'),
        ({'project_name': []}, 'project_name'),
        ({'router_token': 0}, 'router_token'),
        ({'user_domain_name': 1}, 'user_domain_name'),
        ({'project_domain_name': 1}, 'project_domain_name'),
    ],
)
def test_credential_rejects(override, field):
    with pytest.raises(ValidationError) as exc:
        schema.Credential.from_dict(dict(credential().to_dict(), **override))
    assert exc.value.field == field


#
# status
#


def test_status_roundtrip():
    status = schema.RouterStatus(
        router_id=ROUTER_ID,
        state=ApplianceState.CONVERGED,
        desired_generation=42,
        applied_generation=42,
        last_seen='2026-11-18T10:03:11Z',
        instance_id=INSTANCE_ID,
    )
    assert roundtrip(status) == status
    assert status.to_dict()['state'] == 'converged'


def test_status_of_a_router_that_has_never_synced():
    status = schema.RouterStatus(
        router_id=ROUTER_ID,
        state=ApplianceState.UNKNOWN,
        desired_generation=1,
        applied_generation=0,
    )
    assert status.to_dict()['last_seen'] is None
    assert status.to_dict()['instance_id'] is None
    assert roundtrip(status) == status


@pytest.mark.parametrize(
    'override,field',
    [
        ({'state': 'broken'}, 'state'),
        ({'desired_generation': -1}, 'desired_generation'),
        ({'last_seen': 'yesterday'}, 'last_seen'),
        ({'instance_id': 'i-123'}, 'instance_id'),
        ({'errors': {}}, 'errors'),
    ],
)
def test_status_rejects(override, field):
    status = schema.RouterStatus(
        router_id=ROUTER_ID,
        state=ApplianceState.UNKNOWN,
        desired_generation=1,
        applied_generation=0,
    )
    with pytest.raises(ValidationError) as exc:
        schema.RouterStatus.from_dict(dict(status.to_dict(), **override))
    assert exc.value.field == field


def test_status_accepts_an_offset_timestamp():
    status = schema.RouterStatus.from_dict(
        {
            'schema_version': SCHEMA_VERSION,
            'router_id': ROUTER_ID,
            'state': 'converging',
            'desired_generation': 2,
            'applied_generation': 1,
            'last_seen': '2026-11-18T11:03:11+01:00',
        }
    )
    assert status.state is ApplianceState.CONVERGING
