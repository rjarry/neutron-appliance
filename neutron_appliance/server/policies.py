# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry

"""Default policies for the appliance endpoints.

Loaded by neutron through the `neutron.policies` entry point, so that the
rules below are registered defaults rather than lines an operator has to
remember to paste into policy.yaml. An override still goes in policy.yaml;
see etc/neutron/policy.yaml.sample.
"""

from neutron_lib.policy import rules as lib_rules
from oslo_policy import policy


# The dedicated role carried by the shared appliance service user, and by
# nothing else. Create it once per deployment:
#
#   openstack role create appliance-agent
#   openstack role add --project service --user neutron-appliance \
#       appliance-agent
#
# It is the whole of that user's authority. Every other neutron rule asks
# for admin, member or reader, so a user holding only this role can reach
# the sync endpoint and nothing else in the API.
APPLIANCE_AGENT = 'role:appliance-agent'

STATUS_PATH = '/appliance/routers/{router_id}'
SYNC_PATH = '/appliance/routers/{router_id}/sync'

rules = [
    policy.DocumentedRuleDefault(
        name='get_appliance_router',
        check_str=lib_rules.ADMIN,
        description=(
            'Read the authoritative state of an appliance router. '
            'Administrators only: the agent never calls this, and giving '
            'the appliance-agent role read access would let one '
            'compromised appliance enumerate every other one. Tenants see '
            'the same information through the appliance: router tags, '
            'which are a convenience mirror and not the source of truth.'
        ),
        operations=[{'method': 'GET', 'path': STATUS_PATH}],
        scope_types=['project'],
    ),
    policy.DocumentedRuleDefault(
        name='sync_appliance_router',
        check_str=f'({lib_rules.ADMIN}) or ({APPLIANCE_AGENT})',
        description=(
            'Report appliance state and receive the configuration '
            'document. This rule decides who may knock; the per-router '
            'token in X-Appliance-Token decides whose configuration they '
            'get. Both are needed: the service user secret sits on every '
            'appliance config drive, so restricting this rule to the '
            'appliance-agent role is what bounds the damage when one '
            'appliance is compromised.'
        ),
        operations=[{'method': 'POST', 'path': SYNC_PATH}],
        scope_types=['project'],
    ),
]


def list_rules():
    return rules
