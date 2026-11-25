# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry

"""Convergence logic, which needs nothing but what it is given.

No neutron, no Nova, no database: this is the one server module pure
enough to run on any interpreter, so it carries no importorskip.
"""

import dataclasses

from neutron_appliance.server import reconcile


ROUTER_A = '7f0e2c3a-1111-4000-8000-000000000001'
ROUTER_B = '7f0e2c3a-1111-4000-8000-000000000002'


@dataclasses.dataclass
class FakeRow:
    router_id: str
    flavor_snapshot: str = None


def test_a_router_with_no_instance_is_booted():
    actions = reconcile.converge([FakeRow(ROUTER_A)], [], [])
    assert actions == [reconcile.Boot(ROUTER_A, None)]


def test_a_router_with_an_instance_is_left_alone():
    actions = reconcile.converge([FakeRow(ROUTER_A)], [ROUTER_A], [ROUTER_A])
    assert actions == []


def test_an_instance_without_a_port_still_counts_as_built():
    # the port is only ever used to find the instance again; an instance
    # that exists is already proof the router does not need a Boot
    actions = reconcile.converge([FakeRow(ROUTER_A)], [ROUTER_A], [])
    assert actions == []


def test_an_instance_with_no_router_row_is_reaped():
    actions = reconcile.converge([], [ROUTER_A], [])
    assert actions == [reconcile.Reap(ROUTER_A)]


def test_a_port_with_no_router_row_is_reaped():
    actions = reconcile.converge([], [], [ROUTER_A])
    assert actions == [reconcile.Reap(ROUTER_A)]


def test_an_orphan_is_reaped_only_once():
    actions = reconcile.converge([], [ROUTER_A], [ROUTER_A])
    assert actions == [reconcile.Reap(ROUTER_A)]


def test_boots_always_come_before_reaps():
    actions = reconcile.converge([FakeRow(ROUTER_A)], [ROUTER_B], [ROUTER_B])
    assert actions == [
        reconcile.Boot(ROUTER_A, None),
        reconcile.Reap(ROUTER_B),
    ]


def test_the_flavor_snapshot_travels_with_the_boot():
    actions = reconcile.converge(
        [FakeRow(ROUTER_A, flavor_snapshot='{"image": "x"}')], [], []
    )
    assert actions == [reconcile.Boot(ROUTER_A, '{"image": "x"}')]


def test_nothing_to_do_is_an_empty_list():
    assert reconcile.converge([], [], []) == []
