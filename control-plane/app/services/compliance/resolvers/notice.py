# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# notice.*, synthetic.*

`notice.before_first_turn` — per conversation handle seen on the system's
rows in the window: the conversation's earliest `ai.brutor.notice` record
must precede its first ledger row **in time**, and — when that first row
carries a record — **in log order** (lower `seq` in the tenant log). A
notice sealed after the first turn is a late notice: not met for that
conversation, however soon after.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import text

from app.services.compliance.catalogs.base import EvidenceSpec
from app.services.compliance.resolvers._base import (
    DECLARED,
    MET,
    NOT_EVALUABLE,
    NOT_MET,
    UNDETERMINED,
    Outcome,
    ResolverContext,
    aware,
    declaration_record,
    dotted,
    present,
    profile_dict,
    resolver,
    window_params,
)


#: How far before the window a conversation's earlier turns are looked for
#: (a conversation idle longer than this is counted as new).
CONVERSATION_LOOKBACK = timedelta(days=30)


def notice_config(ctx: ResolverContext) -> Optional[Any]:
    """The system's notice config, else the tenant default row."""
    def load():
        rows = ctx.db.execute(
            text(
                "SELECT ai_system_id, enabled, surface, texts, text_digests, exemption, record_id "
                "FROM compliance_notice_configs WHERE tenant_id = :t "
                "AND (ai_system_id = :sid OR ai_system_id IS NULL)"
            ),
            {"t": ctx.tenant_id, "sid": ctx.system_id},
        ).fetchall()
        own = [r for r in rows if r[0] == ctx.system_id]
        return (own or [r for r in rows if r[0] is None] or [None])[0]

    return ctx.cached(("notice_config", ctx.system_id), load)


def _exemption(cfg: Any) -> Optional[Dict[str, Any]]:
    if cfg is None or not isinstance(cfg[5], dict):
        return None
    return cfg[5] if cfg[5].get("asserted") else None


def conversation_outcomes(ctx: ResolverContext) -> List[Dict[str, Any]]:
    """One entry per conversation handle first seen in the window (pure
    over the two queries below)."""
    def tenant_firsts():
        # One scan per fleet evaluation: each (handle, resource group)'s
        # first row in the window.
        return ctx.db.execute(
            text(
                """
                SELECT DISTINCT ON (upper(p.custom_metadata->>'conversation_handle'), p.resource_group_id)
                       upper(p.custom_metadata->>'conversation_handle') AS handle,
                       p.id, p.timestamp, p.record_id, a.seq, p.resource_group_id
                  FROM proxy_logs p
                  LEFT JOIN action_records a ON a.record_id = p.record_id
                 WHERE p.tenant_id = :t AND p.timestamp >= :ws AND p.timestamp < :we
                   AND p.custom_metadata->>'conversation_handle' IS NOT NULL
                 ORDER BY upper(p.custom_metadata->>'conversation_handle'), p.resource_group_id,
                          p.timestamp, p.id
                """
            ),
            window_params(ctx),
        ).fetchall()

    gids = set(ctx.group_ids())
    earliest: Dict[str, Any] = {}
    for r in ctx.shared_cached(("notice_firsts", ctx.tenant_id, ctx.window_start, ctx.window_end), tenant_firsts):
        if r[5] not in gids:
            continue
        cur = earliest.get(r[0])
        if cur is None or (aware(r[2]), r[1]) < (aware(cur[2]), cur[1]):
            earliest[r[0]] = r
    firsts = [tuple(r[:5]) for r in sorted(earliest.values(), key=lambda r: r[0])]
    # A conversation belongs to the window its FIRST turn falls in: drop
    # handles already seen in the look-back before the window.
    if firsts:
        earlier = {r[0] for r in ctx.db.execute(
            text(
                "SELECT DISTINCT upper(custom_metadata->>'conversation_handle') FROM proxy_logs "
                "WHERE tenant_id = :t AND timestamp >= :lb AND timestamp < :ws "
                "AND resource_group_id = ANY(:gids) "
                "AND upper(custom_metadata->>'conversation_handle') = ANY(:handles)"
            ),
            {"t": ctx.tenant_id, "lb": ctx.window_start - CONVERSATION_LOOKBACK, "ws": ctx.window_start,
             "gids": ctx.group_ids(), "handles": [r[0] for r in firsts]},
        ).fetchall()}
        firsts = [r for r in firsts if r[0] not in earlier]
    handles = [r[0] for r in firsts]
    notices: Dict[str, Any] = {}
    if handles:
        for handle, rid, ts, seq in ctx.db.execute(
            text(
                """
                SELECT DISTINCT ON (upper(record->'context'->>'conversation_handle'))
                       upper(record->'context'->>'conversation_handle'), record_id, timestamp, seq
                  FROM action_records
                 WHERE tenant_id = :t
                   AND record->'context'->>'record_type' = 'ai.brutor.notice'
                   AND upper(record->'context'->>'conversation_handle') = ANY(:handles)
                 ORDER BY upper(record->'context'->>'conversation_handle'), timestamp, seq
                """
            ),
            {"t": ctx.tenant_id, "handles": handles},
        ).fetchall():
            notices[handle] = (rid, aware(ts), seq)
    out = []
    for handle, row_id, row_ts, row_record, row_seq in firsts:
        notice = notices.get(handle)
        entry: Dict[str, Any] = {"conversation_handle": handle, "first_row_id": row_id,
                                 "first_record_id": row_record}
        if notice is None:
            entry["outcome"] = "missing"
        else:
            entry["notice_record_id"] = notice[0]
            late_time = notice[1] > aware(row_ts)
            late_order = row_seq is not None and notice[2] is not None and int(notice[2]) > int(row_seq)
            entry["outcome"] = "late" if (late_time or late_order) else "before"
            if late_order and not late_time:
                entry["late_in"] = "log_order"
        out.append(entry)
    return out


