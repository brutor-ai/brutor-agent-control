# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Agent implementation identity — declarations and releases

The credential says WHO is acting (the agent identity). The core records
WHAT software acted and WHICH release of it (``agent_impl_*`` on proxy logs,
runs and segments; one ``agent_implementation_releases`` row per observed
release). This module is the control plane's ONE reading of those facts:

  * the declaration an approver makes per identity — ``implementation_name``,
    ``approved_versions`` (exact versions or a trailing ``.*`` wildcard),
    ``require_release`` — and its validation;
  * ``approval_of`` — whether a version is approved, DERIVED at read time
    from the declaration (never stored), so approving a version takes effect
    for every release at once and can never disagree with the declaration;
  * the release timeline of an identity and its CURRENT release (the one
    seen last), which the composition view, the assurance report and the
    agent checks all read through ``current_releases``.

Nothing here decides access: these values describe; the credential and
the grants decide.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy.orm import Session
from ulid import ULID

from app.database.models.agent_identity_approval_history import (
    SOURCE_BACKFILL_LIVE,
    SOURCE_DECLARATION,
    AgentIdentityApprovalHistory,
)
from app.database.models.agent_implementation_release import AgentImplementationRelease

#: Name bounds — identical to the core's normalisation, so a
#: declared name compares equal to what the core stores. Names are
#: lower-cased; versions and builds are case-preserving.
NAME_PATTERN = re.compile(r"^[a-z0-9._-]{1,64}$")
VERSION_PATTERN = re.compile(r"^[A-Za-z0-9.+_-]{1,64}$")
BUILD_PATTERN = re.compile(r"^[A-Za-z0-9:._-]{1,128}$")

#: The only wildcard: a trailing ``.*`` (``1.4.*`` accepts ``1.4.2`` and
#: ``1.4.2.1``, never ``1.40`` and never ``1.4`` itself).
WILDCARD_SUFFIX = ".*"
#: An approver lists a handful of lines, not a catalogue.
MAX_APPROVED_VERSIONS = 32


class DeclarationError(ValueError):
    """An implementation declaration that cannot be stored (422)."""


def normalise_implementation_name(value: Optional[str]) -> Optional[str]:
    """Trimmed, lower-cased, and within the name bounds; ``None`` or an
    empty string clears the declaration."""
    if value is None:
        return None
    name = value.strip().lower()
    if not name:
        return None
    if not NAME_PATTERN.match(name):
        raise DeclarationError(
            f"implementation_name {value!r} must match [a-z0-9._-]{{1,64}} "
            "(the bounds the gateway normalises declared names to)"
        )
    return name


def validate_version_pattern(value: str) -> str:
    """One approved-version pattern: an exact version, or a version prefix
    followed by ``.*``."""
    if not isinstance(value, str):
        raise DeclarationError(f"approved version {value!r} must be a string")
    pattern = value.strip()
    base = pattern[: -len(WILDCARD_SUFFIX)] if pattern.endswith(WILDCARD_SUFFIX) else pattern
    if not base or "*" in base or not VERSION_PATTERN.match(base):
        raise DeclarationError(
            f"approved version {value!r} must be an exact version "
            "([A-Za-z0-9.+_-]{1,64}) or a version prefix ending in '.*' (e.g. '1.4.*')"
        )
    if len(pattern) > 64 + len(WILDCARD_SUFFIX):
        raise DeclarationError(f"approved version {value!r} is too long")
    return pattern


def validate_approved_versions(values: Optional[Iterable[str]]) -> List[str]:
    """The stored form: validated, de-duplicated and SORTED — a set of
    patterns, so the contract closure's hash never depends on the order an
    approver typed them in."""
    out = sorted({validate_version_pattern(v) for v in (values or [])})
    if len(out) > MAX_APPROVED_VERSIONS:
        raise DeclarationError(
            f"at most {MAX_APPROVED_VERSIONS} approved version patterns"
        )
    return out


def version_matches(version: Optional[str], pattern: str) -> bool:
    if not version:
        return False
    if pattern.endswith(WILDCARD_SUFFIX):
        prefix = pattern[: -len(WILDCARD_SUFFIX)]
        return version.startswith(prefix + ".") and len(version) > len(prefix) + 1
    return version == pattern


def approval_of(version: Optional[str], approved_versions: Optional[Sequence[str]]) -> Optional[bool]:
    """``None`` when the identity approves nothing (observe only — no
    approval to be in or out of); otherwise whether ``version`` matches one
    of the patterns."""
    patterns = list(approved_versions or [])
    if not patterns:
        return None
    return any(version_matches(version, p) for p in patterns)


