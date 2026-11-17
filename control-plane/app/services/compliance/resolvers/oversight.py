# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# oversight.*, authz.*, access.*

Human oversight is shown from what the gateway sealed and the approvals
ledger it keeps: who can stop the system, whether humans decided what they
were asked, and — as an INDICATOR only — whether decision latency or
denial rate moved (movement, never a threshold; see
`ai_system_oversight.py` for why no absolute bound is defensible).

`oversight.exercised` is a RATE (holds decided by a human / holds raised),
so its n-of-m is graded like the health oversight component: a hold RAISED
inside a declared incident window of its AI System, or a tenant-wide one,
is not judged (`judged_holds`). The facts keep the complete
record — requested / decided / lapsed / in flight count every hold — and
state `excluded_in_incident_windows` beside them. A window only excuses
what it covers: a hold raised outside every window that lapsed counts.
Who decided (`authz.human_identity_verified`) and how many gates opened
(`override_available`) are FACTS and see every hold.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy import text

from app.constants import approvals as approval_status
from app.composition.principals import PRINCIPALS
from app.database.models.admin import TenantAdmin
from app.database.models.tenant import Tenant
from app.services.ai_system_judgement_exclusion import PURPOSE_RATE, judged_holds
from app.services.compliance.catalogs.base import EvidenceSpec
from app.services.mfa.policy import effective_mfa_policy, requires_2fa_for_admins, requires_2fa_for_end_users
from app.services.compliance.resolvers._base import (
    DECLARED,
    INDICATOR,
    MAX_CITATIONS,
    MET,
    NOT_EVALUABLE,
    NOT_MET,
    UNDETERMINED,
    Outcome,
    ResolverContext,
    aware,
    counted,
    declaration_record,
    profile_dict,
    present,
    resolver,
    window_params,
)

RUN_ABORT_PERMISSION = "run:abort"
#: Drift metric keys the oversight service publishes.
OVERSIGHT_METRICS = ("approval_latency_p50", "approval_denial_rate")
#: `resolved_by` prefixes that name a machine, not a person.
MACHINE_ACTOR_PREFIXES = ("system:", "service:", "policy:")


def abort_holders(ctx: ResolverContext) -> int:
    def load():
        admins = (
            ctx.db.query(TenantAdmin)
            .filter(TenantAdmin.tenant_id == ctx.tenant_id, TenantAdmin.is_active.is_(True))
            .all()
        )
        return sum(1 for a in admins if a.has_permission(RUN_ABORT_PERMISSION))

    return ctx.cached(("abort_holders", ctx.tenant_id), load)


def _approvals(ctx: ResolverContext) -> List[Any]:
    return ctx.cached(("approvals", ctx.system_id), lambda: ctx.db.execute(
        text(
            "SELECT id, status, resolved_by, created_at, resolved_at, expires_at, requester_group_id "
            "FROM tool_approval_requests WHERE tenant_id = :t "
            "AND requester_group_id = ANY(:gids) AND created_at >= :ws AND created_at < :we"
        ),
        {**window_params(ctx), "gids": ctx.group_ids()},
    ).fetchall())


@resolver("oversight.override_available")
def override_available(ctx: ResolverContext, spec: EvidenceSpec) -> Outcome:
    holders = abort_holders(ctx)
    prof = profile_dict(ctx)
    assignment = prof["core"].get("oversight_assignment")
    gates = len(_approvals(ctx))
    facts = {
        "run_abort_holders": holders,
        "suspend_control": True,
        "oversight_assignment_declared": present(assignment),
        "approval_requests_in_window": gates,
    }
    citations = [declaration_record(ctx)] if present(assignment) and declaration_record(ctx) else []
    n = int(holders > 0) + int(present(assignment))
    if holders == 0:
        return Outcome(NOT_MET, n, 2, "No active administrator holds run:abort — nobody can stop a run.",
                       citations=citations, facts=facts)
    if not present(assignment):
        return Outcome(
            UNDETERMINED, n, 2,
            f"{holders} administrator(s) can stop runs and the system can be suspended; the oversight "
            f"assignment is not declared on the compliance profile.",
            citations=citations, facts=facts,
        )
    return Outcome(
        MET, n, 2,
        f"{holders} administrator(s) can stop runs, the system can be suspended, and the oversight "
        f"assignment is declared.",
        citations=citations, facts=facts,
    )


#: `action_records.epoch` for a record sealed under a contract (core:
#: `evidence/mint.rs::system_of`).
CONTRACT_EPOCH_PREFIX = "contract:"


def _is_human(resolved_by: Optional[str]) -> bool:
    return bool(resolved_by) and not str(resolved_by).startswith(MACHINE_ACTOR_PREFIXES)


