# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry

"""Management network, security group and port lifecycle, against a
stubbed openstacksdk connection.

What this module contributes on top of Neutron's own API is the
idempotent lookup-then-create dance and the tagging convention the orphan
sweep relies on, not Neutron's own CRUD, which is tested upstream.

Needs openstacksdk, and is skipped where the server dependencies are
absent.
"""

from unittest import mock

import pytest


networking = pytest.importorskip('neutron_appliance.server.networking')

ROUTER_ID = '7f0e2c3a-1111-4000-8000-000000000001'
OTHER_ROUTER_ID = '7f0e2c3a-1111-4000-8000-000000000002'
PROJECT_ID = '7f0e2c3a-2222-4000-8000-000000000001'
NETWORK_ID = '7f0e2c3a-3333-4000-8000-000000000001'
SUBNET_ID = '7f0e2c3a-3333-4000-8000-000000000002'
SG_ID = '7f0e2c3a-3333-4000-8000-000000000003'
PORT_ID = '7f0e2c3a-4444-4000-8000-000000000001'
OTHER_PORT_ID = '7f0e2c3a-4444-4000-8000-000000000002'


@pytest.fixture(autouse=True)
def clear_connection_cache():
    """The connection getter is memoised; tests must not share one."""
    networking.get_connection.cache_clear()
    yield
    networking.get_connection.cache_clear()


@pytest.fixture
def project(conf):
    conf.set_override('service_project_id', PROJECT_ID, group='appliance')
    yield conf
    conf.clear_override('service_project_id', group='appliance')


@pytest.fixture
def connection(project):
    conn = mock.MagicMock()
    conn.network.networks.return_value = iter([])
    conn.network.subnets.return_value = iter([])
    conn.network.security_groups.return_value = iter([])
    conn.network.ports.return_value = iter([])
    conn.network.create_network.return_value = mock.Mock(id=NETWORK_ID)
    conn.network.create_subnet.return_value = mock.Mock(id=SUBNET_ID)
    conn.network.create_security_group.return_value = mock.Mock(id=SG_ID)
    conn.network.create_port.return_value = mock.Mock(id=PORT_ID)
    with mock.patch.object(networking, 'get_connection', return_value=conn):
        yield conn


#
# Management network
#


def test_the_network_is_created_when_missing(connection):
    network_id, sg_id = networking.ensure_management_network()
    assert network_id == NETWORK_ID
    assert sg_id == SG_ID
    connection.network.create_network.assert_called_once()
    connection.network.create_subnet.assert_called_once()
    connection.network.create_security_group.assert_called_once()


def test_an_existing_network_is_found_not_recreated(connection):
    connection.network.networks.return_value = iter([mock.Mock(id=NETWORK_ID)])
    connection.network.subnets.return_value = iter([mock.Mock(id=SUBNET_ID)])
    connection.network.security_groups.return_value = iter(
        [mock.Mock(id=SG_ID)]
    )
    network_id, sg_id = networking.ensure_management_network()
    assert (network_id, sg_id) == (NETWORK_ID, SG_ID)
    connection.network.create_network.assert_not_called()
    connection.network.create_subnet.assert_not_called()
    connection.network.create_security_group.assert_not_called()


def test_the_network_is_created_in_the_service_project(connection):
    networking.ensure_management_network()
    attrs = connection.network.create_network.call_args.kwargs
    assert attrs['project_id'] == PROJECT_ID
    attrs = connection.network.create_security_group.call_args.kwargs
    assert attrs['project_id'] == PROJECT_ID


#
# Management ports
#


def test_a_management_port_is_created_and_tagged(connection):
    port_id = networking.ensure_management_port(ROUTER_ID)
    assert port_id == PORT_ID
    connection.network.create_port.assert_called_once()
    attrs = connection.network.create_port.call_args.kwargs
    assert attrs['network_id'] == NETWORK_ID
    assert attrs['project_id'] == PROJECT_ID
    connection.network.add_tag.assert_called_once_with(
        connection.network.create_port.return_value, ROUTER_ID
    )


def test_an_existing_management_port_is_found_not_recreated(connection):
    connection.network.ports.return_value = iter([mock.Mock(id=PORT_ID)])
    port_id = networking.ensure_management_port(ROUTER_ID)
    assert port_id == PORT_ID
    connection.network.create_port.assert_not_called()


def test_deleting_a_port_is_quiet_once_it_is_already_gone(connection):
    networking.delete_management_port(PORT_ID)
    connection.network.delete_port.assert_called_once_with(
        PORT_ID, ignore_missing=True
    )


def test_deleting_router_ports_only_touches_its_own(connection):
    connection.network.ports.return_value = iter(
        [mock.Mock(id=PORT_ID), mock.Mock(id=OTHER_PORT_ID)]
    )
    networking.delete_router_ports(ROUTER_ID)
    assert connection.network.ports.call_args.kwargs['tags'] == ROUTER_ID
    assert connection.network.delete_port.call_count == 2


def test_port_router_ids_is_the_union_of_every_tag(connection):
    connection.network.ports.return_value = [
        mock.Mock(tags=[ROUTER_ID]),
        mock.Mock(tags=[OTHER_ROUTER_ID]),
        mock.Mock(tags=[]),
    ]
    assert networking.port_router_ids() == {ROUTER_ID, OTHER_ROUTER_ID}
