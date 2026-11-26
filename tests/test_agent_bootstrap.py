# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry

"""Config drive discovery and parsing.

The mount path is monkeypatched to a temporary directory everywhere: no test
may require a real config drive, and none may mount anything.
"""

import json
import os
import subprocess

import pytest

from neutron_appliance.agent import bootstrap


# the mount helper is where the branch lives, and there is no public caller
# worth inventing just to avoid this
# pylint: disable=protected-access


ROUTER_ID = '7f0e2c3a-1111-4000-8000-000000000001'

BLOB = {
    'schema_version': 1,
    'router_id': ROUTER_ID,
    'endpoint': 'https://neutron.example.com:9696',
    'sync_path': f'/v2.0/appliance/routers/{ROUTER_ID}/sync',
    'poll_interval_s': 30,
    'credential': {
        'auth_url': 'https://keystone.example.com:5000/v3',
        'username': 'neutron-appliance',
        'password': 'secret',
        'project_name': 'service',
        'router_token': 'router-secret',
    },
}


def test_a_valid_blob_parses():
    parsed = bootstrap.loads(json.dumps(BLOB))
    assert parsed.router_id == ROUTER_ID
    assert parsed.credential.router_token == 'router-secret'
    assert parsed.credential.user_domain_name == 'Default'


def test_invalid_json_is_named():
    with pytest.raises(bootstrap.BootstrapError, match='not valid JSON'):
        bootstrap.loads('{not json')


def test_a_validation_failure_keeps_the_reason():
    broken = dict(BLOB, router_id='not-a-uuid')
    with pytest.raises(bootstrap.BootstrapError, match='invalid bootstrap'):
        bootstrap.loads(json.dumps(broken))


def test_an_unknown_schema_version_is_refused():
    broken = dict(BLOB, schema_version=99)
    with pytest.raises(bootstrap.BootstrapError, match='schema_version'):
        bootstrap.loads(json.dumps(broken))


def test_load_reads_the_given_path(tmp_path):
    path = tmp_path / 'user_data'
    path.write_text(json.dumps(BLOB))
    assert bootstrap.load(str(path)).router_id == ROUTER_ID


def test_read_reports_the_path_it_could_not_read(tmp_path):
    missing = tmp_path / 'nope'
    with pytest.raises(bootstrap.BootstrapError, match=str(missing)):
        bootstrap.load(str(missing))


def test_find_prefers_cloud_inits_copy(tmp_path, monkeypatch):
    copy = tmp_path / 'user-data.txt'
    copy.write_text('{}')
    monkeypatch.setattr(bootstrap, 'CLOUD_INIT_PATHS', (str(copy),))
    monkeypatch.setattr(
        bootstrap,
        '_mount_config_drive',
        lambda: pytest.fail('should not mount when cloud-init left a copy'),
    )
    assert bootstrap.find() == str(copy)


def test_find_falls_back_to_the_config_drive(tmp_path, monkeypatch):
    monkeypatch.setattr(bootstrap, 'CLOUD_INIT_PATHS', ())
    monkeypatch.setattr(
        bootstrap, '_mount_config_drive', lambda: str(tmp_path / 'mounted')
    )
    assert bootstrap.find() == str(tmp_path / 'mounted')


def test_an_already_mounted_drive_is_reused(tmp_path, monkeypatch):
    monkeypatch.setattr(bootstrap, 'MOUNT_POINT', str(tmp_path))
    user_data = tmp_path / bootstrap.USER_DATA
    user_data.parent.mkdir(parents=True)
    user_data.write_text('{}')
    assert bootstrap._mount_config_drive() == str(user_data)


def test_a_missing_device_is_reported_as_such(tmp_path, monkeypatch):
    monkeypatch.setattr(bootstrap, 'MOUNT_POINT', str(tmp_path / 'mnt'))
    monkeypatch.setattr(bootstrap, 'DEVICE', str(tmp_path / 'config-2'))
    with pytest.raises(bootstrap.BootstrapError, match='no config drive'):
        bootstrap._mount_config_drive()


def _fake_run(exc):
    def run(*args, **kwargs):
        raise exc

    return run


def test_a_failed_mount_keeps_mounts_stderr(tmp_path, monkeypatch):
    device = tmp_path / 'config-2'
    device.write_text('')
    monkeypatch.setattr(bootstrap, 'MOUNT_POINT', str(tmp_path / 'mnt'))
    monkeypatch.setattr(bootstrap, 'DEVICE', str(device))
    error = subprocess.CalledProcessError(
        32, ['mount'], stderr='wrong fs type\n'
    )
    monkeypatch.setattr(bootstrap.subprocess, 'run', _fake_run(error))
    with pytest.raises(bootstrap.BootstrapError, match='wrong fs type'):
        bootstrap._mount_config_drive()


def test_a_mount_that_cannot_exec_is_reported(tmp_path, monkeypatch):
    device = tmp_path / 'config-2'
    device.write_text('')
    monkeypatch.setattr(bootstrap, 'MOUNT_POINT', str(tmp_path / 'mnt'))
    monkeypatch.setattr(bootstrap, 'DEVICE', str(device))
    monkeypatch.setattr(
        bootstrap.subprocess,
        'run',
        _fake_run(OSError('no mount binary')),
    )
    with pytest.raises(bootstrap.BootstrapError, match='no mount binary'):
        bootstrap._mount_config_drive()


def test_a_successful_mount_returns_the_user_data_path(tmp_path, monkeypatch):
    device = tmp_path / 'config-2'
    device.write_text('')
    mount_point = tmp_path / 'mnt'
    monkeypatch.setattr(bootstrap, 'MOUNT_POINT', str(mount_point))
    monkeypatch.setattr(bootstrap, 'DEVICE', str(device))
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        user_data = mount_point / bootstrap.USER_DATA
        user_data.parent.mkdir(parents=True)
        user_data.write_text('{}')

    monkeypatch.setattr(bootstrap.subprocess, 'run', run)
    assert bootstrap._mount_config_drive() == str(
        mount_point / bootstrap.USER_DATA
    )
    assert calls == [
        (
            ['mount', '-o', 'ro', str(device), str(mount_point)],
            {'check': True, 'capture_output': True, 'text': True},
        )
    ]
    assert os.path.isdir(mount_point)
