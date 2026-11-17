# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Shared evidence statements for the framework catalogs

The point of the registry is that frameworks COMPOSE the same resolvers:
the Art 12 statement "every governed action produced a sealed record" is
the ISO A.6.2.8, SOC 2 CC7.2 and HIPAA §164.312(b) statement too, and a
report rendered under any of them must show the same row for the same
records. These builders give every catalog the same `(resolver, params)`
identity for the same statement, so the shared-resolver test can hold them
to it, and keep the honest `limit` line in one place.

Not a catalog: this module is not in `CATALOG_MODULES` and exposes no
`FRAMEWORK`. Not legal advice — provision strings are pointers into the
instrument, not interpretations of it.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, Mapping, Optional

from app.services.compliance.catalogs.base import (
    BASIS_DECLARED,
    BASIS_INDICATOR,
    BASIS_JUDGED,
    BASIS_RECOMPUTED,
    EvidenceSpec,
)

#: The crosswalk's language discipline for catalogs written from memory
#: A statement "can serve as evidence toward" a provision; it
#: never satisfies one.
CROSSWALK_PREFIX = "Can serve as evidence toward"

#: Banner line every unverified catalog repeats in its summaries.
UNVERIFIED_NOTE = (
    "Written from public summaries of the instrument; check the published text "
    "before relying on this."
)


def _params(**kwargs: Any) -> Mapping[str, Any]:
    return MappingProxyType({k: v for k, v in kwargs.items() if v is not None})


def _spec(
    key: str,
    statement: str,
    basis: str,
    resolver: str,
    limit: Optional[str],
    params: Optional[Mapping[str, Any]] = None,
) -> EvidenceSpec:
    return EvidenceSpec(
        key=key,
        statement=statement,
        basis=basis,  # type: ignore[arg-type]
        resolver=resolver,
        params=params or MappingProxyType({}),
        limit=limit,
    )


# ---------------------------------------------------------------------------
# records.* / retention.*
# ---------------------------------------------------------------------------


def every_verdict_sealed(statement: Optional[str] = None) -> EvidenceSpec:
    return _spec(
        "records.every_verdict_sealed",
        statement or "Every governed action produced a sealed record, including refusals.",
        BASIS_RECOMPUTED,
        "records.every_verdict_sealed",
        "Integrity is not completeness: covers actions routed through the gateway only; "
        "records sealed late by backfill are reported separately.",
    )


def witnessed(statement: Optional[str] = None) -> EvidenceSpec:
    return _spec(
        "records.witnessed",
        statement
        or "The tenant log's tree heads covering the window were countersigned by an independent witness.",
        BASIS_RECOMPUTED,
        "records.witnessed",
        "A receipt shows inclusion at a tree size, not a witness-observed time; a "
        "same-operator witness is self-attested.",
    )


def chain_complete(statement: Optional[str] = None) -> EvidenceSpec:
    return _spec(
        "records.chain_complete",
        statement or "Record chains are closed and epoch boundaries match the contract history.",
        BASIS_RECOMPUTED,
        "records.chain_complete",
        "Checks the chains the gateway opened; actions that never reached the gateway cannot be counted.",
    )


def integrity_checked(statement: Optional[str] = None) -> EvidenceSpec:
    return _spec(
        "records.integrity_verified",
        statement
        or "The audit row chain over the window recomputes intact, or every gap is a declared prune.",
        BASIS_RECOMPUTED,
        "records.integrity_verified",
        "Detects alteration after the fact; it cannot show that a row was accurate when written.",
    )


def retention_floor(
    days: int,
    statement: str,
    *,
    limit: Optional[str] = None,
    scope: Optional[str] = None,
) -> EvidenceSpec:
    return _spec(
        "retention.floor",
        statement,
        BASIS_RECOMPUTED,
        "retention.floor",
        limit or "Record identifiers, statements and tree heads are permanent; bodies follow retention.",
        _params(floor_days=days, scope=scope),
    )


def declared_prunes(statement: Optional[str] = None) -> EvidenceSpec:
    return _spec(
        "retention.declared_prunes",
        statement or "Every gap in the retained logs is a declared prune.",
        BASIS_RECOMPUTED,
        "retention.declared_prunes",
        "A declared prune is shown as a gap with its reason; the pruned content cannot be recovered.",
    )


# ---------------------------------------------------------------------------
# oversight.* / authz.* / access.*
# ---------------------------------------------------------------------------


def override_available(statement: Optional[str] = None) -> EvidenceSpec:
    return _spec(
        "oversight.override_available",
        statement or "A human can override or stop the system.",
        BASIS_RECOMPUTED,
        "oversight.override_available",
        "Shows the controls exist and are assigned, not that the assigned people are competent.",
    )


