# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Shared AI Resource fields — version + authorship

Every AI Resource an operator can create (LLM model, MCP server, routing
group, provider, skill, agent card) should answer the same four questions
on its General tab: *who made this, when, who last touched it, and what
version is it?*

Before this module, the answers were scattered. `agent_skill` had all of
them; `agent_card` had version and `created_by` but no `updated_by`;
`llm_model` and `mcp_server` had neither. So "who changed this MCP server"
was a question the platform could not answer from the row — you had to go
to the audit log for something that belongs on the record itself.

## Version is not change history

The audit log already records every change; that is not what `version` is
for. `version` is an **operator-authored label** — "this is our 1.3 of the
refund skill" — that appears next to the name and gives a human a handle
to talk about. It defaults to `1.0` and is theirs to change.

It is deliberately *not* a content hash, an optimistic lock, or a
revision pointer. Those are different mechanisms for different jobs, and
conflating them into one column is how a field ends up meaning nothing.

## Authorship is system-set and read-only

`created_by` / `updated_by` are stamped by [`stamp_resource_authorship`]
on flush, from the admin bound to the current request. They are never
taken from the request body: a field that records *who did this* and can
be supplied by the caller records nothing at all.

Doing it on the flush hook rather than in each endpoint is the point.
There are dozens of write paths across these six models, and any one of
them that forgot to set `updated_by` would leave a silently stale value —
which is worse than a null, because it names the wrong person.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Dict, Optional

from sqlalchemy import Column, String, event, inspect
from sqlalchemy.orm.base import NO_VALUE
from sqlalchemy.orm import Session, declarative_mixin

# Default for a newly created resource. A string, not a number: operators
# write "1.0", "2.1.3", "2026-Q3" — semantics are theirs, not ours.
DEFAULT_RESOURCE_VERSION = "1.0"

#: The actor recorded for rows the platform creates itself — boot seeds,
#: provisioning, catalog imports.
#:
#: This is deliberately not a null. A null reads as "we don't know who",
#: which is the right answer for a row that predates authorship tracking
#: and the wrong one for a row the platform demonstrably created. And it is
#: not inventing an account either: `system` cannot log in, so an operator
#: reading the field learns something true and unambiguous — nobody typed
#: this in.
SYSTEM_ACTOR = "system"

# The admin performing the current request. Set by the admin dependency,
# read by the flush hook.
#
# ContextVars rather than thread-locals because the control plane is async:
# FastAPI can interleave requests on one thread, and a thread-local would
# attribute one operator's write to whoever happened to be sharing the
# thread. That is the kind of bug that only shows up under load and is then
# very hard to believe.
#
# There are two of them, and the reason is worth stating plainly because
# the obvious single-var design silently does nothing.
#
# FastAPI runs a *sync* dependency in a threadpool, and
# `run_in_threadpool` hands it a **copy** of the context. A `ContextVar.set`
# inside that copy dies with it — invisible to the endpoint, invisible to
# the flush. `get_current_admin` is sync, so binding the actor there wrote
# to a throwaway context on every single request and `updated_by` was never
# stamped by the listener at all. The only endpoints that recorded an
# editor were the handful assigning `updated_by` by hand.
#
# So the request actor lives in a **mutable slot** installed once per
# request, before any threadpool step. Copies of the context share the slot
# *object*, so mutating it is visible everywhere — while the slot itself
# is still per-request, which is what keeps two concurrent operators from
# being confused for each other.
_actor_slot: ContextVar[Optional[Dict[str, Optional[str]]]] = ContextVar(
    "brutor_actor_slot", default=None
)

# An explicitly scoped actor (`acting_as`). Kept separate from the request
# slot, and deliberately wins over it: provisioning that runs *inside* an
# admin request binds SYSTEM_ACTOR, and the starter resources it creates
# are seeded content rather than something that admin authored.
_current_actor: ContextVar[Optional[str]] = ContextVar(
    "brutor_current_actor", default=None
)


