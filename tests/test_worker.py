# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry

"""The reconciler tick: converge, then apply, with backoff on failure.

`reconcile.converge` itself is covered in test_reconcile.py; what matters
here is that `_tick` wires it to Nova and Neutron correctly, that a failed
step backs off instead of retrying every tick, and that the steps
themselves are idempotent enough for a crash-and-restart to finish the job
the previous run started.

Needs neutron, and is skipped where the server dependencies are absent.
"""

import dataclasses
from unittest import mock

import pytest


# _tick, _breaker and _breakers are private: the reconciler calls them
# itself on a timer, and there is no public surface to test instead.
# pylint: disable=protected-access


worker = pytest.importorskip('neutron_appliance.server.worker')
reconcile = pytest.importorskip('neutron_appliance.server.reconcile')

ROUTER_ID = '7f0e2c3a-1111-4000-8000-000000000001'
PORT_ID = '7f0e2c3a-2222-4000-8000-000000000001'
INSTANCE_ID = '7f0e2c3a-3333-4000-8000-000000000001'
TOKEN = 'a-router-token'


@dataclasses.dataclass
class FakeRow:
    router_id: str
    flavor_snapshot: str = None


@pytest.fixture
def appliance(conf):
    """`reconcile_interval_s`/`max_retry_backoff_s` at small, fast values."""
    conf.set_override('reconcile_interval_s', 1, group='appliance')
    conf.set_override('max_retry_backoff_s', 60, group='appliance')
    yield conf
    conf.clear_override('reconcile_interval_s', group='appliance')
    conf.clear_override('max_retry_backoff_s', group='appliance')


@pytest.fixture
def collaborators(appliance):  # pylint: disable=unused-argument
    """Every module `_tick` talks to, replaced with a bare mock."""
    with (
        mock.patch.object(worker, 'appliance_db') as appliance_db,
        mock.patch.object(worker, 'compute') as compute,
        mock.patch.object(worker, 'networking') as networking,
        mock.patch.object(worker, 'render') as render,
        mock.patch.object(worker.lib_context, 'get_admin_context'),
    ):
        appliance_db.get_routers.return_value = []
        compute.instance_router_ids.return_value = set()
        networking.port_router_ids.return_value = set()
        networking.ensure_management_port.return_value = PORT_ID
        appliance_db.issue_credential.return_value = TOKEN
        compute.public_endpoint.return_value = 'https://neutron.example.com'
        compute.credential.return_value = mock.Mock()
        compute.ca_cert.return_value = None
        compute.ensure_keypair.return_value = None
        compute.boot.return_value = INSTANCE_ID
        yield mock.Mock(
            appliance_db=appliance_db,
            compute=compute,
            networking=networking,
            render=render,
        )


@pytest.fixture
def reconciler(collaborators):  # pylint: disable=unused-argument
    return worker.ApplianceWorker()


#
# the basic wiring
#


def test_nothing_to_do_touches_neither_nova_nor_neutron(
    reconciler, collaborators
):
    reconciler._tick()
    collaborators.compute.boot.assert_not_called()
    collaborators.compute.delete.assert_not_called()
    collaborators.networking.delete_router_ports.assert_not_called()


def test_a_new_router_is_booted(reconciler, collaborators):
    collaborators.appliance_db.get_routers.return_value = [FakeRow(ROUTER_ID)]
    reconciler._tick()
    collaborators.networking.ensure_management_port.assert_called_once_with(
        mock.ANY, ROUTER_ID
    )
    collaborators.appliance_db.issue_credential.assert_called_once_with(
        mock.ANY, ROUTER_ID
    )
    collaborators.compute.boot.assert_called_once()
    collaborators.appliance_db.set_instance.assert_called_once_with(
        mock.ANY, ROUTER_ID, INSTANCE_ID
    )


def test_a_router_deleted_mid_boot_is_not_built(reconciler, collaborators):
    # issue_credential returns None when the row disappeared between
    # converge() reading it and _boot() reaching it
    collaborators.appliance_db.issue_credential.return_value = None
    collaborators.appliance_db.get_routers.return_value = [FakeRow(ROUTER_ID)]
    reconciler._tick()
    collaborators.compute.boot.assert_not_called()


