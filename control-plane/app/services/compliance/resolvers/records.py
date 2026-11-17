# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# records.* — the sealed-record statements

`records.every_verdict_sealed`, `records.witnessed`,
`records.chain_complete`, `records.integrity_verified`.

The "which rows must carry a record" rule mirrors the core's consequential
rule (`brutor-gateway-core/src/services/evidence/mint.rs::should_record_with`):
commands seal, queries do not, unknown defaults to sealed, every refusal
seals; a `readOnlyHint: true` MCP tool seals only through a sensitive
resource group; LLM completions seal only under `inference_records`. A
drift test pins the subtype literals against `constants.rs`.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import text

from app.database.models.evidence_log import (
    GRADE_PLURAL,
    GRADE_SELF_WITNESSED,
    GRADE_STANDALONE,
    GRADE_WITNESSED,
)
from app.services.compliance.catalogs.base import EvidenceSpec
from app.services.compliance.resolvers._base import (
    MAX_CITATIONS,
    MET,
    NOT_EVALUABLE,
    NOT_MET,
    UNDETERMINED,
    Outcome,
    ResolverContext,
    aware,
    counted,
    resolver,
    window_params,
)
from app.services.evidence import core_client

# ---------------------------------------------------------------------------
# The consequential rule (mirror of mint.rs; literals pinned by test)
# ---------------------------------------------------------------------------

#: proxy_type → record surface (`mint.rs::surface_of`).
SURFACE_BY_PROXY_TYPE = {
    "mcp": "mcp",
    "virtual-mcp": "mcp",
    "a2a": "a2a",
    "skill": "skill",
    "llm": "llm",
}

#: Subtypes the writer seals as `denied` (`mint.rs::DENY_SUBTYPES`).
DENY_SUBTYPES = (
    "agent_authz_block",
    "semantic_policy_block",
    "autonomy_block",
    "residency_violation",
    "run_aborted",
    "run_cap_reached",
    "access_denied",
    "skill_quota_exceeded",
    "skill_concurrency_limit",
    "approval_token_invalid",
    # Accepted-caller enforcement refused the A2A call.
    "unaccepted_caller",
    # An LLM-surface governance policy refused the call before the provider.
    "llm_governance_block",
)
#: Approval / engine subtypes that always seal (`mint.rs::is_refusal`).
ALWAYS_SEALED_SUBTYPES = (
    "approval_required",
    "approval_approved",
    "approval_rejected",
    "engine_failure",
)
SUBTYPE_EMBEDDINGS = "embeddings"
SUBTYPE_SKILL_RUN = "skill_run"

#: Approval requests with no stored TTL are assumed to lapse after this.
DEFAULT_APPROVAL_TTL = timedelta(hours=24)
#: The approval sweeper's cadence: an expiry record may trail the TTL.
CHAIN_GRACE = timedelta(minutes=15)
#: A planned record's outcome waits at most 5 s in the writer; anything
#: unconfirmed after this is the may/did gap.
PLANNED_GRACE = timedelta(minutes=15)
#: Records younger than this may not be under a signed head yet (heads
#: every 100 entries / 15 min).
HEAD_GRACE = timedelta(hours=1)

_REFUSAL_SQL = (
    "(COALESCE(p.guardrail_blocked, false) OR COALESCE(p.argument_policy_denied, false) "
    "OR p.agent_authz_decision = 'deny' "
    "OR p.proxy_subtype = ANY(:deny) OR p.proxy_subtype = ANY(:always) "
    "OR p.proxy_subtype LIKE 'rate_limit\\_%')"
)


def _is_high_risk(ctx: ResolverContext) -> bool:
    if (getattr(ctx.system, "eu_ai_act_risk_tier", None) or "").lower() == "high":
        return True
    frameworks = dict(getattr(ctx.profile, "frameworks", None) or {})
    return any(
        isinstance(b, dict) and str(b.get("risk_tier") or "").lower() == "high"
        for b in frameworks.values()
    )


