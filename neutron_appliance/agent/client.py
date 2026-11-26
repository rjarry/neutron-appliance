# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry

"""Sync client: report status and receive desired config in one request.

Two credentials, two jobs (docs/protocol.md section 2): keystoneauth
acquires the shared service-user token and presents it like any other API
client, so the request traverses neutron's normal pipeline; the per-router
token is checked by the appliance extension and decides whose configuration
comes back.

Every failure is translated into a category the loop can act on. None of
them is a reason to touch the applied configuration.
"""

from __future__ import annotations

import random

from keystoneauth1 import exceptions, identity, session

from neutron_appliance.common import protocol, schema


# First retry delay. Doubles per attempt, capped by MAX_BACKOFF_S.
BACKOFF_BASE_S = 1

# A sync that hangs forever would keep the agent from noticing a dead
# control plane. One poll interval is 30s by default; two minutes is roomy.
REQUEST_TIMEOUT_S = 120

# Response bodies are shown in one log line on failure. The document is not
# secret today, but truncating keeps an HTML error page out of the journal.
_DETAIL_LIMIT = 200


class SyncError(Exception):
    """A sync attempt failed. Back off, keep the applied configuration."""


class AuthError(SyncError):
    """401 or 403: the credential is wrong, or the role is missing."""


class NotFoundError(SyncError):
    """404: the server does not know this router. Do not self-destruct."""


class ServerError(SyncError):
    """5xx: the control plane is unwell."""


class NetworkError(SyncError):
    """No answer: DNS, TLS, routing, timeout, or clock skew."""


def backoff_delay(
    attempt: int,
    base: int = BACKOFF_BASE_S,
    cap: int = protocol.MAX_BACKOFF_S,
    jitter=random.random,
) -> float:
    """Seconds to wait before retry `attempt`, with full jitter.

    `attempt` starts at zero. The ceiling doubles up to `cap`, and the
    sleep is a random fraction of it: the point is not the exact delay but
    that a fleet of appliances that lost the same control plane does not
    return in lockstep.
    """
    ceiling = min(cap, base * 2 ** min(attempt, 16))
    return ceiling * jitter()


def _detail(response) -> str:
    body = ' '.join(response.text.split())
    if len(body) > _DETAIL_LIMIT:
        body = body[:_DETAIL_LIMIT] + '...'
    detail = f'HTTP {response.status_code}'
    if body:
        detail += f': {body}'
    return detail


class Client:
    """One router's sync channel to the control plane."""

    def __init__(
        self,
        bootstrap: schema.Bootstrap,
        ca_path: str | None = None,
        router_token: str | None = None,
        timeout: float = REQUEST_TIMEOUT_S,
    ):
        credential = bootstrap.credential
        auth = identity.v3.Password(
            auth_url=credential.auth_url,
            username=credential.username,
            password=credential.password,
            project_name=credential.project_name,
            user_domain_name=credential.user_domain_name,
            project_domain_name=credential.project_domain_name,
        )
        self._session = session.Session(
            auth=auth,
            verify=ca_path or True,
            timeout=timeout,
            app_name='neutron-appliance-agent',
        )
        self._url = bootstrap.endpoint.rstrip('/') + bootstrap.sync_path
        self._router_token = router_token or credential.router_token

    def rotate(self, router_token: str) -> None:
        """Use a new per-router token from the next call on."""
        self._router_token = router_token

    def sync(self, report: schema.SyncRequest) -> schema.SyncResponse:
        """Send one report and return the server's answer."""
        headers = {protocol.APPLIANCE_TOKEN_HEADER: self._router_token}
        try:
            response = self._session.post(
                self._url,
                json=report.to_dict(),
                headers=headers,
                raise_exc=False,
            )
        except exceptions.AuthorizationFailure as e:
            raise AuthError(f'the service user was refused: {e}') from e
        except (
            exceptions.http.Unauthorized,
            exceptions.http.Forbidden,
        ) as e:
            raise AuthError(f'the service user was refused: {e}') from e
        except exceptions.ClientException as e:
            raise NetworkError(str(e)) from e

        if response.status_code in (401, 403):
            raise AuthError(_detail(response))
        if response.status_code == 404:
            raise NotFoundError(_detail(response))
        if response.status_code >= 500:
            raise ServerError(_detail(response))
        if response.status_code != 200:
            raise SyncError(_detail(response))
        try:
            return schema.SyncResponse.from_dict(response.json())
        except ValueError as e:
            raise SyncError(f'malformed sync response: {e}') from e
        except protocol.ProtocolError as e:
            raise SyncError(f'malformed sync response: {e}') from e
