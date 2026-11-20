# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry

"""The two appliance endpoints: routing, policy and the documents.

Three separable things, each of which fails silently in its own way:

  - the URLs `routes` builds are the ones `common/protocol.py` tells the
    agent to call. A prefix typo shows up as a 404 from a server that
    looks perfectly healthy, on appliances already in the field
  - the policy defaults let the appliance-agent role reach the sync
    endpoint and nothing else
  - the handlers answer with the documents the protocol defines

The `context` and `make_router` fixtures come from conftest.py. All of
this needs neutron and is skipped where it is absent.
"""

import datetime
from unittest import mock

import pytest


routes = pytest.importorskip('routes')
webob_exc = pytest.importorskip('webob.exc')
oslo_policy = pytest.importorskip('oslo_policy.policy')
cfg = pytest.importorskip('oslo_config.cfg')
timeutils = pytest.importorskip('oslo_utils.timeutils')
neutron_base = pytest.importorskip('neutron.conf.policies.base')
appliance = pytest.importorskip(
    'neutron_appliance.server.api.extensions.appliance'
)
policies = pytest.importorskip('neutron_appliance.server.policies')
credentials = pytest.importorskip('neutron_appliance.server.credentials')
appliance_db = pytest.importorskip('neutron_appliance.server.db.api')
protocol = pytest.importorskip('neutron_appliance.common.protocol')
schema = pytest.importorskip('neutron_appliance.common.schema')

ROUTER_ID = '7f0e2c3a-1111-4000-8000-000000000001'
OTHER_ROUTER_ID = '7f0e2c3a-1111-4000-8000-000000000002'
BOOT_ID = 'e3b0c442-3333-4000-8000-000000000001'

# the extension middleware is mounted under /v2.0, so the paths the agent
# is handed carry a prefix the mapper itself never sees
API_ROOT = '/v2.0'


#
# Routing
#


def match(path, method):
    (res,) = appliance.Appliance().get_resources()
    mapper = routes.Mapper()
    mapper.resource(
        res.collection,
        res.collection,
        controller=res.controller,
        member=res.member_actions,
        path_prefix=res.path_prefix,
    )
    assert path.startswith(API_ROOT)
    return mapper.match(
        path[len(API_ROOT) :], environ={'REQUEST_METHOD': method}
    )


def test_the_routes_are_the_paths_the_agent_is_told_to_call():
    # protocol.py is what ends up on a config drive. If the two ever
    # disagree, every appliance built from that image is lost
    hit = match(protocol.sync_path(ROUTER_ID), 'POST')
    assert hit['action'] == 'sync'
    assert hit['id'] == ROUTER_ID

    hit = match(protocol.status_path(ROUTER_ID), 'GET')
    assert hit['action'] == 'show'
    assert hit['id'] == ROUTER_ID


@pytest.mark.parametrize(
    ('method', 'path'),
    [
        ('GET', protocol.ROUTERS_PATH),
        ('POST', protocol.ROUTERS_PATH),
        ('PUT', protocol.status_path(ROUTER_ID)),
        ('DELETE', protocol.status_path(ROUTER_ID)),
    ],
)
def test_the_rest_of_the_collection_is_refused(method, path):
    # routes builds a whole REST collection whether one is wanted or
    # not. None of it means anything: an appliance follows its router
    hit = match(path, method)
    handler = getattr(hit['controller'].controller, hit['action'])
    with pytest.raises(webob_exc.HTTPMethodNotAllowed):
        handler(request=None, id=ROUTER_ID, body={})


#
# Policy
#


@pytest.fixture
def rules(conf):
    """An enforcer holding the defaults this package registers."""
    out = oslo_policy.Enforcer(conf)
    # our rules are written in terms of neutron's own admin_only
    out.register_defaults(neutron_base.rules)
    out.register_defaults(policies.list_rules())
    return out


def creds(*roles):
    return {
        'roles': list(roles),
        'user_id': 'u',
        'project_id': 'p',
        'system_scope': None,
        'domain_id': None,
    }


def test_the_agent_role_reaches_sync_and_nothing_else(rules):
    agent = creds('appliance-agent')
    assert rules.enforce('sync_appliance_router', {}, agent)
    # a compromised appliance must not be able to enumerate the state of
    # every other router in the cloud
    assert not rules.enforce('get_appliance_router', {}, agent)


def test_an_ordinary_user_reaches_neither(rules):
    member = creds('member', 'reader')
    assert not rules.enforce('sync_appliance_router', {}, member)
    assert not rules.enforce('get_appliance_router', {}, member)


def test_an_administrator_reaches_both(rules):
    admin = creds('admin')
    assert rules.enforce('sync_appliance_router', {}, admin)
    assert rules.enforce('get_appliance_router', {}, admin)


#
# Handlers
#


class Request:
    """Just enough of a webob request for the handlers."""

    def __init__(self, context, token=None):
        self.context = context
        self.headers = {}
        if token is not None:
            self.headers[protocol.APPLIANCE_TOKEN_HEADER] = token