def inference_records_on(ctx: ResolverContext) -> bool:
    """`attribution.rs::derive_options` — explicit flag wins, else high-risk."""
    core = dict(getattr(ctx.profile, "core", None) or {})
    flag = core.get("inference_records")
    if isinstance(flag, bool):
        return flag
    return _is_high_risk(ctx)


def _system_sensitive(ctx: ResolverContext) -> bool:
    return bool(getattr(ctx.system, "phi_resource", False)) or bool(
        getattr(ctx.system, "sensitive_data", False)
    )


def read_only_map(caps: Any) -> Dict[str, bool]:
    """tool → readOnlyHint (`attribution.rs::read_only_map`)."""
    tools = None
    if isinstance(caps, dict):
        tools = caps.get("tools")
    elif isinstance(caps, list):
        tools = caps
    out: Dict[str, bool] = {}
    for t in tools or []:
        if isinstance(t, dict) and isinstance(t.get("name"), str):
            ann = t.get("annotations") if isinstance(t.get("annotations"), dict) else {}
            out[t["name"]] = bool(ann.get("readOnlyHint")) if isinstance(ann.get("readOnlyHint"), bool) else False
    return out


def _tool_maps(ctx: ResolverContext, server_ids: List[str]) -> Dict[str, Dict[str, bool]]:
    if not server_ids:
        return {}
    rows = ctx.db.execute(
        text(
            "SELECT id, capabilities::jsonb FROM mcp_servers "
            "WHERE tenant_id = :t AND id = ANY(:ids)"
        ),
        {"t": ctx.tenant_id, "ids": server_ids},
    ).fetchall()
    return {r[0]: read_only_map(r[1]) for r in rows}


def _tenant_row_groups(ctx: ResolverContext) -> List[Dict[str, Any]]:
    """Every proxy-log row of the TENANT in the window, grouped by resource
    group and everything the consequential rule reads (one scan per fleet
    evaluation, shared by all systems)."""

    def load():
        sql = text(
            f"""
            SELECT p.resource_group_id AS rg_id,
                   p.proxy_type,
                   {_REFUSAL_SQL} AS refusal,
                   (p.mcp_method = 'tools/call') AS tools_call,
                   CASE WHEN p.mcp_method = 'tools/call' THEN p.server_id END AS server_id,
                   CASE WHEN p.mcp_method = 'tools/call' THEN p.mcp_capability_name END AS tool,
                   (p.skill_execution_id IS NOT NULL OR p.proxy_subtype = :skill_run) AS skill_exec,
                   (p.proxy_subtype = :embeddings OR COALESCE(p.endpoint, '') LIKE '%embeddings%') AS embed,
                   -- A planned record never names a row (proxy_logs.record_id is the
                   -- outcome's), but a row pointing at one would not be sealed.
                   (p.record_id IS NOT NULL AND a.verdict IS DISTINCT FROM 'planned') AS sealed,
                   COALESCE(a.backfilled, false) AS backfilled,
                   count(*) AS n
              FROM proxy_logs p
              LEFT JOIN action_records a ON a.record_id = p.record_id
             WHERE p.tenant_id = :t
               AND p.timestamp >= :ws AND p.timestamp < :we
               AND p.resource_group_id IS NOT NULL
               AND p.proxy_type = ANY(:types)
             GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9, 10
            """
        )
        rows = ctx.db.execute(
            sql,
            {
                **window_params(ctx),
                "types": list(SURFACE_BY_PROXY_TYPE),
                "deny": list(DENY_SUBTYPES),
                "always": list(ALWAYS_SEALED_SUBTYPES),
                "skill_run": SUBTYPE_SKILL_RUN,
                "embeddings": SUBTYPE_EMBEDDINGS,
            },
        ).fetchall()
        return [dict(r._mapping) for r in rows]

    return ctx.shared_cached(("tenant_row_groups", ctx.tenant_id, ctx.window_start, ctx.window_end), load)


