# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry

"""Console breadcrumbs must never be a reason for the agent to stop."""

import os

from neutron_appliance.agent import console


def test_a_line_carries_the_prefix_and_the_message(tmp_path, monkeypatch):
    path = tmp_path / 'console'
    path.write_text('')
    monkeypatch.setattr(console, 'CONSOLE', str(path))
    console.say('generation 3 applied')
    line = path.read_text()
    assert line.endswith('\n')
    assert f'{console.PREFIX}: generation 3 applied' in line


def test_a_console_that_cannot_be_opened_is_ignored(monkeypatch):
    def fail(*args, **kwargs):
        raise OSError('no console')

    monkeypatch.setattr(console.os, 'open', fail)
    assert console.say('anything') is None


def test_a_write_that_fails_still_closes_the_descriptor(tmp_path, monkeypatch):
    path = tmp_path / 'console'
    path.write_text('')
    monkeypatch.setattr(console, 'CONSOLE', str(path))
    closed = []

    def fail_write(fd, data):
        raise OSError('tty gone')

    real_close = os.close

    def close(fd):
        closed.append(fd)
        real_close(fd)

    monkeypatch.setattr(console.os, 'write', fail_write)
    monkeypatch.setattr(console.os, 'close', close)
    assert console.say('anything') is None
    assert len(closed) == 1
