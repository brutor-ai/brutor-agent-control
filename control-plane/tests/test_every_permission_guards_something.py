# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Every registered permission guards something.

A permission that no route asks for and no code checks can be granted,
shown in a role and counted, and decides nothing: an administrator who
gives or withholds it has changed nothing. Each permission of the registry
is asked for by a route of the application under test, or is checked in
the code (`has_permission`), where a route decides by more than "may the
caller come in".
"""

from __future__ import annotations

import re
from pathlib import Path

from tests import app_factory
from tests.app_factory import create_app

REPO = Path(__file__).resolve().parents[1]
INLINE_CONSTANT = re.compile(r"has_permission\(\s*([A-Z][A-Z0-9_]+)\s*\)")
INLINE = re.compile(r"has_permission\(\s*(?:\w+\.)*(\w*Permission)\.([A-Z0-9_]+)(?:\.value)?\s*\)")


def _asked_for_by_a_route() -> set:
    def walk(routes):
        for route in routes:
            included = getattr(route, "original_router", None)
            if included is not None:
                yield from walk(included.routes)
            elif getattr(route, "dependant", None) is not None:
                yield route

    def required(dependant, found):
        found.update(getattr(dependant.call, "required_permissions", None) or ())
        for inner in dependant.dependencies:
            required(inner, found)
        return found

    asked = set()
    for route in walk(create_app().routes):
        asked |= required(route.dependant, set())
    return {getattr(value, "value", value) for value in asked}


def _checked_inline() -> set:
    """The permissions a `has_permission(...)` in the product names: as a
    member of a permission enum, or as a constant of the module that holds
    the permission's string."""
    members = {member.name: member.value for member in app_factory.every_permission()}
    values = set(members.values())
    checked = set()
    for root in ("app", "enterprise/brutor_enterprise"):
        for path in sorted((REPO / root).rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            for _enum, name in INLINE.findall(source):
                if name in members:
                    checked.add(members[name])
            for constant in INLINE_CONSTANT.findall(source):
                written = re.search(rf'^{constant} = "([a-z_:]+)"$', source, re.M)
                if written and written.group(1) in values:
                    checked.add(written.group(1))
    return checked


def test_every_permission_is_asked_for_by_a_route_or_checked_in_the_code():
    registered = {member.value for member in app_factory.every_permission()}
    assert len(registered) > 100
    asked, inline = _asked_for_by_a_route(), _checked_inline()
    assert len(asked) > 100
    assert sorted(registered - asked - inline) == []


def test_the_permission_that_is_only_checked_inline_is_the_one_for_bodies():
    """`proxy:logs:read_bodies` opens no route of its own: a reader of the
    log rows holds it or not, and the row is answered with or without its
    body."""
    asked, inline = _asked_for_by_a_route(), _checked_inline()
    assert "proxy:logs:read_bodies" in inline - asked
