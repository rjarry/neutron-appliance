# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry

"""Periodic reconciler, run as a neutron PeriodicWorker.

Level-triggered: converge "routers that should exist" against "instances that
do exist". It never replays an event stream, so a crash mid-operation is
indistinguishable from a slow operation, and restarting after either one
just means the next tick sees the same thing and finishes the job.
"""

from __future__ import annotations

import secrets
import time

from neutron.worker import PeriodicWorker
from neutron_lib import context as lib_context
from oslo_log import log as logging

from neutron_appliance.server import (
    compute,
    config,
    networking,
    reconcile,
    render,
)
from neutron_appliance.server.db import api as appliance_db


LOG = logging.getLogger(__name__)

_RANDOM = secrets.SystemRandom()


class _Breaker:
    """One router's failure count and the backoff it has earned.

    Kept in memory only: a neutron-server restart resets every router's
    backoff, which is the right failure mode here -- a crash is not
    evidence that whatever was failing still is.
    """

    def __init__(self):
        self.failures = 0
        self.retry_at = 0.0

    def ready(self, now):
        return now >= self.retry_at

    def succeed(self):
        self.failures = 0
        self.retry_at = 0.0

    def fail(self, now, max_backoff_s):
        self.failures += 1
        backoff = min(2**self.failures, max_backoff_s)
        self.retry_at = now + _RANDOM.uniform(0, backoff)


class ApplianceWorker(PeriodicWorker):
    """Reconciles `appliance_routers` rows against Nova and Neutron."""

    def __init__(self):
        interval = config.CONF.appliance.reconcile_interval_s
        initial_delay = _RANDOM.randint(0, interval)
        super().__init__(
            self._tick,
            interval,
            initial_delay,
            desc='neutron-appliance reconciler',
        )
        self._breakers = {}

    def _breaker(self, router_id):
        breaker = self._breakers.get(router_id)
        if breaker is None:
            breaker = self._breakers[router_id] = _Breaker()
        return breaker

    def _tick(self):
        context = lib_context.get_admin_context()
        try:
            actions = reconcile.converge(
                appliance_db.get_routers(context),
                compute.instance_router_ids(),
                networking.port_router_ids(context),
            )
        except Exception:
            LOG.exception('reconciler: could not compute the actions to apply')
            return

        now = time.monotonic()
        max_backoff_s = config.CONF.appliance.max_retry_backoff_s
        # discard breakers for routers converge() did not even mention,
        # so a long-lived server does not grow this dict forever
        wanted = {action.router_id for action in actions}
        for router_id in list(self._breakers):
            if router_id not in wanted:
                del self._breakers[router_id]

        for action in actions:
            breaker = self._breaker(action.router_id)
            if not breaker.ready(now):
                continue
            try:
                if isinstance(action, reconcile.Boot):
                    self._boot(context, action)
                else:
                    self._reap(context, action)
            except Exception:
                LOG.exception(
                    'router %s: reconcile step failed', action.router_id
                )
                breaker.fail(now, max_backoff_s)
            else:
                breaker.succeed()

    def _boot(self, context, action):
        """Build the one management port and instance a router needs.

        Each step is itself idempotent, so a retry after a partial
        failure here continues rather than starts over: a port already
        created is found again, and a credential already issued for an
        unbuilt instance is simply replaced by the next one.
        """
        router_id = action.router_id
        port_id = networking.ensure_management_port(context, router_id)
        token = appliance_db.issue_credential(context, router_id)
        if token is None:
            # the row was deleted between converge() and here; the next
            # tick's orphan sweep reaps the port just created
            return
        bootstrap = render.bootstrap(
            router_id,
            compute.public_endpoint(),
            compute.credential(token),
            compute.ca_cert(),
            config.CONF.appliance.poll_interval_s,
        )
        key_name = compute.ensure_keypair(config.CONF.appliance.debug_ssh_key)
        instance_id = compute.boot(
            router_id,
            port_id,
            action.flavor_snapshot,
            bootstrap,
            key_name,
        )
        appliance_db.set_instance(context, router_id, instance_id)

    @staticmethod
    def _reap(context, action):
        """Destroy whatever `router_id` left behind in Nova and Neutron.

        The instance first, the ports after: a crash in between leaves a
        bare port with no instance pointing at it, which is exactly the
        state the next tick's `Reap` for the same `router_id` cleans up,
        rather than a port some other router could ever be matched to.
        """
        router_id = action.router_id
        instance = compute.find_instance(router_id)
        if instance is not None:
            compute.delete(instance.id)
        networking.delete_router_ports(context, router_id)