@resolver("oversight.exercised")
def exercised(ctx: ResolverContext, spec: EvidenceSpec) -> Outcome:
    rows = _approvals(ctx)
    requested = len(rows)
    now = ctx.now
    # One vocabulary (approval_status): `consumed` is a human approval, and a
    # `pending` row past its deadline has lapsed.
    state = {r[0]: approval_status.decision(r[1], r[5], now) for r in rows}
    pending = sum(1 for r in rows if state[r[0]] == approval_status.PENDING)
    lapsed = sum(1 for r in rows if state[r[0]] == approval_status.EXPIRED)
    decided = [r for r in rows if approval_status.is_decided(r[1])]
    by_human = sum(1 for r in decided if _is_human(r[2]))
    # The n-of-m is graded over holds raised outside every
    # declared incident window; the counts above are the complete record.
    flags = judged_holds(ctx.db, ctx.tenant_id, [(r[6], r[3]) for r in rows], purpose=PURPOSE_RATE)
    graded = [r for r, keep in zip(rows, flags) if keep]
    excluded = requested - len(graded)
    g_pending = sum(1 for r in graded if state[r[0]] == approval_status.PENDING)
    g_by_human = sum(1 for r in graded if approval_status.is_decided(r[1]) and _is_human(r[2]))
    citations = [r[0] for r in ctx.db.execute(
        text(
            "SELECT record_id FROM action_records WHERE tenant_id = :t AND ai_system_id = :sid "
            "AND verdict IN ('awaiting_human','resolved','expired') "
            "AND timestamp >= :ws AND timestamp < :we ORDER BY timestamp DESC LIMIT :lim"
        ),
        {**window_params(ctx), "sid": ctx.system_id, "lim": MAX_CITATIONS},
    ).fetchall()]
    facts = {"requested": requested, "decided": len(decided), "decided_by_human": by_human,
             "lapsed": lapsed, "in_flight": pending, "excluded_in_incident_windows": excluded}
    m = len(graded) - g_pending
    not_judged = f" {excluded} inside incident windows, not judged." if excluded else ""
    if requested == 0:
        return Outcome(NOT_EVALUABLE, 0, 0, "No approval was requested in the window — nothing to evaluate.",
                       facts=facts, citations=citations)
    if not graded:
        return Outcome(
            NOT_EVALUABLE, 0, 0,
            f"All {requested} approval request(s) were raised inside declared incident windows: recorded, "
            f"not judged.",
            facts=facts, citations=citations,
        )
    if m == 0:
        return Outcome(NOT_EVALUABLE, 0, 0, f"{g_pending} approval request(s) still in flight.{not_judged}",
                       facts=facts, citations=citations)
    detail = f"Requested {requested}, decided by a human {by_human}, lapsed {lapsed}."
    if pending:
        detail += f" In flight: {pending}."
    detail += not_judged
    return Outcome(MET if g_by_human == m else NOT_MET, g_by_human, m, detail, citations=citations, facts=facts)


@resolver("oversight.not_rubber_stamp")
def not_rubber_stamp(ctx: ResolverContext, spec: EvidenceSpec) -> Outcome:
    base = {"t": ctx.tenant_id, "sid": ctx.system_id, "keys": list(OVERSIGHT_METRICS)}
    baselines = ctx.db.execute(
        text(
            "SELECT metric_key, state, sample_count FROM ai_system_baselines "
            "WHERE tenant_id = :t AND ai_system_id = :sid AND metric_key = ANY(:keys)"
        ),
        base,
    ).fetchall()
    events = ctx.db.execute(
        text(
            "SELECT id, metric_key, direction, severity, detected_at FROM ai_system_drift_events "
            "WHERE tenant_id = :t AND ai_system_id = :sid AND metric_key = ANY(:keys) "
            "AND detected_at >= :ws AND detected_at < :we ORDER BY detected_at DESC"
        ),
        {**base, "ws": ctx.window_start, "we": ctx.window_end},
    ).fetchall()
    facts = {
        "baselines": [{"metric_key": b[0], "state": b[1], "samples": b[2]} for b in baselines],
        "movement_events": [
            {"id": e[0], "metric_key": e[1], "direction": e[2], "severity": e[3],
             "detected_at": aware(e[4]).isoformat() if e[4] else None}
            for e in events
        ],
    }
    refs = [{"type": "drift_event", "id": e[0]} for e in events]
    if not baselines and not events:
        return Outcome(NOT_EVALUABLE, detail="No approval latency or denial-rate baseline exists yet — "
                                             "movement cannot be observed.", facts=facts)
    if events:
        detail = (f"Movement observed: {len(events)} drift finding(s) on approval latency or denial rate. "
                  f"An indicator for review, not a finding against a threshold.")
    else:
        ready = [b for b in baselines if b[1] == "ready"]
        detail = ("No movement in approval latency or denial rate against the learned baseline."
                  if ready else "Baselines are still learning; movement is not yet observable.")
    return Outcome(INDICATOR, detail=detail, facts=facts, refs=refs)


