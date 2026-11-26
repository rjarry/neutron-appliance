# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry

"""Data-plane backend interface.

One implementation today. The abstraction exists so that a DPDK backend stays
possible without reshaping the agent.

`apply()` is given the complete desired state and must be idempotent: no
deltas, no memory of the previous call. `observe()` is for diagnosis, what
the next sync reports back to the control plane.
"""

from __future__ import annotations

import abc

from neutron_appliance.common import schema


class BackendError(Exception):
    """A document could not be applied, wholly or partly.

    `errors` carries human-readable strings for the next sync call. The
    server stores them and surfaces them to the operator, so they must name
    the interface or route that failed rather than a code.
    """

    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__('; '.join(self.errors) or 'apply failed')


class Backend(abc.ABC):
    """What every data plane must implement."""

    @abc.abstractmethod
    def apply(self, document: schema.ConfigDocument) -> None:
        """Make the world match `document`.

        Raises BackendError when it cannot finish, with the reasons in
        `errors`.
        """

    @abc.abstractmethod
    def observe(self) -> schema.Observed:
        """Report what the agent can see right now."""
