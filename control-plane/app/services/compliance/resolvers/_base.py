# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Resolver engine types

A resolver turns one `EvidenceSpec` into one `StatementResult` for one AI
System over one window. Rules every resolver keeps:

* **never `met` on missing inputs** — no rows, no records, no profile ⇒
  `not_evaluable` (nothing to measure) or `undetermined` (a declaration is
  missing), never a clean result;
* **counts, never percentages** — `n` of `m`, with `m` the population the
  statement is about;
* **citations are record ids**, capped; other pointers (evidence pointers,
  contracts, incidents) go in `refs`;
* **recomputed and judged are never summed** — a judged result carries its
  agreement statistics in `facts`, and every roll-up keeps the two apart;
* **never raise** — an internal failure is `not_evaluable` with the reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services.compliance.catalogs.base import EvidenceSpec

# ---------------------------------------------------------------------------
# Statement statuses
# ---------------------------------------------------------------------------

MET = "met"
NOT_MET = "not_met"
NOT_EVALUABLE = "not_evaluable"
UNDETERMINED = "undetermined"
INDICATOR = "indicator"
DECLARED = "declared"
STATEMENT_STATUSES = frozenset({MET, NOT_MET, NOT_EVALUABLE, UNDETERMINED, INDICATOR, DECLARED})

#: Worst first — the matrix cell shows the worst status of its statements.
#: `declared` and `indicator` say nothing either way, so they rank below
#: every measured outcome but above nothing.
STATUS_RANK = {
    NOT_MET: 0,
    UNDETERMINED: 1,
    NOT_EVALUABLE: 2,
    MET: 3,
    INDICATOR: 4,
    DECLARED: 5,
}

#: Citations per statement. A statement over 10 000 rows cites a sample;
#: `facts` carries the counts.
MAX_CITATIONS = 20

#: Default evaluation window.
DEFAULT_WINDOW_DAYS = 30


def worst(statuses: Sequence[str]) -> Optional[str]:
    ranked = [s for s in statuses if s in STATUS_RANK]
    return min(ranked, key=lambda s: STATUS_RANK[s]) if ranked else None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class StatementResult:
    key: str
    basis: str
    status: str
    n: Optional[int] = None
    m: Optional[int] = None
    detail: str = ""
    limit: Optional[str] = None
    citations: List[str] = field(default_factory=list)
    evaluated_at: datetime = field(default_factory=utcnow)
    statement: str = ""
    resolver: str = ""
    params: Dict[str, Any] = field(default_factory=dict)
    catalog_version: Optional[str] = None
    window_start: Optional[datetime] = None
    window_end: Optional[datetime] = None
    #: Structured sub-counts (e.g. refusals, backfilled, grade counts,
    #: agreement). Never merged into n / m.
    facts: Dict[str, Any] = field(default_factory=dict)
    #: Non-record pointers: [{"type": "evidence_pointer", "id": …}, …].
    refs: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "statement": self.statement,
            "basis": self.basis,
            "resolver": self.resolver,
            "params": dict(self.params),
            "status": self.status,
            "n": self.n,
            "m": self.m,
            "detail": self.detail,
            "limit": self.limit,
            "citations": list(self.citations[:MAX_CITATIONS]),
            "catalog_version": self.catalog_version,
            "window_start": self.window_start.isoformat() if self.window_start else None,
            "window_end": self.window_end.isoformat() if self.window_end else None,
            "evaluated_at": self.evaluated_at.isoformat(),
            "facts": self.facts,
            "refs": self.refs[:MAX_CITATIONS],
        }


@dataclass
class Outcome:
    """What a resolver function returns; the engine completes it into a
    `StatementResult` with the spec's identity, window and version."""

    status: str
    n: Optional[int] = None
    m: Optional[int] = None
    detail: str = ""
    citations: List[str] = field(default_factory=list)
    facts: Dict[str, Any] = field(default_factory=dict)
    refs: List[Dict[str, Any]] = field(default_factory=list)


def counted(n: int, m: int, *, what: str, facts: Optional[Dict[str, Any]] = None,
            citations: Optional[List[str]] = None, empty: str = "",
            refs: Optional[List[Dict[str, Any]]] = None) -> Outcome:
    """The common n-of-m shape: `m == 0` ⇒ not_evaluable (nothing to
    measure is not a clean result), `n == m` ⇒ met, else not_met."""
    if m == 0:
        return Outcome(NOT_EVALUABLE, 0, 0, empty or f"No {what} in the window — nothing to evaluate.",
                       citations or [], facts or {}, refs or [])
    status = MET if n == m else NOT_MET
    return Outcome(status, n, m, f"{n} of {m} {what}.", citations or [], facts or {}, refs or [])


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------


