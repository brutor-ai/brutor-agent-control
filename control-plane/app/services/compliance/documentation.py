# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Documentation export — a versioned skeleton, not a claim

Renders a framework's documentation template for one AI System from what
the platform actually holds: the AI System register entry, the active
contract (version, hash, approver, composition), the compliance profile,
the bindings, the evidence pointers on file, the latest assurance report
and the latest Evidence Report. Templates:

* `eu-annex-iv` — EU AI Act Annex IV technical documentation (Art 11)
* `iso-a6-2-7` — ISO/IEC 42001 A.6.2.7 technical documentation / A.8.2
  information for users
* `soc2-system-description` — SOC 2 system description (DC 200 headings)
* `generic` — every other framework

Each section separates what the platform FILLED (with the source it came
from) from what the operator must still COMPLETE. The document says, in
its own text, that it is a skeleton generated from configuration and not
a statement that any requirement is met.

`content` is deterministic for the same inputs — no timestamps inside it —
so its digest (SHA-256 of the canonical JSON) is stable: re-requesting an
unchanged document returns the existing export instead of a new row and a
new record. A changed input produces a new export, a new digest and a new
`ai.brutor.documentation` record referencing it.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session
from ulid import ULID

from app.database.models.ai_system_compliance_profile import AISystemComplianceProfile
from app.database.models.ai_system_evidence import AISystemEvidence
from app.database.models.compliance_documentation_export import ComplianceDocumentationExport
from app.database.models.compliance_report import ComplianceReport
from app.database.models.resource_group import ResourceGroup
from app.services.compliance import sealing
from app.services.compliance import offering

TEMPLATE_VERSION = "1.0"

SKELETON_NOTICE = (
    "This document is a skeleton generated from the platform's configuration and records. "
    "It is not a statement that any requirement is met, and it is not legal advice. Sections "
    "marked 'to complete' need the operator's own content before the document can be used."
)

TEMPLATES = {
    "eu-ai-act": "eu-annex-iv",
    "iso-42001": "iso-a6-2-7",
    "soc2": "soc2-system-description",
}


class DocumentationError(Exception):
    def __init__(self, message: str, status_code: int = 422):
        super().__init__(message)
        self.status_code = status_code


@dataclass
class DocumentationExport:
    export: ComplianceDocumentationExport
    seal: Optional[sealing.SealOutcome]
    reused: bool


def template_for(framework_id: str) -> str:
    return TEMPLATES.get(framework_id, "generic")


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


def _names(items: Any) -> List[Dict[str, Any]]:
    out = []
    for item in items or []:
        if isinstance(item, dict):
            out.append({k: item.get(k) for k in ("id", "name", "bound_via", "inherited") if k in item})
        else:
            out.append({"id": str(item)})
    return sorted(out, key=lambda d: str(d.get("id")))


