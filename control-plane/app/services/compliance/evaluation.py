# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Obligation evaluation for the Compliance console

Applicability (dated, never a compliance boolean) × evidence statements
(resolved per system over a window). Three views share one engine:

* **matrix** — system × obligation, each cell the worst statement status
  and n-of-m counts kept apart per basis (recomputed and judged are never
  summed);
* **detail** — one obligation's statements for one system, with the
  "same evidence under other frameworks" cross-links;
* **overview** — per system readiness for a framework.

**Version in force.** A window is split at catalog-version
boundaries; each segment is evaluated against the catalog text in force
then, and every result records its `catalog_version`.

Evaluations are cached per `(tenant, framework, window, view)` for
`CACHE_TTL_SECONDS` — the matrix touches every system's ledger.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database.models.ai_system_compliance_profile import AISystemComplianceProfile
from app.database.models.compliance_framework import ComplianceFramework
from app.database.models.resource_group import GroupType, ResourceGroup
from app.database.models.tenant import Tenant
from app.services import compliance_registry
from app.services.ai_obligations import system_context
from app.services.compliance import offering
from app.services.compliance.catalogs.base import (
    NOT_LEGAL_ADVICE,
    STATUS_NOT_APPLICABLE,
    STATUS_UNDETERMINED,
    EvidenceSpec,
    Framework,
    Obligation,
    applicability,
)
from app.services.compliance.resolvers import (
    DEFAULT_WINDOW_DAYS,
    MET,
    NOT_MET,
    ResolverContext,
    StatementResult,
    run,
    worst,
)

#: A live window (one that ends "now") is evaluated on this grid: its end is
#: snapped down to a multiple of it and the window keeps its length. The
#: console asks for "the last 30 days" with an end rounded to the minute, so
#: every minute was a new window and a new, cold evaluation (6 s idle, up to
#: 19 s under load on the demo's ledger). On the grid a window is evaluated
#: once per grid step, and the answer names the window it covers. The price:
#: a live page shows records up to one grid step old.
LIVE_WINDOW_GRID_SECONDS = 300
#: An evaluation is kept for one grid step: a snapped window never changes
#: under its key. Declarations and composition edits clear the tenant's
#: entries at once (`clear_cache`).
CACHE_TTL_SECONDS = LIVE_WINDOW_GRID_SECONDS
CACHE_MAX_ENTRIES = 256
#: Longest window the console may ask for in one evaluation.
MAX_WINDOW_DAYS = 400


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

_cache: Dict[Tuple, Tuple[float, Any]] = {}
_cache_lock = threading.Lock()
#: One lock per key being computed, so concurrent requests for the same
#: evaluation wait for the first instead of each computing it. A page that
#: fires three requests on a cold cache, or a user who reloads a slow page,
#: otherwise multiplies the load that made it slow.
_inflight: Dict[Tuple, threading.Lock] = {}
#: Bumped by `clear_cache`; a value computed before a clear is returned to
#: its caller but not stored.
_generation = 0


def _fresh(key: Tuple) -> Tuple[bool, Any]:
    hit = _cache.get(key)
    if hit and hit[0] > time.monotonic():
        return True, hit[1]
    return False, None


