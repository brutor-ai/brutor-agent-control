# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Composition checks, in the assurance check library

Enforcement stays where it is (an `a2a_call` grant per agent card,
delegation depth, signed chains). These checks bind the enforcement to the
DECLARATIONS, so an owner learns when what the gateway allows, what the
ledger observed and what they declared stop agreeing:

| code | fires when |
|---|---|
| `composition.undeclared_dependency` | a run's segment names a system, or an A2A call through the gateway names a card, with no declared edge |
| `composition.unaccepted_caller` | a service serves (advisory) or refuses (enforced — `enforce_accepted_callers`) a caller not in its `intended_client_systems` |
| `composition.grant_without_edge` | a member agent's `a2a_call` grant reaches a card no declared edge covers |
| `composition.edge_without_grant` | a declared edge whose card no member agent may call (enforcement will deny it) |
| `composition.role_mismatch` | an edge declared `external` whose card is served by an AI System here |
| `composition.dependency_pin_stale` | a pinned edge whose target has since promoted a different active contract |
| `composition.declared_never_observed` | a declared shared-service edge no run exercised in the window (info) |
| `composition.dependency_cycle` | the system is on a cycle of declared shared-service edges |

## One fact, one computation

`composition_findings(index, sid)` is the ONLY implementation. The
Composition tab's `findings`, the AI Systems list's
`composition_finding_count` and the inbox items raised by
`run_composition_checks` all call it with the same tenant index. Each rule
produces exactly one finding per subject; the inbox item for a finding that
no longer occurs is resolved automatically (disposition `fixed`), so a
finding clears when the owner declares or fixes what it names.

These checks are structural — they compare declarations against grants and
observations — so they run in the CP batch tick rather than as per-run CEL
(tier A per-run checks are expressions over one run's facts; none of these
is a fact about one run).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Set

from sqlalchemy.orm import Session

from app.services.ai_system_down_rule import SEGMENT_REFUSED
from app.database.models.assurance_inbox import (
    DISPOSITION_FIXED,
    INBOX_STATE_RESOLVED,
    AssuranceInboxItem,
)
from app.database.models.resource_group import GroupType, ResourceGroup
from app.services.ai_system_composition import (
    DEFAULT_COMPOSITION_WINDOW_HOURS,
    FINDING_DECLARED_NEVER_OBSERVED,
    FINDING_DEPENDENCY_CYCLE,
    FINDING_DEPENDENCY_PIN_STALE,
    FINDING_EDGE_WITHOUT_GRANT,
    FINDING_GRANT_WITHOUT_EDGE,
    FINDING_ROLE_MISMATCH,
    FINDING_UNACCEPTED_CALLER,
    FINDING_UNDECLARED_DEPENDENCY,
    ROLE_EXTERNAL,
    ROLE_SHARED_SERVICE,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    CompositionIndex,
)

log = logging.getLogger("brutor.assurance-checks.composition")

#: Inbox category every composition finding is raised under.
INBOX_CATEGORY_COMPOSITION = "composition"
#: Who resolves an item whose finding no longer occurs.
SYSTEM_ACTOR_COMPOSITION_CHECKS = "system:composition-checks"


def _finding(code: str, severity: str, subject: Optional[str], message: str) -> Dict[str, Any]:
    return {"code": code, "severity": severity, "message": message, "subject_id": subject}


def fingerprint(sid: str, finding: Dict[str, Any]) -> str:
    """Inbox dedup key: one open item per (system, rule, subject)."""
    return f"composition:{sid}:{finding['code']}:{finding.get('subject_id') or '-'}"