def oversight_exercised(statement: Optional[str] = None) -> EvidenceSpec:
    return _spec(
        "oversight.exercised",
        statement or "Human approval requests were decided by humans (requested, decided, lapsed).",
        BASIS_RECOMPUTED,
        "oversight.exercised",
        "Records the fact of human disposition, not its quality.",
    )


def not_rubber_stamp(statement: Optional[str] = None) -> EvidenceSpec:
    return _spec(
        "oversight.not_rubber_stamp",
        statement or "Approval latency and denial rate are tracked for movement.",
        BASIS_INDICATOR,
        "oversight.not_rubber_stamp",
        "An indicator of movement, never a threshold; it cannot show a decision was considered.",
    )


def out_of_grant_refused(statement: Optional[str] = None) -> EvidenceSpec:
    return _spec(
        "authz.out_of_grant_refused",
        statement or "Every action outside the contract or grant was refused.",
        BASIS_RECOMPUTED,
        "authz.out_of_grant_refused",
        "Covers actions the gateway observed; the grant itself is a declaration.",
    )


def least_privilege_declared(statement: Optional[str] = None) -> EvidenceSpec:
    return _spec(
        "authz.least_privilege_declared",
        statement or "Grants, capability filters and argument policies are bound and sealed.",
        BASIS_DECLARED,
        "authz.least_privilege_declared",
        "Whether the grants are the least the system needs is the operator's judgement, not evaluated.",
    )


def human_identity(statement: Optional[str] = None) -> EvidenceSpec:
    return _spec(
        "authz.human_identity_verified",
        statement or "Approvals were decided by authenticated principals (2FA where policy requires it).",
        BASIS_RECOMPUTED,
        "authz.human_identity_verified",
        "Authentication is of the console account; who sat at the keyboard is not observable.",
    )


def tagged_actions_recorded(tag: str, statement: str) -> EvidenceSpec:
    return _spec(
        f"access.sensitive_reads_recorded.{tag}",
        statement,
        BASIS_RECOMPUTED,
        "access.sensitive_reads_recorded",
        "Counts actions the tagging engine marked; untagged traffic is not in the denominator.",
        _params(tag=tag),
    )


# ---------------------------------------------------------------------------
# monitoring.* / change.*
# ---------------------------------------------------------------------------


def daily_health(statement: Optional[str] = None) -> EvidenceSpec:
    return _spec(
        "monitoring.daily_health",
        statement or "Liveness, drift and health were evaluated every day.",
        BASIS_RECOMPUTED,
        "monitoring.daily_health",
        "Shows the monitoring ran; what a human did with its findings is shown in the inbox trail.",
    )


def suspend_available(statement: Optional[str] = None) -> EvidenceSpec:
    return _spec(
        "monitoring.suspend_available",
        statement or "Suspension and run-abort controls are available and every use of them was sealed.",
        BASIS_RECOMPUTED,
        "monitoring.suspend_available",
        "Shows the stop controls and their use; not whether they were used when they should have been.",
    )


def promotions_sealed(statement: Optional[str] = None, *, limit: Optional[str] = None) -> EvidenceSpec:
    return _spec(
        "change.promotions_sealed",
        statement or "Every contract promotion has a sealed epoch-boundary record naming its approver.",
        BASIS_RECOMPUTED,
        "change.promotions_sealed",
        limit or "Changes made outside the contract lifecycle are not visible here.",
    )


def no_unapproved_widening(statement: Optional[str] = None) -> EvidenceSpec:
    return _spec(
        "change.no_unapproved_widening",
        statement or "No grant was widened without going through the lifecycle gate.",
        BASIS_RECOMPUTED,
        "change.no_unapproved_widening",
        "Covers configuration held by the gateway; upstream changes (a vendor's model) are shown as drift.",
    )


# ---------------------------------------------------------------------------
# notice.* / synthetic.* / incidents.* / evidence.* / docs.* / declared
# ---------------------------------------------------------------------------


def notice_before_first_turn(statement: Optional[str] = None) -> EvidenceSpec:
    return _spec(
        "notice.before_first_turn",
        statement or "People were told they were dealing with an AI before the first substantive turn.",
        BASIS_RECOMPUTED,
        "notice.before_first_turn",
        "Counts conversations through the Brutor portal (notice records); other clients "
        "must render their own notice.",
    )


