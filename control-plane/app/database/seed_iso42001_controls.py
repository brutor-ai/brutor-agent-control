# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Seed data for the ISO/IEC 42001:2023 Annex A control catalog
(`iso42001_controls`).

IMPORTANT — accuracy & copyright:
  * The `title` / `summary` fields below are SHORT PARAPHRASED navigation labels
    written by Brutor. They are NOT the verbatim normative text of ISO/IEC
    42001:2023 (which is copyrighted and must not be reproduced).
  * The control IDs and clause groupings follow the published Annex A structure,
    but **must be reconciled against the official ISO/IEC 42001:2023 standard**
    before this catalog is shown to a customer/auditor. Treat this as a solid
    working scaffold, not a certified transcription.

This is CODE-OWNED reference data (identical for every tenant), so the seeder
UPSERTS on startup (keeps titles/notes in sync with the code) — it is NOT a
create-once tenant seed.

`brutor_capability`:
  * "covered"  — Brutor implements this control and can supply live evidence.
  * "partial"  — Brutor supplies part of the control; org completes the rest.
  * "org"      — a purely organisational control Brutor cannot implement for you
                 (we still let you record status/owner/evidence for it).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from sqlalchemy.orm import Session

from app.database.models.iso42001 import Iso42001Control

logger = logging.getLogger(__name__)