def open_actor_scope() -> None:
    """Install this request's actor slot.

    Must run in the request's own context — i.e. from ASGI middleware,
    before FastAPI resolves dependencies — or the slot lands in a copy and
    we are back to the bug this exists to fix.
    """
    _actor_slot.set({"actor": None})


def set_current_actor(username: Optional[str]) -> Any:
    """Bind the acting admin. Returns a reset token for `acting_as`.

    Writes into the request slot when one is open, which is what makes the
    value survive the threadpool boundary between a sync dependency and the
    endpoint that flushes.
    """
    slot = _actor_slot.get()
    if slot is not None:
        previous = slot["actor"]
        slot["actor"] = username
        return ("slot", previous)
    return ("var", _current_actor.set(username))


def reset_current_actor(token: Any) -> None:
    kind, value = token
    if kind == "slot":
        slot = _actor_slot.get()
        if slot is not None:
            slot["actor"] = value
    else:
        _current_actor.reset(value)


def get_current_actor() -> Optional[str]:
    # An explicit `acting_as` scope beats the ambient request actor.
    explicit = _current_actor.get()
    if explicit is not None:
        return explicit
    slot = _actor_slot.get()
    return slot["actor"] if slot else None



def actor_name(admin) -> Optional[str]:
    """The name to record in `created_by` / `updated_by`.

    The flush listener stamps a **username**, because that is what an
    operator reading an audit field can act on. A handful of endpoints
    assign the field by hand and used `admin.id` instead, so the same
    column held `admin` for most resources and `tenant-admin-4f5b…` for a
    few — two answers to one question, and the opaque one is the answer
    nobody can use.

    Falls back to the id only when there is no username, which beats
    recording nothing.
    """
    return getattr(admin, "username", None) or getattr(admin, "id", None)

@contextmanager
def acting_as(username: Optional[str]):
    """Scope an actor around a block — used by seeds and background jobs."""
    token = _current_actor.set(username)
    try:
        yield
    finally:
        _current_actor.reset(token)


@declarative_mixin
class ResourceVersionMixin:
    """An operator-authored version label.

    Applied only to resources whose definition the operator actually
    authors. Deliberately absent from:

      * `server_config` — its version is a property of the MCP server it
        configures, so carrying a second one here would invite the two to
        disagree, and the one on the config would be the wrong answer.
      * `agent_identity` — a credential, not an authored artifact. It has
        no releases to number.
    """

    version = Column(
        String(50),
        nullable=False,
        default=DEFAULT_RESOURCE_VERSION,
        server_default=DEFAULT_RESOURCE_VERSION,
        comment=(
            "Operator-authored version label (default '1.0'). A handle for "
            "humans, not a content hash or a revision pointer — change "
            "history lives in the audit log."
        ),
    )


@declarative_mixin
class ResourceAuthorshipMixin:
    """Who created and last modified this resource.

    Read-only to API clients and stamped automatically on flush. Paired
    with each model's existing `created_at` / `updated_at` columns, which
    already exist platform-wide and keep their `_at` naming.
    """

    created_by = Column(
        String(255),
        nullable=True,
        comment=(
            "Admin who created this resource. System-set on insert from "
            "the acting request; never accepted from a request body."
        ),
    )

    updated_by = Column(
        String(255),
        nullable=True,
        comment=(
            "Admin who last modified this resource. System-set on update; "
            "never accepted from a request body."
        ),
    )

    def authorship_dict(self) -> Dict[str, Any]:
        """The four General-tab fields, in one place.

        `created_at` / `updated_at` are declared per-model rather than in
        the mixin — they already existed everywhere with slightly
        different server defaults, and rewriting them would have been
        churn with no behavioural gain.
        """
        created_at = getattr(self, "created_at", None)
        updated_at = getattr(self, "updated_at", None)
        return {
            "created_by": self.created_by,
            "created_at": created_at.isoformat() if created_at else None,
            "updated_by": self.updated_by,
            "updated_at": updated_at.isoformat() if updated_at else None,
        }


