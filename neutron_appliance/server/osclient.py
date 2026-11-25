# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry

"""The openstacksdk connection shared by every reconciler module.

Authenticated as the service user configured in the `[nova]` group -- the
same group and the same pattern neutron's own Nova notifier uses, so an
operator who already runs notifications has nothing new to configure.

Both Nova and Neutron calls go through this connection, over HTTP, rather
than Neutron ones calling the core plugin in-process: ML2/OVN only finishes
its own post-fork setup in the API and RPC worker processes, so a plugin
call that touches it from `neutron-periodic-workers` hangs forever instead
of returning.
"""

from __future__ import annotations

import functools

from keystoneauth1 import loading as ks_loading
from openstack import connection
from oslo_config import cfg


@functools.cache
def get_connection():
    auth = ks_loading.load_auth_from_conf_options(cfg.CONF, 'nova')
    session = ks_loading.load_session_from_conf_options(
        cfg.CONF, 'nova', auth=auth
    )
    return connection.Connection(session=session)
