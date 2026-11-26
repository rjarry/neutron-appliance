# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry

"""The image manifest is the reproducibility check for M6.

The manifest is generated inside the image, but everything that decides its
content is a pure function: that is what lets these tests prove two builds
from the same inputs produce the same bytes without running
diskimage-builder.
"""

import importlib.util
import json
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / 'image' / 'manifest.py'

_spec = importlib.util.spec_from_file_location('image_manifest', MODULE_PATH)
assert _spec is not None and _spec.loader is not None
manifest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(manifest)


OS_RELEASE = """\
NAME="CentOS Stream"
VERSION="9"
ID="centos"
ID_LIKE="rhel fedora"
PRETTY_NAME="CentOS Stream 9"
# this is a comment
"""

COMMIT = '0123456789abcdef0123456789abcdef01234567'

BUILD_COMMAND = (
    'disk-image-create -a amd64 -o build/neutron-appliance -t qcow2 '
    'centos-minimal vm neutron-appliance'
)


def inputs():
    return {
        'agent_version': '0.0.1',
        'agent_commit': COMMIT,
        'base_element': 'centos-minimal',
        'base_release': '9-stream',
        'arch': 'amd64',
        'build_command': BUILD_COMMAND,
        'os_release': OS_RELEASE,
        'rpm_packages': [
            'zlib 0:1.2.11-40.el9.x86_64',
            'bash 0:5.1.8-6.el9.x86_64',
        ],
        'python': '3.9.21',
        'python_packages': [
            'oslo.log==6.0.0',
            'keystoneauth1==5.8.0',
        ],
    }


def test_two_builds_with_the_same_inputs_match():
    assert manifest.render(**inputs()) == manifest.render(**inputs())


def test_input_order_does_not_change_the_manifest():
    first = manifest.render(**inputs())
    reordered = inputs()
    reordered['rpm_packages'] = list(reversed(reordered['rpm_packages']))
    reordered['python_packages'] = list(reversed(reordered['python_packages']))
    assert manifest.render(**reordered) == first


def test_the_manifest_records_what_m6_asks_for():
    data = json.loads(manifest.render(**inputs()))

    assert data['agent'] == {'version': '0.0.1', 'commit': COMMIT}
    assert data['base']['element'] == 'centos-minimal'
    assert data['base']['release'] == '9-stream'
    assert data['base']['arch'] == 'amd64'
    assert data['base']['os_release']['PRETTY_NAME'] == 'CentOS Stream 9'
    assert data['build']['command'] == BUILD_COMMAND
    assert data['packages']['rpm'] == sorted(inputs()['rpm_packages'])
    assert data['python']['version'] == '3.9.21'
    assert data['python']['packages'] == sorted(inputs()['python_packages'])
    assert 'timestamp' not in data


def test_os_release_comments_and_quotes_are_handled():
    parsed = manifest.parse_os_release(OS_RELEASE)
    assert parsed['ID'] == 'centos'
    assert parsed['PRETTY_NAME'] == 'CentOS Stream 9'
    assert '#' not in parsed
