# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry

"""Per-router token generation, hashing and comparison.

Pure standard library, so these run everywhere, which is the point: the
rules they check are the ones a reviewer cannot verify by reading, and
they must not be skipped on a workstation that has no neutron.
"""

from neutron_appliance.server import credentials


def test_every_token_is_different():
    tokens = {credentials.generate() for _ in range(100)}
    assert len(tokens) == 100


def test_a_token_carries_enough_entropy():
    # base64 of 32 bytes. Short enough for a header, far beyond brute
    # force, and a regression here would be invisible otherwise
    assert len(credentials.generate()) >= 43


def test_hashing_is_stable_and_fits_the_column():
    token = credentials.generate()
    digest = credentials.hash_token(token)
    assert digest == credentials.hash_token(token)
    assert len(digest) == credentials.HASH_SIZE
    assert token not in digest


def test_a_token_matches_its_own_hash():
    token = credentials.generate()
    assert credentials.matches(token, credentials.hash_token(token))


def test_another_token_does_not_match():
    digest = credentials.hash_token(credentials.generate())
    assert not credentials.matches(credentials.generate(), digest)


def test_a_missing_token_or_hash_fails_closed():
    token = credentials.generate()
    # credential_hash is NULL between recording a router and building its
    # appliance; an empty token must not walk through that window
    assert not credentials.matches(token, None)
    assert not credentials.matches(token, '')
    assert not credentials.matches(None, credentials.hash_token(token))
    assert not credentials.matches('', credentials.hash_token(token))
    assert not credentials.matches(None, None)


def test_the_mismatch_names_the_router_and_nothing_else():
    token = credentials.generate()
    error = credentials.CredentialMismatch('router-1')
    assert error.router_id == 'router-1'
    assert token not in str(error)
    # which of the two stored hashes failed is not the caller's business
    assert 'previous' not in str(error)
