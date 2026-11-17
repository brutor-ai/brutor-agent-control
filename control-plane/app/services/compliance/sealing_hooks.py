# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Sealing hooks for governed acts

* **declaration** (`ai.brutor.declaration`, verdict `recorded`) — every
  compliance-profile save; the profile JSON enters the record only as a
  payload digest.
* **epoch boundary** (`ai.brutor.epoch-boundary`, verdict
  `epoch_boundary`) — every contract promotion. The core attaches the
  (new) active contract's hash as the `ai.brutor.ai-system-contract`
  reference and the epoch; the approver is `human` when the contract
  carries a human approval, else `policy`.
* **separability** (`ai.brutor.declaration`, verdict `recorded`, context
  state `separability`, authority = the dependency edge id):
  a dependency declared NOT to contribute to the depending system's
  decision. The claim (edge, target, rationale) enters only as a payload
  digest; the record id is kept on the edge and cited by the obligation
  registry.
* **drift resolution** (`ai.brutor.declaration`, verdict `recorded`,
  context state `drift_resolution`, authority = the epoch / window id) —
  A human closing a drift finding with a kind, and the one
  effect it wrote (a baseline epoch or an incident window). The note enters
  only as a payload digest.
* **incident window** (`ai.brutor.declaration`, context state
  `incident_window`, payload `change` declared | updated | withdrawn,
  authority = the window id); a tenant-wide window names no
  AI System.
* **run abort** (`ai.brutor.run-abort`, verdict `resolved`, disposition
  reject / human / true, authority = the intervention id) — every operator
  abort. The reason is free text: digest only.

**Best-effort, never silent.** A hook never fails the act it records — a
promotion or an abort stands when the core is unreachable. The failure is
logged and raised as a `capture_gap` inbox item (fingerprint
`capture_gap:cp_seal:<record_type>:<system>`), which the Compliance
Overview counts, so an unsealed act is a visible finding, not a missing row.

Never-enters: no admin id, user id or e-mail goes into a record.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app.services.compliance import sealing
from app.services.compliance.sealing import SealOutcome

log = logging.getLogger("brutor.compliance.sealing")

CAPTURE_GAP_PREFIX = "capture_gap:cp_seal:"


def raise_capture_gap(db: Session, tenant_id: str, ai_system_id: Optional[str], record_type: str,
                      subject_id: Optional[str], error: Optional[str]) -> None:
    """Flushes (never commits) an inbox item; never raises."""
    try:
        from app.services.assurance_inbox import AssuranceInboxService

        AssuranceInboxService().raise_item(
            db,
            tenant_id=tenant_id,
            category="capture_gap",
            severity="warning",
            fingerprint=f"{CAPTURE_GAP_PREFIX}{record_type}:{ai_system_id or '-'}",
            title=f"A control-plane record could not be sealed ({record_type}: {error or 'error'})",
            ai_system_id=ai_system_id,
            evidence={"record_type": record_type, "subject_id": subject_id, "error": error},
        )
    except Exception:  # noqa: BLE001
        log.exception("could not raise capture_gap for %s", record_type)


def _seal(db: Session, tenant_id: str, record: Dict[str, Any], subject_id: Optional[str]) -> SealOutcome:
    try:
        outcome = sealing.seal_many(tenant_id, [record])[0]
    except Exception as exc:  # noqa: BLE001 — never fail the act being recorded
        outcome = SealOutcome(error="internal", detail=str(exc)[:300])
    if not outcome.sealed:
        raise_capture_gap(db, tenant_id, record.get("ai_system_id"), record["record_type"], subject_id,
                          outcome.error)
    return outcome


def seal_declaration(db: Session, tenant_id: str, ai_system_id: str, payload: Dict[str, Any], *,
                     framework_id: Optional[str] = None) -> SealOutcome:
    record = sealing.build_record(
        sealing.RECORD_DECLARATION,
        verdict="recorded",
        kind="fyi",
        ai_system_id=ai_system_id,
        disposition={"decision": "accept", "approver": "human", "human_disposed": True},
        context={"framework_id": framework_id, "state": "compliance_profile"},
        payload=payload,
    )
    return _seal(db, tenant_id, record, ai_system_id)