def _row_groups(ctx: ResolverContext) -> List[Dict[str, Any]]:
    """The system's share of the tenant row groups, with each group's
    resource-group sensitivity attached."""

    def load():
        gids = set(ctx.group_ids())
        mine = [dict(g) for g in _tenant_row_groups(ctx) if g["rg_id"] in gids]
        sensitive = {
            r[0]: bool(r[1])
            for r in ctx.db.execute(
                text(
                    "SELECT rg.id, (COALESCE((to_jsonb(rg)->>'phi_resource')::boolean, false) "
                    "OR COALESCE((to_jsonb(rg)->>'sensitive_data')::boolean, false)) "
                    "FROM resource_groups rg WHERE rg.tenant_id = :t AND rg.id = ANY(:gids)"
                ),
                {"t": ctx.tenant_id, "gids": list(gids)},
            ).fetchall()
        }
        for d in mine:
            d["rg_sensitive"] = sensitive.get(d.pop("rg_id"), False)
        return mine

    return ctx.cached(("row_groups", ctx.system_id), load)


def _must_seal(group: Dict[str, Any], tool_maps: Dict[str, Dict[str, bool]],
               inference: bool, system_sensitive: bool) -> bool:
    surface = SURFACE_BY_PROXY_TYPE.get(group["proxy_type"])
    if surface is None:
        return False
    if group["refusal"]:
        return True
    if surface == "mcp":
        if not group["tools_call"]:
            return False
        read_only = tool_maps.get(group["server_id"] or "", {}).get(group["tool"] or "")
        sensitive = bool(group["rg_sensitive"]) or system_sensitive
        return not (read_only is True and not sensitive)
    if surface == "a2a":
        return True
    if surface == "skill":
        return bool(group["skill_exec"])
    if surface == "llm":
        return inference and not group["embed"]
    return False


def sealing_counts(ctx: ResolverContext) -> Dict[str, int]:
    groups = _row_groups(ctx)
    servers = sorted({g["server_id"] for g in groups if g["server_id"]})
    tool_maps = _tool_maps(ctx, servers)
    inference = inference_records_on(ctx)
    sensitive = _system_sensitive(ctx)
    out = {"required": 0, "sealed": 0, "backfilled": 0, "refusals": 0, "refusals_sealed": 0}
    for g in groups:
        if not _must_seal(g, tool_maps, inference, sensitive):
            continue
        n = int(g["n"])
        out["required"] += n
        if g["sealed"]:
            out["sealed"] += n
            if g["backfilled"]:
                out["backfilled"] += n
        if g["refusal"]:
            out["refusals"] += n
            if g["sealed"]:
                out["refusals_sealed"] += n
    return out


def _sample_records(ctx: ResolverContext, extra_where: str = "", params: Optional[Dict[str, Any]] = None) -> List[str]:
    rows = ctx.db.execute(
        text(
            "SELECT record_id FROM action_records "
            "WHERE tenant_id = :t AND ai_system_id = :sid "
            "AND timestamp >= :ws AND timestamp < :we "
            f"{extra_where} ORDER BY timestamp DESC LIMIT :lim"
        ),
        {**window_params(ctx), "sid": ctx.system_id, "lim": MAX_CITATIONS, **(params or {})},
    ).fetchall()
    return [r[0] for r in rows]


