# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry

"""The loop: apply before sync, never report the unapplied, never lose a cache.

The acceptance scenario of stage pre-0 M5 lives at the bottom, against the
loopback stub control plane: first sync, rotation, a server that goes away for
ten minutes, and a reboot that reconverges with the server still down.
"""

import logging

import pytest

from neutron_appliance.agent import bootstrap as agent_bootstrap
from neutron_appliance.agent import client as agent_client
from neutron_appliance.agent import main as agent_main
from neutron_appliance.agent import state as agent_state
from neutron_appliance.agent.backends import base, noop
from neutron_appliance.common import protocol, schema
from neutron_appliance.common.protocol import ApplianceState
from tests import stub_control_plane


ROUTER_ID = stub_control_plane.ROUTER_ID

BOOTSTRAP = stub_control_plane.bootstrap(
    'https://neutron.example.com:9696',
    router_token='drive-token',
    poll_interval_s=protocol.DEFAULT_POLL_INTERVAL_S,
)


class RecordingBackend(base.Backend):
    """Records what it was asked to apply; can be told to fail."""

    def __init__(self):
        self.applied = []
        self.errors = None

    def apply(self, document):
        if self.errors:
            raise base.BackendError(self.errors)
        self.applied.append(document)

    def observe(self):
        return schema.Observed()