def release_key(name: Optional[str], version: Optional[str], build: Optional[str]) -> str:
    """``name@version`` plus ``#build`` when there is one — the release part
    of an agent finding's fingerprint and of a release's display label."""
    key = f"{name or '?'}@{version or '?'}"
    return f"{key}#{build}" if build else key


def release_view(
    release: AgentImplementationRelease,
    approved_versions: Optional[Sequence[str]],
    implementation_name: Optional[str] = None,
) -> Dict[str, Any]:
    """A release as every surface shows it, with ``approved`` derived."""
    d = release.to_dict()
    d["approved"] = approval_of(release.version, approved_versions)
    d["name_matches"] = (
        None if not implementation_name else release.name == implementation_name
    )
    d["label"] = release_key(release.name, release.version, release.build)
    return d


def compact_release(
    release: Optional[AgentImplementationRelease],
    approved_versions: Optional[Sequence[str]],
) -> Optional[Dict[str, Any]]:
    """The ``{name, version, build, trust, approved}`` block the composition
    view and the assurance report carry per member agent."""
    if release is None:
        return None
    return {
        "name": release.name,
        "version": release.version,
        "build": release.build or None,
        "trust": release.trust,
        "approved": approval_of(release.version, approved_versions),
        "last_seen_at": release.last_seen_at.isoformat() if release.last_seen_at else None,
    }


def _order_key(r: AgentImplementationRelease):
    # Latest seen first; ties (same finaliser tick) broken by the newer
    # release, then by id so the answer is deterministic.
    return (r.last_seen_at, r.first_seen_at, r.id)


def releases_of(db: Session, tenant_id: str, agent_id: str) -> List[AgentImplementationRelease]:
    """Every observed release of one identity, most recently seen first."""
    rows = (
        db.query(AgentImplementationRelease)
        .filter(
            AgentImplementationRelease.tenant_id == tenant_id,
            AgentImplementationRelease.agent_id == agent_id,
        )
        .all()
    )
    return sorted(rows, key=_order_key, reverse=True)


def current_releases(
    db: Session, tenant_id: str, agent_ids: Iterable[str]
) -> Dict[str, AgentImplementationRelease]:
    """``{agent id: its current release}`` — THE definition of "current":
    the release seen last. Identities with no observed release are absent."""
    ids = sorted({a for a in agent_ids if a})
    if not ids:
        return {}
    out: Dict[str, AgentImplementationRelease] = {}
    for r in (
        db.query(AgentImplementationRelease)
        .filter(
            AgentImplementationRelease.tenant_id == tenant_id,
            AgentImplementationRelease.agent_id.in_(ids),
        )
        .all()
    ):
        best = out.get(r.agent_id)
        if best is None or _order_key(r) > _order_key(best):
            out[r.agent_id] = r
    return out


# ---------------------------------------------------------------------------
# Approval history
# ---------------------------------------------------------------------------


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def record_approved_versions(
    db: Session,
    agent,
    values: Optional[Iterable[str]],
    *,
    by: Optional[str] = None,
    at: Optional[datetime] = None,
) -> bool:
    """THE only writer of ``agent_identities.approved_versions``.

    Validates the patterns (``DeclarationError`` → 422), sets the live
    declaration, and keeps ``agent_identity_approval_history`` in step: the
    current period is closed at ``at`` and a new one opened — only when the
    value actually changes, so re-sending the same list (an idempotent
    setup script) leaves the history alone. An identity with no history yet
    gets its first period here. The caller commits.

    The agent must already be in the session (``db.add`` it first on
    create): the history row references it.

    Returns whether a new period was opened. Guarded by
    ``tests/test_agent_approval_history.py``: no other code assigns
    ``approved_versions``, because a value set anywhere else would be judged
    as if it had always been in effect.
    """
    versions = validate_approved_versions(values)
    at = _aware(at) or datetime.now(timezone.utc)
    current = (
        db.query(AgentIdentityApprovalHistory)
        .filter(
            AgentIdentityApprovalHistory.agent_id == agent.id,
            AgentIdentityApprovalHistory.valid_to.is_(None),
        )
        .one_or_none()
    )
    prior = sorted(str(v) for v in (agent.approved_versions or []))
    agent.approved_versions = versions
    if current is not None and list(current.approved_versions or []) == versions:
        return False
    if current is None and prior != versions and _aware(getattr(agent, "created_at", None)):
        # An identity with no history whose declaration changes: the value
        # it held until now has been in effect since it was created (this is
        # the only writer, so nothing else can have changed it). Recording
        # that period keeps runs made before this change judged by it.
        created = _aware(agent.created_at)
        if created < at:
            db.add(AgentIdentityApprovalHistory(
                id=f"aah-{ULID()}",
                tenant_id=agent.tenant_id,
                agent_id=agent.id,
                approved_versions=prior,
                valid_from=created,
                valid_to=at,
                changed_by=None,
                source=SOURCE_BACKFILL_LIVE,
            ))
            db.flush()
    if current is not None:
        start = _aware(current.valid_from)
        current.valid_to = at if start is None or at >= start else start
        # The partial unique index allows one open period per identity:
        # close the old one before the new one is inserted.
        db.flush()
    db.add(AgentIdentityApprovalHistory(
        id=f"aah-{ULID()}",
        tenant_id=agent.tenant_id,
        agent_id=agent.id,
        approved_versions=versions,
        valid_from=at,
        valid_to=None,
        changed_by=by,
        source=SOURCE_DECLARATION,
    ))
    return True


