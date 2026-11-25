# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry

"""oslo.config options, [appliance] group.

Read from neutron.conf, so an operator edits one file. Options are added
as the milestone that needs them lands rather than all at once, because an
option that nothing reads is a promise the code does not keep.
"""

from oslo_config import cfg

from neutron_appliance.common import protocol


GROUP = 'appliance'

OPTS = [
    cfg.IntOpt(
        'poll_interval_s',
        default=protocol.DEFAULT_POLL_INTERVAL_S,
        min=protocol.MIN_POLL_INTERVAL_S,
        max=protocol.MAX_POLL_INTERVAL_S,
        help='How often an appliance should call home, in seconds. Sent '
        'in every sync answer, so lowering it takes effect one poll '
        'later without touching a single appliance.',
    ),
    cfg.IntOpt(
        'unknown_after_intervals',
        default=3,
        min=1,
        help='How many missed polls before a router is reported as '
        '"unknown". This is not an error state: an appliance that has '
        'stopped calling home is most likely still forwarding, it just '
        'cannot be asked. Keep it above 1 so that one lost packet does '
        'not raise an alarm.',
    ),
    cfg.IntOpt(
        'credential_grace_period_s',
        default=86400,
        min=0,
        help='How long the previous per-router token keeps working after '
        'a rotation. It covers an agent that crashed between receiving a '
        'new token and persisting it: without a grace period that crash '
        'locks the appliance out of the control plane for good, and the '
        'bug only shows up on the first unlucky reboot.',
    ),
    cfg.StrOpt(
        'mgmt_network_name',
        default='neutron-appliance-mgmt',
        help='Name of the management network every appliance boots onto. '
        'Created, together with its subnet and security group, in '
        'service_project_id the first time the reconciler needs it; left '
        'alone afterwards.',
    ),
    cfg.StrOpt(
        'mgmt_subnet_cidr',
        default='192.168.240.0/22',
        help='Subnet carved out of the management network. Appliances '
        'never route tenant traffic over it, so one block shared by '
        'every router on this neutron-server is enough.',
    ),
    cfg.StrOpt(
        'service_project_id',
        help='Project the reconciler creates the management network and '
        'boots every appliance instance in. Must be the project the '
        '[nova] credentials below are scoped to. No default: booting '
        'into the wrong project is not a mistake this code should be '
        'able to make silently.',
    ),
    cfg.StrOpt(
        'nova_flavor',
        help='Nova flavor booted for a router whose service profile '
        'metainfo (see docs/02-hooking-neutron.md) does not name its '
        'own. No default: a router with no flavor anywhere should fail '
        'loudly rather than boot on a guess.',
    ),
    cfg.StrOpt(
        'api_endpoint_override',
        help='Public endpoint written to the config drive instead of '
        "the one read from Keystone's catalogue. Needed when the "
        'catalogue is not reachable from the management network, for '
        'example because it is NAT-only.',
    ),
    cfg.StrOpt(
        'debug_ssh_key',
        help='Public key imported into Nova and passed to every '
        'appliance at boot, for interactive debugging. Unset means no '
        'key is injected and nothing can log in. Whether the image '
        "honours it is outside this driver's control.",
    ),
    cfg.IntOpt(
        'reconcile_interval_s',
        default=30,
        min=1,
        help='How often the reconciler compares routers that should '
        'exist against instances and ports that do.',
    ),
    cfg.IntOpt(
        'max_retry_backoff_s',
        default=300,
        min=1,
        help='Ceiling of the exponential backoff applied to a router '
        'whose last reconcile step failed, so a Nova outage does not '
        'turn into a tight retry loop against it.',
    ),
]


def register_opts(conf=cfg.CONF):
    """Make the [appliance] group readable. Safe to call twice."""
    conf.register_opts(OPTS, group=GROUP)


def list_opts():
    """Entry point for oslo-config-generator."""
    return [(GROUP, OPTS)]


# Registered on import so that importing this module is enough to read the
# options, whichever of neutron-server's many entry paths got here first.
register_opts()

CONF = cfg.CONF