def composition_findings(index: CompositionIndex, sid: str) -> List[Dict[str, Any]]:
    """Every composition finding for one system. Deterministic order."""
    out: List[Dict[str, Any]] = []
    name = index.system_name(sid) or sid
    edges = index.edges_by_system.get(sid, [])
    declared_targets = {
        e.depends_on_system_id for e in edges
        if e.role == ROLE_SHARED_SERVICE and e.depends_on_system_id
    }
    declared_cards = {e.agent_card_id for e in edges if e.agent_card_id}

    def covered(card: str) -> bool:
        """A card is covered by a declared edge naming it, or by a declared
        shared-service edge to the system the card resolves to."""
        return card in declared_cards or index.card_system.get(card) in declared_targets

    # -- undeclared dependency: observed delegation with no declared edge.
    observed_targets = index.observed_targets(sid)
    for target, st in sorted(observed_targets.items()):
        if target not in declared_targets:
            out.append(_finding(
                FINDING_UNDECLARED_DEPENDENCY, SEVERITY_WARNING, target,
                f"{name}'s runs delegated to {index.system_name(target) or target} "
                f"{st.calls} time(s) in the window, but no dependency is declared.",
            ))
    for card, calls in index.a2a_calls_from(sid):
        if covered(card):
            continue
        resolved = index.card_system.get(card)
        if resolved is not None and resolved in observed_targets:
            continue  # already one finding, on the system behind the card
        out.append(_finding(
            FINDING_UNDECLARED_DEPENDENCY, SEVERITY_WARNING, card,
            f"{name} called agent card {index.card_name(card) or card} "
            f"{calls['calls']} time(s) in the window through the gateway, but no "
            f"dependency is declared.",
        ))

    # -- unaccepted caller. Per service: advisory by
    # default — the call is served and this finding says so; with
    # `enforce_accepted_callers` the core refuses the call before it reaches
    # the service (proxy subtype `unaccepted_caller`), and this finding
    # counts the refused attempts. One finding per caller either way.
    accepted = index.accepted_callers(sid)
    enforced = index.enforces_accepted_callers(sid)
    served = index.observed_callers(sid)
    refused = index.refused_callers(sid)
    for caller in sorted((set(served) | set(refused)) - accepted):
        caller_name = index.system_name(caller) or caller
        # A refused call can also leave a segment (the core writes one when
        # the refused caller declared its own edge to the card — stored as
        # `failed` with cause `blocked_by_policy`), and the same refusal is
        # the `unaccepted_caller` proxy_logs row counted below: so "served"
        # excludes refused segments, and a caller seen in both sources is one
        # finding. Never added up. (The refusal reads as the call token
        # `blocked` — `ai_system_down_rule.segment_call_outcome`.)
        n_served = (
            served[caller].calls - served[caller].by_outcome.get(SEGMENT_REFUSED, 0)
            if caller in served else 0
        )
        n_refused = refused.get(caller, 0)
        parts = []
        if n_served:
            parts.append(f"was served by {name} in {n_served} run(s)")
        if n_refused:
            parts.append(f"had {n_refused} call(s) refused")
        mode = (
            "Enforced: the gateway refuses its calls."
            if enforced
            else "Advisory: its calls are still served — accept it as a client, "
            "or enable enforce_accepted_callers to refuse it."
        )
        out.append(_finding(
            FINDING_UNACCEPTED_CALLER, SEVERITY_WARNING, caller,
            f"{caller_name} {' and '.join(parts)} in the window but is not in "
            f"{name}'s intended client systems. {mode}",
        ))

    # -- grants bound to declarations.
    granted = index.granted_cards.get(sid, set())
    for card in sorted(granted):
        if not covered(card):
            out.append(_finding(
                FINDING_GRANT_WITHOUT_EDGE, SEVERITY_WARNING, card,
                f"A member agent of {name} may call agent card "
                f"{index.card_name(card) or card} (a2a_call grant), but no dependency "
                f"covers it — declare the edge or revoke the grant.",
            ))
    wildcard = sid in index.wildcard_a2a
    for e in edges:
        cards = _edge_cards(index, e)
        if wildcard or not cards or cards & granted:
            continue  # no card known yet cannot be judged; a wildcard reaches all
        subject = e.agent_card_id or e.depends_on_system_id
        label = (
            index.card_name(e.agent_card_id) or e.agent_card_id
            if e.agent_card_id
            else index.system_name(e.depends_on_system_id) or e.depends_on_system_id
        )
        out.append(_finding(
            FINDING_EDGE_WITHOUT_GRANT, SEVERITY_WARNING, subject,
            f"{name} declares a dependency on {label}, but no member agent holds an "
            f"a2a_call grant for it — the gateway will deny the delegation.",
        ))

    # -- declared, never exercised — only meaningful if the system ran.
    if index.own_runs.get(sid, 0) > 0:
        for e in edges:
            if (
                e.role == ROLE_SHARED_SERVICE
                and e.depends_on_system_id
                and index.pair(sid, e.depends_on_system_id).calls == 0
            ):
                out.append(_finding(
                    FINDING_DECLARED_NEVER_OBSERVED, SEVERITY_INFO, e.depends_on_system_id,
                    f"{name} declares a dependency on "
                    f"{index.system_name(e.depends_on_system_id) or e.depends_on_system_id} "
                    f"that none of its runs exercised in the window.",
                ))

    # -- declared external, but the card is an AI System here.
    for e in edges:
        if e.role == ROLE_EXTERNAL and e.agent_card_id in index.card_system:
            resolved = index.card_system[e.agent_card_id]
            out.append(_finding(
                FINDING_ROLE_MISMATCH, SEVERITY_WARNING, e.agent_card_id,
                f"Agent card {index.card_name(e.agent_card_id) or e.agent_card_id} is "
                f"declared external, but it is served by AI System "
                f"{index.system_name(resolved) or resolved} in this tenant — "
                f"declare it as a shared service.",
            ))

    # -- a pinned edge whose target has moved on.
    for e in edges:
        if e.role != ROLE_SHARED_SERVICE or not e.contract_pin or not e.depends_on_system_id:
            continue
        active = index.active_contracts.get(e.depends_on_system_id)
        if active is not None and active[0] != e.contract_pin:
            target = index.system_name(e.depends_on_system_id) or e.depends_on_system_id
            pinned_v = index.contract_versions.get(e.contract_pin)
            out.append(_finding(
                FINDING_DEPENDENCY_PIN_STALE, SEVERITY_WARNING, e.depends_on_system_id,
                f"{name} is pinned to {target}'s contract "
                f"{f'v{pinned_v}' if pinned_v is not None else e.contract_pin}, but "
                f"{target} now runs under v{active[1]} — review its diff and move the "
                f"pin, or follow the active contract.",
            ))

    if sid in index._cycles:
        succ = index._cycles[sid]
        out.append(_finding(
            FINDING_DEPENDENCY_CYCLE, SEVERITY_WARNING, succ,
            f"{name} is on a dependency cycle (next: "
            f"{index.system_name(succ) or succ}). Legitimate between services, "
            f"but liveness and health can mask each other around it, and two "
            f"systems following each other's active contract drift each other on "
            f"every promotion — pin one side.",
        ))
    return out