def _inputs(db: Session, system: ResourceGroup, framework_id: str) -> Dict[str, Any]:
    from app.database.models.ai_system_health import AISystemAssuranceReport
    from app.services.ai_system_closure import resolve_closure
    from app.services.ai_system_contract_service import current_contract

    tenant_id = system.tenant_id
    contract = current_contract(db, tenant_id, system.id)
    if contract is not None:
        closure = contract.resolved_closure or {}
        closure_source = f"active contract v{contract.version}"
    else:
        closure = resolve_closure(db, system)
        closure_source = "live configuration (no active contract)"
    composition = closure.get("composition") or {}

    profile = db.get(AISystemComplianceProfile, system.id)
    if profile is not None and profile.tenant_id != tenant_id:
        profile = None

    evidence = (
        db.query(AISystemEvidence)
        .filter(AISystemEvidence.tenant_id == tenant_id, AISystemEvidence.ai_system_id == system.id)
        .order_by(AISystemEvidence.kind, AISystemEvidence.id)
        .all()
    )
    report = (
        db.query(AISystemAssuranceReport)
        .filter(
            AISystemAssuranceReport.tenant_id == tenant_id,
            AISystemAssuranceReport.ai_system_id == system.id,
        )
        .order_by(AISystemAssuranceReport.created_at.desc())
        .first()
    )
    # The declared dependencies, as compliance documents
    # them (the same register the assurance report's "Composition and
    # dependencies" section renders).
    from app.services.ai_system_composite_compliance import dependency_register

    dependencies = dependency_register(db, system)

    evidence_report = (
        db.query(ComplianceReport)
        .filter(
            ComplianceReport.tenant_id == tenant_id,
            ComplianceReport.framework_id == framework_id,
            ComplianceReport.ai_system_id == system.id,
            ComplianceReport.cadence == "monthly",
        )
        .order_by(ComplianceReport.period_start.desc())
        .first()
    )
    return {
        "system": {
            "id": system.id,
            "name": system.display_name or system.name,
            "system_kind": system.system_kind,
            "owner": system.owner,
            "intended_use": system.intended_use,
            "intended_client_labels": system.intended_client_labels,
            "intended_client_systems": system.intended_client_systems,
            "lifecycle_stage": system.lifecycle_stage,
            "autonomy_level": system.autonomy_level,
            "eu_ai_act_role": system.eu_ai_act_role,
            "eu_ai_act_risk_tier": system.eu_ai_act_risk_tier,
            "description": system.description,
        },
        "contract": (
            {
                "version": contract.version,
                "hash": contract.contract_hash,
                "approved_by": contract.approved_by,
                "promoted_at": contract.promoted_at.isoformat() if contract.promoted_at else None,
            }
            if contract is not None
            else None
        ),
        "composition_source": closure_source,
        "composition": {
            "llm_models": _names(composition.get("llm_models")),
            "mcp_servers": _names(composition.get("mcp_servers")),
            "skills": _names(composition.get("skills")),
            "agent_identities": _names(composition.get("agent_identities")),
            "guardrails": _names(composition.get("guardrails")),
            "argument_policies": _names(composition.get("argument_policies")),
        },
        # Components are the system's own member agents;
        # dependencies are the other systems and external agents it relies on.
        "components": _names(composition.get("agent_identities")),
        "dependencies": dependencies,
        "autonomy_level": closure.get("autonomy_level"),
        "limits": closure.get("limits") or {},
        "operating_envelope": closure.get("operating_envelope"),
        "profile": (
            {"core": profile.core or {}, "frameworks": profile.frameworks or {},
             "declaration_record_id": profile.declaration_record_id}
            if profile is not None
            else None
        ),
        "evidence_pointers": [
            {
                "id": e.id,
                "kind": e.kind,
                "title": e.title,
                "uri": e.uri,
                "sha256": e.sha256,
                "assessor": e.assessor,
                "independent": e.independent,
                "produced_at": e.produced_at.isoformat() if e.produced_at else None,
                "valid_until": e.valid_until.isoformat() if e.valid_until else None,
            }
            for e in evidence
        ],
        "latest_assurance_report": (
            {
                "id": report.id,
                "created_at": report.created_at.isoformat() if report.created_at else None,
                "health_state": report.health_state,
                "contract_version": report.contract_version,
            }
            if report is not None
            else None
        ),
        "latest_evidence_report": (
            {
                "id": evidence_report.id,
                "period_start": evidence_report.period_start.isoformat(),
                "period_end": evidence_report.period_end.isoformat(),
                "record_id": evidence_report.record_id,
                "bundle_id": evidence_report.bundle_id,
            }
            if evidence_report is not None
            else None
        ),
    }


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


def _section(sid: str, title: str, reference: str, filled: Dict[str, Any], to_complete: List[str],
             sources: List[str]) -> Dict[str, Any]:
    return {
        "id": sid,
        "title": title,
        "reference": reference,
        "filled": filled,
        "to_complete": to_complete,
        "sources": sources,
    }


