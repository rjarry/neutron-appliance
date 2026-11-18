# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry

"""Sync client: report status and receive desired config in one request."""

# TODO: keystone token (service user) + X-Appliance-Token (per router)
# TODO: exponential backoff with jitter; never discard config on failure