def cached(key: Tuple, fn: Callable[[], Any], ttl: int = CACHE_TTL_SECONDS) -> Any:
    with _cache_lock:
        ok, value = _fresh(key)
        if ok:
            return value
        key_lock = _inflight.setdefault(key, threading.Lock())
    with key_lock:
        with _cache_lock:
            ok, value = _fresh(key)  # computed while this request waited
            if ok:
                return value
            generation = _generation
        try:
            value = fn()
        finally:
            with _cache_lock:
                if _inflight.get(key) is key_lock:
                    _inflight.pop(key, None)
        with _cache_lock:
            if generation == _generation:
                if len(_cache) >= CACHE_MAX_ENTRIES:
                    for k in sorted(_cache, key=lambda k: _cache[k][0])[: CACHE_MAX_ENTRIES // 4]:
                        _cache.pop(k, None)
                _cache[key] = (time.monotonic() + ttl, value)
    return value


def clear_cache(tenant_id: Optional[str] = None) -> None:
    global _generation
    with _cache_lock:
        _generation += 1
        if tenant_id is None:
            _cache.clear()
        else:
            for k in [k for k in _cache if k and k[1] == tenant_id]:
                _cache.pop(k, None)


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


def framework_or_404(framework_id: str) -> Framework:
    """The offered catalog; 404 unknown, 409 known but not offered."""
    return offering.require(framework_id)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_window(
    start: Optional[datetime], end: Optional[datetime], *, now: Optional[datetime] = None
) -> Tuple[datetime, datetime]:
    """`[start, end)`; defaults to the last 30 days.

    A LIVE window (no end given, or an end within one grid step of now) is
    snapped to `LIVE_WINDOW_GRID_SECONDS`: the end moves down to the grid and
    the start moves with it, so the length asked for is kept and every
    request of the same grid step evaluates the same window. A window that
    ended in the past is evaluated exactly as asked.
    """
    now = now or datetime.now(timezone.utc)
    grid = timedelta(seconds=LIVE_WINDOW_GRID_SECONDS)
    asked_end = _aware(end) if end else now
    asked_start = _aware(start) if start else None
    if now - asked_end < grid:  # live: ends now (or was rounded up past it)
        epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
        snapped = epoch + ((min(asked_end, now) - epoch) // grid) * grid
        if asked_start is not None:
            asked_start -= asked_end - snapped
        asked_end = snapped
    end = asked_end
    start = asked_start if asked_start is not None else end - timedelta(days=DEFAULT_WINDOW_DAYS)
    if start >= end:
        raise HTTPException(status_code=422, detail="`from` must be before `to`")
    if end - start > timedelta(days=MAX_WINDOW_DAYS):
        raise HTTPException(status_code=422, detail=f"The window may span at most {MAX_WINDOW_DAYS} days")
    return start, end


def active_systems(db: Session, tenant_id: str, ai_system_id: Optional[str] = None) -> List[ResourceGroup]:
    q = db.query(ResourceGroup).filter(
        ResourceGroup.tenant_id == tenant_id,
        ResourceGroup.group_type == GroupType.AI_SYSTEM,
    )
    if ai_system_id:
        q = q.filter(ResourceGroup.id == ai_system_id)
    else:
        q = q.filter(ResourceGroup.is_active.is_(True))
    return q.order_by(ResourceGroup.name).all()


def load_profiles(db: Session, tenant_id: str) -> Dict[str, AISystemComplianceProfile]:
    return {
        p.ai_system_id: p
        for p in db.query(AISystemComplianceProfile)
        .filter(AISystemComplianceProfile.tenant_id == tenant_id)
        .all()
    }


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


@dataclass
class Segment:
    catalog_version: Optional[str]
    start: datetime
    end: datetime


def segments(framework: Framework, start: datetime, end: datetime) -> List[Segment]:
    """Split `[start, end)` at catalog-version boundaries."""
    last_day = (end - timedelta(microseconds=1)).date()
    out = []
    for version, seg_start, seg_end in framework.segments(start.date(), last_day):
        s = max(start, datetime.combine(seg_start, dtime.min, tzinfo=timezone.utc))
        e = min(end, datetime.combine(seg_end + timedelta(days=1), dtime.min, tzinfo=timezone.utc))
        if s < e:
            out.append(Segment(version, s, e))
    return out


class SystemEvaluator:
    """Evaluates statements for ONE system, memoising by spec identity and
    segment so a resolver cited by five obligations runs once."""

    def __init__(self, db: Session, tenant_id: str, framework: Framework, system: ResourceGroup,
                 profile: Optional[AISystemComplianceProfile], window: Tuple[datetime, datetime],
                 now: Optional[datetime] = None, shared: Optional[Dict[Any, Any]] = None):
        self.db = db
        self.tenant_id = tenant_id
        self.framework = framework
        self.system = system
        self.profile = profile
        self.window = window
        self.now = now or datetime.now(timezone.utc)
        self.segments = segments(framework, *window)
        self._contexts: Dict[Tuple, ResolverContext] = {}
        self._results: Dict[Tuple, StatementResult] = {}
        self.shared: Dict[Any, Any] = shared if shared is not None else {}
        # One tenant tier computation per fleet evaluation.
        tier_facts = None
        if framework.composite_classification:
            from app.services.ai_system_composition import load_tier_facts

            key = ("tier_facts", tenant_id)
            if key not in self.shared:
                self.shared[key] = load_tier_facts(db, tenant_id)
            tier_facts = self.shared[key]
        self.tier_facts = tier_facts
        self.context = system_context(db, system, framework, profile, tier_facts=tier_facts)

    def _ctx(self, seg: Segment) -> ResolverContext:
        key = (seg.start, seg.end)
        if key not in self._contexts:
            self._contexts[key] = ResolverContext(
                db=self.db, tenant_id=self.tenant_id, system=self.system, window_start=seg.start,
                window_end=seg.end, framework_id=self.framework.id, now=self.now, profile=self.profile,
                shared=self.shared,
            )
        return self._contexts[key]

    def statement(self, spec: EvidenceSpec, seg: Segment) -> StatementResult:
        key = (spec.identity, seg.catalog_version, seg.start, seg.end)
        if key not in self._results:
            self._results[key] = run(self._ctx(seg), spec, catalog_version=seg.catalog_version)
        base = self._results[key]
        # Same resolver+params, possibly different statement wording/key.
        if base.key == spec.key and base.statement == spec.statement:
            return base
        clone = StatementResult(**{**base.__dict__})
        clone.key, clone.statement, clone.limit = spec.key, spec.statement, spec.limit or base.limit
        return clone

    def obligation(self, obligation: Obligation, *, with_statements: bool = True) -> Dict[str, Any]:
        status, reasons = applicability(obligation, self.context)
        present_in = [s for s in self.segments if obligation in self.framework.obligations_in(s.catalog_version)]
        current_version = self.segments[-1].catalog_version if self.segments else self.framework.current_version
        applies_from = obligation.applies_from_in(current_version)
        results: List[StatementResult] = []
        if with_statements and status != STATUS_NOT_APPLICABLE:
            for seg in present_in:
                for spec in obligation.evidence:
                    results.append(self.statement(spec, seg))
        return {
            "key": obligation.key,
            "title": obligation.title,
            "reference": obligation.reference,
            "applies_from": applies_from.isoformat(),
            "in_force": self.now.date() >= applies_from,
            "applicability": status,
            "reason": "; ".join(reasons),
            "needs_review": obligation.needs_review,
            "review_note": obligation.review_note,
            "present_in_window": bool(present_in),
            "statements": results,
        }


def summarize_statements(results: List[StatementResult]) -> Dict[str, Any]:
    """Cell summary. Counts statements by status and — per basis, never
    summed across bases — statements met of statements measured."""
    counts: Dict[str, int] = {}
    by_basis: Dict[str, Dict[str, int]] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
        if r.status in (MET, NOT_MET):
            b = by_basis.setdefault(r.basis, {"met": 0, "measured": 0})
            b["measured"] += 1
            b["met"] += int(r.status == MET)
    return {
        "worst_status": worst([r.status for r in results]),
        "status_counts": counts,
        "by_basis": by_basis,
        "summary": "; ".join(
            f"{basis}: {v['met']} of {v['measured']} statements met" for basis, v in sorted(by_basis.items())
        ),
    }


def other_frameworks(spec: EvidenceSpec, framework_id: str) -> List[Dict[str, Any]]:
    """Where the same `(resolver, params)` is cited under other catalogs."""
    out = []
    for fid, fw in offering.offered_catalogs().items():
        if fid == framework_id:
            continue
        for ob in fw.obligations:
            if any(s.identity == spec.identity for s in ob.evidence):
                out.append({"framework_id": fid, "framework_title": fw.title, "obligation_key": ob.key,
                            "title": ob.title, "reference": ob.reference})
    return out


def framework_block(db: Session, tenant: Tenant, fw: Framework) -> Dict[str, Any]:
    row = db.get(ComplianceFramework, fw.id)
    block = fw.to_summary()
    block["enabled"] = compliance_registry.is_enabled(tenant, row) if row is not None else False
    block["registered"] = row is not None
    block["not_legal_advice"] = NOT_LEGAL_ADVICE
    return block


def statement_dict(r: StatementResult) -> Dict[str, Any]:
    return r.to_dict()


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


def _evaluators(db: Session, tenant_id: str, fw: Framework, window, ai_system_id: Optional[str]) -> List[SystemEvaluator]:
    profiles = load_profiles(db, tenant_id)
    now = datetime.now(timezone.utc)
    shared: Dict[Any, Any] = {}
    return [
        SystemEvaluator(db, tenant_id, fw, s, profiles.get(s.id), window, now=now, shared=shared)
        for s in active_systems(db, tenant_id, ai_system_id)
    ]


class _Evaluated:
    """A system's full obligation evaluation, detached from the session so
    matrix, overview and detail share one cached computation."""

    def __init__(self, ev: SystemEvaluator):
        from types import SimpleNamespace

        self.system = SimpleNamespace(id=ev.system.id, name=ev.system.name, display_name=ev.system.display_name)
        self.context = ev.context
        self.profile = (
            SimpleNamespace(declaration_record_id=ev.profile.declaration_record_id) if ev.profile is not None else None
        )
        self._results = {ob.key: ev.obligation(ob) for ob in ev.framework.obligations}

    def obligation(self, ob: Obligation) -> Dict[str, Any]:
        return self._results[ob.key]


def _fleet(db: Session, tenant_id: str, fw: Framework, window, ai_system_id: Optional[str]) -> List[_Evaluated]:
    return cached(
        ("fleet", tenant_id, fw.id, window[0], window[1], ai_system_id),
        lambda: [_Evaluated(ev) for ev in _evaluators(db, tenant_id, fw, window, ai_system_id)],
    )


def matrix(db: Session, tenant: Tenant, fw: Framework, window, ai_system_id: Optional[str] = None) -> Dict[str, Any]:
    def build():
        evaluators = _fleet(db, tenant.id, fw, window, ai_system_id)
        rows = []
        for ev in evaluators:
            cells = []
            for ob in fw.obligations:
                res = ev.obligation(ob)
                if not res["present_in_window"] and not res["statements"]:
                    # Not in the catalog text in force during the window.
                    cells.append({"obligation_key": ob.key, "applicability": res["applicability"],
                                  "in_force": res["in_force"], "present_in_window": False,
                                  "worst_status": None, "status_counts": {}, "by_basis": {}, "summary": ""})
                    continue
                cells.append({
                    "obligation_key": ob.key,
                    "applicability": res["applicability"],
                    "reason": res["reason"],
                    "in_force": res["in_force"],
                    "present_in_window": res["present_in_window"],
                    **summarize_statements(res["statements"]),
                    "catalog_versions": sorted({r.catalog_version for r in res["statements"] if r.catalog_version}),
                })
            rows.append({
                "ai_system_id": ev.system.id,
                "name": ev.system.display_name or ev.system.name,
                "risk_tier": ev.context.risk_tier,
                "risk_tier_declared": ev.context.risk_tier_declared,
                "effective_tier_sources": ev.context.risk_tier_sources,
                "role": ev.context.role,
                "profile_declared": ev.profile is not None,
                "cells": cells,
            })
        return {
            "framework": framework_block(db, tenant, fw),
            "window": {"from": window[0].isoformat(), "to": window[1].isoformat()},
            "catalog_versions": [s.catalog_version for s in segments(fw, *window)],
            "obligations": [
                {"key": o.key, "title": o.title, "reference": o.reference,
                 "applies_from": o.applies_from.isoformat(), "aliases": sorted(o.aliases)}
                for o in fw.obligations
            ],
            "systems": rows,
        }

    return cached(("matrix", tenant.id, fw.id, window[0], window[1], ai_system_id), build)


def detail(db: Session, tenant: Tenant, fw: Framework, key: str, ai_system_id: Optional[str], window) -> Dict[str, Any]:
    obligations = fw.resolve_key(key)
    if not obligations:
        raise HTTPException(status_code=404, detail=f"Unknown obligation {key!r} in {fw.id}")

    def build():
        evaluators = _fleet(db, tenant.id, fw, window, ai_system_id)
        if ai_system_id and not evaluators:
            raise HTTPException(status_code=404, detail=f"AI System {ai_system_id!r} not found")
        systems = []
        for ev in evaluators:
            per_obligation = []
            for ob in obligations:
                res = ev.obligation(ob)
                per_obligation.append({
                    **{k: v for k, v in res.items() if k != "statements"},
                    "statements": [
                        {**statement_dict(r), "other_frameworks": other_frameworks(
                            next(s for s in ob.evidence if s.key == r.key), fw.id)}
                        for r in res["statements"]
                    ],
                    **summarize_statements(res["statements"]),
                })
            systems.append({
                "ai_system_id": ev.system.id,
                "name": ev.system.display_name or ev.system.name,
                "obligations": per_obligation,
            })
        head = obligations[0]
        return {
            "framework": framework_block(db, tenant, fw),
            "key": key,
            "resolved_keys": [o.key for o in obligations],
            "title": head.title if len(obligations) == 1 else f"{len(obligations)} obligations (formerly {key})",
            "reference": head.reference if len(obligations) == 1 else ", ".join(o.reference for o in obligations),
            "summary": head.summary if len(obligations) == 1 else None,
            "limit": head.limit if len(obligations) == 1 else None,
            "not_legal_advice": NOT_LEGAL_ADVICE,
            "window": {"from": window[0].isoformat(), "to": window[1].isoformat()},
            "systems": systems,
        }

    return cached(("detail", tenant.id, fw.id, window[0], window[1], key, ai_system_id), build)


def _latest_head(db: Session, tenant_id: str) -> Optional[Dict[str, Any]]:
    row = db.execute(
        text(
            "SELECT id, tree_size, grade, issued_at FROM evidence_tree_heads WHERE tenant_id = :t "
            "ORDER BY issued_at DESC LIMIT 1"
        ),
        {"t": tenant_id},
    ).first()
    if row is None:
        return None
    return {"id": row[0], "tree_size": int(row[1]), "grade": row[2],
            "issued_at": row[3].isoformat() if row[3] else None}


def _record_lag(db: Session, tenant_id: str, since: datetime) -> Dict[str, Any]:
    row = db.execute(
        text(
            "SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (created_at - timestamp))), "
            "max(EXTRACT(EPOCH FROM (created_at - timestamp))), count(*) FROM action_records "
            "WHERE tenant_id = :t AND created_at >= :s AND proxy_log_id IS NOT NULL "
            "AND NOT backfilled"  # generated column: no record body is read
        ),
        {"t": tenant_id, "s": since},
    ).first()
    return {
        "p50_seconds": round(float(row[0]), 3) if row and row[0] is not None else None,
        "max_seconds": round(float(row[1]), 3) if row and row[1] is not None else None,
        "records": int(row[2] or 0) if row else 0,
    }


def _capture_gap_items(db: Session, tenant_id: str) -> Dict[Optional[str], int]:
    rows = db.execute(
        text(
            "SELECT ai_system_id, count(*) FROM assurance_inbox_items WHERE tenant_id = :t "
            "AND category = 'capture_gap' AND state <> 'resolved' GROUP BY ai_system_id"
        ),
        {"t": tenant_id},
    ).fetchall()
    return {r[0]: int(r[1]) for r in rows}


def _latest_reports(db: Session, tenant_id: str, framework_id: str) -> Dict[Optional[str], Dict[str, Any]]:
    rows = db.execute(
        text(
            "SELECT ai_system_id, id, cadence, period_start, period_end, status "
            "FROM compliance_reports WHERE tenant_id = :t AND framework_id = :f "
            "ORDER BY created_at DESC LIMIT 500"
        ),
        {"t": tenant_id, "f": framework_id},
    ).fetchall()
    out: Dict[Optional[str], Dict[str, Any]] = {}
    for r in rows:
        if r[0] in out:
            continue
        out[r[0]] = {"id": r[1], "cadence": r[2], "period_start": str(r[3]) if r[3] else None,
                     "period_end": str(r[4]) if r[4] else None, "status": r[5]}
    return out


def _safe(db: Session, fn: Callable[[], Any], default: Any) -> Any:
    """An overview side-panel query must never fail the overview."""
    try:
        return fn()
    except Exception:  # noqa: BLE001
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return default


def overview(db: Session, tenant: Tenant, fw: Framework, window) -> Dict[str, Any]:
    def build():
        evaluators = _fleet(db, tenant.id, fw, window, None)
        head = _safe(db, lambda: _latest_head(db, tenant.id), None)
        gaps = _safe(db, lambda: _capture_gap_items(db, tenant.id), {})
        reports = _safe(db, lambda: _latest_reports(db, tenant.id, fw.id), {})
        lag = _safe(db, lambda: _record_lag(db, tenant.id, window[0]),
                    {"p50_seconds": None, "max_seconds": None, "records": 0})
        today = datetime.now(timezone.utc).date()
        systems = []
        for ev in evaluators:
            in_force, upcoming, undetermined, results = [], [], [], []
            for ob in fw.obligations:
                res = ev.obligation(ob)
                if res["applicability"] == STATUS_NOT_APPLICABLE:
                    continue
                entry = {"key": ob.key, "title": ob.title, "applies_from": res["applies_from"]}
                (in_force if res["in_force"] else upcoming).append(entry)
                if res["applicability"] == STATUS_UNDETERMINED:
                    undetermined.append({**entry, "reason": res["reason"]})
                results.extend(res["statements"])
            # Distinct statements (a resolver cited twice counts once).
            distinct = {(r.resolver, tuple(sorted(r.params.items())), r.catalog_version): r for r in results}
            sealing = next((r for r in distinct.values() if r.resolver == "records.every_verdict_sealed"), None)
            systems.append({
                "ai_system_id": ev.system.id,
                "name": ev.system.display_name or ev.system.name,
                "risk_tier": ev.context.risk_tier,
                "risk_tier_declared": ev.context.risk_tier_declared,
                "effective_tier_sources": ev.context.risk_tier_sources,
                "role": ev.context.role,
                "profile_declared": ev.profile is not None,
                "profile_sealed": bool(ev.profile is not None and ev.profile.declaration_record_id),
                "obligations_in_force": in_force,
                "obligations_upcoming": sorted(upcoming, key=lambda e: e["applies_from"]),
                "undetermined": undetermined,
                "statement_counts": summarize_statements(list(distinct.values())),
                "capture": {
                    "unsealed_governed_actions": (
                        (sealing.m or 0) - (sealing.n or 0) if sealing and sealing.m is not None else None
                    ),
                    "open_capture_gap_items": gaps.get(ev.system.id, 0),
                },
                "latest_report": reports.get(ev.system.id) or reports.get(None),
            })
        timeline = sorted(
            ({"key": o.key, "title": o.title, "applies_from": o.applies_from.isoformat(),
              "in_force": today >= o.applies_from} for o in fw.obligations),
            key=lambda e: (e["applies_from"], e["key"]),
        )
        return {
            "framework": framework_block(db, tenant, fw),
            "window": {"from": window[0].isoformat(), "to": window[1].isoformat()},
            "evidence": {
                "latest_head": head,
                "grade": head["grade"] if head else None,
                "record_lag": lag,
                "open_capture_gap_items": sum(gaps.values()),
            },
            "timeline": timeline,
            "systems": systems,
        }

    return cached(("overview", tenant.id, fw.id, window[0], window[1]), build)


def systems_table(db: Session, tenant: Tenant) -> Dict[str, Any]:
    from app.services.compliance.profile import profile_view
    from app.services.compliance.retention_floors import active_frameworks, floor_sources

    from app.services.ai_system_composition import load_tier_facts

    active = active_frameworks(db, tenant)
    profiles = load_profiles(db, tenant.id)
    tier_facts = load_tier_facts(db, tenant.id)
    items = []
    for s in active_systems(db, tenant.id):
        p = profiles.get(s.id)
        sources = floor_sources(s, p, active, tier_facts)
        view = profile_view(s, p, tenant.id)
        blocks = view["frameworks"]
        items.append({
            **view,
            "name": s.display_name or s.name,
            "lifecycle_stage": getattr(s, "lifecycle_stage", None),
            "active_frameworks": [
                fw.id for fw in active
                if not (isinstance(blocks.get(fw.id), dict) and blocks[fw.id].get("in_scope") is False)
            ],
            "retention_floor_days": max((x.floor_days for x in sources), default=None),
        })
    return {"items": items, "frameworks_enabled": [fw.id for fw in active]}


__all__ = [
    "CACHE_TTL_SECONDS",
    "SystemEvaluator",
    "cached",
    "clear_cache",
    "detail",
    "framework_or_404",
    "matrix",
    "other_frameworks",
    "overview",
    "parse_window",
    "segments",
    "summarize_statements",
    "systems_table",
]
