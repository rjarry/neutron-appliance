# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry

"""Render the appliance image provenance manifest.

Run inside the image at the end of the build. Everything that goes into the
manifest is either a build input or a package version: no timestamp, host
name or build path, so that two builds from the same inputs produce the same
bytes.
"""

from __future__ import annotations

from importlib import metadata
import json
import os
import subprocess
import sys


SCHEMA_VERSION = 1

OS_RELEASE_PATH = '/etc/os-release'

RPM_QUERY = '%{NAME} %{EPOCHNUM}:%{VERSION}-%{RELEASE}.%{ARCH}'


def parse_os_release(text):
    """Turn /etc/os-release into a dictionary of key/value pairs."""
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
            value = value[1:-1]
        values[key.strip()] = value
    return values


def read_rpm_packages():
    """One sorted line per installed RPM, with its full version."""
    output = subprocess.run(
        ['rpm', '-qa', '--qf', RPM_QUERY + '\n'],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return sorted(line for line in output.splitlines() if line)


def read_python_packages():
    """One sorted name==version line per installed Python distribution."""
    packages = set()
    for distribution in metadata.distributions():
        name = distribution.metadata['Name']
        if not name:
            continue
        name = name.lower().replace('_', '-')
        packages.add(f'{name}=={distribution.version}')
    return sorted(packages)


def python_version():
    """The interpreter version, without the release level."""
    return '.'.join(str(part) for part in sys.version_info[:3])


def build_manifest(
    *,
    agent_version,
    agent_commit,
    base_element,
    base_release,
    arch,
    build_command,
    os_release,
    rpm_packages,
    python,
    python_packages,
):
    """The manifest as a dictionary, with every sequence sorted."""
    return {
        'schema_version': SCHEMA_VERSION,
        'agent': {
            'version': agent_version,
            'commit': agent_commit,
        },
        'base': {
            'element': base_element,
            'release': base_release,
            'arch': arch,
            'os_release': parse_os_release(os_release),
        },
        'build': {
            'command': build_command,
        },
        'packages': {
            'rpm': sorted(rpm_packages),
        },
        'python': {
            'version': python,
            'packages': sorted(python_packages),
        },
    }


def render(**kwargs):
    """The manifest as deterministic JSON."""
    return (
        json.dumps(build_manifest(**kwargs), indent=2, sort_keys=True) + '\n'
    )


def main(argv):
    if len(argv) != 2:
        print(f'usage: {argv[0]} OUTPUT', file=sys.stderr)
        return 2

    with open(OS_RELEASE_PATH, encoding='utf-8') as stream:
        os_release = stream.read()

    text = render(
        agent_version=os.environ.get(
            'DIB_NEUTRON_APPLIANCE_VERSION', 'unknown'
        ),
        agent_commit=os.environ.get('DIB_NEUTRON_APPLIANCE_COMMIT', 'unknown'),
        base_element=os.environ.get(
            'DIB_NEUTRON_APPLIANCE_BASE_ELEMENT', 'unknown'
        ),
        base_release=os.environ.get('DIB_NEUTRON_APPLIANCE_BASE_RELEASE')
        or os.environ.get('DIB_RELEASE', 'unknown'),
        arch=os.environ.get('DIB_NEUTRON_APPLIANCE_ARCH')
        or os.environ.get('ARCH', 'unknown'),
        build_command=os.environ.get(
            'DIB_NEUTRON_APPLIANCE_BUILD_COMMAND', 'unknown'
        ),
        os_release=os_release,
        rpm_packages=read_rpm_packages(),
        python=python_version(),
        python_packages=read_python_packages(),
    )

    with open(argv[1], 'w', encoding='utf-8') as stream:
        stream.write(text)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