def notice_text(statement: Optional[str] = None) -> EvidenceSpec:
    return _spec(
        "notice.text_declared",
        statement or "The notice text and the surface it is shown on are declared and sealed.",
        BASIS_DECLARED,
        "notice.text_declared",
        "The text is declared by the operator; whether it is understood is not evaluated.",
    )


def synthetic_marking(statement: Optional[str] = None) -> EvidenceSpec:
    return _spec(
        "synthetic.marking_declared",
        statement or "The approach to marking synthetic content is declared.",
        BASIS_DECLARED,
        "synthetic.marking_declared",
        "Content-level marking (watermarking, C2PA) is not provided; outputs carry an "
        "X-Brutor-AI-Generated header only.",
    )


def incident_register(statement: Optional[str] = None) -> EvidenceSpec:
    return _spec(
        "incidents.register_exists",
        statement or "An incident register is kept and no open incident is past its deadline.",
        BASIS_RECOMPUTED,
        "incidents.register_exists",
        "Shows incidents someone entered; an incident nobody recorded cannot be counted.",
    )


def incident_deadline(framework_id: str, statement: str, *, limit: Optional[str] = None) -> EvidenceSpec:
    return _spec(
        "incidents.reported_within_deadline",
        statement,
        BASIS_RECOMPUTED,
        "incidents.reported_within_deadline",
        limit or "Deadlines are computed and shown, not enforced; awareness time is operator-entered.",
        _params(framework=framework_id),
    )


# ---------------------------------------------------------------------------
# dependencies.* — the edge set of a composite AI System
# ---------------------------------------------------------------------------


def contributing_dependencies_assessed(statement: Optional[str] = None) -> EvidenceSpec:
    return _spec(
        "dependencies.contributing_assessed",
        statement
        or "Every dependency that contributes to this system's decisions is assessed at its tier, "
        "or carries a sealed separability rationale.",
        BASIS_RECOMPUTED,
        "dependencies.contributing_assessed",
        "Counts declared dependency edges only; an undeclared delegation is a composition finding, "
        "not a statement input. An external agent's assessment is evidenced by supplier information, "
        "not examined.",
    )


def supplier_information_on_file(statement: Optional[str] = None) -> EvidenceSpec:
    return _spec(
        "dependencies.supplier_information_on_file",
        statement
        or "Every dependency supplied by another legal entity has the supplier's written information on file.",
        BASIS_RECOMPUTED,
        "dependencies.supplier_information_on_file",
        "A pointer to the agreement, pinned by hash; its content is not evaluated.",
    )


def pointer(kind: str, statement: str, limit: Optional[str] = None) -> EvidenceSpec:
    return _spec(
        f"evidence.pointer_on_file.{kind}",
        statement,
        BASIS_RECOMPUTED,
        "evidence.pointer_on_file",
        limit or "A pointer to a document produced outside the platform; its content is not evaluated.",
        _params(kind=kind),
    )


def tech_docs(framework_id: str, statement: Optional[str] = None) -> EvidenceSpec:
    return _spec(
        "docs.technical_documentation",
        statement or "A documentation export exists for the active contract version, with its digest sealed.",
        BASIS_RECOMPUTED,
        "docs.technical_documentation",
        "A skeleton generated from the register and contract; its adequacy is not evaluated.",
        _params(framework=framework_id),
    )


def declared(key: str, path: str, statement: str, limit: str) -> EvidenceSpec:
    """A DECLARED statement over a compliance-profile field (sealed with
    every profile save, never checked)."""
    return _spec(
        key,
        statement,
        BASIS_DECLARED,
        "profile.field_declared",
        limit,
        _params(path=path),
    )


def judged(template: str, statement: str) -> EvidenceSpec:
    return _spec(
        f"judge.{template}",
        statement,
        BASIS_JUDGED,
        f"judge.{template}",
        "Judged by a model over a disclosed sample; stated with human agreement, never "
        "merged with recomputed rows.",
    )


__all__ = [
    "CROSSWALK_PREFIX",
    "UNVERIFIED_NOTE",
    "chain_complete",
    "daily_health",
    "declared",
    "declared_prunes",
    "every_verdict_sealed",
    "human_identity",
    "incident_deadline",
    "incident_register",
    "integrity_checked",
    "judged",
    "least_privilege_declared",
    "no_unapproved_widening",
    "not_rubber_stamp",
    "notice_before_first_turn",
    "notice_text",
    "out_of_grant_refused",
    "override_available",
    "oversight_exercised",
    "pointer",
    "promotions_sealed",
    "retention_floor",
    "suspend_available",
    "synthetic_marking",
    "tagged_actions_recorded",
    "tech_docs",
    "witnessed",
]