def test_an_orphan_instance_and_port_are_both_reaped(
    reconciler, collaborators
):
    collaborators.compute.instance_router_ids.return_value = {ROUTER_ID}
    collaborators.networking.port_router_ids.return_value = {ROUTER_ID}
    instance = mock.Mock(id=INSTANCE_ID)
    collaborators.compute.find_instance.return_value = instance
    reconciler._tick()
    collaborators.compute.delete.assert_called_once_with(INSTANCE_ID)
    collaborators.networking.delete_router_ports.assert_called_once_with(
        mock.ANY, ROUTER_ID
    )


def test_an_already_gone_instance_is_not_deleted_again(
    reconciler, collaborators
):
    collaborators.compute.instance_router_ids.return_value = {ROUTER_ID}
    collaborators.compute.find_instance.return_value = None
    reconciler._tick()
    collaborators.compute.delete.assert_not_called()
    collaborators.networking.delete_router_ports.assert_called_once()


#
# the circuit breaker
#


def test_a_failed_boot_is_not_retried_within_the_same_tick(
    reconciler, collaborators
):
    collaborators.appliance_db.get_routers.return_value = [FakeRow(ROUTER_ID)]
    collaborators.compute.boot.side_effect = RuntimeError('nova is down')
    reconciler._tick()
    assert collaborators.compute.boot.call_count == 1


def test_a_failed_boot_is_not_retried_before_its_backoff_elapses(
    reconciler, collaborators
):
    collaborators.appliance_db.get_routers.return_value = [FakeRow(ROUTER_ID)]
    collaborators.compute.boot.side_effect = RuntimeError('nova is down')
    reconciler._tick()
    reconciler._tick()
    assert collaborators.compute.boot.call_count == 1


def test_a_successful_retry_clears_the_breaker(reconciler, collaborators):
    collaborators.appliance_db.get_routers.return_value = [FakeRow(ROUTER_ID)]
    breaker = reconciler._breaker(ROUTER_ID)
    breaker.fail(now=0.0, max_backoff_s=60)
    breaker.retry_at = 0.0  # force it ready regardless of the jittered delay
    reconciler._tick()
    assert not breaker.failures


def test_a_breaker_for_a_router_that_is_no_longer_mentioned_is_dropped(
    reconciler, collaborators
):
    collaborators.appliance_db.get_routers.return_value = [FakeRow(ROUTER_ID)]
    collaborators.compute.boot.side_effect = RuntimeError('nova is down')
    reconciler._tick()
    assert ROUTER_ID in reconciler._breakers

    collaborators.appliance_db.get_routers.return_value = []
    reconciler._tick()
    assert ROUTER_ID not in reconciler._breakers


#
# crash mid-create, then restart
#


def test_a_crash_between_the_port_and_the_instance_is_resumed(
    reconciler, collaborators
):
    """The scenario the orphan sweep and idempotent steps exist for.

    The first tick creates the management port, then Nova is unreachable
    before `boot()` returns: nothing records an instance id. A brand new
    worker -- the restart -- sees the same router with still no instance,
    finds the same port again instead of leaking a second one, and
    finishes the boot.
    """
    collaborators.appliance_db.get_routers.return_value = [FakeRow(ROUTER_ID)]
    collaborators.compute.boot.side_effect = RuntimeError('nova is down')

    reconciler._tick()

    collaborators.networking.ensure_management_port.assert_called_once()
    collaborators.appliance_db.set_instance.assert_not_called()

    # "restart": a fresh worker, a fresh breaker, same world otherwise
    collaborators.compute.boot.side_effect = None
    restarted = worker.ApplianceWorker()
    restarted._tick()

    assert collaborators.networking.ensure_management_port.call_count == 2
    collaborators.appliance_db.set_instance.assert_called_once_with(
        mock.ANY, ROUTER_ID, INSTANCE_ID
    )