def _components_and_dependencies(i: Dict[str, Any]) -> Dict[str, Any]:
    """Member components AND declared dependencies, each
    dependency with role, criticality, contribution, contract reference,
    assurance basis (tested inside Brutor / assumed at the boundary) and
    supplier entity."""
    return {
        "member_components": i["components"],
        "dependencies": [
            {
                "role": d["role"],
                "depends_on_system": (
                    {"id": d["depends_on_system_id"], "name": d["depends_on_system_name"]}
                    if d["depends_on_system_id"] else None
                ),
                "agent_card": (
                    {"id": d["agent_card_id"], "name": d["agent_card_name"]}
                    if d["agent_card_id"] else None
                ),
                "criticality": d["criticality"],
                "fallback": d["fallback"],
                "contributes_to_decision": d["contributes_to_decision"],
                "separability": d["separability"],
                "contract": d["contract"],
                "assurance_basis": d["assurance_basis"],
                "supplier_legal_entity": d["supplier_legal_entity"],
                "supplier_information": {
                    k: d["supplier_information"][k] for k in ("required", "on_file", "unexpired")
                },
            }
            for d in i["dependencies"]
        ],
    }


def _eu_annex_iv(i: Dict[str, Any]) -> List[Dict[str, Any]]:
    s, c = i["system"], i["composition"]
    return [
        _section("1", "General description of the AI system", "Annex IV(1)",
                 {"name": s["name"], "intended_purpose": s["intended_use"], "accountable_owner": s["owner"],
                  "provider_role": s["eu_ai_act_role"],
                  "risk_tier": s["eu_ai_act_risk_tier"], "contract": i["contract"],
                  "interacts_with": {"llm_models": c["llm_models"], "mcp_servers": c["mcp_servers"],
                                     "skills": c["skills"]}},
                 ["Provider name and version history", "Forms in which the system is placed on the market",
                  "Hardware the system runs on", "Instructions for use (Art 13)"],
                 ["AI System register", "contract", "bindings"]),
        _section("2", "Elements of the AI system and its development process", "Annex IV(2)",
                 {"composition": c, "composition_source": i["composition_source"],
                  "components_and_dependencies": _components_and_dependencies(i),
                  "operating_envelope": i["operating_envelope"]},
                 ["Design specifications and key design choices", "Data requirements and datasheets (Art 10)",
                  "Validation and testing procedures and results",
                  "How each external (assumed) dependency's supplier meets Art 25(4)"],
                 ["contract closure", "dependency register"]),
        _section("3", "Monitoring, functioning and control", "Annex IV(3); Art 14",
                 {"autonomy_level": i["autonomy_level"], "limits": i["limits"],
                  "human_oversight": "Approval gates, run abort and suspension are recorded as sealed records."},
                 ["Expected level of accuracy for the intended persons or groups",
                  "Foreseeable unintended outcomes and sources of risk"],
                 ["contract closure", "evidence log"]),
        _section("4", "Appropriateness of the performance metrics", "Annex IV(4)",
                 {"evidence_pointers": [e for e in i["evidence_pointers"] if e["kind"] in ("eval_report", "red_team_report")]},
                 ["Description and justification of the performance metrics"],
                 ["evidence pointers"]),
        _section("5", "Risk management system", "Annex IV(5); Art 9",
                 {"evidence_pointers": [e for e in i["evidence_pointers"] if e["kind"] in ("risk_assessment", "impact_assessment")]},
                 ["Description of the risk management system"], ["evidence pointers"]),
        _section("6", "Changes through the lifecycle", "Annex IV(6)",
                 {"contract": i["contract"]},
                 ["Description of relevant changes made by the provider"], ["contract history"]),
        _section("7", "Harmonised standards and specifications applied", "Annex IV(7)", {},
                 ["List of harmonised standards or other specifications applied"], []),
        _section("8", "EU declaration of conformity", "Annex IV(8); Art 47", {},
                 ["Copy of the EU declaration of conformity"], []),
        _section("9", "Post-market monitoring plan", "Annex IV(9); Art 72",
                 {"latest_assurance_report": i["latest_assurance_report"],
                  "latest_evidence_report": i["latest_evidence_report"],
                  "monitoring": "Daily health, drift and liveness are evaluated and cited by the Evidence Report."},
                 ["The post-market monitoring plan (Art 72(3))"], ["assurance report", "evidence report"]),
    ]


