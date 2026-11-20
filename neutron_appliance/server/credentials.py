# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry

"""Per-router tokens: generate, hash, compare.

The per-router token is the real authorisation of the sync endpoint
(docs/protocol.md section 2). Three rules hold this module together:

  - only the hash is ever stored, so a database dump does not hand an
    attacker every appliance's credential
  - the comparison is constant time, so the hash cannot be guessed one byte
    at a time
  - the clear text exists exactly twice, in the answer that carries it and
    on the agent's disk. It is never logged

Plain SHA-256 and not a KDF on purpose: the token is 256 bits of output
from `secrets`, so there is no low-entropy input for a slow hash to defend.

Pure standard library, so that the rules above can be tested without
neutron installed.
"""

import hashlib
import hmac
import secrets


# 32 bytes from the system CSPRNG, URL-safe base64 encoded. Comfortably
# beyond brute force, and short enough to fit a header without wrapping.
TOKEN_BYTES = 32

# Length of the hex-encoded digest, which is what the database column
# holds. Named so that the model and the migration can agree with this
# module rather than with a literal.
HASH_SIZE = 64


class CredentialMismatch(Exception):
    """The token presented does not belong to this router.

    Deliberately says nothing about which of the two stored hashes failed,
    or whether the router has a credential at all.
    """

    def __init__(self, router_id: str):
        self.router_id = router_id
        super().__init__(f'router {router_id}: invalid appliance credential')


def generate() -> str:
    """A fresh per-router token. The only place clear text is created."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    """The hex digest stored for a token."""
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def matches(token, digest) -> bool:
    """True if `token` hashes to `digest`, compared in constant time.

    A missing token or a router with no credential yet is a mismatch: this
    fails closed, which matters because `credential_hash` is NULL between
    the moment a router is recorded and the moment its appliance is built.
    """
    if not token or not digest:
        return False
    return hmac.compare_digest(hash_token(token), digest)