#: `context.state` of a separability declaration.
STATE_SEPARABILITY = "separability"


def separability_payload(edge: Any) -> Dict[str, Any]:
    """What a separability declaration seals: the claim, not the edge row.
    Changing any of these is a new claim and needs a new record."""
    return {
        "dependency_id": edge.id,
        "ai_system_id": edge.ai_system_id,
        "depends_on_system_id": edge.depends_on_system_id,
        "agent_card_id": edge.agent_card_id,
        "contributes_to_decision": False,
        "separability_rationale": edge.separability_rationale,
    }


def seal_separability(db: Session, edge: Any) -> SealOutcome:
    """Seal a dependency's separability claim. The record is
    attributed to the DEPENDING system, whose owner makes the claim."""
    record = sealing.build_record(
        sealing.RECORD_DECLARATION,
        verdict="recorded",
        kind="fyi",
        ai_system_id=edge.ai_system_id,
        disposition={
            "decision": "accept",
            "approver": "human",
            "human_disposed": True,
            # The edge id is an opaque config handle, not an identity.
            "authority": edge.id,
        },
        context={"state": STATE_SEPARABILITY},
        payload=separability_payload(edge),
    )
    return _seal(db, edge.tenant_id, record, edge.id)


def seal_epoch_boundary(db: Session, contract: Any) -> SealOutcome:
    """Call after the promotion is committed, so the core attributes the
    record to the new epoch."""
    human = bool(getattr(contract, "approved_by", None))
    record = sealing.build_record(
        sealing.RECORD_EPOCH_BOUNDARY,
        verdict="epoch_boundary",
        kind="fyi",
        ai_system_id=contract.ai_system_id,
        disposition={
            "decision": "accept",
            "approver": "human" if human else "policy",
            "human_disposed": human,
            # The contract id is an opaque config handle, not an identity.
            "authority": contract.id,
        },
        context={"contract_version": str(contract.version)},
    )
    return _seal(db, contract.tenant_id, record, contract.id)


def seal_run_abort(db: Session, intervention: Any, ai_system_id: Optional[str]) -> SealOutcome:
    record = sealing.build_record(
        sealing.RECORD_RUN_ABORT,
        verdict="resolved",
        kind="decide",
        ai_system_id=ai_system_id,
        disposition={
            "decision": "reject",
            "approver": "human",
            "human_disposed": True,
            "authority": intervention.id,
        },
        controls=[{"id": "ai.brutor.run_abort", "result": "pass", "blocking": True}],
        context={"root_task_id": intervention.root_task_id, "intervention_id": intervention.id},
        payload={"reason": intervention.reason},
    )
    return _seal(db, intervention.tenant_id, record, intervention.id)


#: `context.state` of a drift resolution and of an incident
#: window declared, edited or withdrawn.
STATE_DRIFT_RESOLUTION = "drift_resolution"
STATE_INCIDENT_WINDOW = "incident_window"

#: What happened to an incident window (payload `change`).
WINDOW_DECLARED = "declared"
WINDOW_UPDATED = "updated"
WINDOW_WITHDRAWN = "withdrawn"


def _iso(dt: Any) -> Optional[str]:
    return dt.isoformat() if dt is not None else None


def drift_resolution_payload(event: Any, effect: Any, *, attached: bool = False) -> Dict[str, Any]:
    """What a resolution seals: the finding, the kind, and the one effect it
    has on detection (a baseline epoch or an incident window). The note
    enters only through the payload digest."""
    from app.database.models.ai_system_drift_resolution import AISystemIncidentWindow

    body: Dict[str, Any] = {
        "drift_event_id": event.id,
        "ai_system_id": event.ai_system_id,
        "drift_class": event.drift_class,
        "metric_key": event.metric_key,
        "basis": event.basis,
        "caller_system_id": event.caller_system_id,
        "resolution_kind": event.resolution_kind,
        "resolved_at": _iso(event.resolved_at),
        "note": event.resolution_note,
    }
    if isinstance(effect, AISystemIncidentWindow):
        # `attached`: the finding closed against an existing window (one
        # incident, many identical findings) rather than declaring one.
        body["effect"] = {"incident_window": incident_window_payload(effect), "attached": attached}
    else:
        body["effect"] = {
            "baseline_epoch": {
                "id": effect.id,
                "metric_key": effect.metric_key,
                "basis": effect.basis,
                "caller_system_id": effect.caller_system_id,
                "starts_at": _iso(effect.starts_at),
                "kind": effect.kind,
            }
        }
    return body


