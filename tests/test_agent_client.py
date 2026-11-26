# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry

"""The sync client: URL, credentials, and one exception per outcome.

The status codes are the contract (docs/protocol.md section 5). A 503 that
comes back as an AuthError would make the agent discard a perfectly good
token, so each one is pinned down.
"""

import uuid

from keystoneauth1 import exceptions as keystone
import pytest

from neutron_appliance.agent import client as agent_client
from neutron_appliance.common import protocol, schema
from tests import stub_control_plane


# pylint: disable=protected-access


ROUTER_ID = stub_control_plane.ROUTER_ID


def report(applied=0, boot_id=None):
    return schema.SyncRequest(
        agent_version='test',
        boot_id=boot_id or str(uuid.uuid4()),
        uptime_s=1,
        applied_generation=applied,
        state=protocol.ApplianceState.CONVERGING,
    )


class FakeResponse:
    def __init__(self, status_code, payload=None, text=''):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeSession:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def make_client(result):
    client = agent_client.Client(
        stub_control_plane.bootstrap('https://neutron.example.com:9696')
    )
    client._session = FakeSession(result)
    return client


def test_the_url_is_the_endpoint_plus_the_sync_path():
    blob = stub_control_plane.bootstrap('https://neutron.example.com:9696/')
    assert (
        agent_client.Client(blob)._url
        == f'https://neutron.example.com:9696{protocol.sync_path(ROUTER_ID)}'
    )


def test_a_successful_answer_is_parsed():
    config = schema.ConfigDocument(router_id=ROUTER_ID, generation=2)
    answer = schema.SyncResponse(generation=2, config=config)
    client = make_client(FakeResponse(200, answer.to_dict()))
    assert client.sync(report()) == answer


def test_the_report_and_the_router_token_are_sent():
    answer = schema.SyncResponse(generation=0)
    client = make_client(FakeResponse(200, answer.to_dict()))
    client.sync(report(applied=4))
    url, kwargs = client._session.calls[0]
    assert url.endswith(protocol.sync_path(ROUTER_ID))
    assert kwargs['json']['applied_generation'] == 4
    assert kwargs['headers'] == {
        protocol.APPLIANCE_TOKEN_HEADER: stub_control_plane.INITIAL_TOKEN
    }


def test_rotate_changes_the_next_header():
    answer = schema.SyncResponse(generation=0)
    client = make_client(FakeResponse(200, answer.to_dict()))
    client.rotate('new-token')
    client.sync(report())
    _, kwargs = client._session.calls[0]
    assert kwargs['headers'] == {protocol.APPLIANCE_TOKEN_HEADER: 'new-token'}


@pytest.mark.parametrize(
    'status, error',
    [
        (401, agent_client.AuthError),
        (403, agent_client.AuthError),
        (404, agent_client.NotFoundError),
        (500, agent_client.ServerError),
        (503, agent_client.ServerError),
        (400, agent_client.SyncError),
        (202, agent_client.SyncError),
    ],
)
def test_each_status_maps_to_the_reaction_the_loop_needs(status, error):
    client = make_client(FakeResponse(status, {}, text='sad'))
    with pytest.raises(error, match=f'HTTP {status}'):
        client.sync(report())


def test_a_malformed_body_is_a_sync_error():
    client = make_client(FakeResponse(200, ValueError('not json')))
    with pytest.raises(agent_client.SyncError, match='malformed'):
        client.sync(report())


def test_a_long_error_body_is_truncated():
    client = make_client(FakeResponse(500, {}, text='x' * 300))
    with pytest.raises(agent_client.ServerError) as raised:
        client.sync(report())
    assert str(raised.value).startswith('HTTP 500: ')
    assert str(raised.value).endswith('...')
    assert len(str(raised.value)) < 220


def test_an_empty_error_body_is_just_the_status():
    client = make_client(FakeResponse(503, {}, text='  \n '))
    with pytest.raises(agent_client.ServerError, match=r'^HTTP 503$'):
        client.sync(report())


def test_a_body_that_is_not_a_sync_response_is_a_sync_error():
    client = make_client(FakeResponse(200, {'generation': 'many'}))
    with pytest.raises(agent_client.SyncError, match='malformed'):
        client.sync(report())


def test_a_refused_service_user_is_an_auth_error():
    client = make_client(keystone.http.Unauthorized('no service token'))
    with pytest.raises(agent_client.AuthError, match='service user'):
        client.sync(report())


def test_an_authorization_failure_is_an_auth_error():
    client = make_client(keystone.AuthorizationFailure('bad password'))
    with pytest.raises(agent_client.AuthError, match='service user'):
        client.sync(report())


def test_a_tls_failure_is_a_network_error_that_names_the_cause():
    failure = keystone.connection.SSLError('certificate verify failed')
    client = make_client(failure)
    with pytest.raises(
        agent_client.NetworkError, match='certificate verify failed'
    ):
        client.sync(report())


def test_a_connection_failure_is_a_network_error():
    client = make_client(keystone.connection.ConnectFailure('no route'))
    with pytest.raises(agent_client.NetworkError, match='no route'):
        client.sync(report())


@pytest.mark.parametrize(
    'attempt, delay',
    [(0, 1), (1, 2), (2, 4), (8, 256), (9, 300), (10, 300), (200, 300)],
)
def test_backoff_doubles_then_caps(attempt, delay):
    assert agent_client.backoff_delay(attempt, jitter=lambda: 1.0) == delay


def test_backoff_is_a_random_fraction_of_the_ceiling():
    assert agent_client.backoff_delay(3, jitter=lambda: 0.5) == 4
    assert not agent_client.backoff_delay(3, jitter=lambda: 0.0)


#
# Against a real loopback HTTP server, the whole request path.
#


@pytest.fixture
def stub():
    server = stub_control_plane.StubControlPlane()
    yield server
    server.close()


def test_a_request_traverses_auth_and_sync(stub):
    client = agent_client.Client(stub_control_plane.bootstrap(stub.url))
    answer = client.sync(report())
    assert answer.generation == 1
    assert answer.config is not None
    assert answer.credential is not None
    assert stub.auth_requests == 1
    assert [r.applied_generation for r in stub.reports] == [0]


def test_the_rotated_token_is_the_one_that_keeps_working(stub):
    client = agent_client.Client(stub_control_plane.bootstrap(stub.url))
    boot_id = str(uuid.uuid4())
    rotated = client.sync(report(boot_id=boot_id)).credential.router_token
    client.rotate(rotated)
    heartbeat = client.sync(report(applied=1, boot_id=boot_id))
    assert heartbeat.config is None
    assert heartbeat.credential is None
    assert stub.previous_token is None

    client.rotate(stub_control_plane.INITIAL_TOKEN)
    with pytest.raises(agent_client.AuthError):
        client.sync(report(applied=1, boot_id=boot_id))
