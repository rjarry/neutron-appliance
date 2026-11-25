# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry

"""The Nova wrapper, against a stubbed openstacksdk connection.

Nothing here makes a network call: `_get_connection` is replaced wholesale,
so what is actually exercised is the shape of the request this module
builds and the lookups it does before building it, not openstacksdk or
Nova themselves.

Needs neutron-lib and openstacksdk, and is skipped where the server
dependencies are absent.
"""

import base64
import json
from unittest import mock

import pytest


compute = pytest.importorskip('neutron_appliance.server.compute')
schema = pytest.importorskip('neutron_appliance.common.schema')

ROUTER_ID = '7f0e2c3a-1111-4000-8000-000000000001'
OTHER_ROUTER_ID = '7f0e2c3a-1111-4000-8000-000000000002'
PORT_ID = '7f0e2c3a-2222-4000-8000-000000000001'
INSTANCE_ID = '7f0e2c3a-3333-4000-8000-000000000001'


@pytest.fixture(autouse=True)
def clear_connection_cache():
    """The connection getter is memoised; tests must not share one."""
    compute._get_connection.cache_clear()  # pylint: disable=protected-access
    yield
    compute._get_connection.cache_clear()  # pylint: disable=protected-access


@pytest.fixture
def nova_conf(conf):
    """`[nova]` registered and filled in with made-up password auth."""
    common_config = pytest.importorskip('neutron.conf.common')
    ks_loading = pytest.importorskip('keystoneauth1.loading')
    ks_loading.register_auth_conf_options(
        conf, common_config.NOVA_CONF_SECTION
    )
    ks_loading.register_session_conf_options(
        conf, common_config.NOVA_CONF_SECTION
    )
    common_config.register_nova_opts()
    conf.set_override('auth_type', 'password', group='nova')
    # the password plugin's own options (auth_url, username, ...) are only
    # registered once something asks for them
    conf.register_opts(
        ks_loading.get_auth_plugin_conf_options('password'), group='nova'
    )
    overrides = {
        'auth_url': 'https://keystone.example.com/v3',
        'username': 'neutron',
        'password': 'secret',
        'project_name': 'service',
        'user_domain_name': 'Default',
        'project_domain_name': 'Default',
    }
    for name, value in overrides.items():
        conf.set_override(name, value, group='nova')
    yield conf
    for name in overrides:
        conf.clear_override(name, group='nova')
    conf.clear_override('auth_type', group='nova')


@pytest.fixture
def connection():
    conn = mock.MagicMock()
    with mock.patch.object(compute, '_get_connection', return_value=conn):
        yield conn


#
# credential() and ca_cert()
#


def test_the_credential_comes_from_the_nova_auth_config(nova_conf):
    credential = compute.credential('a-router-token')
    assert credential == schema.Credential(
        auth_url='https://keystone.example.com/v3',
        username='neutron',
        password='secret',
        project_name='service',
        router_token='a-router-token',
        user_domain_name='Default',
        project_domain_name='Default',
    )


def test_no_cafile_configured_means_no_ca_cert(nova_conf):
    assert compute.ca_cert() is None


def test_the_cafile_is_read_whole(nova_conf, tmp_path):
    cert_path = tmp_path / 'ca.pem'
    cert_path.write_text('-----BEGIN CERTIFICATE-----\n...\n')
    nova_conf.set_override('cafile', str(cert_path), group='nova')
    assert compute.ca_cert() == cert_path.read_text()


#
# public_endpoint()
#


def test_the_override_wins_over_the_catalogue(conf, connection):
    conf.set_override(
        'api_endpoint_override',
        'https://override.example.com',
        group='appliance',
    )
    try:
        assert compute.public_endpoint() == 'https://override.example.com'
        connection.session.get_endpoint.assert_not_called()
    finally:
        conf.clear_override('api_endpoint_override', group='appliance')


def test_the_endpoint_comes_from_the_catalogue(conf, connection):
    connection.session.get_endpoint.return_value = (
        'https://neutron.example.com'
    )
    assert compute.public_endpoint() == 'https://neutron.example.com'
    connection.session.get_endpoint.assert_called_once_with(
        service_type='network', interface='public'
    )


def test_a_missing_catalogue_entry_is_a_hard_error(conf, connection):
    connection.session.get_endpoint.return_value = None
    with pytest.raises(RuntimeError):
        compute.public_endpoint()


#
# ensure_keypair()
#


def test_no_debug_key_means_no_keypair_is_touched(connection):
    assert compute.ensure_keypair(None) is None
    connection.compute.find_keypair.assert_not_called()
    connection.compute.create_keypair.assert_not_called()


def test_an_existing_keypair_is_not_recreated(connection):
    connection.compute.find_keypair.return_value = mock.Mock()
    name = compute.ensure_keypair('ssh-ed25519 AAAA...')
    assert name == compute.DEBUG_KEYPAIR_NAME
    connection.compute.create_keypair.assert_not_called()


def test_a_missing_keypair_is_imported(connection):
    connection.compute.find_keypair.return_value = None
    name = compute.ensure_keypair('ssh-ed25519 AAAA...')
    assert name == compute.DEBUG_KEYPAIR_NAME
    connection.compute.create_keypair.assert_called_once_with(
        name=compute.DEBUG_KEYPAIR_NAME, public_key='ssh-ed25519 AAAA...'
    )