def incident_window_payload(window: Any) -> Dict[str, Any]:
    return {
        "id": window.id,
        "ai_system_id": window.ai_system_id,
        "resource_type": window.resource_type,
        "resource_id": window.resource_id,
        "starts_at": _iso(window.starts_at),
        "ends_at": _iso(window.ends_at),
        "cause_category": window.cause_category,
        "reason": window.reason,
        "drift_event_id": window.drift_event_id,
    }


def seal_drift_resolution(db: Session, event: Any, effect: Any, *, attached: bool = False) -> SealOutcome:
    """A human closing a drift finding with a kind is a sealed
    declaration, attributed to the finding's system. Authority = the
    effect's id (an opaque config handle, never an identity)."""
    record = sealing.build_record(
        sealing.RECORD_DECLARATION,
        verdict="recorded",
        kind="fyi",
        ai_system_id=event.ai_system_id,
        disposition={
            "decision": "accept",
            "approver": "human",
            "human_disposed": True,
            "authority": effect.id,
        },
        context={"state": STATE_DRIFT_RESOLUTION},
        payload=drift_resolution_payload(event, effect, attached=attached),
    )
    return _seal(db, event.tenant_id, record, event.id)


def seal_incident_window(db: Session, window: Any, change: str) -> SealOutcome:
    """Declaring, editing or withdrawing an incident window is
    a sealed declaration. A tenant-wide window names no AI System."""
    record = sealing.build_record(
        sealing.RECORD_DECLARATION,
        verdict="recorded",
        kind="fyi",
        ai_system_id=window.ai_system_id,
        disposition={
            "decision": "accept",
            "approver": "human",
            "human_disposed": True,
            "authority": window.id,
        },
        context={"state": STATE_INCIDENT_WINDOW},
        payload={"change": change, "incident_window": incident_window_payload(window)},
    )
    return _seal(db, window.tenant_id, record, window.id)


def nearest_ai_system(db: Session, tenant_id: str, group_id: Optional[str]) -> Optional[str]:
    """The nearest AI System at or above `group_id` (the core's
    `nearest_ai_system`)."""
    if not group_id:
        return None
    from sqlalchemy import text

    try:
        return _nearest(db, tenant_id, group_id, text)
    except Exception:  # noqa: BLE001 — attribution must never fail the act being sealed
        log.exception("nearest AI System lookup failed for group %s", group_id)
        return None


def _nearest(db: Session, tenant_id: str, group_id: str, text) -> Optional[str]:
    row = db.execute(
        text(
            """
            WITH RECURSIVE chain AS (
                SELECT id, parent_group_id, CAST(group_type AS TEXT) AS gt, 0 AS depth
                  FROM resource_groups WHERE id = :g AND tenant_id = :t
                UNION ALL
                SELECT rg.id, rg.parent_group_id, CAST(rg.group_type AS TEXT), c.depth + 1
                  FROM resource_groups rg JOIN chain c ON rg.id = c.parent_group_id
                 WHERE rg.tenant_id = :t AND c.depth < 32
            )
            SELECT id FROM chain WHERE UPPER(gt) = 'AI_SYSTEM' ORDER BY depth LIMIT 1
            """
        ),
        {"g": group_id, "t": tenant_id},
    ).first()
    return row[0] if row else None


__all__ = [
    "CAPTURE_GAP_PREFIX",
    "nearest_ai_system",
    "raise_capture_gap",
    "seal_declaration",
    "seal_epoch_boundary",
    "seal_run_abort",
    "seal_separability",
    "separability_payload",
    "STATE_SEPARABILITY",
    "STATE_DRIFT_RESOLUTION",
    "STATE_INCIDENT_WINDOW",
    "WINDOW_DECLARED",
    "WINDOW_UPDATED",
    "WINDOW_WITHDRAWN",
    "drift_resolution_payload",
    "incident_window_payload",
    "seal_drift_resolution",
    "seal_incident_window",
]
