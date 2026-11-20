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

# TODO: M4 adds mgmt_network_name, mgmt_subnet_cidr, the service project,
# TODO: the nova flavor, api_endpoint_override and debug_ssh_key.
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