@resolver("records.every_verdict_sealed")
def every_verdict_sealed(ctx: ResolverContext, spec: EvidenceSpec) -> Outcome:
    # n/m count governed proxy-log rows and whether each carries its
    # outcome record. A `planned` record (two-phase) is not a governed
    # row — it has no proxy_log_id and never counts towards n or m. An
    # unconfirmed one older than PLANNED_GRACE is the "may without did" gap
    # (dispatch attested, outcome never recorded) and is reported
    # separately as `planned_unconfirmed` — the row it would have produced
    # is not in proxy_logs, so it cannot appear in m.
    c = sealing_counts(ctx)
    planned = planned_status(ctx)
    unconfirmed = planned["unconfirmed"]
    facts = {
        "refusals_sealed": c["refusals_sealed"],
        "refusals": c["refusals"],
        "sealed_late_by_backfill": c["backfilled"],
        "inference_records": inference_records_on(ctx),
        "planned_records": planned["total"],
        "planned_unconfirmed": len(unconfirmed),
    }
    refs = [{"type": "planned_record", "id": rid, "outcome": "unconfirmed"} for rid in unconfirmed[:MAX_CITATIONS]]
    planned_note = (
        f" {len(unconfirmed)} planned action(s) never confirmed by an outcome record "
        "(dispatch attested, result not recorded)."
        if unconfirmed else ""
    )
    if c["required"] == 0:
        return Outcome(NOT_EVALUABLE, 0, 0,
                       "No governed actions in the window — nothing to evaluate." + planned_note,
                       facts=facts, refs=refs)
    missing = c["required"] - c["sealed"]
    detail = (
        f"{c['sealed']} of {c['required']} governed actions carry a sealed record; "
        f"refusals {c['refusals_sealed']} of {c['refusals']}."
    )
    if c["backfilled"]:
        detail += f" Sealed late by backfill: {c['backfilled']}."
    if missing:
        detail += f" {missing} without a record (capture gap)."
    detail += planned_note
    return Outcome(
        MET if missing == 0 else NOT_MET,
        c["sealed"],
        c["required"],
        detail,
        citations=_sample_records(ctx, "AND proxy_log_id IS NOT NULL AND verdict <> 'planned'"),
        facts=facts,
        refs=refs,
    )


# ---------------------------------------------------------------------------
# records.witnessed
# ---------------------------------------------------------------------------


@resolver("records.witnessed")
def witnessed(ctx: ResolverContext, spec: EvidenceSpec) -> Outcome:
    span = ctx.db.execute(
        text(
            "SELECT log_id, min(seq), max(seq), count(*) FROM action_records "
            "WHERE tenant_id = :t AND ai_system_id = :sid "
            "AND timestamp >= :ws AND timestamp < :we GROUP BY log_id"
        ),
        {**window_params(ctx), "sid": ctx.system_id},
    ).fetchall()
    if not span:
        return Outcome(NOT_EVALUABLE, 0, 0, "No sealed records in the window — nothing to witness.")
    grades = {GRADE_STANDALONE: 0, GRADE_SELF_WITNESSED: 0, GRADE_WITNESSED: 0, GRADE_PLURAL: 0}
    heads_total = 0
    witnessed_n = 0
    uncovered_old = 0
    uncovered_recent = 0
    citations: List[str] = []
    refs: List[Dict[str, Any]] = []
    for log_id, min_seq, max_seq, _count in span:
        heads = ctx.db.execute(
            text(
                "SELECT id, tree_size, prev_size, grade FROM evidence_tree_heads "
                "WHERE log_id = :l AND tree_size > :lo AND prev_size <= :hi "
                "ORDER BY tree_size"
            ),
            {"l": log_id, "lo": int(min_seq), "hi": int(max_seq)},
        ).fetchall()
        covered_to = max((int(h[1]) for h in heads), default=0)
        for hid, _size, _prev, grade in heads:
            heads_total += 1
            grades[grade] = grades.get(grade, 0) + 1
            if grade in (GRADE_WITNESSED, GRADE_PLURAL):
                witnessed_n += 1
            refs.append({"type": "tree_head", "id": hid, "grade": grade})
        if covered_to <= int(max_seq):
            rows = ctx.db.execute(
                text(
                    "SELECT record_id, timestamp FROM action_records "
                    "WHERE log_id = :l AND seq >= :from_seq AND seq <= :hi AND tenant_id = :t "
                    "AND ai_system_id = :sid AND timestamp >= :ws AND timestamp < :we"
                ),
                {**window_params(ctx), "l": log_id, "from_seq": covered_to, "hi": int(max_seq), "sid": ctx.system_id},
            ).fetchall()
            for rid, ts in rows:
                if ctx.now - aware(ts) > HEAD_GRACE:
                    uncovered_old += 1
                    citations.append(rid)
                else:
                    uncovered_recent += 1
    facts = {"grades": grades, "plural": grades[GRADE_PLURAL], "uncovered_records": uncovered_old,
             "awaiting_next_head": uncovered_recent}
    if uncovered_old:
        return Outcome(
            NOT_MET, witnessed_n, heads_total,
            f"{uncovered_old} record(s) older than an hour are not covered by any signed tree head.",
            citations=citations, facts=facts, refs=refs,
        )
    if heads_total == 0:
        return Outcome(NOT_EVALUABLE, 0, 0, "Records are awaiting their first tree head.", facts=facts)
    out = counted(witnessed_n, heads_total, what="tree heads covering the window are witnessed independently",
                  facts=facts, refs=refs)
    if grades[GRADE_PLURAL]:
        out.detail += f" {grades[GRADE_PLURAL]} by two or more independent witnesses (plural)."
    if grades[GRADE_SELF_WITNESSED]:
        out.detail += f" {grades[GRADE_SELF_WITNESSED]} self-witnessed (same operator) — not independent."
    return out