def _iso(i: Dict[str, Any]) -> List[Dict[str, Any]]:
    s = i["system"]
    return [
        _section("A.6.2.7-1", "System identity and intended use", "A.6.2.7",
                 {"name": s["name"], "owner": s["owner"], "intended_use": s["intended_use"],
                  "intended_client_labels": s["intended_client_labels"],
                  "intended_client_systems": s["intended_client_systems"],
                  "lifecycle_stage": s["lifecycle_stage"],
                  "contract": i["contract"]},
                 ["Technical documentation audience and distribution"], ["AI System register", "contract"]),
        _section("A.6.2.7-2", "Architecture and composition", "A.6.2.7",
                 {"composition": i["composition"], "composition_source": i["composition_source"],
                  "components_and_dependencies": _components_and_dependencies(i)},
                 ["Architecture narrative"], ["contract closure", "dependency register"]),
        _section("A.6.2.7-3", "Operation, monitoring and event logs", "A.6.2.6, A.6.2.8",
                 {"autonomy_level": i["autonomy_level"], "limits": i["limits"],
                  "latest_assurance_report": i["latest_assurance_report"],
                  "event_logs": "Every governed action produces a sealed record in the tenant log."},
                 ["Operational procedures and escalation contacts"], ["contract closure", "assurance report"]),
        _section("A.8.2", "Information for users", "A.8.2",
                 {"profile_core": (i["profile"] or {}).get("core")},
                 ["User-facing documentation and the AI-interaction notice text"], ["compliance profile"]),
        _section("A.6.2.7-4", "Evidence and assessments on file", "A.5, A.6.2.4",
                 {"evidence_pointers": i["evidence_pointers"], "latest_evidence_report": i["latest_evidence_report"]},
                 ["Statement of Applicability reference"], ["evidence pointers", "evidence report"]),
    ]


def _soc2(i: Dict[str, Any]) -> List[Dict[str, Any]]:
    s = i["system"]
    return [
        _section("DC1", "Types of services provided", "DC 200 (1)",
                 {"name": s["name"], "intended_use": s["intended_use"],
                  "intended_client_labels": s["intended_client_labels"],
                  "intended_client_systems": s["intended_client_systems"]},
                 ["Description of the services provided to user entities"], ["AI System register"]),
        _section("DC2", "Principal service commitments and system requirements", "DC 200 (2)", {},
                 ["Service commitments and system requirements"], []),
        _section("DC3", "Components of the system", "DC 200 (3)",
                 {"composition": i["composition"], "composition_source": i["composition_source"],
                  "components_and_dependencies": _components_and_dependencies(i),
                  "contract": i["contract"]},
                 ["Infrastructure, people and procedures outside the gateway"],
                 ["contract closure", "dependency register"]),
        _section("DC4", "Incidents", "DC 200 (4)", {},
                 ["Significant incidents during the period (see the incident register)"], ["incident register"]),
        _section("DC5", "Relevant controls", "DC 200 (5)",
                 {"limits": i["limits"], "autonomy_level": i["autonomy_level"],
                  "latest_evidence_report": i["latest_evidence_report"]},
                 ["Control descriptions mapped to the criteria"], ["contract closure", "evidence report"]),
        _section("DC6", "Complementary user entity controls", "DC 200 (6)", {},
                 ["Controls user entities are expected to operate"], []),
        _section("DC7", "Subservice organisations", "DC 200 (7)",
                 {"llm_models": i["composition"]["llm_models"],
                  # Shared services are inclusive (observed
                  # and graded here), external agents carve-out (assumed).
                  "dependency_suppliers": [
                      {"supplier_legal_entity": d["supplier_legal_entity"], "role": d["role"],
                       "treatment": "carve-out" if d["assurance_basis"] == "assumed" else "inclusive",
                       "agent_card_id": d["agent_card_id"], "depends_on_system_id": d["depends_on_system_id"]}
                      for d in i["dependencies"] if d["supplier_information"]["required"]
                  ]},
                 ["Carve-out or inclusive treatment of model and tool providers"], ["bindings", "dependency register"]),
        _section("DC8", "Changes during the period", "DC 200 (8)", {"contract": i["contract"]},
                 ["Significant changes to the system during the period"], ["contract history"]),
    ]


