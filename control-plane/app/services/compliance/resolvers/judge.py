# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# judge.<template> — judged statements

A template (`no_manipulation`, `notice_clear`, `per_instructions`,
`approval_rationale`) is bound to a tenant's tier-B assurance checks by
`assurance_checks.evaluator->>'template'`. The statement reads the sealed
`ai.brutor.assessment` records of those checks for the system in the
window (`context.check_id`), counts met of assessed (n/a excluded), and
states the human agreement of the CURRENT evaluator fingerprint over the
last 30 days of graded blind reviews beside it — never folded into n / m.

Unsealed check results are not evidence: when a check produced results but
no assessment record, the statement is `not_evaluable` and says so.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import text

from app.services.compliance import agreement
from app.services.compliance.catalogs.base import EvidenceSpec
from app.services.compliance.resolvers._base import (
    MAX_CITATIONS,
    MET,
    NOT_EVALUABLE,
    NOT_MET,
    Outcome,
    ResolverContext,
    split_judge,
    window_params,
)

#: Control id an assessment record carries for a template, when present.
CONTROL_PREFIX = "ai.brutor.judge."


def _result_of(record: Dict[str, Any], template: str) -> Optional[str]:
    controls = record.get("controls") if isinstance(record, dict) else None
    if not isinstance(controls, list) or not controls:
        return None
    chosen = next((c for c in controls if isinstance(c, dict) and c.get("id") == CONTROL_PREFIX + template), None)
    chosen = chosen or next((c for c in controls if isinstance(c, dict)), None)
    result = (chosen or {}).get("result")
    return {"pass": "met", "fail": "not_met", "n/a": "n/a"}.get(result)


def bound_checks(ctx: ResolverContext, template: str) -> List[str]:
    rows = ctx.db.execute(
        text("SELECT id FROM assurance_checks WHERE tenant_id = :t AND evaluator->>'template' = :tpl"),
        {"t": ctx.tenant_id, "tpl": template},
    ).fetchall()
    return [r[0] for r in rows]


def agreement_for(ctx: ResolverContext, check_id: str, fingerprint: str) -> Dict[str, Any]:
    since = ctx.window_end - timedelta(days=agreement.AGREEMENT_WINDOW_DAYS)
    rows = ctx.db.execute(
        text(
            "SELECT judged_result, human_result, judged_score, human_score, blind "
            "FROM assurance_check_reviews WHERE tenant_id = :t AND check_id = :c "
            "AND evaluator_fingerprint = :fp AND status = 'graded' "
            "AND reviewed_at >= :since AND reviewed_at < :we"
        ),
        {"t": ctx.tenant_id, "c": check_id, "fp": fingerprint, "since": since, "we": ctx.window_end},
    ).fetchall()
    return agreement.summarize(
        categorical=[(r[0], r[1]) for r in rows],
        numeric=[(r[2], r[3]) for r in rows],
        blind=all(bool(r[4]) for r in rows) if rows else True,
    )


def judge(ctx: ResolverContext, spec: EvidenceSpec) -> Outcome:
    template = split_judge(spec.resolver)
    if template is None:
        return Outcome(NOT_EVALUABLE, detail=f"Unknown judged template {spec.resolver!r}.")
    checks = bound_checks(ctx, template)
    if not checks:
        return Outcome(NOT_EVALUABLE, detail=f"No assurance check is bound to the {template} template.",
                       facts={"template": template})
    records = ctx.db.execute(
        text(
            "SELECT record_id, record, record->'context'->>'check_id', "
            "record->'context'->>'evaluator_fingerprint', timestamp FROM action_records "
            "WHERE tenant_id = :t AND ai_system_id = :sid AND verdict = 'assessment' "
            "AND timestamp >= :ws AND timestamp < :we "
            "AND record->'context'->>'check_id' = ANY(:checks) ORDER BY timestamp DESC"
        ),
        {**window_params(ctx), "sid": ctx.system_id, "checks": checks},
    ).fetchall()
    facts: Dict[str, Any] = {"template": template, "checks": checks}
    if not records:
        unsealed = ctx.db.execute(
            text(
                "SELECT count(*) FROM assurance_check_results WHERE tenant_id = :t AND check_id = ANY(:checks) "
                "AND evaluated_at >= :ws AND evaluated_at < :we AND backtest = false"
            ),
            {**window_params(ctx), "checks": checks},
        ).scalar() or 0
        facts["unsealed_results"] = int(unsealed)
        detail = (f"{unsealed} judged result(s) exist but none is sealed as an assessment record."
                  if unsealed else "No judged assessment in the window — nothing to evaluate.")
        return Outcome(NOT_EVALUABLE, 0, 0, detail, facts=facts)

    met = not_met = na = 0
    for _rid, body, *_ in records:
        r = _result_of(body, template)
        if r == "met":
            met += 1
        elif r == "not_met":
            not_met += 1
        else:
            na += 1
    # Agreement of the evaluator that produced the latest assessment, per
    # check (a fingerprint change resets calibration).
    latest: Dict[str, str] = {}
    for _rid, _body, check_id, fp, _ts in records:
        if check_id and fp and check_id not in latest:
            latest[check_id] = fp
    agreements = {cid: agreement_for(ctx, cid, fp) for cid, fp in latest.items()}
    facts.update({"assessed": met + not_met, "not_applicable": na, "agreement": agreements})
    m = met + not_met
    citations = [r[0] for r in records[:MAX_CITATIONS]]
    if m == 0:
        return Outcome(NOT_EVALUABLE, 0, 0, f"{na} assessment(s) in the window, all not applicable.",
                       citations=citations, facts=facts)
    detail = f"Judged: met {met} of {m}."
    if agreements:
        detail += " " + "; ".join(agreement.render(a) for a in agreements.values()) + "."
    else:
        detail += " No human agreement statistic yet (assessments carry no evaluator fingerprint)."
    return Outcome(MET if met == m else NOT_MET, met, m, detail, citations=citations, facts=facts)


__all__ = ["agreement_for", "bound_checks", "judge"]