# ---------------------------------------------------------------------------
# records.chain_complete
# ---------------------------------------------------------------------------


def _children(ctx: ResolverContext, parent_ids: List[str], relations: Tuple[str, ...]) -> Dict[str, str]:
    if not parent_ids:
        return {}
    # Generated columns, answered from ix_action_records_chain_parent:
    # no record body is read. Joined to the parents by id instead of sending
    # thousands of ids as a parameter.
    rows = ctx.db.execute(
        text(
            "SELECT chain_parent_record_id, record_id FROM action_records "
            "WHERE chain_parent_record_id = ANY(:ids) "
            "AND tenant_id = :t AND timestamp >= :ws AND chain_relation = ANY(:rel)"
        ),
        {"t": ctx.tenant_id, "ws": ctx.window_start, "rel": list(relations), "ids": parent_ids},
    ).fetchall()
    out: Dict[str, str] = {}
    for parent, child in rows:
        out.setdefault(parent, child)
    return out


def _approval_expiry(ctx: ResolverContext, request_ids: List[str]) -> Dict[str, Any]:
    ids = [r for r in request_ids if r]
    if not ids:
        return {}
    # Token approvals live in tool_approval_requests; a skill parked by its
    # execution mode (`approval_required`) is its own approval request, keyed
    # by the execution id the core stamps as `context.approval_request_id`.
    rows = ctx.db.execute(
        text(
            "SELECT id, expires_at FROM tool_approval_requests "
            "WHERE tenant_id = :t AND id = ANY(:ids) "
            "UNION ALL "
            "SELECT id, approval_expires_at FROM agent_skill_executions "
            "WHERE tenant_id = :t AND id = ANY(:ids) AND approval_expires_at IS NOT NULL"
        ),
        {"t": ctx.tenant_id, "ids": ids},
    ).fetchall()
    return {r[0]: aware(r[1]) for r in rows if r[1] is not None}


