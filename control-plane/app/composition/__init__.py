# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
How the control plane application is put together.

`create_app` in `app/app.py` assembles the application from the registries in
this package instead of from inline statements, so that a second distribution
can add to the assembly without editing it:

* `services`: the long-lived service objects every router is built from.
* `routers`: the ordered list of routers the application mounts.
"""
