# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry

"""The on-disk cache: round trip, refusals, and atomic replacement.

A truncated cache read at boot is the one failure that costs forwarding, so
the write path is tested for what it leaves behind when it fails halfway.
"""

import json
import os
import stat

import pytest

from neutron_appliance.agent import state
from neutron_appliance.common import schema


ROUTER_ID = '7f0e2c3a-1111-4000-8000-000000000001'


def document(generation=3):
    return schema.ConfigDocument(router_id=ROUTER_ID, generation=generation)


def test_an_absent_file_is_an_empty_state(tmp_path):
    assert state.load(str(tmp_path / 'state.json')) == state.State()


def test_a_saved_state_round_trips(tmp_path):
    path = str(tmp_path / 'state.json')
    saved = state.State(
        applied_generation=3, config=document(3), router_token='secret'
    )
    state.save(saved, path)
    assert state.load(path) == saved
    assert 'secret' not in repr(saved)


def test_an_empty_state_round_trips(tmp_path):
    path = str(tmp_path / 'state.json')
    state.save(state.State(), path)
    assert state.load(path) == state.State()


def test_the_token_is_written_in_clear_to_disk(tmp_path):
    path = tmp_path / 'state.json'
    state.save(state.State(router_token='secret'), str(path))
    stored = json.loads(path.read_text())
    assert stored['router_token'] == 'secret'


def test_the_file_is_only_readable_by_its_owner(tmp_path):
    path = tmp_path / 'state.json'
    state.save(state.State(router_token='secret'), str(path))
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600


def test_no_temporary_file_is_left_behind(tmp_path):
    state.save(state.State(), str(tmp_path / 'state.json'))
    assert sorted(p.name for p in tmp_path.iterdir()) == ['state.json']


def test_a_failed_replace_leaves_the_previous_state_intact(
    tmp_path, monkeypatch
):
    path = tmp_path / 'state.json'
    state.save(state.State(applied_generation=1), str(path))
    before = path.read_text()

    def fail(*args, **kwargs):
        raise OSError('disk full')

    monkeypatch.setattr(state.os, 'replace', fail)
    with pytest.raises(OSError, match='disk full'):
        state.save(state.State(applied_generation=2), str(path))
    assert path.read_text() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ['state.json']


def test_invalid_json_is_refused(tmp_path):
    path = tmp_path / 'state.json'
    path.write_text('{not json')
    with pytest.raises(state.StateError, match='invalid state file'):
        state.load(str(path))


def test_a_path_that_cannot_be_read_is_named(tmp_path):
    with pytest.raises(state.StateError, match='cannot read'):
        state.load(str(tmp_path))


@pytest.mark.parametrize('data', [None, [], 'string', 42])
def test_a_state_that_is_not_an_object_is_refused(data):
    with pytest.raises(state.StateError, match='not a JSON object'):
        state.State.from_dict(data)


@pytest.mark.parametrize('generation', [True, '1', 1.5, None])
def test_a_generation_that_is_not_an_integer_is_refused(generation):
    with pytest.raises(state.StateError, match='not an integer'):
        state.State.from_dict({'applied_generation': generation})


def test_a_negative_generation_is_refused():
    with pytest.raises(state.StateError, match='negative'):
        state.State.from_dict({'applied_generation': -1})


def test_a_token_that_is_not_a_string_is_refused():
    with pytest.raises(state.StateError, match='not a string'):
        state.State.from_dict({'router_token': 123})


def test_an_invalid_cached_document_is_refused():
    with pytest.raises(state.StateError, match='invalid cached config'):
        state.State.from_dict({'config': {'generation': 'three'}})


def test_none_fields_are_omitted():
    assert state.State().to_dict() == {'applied_generation': 0}


def test_missing_optional_fields_are_defaulted():
    parsed = state.State.from_dict({'applied_generation': 4})
    assert parsed == state.State(applied_generation=4)


def test_a_ca_bundle_is_written_only_when_present(tmp_path):
    path = str(tmp_path / 'ca.pem')
    assert state.ensure_ca(None, path) is None
    assert not os.path.exists(path)
    assert state.ensure_ca('PEM', path) == path
    assert (tmp_path / 'ca.pem').read_text() == 'PEM'