def epoch_gaps(ctx: ResolverContext) -> Dict[str, Any]:
    """Contracts promoted in the window vs the `epoch_boundary` records."""
    promoted = ctx.db.execute(
        text(
            "SELECT id, version, promoted_at, approved_by FROM ai_system_contracts "
            "WHERE tenant_id = :t AND ai_system_id = :sid "
            "AND promoted_at >= :ws AND promoted_at < :we ORDER BY promoted_at"
        ),
        {**window_params(ctx), "sid": ctx.system_id},
    ).fetchall()
    boundaries = ctx.db.execute(
        text(
            "SELECT record_id, record->'context'->>'contract_version', "
            "record->'disposition'->>'approver' FROM action_records "
            "WHERE tenant_id = :t AND ai_system_id = :sid AND verdict = 'epoch_boundary'"
        ),
        {"t": ctx.tenant_id, "sid": ctx.system_id},
    ).fetchall()
    by_version = {str(v): (rid, approver) for rid, v, approver in boundaries if v is not None}
    sealed, missing, citations, unattributed = [], [], [], []
    for cid, version, _at, _approved_by in promoted:
        hit = by_version.get(str(version))
        if hit:
            sealed.append(cid)
            citations.append(hit[0])
            if not hit[1]:
                unattributed.append(cid)
        else:
            missing.append({"contract_id": cid, "version": version})
    return {"promotions": len(promoted), "sealed": len(sealed), "missing": missing,
            "citations": citations, "without_approver": unattributed}


def planned_status(ctx: ResolverContext) -> Dict[str, Any]:
    """The system's `planned` records in the window (two-phase
    actions) against the outcomes that `confirms` them: `confirmed`,
    `open` (younger than PLANNED_GRACE — the outcome may still land) and
    `unconfirmed` (record ids older than that with no outcome: the visible
    may-without-did gap a crash mid-dispatch leaves). Shared by
    `records.every_verdict_sealed` and `records.chain_complete`."""

    def load():
        rows = ctx.db.execute(
            text(
                "SELECT record_id, timestamp FROM action_records WHERE tenant_id = :t "
                "AND ai_system_id = :sid AND verdict = 'planned' "
                "AND timestamp >= :ws AND timestamp < :we"
            ),
            {**window_params(ctx), "sid": ctx.system_id},
        ).fetchall()
        confirmed = _children(ctx, [r[0] for r in rows], ("confirms",))
        out: Dict[str, Any] = {"total": len(rows), "confirmed": 0, "open": 0, "unconfirmed": []}
        for rid, ts in rows:
            if rid in confirmed:
                out["confirmed"] += 1
            elif ctx.now - aware(ts) > PLANNED_GRACE:
                out["unconfirmed"].append(rid)
            else:
                out["open"] += 1
        return out

    return ctx.cached(("planned_status", ctx.system_id), load)


@resolver("records.chain_complete")
def chain_complete(ctx: ResolverContext, spec: EvidenceSpec) -> Outcome:
    base = {**window_params(ctx), "sid": ctx.system_id}
    awaiting = ctx.db.execute(
        text(
            "SELECT record_id, timestamp, record->'context'->>'approval_request_id' "
            "FROM action_records WHERE tenant_id = :t AND ai_system_id = :sid "
            "AND verdict = 'awaiting_human' AND timestamp >= :ws AND timestamp < :we"
        ),
        base,
    ).fetchall()
    planned = planned_status(ctx)
    closed = _children(ctx, [r[0] for r in awaiting], ("supersedes",))
    expiry = _approval_expiry(ctx, [r[2] for r in awaiting])

    approval_gaps, in_flight, closed_n = [], 0, 0
    for rid, ts, req_id in awaiting:
        if rid in closed:
            closed_n += 1
            continue
        due = expiry.get(req_id) or (aware(ts) + DEFAULT_APPROVAL_TTL)
        if ctx.now > due + CHAIN_GRACE:
            approval_gaps.append(rid)
        else:
            in_flight += 1
    planned_gaps, planned_open, planned_ok = planned["unconfirmed"], planned["open"], planned["confirmed"]
    epochs = epoch_gaps(ctx)

    m = (len(awaiting) - in_flight) + (planned["total"] - planned_open) + epochs["promotions"]
    n = closed_n + planned_ok + epochs["sealed"]
    facts = {
        "approval_chains": len(awaiting),
        "approval_chains_closed": closed_n,
        "approval_chains_in_flight": in_flight,
        "approval_chain_gaps": len(approval_gaps),
        "two_phase_actions": len(planned),
        "two_phase_unconfirmed": len(planned_gaps),
        "promotions": epochs["promotions"],
        "promotions_with_epoch_boundary": epochs["sealed"],
        "promotions_missing_epoch_boundary": epochs["missing"],
    }
    citations = approval_gaps + planned_gaps + epochs["citations"]
    if m == 0:
        return Outcome(
            NOT_EVALUABLE, 0, 0,
            "No approval, two-phase or epoch chains closed in the window — nothing to check.",
            citations=citations, facts=facts,
        )
    parts = []
    if approval_gaps:
        parts.append(f"{len(approval_gaps)} approval request(s) past their TTL with no superseding record")
    if planned_gaps:
        parts.append(f"{len(planned_gaps)} planned action(s) never confirmed")
    if epochs["missing"]:
        parts.append(f"{len(epochs['missing'])} promotion(s) without an epoch boundary record")
    detail = f"{n} of {m} chains complete."
    if parts:
        detail += " Gaps: " + "; ".join(parts) + "."
    if in_flight or planned_open:
        detail += f" In flight (not counted): {in_flight + planned_open}."
    return Outcome(MET if n == m else NOT_MET, n, m, detail, citations=citations, facts=facts)