def stamp_resource_authorship(session: Session, _flush_context, _instances) -> None:
    """Stamp authorship on every dirty AI Resource in this flush.

    Registered once against `Session` so it applies to every write path in
    the control plane — endpoints, services, seeds, background jobs —
    rather than depending on dozens of call sites each remembering.

    Seeds bind `SYSTEM_ACTOR` explicitly, so platform-created rows are
    attributable. With no actor bound at all (a migration, a test) the
    fields are left as they are: a null honestly says "we don't know who",
    which beats guessing.
    """
    actor = get_current_actor()
    if actor is None:
        return

    for obj in session.new:
        if isinstance(obj, ResourceAuthorshipMixin):
            if obj.created_by is None:
                obj.created_by = actor
            if obj.updated_by is None:
                obj.updated_by = actor

    for obj in session.dirty:
        if not isinstance(obj, ResourceAuthorshipMixin):
            continue
        # `session.dirty` is optimistic — it includes objects that were
        # touched but not actually changed. Stamping those would rewrite
        # `updated_by` on a no-op save and name the wrong person as the
        # last editor.
        state = inspect(obj)
        if not state.modified:
            continue
        # `history.has_changes()` is not enough: SQLAlchemy records a *set
        # event*, not a value change, so `obj.x = obj.x` reports as
        # changed. Saving a form without touching it is something
        # operators do constantly, and stamping those would name the wrong
        # person as the last editor.
        #
        # `committed_state` holds the pre-modification value for each
        # touched attribute, so comparing it against the current value
        # tells us whether anything actually moved. The `NO_VALUE`
        # sentinel means the attribute was never loaded — assigning None
        # to it is not a change, assigning a value is.
        #
        # Two things this must not do, both of which look harmless:
        #
        #   * Iterate `committed_state` directly. Reading an attribute can
        #     append to it, and mutating a dict mid-iteration raises
        #     RuntimeError — which, raised from `before_flush`, aborts the
        #     whole transaction. Snapshot it first.
        #   * Reach the current value with `getattr`. On an unloaded
        #     attribute that emits a lazy SELECT in the middle of a flush,
        #     which is both the mutation above and a query at the worst
        #     possible moment. `state.dict` is what is already loaded on
        #     the instance, and never goes to the database.
        changed = set()
        for key, previous in list(state.committed_state.items()):
            current = state.dict.get(key, NO_VALUE)
            if current is NO_VALUE:
                # Not loaded, so there is no current value to compare
                # against. Nothing we could conclude here is worth a
                # query.
                continue
            if previous is NO_VALUE:
                if current is not None:
                    changed.add(key)
            elif current != previous:
                changed.add(key)
        # Ignore a flush whose only change is the stamp itself, or the
        # timestamps SQLAlchemy maintains.
        if not (changed - {"updated_by", "updated_at", "created_by", "created_at"}):
            continue
        obj.updated_by = actor


def install_authorship_listener() -> None:
    """Wire the flush hook. Idempotent — safe to call on every import."""
    if not event.contains(Session, "before_flush", stamp_resource_authorship):
        event.listen(Session, "before_flush", stamp_resource_authorship)


# Installed at import time so no caller can forget it. The listener is a
# no-op unless an actor is bound, so importing this module has no effect
# on seeds, migrations or tests that never set one.
install_authorship_listener()


__all__ = [
    "DEFAULT_RESOURCE_VERSION",
    "ResourceVersionMixin",
    "ResourceAuthorshipMixin",
    "set_current_actor",
    "reset_current_actor",
    "get_current_actor",
    "SYSTEM_ACTOR",
    "open_actor_scope",
    "actor_name",
    "acting_as",
    "stamp_resource_authorship",
    "install_authorship_listener",
]