# ---------------------------------------------------------------------------
# authz.*
# ---------------------------------------------------------------------------


def _contract_history(ctx: ResolverContext) -> List[Any]:
    return ctx.db.execute(
        text(
            "SELECT version, contract_hash, promoted_at FROM ai_system_contracts "
            "WHERE tenant_id = :t AND ai_system_id = :sid AND promoted_at IS NOT NULL "
            "ORDER BY promoted_at"
        ),
        {"t": ctx.tenant_id, "sid": ctx.system_id},
    ).fetchall()


def contract_in_force(history: List[Any], at) -> Optional[str]:
    """The hash of the contract promoted most recently at or before `at`."""
    current = None
    for _version, chash, promoted_at in history:
        if aware(promoted_at) <= at:
            current = chash
        else:
            break
    return current


@resolver("authz.out_of_grant_refused")
def out_of_grant_refused(ctx: ResolverContext, spec: EvidenceSpec) -> Outcome:
    params = {**window_params(ctx), "gids": ctx.group_ids()}
    attempts = ctx.db.execute(
        text(
            "SELECT p.id, p.record_id, a.verdict FROM proxy_logs p "
            "LEFT JOIN action_records a ON a.record_id = p.record_id "
            "WHERE p.tenant_id = :t AND p.timestamp >= :ws AND p.timestamp < :we "
            "AND p.resource_group_id = ANY(:gids) "
            "AND (p.agent_authz_decision = 'deny' "
            "     OR p.proxy_subtype IN ('agent_authz_block','access_denied','semantic_policy_block'))"
        ),
        params,
    ).fetchall()
    refused = [a for a in attempts if a[2] == "denied"]
    history = _contract_history(ctx)
    # The contract a record names, read from the `epoch` column: the core
    # writes it as "contract:<hash>" from the same attribution that fills the
    # body's `system.contract_hash` (mint.rs `system_of`), so this is the same
    # fact without de-toasting every record body (1 s per system on 33,000
    # records).
    records = ctx.db.execute(
        text(
            "SELECT record_id, timestamp, substr(epoch, :plen) FROM action_records "
            "WHERE tenant_id = :t AND ai_system_id = :sid AND timestamp >= :ws AND timestamp < :we "
            "AND epoch LIKE :prefix"
        ),
        {**window_params(ctx), "sid": ctx.system_id,
         "prefix": CONTRACT_EPOCH_PREFIX + "%", "plen": len(CONTRACT_EPOCH_PREFIX) + 1},
    ).fetchall()
    mismatched = [r[0] for r in records if contract_in_force(history, aware(r[1])) not in (None, r[2])]
    matched = len(records) - len(mismatched)
    facts = {
        "out_of_grant_attempts": len(attempts),
        "out_of_grant_sealed_denied": len(refused),
        "records_with_contract": len(records),
        "records_on_active_contract": matched,
    }
    if not attempts and not records:
        return Outcome(NOT_EVALUABLE, 0, 0,
                       "No sealed actions and no out-of-grant attempts in the window — nothing to evaluate.",
                       facts=facts)
    problems = []
    if len(refused) < len(attempts):
        problems.append(f"{len(attempts) - len(refused)} out-of-grant attempt(s) without a sealed denial")
    if mismatched:
        problems.append(f"{len(mismatched)} record(s) name a contract other than the one in force")
    detail = (f"{len(refused)} of {len(attempts)} out-of-grant attempts sealed as denied; "
              f"{matched} of {len(records)} records name the contract in force.")
    if problems:
        detail += " " + "; ".join(problems) + "."
    return Outcome(
        NOT_MET if problems else MET,
        len(refused) + matched,
        len(attempts) + len(records),
        detail,
        citations=mismatched[:MAX_CITATIONS] + [a[1] for a in refused if a[1]][:MAX_CITATIONS],
        facts=facts,
    )


@resolver("authz.least_privilege_declared")
def least_privilege_declared(ctx: ResolverContext, spec: EvidenceSpec) -> Outcome:
    contract = ctx.db.execute(
        text(
            "SELECT id, version, contract_hash, approved_by FROM ai_system_contracts "
            "WHERE tenant_id = :t AND ai_system_id = :sid AND status = 'active' "
            "ORDER BY version DESC LIMIT 1"
        ),
        {"t": ctx.tenant_id, "sid": ctx.system_id},
    ).first()
    decl = declaration_record(ctx)
    facts = {"active_contract_version": contract[1] if contract else None,
             "contract_human_approval": bool(contract and contract[3]),
             "declaration_sealed": bool(decl)}
    refs = [{"type": "contract", "id": contract[0], "hash": contract[2]}] if contract else []
    if contract is None:
        return Outcome(UNDETERMINED, detail="No active contract binds the system's grants — nothing is declared.",
                       facts=facts)
    if not decl:
        return Outcome(UNDETERMINED,
                       detail=f"Grants are bound by contract v{contract[1]}, but no sealed profile declaration "
                              f"exists to cite.", facts=facts, refs=refs)
    return Outcome(
        DECLARED,
        detail=f"Grants, filters and argument policies bound by contract v{contract[1]}; declaration sealed.",
        citations=[decl], facts=facts, refs=refs,
    )