# ---------------------------------------------------------------------------
# records.integrity_verified (row chain, verified on the core)
# ---------------------------------------------------------------------------

INTACT_VERDICTS = frozenset({"verified", "pruned"})
#: Chain verification is a core round trip that hashes up to 10 000 rows;
#: an evaluation verifies at most this many chains per system.
MAX_CHAINS_VERIFIED = 10
CHAIN_VERIFY_TIMEOUT_SECONDS = 10.0
#: Chains verified at the same time. Each verify is one core request that
#: reads and hashes the chain's rows (about 0.5 ms a row: 2.5 s for a chain
#: of 5,000); the core serves them concurrently. Run one after another, the
#: ten largest chains of the demo took 9 s per system and Compliance →
#: Overview timed out.
CHAIN_VERIFY_WORKERS = 5
BAD_VERDICTS = frozenset({"gap", "broken", "forged"})


def _verify_range(tenant_id: str, chain_id: str, lo: int, hi: int) -> Dict[str, Any]:
    """The core's report for one chain range, as `chain_reports` stores it."""
    span = hi - lo + 1
    report = core_client.verify_chain(
        chain_id, from_seq=lo, limit=span, tenant_id=tenant_id,
        timeout=CHAIN_VERIFY_TIMEOUT_SECONDS,
    )
    if core_client.is_error(report):
        return {"verdict": None, "error": report.get("error"), "detail": report.get("detail")}
    return {
        "verdict": report.get("verdict"),
        "truncated": bool(report.get("truncated")) or span > core_client.MAX_CHAIN_SCAN,
        "undeclared_gaps": len(report.get("undeclared_gaps") or []),
        "pruned_rows": int(report.get("pruned_rows") or 0),
    }


def verify_ranges(ctx: ResolverContext, ranges: List[Tuple[str, int, int]]) -> Dict[Tuple[str, int, int], Dict[str, Any]]:
    """Verify chain ranges on the core, concurrently, once per evaluation.

    The same range is verified once for every system of the evaluation
    (`ctx.shared`): two systems whose rows share a chain would otherwise
    each pay for it. Rows of a chain are append-only, so a range's verdict
    is the same whichever system asked.
    """
    from concurrent.futures import ThreadPoolExecutor

    keyed = {("chain_verify", ctx.tenant_id, c, lo, hi): (c, lo, hi) for c, lo, hi in ranges}
    todo = [(k, r) for k, r in keyed.items() if k not in ctx.shared]
    if todo:
        with ThreadPoolExecutor(max_workers=min(CHAIN_VERIFY_WORKERS, len(todo))) as pool:
            results = list(pool.map(lambda kr: _verify_range(ctx.tenant_id, *kr[1]), todo))
        for (k, _), result in zip(todo, results):
            ctx.shared[k] = result
    return {r: ctx.shared[k] for k, r in keyed.items()}


