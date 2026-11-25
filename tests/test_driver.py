# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry

"""Driver callbacks, exercised without a database.

These need neutron, which the appliance image must never carry and which
pins a dependency tree this repository does not. They are skipped where it
is absent; install it with `pip install -e .[server]` to run them.
"""

from unittest import mock

import pytest


# Every callback is private: neutron calls them through the registry, and
# there is no public surface to test instead.
# pylint: disable=protected-access


events = pytest.importorskip('neutron_lib.callbacks.events')
registry = pytest.importorskip('neutron_lib.callbacks.registry')
resources = pytest.importorskip('neutron_lib.callbacks.resources')
callback_exc = pytest.importorskip('neutron_lib.callbacks.exceptions')
driver = pytest.importorskip('neutron_appliance.server.driver')
base = pytest.importorskip('neutron.services.l3_router.service_providers.base')

ROUTER_ID = '7f0e2c3a-1111-4000-8000-000000000001'
FLAVOR_ID = '7f0e2c3a-2222-4000-8000-000000000002'
PROFILE_ID = '7f0e2c3a-3333-4000-8000-000000000003'
METAINFO = '{"nova_flavor": "router.small", "image": "appliance-1.2"}'
OTHER_DRIVER = 'some.other.provider.Driver'


@pytest.fixture
def appliance_db():
    with mock.patch.object(driver, 'appliance_db') as db:
        yield db


@pytest.fixture
def no_worker():
    """These tests exercise callbacks, not the reconciler it registers.

    A real `ApplianceWorker` needs oslo.config and oslo.service set up in
    ways no test here asks for, so construction is replaced wholesale
    instead of partially faking that environment.
    """
    with mock.patch.object(driver, 'ApplianceWorker') as worker_cls:
        yield worker_cls


@pytest.fixture
def provider(appliance_db, no_worker):
    """A driver whose flavor, plugin and provider association are fake."""
    l3plugin = mock.MagicMock()
    # the registry is global and our subscriptions are per instance: start
    # from an empty one so that a published event reaches this driver and
    # nothing else
    registry.clear()
    instance = driver.ApplianceL3Driver(l3plugin)
    # every router is ours unless a test says otherwise
    l3plugin.l3_driver_controller.get_provider_for_router.return_value = (
        instance
    )
    flavor_plugin = mock.MagicMock()
    flavor_plugin.get_flavor.return_value = {
        'id': FLAVOR_ID,
        'service_profiles': [PROFILE_ID],
    }
    flavor_plugin.get_flavor_next_provider.return_value = [
        {'driver': instance._driver_path, 'provider': 'appliance'}
    ]
    flavor_plugin.get_service_profile.return_value = {'metainfo': METAINFO}
    instance._flavor_plugin_ref = flavor_plugin
    yield instance
    registry.clear()


def payload(**kwargs):
    kwargs.setdefault('resource_id', ROUTER_ID)
    return events.DBEventPayload(mock.Mock(), **kwargs)


def test_capabilities_are_unsupported():
    # wrong here and a router can only ever be deleted, never fixed
    assert driver.ApplianceL3Driver.ha_support is base.UNSUPPORTED
    assert driver.ApplianceL3Driver.distributed_support is base.UNSUPPORTED


def test_the_reconciler_is_registered(provider, no_worker):
    provider.l3plugin.add_worker.assert_called_once_with(
        no_worker.return_value
    )


def test_plain_router_is_accepted(provider, appliance_db):
    router = {'name': 'r1', 'admin_state_up': True}
    provider._check_router_create(None, None, None, payload(states=(router,)))
    provider._flavor_plugin.get_flavor_next_provider.assert_not_called()


def test_another_providers_flavor_is_ignored(provider):
    provider._flavor_plugin.get_flavor_next_provider.return_value = [
        {'driver': OTHER_DRIVER, 'provider': 'other'}
    ]
    router = {'flavor_id': FLAVOR_ID, 'external_gateway_info': {'x': 'y'}}
    provider._check_router_create(None, None, None, payload(states=(router,)))


def test_provider_spelled_with_a_colon_is_still_ours(provider):
    module, _, klass = provider._driver_path.rpartition('.')
    provider._flavor_plugin.get_flavor_next_provider.return_value = [
        {'driver': f'{module}:{klass}', 'provider': 'appliance'}
    ]
    assert provider._is_appliance_flavor(mock.Mock(), {'flavor_id': FLAVOR_ID})


@pytest.mark.parametrize(
    'attribute,value',
    [
        ('external_gateway_info', {'network_id': 'net'}),
        ('availability_zone_hints', ['az1']),
        ('routes', [{'destination': '0.0.0.0/0', 'nexthop': '10.0.0.1'}]),
        ('enable_ndp_proxy', True),
    ],
)
def test_unsupported_attributes_are_refused(provider, attribute, value):
    router = {'flavor_id': FLAVOR_ID, attribute: value}
    with pytest.raises(driver.ApplianceNotSupported) as raised:
        provider._check_router_create(
            None, None, None, payload(states=(router,))
        )
    # the message must name the router and what was refused
    assert ROUTER_ID in str(raised.value)
    assert driver.UNSUPPORTED_ATTRIBUTES[attribute] in str(raised.value)


def test_unset_attributes_are_not_refused(provider):
    router = {
        'flavor_id': FLAVOR_ID,
        'external_gateway_info': None,
        'availability_zone_hints': [],
        'enable_ndp_proxy': False,
    }
    provider._check_router_create(None, None, None, payload(states=(router,)))