@resolver("notice.before_first_turn")
def before_first_turn(ctx: ResolverContext, spec: EvidenceSpec) -> Outcome:
    conv = conversation_outcomes(ctx)
    before = [c for c in conv if c["outcome"] == "before"]
    late = [c for c in conv if c["outcome"] == "late"]
    missing = [c for c in conv if c["outcome"] == "missing"]
    cfg = notice_config(ctx)
    exemption = _exemption(cfg)
    facts = {"conversations": len(conv), "notice_before_first_turn": len(before),
             "late_notice": len(late), "no_notice": len(missing),
             "exemption_asserted": bool(exemption)}
    citations = [c["notice_record_id"] for c in before + late if c.get("notice_record_id")]
    refs = [{"type": "proxy_log", "id": c["first_row_id"], "outcome": c["outcome"]} for c in late + missing]
    if not conv:
        if exemption:
            return Outcome(DECLARED, detail="No conversations in the window; an exemption is asserted "
                                            f"({exemption.get('basis') or 'basis not stated'}).",
                           citations=[cfg[6]] if cfg and cfg[6] else [], facts=facts)
        return Outcome(NOT_EVALUABLE, 0, 0,
                       "No conversations carried a conversation handle in the window — nothing to evaluate.",
                       facts=facts)
    n, m = len(before), len(conv)
    detail = f"{n} of {m} conversations showed the notice before the first turn."
    if late:
        detail += f" Late notice: {len(late)}."
    if missing:
        detail += f" No notice: {len(missing)}."
    if n < m and exemption:
        return Outcome(DECLARED, n, m, detail + f" An exemption is asserted "
                                               f"({exemption.get('basis') or 'basis not stated'}).",
                       citations=citations + ([cfg[6]] if cfg[6] else []), facts=facts, refs=refs)
    return Outcome(MET if n == m else NOT_MET, n, m, detail, citations=citations, facts=facts, refs=refs)


@resolver("notice.text_declared")
def text_declared(ctx: ResolverContext, spec: EvidenceSpec) -> Outcome:
    cfg = notice_config(ctx)
    if cfg is None:
        return Outcome(UNDETERMINED, detail="No AI-interaction notice is configured for this system or tenant "
                                            "(the portal shows its built-in default text, which is not declared).")
    _sys, enabled, surface, texts, digests, exemption, record_id = cfg
    facts = {"scope": "system" if _sys else "tenant", "enabled": bool(enabled), "surface": surface,
             "locales": sorted((digests or texts or {}).keys()), "exemption_asserted": bool(_exemption(cfg))}
    if not record_id:
        return Outcome(UNDETERMINED, detail="The notice configuration exists but its declaration is not sealed.",
                       facts=facts)
    if not enabled and not _exemption(cfg):
        return Outcome(NOT_MET, detail="The notice is switched off and no exemption is asserted.",
                       citations=[record_id], facts=facts)
    if not enabled:
        return Outcome(DECLARED, detail=f"Notice off under an asserted exemption "
                                        f"({_exemption(cfg).get('basis') or 'basis not stated'}); sealed.",
                       citations=[record_id], facts=facts)
    return Outcome(DECLARED, detail=f"Notice text declared for {len(facts['locales']) or 'the default'} "
                                    f"locale(s), shown as {surface}; sealed.",
                   citations=[record_id], facts=facts)


#: Profile paths a synthetic-content marking approach may be declared at.
MARKING_PATHS = ("core.synthetic_marking",)


@resolver("synthetic.marking_declared")
def marking_declared(ctx: ResolverContext, spec: EvidenceSpec) -> Outcome:
    prof = profile_dict(ctx)
    paths = list(MARKING_PATHS)
    if ctx.framework_id:
        paths.append(f"frameworks.{ctx.framework_id}.synthetic_marking")
    value = None
    for path in paths:
        found, v = dotted(prof, path)
        if found and present(v):
            value = v
            break
    decl = declaration_record(ctx)
    facts = {"declared": present(value), "sealed": bool(decl), "content_marking_provided": False}
    if not present(value):
        return Outcome(UNDETERMINED, detail="No synthetic-content marking approach is declared.", facts=facts)
    if not decl:
        return Outcome(UNDETERMINED, detail="A marking approach is declared but the declaration is not sealed.",
                       facts=facts)
    return Outcome(DECLARED, detail="Marking approach declared and sealed. Content-level marking is not "
                                    "provided by the gateway.", citations=[decl], facts=facts)


__all__ = ["before_first_turn", "conversation_outcomes", "marking_declared", "notice_config", "text_declared"]