def chain_reports(ctx: ResolverContext) -> List[Dict[str, Any]]:
    """Per row chain touching the system's rows in the window: the core's
    verdict over the covering sequence range. Computed once per
    evaluation (shared by `records.integrity_verified` and
    `retention.declared_prunes`)."""

    def load():
        rows = ctx.db.execute(
            text(
                "SELECT chain_id, min(chain_seq), max(chain_seq), count(*) FROM proxy_logs "
                "WHERE tenant_id = :t AND timestamp >= :ws AND timestamp < :we "
                "AND resource_group_id = ANY(:gids) GROUP BY chain_id"
            ),
            {**window_params(ctx), "gids": ctx.group_ids()},
        ).fetchall()
        out: List[Dict[str, Any]] = []
        to_verify: List[Tuple[str, int, int]] = []
        for chain_id, lo, hi, count in sorted(rows, key=lambda r: -int(r[3] or 0)):
            if chain_id is None or lo is None:
                out.append({"chain_id": None, "rows": int(count), "verdict": "unchained"})
                continue
            entry: Dict[str, Any] = {"chain_id": chain_id, "rows": int(count), "seq_start": int(lo), "seq_end": int(hi)}
            if len(to_verify) >= MAX_CHAINS_VERIFIED:
                entry.update({"verdict": None, "error": "not_checked",
                              "detail": f"only the {MAX_CHAINS_VERIFIED} largest chains are verified per evaluation"})
            else:
                to_verify.append((chain_id, int(lo), int(hi)))
            out.append(entry)
        reports = verify_ranges(ctx, to_verify)
        for entry in out:
            key = (entry.get("chain_id"), entry.get("seq_start"), entry.get("seq_end"))
            if key in reports:
                entry.update(reports[key])
        return out

    return ctx.cached(("chain_reports", ctx.system_id), load)


@resolver("records.integrity_verified")
def integrity_verified(ctx: ResolverContext, spec: EvidenceSpec) -> Outcome:
    reports = chain_reports(ctx)
    if not reports:
        return Outcome(NOT_EVALUABLE, 0, 0, "No ledger rows in the window — nothing to check.")
    intact = [r for r in reports if r.get("verdict") in INTACT_VERDICTS and not r.get("truncated")]
    bad = [r for r in reports if r.get("verdict") in BAD_VERDICTS]
    unknown = [r for r in reports if r not in intact and r not in bad]
    facts = {"chains": reports}
    n, m = len(intact), len(reports)
    if bad:
        verdicts = ", ".join(sorted({str(r["verdict"]) for r in bad}))
        return Outcome(NOT_MET, n, m, f"{len(bad)} of {m} row chains did not recompute intact ({verdicts}).",
                       facts=facts)
    if unknown:
        reasons = sorted({
            str(r.get("verdict") or r.get("error") or "unknown") + (" (truncated)" if r.get("truncated") else "")
            for r in unknown
        })
        return Outcome(
            UNDETERMINED if any(r.get("verdict") for r in unknown) else NOT_EVALUABLE,
            n, m,
            f"{n} of {m} row chains recompute intact; {len(unknown)} could not be established ({', '.join(reasons)}).",
            facts=facts,
        )
    return Outcome(MET, n, m, f"{n} of {m} row chains recompute intact (a signed checkpoint covers them, or every gap is a declared prune).",
                   facts=facts)


__all__ = [
    "ALWAYS_SEALED_SUBTYPES",
    "DENY_SUBTYPES",
    "SURFACE_BY_PROXY_TYPE",
    "chain_reports",
    "epoch_gaps",
    "inference_records_on",
    "read_only_map",
    "sealing_counts",
]
