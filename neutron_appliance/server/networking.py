# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry

"""Management network, security group and appliance ports.

Everything here is idempotent by construction: the reconciler calls it on
every tick, so "already exists" is the common case, not an error. Lookups
go by name and project rather than by a remembered id, because nothing
here is allowed to depend on state this module does not itself own -- that
state lives in Neutron, and Neutron is the source of truth.

Talks to Neutron over HTTP, the same way `compute.py` talks to Nova,
rather than calling the core plugin in-process: see `osclient.py` for why.

Ports are the only place a router id is recorded outside the appliance
database: tagged with `router_id` the moment they are created, so that the
orphan sweep can find a management port whose router disappeared even if
neutron-server crashed before anything else learned about it.
"""

from __future__ import annotations

from oslo_log import log as logging

from neutron_appliance.server import config
from neutron_appliance.server.osclient import get_connection


LOG = logging.getLogger(__name__)

_SG_DESCRIPTION = (
    'Egress-only security group shared by every neutron-appliance '
    'management port. Created and owned by neutron-appliance.'
)


def _ensure_network(conn, project_id, name):
    network = next(
        conn.network.networks(project_id=project_id, name=name), None
    )
    if network is not None:
        return network
    network = conn.network.create_network(
        project_id=project_id,
        name=name,
        admin_state_up=True,
        shared=False,
    )
    LOG.info('management network %s created', network.id)
    return network


def _ensure_subnet(conn, project_id, network_id, cidr):
    subnet = next(conn.network.subnets(network_id=network_id), None)
    if subnet is not None:
        return subnet
    subnet = conn.network.create_subnet(
        project_id=project_id,
        network_id=network_id,
        ip_version=4,
        cidr=cidr,
        enable_dhcp=True,
    )
    LOG.info('management subnet %s created', subnet.id)
    return subnet


def _ensure_security_group(conn, project_id, name):
    security_group = next(
        conn.network.security_groups(project_id=project_id, name=name), None
    )
    if security_group is not None:
        return security_group
    # a security group is created with the egress-allow-all template rules
    # and none of the ingress ones (see neutron's securitygroupdefaultrules),
    # which is exactly "egress-only"
    security_group = conn.network.create_security_group(
        project_id=project_id,
        name=name,
        description=_SG_DESCRIPTION,
    )
    LOG.info('management security group %s created', security_group.id)
    return security_group


def ensure_management_network():
    """Idempotently create the shared management network and its subnet
    and security group. Returns (network_id, security_group_id).

    Safe to call on every tick: each lookup is by name within the service
    project, and nothing is recreated once it exists. External connectivity
    for this network is an operator-provisioned prerequisite, not something
    this reconciler builds.
    """
    conn = get_connection()
    conf = config.CONF.appliance
    project_id = conf.service_project_id
    network = _ensure_network(conn, project_id, conf.mgmt_network_name)
    _ensure_subnet(conn, project_id, network.id, conf.mgmt_subnet_cidr)
    security_group = _ensure_security_group(
        conn, project_id, conf.mgmt_network_name
    )
    return network.id, security_group.id


def ensure_management_port(router_id):
    """Idempotently create the management port of one router.

    Found by tag rather than by a remembered id, so that a worker
    restarted between creating the port and recording it on the router
    finds the same port again instead of leaking a second one.
    """
    conn = get_connection()
    network_id, security_group_id = ensure_management_network()
    port = next(
        conn.network.ports(network_id=network_id, tags=router_id), None
    )
    if port is not None:
        return port.id
    conf = config.CONF.appliance
    port = conn.network.create_port(
        project_id=conf.service_project_id,
        network_id=network_id,
        name=f'appliance-{router_id}',
        admin_state_up=True,
        security_group_ids=[security_group_id],
    )
    conn.network.add_tag(port, router_id)
    LOG.info('router %s: management port %s created', router_id, port.id)
    return port.id


def delete_management_port(port_id):
    """Delete one management port. Quiet if it is already gone."""
    get_connection().network.delete_port(port_id, ignore_missing=True)
    LOG.info('management port %s deleted', port_id)


def delete_router_ports(router_id):
    """Delete every management port tagged with `router_id`.

    Plural on purpose: nothing here stops two ticks from racing and each
    creating a port for a router neither saw one for yet. Deleting
    everything tagged, rather than trusting there is only one, is what
    makes the orphan sweep clean that up instead of leaking it forever.
    """
    conn = get_connection()
    network_id, _ = ensure_management_network()
    for port in conn.network.ports(network_id=network_id, tags=router_id):
        delete_management_port(port.id)


def port_router_ids():
    """Router ids tagged onto a management port, right now.

    Not what the database remembers: a crash between creating a port and
    recording it on the router must not make the next tick create a
    second one, and a port something else deleted must not be trusted
    either. See `reconcile.converge`.
    """
    conn = get_connection()
    network_id, _ = ensure_management_network()
    return {
        tag
        for port in conn.network.ports(network_id=network_id)
        for tag in (port.tags or [])
    }
