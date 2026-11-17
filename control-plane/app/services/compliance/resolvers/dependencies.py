# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# dependencies.* — the edge set of a composite AI System

Two framework-neutral resolvers over a system's declared dependency edges:

* `dependencies.contributing_assessed` — every dependency that
  contributes to the system's decisions is itself assessed at (at least)
  the tier the system is evaluated at, or carries a SEALED separability
  rationale. A shared service is assessed by the registry at its effective
  tier; an external agent cannot be assessed inside Brutor, so the
  supplier's own assessment must be evidenced by supplier information on
  file for that edge.
* `dependencies.supplier_information_on_file` — every dependency supplied by
  someone other than the deployer (every external agent; a shared service
  with `supplier_legal_entity`) has an unexpired `supplier_information`
  pointer attached to the edge.

n of m, m = the edges the statement is about; no edges ⇒ not_evaluable
(nothing to evaluate is not a clean result). Citations are the sealed
separability declaration records; refs are the evidence pointers.
"""

from __future__ import annotations

from typing import Any, Dict, List

from app.services.compliance.catalogs.base import EvidenceSpec
from app.services.compliance.resolvers._base import (
    Outcome,
    ResolverContext,
    counted,
    resolver,
)


def _tier_facts(ctx: ResolverContext):
    from app.services.ai_system_composition import load_tier_facts

    return ctx.shared_cached(("tier_facts", ctx.tenant_id), lambda: load_tier_facts(ctx.db, ctx.tenant_id))


def _edges(ctx: ResolverContext) -> List[Any]:
    return _tier_facts(ctx).edges_of(ctx.system_id)


def _supplier_evidence(ctx: ResolverContext, edges: List[Any]) -> Dict[str, List[Any]]:
    from app.services.ai_system_composite_compliance import supplier_evidence

    return ctx.cached(
        ("supplier_evidence", ctx.system_id),
        lambda: supplier_evidence(ctx.db, ctx.tenant_id, [e.id for e in edges]),
    )


def _label(facts: Any, edge: Any) -> str:
    return facts.names.get(edge.depends_on_system_id or "") or edge.depends_on_system_id or edge.agent_card_id or edge.id


@resolver("dependencies.contributing_assessed")
def contributing_dependencies_assessed(ctx: ResolverContext, spec: EvidenceSpec) -> Outcome:
    from app.services.ai_system_composite_compliance import unexpired
    from app.services.ai_system_composition import ROLE_EXTERNAL, max_tier, tier_rank

    facts = _tier_facts(ctx)
    edges = _edges(ctx)
    evidence = _supplier_evidence(ctx, edges)
    root_tier = max_tier(facts.declared.get(ctx.system_id), facts.effective_tier(ctx.system_id))

    met: List[str] = []
    unmet: List[str] = []
    citations: List[str] = []
    refs: List[Dict[str, Any]] = []
    counts = {"assessed_at_tier": 0, "separable_sealed": 0, "external_supplier_evidenced": 0,
              "separable_unsealed": 0, "external_without_supplier_information": 0,
              "assessed_below_tier": 0}
    for e in edges:
        name = _label(facts, e)
        if not e.contributes_to_decision:
            if e.separability_record_id:
                counts["separable_sealed"] += 1
                citations.append(e.separability_record_id)
                met.append(e.id)
            else:
                counts["separable_unsealed"] += 1
                unmet.append(f"{name}: separability declared but not sealed")
            continue
        if e.role == ROLE_EXTERNAL:
            current = unexpired(evidence.get(e.id, []), ctx.now)
            if current:
                counts["external_supplier_evidenced"] += 1
                refs.extend({"type": "evidence_pointer", "id": r.id, "dependency_id": e.id} for r in current)
                met.append(e.id)
            else:
                counts["external_without_supplier_information"] += 1
                unmet.append(f"{name}: external, contributes, no supplier information on file")
            continue
        target_tier = facts.effective_tier(e.depends_on_system_id or "")
        if tier_rank(target_tier) >= tier_rank(root_tier):
            counts["assessed_at_tier"] += 1
            met.append(e.id)
        else:
            counts["assessed_below_tier"] += 1
            unmet.append(f"{name}: assessed at {target_tier or 'no tier'}, below {root_tier}")

    facts_out = {"evaluated_tier": root_tier, **counts, "unmet": unmet}
    out = counted(
        len(met), len(edges), what="dependencies contributing to this system's decisions are assessed at "
        f"{root_tier or 'its'} tier or carry a sealed separability rationale",
        facts=facts_out, citations=citations, refs=refs,
        empty="The system declares no dependencies — nothing to evaluate.",
    )
    if unmet:
        out.detail += " Not met: " + "; ".join(unmet) + "."
    return out


@resolver("dependencies.supplier_information_on_file")
def supplier_information_on_file(ctx: ResolverContext, spec: EvidenceSpec) -> Outcome:
    from app.services.ai_system_composite_compliance import supplier_required, unexpired

    facts = _tier_facts(ctx)
    edges = [e for e in _edges(ctx) if supplier_required(e)]
    evidence = _supplier_evidence(ctx, edges)
    met, missing, expired = [], [], []
    refs: List[Dict[str, Any]] = []
    for e in edges:
        rows = evidence.get(e.id, [])
        current = unexpired(rows, ctx.now)
        refs.extend(
            {"type": "evidence_pointer", "id": r.id, "dependency_id": e.id, "expired": r not in current}
            for r in rows
        )
        if current:
            met.append(e.id)
        elif rows:
            expired.append(_label(facts, e))
        else:
            missing.append(_label(facts, e))
    out = counted(
        len(met), len(edges),
        what="dependencies supplied by another legal entity have supplier information on file",
        facts={"supplied_dependencies": len(edges), "missing": missing, "expired": expired},
        refs=refs,
        empty="No dependency of this system is supplied by another legal entity — nothing to evaluate.",
    )
    if missing:
        out.detail += " Missing for: " + ", ".join(missing) + "."
    if expired:
        out.detail += " Expired for: " + ", ".join(expired) + "."
    return out


__all__ = ["contributing_dependencies_assessed", "supplier_information_on_file"]