@pytest.fixture
def controller(context):
    """The controller, with policy and the L3 plugin stubbed out.

    The rules themselves are exercised above; what matters here is that
    every handler asks, and which rule it asks for. The L3 plugin is a
    stub because a neutron router needs the whole plugin stack and all
    this one contributes to the document is `admin_state_up`.
    """
    asked = []
    l3_plugin = mock.Mock()
    l3_plugin.get_router.side_effect = lambda ctx, router_id: {
        'id': router_id,
        'admin_state_up': True,
    }
    with (
        mock.patch.object(
            appliance.policy,
            'enforce',
            side_effect=lambda ctx, rule, target: asked.append(rule),
        ),
        mock.patch.object(
            appliance.directory, 'get_plugin', return_value=l3_plugin
        ),
    ):
        ctrl = appliance.Controller()
        ctrl.asked = asked
        yield ctrl


@pytest.fixture
def synced(context, make_router):
    """An appliance router holding a freshly issued credential."""
    router = make_router(ROUTER_ID)
    appliance_db.create_router(context, router)
    return router, appliance_db.issue_credential(context, router)


def sync_body(applied_generation=0, state=protocol.ApplianceState.CONVERGING):
    return schema.SyncRequest(
        agent_version='0.0.1',
        boot_id=BOOT_ID,
        uptime_s=1,
        applied_generation=applied_generation,
        state=state,
    ).to_dict()


def test_a_first_sync_returns_an_empty_document(controller, context, synced):
    router, token = synced
    answer = controller.sync(
        request=Request(context, token), id=router, body=sync_body()
    )
    assert controller.asked == ['sync_appliance_router']
    response = schema.SyncResponse.from_dict(answer)
    assert response.generation == appliance_db.FIRST_GENERATION
    # pre-0 has no data plane: the document is empty by construction
    assert response.config.router_id == router
    assert response.config.interfaces == []
    assert response.config.routes == []
    assert response.poll_interval_s == cfg.CONF.appliance.poll_interval_s
    # the token that came off the config drive is used exactly once
    assert response.credential.router_token != token
    assert appliance_db.get_router(context, router).last_seen is not None


def test_a_converged_agent_gets_a_bare_heartbeat(controller, context, synced):
    router, token = synced
    first = controller.sync(
        request=Request(context, token), id=router, body=sync_body()
    )
    token = schema.SyncResponse.from_dict(first).credential.router_token
    answer = controller.sync(
        request=Request(context, token),
        id=router,
        body=sync_body(
            applied_generation=appliance_db.FIRST_GENERATION,
            state=protocol.ApplianceState.CONVERGED,
        ),
    )
    response = schema.SyncResponse.from_dict(answer)
    assert response.config is None
    assert response.credential is None


def test_the_wrong_routers_token_is_refused(
    controller, context, make_router, synced
):
    router, _ = synced
    other = make_router(OTHER_ROUTER_ID)
    appliance_db.create_router(context, other)
    stolen = appliance_db.issue_credential(context, other)
    # a valid token, for the wrong router, against this router's URL
    with pytest.raises(credentials.CredentialMismatch):
        controller.sync(
            request=Request(context, stolen), id=router, body=sync_body()
        )
    assert (
        appliance.FAULT_MAP[credentials.CredentialMismatch]
        is webob_exc.HTTPUnauthorized
    )


def test_a_malformed_report_is_a_bad_request(controller, context, synced):
    router, token = synced
    with pytest.raises(protocol.ValidationError):
        controller.sync(
            request=Request(context, token), id=router, body={'state': 'nope'}
        )
    assert issubclass(protocol.ValidationError, protocol.ProtocolError)
    assert appliance.FAULT_MAP[protocol.ProtocolError] is (
        webob_exc.HTTPBadRequest
    )


def test_an_unknown_router_is_not_found(controller, context):
    with pytest.raises(webob_exc.HTTPNotFound):
        controller.sync(
            request=Request(context, 'token'), id='nobody', body=sync_body()
        )
    with pytest.raises(webob_exc.HTTPNotFound):
        controller.show(request=Request(context), id='nobody')


def test_the_status_reports_what_the_agent_last_said(
    controller, context, synced
):
    router, token = synced
    controller.sync(
        request=Request(context, token), id=router, body=sync_body()
    )
    status = schema.RouterStatus.from_dict(
        controller.show(request=Request(context), id=router)
    )
    assert controller.asked == [
        'sync_appliance_router',
        'get_appliance_router',
    ]
    assert status.router_id == router
    assert status.desired_generation == appliance_db.FIRST_GENERATION
    assert not status.applied_generation
    assert status.state is protocol.ApplianceState.CONVERGING
    assert status.last_seen is not None
    assert status.errors == []


def test_a_silent_appliance_becomes_unknown(controller, context, synced):
    router, token = synced
    controller.sync(
        request=Request(context, token), id=router, body=sync_body()
    )
    conf = cfg.CONF.appliance
    silent_for = conf.unknown_after_intervals * conf.poll_interval_s + 60
    later = timeutils.utcnow() + datetime.timedelta(seconds=silent_for)
    with mock.patch.object(appliance.timeutils, 'utcnow', return_value=later):
        status = schema.RouterStatus.from_dict(
            controller.show(request=Request(context), id=router)
        )
    # not an error: the appliance is most likely still forwarding, it
    # just cannot be asked
    assert status.state is protocol.ApplianceState.UNKNOWN
