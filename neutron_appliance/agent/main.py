# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry

"""Agent entry point: read bootstrap, apply cached config, then sync.

The order is the hard requirement of the pull model: the on-disk cache is
applied *before* the control plane is contacted. Forwarding never depends on
reachability, only configuration changes do. A failed sync keeps the applied
configuration; nothing here ever tears it down.
"""

from __future__ import annotations

import importlib.metadata
import logging
import os
import random
import time
import uuid

from neutron_appliance.agent import bootstrap, console, state
from neutron_appliance.agent import client as sync_client
from neutron_appliance.agent.backends import base, noop
from neutron_appliance.common import protocol, schema
from neutron_appliance.common.protocol import ApplianceState


LOG = logging.getLogger(__name__)


def agent_version() -> str:
    """The version of this agent, for the sync report."""
    try:
        return importlib.metadata.version('neutron-appliance')
    except importlib.metadata.PackageNotFoundError:
        return 'unknown'


class Agent:
    """The sync loop, with every collaborator replaceable for the tests."""

    def __init__(
        self,
        *,
        backend=None,
        state_path=state.STATE_PATH,
        bootstrap_loader=bootstrap.load,
        client_factory=sync_client.Client,
        sleep=time.sleep,
        clock=time.monotonic,
        jitter=random.random,
    ):
        self.backend = backend or noop.NoopBackend()
        self.state_path = state_path
        self._bootstrap_loader = bootstrap_loader
        self._client_factory = client_factory
        self._sleep = sleep
        self._clock = clock
        self._jitter = jitter

        self.agent_version = agent_version()
        self.boot_id = str(uuid.uuid4())
        self.started_at = clock()
        self.poll_interval = protocol.DEFAULT_POLL_INTERVAL_S
        self.applied_generation = 0
        self.config = None
        self.router_token = None
        self.appliance_state = ApplianceState.CONVERGING
        self.errors = []
        self.bootstrap = None
        self.client = None
        self._attempt = 0
        self._saved = state.State()

    def start(self) -> None:
        """Apply the last configuration from disk, before any network call."""
        LOG.info(
            'agent %s starting, boot %s', self.agent_version, self.boot_id
        )
        console.say(
            f'starting, agent {self.agent_version}, boot {self.boot_id}'
        )
        try:
            cached = state.load(self.state_path)
        except state.StateError as e:
            LOG.error('cannot read cached state: %s', e)
            console.say(f'cannot read cached state: {e}')
            cached = state.State()
        self._saved = cached
        self.router_token = cached.router_token
        self.config = cached.config
        self.applied_generation = cached.applied_generation

        if cached.config is None:
            console.say(
                'no cached configuration, waiting for the control plane'
            )
            return

        console.say(f'reapplying cached generation {cached.config.generation}')
        try:
            self.backend.apply(cached.config)
        except base.BackendError as e:
            self.appliance_state = ApplianceState.ERROR
            self.errors = list(e.errors)
            # never report a generation that is not applied in this boot
            self.applied_generation = 0
            self._log_failure('cannot apply cached configuration', e)
        except Exception as e:
            self.appliance_state = ApplianceState.ERROR
            self.errors = [f'cannot apply cached configuration: {e}']
            # never report a generation that is not applied in this boot
            self.applied_generation = 0
            LOG.exception('cannot apply cached configuration')
            console.say(f'cannot apply cached configuration: {e}')
        else:
            self.appliance_state = ApplianceState.CONVERGED
            self.applied_generation = cached.config.generation
            console.say(
                f'cached generation {cached.config.generation} applied'
            )

    def run(self) -> None:
        self.start()
        while True:
            self._sleep(self.sync_once())

    def sync_once(self) -> float:
        """One attempt. Returns how long the loop should sleep afterwards."""
        if not self._connect():
            return self._backoff()

        report = self._report()
        try:
            answer = self.client.sync(report)
        except sync_client.AuthError as e:
            return self._on_auth_error(e)
        except sync_client.NotFoundError as e:
            self._log_failure('the control plane does not know this router', e)
            return self.poll_interval
        except sync_client.SyncError as e:
            self._log_failure('sync failed', e)
            return self._backoff()

        self._attempt = 0
        self.poll_interval = answer.poll_interval_s
        self._take_credential(answer)
        if answer.config is not None:
            self._apply(answer.config)
        elif answer.generation > self.applied_generation:
            self.appliance_state = ApplianceState.CONVERGING
            self.errors = [
                f'the server reported generation {answer.generation} '
                'without a configuration document'
            ]
            LOG.error(
                'router %s: generation %d without a document',
                self.bootstrap.router_id,
                answer.generation,
            )
        else:
            self.appliance_state = ApplianceState.CONVERGED
            self.errors = []
        self._save()
        return self.poll_interval

    def _connect(self) -> bool:
        """Build the client from the config drive, once the first time."""
        if self.client is not None:
            return True
        try:
            blob = self._bootstrap_loader()
        except Exception as e:
            self._log_failure('cannot read the bootstrap blob', e)
            return False
        try:
            ca_path = state.ensure_ca(
                blob.ca_cert,
                os.path.join(
                    os.path.dirname(self.state_path), state.CA_FILENAME
                ),
            )
            self.client = self._client_factory(
                blob,
                ca_path=ca_path,
                router_token=self.router_token,
            )
        except Exception as e:
            self._log_failure('cannot build the sync client', e)
            return False
        self.bootstrap = blob
        if self.router_token is None:
            self.router_token = blob.credential.router_token
        self.poll_interval = blob.poll_interval_s
        LOG.info(
            'router %s: polling %s every %ss',
            blob.router_id,
            blob.endpoint,
            self.poll_interval,
        )
        console.say(
            f'syncing with {blob.endpoint}{blob.sync_path} '
            f'every {self.poll_interval}s'
        )
        return True

    def _report(self) -> schema.SyncRequest:
        errors = list(self.errors)
        try:
            observed = self.backend.observe()
        except Exception as e:
            observed = schema.Observed()
            errors.append(f'cannot observe interfaces: {e}')
            LOG.exception('cannot observe interfaces')
        return schema.SyncRequest(
            agent_version=self.agent_version,
            boot_id=self.boot_id,
            uptime_s=max(0, int(self._clock() - self.started_at)),
            applied_generation=self.applied_generation,
            state=self.appliance_state,
            errors=errors,
            observed=observed,
        )

    def _apply(self, document: schema.ConfigDocument) -> None:
        try:
            self.backend.apply(document)
        except base.BackendError as e:
            self.appliance_state = ApplianceState.ERROR
            self.errors = list(e.errors)
            self._log_failure(
                f'generation {document.generation} not applied', e
            )
            return
        except Exception as e:
            self.appliance_state = ApplianceState.ERROR
            self.errors = [
                f'cannot apply generation {document.generation}: {e}'
            ]
            LOG.exception(
                'router %s: cannot apply generation %d',
                document.router_id,
                document.generation,
            )
            console.say(f'cannot apply generation {document.generation}: {e}')
            return
        self.applied_generation = document.generation
        self.config = document
        self.appliance_state = ApplianceState.CONVERGED
        self.errors = []
        LOG.info(
            'router %s: generation %d applied',
            document.router_id,
            document.generation,
        )
        console.say(f'generation {document.generation} applied')

    def _take_credential(self, answer: schema.SyncResponse) -> None:
        """Persist a rotated token atomically, then start using it.

        Never the other way round: a crash between using the new token and
        writing it to disk would leave the agent holding nothing the server
        knows.
        """
        if answer.credential is None:
            return
        token = answer.credential.router_token
        rotated = state.State(
            applied_generation=self.applied_generation,
            config=self.config,
            router_token=token,
        )
        try:
            state.save(rotated, self.state_path)
        except OSError as e:
            self._log_failure(
                'cannot persist the rotated credential, keeping the old one',
                e,
            )
            return
        self._saved = rotated
        self.router_token = token
        if self.client is not None:
            self.client.rotate(token)
        LOG.info('router %s: credential rotated', self.bootstrap.router_id)
        console.say('credential rotated and persisted')

    def _on_auth_error(self, error: Exception) -> float:
        self._log_failure('the control plane refused our credential', error)
        console.say('sync rejected, re-reading the config drive')
        try:
            blob = self._bootstrap_loader()
        except Exception as e:
            self._log_failure('cannot re-read the bootstrap blob', e)
        else:
            if blob.credential.router_token != self.router_token:
                self.router_token = blob.credential.router_token
                LOG.warning(
                    'router %s: falling back to the config drive token',
                    blob.router_id,
                )
        # the endpoint or the credential may have changed
        self.client = None
        self.bootstrap = None
        return self._backoff(base=self.poll_interval)

    def _save(self) -> None:
        current = state.State(
            applied_generation=self.applied_generation,
            config=self.config,
            router_token=self.router_token,
        )
        if current == self._saved:
            return
        try:
            state.save(current, self.state_path)
        except OSError as e:
            self._log_failure('cannot persist state', e)
        else:
            self._saved = current

    def _backoff(self, base: int = sync_client.BACKOFF_BASE_S) -> float:
        delay = sync_client.backoff_delay(
            self._attempt, base=base, jitter=self._jitter
        )
        self._attempt += 1
        return delay

    @staticmethod
    def _log_failure(what: str, error: Exception) -> None:
        LOG.error('%s: %s', what, error)
        console.say(f'{what}: {error}')


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s %(message)s',
    )
    Agent().run()
    return 0