def _principal(ctx: ResolverContext, resolved_by: str):
    """(kind, totp_enabled) for a resolver id, or None when it names no
    authenticated principal of this tenant."""
    # An administrator of any registered principal: one pinned to
    # a tenant counts for its own tenant only, a platform-wide one for all.
    for principal in PRINCIPALS.specs():
        if principal.find_admin is None:
            continue
        admin = principal.find_admin(ctx.db, resolved_by)
        if admin is None:
            continue
        if principal.platform_wide or getattr(admin, "tenant_id", None) == ctx.tenant_id:
            return "admin", bool(admin.totp_enabled)
    if str(resolved_by).isdigit():
        row = ctx.db.execute(
            text("SELECT totp_enabled FROM end_users WHERE id = :i AND tenant_id = :t"),
            {"i": int(resolved_by), "t": ctx.tenant_id},
        ).first()
        if row is not None:
            return "user", bool(row[0])
    return None


@resolver("authz.human_identity_verified")
def human_identity_verified(ctx: ResolverContext, spec: EvidenceSpec) -> Outcome:
    decided = [r for r in _approvals(ctx) if approval_status.is_decided(r[1])]
    tenant = ctx.db.get(Tenant, ctx.tenant_id)
    # One reading of the stored value (unrecognised means everyone), the same
    # one sign-in enforces.
    policy = effective_mfa_policy(tenant.mfa_policy if tenant is not None else None)
    ok, unknown, no_mfa = 0, 0, 0
    refs: List[Dict[str, Any]] = []
    for rid, _status, resolved_by, *_ in decided:
        principal = _principal(ctx, resolved_by) if _is_human(resolved_by) else None
        if principal is None:
            unknown += 1
            refs.append({"type": "approval_request", "id": rid, "issue": "not an authenticated principal"})
            continue
        kind, totp = principal
        needs_mfa = requires_2fa_for_end_users(policy) or (requires_2fa_for_admins(policy) and kind == "admin")
        if needs_mfa and not totp:
            no_mfa += 1
            refs.append({"type": "approval_request", "id": rid, "issue": "2FA required by policy, not enabled"})
            continue
        ok += 1
    facts = {"mfa_policy": policy, "not_authenticated_principal": unknown, "missing_required_2fa": no_mfa}
    return counted(ok, len(decided), what="approval decisions were made by authenticated principals"
                                           " (2FA where the tenant policy requires it)",
                   facts=facts, refs=refs,
                   empty="No approval was decided in the window — nothing to evaluate.")


# ---------------------------------------------------------------------------
# access.sensitive_reads_recorded
# ---------------------------------------------------------------------------

RG_FLAG_TAGS = {"phi_resource", "sensitive_data"}


@resolver("access.sensitive_reads_recorded")
def sensitive_reads_recorded(ctx: ResolverContext, spec: EvidenceSpec) -> Outcome:
    tag = str(spec.params.get("tag") or "")
    if not tag:
        return Outcome(NOT_EVALUABLE, detail="The statement names no tag.")
    params = {**window_params(ctx), "gids": ctx.group_ids(), "tag": tag}
    if tag in RG_FLAG_TAGS:
        where = f"COALESCE((to_jsonb(rg)->>'{tag}')::boolean, false)"
    else:
        where = "(jsonb_typeof(p.compliance_tags::jsonb) = 'array' AND p.compliance_tags::jsonb ? :tag)"
    row = ctx.db.execute(
        text(
            "SELECT count(*), count(p.record_id) FROM proxy_logs p "
            "LEFT JOIN resource_groups rg ON rg.id = p.resource_group_id "
            "WHERE p.tenant_id = :t AND p.timestamp >= :ws AND p.timestamp < :we "
            f"AND p.resource_group_id = ANY(:gids) AND {where}"
        ),
        params,
    ).first()
    m, n = int(row[0] or 0), int(row[1] or 0)
    return counted(n, m, what=f"actions tagged {tag} carry a sealed record",
                   facts={"tag": tag},
                   empty=f"No actions tagged {tag} in the window — nothing to evaluate.")


__all__ = [
    "abort_holders",
    "contract_in_force",
    "exercised",
    "human_identity_verified",
    "least_privilege_declared",
    "not_rubber_stamp",
    "out_of_grant_refused",
    "override_available",
    "sensitive_reads_recorded",
]