#: One identity's declared periods: ``(valid_from, valid_to, versions)``,
#: oldest first. ``valid_to`` None = the current declaration.
ApprovalTimeline = List[Tuple[datetime, Optional[datetime], List[str]]]


def approval_timelines(
    db: Session, tenant_id: str, agent_ids: Optional[Iterable[str]] = None
) -> Dict[str, ApprovalTimeline]:
    """``{agent id: its approval timeline}`` for a tenant (or some agents)."""
    q = db.query(AgentIdentityApprovalHistory).filter(
        AgentIdentityApprovalHistory.tenant_id == tenant_id
    )
    if agent_ids is not None:
        ids = sorted({a for a in agent_ids if a})
        if not ids:
            return {}
        q = q.filter(AgentIdentityApprovalHistory.agent_id.in_(ids))
    out: Dict[str, ApprovalTimeline] = {}
    for row in q:
        out.setdefault(row.agent_id, []).append(
            (_aware(row.valid_from), _aware(row.valid_to), list(row.approved_versions or []))
        )
    for periods in out.values():
        periods.sort(key=lambda p: (p[0], p[1] is None, p[1] or p[0]))
    return out


def approved_versions_at(
    timeline: Optional[ApprovalTimeline],
    at: Optional[datetime],
    live: Optional[Sequence[str]],
) -> List[str]:
    """The ``approved_versions`` in effect at ``at``.

    Periods are half-open ``[valid_from, valid_to)``: a run that starts at
    the very instant a new declaration takes effect is judged by the new
    one. Two fallbacks, both explicit:

      * no history at all (an identity no declaration ever touched) → the
        live value, which for such an identity is the column's default;
      * ``at`` before the earliest period → the EARLIEST period. The first
        period is anchored at the identity's creation (the writer on
        create, or the backfill of existing approvals), so only clock skew between the
        gateway and the control plane — or a backfilled first period whose
        true start is unknown — can put a run before it. The earliest
        record is the closest evidence of what was declared then;
        answering "no constraint" instead would hide a release that ran
        outside every declaration the approver ever made.
    """
    if not timeline:
        return list(live or [])
    at = _aware(at)
    if at is None or at < timeline[0][0]:
        return list(timeline[0][2])
    starts = [p[0] for p in timeline]
    latest = bisect_right(starts, at) - 1
    # The period containing `at`, searching back from the latest one that
    # started at or before it (zero-length periods never contain anything).
    for i in range(latest, -1, -1):
        _start, end, versions = timeline[i]
        if end is None or at < end:
            return list(versions)
    # A gap in the history (none is ever written): the declaration made
    # most recently before `at` is the one in effect.
    return list(timeline[latest][2])


__all__ = [
    "BUILD_PATTERN",
    "DeclarationError",
    "MAX_APPROVED_VERSIONS",
    "NAME_PATTERN",
    "VERSION_PATTERN",
    "WILDCARD_SUFFIX",
    "ApprovalTimeline",
    "approval_of",
    "approval_timelines",
    "approved_versions_at",
    "compact_release",
    "current_releases",
    "normalise_implementation_name",
    "record_approved_versions",
    "release_key",
    "release_view",
    "releases_of",
    "validate_approved_versions",
    "validate_version_pattern",
    "version_matches",
]
