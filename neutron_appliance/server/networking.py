# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry

"""Management network, security group and appliance ports.

Everything here is idempotent by construction: the reconciler calls it on
every tick, so "already exists" is the common case, not an error. Lookups
go by name and project rather than by a remembered id, because nothing
here is allowed to depend on state this module does not itself own -- that
state lives in Neutron, and Neutron is the source of truth.

Ports are the only place a router id is recorded outside the appliance
database: tagged with `router_id` the moment they are created, so that the
orphan sweep can find a management port whose router disappeared even if
neutron-server crashed before anything else learned about it.
"""

from __future__ import annotations

from neutron.extensions import tagging
from neutron_lib import constants as lib_const
from neutron_lib import exceptions as lib_exc
from neutron_lib.plugins import directory
from neutron_lib.plugins import utils as plugin_utils
from oslo_log import log as logging

from neutron_appliance.server import config


LOG = logging.getLogger(__name__)

# Collection name the tag plugin expects, not the attribute name on a port.
TAGGED_RESOURCE = 'ports'

_SG_DESCRIPTION = (
    'Egress-only security group shared by every neutron-appliance '
    'management port. Created and owned by neutron-appliance.'
)


def _core_plugin():
    return directory.get_plugin()


def _tag_plugin():
    plugin = directory.get_plugin(tagging.TAG_PLUGIN_TYPE)
    if plugin is None:
        raise RuntimeError(
            "the 'tag' service plugin must be enabled for "
            'neutron-appliance to track its management ports'
        )
    return plugin


def _find_network(core_plugin, context, project_id, name):
    networks = core_plugin.get_networks(
        context, filters={'project_id': [project_id], 'name': [name]}
    )
    if networks:
        return networks[0]
    return None


def _ensure_network(core_plugin, context, project_id, name):
    network = _find_network(core_plugin, context, project_id, name)
    if network is not None:
        return network
    network = plugin_utils.create_network(
        core_plugin,
        context,
        {
            'network': {
                'project_id': project_id,
                'name': name,
                'admin_state_up': True,
                'shared': False,
            }
        },
    )
    LOG.info('management network %s created', network['id'])
    return network


def _ensure_subnet(core_plugin, context, project_id, network_id, cidr):
    subnets = core_plugin.get_subnets(
        context, filters={'network_id': [network_id]}
    )
    if subnets:
        return subnets[0]
    subnet = plugin_utils.create_subnet(
        core_plugin,
        context,
        {
            'subnet': {
                'project_id': project_id,
                'network_id': network_id,
                'ip_version': lib_const.IP_VERSION_4,
                'cidr': cidr,
                'enable_dhcp': True,
            }
        },
    )
    LOG.info('management subnet %s created', subnet['id'])
    return subnet


def _find_security_group(core_plugin, context, project_id, name):
    security_groups = core_plugin.get_security_groups(
        context, filters={'project_id': [project_id], 'name': [name]}
    )
    if security_groups:
        return security_groups[0]
    return None


def _ensure_security_group(core_plugin, context, project_id, name):
    security_group = _find_security_group(
        core_plugin, context, project_id, name
    )
    if security_group is not None:
        return security_group
    # a non-default security group is created with the egress-allow-all
    # template rules and none of the ingress ones (see neutron's
    # securitygroupdefaultrules), which is exactly "egress-only"
    security_group = core_plugin.create_security_group(
        context,
        {
            'security_group': {
                'project_id': project_id,
                'name': name,
                'description': _SG_DESCRIPTION,
            }
        },
        default_sg=False,
    )
    LOG.info('management security group %s created', security_group['id'])
    return security_group


def ensure_management_network(context):
    """Idempotently create the shared management network and its subnet
    and security group. Returns (network_id, security_group_id).

    Safe to call on every tick: each lookup is by name within the service
    project, and nothing is recreated once it exists. External connectivity
    for this network is an operator-provisioned prerequisite, not something
    this reconciler builds.
    """
    core_plugin = _core_plugin()
    conf = config.CONF.appliance
    project_id = conf.service_project_id
    network = _ensure_network(
        core_plugin, context, project_id, conf.mgmt_network_name
    )
    _ensure_subnet(
        core_plugin,
        context,
        project_id,
        network['id'],
        conf.mgmt_subnet_cidr,
    )
    security_group = _ensure_security_group(
        core_plugin, context, project_id, conf.mgmt_network_name
    )
    return network['id'], security_group['id']


def _find_management_port(core_plugin, context, network_id, router_id):
    ports = core_plugin.get_ports(
        context, filters={'network_id': [network_id]}
    )
    for port in ports:
        if router_id in (port.get('tags') or []):
            return port
    return None


def ensure_management_port(context, router_id):
    """Idempotently create the management port of one router.

    Found by tag rather than by a remembered id, so that a worker
    restarted between creating the port and recording it on the router
    finds the same port again instead of leaking a second one.
    """
    core_plugin = _core_plugin()
    network_id, security_group_id = ensure_management_network(context)
    port = _find_management_port(core_plugin, context, network_id, router_id)
    if port is not None:
        return port['id']
    conf = config.CONF.appliance
    port = plugin_utils.create_port(
        core_plugin,
        context,
        {
            'port': {
                'project_id': conf.service_project_id,
                'network_id': network_id,
                'name': f'appliance-{router_id}',
                'admin_state_up': True,
                'security_groups': [security_group_id],
                'fixed_ips': lib_const.ATTR_NOT_SPECIFIED,
                'mac_address': lib_const.ATTR_NOT_SPECIFIED,
            }
        },
    )
    _tag_plugin().create_tags(
        context, TAGGED_RESOURCE, port['id'], {'tags': [router_id]}
    )
    LOG.info('router %s: management port %s created', router_id, port['id'])
    return port['id']


def delete_management_port(context, port_id):
    """Delete one management port. Quiet if it is already gone."""
    try:
        _core_plugin().delete_port(context, port_id)
    except lib_exc.PortNotFound:
        return
    LOG.info('management port %s deleted', port_id)


def delete_router_ports(context, router_id):
    """Delete every management port tagged with `router_id`.

    Plural on purpose: nothing here stops two ticks from racing and each
    creating a port for a router neither saw one for yet. Deleting
    everything tagged, rather than trusting there is only one, is what
    makes the orphan sweep clean that up instead of leaking it forever.
    """
    core_plugin = _core_plugin()
    network_id, _ = ensure_management_network(context)
    ports = core_plugin.get_ports(
        context, filters={'network_id': [network_id]}
    )
    for port in ports:
        if router_id in (port.get('tags') or []):
            delete_management_port(context, port['id'])


def port_router_ids(context):
    """Router ids tagged onto a management port, right now.

    Not what the database remembers: a crash between creating a port and
    recording it on the router must not make the next tick create a
    second one, and a port something else deleted must not be trusted
    either. See `reconcile.converge`.
    """
    core_plugin = _core_plugin()
    network_id, _ = ensure_management_network(context)
    ports = core_plugin.get_ports(
        context, filters={'network_id': [network_id]}
    )
    return {tag for port in ports for tag in (port.get('tags') or [])}
