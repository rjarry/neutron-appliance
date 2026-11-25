# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry

"""Convergence logic, kept free of any scheduling framework so that it can be
tested without one.

Pure and total: no I/O, no neutron, no Nova. Given what the database says
should exist and what the worker found actually exists, it says what to do
about it and nothing more. Any router whose instance went missing -- because
it never got built, or because something outside this code destroyed it --
is indistinguishable from a brand new one: both get a `Boot`. That is the
whole of "level-triggered": there is no memory of *why* a router needs an
instance, only whether it has one right now.
"""

from __future__ import annotations

from collections.abc import Iterable
import dataclasses
from typing import Union


@dataclasses.dataclass(frozen=True)
class Boot:
    """A router with no live instance. Build it a management port and boot."""

    router_id: str
    flavor_snapshot: str | None


@dataclasses.dataclass(frozen=True)
class Reap:
    """A router id with resources in Nova or Neutron but no `appliance_routers`
    row.

    Not necessarily a bug: this is also the state of a router one tick after
    its row was deleted. `router_id` is all the caller gets; it looks up
    whatever instance or port actually carries the tag or the name, because
    this module never saw them and does not remember the ones it reaped
    last time either.
    """

    router_id: str


Action = Union[Boot, Reap]


def converge(
    routers: Iterable,
    instance_router_ids: Iterable[str],
    port_router_ids: Iterable[str],
) -> list[Action]:
    """Diff routers that should exist against resources that do.

    `routers` are the desired state, straight from `db.api.get_routers()`.
    `instance_router_ids` and `port_router_ids` are the router ids tagged
    onto, respectively, every Nova instance and every management port the
    worker found when it looked -- not what the database remembers booting,
    which is the whole point of a level-triggered reconciler: a crash
    between booting an instance and recording it must not repeat the boot,
    and state recorded for an instance something else destroyed must not
    be trusted either.

    `Boot` actions always come before `Reap` actions, so a worker that dies
    partway through applying the result leaves extra resources behind
    rather than missing ones: the next tick's orphan sweep cleans up the
    former, but there is no fixing a router stuck without an instance for
    one more interval than necessary.
    """
    desired = {router.router_id: router for router in routers}
    instance_router_ids = set(instance_router_ids)
    port_router_ids = set(port_router_ids)

    boots = [
        Boot(router_id, router.flavor_snapshot)
        for router_id, router in desired.items()
        if router_id not in instance_router_ids
    ]
    orphans = (instance_router_ids | port_router_ids) - desired.keys()
    reaps = [Reap(router_id) for router_id in sorted(orphans)]
    return boots + reaps
