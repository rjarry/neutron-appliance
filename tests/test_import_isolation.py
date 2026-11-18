# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry

"""The appliance image must not carry the Neutron server stack.

ruff banned-api catches a direct `import neutron` while you type it, but it
cannot see transitive imports. This does, by making the modules unimportable
and walking every module in the two packages that ship inside the VM.
"""

import builtins
import importlib
import pkgutil

import pytest

import neutron_appliance.agent
import neutron_appliance.common


BANNED = ('neutron', 'neutron_lib')

GUEST_PACKAGES = (neutron_appliance.common, neutron_appliance.agent)


def _guest_modules():
    for package in GUEST_PACKAGES:
        yield package.__name__
        for mod in pkgutil.walk_packages(
            package.__path__, package.__name__ + '.'
        ):
            yield mod.name


@pytest.fixture
def no_neutron(monkeypatch):
    """Make neutron unimportable for the duration of a test."""
    real_import = builtins.__import__

    def guard(name, *args, **kwargs):
        root = name.split('.')[0]
        if root in BANNED:
            raise ImportError(
                f'{name} must not be importable from the appliance image'
            )
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', guard)


@pytest.mark.parametrize('name', sorted(_guest_modules()))
def test_guest_module_imports_without_neutron(name, no_neutron):
    for mod in list(importlib.sys.modules):
        if mod.startswith(name):
            del importlib.sys.modules[mod]
    importlib.import_module(name)