def _edge_cards(index: CompositionIndex, edge) -> Set[str]:
    """The agent cards an edge reaches: its own card, or for a shared service
    without one, every card a declared edge has resolved to its target."""
    if edge.agent_card_id:
        return {edge.agent_card_id}
    if edge.role == ROLE_SHARED_SERVICE and edge.depends_on_system_id:
        return index.cards_of_system(edge.depends_on_system_id)
    return set()


# ---------------------------------------------------------------------------
# Into the inbox
# ---------------------------------------------------------------------------


def run_composition_checks(
    db: Session,
    tenant_id: str,
    *,
    index: Optional[CompositionIndex] = None,
    inbox=None,
) -> Dict[str, int]:
    """Raise every current composition finding of a tenant into the inbox,
    and resolve the open items whose finding no longer occurs.

    Returns `{raised, resolved}`. Flushes; the caller commits.
    """
    from app.services.assurance_inbox import AssuranceInboxService

    inbox = inbox or AssuranceInboxService()
    index = index or CompositionIndex(
        db, tenant_id, window_hours=DEFAULT_COMPOSITION_WINDOW_HOURS
    )
    current: Set[str] = set()
    raised = 0
    for sid in sorted(index.systems):
        for f in composition_findings(index, sid):
            fp = fingerprint(sid, f)
            current.add(fp)
            item = inbox.raise_item(
                db,
                tenant_id=tenant_id,
                category=INBOX_CATEGORY_COMPOSITION,
                severity=f["severity"],
                fingerprint=fp,
                title=f["message"],
                ai_system_id=sid,
                evidence={"code": f["code"], "subject_id": f.get("subject_id")},
            )
            raised += int(item is not None)

    resolved = 0
    stale = (
        db.query(AssuranceInboxItem)
        .filter(
            AssuranceInboxItem.tenant_id == tenant_id,
            AssuranceInboxItem.category == INBOX_CATEGORY_COMPOSITION,
            AssuranceInboxItem.state != INBOX_STATE_RESOLVED,
        )
        .all()
    )
    for item in stale:
        if item.fingerprint in current:
            continue
        inbox.resolve(
            db,
            item,
            admin=SYSTEM_ACTOR_COMPOSITION_CHECKS,
            disposition=DISPOSITION_FIXED,
            note=(
                "Cleared automatically: the composition check no longer finds "
                "this (declared, accepted, granted or re-pinned)."
            ),
        )
        resolved += 1
    db.flush()
    return {"raised": raised, "resolved": resolved}


def run_composition_checks_all(db: Session) -> Dict[str, int]:
    """Every tenant that has an AI System. One failing tenant does not stop
    the rest; each tenant commits on its own."""
    totals = {"raised": 0, "resolved": 0}
    tenants = [
        t for (t,) in db.query(ResourceGroup.tenant_id)
        .filter(ResourceGroup.group_type == GroupType.AI_SYSTEM)
        .distinct()
        .all()
    ]
    for tenant_id in sorted(tenants):
        try:
            stats = run_composition_checks(db, tenant_id)
            db.commit()
        except Exception:  # noqa: BLE001 — one tenant must not stop the tick
            db.rollback()
            log.exception("composition checks failed for tenant %s", tenant_id)
            continue
        for k in totals:
            totals[k] += stats[k]
    return totals


__all__ = [
    "INBOX_CATEGORY_COMPOSITION",
    "SYSTEM_ACTOR_COMPOSITION_CHECKS",
    "composition_findings",
    "fingerprint",
    "run_composition_checks",
    "run_composition_checks_all",
]
