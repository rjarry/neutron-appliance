# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry

"""The L3ServiceProvider subclass loaded by neutron-server.

On the API path this computes desired state and commits it, and does nothing
else. It never calls Nova and never calls the appliance: callbacks run in the
API worker, and PRECOMMIT_* runs inside the open database transaction.

Booting, attaching and deleting instances is the reconciler's job (M4). All
this driver leaves behind is a row in `appliance_routers` and a generation
number that says how far behind the appliance is.
"""

from neutron.services.l3_router.service_providers import base
from neutron_lib import constants as lib_const
from neutron_lib import exceptions as lib_exc
from neutron_lib.api import validators
from neutron_lib.callbacks import events, priority_group, registry, resources
from neutron_lib.plugins import constants as plugin_constants
from neutron_lib.plugins import directory
from oslo_log import log as logging

from neutron_appliance.server.db import api as appliance_db
from neutron_appliance.server.worker import ApplianceWorker


LOG = logging.getLogger(__name__)

# Router attributes that stage pre-0 cannot honour, and the wording of the
# refusal. Accepting one and ignoring it is exactly the failure this table
# exists to prevent: a tenant gets a 200 and finds out days later that
# nothing was ever configured.
UNSUPPORTED_ATTRIBUTES = {
    'external_gateway_info': 'an external gateway',
    'availability_zone_hints': 'availability zone hints',
    'routes': 'extra routes',
    'enable_ndp_proxy': 'NDP proxy',
    'enable_default_route_bfd': 'BFD on the default route',
    'enable_default_route_ecmp': 'ECMP default routes',
}

# Router attributes that end up in the configuration document. Changing one
# of these is what makes a router diverge from its appliance; changing a
# name or a description does not.
DOCUMENT_ATTRIBUTES = ('admin_state_up',)


class ApplianceNotSupported(lib_exc.BadRequest):
    """Asked for something this stage of the appliance cannot do."""

    message = 'Appliance router %(router_id)s: %(what)s is not supported yet'


