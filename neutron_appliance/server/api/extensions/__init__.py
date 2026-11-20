# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry

"""Directory scanned by neutron's extension loader.

`api_extensions_path` in neutron.conf must point at this directory. The
loader reads every `*.py` in it that does not start with an underscore,
executes it by path, and looks for a class named after the file, so
`appliance.py` must define `Appliance` and nothing here may be renamed
without renaming the other.

Printing the path to put in the configuration:

    python -c 'import neutron_appliance.server.api.extensions as e
    print(e.__path__[0])'
"""
