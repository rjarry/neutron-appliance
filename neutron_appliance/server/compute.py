# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry

"""Nova client wrapper: boot, delete, in the service project.

Everything talks through openstacksdk, authenticated as the service user
configured in the `[nova]` group -- the same group and the same pattern
neutron's own Nova notifier uses, so an operator who already runs
notifications has nothing new to configure.

`boot()` returns as soon as Nova accepts the request: it does not wait for
the instance to go ACTIVE. Recording the id it returns is what makes the
reconciler's next tick see an instance already exists, so a worker that
dies between the two never boots a second one.
"""

from __future__ import annotations

import base64
import functools
import json

from keystoneauth1 import loading as ks_loading
from openstack import connection
from oslo_config import cfg
from oslo_log import log as logging

from neutron_appliance.common import schema
from neutron_appliance.server import config


LOG = logging.getLogger(__name__)

DEBUG_KEYPAIR_NAME = 'neutron-appliance-debug'


@functools.cache
def _get_connection():
    auth = ks_loading.load_auth_from_conf_options(cfg.CONF, 'nova')
    session = ks_loading.load_session_from_conf_options(
        cfg.CONF, 'nova', auth=auth
    )
    return connection.Connection(session=session)


def credential(router_token: str) -> schema.Credential:
    """The service-user half of a router's bootstrap credential.

    Read straight from the same `[nova]` options the Nova session above
    is built from, which only works for `auth_type = password`: the
    config sample documents that requirement.
    """
    conf = cfg.CONF.nova
    return schema.Credential(
        auth_url=conf.auth_url,
        username=conf.username,
        password=conf.password,
        project_name=conf.project_name,
        router_token=router_token,
        user_domain_name=conf.user_domain_name,
        project_domain_name=conf.project_domain_name,
    )


def public_endpoint() -> str:
    """The URL an appliance should call home to.

    Read from Keystone's catalogue -- the one URL every member of the
    deployment is expected to resolve -- unless the operator overrides it
    because the management network cannot reach it.
    """
    override = config.CONF.appliance.api_endpoint_override
    if override:
        return override
    session = _get_connection().session
    endpoint = session.get_endpoint(service_type='network', interface='public')
    if not endpoint:
        raise RuntimeError(
            "no public 'network' endpoint in the keystone catalogue; set "
            '[appliance] api_endpoint_override'
        )
    return endpoint


def ca_cert() -> str | None:
    """PEM bundle to ship on the config drive, or None for a well-known CA."""
    path = cfg.CONF.nova.cafile
    if not path:
        return None
    with open(path, encoding='utf-8') as cert_file:
        return cert_file.read()


def _parse_flavor_snapshot(flavor_snapshot: str | None):
    """The Nova flavor and image a router's instance is built from.

    `nova_flavor` falls back to the operator default; `image` has none on
    purpose, because a router with no image anywhere should fail loudly
    rather than boot on a guess.
    """
    data = json.loads(flavor_snapshot) if flavor_snapshot else {}
    nova_flavor = data.get('nova_flavor') or config.CONF.appliance.nova_flavor
    image = data.get('image')
    if not nova_flavor:
        raise ValueError(
            'no nova flavor: neither the service profile metainfo nor '
            '[appliance] nova_flavor names one'
        )
    if not image:
        raise ValueError(
            'no image: the service profile metainfo must set "image"'
        )
    return nova_flavor, image


def ensure_keypair(public_key: str | None) -> str | None:
    """Idempotently import the debug key. Returns its name, or None.

    Best effort: whether the image actually honours the injected key is
    outside this driver's control.
    """
    if not public_key:
        return None
    conn = _get_connection()
    if conn.compute.find_keypair(DEBUG_KEYPAIR_NAME) is None:
        conn.compute.create_keypair(
            name=DEBUG_KEYPAIR_NAME, public_key=public_key
        )
    return DEBUG_KEYPAIR_NAME


def boot(
    router_id: str,
    port_id: str,
    flavor_snapshot: str | None,
    bootstrap: schema.Bootstrap,
    key_name: str | None = None,
) -> str:
    """Boot one appliance instance. Returns its Nova id.

    One NIC -- the management port `networking.py` already created -- and
    a config drive whose `user_data` is the bootstrap blob: the agent
    reads it back as plain JSON, no cloud-init involved. Tagged with the
    router id, so the orphan sweep can find the instance even if
    neutron-server dies before anything else learns its id.
    """
    conn = _get_connection()
    nova_flavor, image = _parse_flavor_snapshot(flavor_snapshot)
    flavor = conn.compute.find_flavor(nova_flavor, ignore_missing=False)
    found_image = conn.image.find_image(image, ignore_missing=False)
    user_data = base64.b64encode(
        json.dumps(bootstrap.to_dict()).encode('utf-8')
    ).decode('ascii')
    attrs = {
        'name': f'appliance-{router_id}',
        'flavor_id': flavor.id,
        'image_id': found_image.id,
        'networks': [{'port': port_id}],
        'config_drive': True,
        'user_data': user_data,
        'tags': [router_id],
    }
    if key_name:
        attrs['key_name'] = key_name
    server = conn.compute.create_server(**attrs)
    LOG.info(
        'router %s: instance %s requested (flavor %s, image %s)',
        router_id,
        server.id,
        nova_flavor,
        image,
    )
    return server.id


def delete(instance_id: str):
    """Delete one appliance instance. Quiet if it is already gone."""
    _get_connection().compute.delete_server(instance_id, ignore_missing=True)
    LOG.info('instance %s deletion requested', instance_id)


def find_instance(router_id: str):
    """The Nova instance tagged with `router_id`, or None."""
    for server in _get_connection().compute.servers(tags=router_id):
        return server
    return None


def instance_router_ids():
    """Router ids tagged onto a Nova instance that exists right now.

    Not what the database remembers: see `reconcile.converge`.
    """
    return {
        tag
        for server in _get_connection().compute.servers()
        for tag in (server.tags or [])
    }