@registry.has_registry_receivers
class ApplianceL3Driver(base.L3ServiceProvider):
    """Routers backed by a service VM, dispatched to by flavor.

    The capability flags must be right the first time: `ha` and
    `distributed` are effectively create-only for flavored routers, so a
    wrong declaration produces a router that can only be deleted, never
    fixed. The driver controller rejects incompatible requests on our
    behalf once these are declared.
    """

    ha_support = base.UNSUPPORTED
    distributed_support = base.UNSUPPORTED

    def __init__(self, l3plugin):
        super().__init__(l3plugin)
        # How this driver is spelled in the service_provider configuration
        # line. Flavors name a driver by dotted path, not by provider name.
        self._driver_path = f'{__name__}.{type(self).__name__}'
        self._flavor_plugin_ref = None
        self.l3plugin.add_worker(ApplianceWorker())

    @property
    def _flavor_plugin(self):
        if self._flavor_plugin_ref is None:
            self._flavor_plugin_ref = directory.get_plugin(
                plugin_constants.FLAVORS
            )
        return self._flavor_plugin_ref

    def _is_appliance_flavor(self, context, router):
        """True if this router's flavor points at us.

        Used before the router exists, where there is no provider
        association to ask. Afterwards, owns_router() is the cheaper and
        more reliable question.
        """
        flavor_id = router.get('flavor_id')
        if not flavor_id or flavor_id is lib_const.ATTR_NOT_SPECIFIED:
            return False
        provider = self._flavor_plugin.get_flavor_next_provider(
            context, flavor_id
        )[0]
        # a provider line may spell the driver 'module:Class' or
        # 'module.Class'; both name the same class
        return str(provider['driver']).replace(':', '.') == self._driver_path

    def _flavor_snapshot(self, context, flavor_id):
        """The service profile metainfo, as it reads right now.

        Snapshotted into the router's row because the profile is editable
        at runtime: an operator typo must not retroactively rebuild every
        appliance that was created from it.
        """
        if not flavor_id:
            # a router can be associated with a provider by its ha and
            # distributed attributes alone, with no flavor in sight
            return None
        flavor = self._flavor_plugin.get_flavor(context, flavor_id)
        # only the first binding is ever used by neutron
        profiles = flavor.get('service_profiles') or []
        if not profiles:
            return None
        profile = self._flavor_plugin.get_service_profile(context, profiles[0])
        return profile.get('metainfo')

    @staticmethod
    def _reject_unsupported(router_id, body):
        for attribute, what in UNSUPPORTED_ATTRIBUTES.items():
            value = body.get(attribute)
            if validators.is_attr_set(value) and value:
                raise ApplianceNotSupported(router_id=router_id, what=what)

    @registry.receives(
        resources.ROUTER,
        [events.BEFORE_CREATE],
        priority_group.PRIORITY_ROUTER_DRIVER,
    )
    def _check_router_create(self, resource, event, trigger, payload):
        router = payload.latest_state
        if not self._is_appliance_flavor(payload.context, router):
            return
        # 'ha' and 'distributed' are not checked here: the driver
        # controller already refuses them for us, from the capability
        # flags above.
        self._reject_unsupported(payload.resource_id, router)

    @registry.receives(
        resources.ROUTER_CONTROLLER, [events.PRECOMMIT_ADD_ASSOCIATION]
    )
    def _add_router_association(self, resource, event, trigger, payload):
        """A router became ours. Record it, in its own transaction."""
        if payload.metadata.get('new_driver') is not self:
            return
        context = payload.context
        router_id = payload.resource_id
        flavor_id = payload.states[0].get('flavor_id')
        appliance_db.create_router(
            context,
            router_id,
            flavor_id=flavor_id,
            flavor_snapshot=self._flavor_snapshot(context, flavor_id),
        )

    @registry.receives(
        resources.ROUTER_CONTROLLER, [events.PRECOMMIT_DELETE_ASSOCIATIONS]
    )
    def _delete_router_association(self, resource, event, trigger, payload):
        """A router stopped being ours, or is being deleted."""
        if payload.metadata.get('old_driver') is not self:
            return
        appliance_db.delete_router(payload.context, payload.resource_id)

    @registry.receives(
        resources.ROUTER,
        [events.PRECOMMIT_UPDATE],
        priority_group.PRIORITY_ROUTER_DRIVER,
    )
    def _update_router_precommit(self, resource, event, trigger, payload):
        context = payload.context
        router_id = payload.resource_id
        if not self.owns_router(context, router_id):
            return
        body = payload.request_body or {}
        self._reject_unsupported(router_id, body)
        original = payload.states[0]
        changed = [
            attribute
            for attribute in DOCUMENT_ATTRIBUTES
            if attribute in body and body[attribute] != original[attribute]
        ]
        if changed:
            LOG.info(
                'router %s: %s changed, new configuration needed',
                router_id,
                ', '.join(changed),
            )
            appliance_db.bump_generation(context, router_id)

    @registry.receives(resources.ROUTER_GATEWAY, [events.BEFORE_CREATE])
    def _check_router_gateway(self, resource, event, trigger, payload):
        router_id = payload.resource_id
        if not self.owns_router(payload.context, router_id):
            return
        raise ApplianceNotSupported(
            router_id=router_id, what='an external gateway'
        )

    @registry.receives(resources.ROUTER_INTERFACE, [events.BEFORE_CREATE])
    def _check_router_interface(self, resource, event, trigger, payload):
        router_id = payload.resource_id
        if not self.owns_router(payload.context, router_id):
            return
        raise ApplianceNotSupported(
            router_id=router_id, what='a router interface'
        )

    # Floating IPs need no hook of their own: associating one requires the
    # router to have a gateway port on the floating network, and the check
    # above means an appliance router never has one. FLOATING_IP
    # BEFORE_CREATE carries no router id anyway, so there would be nothing
    # to match against.