def test_association_records_the_router(provider, appliance_db):
    router_db = {'id': ROUTER_ID, 'flavor_id': FLAVOR_ID}
    provider._add_router_association(
        None,
        None,
        None,
        payload(states=(router_db,), metadata={'new_driver': provider}),
    )
    appliance_db.create_router.assert_called_once_with(
        mock.ANY,
        ROUTER_ID,
        flavor_id=FLAVOR_ID,
        # the profile is editable at runtime, so it is snapshotted now
        flavor_snapshot=METAINFO,
    )


def test_another_drivers_association_is_ignored(provider, appliance_db):
    provider._add_router_association(
        None,
        None,
        None,
        payload(
            states=({'id': ROUTER_ID},), metadata={'new_driver': object()}
        ),
    )
    # a payload without any driver at all must not raise either
    provider._add_router_association(
        None, None, None, payload(states=({'id': ROUTER_ID},))
    )
    appliance_db.create_router.assert_not_called()


def test_disassociation_forgets_the_router(provider, appliance_db):
    provider._delete_router_association(
        None, None, None, payload(metadata={'old_driver': provider})
    )
    appliance_db.delete_router.assert_called_once_with(mock.ANY, ROUTER_ID)


def test_another_drivers_disassociation_is_ignored(provider, appliance_db):
    provider._delete_router_association(
        None, None, None, payload(metadata={'old_driver': object()})
    )
    appliance_db.delete_router.assert_not_called()


def test_admin_state_change_bumps_the_generation(provider, appliance_db):
    provider._update_router_precommit(
        None,
        None,
        None,
        payload(
            states=({'admin_state_up': True, 'name': 'r1'},),
            request_body={'admin_state_up': False},
        ),
    )
    appliance_db.bump_generation.assert_called_once_with(mock.ANY, ROUTER_ID)


def test_cosmetic_change_does_not_bump_the_generation(provider, appliance_db):
    provider._update_router_precommit(
        None,
        None,
        None,
        payload(
            states=({'admin_state_up': True, 'name': 'r1'},),
            request_body={'name': 'r2', 'admin_state_up': True},
        ),
    )
    appliance_db.bump_generation.assert_not_called()


def test_update_of_another_providers_router_is_ignored(provider, appliance_db):
    provider.l3plugin.l3_driver_controller.get_provider_for_router.return_value = (  # noqa: E501
        object()
    )
    provider._update_router_precommit(
        None,
        None,
        None,
        payload(
            states=({'admin_state_up': True},),
            request_body={'routes': [{'destination': '0.0.0.0/0'}]},
        ),
    )
    appliance_db.bump_generation.assert_not_called()


def test_extra_routes_are_refused(provider, appliance_db):
    with pytest.raises(driver.ApplianceNotSupported):
        provider._update_router_precommit(
            None,
            None,
            None,
            payload(
                states=({'admin_state_up': True},),
                request_body={
                    'routes': [
                        {'destination': '10.0.0.0/8', 'nexthop': '10.0.0.1'}
                    ]
                },
            ),
        )
    appliance_db.bump_generation.assert_not_called()


def test_external_gateway_is_refused(provider):
    with pytest.raises(driver.ApplianceNotSupported):
        provider._check_router_gateway(None, None, None, payload())


def test_router_interface_is_refused(provider):
    with pytest.raises(driver.ApplianceNotSupported):
        provider._check_router_interface(None, None, None, payload())


def test_another_providers_gateway_is_left_alone(provider):
    provider.l3plugin.l3_driver_controller.get_provider_for_router.return_value = (  # noqa: E501
        object()
    )
    provider._check_router_gateway(None, None, None, payload())
    provider._check_router_interface(None, None, None, payload())


# The callbacks above are called directly, which proves what they do but not
# that neutron will ever reach them. These go through the registry, so a
# resource or event named wrong fails here instead of in a deployment.


@pytest.mark.parametrize(
    'resource,event,kwargs',
    [
        (
            resources.ROUTER,
            events.BEFORE_CREATE,
            {
                'states': (
                    {
                        'flavor_id': FLAVOR_ID,
                        'external_gateway_info': {'network_id': 'net'},
                    },
                )
            },
        ),
        (resources.ROUTER_GATEWAY, events.BEFORE_CREATE, {}),
        (resources.ROUTER_INTERFACE, events.BEFORE_CREATE, {}),
        (
            resources.ROUTER,
            events.PRECOMMIT_UPDATE,
            {
                'states': ({'admin_state_up': True},),
                'request_body': {'availability_zone_hints': ['az1']},
            },
        ),
    ],
)
def test_the_registry_reaches_the_refusals(provider, resource, event, kwargs):
    with pytest.raises(callback_exc.CallbackFailure) as raised:
        registry.publish(resource, event, mock.Mock(), payload(**kwargs))
    # the API layer unpacks this and answers 400, not 500
    (inner,) = raised.value.inner_exceptions
    assert isinstance(inner, driver.ApplianceNotSupported)


def test_the_registry_reaches_the_association(provider, appliance_db):
    registry.publish(
        resources.ROUTER_CONTROLLER,
        events.PRECOMMIT_ADD_ASSOCIATION,
        mock.Mock(),
        payload(
            states=({'id': ROUTER_ID, 'flavor_id': FLAVOR_ID},),
            metadata={'old_driver': None, 'new_driver': provider},
        ),
    )
    appliance_db.create_router.assert_called_once()
    registry.publish(
        resources.ROUTER_CONTROLLER,
        events.PRECOMMIT_DELETE_ASSOCIATIONS,
        mock.Mock(),
        payload(metadata={'old_driver': provider, 'new_driver': None}),
    )
    appliance_db.delete_router.assert_called_once_with(mock.ANY, ROUTER_ID)
