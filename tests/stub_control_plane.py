# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry

"""A stand-in control plane for the agent tests.

It speaks the two requests the agent makes: keystone password auth and the
appliance sync endpoint, with the same header, the same rotation rule and the
same "config only when the agent is behind" rule as the real server. No
neutron, no database, no network beyond loopback.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import uuid

from neutron_appliance.common import protocol, schema


# do_POST is the name http.server looks up, and the handler is a closure over
# the stub that owns the state.
# pylint: disable=attribute-defined-outside-init,invalid-name,protected-access


ROUTER_ID = '7f0e2c3a-1111-4000-8000-000000000001'
SERVICE_USER = 'neutron-appliance'
SERVICE_PASSWORD = 'service-secret'
INITIAL_TOKEN = 'bootstrap-token'

# Far enough in the future that keystoneauth never refreshes mid-test.
EXPIRES_AT = '2030-01-01T00:00:00.000000Z'
ISSUED_AT = '2029-01-01T00:00:00.000000Z'


def bootstrap(
    endpoint: str,
    router_token: str = INITIAL_TOKEN,
    poll_interval_s: int = protocol.MIN_POLL_INTERVAL_S,
) -> schema.Bootstrap:
    """The blob the real server would put on the config drive."""
    return schema.Bootstrap(
        router_id=ROUTER_ID,
        endpoint=endpoint,
        sync_path=protocol.sync_path(ROUTER_ID),
        credential=schema.Credential(
            auth_url=f'{endpoint}/v3',
            username=SERVICE_USER,
            password=SERVICE_PASSWORD,
            project_name='service',
            router_token=router_token,
        ),
        poll_interval_s=poll_interval_s,
    )


class StubControlPlane:
    """One router, one token, one generation, on 127.0.0.1."""

    def __init__(self):
        self.generation = 1
        self.config = schema.ConfigDocument(
            router_id=ROUTER_ID, generation=self.generation
        )
        self.current_token = INITIAL_TOKEN
        self.previous_token = None
        self.boot_id = None
        self.reports = []
        self.auth_requests = 0
        # Set to an HTTP status to make the next sync calls fail.
        self.fail_status = None
        self._lock = threading.Lock()
        self._sync_path = protocol.sync_path(ROUTER_ID)
        self._server = ThreadingHTTPServer(('127.0.0.1', 0), self._handler())
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True
        )
        self._thread.start()

    @property
    def url(self) -> str:
        return f'http://127.0.0.1:{self._server.server_port}'

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def _handler(self):
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self._body = self.rfile.read(
                    int(self.headers.get('content-length', 0))
                )
                if self.path == '/v3/auth/tokens':
                    stub._answer_auth(self)
                elif self.path == stub._sync_path:
                    stub._answer_sync(self)
                else:
                    self._send(404, {'error': 'not found'})

            def _send(self, status, payload, headers=None):
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        return Handler

    def _answer_auth(self, handler):
        with self._lock:
            self.auth_requests += 1
        body = {
            'token': {
                'methods': ['password'],
                'expires_at': EXPIRES_AT,
                'issued_at': ISSUED_AT,
                'user': {
                    'id': 'user-1',
                    'name': SERVICE_USER,
                    'domain': {'id': 'default', 'name': 'Default'},
                },
                'project': {
                    'id': 'project-1',
                    'name': 'service',
                    'domain': {'id': 'default', 'name': 'Default'},
                },
                'roles': [],
                'catalog': [],
            }
        }
        handler._send(201, body, {'X-Subject-Token': 'keystone-token'})

    def _answer_sync(self, handler):
        token = handler.headers.get(protocol.APPLIANCE_TOKEN_HEADER)
        with self._lock:
            if self.fail_status is not None:
                handler._send(self.fail_status, {'error': 'unavailable'})
                return
            accepted = {self.current_token}
            if self.previous_token is not None:
                accepted.add(self.previous_token)
            if token not in accepted:
                handler._send(401, {'error': 'credential mismatch'})
                return
            report = schema.SyncRequest.from_dict(json.loads(handler._body))
            self.reports.append(report)
            rotated = None
            if self.boot_id != report.boot_id:
                self.previous_token = self.current_token
                self.current_token = uuid.uuid4().hex
                self.boot_id = report.boot_id
                rotated = self.current_token
            elif token == self.previous_token:
                self.previous_token = self.current_token
                self.current_token = uuid.uuid4().hex
                rotated = self.current_token
            elif token == self.current_token:
                self.previous_token = None
            config = None
            if report.applied_generation != self.generation:
                config = self.config
            answer = schema.SyncResponse(
                generation=self.generation,
                poll_interval_s=protocol.MIN_POLL_INTERVAL_S,
                config=config,
                credential=(
                    schema.CredentialUpdate(router_token=rotated)
                    if rotated
                    else None
                ),
            )
            handler._send(200, answer.to_dict())
