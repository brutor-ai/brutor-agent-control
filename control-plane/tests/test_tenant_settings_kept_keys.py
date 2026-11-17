# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Keys of a tenant's settings that have a route of their own are left as
stored by the general tenant update.

The point is `TENANT_SETTINGS_KEPT_KEYS` (`app/composition/hooks.py`). The
Community Edition registers nothing there: every key of `settings` is the
administrator's to write.
"""

from __future__ import annotations

from app.api.v1.endpoints.admin import tenant_settings
from app.composition import hooks


def test_with_nothing_registered_every_key_is_written(monkeypatch):
    monkeypatch.setattr(hooks, "HOOKS", hooks.HookRegistry())
    stored = {"a": 1, "b": 2}
    assert tenant_settings._with_kept_settings(stored, {"a": 9}) == {"a": 9}
    assert tenant_settings._with_kept_settings(stored, {}) == {}


def test_a_kept_key_is_neither_set_nor_dropped(monkeypatch):
    registry = hooks.HookRegistry([
        hooks.HookSpec("one", hooks.TENANT_SETTINGS_KEPT_KEYS, lambda: ("kept", "absent")),
        hooks.HookSpec("two", hooks.TENANT_SETTINGS_KEPT_KEYS, lambda: ["also"]),
    ])
    monkeypatch.setattr(hooks, "HOOKS", registry)
    stored = {"kept": [1], "also": "x", "free": "old"}
    assert tenant_settings._with_kept_settings(stored, {"kept": [2], "absent": True, "free": "new"}) == {
        "kept": [1], "also": "x", "free": "new",
    }
    assert tenant_settings._with_kept_settings(stored, {}) == {"kept": [1], "also": "x"}
    assert tenant_settings._with_kept_settings(None, {"kept": 1, "free": 2}) == {"free": 2}
    # Something that is not a mapping is the model's to refuse, not this function's.
    assert tenant_settings._with_kept_settings(stored, None) is None
