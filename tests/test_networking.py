# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry

"""Management network, security group and port lifecycle.

The core plugin and the tag plugin are stubs here: what this module
contributes on top of them is the idempotent lookup-then-create dance and
the tagging convention the orphan sweep relies on, not Neutron's own CRUD,
which is tested upstream.

Needs neutron-lib and is skipped where the server dependencies are absent.
"""

from unittest import mock

import pytest


lib_exc = pytest.importorskip('neutron_lib.exceptions')
tagging = pytest.importorskip('neutron.extensions.tagging')
networking = pytest.importorskip('neutron_appliance.server.networking')

ROUTER_ID = '7f0e2c3a-1111-4000-8000-000000000001'
OTHER_ROUTER_ID = '7f0e2c3a-1111-4000-8000-000000000002'
PROJECT_ID = '7f0e2c3a-2222-4000-8000-000000000001'
NETWORK_ID = '7f0e2c3a-3333-4000-8000-000000000001'
SUBNET_ID = '7f0e2c3a-3333-4000-8000-000000000002'
SG_ID = '7f0e2c3a-3333-4000-8000-000000000003'
PORT_ID = '7f0e2c3a-4444-4000-8000-000000000001'
OTHER_PORT_ID = '7f0e2c3a-4444-4000-8000-000000000002'


@pytest.fixture
def project(conf):
    conf.set_override('service_project_id', PROJECT_ID, group='appliance')
    yield conf
    conf.clear_override('service_project_id', group='appliance')


@pytest.fixture
def core_plugin():
    plugin = mock.MagicMock()
    plugin.get_networks.return_value = []
    plugin.get_subnets.return_value = []
    plugin.get_security_groups.return_value = []
    plugin.get_ports.return_value = []
    plugin.create_security_group.return_value = {'id': SG_ID}
    return plugin


@pytest.fixture
def tag_plugin():
    return mock.MagicMock()


@pytest.fixture
def plugins(project, core_plugin, tag_plugin):
    """Everything `directory.get_plugin` can be asked for here, stubbed."""

    def get_plugin(alias=None):
        if alias is None:
            return core_plugin
        if alias == tagging.TAG_PLUGIN_TYPE:
            return tag_plugin
        return None

    with (
        mock.patch.object(
            networking.directory, 'get_plugin', side_effect=get_plugin
        ),
        mock.patch.object(
            networking.plugin_utils,
            'create_network',
            return_value={'id': NETWORK_ID},
        ) as create_network,
        mock.patch.object(
            networking.plugin_utils,
            'create_subnet',
            return_value={'id': SUBNET_ID},
        ) as create_subnet,
        mock.patch.object(
            networking.plugin_utils,
            'create_port',
            return_value={'id': PORT_ID},
        ) as create_port,
    ):
        core_plugin.create_network = create_network
        core_plugin.create_subnet = create_subnet
        core_plugin.create_port = create_port
        yield core_plugin


#
# Management network
#


def test_the_network_is_created_when_missing(plugins, context):
    network_id, sg_id = networking.ensure_management_network(context)
    assert network_id == NETWORK_ID
    assert sg_id == SG_ID
    plugins.create_network.assert_called_once()
    plugins.create_subnet.assert_called_once()
    plugins.create_security_group.assert_called_once()


def test_an_existing_network_is_found_not_recreated(plugins, context):
    plugins.get_networks.return_value = [{'id': NETWORK_ID}]
    plugins.get_subnets.return_value = [{'id': SUBNET_ID}]
    plugins.get_security_groups.return_value = [{'id': SG_ID}]
    network_id, sg_id = networking.ensure_management_network(context)
    assert (network_id, sg_id) == (NETWORK_ID, SG_ID)
    plugins.create_network.assert_not_called()
    plugins.create_subnet.assert_not_called()
    plugins.create_security_group.assert_not_called()


def test_the_network_is_created_in_the_service_project(plugins, context):
    networking.ensure_management_network(context)
    net = plugins.create_network.call_args.args[2]['network']
    assert net['project_id'] == PROJECT_ID
    sg = plugins.create_security_group.call_args.args[1]['security_group']
    assert sg['project_id'] == PROJECT_ID


#
# Management ports
#


def test_a_management_port_is_created_and_tagged(plugins, tag_plugin, context):
    port_id = networking.ensure_management_port(context, ROUTER_ID)
    assert port_id == PORT_ID
    plugins.create_port.assert_called_once()
    port = plugins.create_port.call_args.args[2]['port']
    assert port['network_id'] == NETWORK_ID
    assert port['project_id'] == PROJECT_ID
    tag_plugin.create_tags.assert_called_once_with(
        context, networking.TAGGED_RESOURCE, PORT_ID, {'tags': [ROUTER_ID]}
    )


def test_an_existing_management_port_is_found_not_recreated(plugins, context):
    plugins.get_ports.return_value = [
        {'id': PORT_ID, 'tags': [ROUTER_ID]},
        {'id': OTHER_PORT_ID, 'tags': [OTHER_ROUTER_ID]},
    ]
    port_id = networking.ensure_management_port(context, ROUTER_ID)
    assert port_id == PORT_ID
    plugins.create_port.assert_not_called()


def test_a_missing_tag_plugin_is_a_hard_error(plugins, context):
    with mock.patch.object(
        networking.directory,
        'get_plugin',
        side_effect=lambda alias=None: plugins if alias is None else None,
    ):
        with pytest.raises(RuntimeError):
            networking.ensure_management_port(context, ROUTER_ID)


def test_deleting_a_port_is_quiet_once_it_is_already_gone(plugins, context):
    plugins.delete_port.side_effect = lib_exc.PortNotFound(port_id=PORT_ID)
    networking.delete_management_port(context, PORT_ID)
    plugins.delete_port.assert_called_once_with(context, PORT_ID)


def test_deleting_router_ports_only_touches_its_own(plugins, context):
    plugins.get_ports.return_value = [
        {'id': PORT_ID, 'tags': [ROUTER_ID]},
        {'id': OTHER_PORT_ID, 'tags': [OTHER_ROUTER_ID]},
    ]
    networking.delete_router_ports(context, ROUTER_ID)
    plugins.delete_port.assert_called_once_with(context, PORT_ID)


def test_port_router_ids_is_the_union_of_every_tag(plugins, context):
    plugins.get_ports.return_value = [
        {'id': PORT_ID, 'tags': [ROUTER_ID]},
        {'id': OTHER_PORT_ID, 'tags': [OTHER_ROUTER_ID]},
        {'id': 'untagged', 'tags': []},
    ]
    assert networking.port_router_ids(context) == {
        ROUTER_ID,
        OTHER_ROUTER_ID,
    }