def _generic(i: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [
        _section("G1", "System identity", "—", {"system": i["system"], "contract": i["contract"]},
                 ["Framework-specific identification requirements"], ["AI System register", "contract"]),
        _section("G2", "Composition and controls", "—",
                 {"composition": i["composition"], "limits": i["limits"], "autonomy_level": i["autonomy_level"],
                  "components_and_dependencies": _components_and_dependencies(i)},
                 [], ["contract closure", "dependency register"]),
        _section("G3", "Evidence on file", "—",
                 {"evidence_pointers": i["evidence_pointers"], "latest_assurance_report": i["latest_assurance_report"],
                  "latest_evidence_report": i["latest_evidence_report"]},
                 [], ["evidence pointers", "assurance report", "evidence report"]),
    ]


_BUILDERS = {
    "eu-annex-iv": _eu_annex_iv,
    "iso-a6-2-7": _iso,
    "soc2-system-description": _soc2,
    "generic": _generic,
}


def render_content(db: Session, system: ResourceGroup, framework_id: str) -> Dict[str, Any]:
    """The document (deterministic for the same inputs)."""
    known = offering.any_catalog(framework_id)
    if known is None:
        raise DocumentationError(f"Unknown framework {framework_id!r}", 404)
    reason = offering.unavailable_reason(known)
    if reason is not None:
        raise DocumentationError(reason, 409)
    catalog = known
    template = template_for(framework_id)
    inputs = _inputs(db, system, framework_id)
    return {
        "kind": "ai.brutor/documentation-skeleton",
        "template": template,
        "template_version": TEMPLATE_VERSION,
        "framework": {
            "id": catalog.id,
            "title": catalog.title,
            "reference": catalog.reference,
            "catalog_version": catalog.current_version,
            "verified_against_instrument": catalog.verified_against_instrument,
        },
        "skeleton": True,
        "notice": SKELETON_NOTICE,
        "ai_system_id": system.id,
        "contract_version": (inputs["contract"] or {}).get("version"),
        "profile": inputs["profile"],
        "sections": _BUILDERS[template](inputs),
    }


def export(
    db: Session, system: ResourceGroup, framework_id: str, *, actor: Optional[str]
) -> DocumentationExport:
    """Render, persist and seal — or return the latest export when its
    digest is unchanged (idempotent)."""
    content = render_content(db, system, framework_id)
    digest = sealing.json_digest(content)
    latest = (
        db.query(ComplianceDocumentationExport)
        .filter(
            ComplianceDocumentationExport.tenant_id == system.tenant_id,
            ComplianceDocumentationExport.ai_system_id == system.id,
            ComplianceDocumentationExport.framework_id == framework_id,
        )
        .order_by(ComplianceDocumentationExport.created_at.desc(), ComplianceDocumentationExport.id.desc())
        .first()
    )
    if latest is not None and latest.digest == digest:
        return DocumentationExport(export=latest, seal=None, reused=True)

    row = ComplianceDocumentationExport(
        id=f"doc-{ULID()}".lower(),
        tenant_id=system.tenant_id,
        ai_system_id=system.id,
        framework_id=framework_id,
        template=content["template"],
        contract_version=content["contract_version"],
        content=content,
        digest=digest,
        created_by=actor,
    )
    db.add(row)
    db.flush()
    outcome = sealing.seal(
        system.tenant_id,
        sealing.RECORD_DOCUMENTATION,
        ai_system_id=system.id,
        context={
            "framework_id": framework_id,
            "catalog_version": content["framework"]["catalog_version"],
            "contract_version": content["contract_version"],
        },
        references=sealing.references(
            sealing.reference("ai.brutor.documentation-export", digest, "document_digest")
        ),
    )
    row.record_id = outcome.record_id
    db.flush()
    return DocumentationExport(export=row, seal=outcome, reused=False)


# ---------------------------------------------------------------------------
# HTML rendering (no PDF dependency — print to PDF from the browser)
# ---------------------------------------------------------------------------


def _html_value(value: Any) -> str:
    if value is None or value == "" or value == [] or value == {}:
        return "<em>not recorded</em>"
    if isinstance(value, dict):
        rows = "".join(
            f"<tr><th>{html.escape(str(k))}</th><td>{_html_value(v)}</td></tr>" for k, v in value.items()
        )
        return f"<table>{rows}</table>"
    if isinstance(value, list):
        return "<ul>" + "".join(f"<li>{_html_value(v)}</li>" for v in value) + "</ul>"
    return html.escape(str(value))


def render_html(row: ComplianceDocumentationExport) -> str:
    content = row.content or {}
    fw = content.get("framework") or {}
    parts = [
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">",
        f"<title>{html.escape(fw.get('title') or '')} documentation skeleton</title>",
        "<style>body{font-family:system-ui,sans-serif;max-width:60rem;margin:2rem auto;padding:0 1rem;"
        "line-height:1.45}table{border-collapse:collapse}th,td{border:1px solid #ccc;padding:.25rem .5rem;"
        "text-align:left;vertical-align:top}.notice{border:2px solid #b45309;padding:.75rem;background:#fffbeb}"
        ".todo{color:#92400e}</style></head><body>",
        f"<h1>{html.escape(fw.get('title') or '')} — documentation skeleton</h1>",
        f"<p class=\"notice\"><strong>Skeleton, not a claim.</strong> {html.escape(content.get('notice') or SKELETON_NOTICE)}</p>",
        "<p>",
        f"AI System <code>{html.escape(row.ai_system_id)}</code> · template <code>{html.escape(row.template)}</code> "
        f"v{html.escape(str(content.get('template_version')))} · catalog {html.escape(str(fw.get('catalog_version')))}"
        f" · contract version {html.escape(str(row.contract_version))}<br>",
        f"Digest (SHA-256, canonical JSON): <code>{html.escape(row.digest)}</code><br>",
        f"Sealed record: <code>{html.escape(row.record_id or 'not sealed')}</code>",
        "</p>",
    ]
    if fw.get("verified_against_instrument") is False:
        parts.append(
            "<p class=\"notice\">This framework's catalog was written from summaries of the "
            "instrument; check the published text before relying on it.</p>"
        )
    for section in content.get("sections") or []:
        parts.append(
            f"<section><h2>{html.escape(section['id'])} {html.escape(section['title'])}</h2>"
            f"<p><small>{html.escape(section.get('reference') or '')}</small></p>"
        )
        parts.append(f"<h3>Filled from the platform</h3>{_html_value(section.get('filled'))}")
        if section.get("sources"):
            parts.append(f"<p><small>Sources: {html.escape(', '.join(section['sources']))}</small></p>")
        if section.get("to_complete"):
            parts.append(
                "<h3 class=\"todo\">To complete</h3><ul class=\"todo\">"
                + "".join(f"<li>{html.escape(t)}</li>" for t in section["to_complete"])
                + "</ul>"
            )
        parts.append("</section>")
    parts.append("</body></html>")
    return "".join(parts)


__all__ = [
    "DocumentationError",
    "DocumentationExport",
    "SKELETON_NOTICE",
    "TEMPLATES",
    "TEMPLATE_VERSION",
    "export",
    "render_content",
    "render_html",
    "template_for",
]