#
# boot()
#


def bootstrap():
    return schema.Bootstrap(
        router_id=ROUTER_ID,
        endpoint='https://neutron.example.com',
        sync_path='/v2.0/appliance/sync',
        credential=schema.Credential(
            auth_url='https://keystone.example.com/v3',
            username='neutron',
            password='secret',
            project_name='service',
            router_token='a-router-token',
        ),
    )


def test_a_router_with_no_image_in_its_snapshot_is_refused(connection):
    with pytest.raises(ValueError):
        compute.boot(ROUTER_ID, PORT_ID, '{}', bootstrap())
    connection.compute.create_server.assert_not_called()


def test_a_router_with_no_flavor_anywhere_is_refused(conf, connection):
    with pytest.raises(ValueError):
        compute.boot(
            ROUTER_ID, PORT_ID, json.dumps({'image': 'cirros'}), bootstrap()
        )
    connection.compute.create_server.assert_not_called()


def test_the_configured_flavor_is_the_fallback(conf, connection):
    conf.set_override('nova_flavor', 'm1.small', group='appliance')
    try:
        connection.compute.find_flavor.return_value = mock.Mock(id='flavor-1')
        connection.image.find_image.return_value = mock.Mock(id='image-1')
        compute.boot(
            ROUTER_ID, PORT_ID, json.dumps({'image': 'cirros'}), bootstrap()
        )
        connection.compute.find_flavor.assert_called_once_with(
            'm1.small', ignore_missing=False
        )
    finally:
        conf.clear_override('nova_flavor', group='appliance')


def test_the_snapshot_flavor_wins_over_the_default(conf, connection):
    conf.set_override('nova_flavor', 'm1.small', group='appliance')
    try:
        connection.compute.find_flavor.return_value = mock.Mock(id='flavor-1')
        connection.image.find_image.return_value = mock.Mock(id='image-1')
        compute.boot(
            ROUTER_ID,
            PORT_ID,
            json.dumps({'nova_flavor': 'm1.large', 'image': 'cirros'}),
            bootstrap(),
        )
        connection.compute.find_flavor.assert_called_once_with(
            'm1.large', ignore_missing=False
        )
    finally:
        conf.clear_override('nova_flavor', group='appliance')


def test_a_booted_instance_has_one_nic_and_a_config_drive(connection):
    connection.compute.find_flavor.return_value = mock.Mock(id='flavor-1')
    connection.image.find_image.return_value = mock.Mock(id='image-1')
    connection.compute.create_server.return_value = mock.Mock(id=INSTANCE_ID)
    snapshot = json.dumps({'nova_flavor': 'm1.small', 'image': 'cirros'})
    instance_id = compute.boot(ROUTER_ID, PORT_ID, snapshot, bootstrap())
    assert instance_id == INSTANCE_ID
    attrs = connection.compute.create_server.call_args.kwargs
    assert attrs['flavor_id'] == 'flavor-1'
    assert attrs['image_id'] == 'image-1'
    assert attrs['networks'] == [{'port': PORT_ID}]
    assert attrs['config_drive'] is True
    assert attrs['tags'] == [ROUTER_ID]
    assert 'key_name' not in attrs
    payload = json.loads(base64.b64decode(attrs['user_data']).decode('utf-8'))
    assert payload['router_id'] == ROUTER_ID


def test_a_key_name_is_passed_through_when_given(connection):
    connection.compute.find_flavor.return_value = mock.Mock(id='flavor-1')
    connection.image.find_image.return_value = mock.Mock(id='image-1')
    connection.compute.create_server.return_value = mock.Mock(id=INSTANCE_ID)
    snapshot = json.dumps({'nova_flavor': 'm1.small', 'image': 'cirros'})
    compute.boot(
        ROUTER_ID,
        PORT_ID,
        snapshot,
        bootstrap(),
        key_name='neutron-appliance-debug',
    )
    attrs = connection.compute.create_server.call_args.kwargs
    assert attrs['key_name'] == 'neutron-appliance-debug'


#
# delete(), find_instance(), instance_router_ids()
#


def test_deleting_an_instance_is_idempotent(connection):
    compute.delete(INSTANCE_ID)
    connection.compute.delete_server.assert_called_once_with(
        INSTANCE_ID, ignore_missing=True
    )


def test_find_instance_returns_the_first_tagged_match(connection):
    server = mock.Mock()
    connection.compute.servers.return_value = iter([server])
    assert compute.find_instance(ROUTER_ID) is server
    connection.compute.servers.assert_called_once_with(tags=ROUTER_ID)


def test_find_instance_is_none_when_nothing_matches(connection):
    connection.compute.servers.return_value = iter([])
    assert compute.find_instance(ROUTER_ID) is None


def test_instance_router_ids_is_the_union_of_every_tag(connection):
    connection.compute.servers.return_value = [
        mock.Mock(tags=[ROUTER_ID]),
        mock.Mock(tags=[OTHER_ROUTER_ID]),
        mock.Mock(tags=[]),
    ]
    assert compute.instance_router_ids() == {ROUTER_ID, OTHER_ROUTER_ID}
