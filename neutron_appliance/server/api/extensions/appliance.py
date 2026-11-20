# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry

"""The appliance API extension: sync and status.

    POST /v2.0/appliance/routers/{router_id}/sync
    GET  /v2.0/appliance/routers/{router_id}

Both live under the neutron API tree, so a request traverses the normal
pipeline and arrives here already authenticated by keystonemiddleware:
no `api-paste.ini` change and no exempted path. That is the operational
reason the two-secret design was chosen, and it is why there is no WSGI
middleware anywhere in this file.

Authentication is therefore in two halves. Keystone and the policy rules
in `server/policies.py` decide who may knock; the per-router token in
`X-Appliance-Token`, checked against the hash stored for the `router_id`
of the URL, decides whose configuration they get. Neither half is
sufficient: the service user secret sits on every appliance's config
drive.

Neutron's loader executes this file by path and instantiates the class
whose name is the file's, capitalised. `Appliance` and `appliance.py` are
one name, spelled twice.
"""

from neutron import policy
from neutron.api import extensions as api_extensions
from neutron.api.v2 import resource
from neutron_lib.api import extensions as lib_extensions
from neutron_lib.api import faults
from neutron_lib.plugins import constants as plugin_constants
from neutron_lib.plugins import directory
from oslo_log import log as logging
from oslo_utils import timeutils
import webob.exc

from neutron_appliance.common import protocol, schema
from neutron_appliance.server import config, credentials, render
from neutron_appliance.server.db import api as appliance_db


LOG = logging.getLogger(__name__)

ALIAS = 'appliance'
COLLECTION = 'routers'
PATH_PREFIX = '/appliance'

# Everything neutron maps by default, plus the two failures only this
# extension can produce. A per-router token that matches nothing is a 401
# and not a 403: the credential is wrong, not insufficient, and the agent
# treats the two the same way anyway (docs/protocol.md section 5).
FAULT_MAP = dict(faults.FAULT_MAP)
FAULT_MAP[credentials.CredentialMismatch] = webob.exc.HTTPUnauthorized
FAULT_MAP[protocol.ProtocolError] = webob.exc.HTTPBadRequest


class Controller:
    """Handlers for the two appliance endpoints.

    `routes` builds a full REST collection from a resource declaration,
    so the other five verbs exist whether or not they mean anything here.
    They are answered with 405 rather than left to raise AttributeError
    and become a 500.

    It also passes the member identifier as a keyword argument named
    'id', so the handlers have to spell it that way, builtin or not.
    """

    # pylint: disable=redefined-builtin

    def _method_not_allowed(self, request, **kwargs):
        raise webob.exc.HTTPMethodNotAllowed(
            detail='appliance routers follow their neutron router; they '
            'are not created, listed, modified or deleted through this '
            'extension'
        )

    index = create = update = delete = _method_not_allowed

    @staticmethod
    def _appliance_row(context, router_id):
        entry = appliance_db.get_router(context, router_id)
        if entry is None:
            # also the answer for a router that exists but is not ours,
            # which is the same thing from out here
            raise webob.exc.HTTPNotFound(
                detail=f'{router_id} is not an appliance router'
            )
        return entry

    def show(self, request, id, **kwargs):
        """The authoritative state of one appliance router.

        Administrators only. The agent never calls this, and it is not the
        channel tenants use: router tags mirror the same information, with
        the caveat that they are user-writable and this is the source of
        truth.
        """
        context = request.context
        policy.enforce(
            context, 'get_appliance_router', {'project_id': context.project_id}
        )
        entry = self._appliance_row(context, id)
        conf = config.CONF.appliance
        status = render.router_status(
            entry,
            conf.unknown_after_intervals * conf.poll_interval_s,
            timeutils.utcnow(),
        )
        return status.to_dict()

    def sync(self, request, id, body=None):
        """One call, both jobs: take the report, answer with desired state.

        Idempotent and safe to retry, which is what lets the agent back
        off and repeat a call it never saw the answer to.
        """
        context = request.context
        policy.enforce(
            context,
            'sync_appliance_router',
            {'project_id': context.project_id},
        )
        report = schema.SyncRequest.from_dict(body)
        outcome = appliance_db.sync_router(
            context,
            id,
            request.headers.get(protocol.APPLIANCE_TOKEN_HEADER),
            report,
            config.CONF.appliance.credential_grace_period_s,
        )
        if outcome is None:
            raise webob.exc.HTTPNotFound(
                detail=f'{id} is not an appliance router'
            )
        return self._answer(context, id, outcome).to_dict()

    @staticmethod
    def _answer(context, router_id, outcome):
        document = None
        if outcome.applied_generation != outcome.desired_generation:
            # the agent is behind, so it gets the whole document. At the
            # current generation the answer is a bare heartbeat, which is
            # the common case by a wide margin
            l3_plugin = directory.get_plugin(plugin_constants.L3)
            # the appliance row belongs to no project, and the agent's
            # own context cannot see a tenant's router
            router = l3_plugin.get_router(context.elevated(), router_id)
            document = render.config_document(
                router, outcome.desired_generation
            )
            LOG.debug('router %s: sending %s', router_id, document)
        credential = None
        if outcome.new_token:
            credential = schema.CredentialUpdate(
                router_token=outcome.new_token
            )
        return schema.SyncResponse(
            generation=outcome.desired_generation,
            poll_interval_s=config.CONF.appliance.poll_interval_s,
            config=document,
            credential=credential,
        )


class Appliance(lib_extensions.ExtensionDescriptor):
    """Descriptor neutron's extension manager loads this file for."""

    @classmethod
    def get_name(cls):
        return 'Appliance routers'

    @classmethod
    def get_alias(cls):
        return ALIAS

    @classmethod
    def get_description(cls):
        return (
            'Sync and status endpoints for neutron routers backed by a '
            'service VM.'
        )

    @classmethod
    def get_updated(cls):
        return '2026-11-20T00:00:00Z'

    def get_resources(self):
        return [
            api_extensions.ResourceExtension(
                COLLECTION,
                resource.Resource(Controller(), faults=FAULT_MAP),
                path_prefix=PATH_PREFIX,
                member_actions={'sync': 'POST'},
            )
        ]


# An extension is normally claimed by a plugin through
# `supported_extension_aliases`, and the manager drops any extension no
# plugin claims. We are a service provider behind the L3 plugin, not a
# plugin, so there is nobody to claim us; this is neutron's supported way
# of saying so. It has to run before the manager checks, which is why it
# is here, in the module the manager itself executes.
api_extensions.register_custom_supported_check(ALIAS, lambda: True)
