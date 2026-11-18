# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry

"""Periodic reconciler, run as a neutron PeriodicWorker.

Level-triggered: converge "routers that should exist" against "instances that
do exist". It never replays an event stream, so a crash mid-operation is
indistinguishable from a slow operation.
"""

# TODO: ApplianceWorker(neutron.worker.PeriodicWorker)