class ScriptedClient:
    """Returns or raises one queued answer per call."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.reports = []
        self.rotations = []

    def sync(self, report):
        self.reports.append(report)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def rotate(self, token):
        self.rotations.append(token)


class ExplodingBackend(RecordingBackend):
    """A backend that fails in the way a real one never should."""

    def apply(self, document):
        raise RuntimeError('kaboom')

    def observe(self):
        raise RuntimeError('cannot list interfaces')


def document(generation):
    return schema.ConfigDocument(router_id=ROUTER_ID, generation=generation)


def cached(generation=3, token='cached-token'):
    return agent_state.State(
        applied_generation=generation,
        config=document(generation),
        router_token=token,
    )


def make_agent(
    tmp_path,
    *,
    cached_state=None,
    backend=None,
    client=None,
    loader=None,
    factory=None,
    jitter=None,
):
    path = str(tmp_path / 'state.json')
    if cached_state is not None:
        agent_state.save(cached_state, path)
    return agent_main.Agent(
        backend=backend or RecordingBackend(),
        state_path=path,
        bootstrap_loader=loader or (lambda: BOOTSTRAP),
        client_factory=factory or (lambda blob, **kwargs: client),
        sleep=lambda seconds: None,
        clock=lambda: 0.0,
        jitter=jitter or (lambda: 1.0),
    )


#
# Boot: disk first, network later.
#


def test_start_applies_the_cache_without_reading_the_config_drive(tmp_path):
    def loader():
        raise AssertionError('the config drive must not be read at startup')

    backend = RecordingBackend()
    agent = make_agent(
        tmp_path, cached_state=cached(3), backend=backend, loader=loader
    )
    agent.start()
    assert [d.generation for d in backend.applied] == [3]
    assert agent.applied_generation == 3
    assert agent.appliance_state is ApplianceState.CONVERGED
    assert agent.config.generation == 3
    assert agent.client is None


def test_a_cache_that_cannot_be_applied_is_not_reported_as_applied(
    tmp_path,
):
    backend = RecordingBackend()
    backend.errors = ['device disappeared']
    agent = make_agent(tmp_path, cached_state=cached(5), backend=backend)
    agent.start()
    assert not agent.applied_generation
    assert agent.appliance_state is ApplianceState.ERROR
    assert agent.errors == ['device disappeared']


def test_a_cache_apply_that_raises_is_reported_not_fatal(tmp_path):
    agent = make_agent(
        tmp_path, cached_state=cached(5), backend=ExplodingBackend()
    )
    agent.start()
    assert not agent.applied_generation
    assert agent.appliance_state is ApplianceState.ERROR
    assert agent.errors == ['cannot apply cached configuration: kaboom']


def test_a_corrupt_cache_is_ignored_and_the_agent_starts_empty(tmp_path):
    path = tmp_path / 'state.json'
    path.write_text('{not json')
    agent = make_agent(tmp_path)
    agent.start()
    assert not agent.applied_generation
    assert agent.config is None


def test_no_config_drive_leaves_the_cache_alone(tmp_path):
    def loader():
        raise agent_bootstrap.BootstrapError('no config drive')

    backend = RecordingBackend()
    client = ScriptedClient()
    agent = make_agent(
        tmp_path,
        cached_state=cached(2),
        backend=backend,
        client=client,
        loader=loader,
    )
    agent.start()
    assert agent.sync_once() > 0
    assert not client.reports
    assert agent.applied_generation == 2
    assert [d.generation for d in backend.applied] == [2]


#
# Sync: apply, then report on the next call.
#


def test_the_first_report_says_nothing_has_been_applied(tmp_path):
    backend = RecordingBackend()
    client = ScriptedClient(
        schema.SyncResponse(generation=1, config=document(1))
    )
    agent = make_agent(tmp_path, backend=backend, client=client)
    agent.start()
    agent.sync_once()
    assert not client.reports[0].applied_generation
    assert backend.applied == [document(1)]
    assert agent.applied_generation == 1
    assert agent.appliance_state is ApplianceState.CONVERGED


def test_a_generation_is_reported_only_once_it_is_applied(tmp_path):
    client = ScriptedClient(
        schema.SyncResponse(generation=1, config=document(1)),
        schema.SyncResponse(generation=1),
    )
    agent = make_agent(tmp_path, client=client)
    agent.start()
    agent.sync_once()
    agent.sync_once()
    assert [r.applied_generation for r in client.reports] == [0, 1]
    assert agent.appliance_state is ApplianceState.CONVERGED


def test_a_heartbeat_keeps_the_applied_document(tmp_path):
    client = ScriptedClient(schema.SyncResponse(generation=3))
    backend = RecordingBackend()
    agent = make_agent(
        tmp_path, cached_state=cached(3), backend=backend, client=client
    )
    agent.start()
    backend.applied.clear()
    agent.sync_once()
    assert not backend.applied
    assert agent.applied_generation == 3
    assert agent.appliance_state is ApplianceState.CONVERGED
    assert not agent.errors


def test_a_generation_without_a_document_is_not_invented(tmp_path):
    client = ScriptedClient(schema.SyncResponse(generation=5))
    agent = make_agent(tmp_path, client=client)
    agent.start()
    agent.sync_once()
    assert not agent.applied_generation
    assert agent.appliance_state is ApplianceState.CONVERGING
    assert agent.errors


def test_an_observe_failure_is_carried_on_the_next_report(tmp_path):
    client = ScriptedClient(schema.SyncResponse(generation=0))
    agent = make_agent(tmp_path, backend=ExplodingBackend(), client=client)
    agent.start()
    agent.sync_once()
    assert client.reports[0].errors == [
        'cannot observe interfaces: cannot list interfaces'
    ]
    assert client.reports[0].observed == schema.Observed()


#
# Failures: keep the world as it is.
#


def test_a_failed_apply_keeps_the_old_generation_and_reports_why(tmp_path):
    backend = RecordingBackend()
    client = ScriptedClient(
        schema.SyncResponse(generation=2, config=document(2)),
        schema.SyncResponse(generation=2, config=document(2)),
    )
    agent = make_agent(
        tmp_path, cached_state=cached(1), backend=backend, client=client
    )
    agent.start()
    backend.errors = ['interface fa:16:3e:00:00:01 absent after 90s']
    agent.sync_once()
    assert agent.applied_generation == 1
    assert agent.appliance_state is ApplianceState.ERROR
    assert agent.errors == ['interface fa:16:3e:00:00:01 absent after 90s']
    # the cache is still the last document that actually took effect
    stored = agent_state.load(agent.state_path)
    assert stored.applied_generation == 1
    assert stored.config.generation == 1
    # and the server is told on the next call
    agent.sync_once()
    assert client.reports[-1].applied_generation == 1
    assert client.reports[-1].state is ApplianceState.ERROR
    assert client.reports[-1].errors == agent.errors


def test_an_apply_that_raises_is_reported_not_fatal(tmp_path):
    client = ScriptedClient(
        schema.SyncResponse(generation=2, config=document(2))
    )
    agent = make_agent(tmp_path, backend=ExplodingBackend(), client=client)
    agent.start()
    agent.sync_once()
    assert not agent.applied_generation
    assert agent.appliance_state is ApplianceState.ERROR
    assert agent.errors == ['cannot apply generation 2: kaboom']


def test_ten_minutes_of_failures_change_nothing(tmp_path):
    backend = RecordingBackend()
    client = ScriptedClient(
        *[agent_client.NetworkError('no route to host')] * 50
    )
    agent = make_agent(
        tmp_path, cached_state=cached(3), backend=backend, client=client
    )
    agent.start()
    applied = list(backend.applied)

    elapsed = 0
    delays = []
    while elapsed < 600:
        delay = agent.sync_once()
        delays.append(delay)
        elapsed += delay

    assert elapsed >= 600
    assert max(delays) <= protocol.MAX_BACKOFF_S
    assert delays[-1] == protocol.MAX_BACKOFF_S
    assert agent.applied_generation == 3
    assert agent.config.generation == 3
    assert backend.applied == applied
    assert agent_state.load(agent.state_path) == cached(3)


def test_a_reboot_with_the_server_still_down_reapplies_the_cache(tmp_path):
    path = str(tmp_path / 'state.json')
    agent_state.save(cached(4), path)

    def loader():
        raise agent_bootstrap.BootstrapError('no config drive')

    backend = RecordingBackend()
    agent = make_agent(
        tmp_path,
        backend=backend,
        client=ScriptedClient(),
        loader=loader,
    )
    agent.start()
    assert agent.applied_generation == 4
    assert [d.generation for d in backend.applied] == [4]
    assert agent.sync_once() > 0
    assert agent.applied_generation == 4
    assert [d.generation for d in backend.applied] == [4]


def test_a_404_is_logged_and_the_agent_keeps_running(tmp_path, monkeypatch):
    messages = []
    monkeypatch.setattr(agent_main.console, 'say', messages.append)
    client = ScriptedClient(
        agent_client.NotFoundError('HTTP 404'),
        schema.SyncResponse(generation=2),
    )
    agent = make_agent(tmp_path, cached_state=cached(2), client=client)
    agent.start()
    assert agent.sync_once() == agent.poll_interval
    assert agent.applied_generation == 2
    assert agent.config.generation == 2
    agent.sync_once()
    assert len(client.reports) == 2
    assert any('does not know this router' in m for m in messages)


def test_a_client_that_cannot_be_built_keeps_the_cache(tmp_path):
    def factory(blob, **kwargs):
        raise RuntimeError('bad keystone URL')

    client = ScriptedClient()
    agent = make_agent(
        tmp_path,
        cached_state=cached(2),
        client=client,
        factory=factory,
    )
    agent.start()
    assert agent.sync_once() > 0
    assert not client.reports
    assert agent.applied_generation == 2


def test_a_401_that_cannot_re_read_the_drive_keeps_the_cache(tmp_path):
    calls = {'count': 0}

    def loader():
        calls['count'] += 1
        if calls['count'] == 1:
            return BOOTSTRAP
        raise agent_bootstrap.BootstrapError('drive unplugged')

    client = ScriptedClient(agent_client.AuthError('HTTP 401'))
    agent = make_agent(
        tmp_path, cached_state=cached(2), client=client, loader=loader
    )
    agent.start()
    assert agent.sync_once() > 0
    assert agent.applied_generation == 2
    assert agent.config.generation == 2
    assert agent.client is None
    assert agent.router_token == 'cached-token'


def test_a_401_re_reads_the_config_drive_and_keeps_the_cache(tmp_path):
    client = ScriptedClient(agent_client.AuthError('HTTP 401'))
    agent = make_agent(
        tmp_path, cached_state=cached(2, token='stale-token'), client=client
    )
    agent.start()
    delay = agent.sync_once()
    assert delay >= protocol.DEFAULT_POLL_INTERVAL_S
    assert agent.router_token == 'drive-token'
    assert agent.client is None
    assert agent.applied_generation == 2
    assert agent.config.generation == 2
    assert agent_state.load(agent.state_path).config.generation == 2


#
# Rotation: persist, then use.
#


def test_a_rotated_token_is_persisted_before_the_client_uses_it(
    tmp_path, monkeypatch
):
    calls = []
    real_save = agent_state.save

    def spy_save(value, path=agent_state.STATE_PATH):
        calls.append(('save', value.router_token))
        real_save(value, path)

    monkeypatch.setattr(agent_main.state, 'save', spy_save)

    client = ScriptedClient(
        schema.SyncResponse(
            generation=0,
            credential=schema.CredentialUpdate(router_token='rotated-token'),
        )
    )
    client.rotate = lambda token: calls.append(('rotate', token))
    agent = make_agent(tmp_path, client=client)
    agent.start()
    agent.sync_once()

    rotate_at = calls.index(('rotate', 'rotated-token'))
    assert ('save', 'rotated-token') in calls[:rotate_at]
    assert agent.router_token == 'rotated-token'
    assert agent_state.load(agent.state_path).router_token == 'rotated-token'


def test_a_rotation_that_cannot_be_persisted_is_not_used(tmp_path):
    client = ScriptedClient(
        schema.SyncResponse(
            generation=0,
            credential=schema.CredentialUpdate(router_token='rotated-token'),
        )
    )
    agent = make_agent(tmp_path, client=client)
    agent.start()
    agent.state_path = str(tmp_path / 'read-only' / 'state.json')
    (tmp_path / 'read-only').write_text('')
    agent.sync_once()
    # the token in use is the one that was persisted, not the new one
    assert agent.router_token == 'drive-token'
    assert not client.rotations


#
# The acceptance scenario, over loopback HTTP.
#


@pytest.fixture
def stub():
    server = stub_control_plane.StubControlPlane()
    yield server
    server.close()


def test_the_whole_loop_against_a_stub_server(tmp_path, stub):
    backend = RecordingBackend()
    agent = agent_main.Agent(
        backend=backend,
        state_path=str(tmp_path / 'state.json'),
        bootstrap_loader=lambda: stub_control_plane.bootstrap(stub.url),
        sleep=lambda seconds: None,
        clock=lambda: 0.0,
        jitter=lambda: 1.0,
    )
    agent.start()
    agent.sync_once()

    stored = agent_state.load(agent.state_path)
    assert stored.applied_generation == 1
    assert stored.config.generation == 1
    assert stored.router_token == stub.current_token
    assert stored.router_token != stub_control_plane.INITIAL_TOKEN
    assert backend.applied == [document(1)]

    # the next call uses the rotated token and retires the old one
    agent.sync_once()
    assert stub.previous_token is None
    assert [r.applied_generation for r in stub.reports] == [0, 1]


def test_a_reboot_reuses_the_persisted_rotated_token(tmp_path, stub):
    path = str(tmp_path / 'state.json')

    def make():
        return agent_main.Agent(
            backend=RecordingBackend(),
            state_path=path,
            bootstrap_loader=lambda: stub_control_plane.bootstrap(stub.url),
            sleep=lambda seconds: None,
            clock=lambda: 0.0,
            jitter=lambda: 1.0,
        )

    first = make()
    first.start()
    first.sync_once()
    assert stub.current_token != stub_control_plane.INITIAL_TOKEN

    # the grace window is over: the token on the config drive is dead
    stub.previous_token = None

    second = make()
    second.start()
    second.sync_once()
    # a second report reached the server, so the rotated token authenticated
    assert len(stub.reports) == 2
    assert second.router_token == stub.current_token


def test_the_loop_keeps_the_cache_while_the_stub_server_is_down(
    tmp_path, stub
):
    backend = RecordingBackend()
    agent = agent_main.Agent(
        backend=backend,
        state_path=str(tmp_path / 'state.json'),
        bootstrap_loader=lambda: stub_control_plane.bootstrap(stub.url),
        sleep=lambda seconds: None,
        clock=lambda: 0.0,
        jitter=lambda: 1.0,
    )
    agent.start()
    agent.sync_once()
    applied = list(backend.applied)

    stub.fail_status = 503
    for _ in range(10):
        assert agent.sync_once() > 0
    assert agent.applied_generation == 1
    assert backend.applied == applied

    stub.fail_status = None
    agent.sync_once()
    assert agent.applied_generation == 1
    assert agent.appliance_state is ApplianceState.CONVERGED


#
# The noop backend, which is the only one pre-0 ships.
#


def test_the_noop_backend_changes_nothing(caplog):
    with caplog.at_level(logging.DEBUG):
        backend = noop.NoopBackend()
        backend.apply(document(7))
        assert backend.observe() == schema.Observed()
    assert 'generation 7' in caplog.text


def test_the_noop_backend_names_the_interfaces_and_routes(caplog):
    full = schema.ConfigDocument(
        router_id=ROUTER_ID,
        generation=2,
        interfaces=[
            schema.Interface(
                mac='fa:16:3e:11:22:33',
                role=protocol.InterfaceRole.INTERNAL,
                addresses=['10.0.0.1/24'],
                mtu=1442,
            )
        ],
        routes=[schema.Route(destination='0.0.0.0/0', nexthop='172.24.4.1')],
    )
    with caplog.at_level(logging.INFO):
        noop.NoopBackend().apply(full)
    assert 'fa:16:3e:11:22:33' in caplog.text
    assert '0.0.0.0/0' in caplog.text


def test_agent_version_falls_back_when_the_package_is_missing(monkeypatch):
    def missing(name):
        raise agent_main.importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(agent_main.importlib.metadata, 'version', missing)
    assert agent_main.agent_version() == 'unknown'


def test_main_runs_the_agent(monkeypatch):
    ran = []

    class StubAgent:
        def run(self):
            ran.append(True)

    monkeypatch.setattr(agent_main, 'Agent', StubAgent)
    assert not agent_main.main()
    assert ran == [True]