@dataclass
class ResolverContext:
    db: Session
    tenant_id: str
    system: Any  # ResourceGroup (group_type ai_system)
    window_start: datetime
    window_end: datetime
    framework_id: Optional[str] = None
    now: datetime = field(default_factory=utcnow)
    profile: Any = None  # AISystemComplianceProfile | None
    #: Shared across the statements of one evaluation (per system):
    #: expensive sub-results (group ids, chain verdicts) are computed once.
    memo: Dict[Any, Any] = field(default_factory=dict)
    #: Shared across the systems of one fleet evaluation (same tenant):
    #: tenant-wide scans keyed by window are done once, not per system.
    shared: Dict[Any, Any] = field(default_factory=dict)

    def shared_cached(self, key: Any, fn: Callable[[], Any]) -> Any:
        if key not in self.shared:
            self.shared[key] = fn()
        return self.shared[key]

    @property
    def system_id(self) -> str:
        return self.system.id

    def cached(self, key: Any, fn: Callable[[], Any]) -> Any:
        if key not in self.memo:
            self.memo[key] = fn()
        return self.memo[key]

    def group_ids(self) -> List[str]:
        return self.cached(("group_ids", self.system_id), lambda: system_group_ids(
            self.db, self.tenant_id, self.system_id
        ))

    def days(self) -> List[date]:
        """Calendar days (UTC) the window covers, up to today."""
        start = self.window_start.date()
        # The window end is exclusive.
        end = (min(self.window_end, self.now) - timedelta(microseconds=1)).date()
        out = []
        d = start
        while d <= end:
            out.append(d)
            d += timedelta(days=1)
        return out


def system_group_ids(db: Session, tenant_id: str, ai_system_id: str) -> List[str]:
    """The AI System's own group plus every descendant whose NEAREST AI
    System is this one — the same attribution the core's
    `nearest_ai_system` applies to a row (a nested AI System owns its own
    subtree)."""
    rows = db.execute(
        text(
            """
            WITH RECURSIVE tree AS (
                SELECT id FROM resource_groups WHERE id = :sid AND tenant_id = :t
                UNION ALL
                SELECT rg.id FROM resource_groups rg
                  JOIN tree ON rg.parent_group_id = tree.id
                 WHERE rg.tenant_id = :t
                   AND UPPER(COALESCE(CAST(rg.group_type AS TEXT), '')) <> 'AI_SYSTEM'
            )
            SELECT id FROM tree
            """
        ),
        {"sid": ai_system_id, "t": tenant_id},
    ).fetchall()
    ids = [r[0] for r in rows]
    return ids or [ai_system_id]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

ResolverFn = Callable[[ResolverContext, EvidenceSpec], Outcome]

RESOLVERS: Dict[str, ResolverFn] = {}

#: Tier-B templates a `judge.<template>` resolver may name.
JUDGE_TEMPLATES = frozenset({"no_manipulation", "notice_clear", "per_instructions", "approval_rationale"})
JUDGE_PREFIX = "judge."


def resolver(resolver_id: str) -> Callable[[ResolverFn], ResolverFn]:
    def register(fn: ResolverFn) -> ResolverFn:
        if resolver_id in RESOLVERS:
            raise RuntimeError(f"resolver {resolver_id!r} registered twice")
        RESOLVERS[resolver_id] = fn
        return fn

    return register


def split_judge(resolver_id: str) -> Optional[str]:
    if resolver_id.startswith(JUDGE_PREFIX):
        template = resolver_id[len(JUDGE_PREFIX):]
        if template in JUDGE_TEMPLATES:
            return template
    return None


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------


def window_params(ctx: ResolverContext) -> Dict[str, Any]:
    return {"t": ctx.tenant_id, "ws": ctx.window_start, "we": ctx.window_end}


def profile_dict(ctx: ResolverContext) -> Dict[str, Any]:
    p = ctx.profile
    if p is None:
        return {"core": {}, "frameworks": {}}
    return {"core": dict(p.core or {}), "frameworks": dict(p.frameworks or {})}


def declaration_record(ctx: ResolverContext) -> Optional[str]:
    return getattr(ctx.profile, "declaration_record_id", None) if ctx.profile is not None else None


def dotted(data: Dict[str, Any], path: str) -> Tuple[bool, Any]:
    """Resolve `a.b.c` where segment 2 may itself contain dashes
    (`frameworks.eu-ai-act.qms_reference`)."""
    cur: Any = data
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False, None
        cur = cur[part]
    return True, cur


def present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return bool(value)
    return True


__all__ = [
    "DECLARED",
    "DEFAULT_WINDOW_DAYS",
    "INDICATOR",
    "JUDGE_PREFIX",
    "JUDGE_TEMPLATES",
    "MAX_CITATIONS",
    "MET",
    "NOT_EVALUABLE",
    "NOT_MET",
    "Outcome",
    "RESOLVERS",
    "ResolverContext",
    "STATEMENT_STATUSES",
    "STATUS_RANK",
    "StatementResult",
    "UNDETERMINED",
    "aware",
    "counted",
    "declaration_record",
    "dotted",
    "present",
    "profile_dict",
    "resolver",
    "split_judge",
    "system_group_ids",
    "utcnow",
    "window_params",
    "worst",
]