# (control_id, clause, category, title, summary, brutor_note, brutor_capability)
ISO42001_CONTROLS: List[Dict[str, Any]] = [
    # --- A.2 Policies related to AI ---
    ("A.2.2", "A.2", "Policies related to AI", "AI policy",
     "Establish a documented AI policy set by top management.",
     "Policy-as-Code (Git-versioned YAML) + the AI policy-document layer link each written policy statement to the technical control that enforces it.", "partial"),
    ("A.2.3", "A.2", "Policies related to AI", "Alignment with other policies",
     "Align the AI policy with other organisational policies.",
     "Brutor governance (guardrails, residency, compliance profiles) can be mapped to your existing security/privacy policies.", "org"),
    ("A.2.4", "A.2", "Policies related to AI", "Review of the AI policy",
     "Review the AI policy at planned intervals.",
     "Policy versions + change history give the review trail; management-review report surfaces due reviews.", "partial"),

    # --- A.3 Internal organization ---
    ("A.3.2", "A.3", "Internal organization", "AI roles and responsibilities",
     "Define and allocate AI roles and responsibilities.",
     "Accountable-owner fields on every AI system in the registry + admin RBAC roles.", "partial"),
    ("A.3.3", "A.3", "Internal organization", "Reporting of concerns",
     "Provide a mechanism to report concerns about AI systems.",
     "Corrective-action (CAPA) register captures raised concerns and their resolution.", "partial"),

    # --- A.4 Resources for AI systems ---
    ("A.4.2", "A.4", "Resources for AI systems", "Resource documentation",
     "Document the resources (data, tooling, systems, people) used for AI.",
     "The AI system inventory (registry) + shadow-AI discovery document AI systems, models and their resources.", "covered"),
    ("A.4.3", "A.4", "Resources for AI systems", "Data resources",
     "Document data resources used by AI systems.",
     "Knowledgebase (RAG) source records capture data resources, provenance and PII handling.", "partial"),
    ("A.4.4", "A.4", "Resources for AI systems", "Tooling resources",
     "Document tooling resources for AI systems.",
     "MCP servers / tools and agent skills are inventoried and governed through the gateway.", "partial"),
    ("A.4.5", "A.4", "Resources for AI systems", "System and computing resources",
     "Document system and computing resources for AI.",
     "Provider/model catalog records the compute/serving resources behind each model.", "partial"),
    ("A.4.6", "A.4", "Resources for AI systems", "Human resources",
     "Document human resources and competencies for AI.",
     "Organisational — recorded via control owners; Brutor cannot supply this.", "org"),

    # --- A.5 Assessing impacts of AI systems ---
    ("A.5.2", "A.5", "Assessing impacts of AI systems", "AI system impact assessment process",
     "Establish a process to assess AI system impacts.",
     "AI Impact Assessment module (templated, versioned, approved) attached to each AI system.", "partial"),
    ("A.5.3", "A.5", "Assessing impacts of AI systems", "Documentation of impact assessments",
     "Document the results of AI system impact assessments.",
     "Impact assessments are versioned records with sign-off, exportable in the evidence pack.", "partial"),
    ("A.5.4", "A.5", "Assessing impacts of AI systems", "Impact on individuals or groups",
     "Assess impacts on individuals and groups.",
     "Impact-assessment template covers affected stakeholders and individual/group harms.", "partial"),
    ("A.5.5", "A.5", "Assessing impacts of AI systems", "Societal impacts",
     "Assess societal impacts of AI systems.",
     "Impact-assessment template covers societal/collective impacts.", "partial"),

    # --- A.6 AI system life cycle ---
    ("A.6.1.2", "A.6", "AI system life cycle", "Objectives for responsible development",
     "Define objectives for responsible AI development.",
     "Recorded per system; guardrail/policy objectives map to responsible-development goals.", "org"),
    ("A.6.1.3", "A.6", "AI system life cycle", "Responsible design and development",
     "Define processes for responsible design and development.",
     "Lifecycle states + approval gates enforce a defined path to production.", "partial"),
    ("A.6.2.2", "A.6", "AI system life cycle", "AI system requirements and specification",
     "Specify requirements for AI systems.",
     "Registry + impact assessment capture intended purpose and requirements per system.", "partial"),
    ("A.6.2.3", "A.6", "AI system life cycle", "Documentation of design and development",
     "Document AI system design and development.",
     "Per-system factsheets / model cards document design characteristics.", "partial"),
    ("A.6.2.4", "A.6", "AI system life cycle", "Verification and validation",
     "Verify and validate AI systems.",
     "Model health probing + circuit breakers provide runtime validation signals.", "partial"),
    ("A.6.2.5", "A.6", "AI system life cycle", "AI system deployment",
     "Manage AI system deployment.",
     "Lifecycle state machine gates deployment to production (approval + AIIA required).", "partial"),
    ("A.6.2.6", "A.6", "AI system life cycle", "Operation and monitoring",
     "Operate and monitor AI systems.",
     "Runtime enforcement (guardrails, semantic policy, quota, residency) + Mission Control monitoring — evidenced per request. STRONG.", "covered"),
    ("A.6.2.7", "A.6", "AI system life cycle", "Technical documentation",
     "Maintain AI system technical documentation.",
     "Factsheets / model cards + provider/model config form the technical documentation.", "partial"),
    ("A.6.2.8", "A.6", "AI system life cycle", "Recording of event logs",
     "Record AI system event logs.",
     "Immutable audit + proxy logs record every AI interaction, decision and block. STRONG.", "covered"),

    # --- A.7 Data for AI systems ---
    ("A.7.2", "A.7", "Data for AI systems", "Data for development and enhancement",
     "Manage data used to develop/enhance AI systems.",
     "Knowledgebase source records for RAG data; provider data-handling captured in supplier profile.", "partial"),
    ("A.7.3", "A.7", "Data for AI systems", "Acquisition of data",
     "Govern acquisition of data.",
     "KB connectors record data sources and acquisition; org owns upstream sourcing.", "partial"),
    ("A.7.4", "A.7", "Data for AI systems", "Quality of data",
     "Ensure quality of data for AI systems.",
     "Organisational data-quality process; Brutor records the data source metadata.", "org"),
    ("A.7.5", "A.7", "Data for AI systems", "Data provenance",
     "Record provenance of data.",
     "KB source provenance (origin, ingestion date, categories) captured per collection.", "partial"),
    ("A.7.6", "A.7", "Data for AI systems", "Data preparation",
     "Govern data preparation.",
     "KB ingestion pipeline records preparation steps; org owns methodology.", "partial"),

    # --- A.8 Information for interested parties ---
    ("A.8.2", "A.8", "Information for interested parties", "System documentation and information for users",
     "Provide documentation/information to users of AI systems.",
     "Model cards surfaced in the user portal (intended use, limitations).", "partial"),
    ("A.8.3", "A.8", "Information for interested parties", "External reporting",
     "Enable external reporting about AI systems.",
     "Evidence pack + factsheets can be shared externally; org owns disclosure process.", "org"),
    ("A.8.4", "A.8", "Information for interested parties", "Communication of incidents",
     "Communicate AI incidents to interested parties.",
     "Alerting + CAPA register capture incidents; org owns external communication.", "partial"),
    ("A.8.5", "A.8", "Information for interested parties", "Information for interested parties",
     "Provide information to interested parties.",
     "Organisational; supported by exportable evidence and reports.", "org"),

    # --- A.9 Use of AI systems ---
    ("A.9.2", "A.9", "Use of AI systems", "Processes for responsible use",
     "Establish processes for responsible use of AI.",
     "Guardrails, semantic-governance policy and quotas enforce responsible use at runtime. STRONG.", "covered"),
    ("A.9.3", "A.9", "Use of AI systems", "Objectives for responsible use",
     "Define objectives for responsible use.",
     "Policy objectives map to enforced controls; org sets the objectives.", "partial"),
    ("A.9.4", "A.9", "Use of AI systems", "Intended use of the AI system",
     "Define and communicate intended use.",
     "Intended-use field per AI system + model cards in the portal.", "partial"),

    # --- A.10 Third-party and customer relationships ---
    ("A.10.2", "A.10", "Third-party and customer relationships", "Allocating responsibilities",
     "Allocate responsibilities across third parties.",
     "Supplier profile + control owners record responsibility allocation.", "partial"),
    ("A.10.3", "A.10", "Third-party and customer relationships", "Suppliers",
     "Manage AI suppliers.",
     "Provider risk register + single enforcement point through which all supplier traffic flows. STRONG.", "covered"),
    ("A.10.4", "A.10", "Third-party and customer relationships", "Customers",
     "Manage customer relationships for AI.",
     "Organisational; supported by exportable customer-facing evidence.", "org"),
]


def seed_iso42001_controls(db: Session) -> int:
    """Upsert the Annex A control catalog. Reference data owned by code —
    safe to run on every startup; keeps titles/notes/capability in sync.
    Returns the number of rows created or updated."""
    changed = 0
    for order, (cid, clause, category, title, summary, note, cap) in enumerate(
        ISO42001_CONTROLS
    ):
        row = db.get(Iso42001Control, cid)
        if row is None:
            db.add(
                Iso42001Control(
                    control_id=cid,
                    clause=clause,
                    category=category,
                    title=title,
                    summary=summary,
                    brutor_note=note,
                    brutor_capability=cap,
                    sort_order=order,
                )
            )
            changed += 1
        else:
            # Keep code-owned fields current.
            if (
                row.clause != clause
                or row.category != category
                or row.title != title
                or row.summary != summary
                or row.brutor_note != note
                or row.brutor_capability != cap
                or row.sort_order != order
            ):
                row.clause = clause
                row.category = category
                row.title = title
                row.summary = summary
                row.brutor_note = note
                row.brutor_capability = cap
                row.sort_order = order
                changed += 1
    if changed:
        logger.info("Seeded/updated %d ISO 42001 Annex A controls", changed)
    return changed
