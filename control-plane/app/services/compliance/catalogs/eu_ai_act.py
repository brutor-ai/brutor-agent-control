# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# EU AI Act catalog — the first fully populated framework

Regulation (EU) 2024/1689 (OJ L, 2024/1689, 12.7.2024) as amended by the
Digital Omnibus on AI, Regulation (EU) 2026/1744 of 8 July 2026 (OJ L,
2026/1744, 24.7.2026; in force 27 July 2026, its Art 2). Checked on
2026-10-16 against the EUR-Lex consolidated text 02024R1689-20260727 — the
only consolidation listed, and 2026/1744 the only amending act recorded
against 32024R1689 on that date — and against the two OJ texts.

Dates of application (Art 113 as amended): Chapters I–II from 2 February
2025, except the new Art 5(1)(ba)/(bb) prohibitions from 2 December 2026;
Chapter V (GPAI) from 2 August 2025; Chapter III Sections 1–3 from
2 December 2027 for Art 6(2)/Annex III systems and 2 August 2028 for
Art 6(1)/Annex I systems; everything else (incl. Art 50) from 2 August
2026. Art 111(4): Art 50(2) for systems placed on the market before
2 August 2026 by 2 December 2026.

Art 43, 47, 48, 49, 72, 73 and 86 sit outside Chapter III Sections 1–3
and so fall, as written, under the general 2 August 2026 date; they bind only systems
classified high-risk, and the classification rules (Art 6, Section 1)
apply from the dates above, so the catalog dates them with Section 1.

Two catalog versions: `2024-08` — the Act as published (Annex III
duties from 2 August 2026, Annex I from 2 August 2027 per the original
Art 113(c), no Omnibus additions) — in force until the Omnibus; `2026-09` —
as amended. A window before 27 July 2026 is evaluated against the text in
force then.

Every obligation cites the provision it was checked against (`sources`,
verbatim excerpts). Every statement names what it does NOT show (`limit`).
Organisational duties (risk management, QMS, conformity assessment) are
evidenced by pointer and declaration only, never claimed. Not legal advice.
"""

from __future__ import annotations

from dataclasses import replace as _replace
from datetime import date, timedelta
from types import MappingProxyType
from typing import Dict, Optional

from app.database.models.ai_asset import RISK_TIER_LIMITED, RISK_TIER_MINIMAL, RISK_TIERS
from app.database.models.resource_group import EU_AI_ACT_ROLES
from app.services.compliance.catalogs import common as _common
from app.services.compliance.catalogs.base import (
    BASIS_DECLARED,
    BASIS_INDICATOR,
    BASIS_JUDGED,
    BASIS_RECOMPUTED,
    GPAI,
    GPAI_SYSTEMIC_RISK,
    INSTRUMENT_FINAL,
    KIND_REGULATION,
    CatalogVersion,
    EvidenceSpec,
    Framework,
    Obligation,
    Source,
)

FRAMEWORK_ID = "eu-ai-act"

#: Role groupings the rules reference. `provider_and_deployer` carries both
#: duty sets — Art 25 flips a deployer into this on rebranding or
#: substantial modification.
PROVIDER_SIDE = frozenset({
    "provider", "provider_and_deployer", "product_manufacturer",
    "authorised_representative", "importer", "distributor",
})
DEPLOYER_SIDE = frozenset({"deployer", "provider_and_deployer"})
#: Art 50(1)/(2) bind "providers" only (a product manufacturer is the
#: provider under Art 25(3)); importers and distributors have no Art 50 duty.
ART50_PROVIDERS = frozenset({"provider", "provider_and_deployer", "product_manufacturer"})

HIGH = frozenset({"high"})
#: Art 6(3): an Annex III system the provider assessed as not high-risk.
NOT_HIGH = frozenset({RISK_TIER_MINIMAL, RISK_TIER_LIMITED})
#: Art 6(4) / 49(2) bind the provider or its authorised representative.
ANNEX3_SELF_ASSESSORS = frozenset({"provider", "provider_and_deployer", "authorised_representative"})

V_ORIGINAL = "2024-08"
V_OMNIBUS = "2026-09"

#: Omnibus in force (Reg. 2026/1744, OJ publication + 20 days).
OMNIBUS_IN_FORCE = date(2026, 7, 27)

#: Pre-Omnibus dates (Art 113 of Reg. 2024/1689 as published).
_ORIGINAL_ANNEX3 = MappingProxyType({V_ORIGINAL: date(2026, 8, 2)})
_ORIGINAL_ANNEX1 = MappingProxyType({V_ORIGINAL: date(2027, 8, 2)})

ANNEX3_DATE = date(2027, 12, 2)


# ---------------------------------------------------------------------------
# Sources — the published text each obligation was checked against
# ---------------------------------------------------------------------------

CHECKED_ON = date(2026, 10, 16)

_INSTR_CONSOLIDATED = (
    "Regulation (EU) 2024/1689 (AI Act), consolidated text 02024R1689-20260727 "
    "as amended by Regulation (EU) 2026/1744"
)
_INSTR_OJ = "Regulation (EU) 2024/1689 (AI Act), OJ L, 2024/1689, 12.7.2024"
_URL_CONSOLIDATED = "https://eur-lex.europa.eu/legal-content/EN/TXT/HTML/?uri=CELEX:02024R1689-20260727"
_URL_OJ = "https://eur-lex.europa.eu/legal-content/EN/TXT/HTML/?uri=CELEX:32024R1689"


def _src(provision: str, anchor: str, excerpt: str, *, original: bool = False) -> Source:
    """A provision of the consolidated text (or, `original`, the OJ text as
    published — recitals, and the pre-Omnibus Art 113 the replay uses)."""
    return Source(
        instrument=_INSTR_OJ if original else _INSTR_CONSOLIDATED,
        provision=provision,
        url=f"{_URL_OJ if original else _URL_CONSOLIDATED}#{anchor}",
        checked_on=CHECKED_ON,
        excerpt=excerpt,
    )


#: Art 113 — the dates of application the `applies_from` values encode.
SRC_113_CH1_2 = _src(
    "Art. 113, third paragraph, point (a)", "art_113",
    "Chapters I and II shall apply from 2 February 2025, with the exception of Article 5(1), "
    "first subparagraph, points (ba) and (bb), and Article 5(1a) and (1b) which shall apply "
    "from 2 December 2026;",
)
SRC_113_GPAI = _src(
    "Art. 113, third paragraph, point (b)", "art_113",
    "Chapter III Section 4, Chapter V, Chapter VII and Chapter XII and Article 78 shall apply "
    "from 2 August 2025, with the exception of Article 101",
)
SRC_113_GENERAL = _src(
    "Art. 113, second paragraph", "art_113",
    "This Regulation shall enter into force on the twentieth day following that of its "
    "publication in the Official Journal of the European Union. It shall apply from 2 August 2026.",
)
SRC_113_ANNEX3 = _src(
    "Art. 113, third paragraph, point (c)(i)", "art_113",
    "Chapter III, Sections 1, 2, and 3, with the exception of Article 6(5), shall apply from: "
    "(i) 2 December 2027 as regards AI systems classified as high-risk pursuant to Article 6(2) "
    "and Annex III",
)
SRC_113_ANNEX1 = _src(
    "Art. 113, third paragraph, point (c)(ii)", "art_113",
    "(ii) 2 August 2028 as regards AI systems classified as high-risk pursuant to Article 6(1) "
    "and Annex I",
)
SRC_113_ORIGINAL_ANNEX1 = _src(
    "Art. 113, third paragraph, point (c) (as published, pre-Omnibus replay)", "art_113",
    "Article 6(1) and the corresponding obligations in this Regulation shall apply from 2 August 2027.",
    original=True,
)


# ---------------------------------------------------------------------------
# Evidence statements (shared resolver ids)
# ---------------------------------------------------------------------------


def _declared(key: str, path: str, statement: str, limit: str) -> EvidenceSpec:
    return EvidenceSpec(
        key=key,
        statement=statement,
        basis=BASIS_DECLARED,
        resolver="profile.field_declared",
        params=MappingProxyType({"path": path}),
        limit=limit,
    )


def _pointer(kind: str, statement: str, limit: Optional[str] = None) -> EvidenceSpec:
    return EvidenceSpec(
        key=f"evidence.pointer_on_file.{kind}",
        statement=statement,
        basis=BASIS_RECOMPUTED,
        resolver="evidence.pointer_on_file",
        params=MappingProxyType({"kind": kind}),
        limit=limit
        or "A pointer to a document produced outside the platform; its content is not evaluated.",
    )


# Composite systems (shared factories: the same statements
# other catalogs cite).
CONTRIBUTING_DEPENDENCIES_ASSESSED = _common.contributing_dependencies_assessed()
SUPPLIER_INFORMATION_ON_FILE = _common.supplier_information_on_file()

EVERY_VERDICT_SEALED = EvidenceSpec(
    key="records.every_verdict_sealed",
    statement="Every governed action produced a sealed record, including refusals.",
    basis=BASIS_RECOMPUTED,
    resolver="records.every_verdict_sealed",
    limit=(
        "Integrity is not completeness: covers actions routed through the gateway only; "
        "records sealed late by backfill are reported separately."
    ),
)
WITNESSED = EvidenceSpec(
    key="records.witnessed",
    statement="The tenant log's tree heads covering the window were witnessed by an independent service.",
    basis=BASIS_RECOMPUTED,
    resolver="records.witnessed",
    limit=(
        "A receipt shows inclusion at a tree size, not a witness-observed time; a "
        "same-operator witness is self-attested."
    ),
)
CHAIN_COMPLETE = EvidenceSpec(
    key="records.chain_complete",
    statement="Approval chains are closed and epoch boundaries match the contract history.",
    basis=BASIS_RECOMPUTED,
    resolver="records.chain_complete",
    limit="Checks the chains the gateway opened; actions that never reached the gateway cannot be counted.",
)
RETENTION_183 = EvidenceSpec(
    key="retention.floor",
    statement="Logs are kept for at least six months (183 days).",
    basis=BASIS_RECOMPUTED,
    resolver="retention.floor",
    params=MappingProxyType({"floor_days": 183}),
    limit="Record identifiers, statements and tree heads are permanent; bodies follow retention.",
)
DECLARED_PRUNES = EvidenceSpec(
    key="retention.declared_prunes",
    statement="Every gap in the retained logs is a declared prune.",
    basis=BASIS_RECOMPUTED,
    resolver="retention.declared_prunes",
)
OVERRIDE_AVAILABLE = EvidenceSpec(
    key="oversight.override_available",
    statement="A human can override or stop the system.",
    basis=BASIS_RECOMPUTED,
    resolver="oversight.override_available",
    limit="Shows the controls exist and are assigned, not that the assigned people are competent.",
)
OVERSIGHT_EXERCISED = EvidenceSpec(
    key="oversight.exercised",
    statement="Human approval requests were decided by humans (requested, decided, lapsed).",
    basis=BASIS_RECOMPUTED,
    resolver="oversight.exercised",
    limit="Records the fact of human disposition, not its quality.",
)
NOT_RUBBER_STAMP = EvidenceSpec(
    key="oversight.not_rubber_stamp",
    statement="Approval latency and denial rate are tracked for movement.",
    basis=BASIS_INDICATOR,
    resolver="oversight.not_rubber_stamp",
    limit="An indicator of movement, never a threshold; it cannot show a decision was considered.",
)
OUT_OF_GRANT_REFUSED = EvidenceSpec(
    key="authz.out_of_grant_refused",
    statement="Every action outside the contract or grant was refused.",
    basis=BASIS_RECOMPUTED,
    resolver="authz.out_of_grant_refused",
    limit="Covers actions the gateway observed; the grant itself is a declaration.",
)
DAILY_HEALTH = EvidenceSpec(
    key="monitoring.daily_health",
    statement="Liveness, drift and health were evaluated every day.",
    basis=BASIS_RECOMPUTED,
    resolver="monitoring.daily_health",
)
SUSPEND_AVAILABLE = EvidenceSpec(
    key="monitoring.suspend_available",
    statement="Suspension and run-abort controls are available and every use of them was sealed.",
    basis=BASIS_RECOMPUTED,
    resolver="monitoring.suspend_available",
)
TECH_DOCS = EvidenceSpec(
    key="docs.technical_documentation",
    statement="A documentation export exists for the active contract version, with its digest sealed.",
    basis=BASIS_RECOMPUTED,
    resolver="docs.technical_documentation",
    params=MappingProxyType({"framework": FRAMEWORK_ID}),
    limit="A skeleton generated from the register and contract; its adequacy is not evaluated.",
)
REGISTRATION = EvidenceSpec(
    key="docs.registration_recorded",
    statement="The EU database registration identifier is recorded.",
    basis=BASIS_DECLARED,
    resolver="docs.registration_recorded",
    params=MappingProxyType({"framework": FRAMEWORK_ID}),
    limit="The identifier is declared; the registry entry itself is not queried.",
)
NOTICE_BEFORE_FIRST_TURN = EvidenceSpec(
    key="notice.before_first_turn",
    statement="People were told they were dealing with an AI before the first substantive turn.",
    basis=BASIS_RECOMPUTED,
    resolver="notice.before_first_turn",
    limit=(
        "Counts conversations through the Brutor portal (notice records); other clients "
        "must render their own notice."
    ),
)
NOTICE_TEXT = EvidenceSpec(
    key="notice.text_declared",
    statement="The notice text and the surface it is shown on are declared and sealed.",
    basis=BASIS_DECLARED,
    resolver="notice.text_declared",
)
SYNTHETIC_MARKING = EvidenceSpec(
    key="synthetic.marking_declared",
    statement="The approach to marking synthetic content is declared.",
    basis=BASIS_DECLARED,
    resolver="synthetic.marking_declared",
    limit=(
        "Content-level marking (watermarking, C2PA) is not provided; outputs carry an "
        "X-Brutor-AI-Generated header only."
    ),
)
INCIDENT_REGISTER = EvidenceSpec(
    key="incidents.register_exists",
    statement="An incident register is kept and no open incident is past its deadline.",
    basis=BASIS_RECOMPUTED,
    resolver="incidents.register_exists",
)
INCIDENT_DEADLINE = EvidenceSpec(
    key="incidents.reported_within_deadline",
    statement="Serious incidents were reported within the Art 73 deadline (15 days; death 10; widespread 2).",
    basis=BASIS_RECOMPUTED,
    resolver="incidents.reported_within_deadline",
    params=MappingProxyType({"framework": FRAMEWORK_ID}),
    limit="Deadlines are computed and shown, not enforced; awareness time is operator-entered.",
)


def _judge(template: str, statement: str) -> EvidenceSpec:
    return EvidenceSpec(
        key=f"judge.{template}",
        statement=statement,
        basis=BASIS_JUDGED,
        resolver=f"judge.{template}",
        limit="Judged by a model over a disclosed sample; stated with human agreement, never merged with recomputed rows.",
    )


# ---------------------------------------------------------------------------
# Obligations
# ---------------------------------------------------------------------------

_O = Obligation

OBLIGATIONS = [
    _O(
        key="art4-ai-literacy",
        title="AI literacy",
        reference="Art 4(1)",
        applies_from=date(2025, 2, 2),
        summary=(
            "Providers and deployers of AI systems take measures to support the AI literacy of "
            "their staff and others operating or using AI systems on their behalf. As amended by "
            "Reg. 2026/1744 the duty does not require guaranteeing any specific level of literacy."
        ),
        evidence=[
            _declared(
                "declared.ai_literacy",
                "frameworks.eu-ai-act.ai_literacy_reference",
                "An AI literacy programme is declared.",
                "Training is organisational; the gateway cannot observe it.",
            )
        ],
        limit="Organisational duty, evidenced by declaration only.",
        sources=(
            _src(
                "Art. 4(1)", "art_4",
                "Providers and deployers of AI systems shall take measures to support the development "
                "of AI literacy of their staff and other persons dealing with the operation and use of "
                "AI systems on their behalf",
            ),
            SRC_113_CH1_2,
        ),
    ),
    _O(
        key="art5-prohibitions",
        title="Prohibited practices",
        reference="Art 5(1)",
        applies_from=date(2025, 2, 2),
        summary=(
            "The practices listed in Art 5(1) are prohibited (placing on the market, putting into "
            "service or use). Every system is subject to the prohibitions; a system classified "
            "unacceptable may not be operated."
        ),
        evidence=[_judge("no_manipulation", "Sampled runs show no manipulative or deceptive technique.")],
        limit=(
            "The judged statement looks for the Art 5(1)(a) manipulative or deceptive techniques "
            "only; the other prohibited practices are not evidenced by the gateway."
        ),
        sources=(
            _src(
                "Art. 5(1)(a)", "art_5",
                "the placing on the market, the putting into service or the use of an AI system that "
                "deploys subliminal techniques beyond a person’s consciousness or purposefully "
                "manipulative or deceptive techniques",
            ),
            SRC_113_CH1_2,
        ),
    ),
    _O(
        key="art5-prohibitions-2026",
        title="Additional prohibited practices (Omnibus)",
        reference="Art 5(1)(ba), (bb), 5(1a), 5(1b) as inserted by Reg. 2026/1744",
        applies_from=date(2026, 12, 2),
        summary=(
            "From 2 December 2026: AI systems that generate or manipulate realistic intimate or "
            "sexually explicit material of an identifiable person without explicit consent, or "
            "child sexual abuse material, are prohibited (Art 5(1)(ba)/(bb), scoped by Art 5(1a))."
        ),
        introduced_in=V_OMNIBUS,
        evidence=[_judge("no_manipulation", "Sampled runs show no manipulative or deceptive technique.")],
        limit=(
            "No statement looks for the generation of the material these points prohibit; the "
            "judged statement covers manipulative techniques only."
        ),
        sources=(
            _src(
                "Art. 5(1)(ba)", "art_5",
                "the placing on the market, the putting into service or the use of an AI system that "
                "generates or manipulates realistic images, videos, audio or similar material of an "
                "identifiable natural person’s intimate parts",
            ),
            SRC_113_CH1_2,
        ),
    ),
    _O(
        key="art9-risk-management",
        title="Risk management system",
        reference="Art 9",
        applies_from=ANNEX3_DATE,
        applies_from_by_version=_ORIGINAL_ANNEX3,
        summary="A continuous, iterative risk-management process across the system's lifecycle.",
        risk_tiers=HIGH,
        roles=PROVIDER_SIDE,
        aliases=frozenset({"annex3-provider-duties"}),
        evidence=[_pointer("risk_assessment", "A current risk assessment is on file.")],
        limit="Organisational duty, evidenced by pointer only.",
        sources=(
            _src(
                "Art. 9(1)", "art_9",
                "A risk management system shall be established, implemented, documented and "
                "maintained in relation to high-risk AI systems.",
            ),
            SRC_113_ANNEX3,
        ),
    ),
    _O(
        key="art10-data-governance",
        title="Data and data governance",
        reference="Art 10",
        applies_from=ANNEX3_DATE,
        applies_from_by_version=_ORIGINAL_ANNEX3,
        summary=(
            "Training, validation and testing data sets meet the quality criteria of Art 10(2)-(4) "
            "and Art 4a(1) (as amended by Reg. 2026/1744)."
        ),
        risk_tiers=HIGH,
        roles=PROVIDER_SIDE,
        aliases=frozenset({"annex3-provider-duties"}),
        evidence=[_pointer("data_governance", "A data-governance record is on file.")],
        limit="Training data is outside the gateway; evidenced by pointer only.",
        sources=(
            _src(
                "Art. 10(2)", "art_10",
                "Training, validation and testing data sets shall be subject to data governance and "
                "management practices appropriate for the intended purpose of the high-risk AI system.",
            ),
            SRC_113_ANNEX3,
        ),
    ),
    _O(
        key="art11-technical-documentation",
        title="Technical documentation",
        reference="Art 11, Annex IV",
        applies_from=ANNEX3_DATE,
        applies_from_by_version=_ORIGINAL_ANNEX3,
        summary="Technical documentation drawn up before placing on the market and kept current.",
        risk_tiers=HIGH,
        roles=PROVIDER_SIDE,
        aliases=frozenset({"annex3-provider-duties"}),
        evidence=[TECH_DOCS],
        sources=(
            _src(
                "Art. 11(1)", "art_11",
                "The technical documentation of a high-risk AI system shall be drawn up before that "
                "system is placed on the market or put into service and shall be kept up-to date.",
            ),
            SRC_113_ANNEX3,
        ),
    ),
    _O(
        key="art12-record-keeping",
        title="Record-keeping (automatic logging)",
        reference="Art 12",
        applies_from=ANNEX3_DATE,
        applies_from_by_version=_ORIGINAL_ANNEX3,
        summary=(
            "Automatic recording of events over the system's lifetime, enabling traceability "
            "and post-market monitoring."
        ),
        risk_tiers=HIGH,
        roles=PROVIDER_SIDE,
        aliases=frozenset({"annex3-provider-duties"}),
        evidence=[EVERY_VERDICT_SEALED, WITNESSED, CHAIN_COMPLETE],
        sources=(
            _src(
                "Art. 12(1)", "art_12",
                "High-risk AI systems shall technically allow for the automatic recording of events "
                "(logs) over the lifetime of the system.",
            ),
            SRC_113_ANNEX3,
        ),
    ),
    _O(
        key="art13-transparency-to-deployers",
        title="Transparency and information to deployers",
        reference="Art 13",
        applies_from=ANNEX3_DATE,
        applies_from_by_version=_ORIGINAL_ANNEX3,
        summary="Instructions for use enabling deployers to interpret output and use the system appropriately.",
        risk_tiers=HIGH,
        roles=PROVIDER_SIDE,
        aliases=frozenset({"annex3-provider-duties"}),
        evidence=[
            TECH_DOCS,
            _declared(
                "declared.instructions_for_use",
                "frameworks.eu-ai-act.instructions_for_use_reference",
                "Instructions for use are declared.",
                "The instructions' content is not evaluated.",
            ),
        ],
        sources=(
            _src(
                "Art. 13(2)", "art_13",
                "High-risk AI systems shall be accompanied by instructions for use in an appropriate "
                "digital format or otherwise that include concise, complete, correct and clear information",
            ),
            SRC_113_ANNEX3,
        ),
    ),
    _O(
        key="art14-human-oversight",
        title="Human oversight",
        reference="Art 14",
        applies_from=ANNEX3_DATE,
        applies_from_by_version=_ORIGINAL_ANNEX3,
        summary="Designed so natural persons can oversee, interpret, override and stop the system.",
        risk_tiers=HIGH,
        roles=PROVIDER_SIDE,
        aliases=frozenset({"annex3-provider-duties"}),
        evidence=[
            OVERRIDE_AVAILABLE,
            OVERSIGHT_EXERCISED,
            NOT_RUBBER_STAMP,
            _judge("approval_rationale", "Sampled approval decisions carry a rationale consistent with the request."),
        ],
        sources=(
            _src(
                "Art. 14(1)", "art_14",
                "High-risk AI systems shall be designed and developed in such a way, including with "
                "appropriate human-machine interface tools, that they can be effectively overseen by "
                "natural persons during the period in which they are in use.",
            ),
            _src(
                "Art. 14(4)(e)", "art_14",
                "to intervene in the operation of the high-risk AI system or interrupt the system "
                "through a ‘stop’ button or a similar procedure",
            ),
            SRC_113_ANNEX3,
        ),
    ),
    _O(
        key="art15-accuracy-robustness",
        title="Accuracy, robustness and cybersecurity",
        reference="Art 15",
        applies_from=ANNEX3_DATE,
        applies_from_by_version=_ORIGINAL_ANNEX3,
        summary="Appropriate accuracy, robustness and cybersecurity, declared in the instructions for use.",
        risk_tiers=HIGH,
        roles=PROVIDER_SIDE,
        aliases=frozenset({"annex3-provider-duties"}),
        evidence=[
            _pointer("eval_report", "A current evaluation report is on file."),
            _pointer("red_team_report", "A current red-team report is on file."),
        ],
        sources=(
            _src(
                "Art. 15(1)", "art_15",
                "High-risk AI systems shall be designed and developed in such a way that they achieve "
                "an appropriate level of accuracy, robustness, and cybersecurity",
            ),
            _src(
                "Art. 15(3)", "art_15",
                "The levels of accuracy and the relevant accuracy metrics of high-risk AI systems shall "
                "be declared in the accompanying instructions of use.",
            ),
            SRC_113_ANNEX3,
        ),
    ),
    _O(
        key="art17-quality-management",
        title="Quality management system",
        reference="Art 17",
        applies_from=ANNEX3_DATE,
        applies_from_by_version=_ORIGINAL_ANNEX3,
        summary="A documented quality management system.",
        risk_tiers=HIGH,
        roles=PROVIDER_SIDE,
        aliases=frozenset({"annex3-provider-duties"}),
        evidence=[
            _declared(
                "declared.qms",
                "frameworks.eu-ai-act.qms_reference",
                "A quality management system is declared.",
                "Organisational duty; the QMS itself is not evaluated.",
            )
        ],
        limit="Organisational duty, evidenced by declaration only.",
        sources=(
            _src(
                "Art. 17(1)", "art_17",
                "Providers of high-risk AI systems shall put a quality management system in place that "
                "ensures compliance with this Regulation.",
            ),
            SRC_113_ANNEX3,
        ),
    ),
    _O(
        key="art19-log-retention-provider",
        title="Automatically generated logs (provider)",
        reference="Art 19(1)",
        applies_from=ANNEX3_DATE,
        applies_from_by_version=_ORIGINAL_ANNEX3,
        summary=(
            "Providers keep the automatically generated logs under their control for a period "
            "appropriate to the intended purpose, of at least six months unless other law provides otherwise."
        ),
        risk_tiers=HIGH,
        roles=PROVIDER_SIDE,
        aliases=frozenset({"annex3-provider-duties"}),
        evidence=[RETENTION_183, DECLARED_PRUNES],
        sources=(
            _src(
                "Art. 19(1)", "art_19",
                "Providers of high-risk AI systems shall keep the logs referred to in Article 12(1), "
                "automatically generated by their high-risk AI systems, to the extent such logs are "
                "under their control.",
            ),
            _src(
                "Art. 19(1)", "art_19",
                "the logs shall be kept for a period appropriate to the intended purpose of the "
                "high-risk AI system, of at least six months",
            ),
            SRC_113_ANNEX3,
        ),
    ),
    _O(
        key="art6-composite-classification",
        title="Classification of a composite system",
        reference="Art 6(2)-(3); Recital 12; draft Commission high-risk classification guidelines (May 2026)",
        applies_from=ANNEX3_DATE,
        applies_from_by_version=_ORIGINAL_ANNEX3,
        summary=(
            "Brutor's reading, from the Commission's DRAFT classification guidelines (not the Act's "
            "text): where components' combined outputs materially influence a high-risk decision "
            "they are assessed as one system, unless a component is genuinely separable and does "
            "not contribute to the high-risk purpose. The Act's anchor is Art 6(3): an Annex III "
            "system escapes high-risk only if, among other things, it does not materially influence "
            "the outcome of decision making."
        ),
        risk_tiers=HIGH,
        needs_review=True,
        review_note=(
            "Rests on the Commission's DRAFT classification guidelines (consultation closed "
            "23 June 2026), which are not published on EUR-Lex and were not checked here; "
            "re-check against the final text."
        ),
        evidence=[CONTRIBUTING_DEPENDENCIES_ASSESSED],
        limit="Covers declared dependency edges; the composition checks report undeclared delegation.",
        sources=(
            _src(
                "Art. 6(3), first subparagraph", "art_6",
                "an AI system referred to in Annex III shall not be considered to be high-risk where it "
                "does not pose a significant risk of harm to the health, safety or fundamental rights of "
                "natural persons, including by not materially influencing the outcome of decision making.",
            ),
            _src(
                "Recital 12", "rct_12",
                "AI systems can be used on a stand-alone basis or as a component of a product, "
                "irrespective of whether the system is physically integrated into the product (embedded) "
                "or serves the functionality of the product without being integrated therein (non-embedded).",
                original=True,
            ),
            SRC_113_ANNEX3,
        ),
    ),
    _O(
        key="art25-4-supplier-agreements",
        title="Written agreements with suppliers",
        reference="Art 25(4), first subparagraph (as amended by Reg. 2026/1744)",
        applies_from=ANNEX3_DATE,
        applies_from_by_version=_ORIGINAL_ANNEX3,
        summary=(
            "The provider of a high-risk system and a third party supplying an AI system, AI model, "
            "tools, services, components or processes used or integrated in it specify by written "
            "agreement the information, capabilities, technical access and other assistance the "
            "provider needs to comply. Not applicable to third parties making tools, services, "
            "processes or components (other than GPAI models) public under a free and open-source licence."
        ),
        risk_tiers=HIGH,
        roles=PROVIDER_SIDE,
        evidence=[SUPPLIER_INFORMATION_ON_FILE],
        limit="Evidenced by pointer to the agreement; its terms are not evaluated.",
        sources=(
            _src(
                "Art. 25(4), first subparagraph", "art_25",
                "The provider of a high-risk AI system and the third party that supplies an AI system, "
                "AI model, tools, services, components, or processes that are used or integrated in a "
                "high-risk AI system shall, by written agreement, specify the necessary information",
            ),
            SRC_113_ANNEX3,
        ),
    ),
    _O(
        key="art26-1-use-per-instructions",
        title="Use in accordance with instructions",
        reference="Art 26(1)",
        applies_from=ANNEX3_DATE,
        applies_from_by_version=_ORIGINAL_ANNEX3,
        summary=(
            "Deployers take appropriate technical and organisational measures to use the system in "
            "accordance with the instructions for use."
        ),
        risk_tiers=HIGH,
        roles=DEPLOYER_SIDE,
        aliases=frozenset({"annex3-deployer-duties"}),
        evidence=[
            OUT_OF_GRANT_REFUSED,
            _judge("per_instructions", "Sampled runs stay within the declared intended use."),
        ],
        sources=(
            _src(
                "Art. 26(1)", "art_26",
                "Deployers of high-risk AI systems shall take appropriate technical and organisational "
                "measures to ensure they use such systems in accordance with the instructions for use "
                "accompanying the systems",
            ),
            SRC_113_ANNEX3,
        ),
    ),
    _O(
        key="art26-2-oversight-assignment",
        title="Human oversight assignment",
        reference="Art 26(2)",
        applies_from=ANNEX3_DATE,
        applies_from_by_version=_ORIGINAL_ANNEX3,
        summary=(
            "Human oversight is assigned to natural persons with the necessary competence, training "
            "and authority, and the necessary support."
        ),
        risk_tiers=HIGH,
        roles=DEPLOYER_SIDE,
        aliases=frozenset({"annex3-deployer-duties"}),
        evidence=[OVERRIDE_AVAILABLE],
        sources=(
            _src(
                "Art. 26(2)", "art_26",
                "Deployers shall assign human oversight to natural persons who have the necessary "
                "competence, training and authority, as well as the necessary support.",
            ),
            SRC_113_ANNEX3,
        ),
    ),
    _O(
        key="art26-5-monitoring-and-suspend",
        title="Monitoring and suspension",
        reference="Art 26(5)",
        applies_from=ANNEX3_DATE,
        applies_from_by_version=_ORIGINAL_ANNEX3,
        summary=(
            "Deployers monitor operation on the basis of the instructions for use; where they have "
            "reason to consider that use may present a risk within Art 79(1) they inform the provider "
            "or distributor and the market surveillance authority without undue delay and suspend use; "
            "a serious incident is reported immediately, first to the provider."
        ),
        risk_tiers=HIGH,
        roles=DEPLOYER_SIDE,
        aliases=frozenset({"annex3-deployer-duties"}),
        evidence=[DAILY_HEALTH, SUSPEND_AVAILABLE],
        sources=(
            _src(
                "Art. 26(5), first subparagraph", "art_26",
                "Deployers shall monitor the operation of the high-risk AI system on the basis of the "
                "instructions for use",
            ),
            _src(
                "Art. 26(5), first subparagraph", "art_26",
                "they shall, without undue delay, inform the provider or distributor and the relevant "
                "market surveillance authority, and shall suspend the use of that system.",
            ),
            SRC_113_ANNEX3,
        ),
    ),
    _O(
        key="art26-6-log-retention-deployer",
        title="Log retention (deployer)",
        reference="Art 26(6)",
        applies_from=ANNEX3_DATE,
        applies_from_by_version=_ORIGINAL_ANNEX3,
        summary=(
            "Deployers keep the automatically generated logs under their control for a period "
            "appropriate to the intended purpose, of at least six months unless other law provides otherwise."
        ),
        risk_tiers=HIGH,
        roles=DEPLOYER_SIDE,
        aliases=frozenset({"annex3-deployer-duties"}),
        evidence=[RETENTION_183],
        sources=(
            _src(
                "Art. 26(6), first subparagraph", "art_26",
                "Deployers of high-risk AI systems shall keep the logs automatically generated by that "
                "high-risk AI system to the extent such logs are under their control, for a period "
                "appropriate to the intended purpose of the high-risk AI system, of at least six months",
            ),
            SRC_113_ANNEX3,
        ),
    ),
    _O(
        key="art26-7-worker-information",
        title="Information to workers",
        reference="Art 26(7)",
        applies_from=ANNEX3_DATE,
        applies_from_by_version=_ORIGINAL_ANNEX3,
        summary=(
            "Deployers who are employers inform workers' representatives and affected workers before "
            "putting into service or using the system at the workplace."
        ),
        risk_tiers=HIGH,
        roles=DEPLOYER_SIDE,
        aliases=frozenset({"annex3-deployer-duties"}),
        evidence=[
            _declared(
                "declared.worker_information",
                "frameworks.eu-ai-act.worker_information_reference",
                "Worker information is declared.",
                "Organisational duty; the information given is not evaluated.",
            )
        ],
        limit="Organisational duty, evidenced by declaration only.",
        sources=(
            _src(
                "Art. 26(7)", "art_26",
                "Before putting into service or using a high-risk AI system at the workplace, deployers "
                "who are employers shall inform workers’ representatives and the affected workers that "
                "they will be subject to the use of the high-risk AI system.",
            ),
            SRC_113_ANNEX3,
        ),
    ),
    _O(
        key="art26-11-affected-person-information",
        title="Information to affected persons",
        reference="Art 26(11)",
        applies_from=ANNEX3_DATE,
        applies_from_by_version=_ORIGINAL_ANNEX3,
        summary=(
            "Deployers of Annex III systems that make, or assist in making, decisions related to "
            "natural persons inform those persons that they are subject to the use of the system."
        ),
        risk_tiers=HIGH,
        roles=DEPLOYER_SIDE,
        needs_review=True,
        review_note=(
            "Applies to Annex III systems that make or assist decisions related to natural persons "
            "(law-enforcement use follows Directive (EU) 2016/680 Art 13) — confirm the system is in scope."
        ),
        evidence=[NOTICE_TEXT],
        limit=(
            "The notice text is declared; whether each affected person was informed — often outside "
            "the conversation the gateway sees — is not shown."
        ),
        sources=(
            _src(
                "Art. 26(11)", "art_26",
                "deployers of high-risk AI systems referred to in Annex III that make decisions or "
                "assist in making decisions related to natural persons shall inform the natural persons "
                "that they are subject to the use of the high-risk AI system.",
            ),
            SRC_113_ANNEX3,
        ),
    ),
    _O(
        key="art27-fria",
        title="Fundamental rights impact assessment",
        reference="Art 27",
        applies_from=ANNEX3_DATE,
        applies_from_by_version=_ORIGINAL_ANNEX3,
        summary=(
            "Before first use of an Art 6(2) high-risk system (except Annex III point 2, critical "
            "infrastructure), deployers that are bodies governed by public law or private entities "
            "providing public services, and deployers of Annex III 5(b)/(c) systems, assess the "
            "impact on fundamental rights and notify the market surveillance authority of the results."
        ),
        risk_tiers=HIGH,
        roles=DEPLOYER_SIDE,
        needs_review=True,
        review_note=(
            "Applies to public bodies, public-service providers and Annex III 5(b)/(c) "
            "deployers, not to Annex III point 2 systems — confirm whether the deployer is in scope."
        ),
        aliases=frozenset({"annex3-deployer-duties"}),
        evidence=[_pointer("impact_assessment", "A fundamental rights impact assessment is on file.")],
        sources=(
            _src(
                "Art. 27(1)", "art_27",
                "deployers that are bodies governed by public law, or are private entities providing "
                "public services, and deployers of high-risk AI systems referred to in points 5 (b) and "
                "(c) of Annex III, shall perform an assessment of the impact on fundamental rights",
            ),
            SRC_113_ANNEX3,
        ),
    ),
    _O(
        key="art49-registration",
        title="Registration in the EU database",
        reference="Art 49(1), (3); Art 26(8); Art 71",
        applies_from=ANNEX3_DATE,
        applies_from_by_version=_ORIGINAL_ANNEX3,
        summary=(
            "Before placing on the market or putting into service an Annex III high-risk system "
            "(except point 2, registered nationally), the provider registers itself and the system in "
            "the EU database; deployers that are public authorities or EU bodies register their use "
            "and do not use a system they find unregistered (Art 26(8)). Annex I systems are not "
            "registered under Art 49; Art 6(3) not-high-risk registration is art6-4-non-high-risk-assessment."
        ),
        risk_tiers=HIGH,
        aliases=frozenset({"annex3-provider-duties"}),
        evidence=[REGISTRATION],
        sources=(
            _src(
                "Art. 49(1)", "art_49",
                "Before placing on the market or putting into service a high-risk AI system listed in "
                "Annex III, with the exception of high-risk AI systems referred to in point 2 of Annex "
                "III, the provider or, where applicable, the authorised representative shall register "
                "themselves and their system in the EU database referred to in Article 71.",
            ),
            _src(
                "Art. 49(3)", "art_49",
                "deployers that are public authorities, Union institutions, bodies, offices or agencies "
                "or persons acting on their behalf shall register themselves, select the system and "
                "register its use in the EU database",
            ),
            _src(
                "Art. 26(8)", "art_26",
                "When such deployers find that the high-risk AI system that they envisage using has not "
                "been registered in the EU database referred to in Article 71, they shall not use that "
                "system and shall inform the provider or the distributor.",
            ),
            SRC_113_ANNEX3,
        ),
    ),
    _O(
        key="art50-1-ai-interaction-notice",
        title="AI-interaction notice",
        reference="Art 50(1), (5)",
        applies_from=date(2026, 8, 2),
        summary=(
            "Providers design systems intended to interact directly with natural persons so that "
            "those persons are informed they are interacting with an AI, unless obvious to a "
            "reasonably well-informed person; the information is given clearly at the latest at the "
            "first interaction (Art 50(5))."
        ),
        roles=ART50_PROVIDERS,
        flags=frozenset({"interacts_with_natural_persons"}),
        aliases=frozenset({"art50-transparency"}),
        evidence=[
            NOTICE_BEFORE_FIRST_TURN,
            NOTICE_TEXT,
            _judge("notice_clear", "Sampled notices are clear and distinguishable."),
        ],
        sources=(
            _src(
                "Art. 50(1)", "art_50",
                "Providers shall ensure that AI systems intended to interact directly with natural "
                "persons are designed and developed in such a way that the natural persons concerned "
                "are informed that they are interacting with an AI system",
            ),
            SRC_113_GENERAL,
        ),
    ),
    _O(
        key="art50-2-synthetic-marking",
        title="Marking of synthetic content",
        reference="Art 50(2)",
        applies_from=date(2026, 8, 2),
        summary=(
            "Providers of systems generating synthetic audio, image, video or text ensure outputs are "
            "marked in a machine-readable format and detectable as artificially generated or "
            "manipulated (systems placed on the market before 2 August 2026: see art50-legacy)."
        ),
        roles=ART50_PROVIDERS,
        flags=frozenset({"generates_synthetic_content"}),
        aliases=frozenset({"art50-transparency"}),
        evidence=[SYNTHETIC_MARKING],
        limit="Content-level marking is not provided by the gateway.",
        sources=(
            _src(
                "Art. 50(2)", "art_50",
                "shall ensure that the outputs of the AI system are marked in a machine-readable format "
                "and detectable as artificially generated or manipulated.",
            ),
            SRC_113_GENERAL,
        ),
    ),
    _O(
        key="art50-legacy",
        title="Synthetic-content marking for legacy systems",
        reference="Art 111(4) as inserted by Reg. 2026/1744",
        applies_from=date(2026, 12, 2),
        summary=(
            "Providers of systems generating synthetic content that were placed on the market before "
            "2 August 2026 comply with Art 50(2) by 2 December 2026 (a transitional period)."
        ),
        roles=ART50_PROVIDERS,
        flags=frozenset({"generates_synthetic_content"}),
        introduced_in=V_OMNIBUS,
        needs_review=True,
        review_note="Applies to systems placed on the market before 2 August 2026 — confirm the placement date.",
        evidence=[SYNTHETIC_MARKING],
        sources=(
            _src(
                "Art. 111(4)", "art_111",
                "Providers of AI systems, including general-purpose AI systems, generating synthetic "
                "audio, image, video or text content, that have been placed on the market before "
                "2 August 2026 shall take the necessary steps in order to comply with Article 50(2) "
                "by 2 December 2026.",
            ),
        ),
    ),
    _O(
        key="art50-4-deepfake-text-disclosure",
        title="Deep fake and public-interest text disclosure",
        reference="Art 50(4)",
        applies_from=date(2026, 8, 2),
        summary=(
            "Deployers disclose deep fakes, and AI-generated text published to inform the public on "
            "matters of public interest unless it underwent human review or editorial control under "
            "someone's editorial responsibility."
        ),
        roles=DEPLOYER_SIDE,
        flags=frozenset({"generates_synthetic_content"}),
        aliases=frozenset({"art50-transparency"}),
        evidence=[
            _declared(
                "declared.deepfake_disclosure",
                "frameworks.eu-ai-act.deepfake_disclosure_approach",
                "The disclosure approach for deep fakes and public-interest text is declared.",
                "Publication happens outside the gateway; the disclosure is not observed.",
            )
        ],
        sources=(
            _src(
                "Art. 50(4), first subparagraph", "art_50",
                "Deployers of an AI system that generates or manipulates image, audio or video content "
                "constituting a deep fake, shall disclose that the content has been artificially "
                "generated or manipulated.",
            ),
            _src(
                "Art. 50(4), second subparagraph", "art_50",
                "Deployers of an AI system that generates or manipulates text which is published with "
                "the purpose of informing the public on matters of public interest shall disclose that "
                "the text has been artificially generated or manipulated.",
            ),
            SRC_113_GENERAL,
        ),
    ),
    _O(
        key="art72-post-market-monitoring",
        title="Post-market monitoring",
        reference="Art 72",
        applies_from=ANNEX3_DATE,
        applies_from_by_version=_ORIGINAL_ANNEX3,
        summary=(
            "Providers establish and document a post-market monitoring system, proportionate to the "
            "risks and based on a plan in the technical documentation, that collects and analyses "
            "performance data over the lifetime — the run ledger and drift findings are raw material."
        ),
        risk_tiers=HIGH,
        roles=PROVIDER_SIDE,
        aliases=frozenset({"art72-post-market"}),
        evidence=[DAILY_HEALTH],
        sources=(
            _src(
                "Art. 72(1)", "art_72",
                "Providers shall establish and document a post-market monitoring system in a manner "
                "that is proportionate to the nature of the AI technologies and the risks of the "
                "high-risk AI system.",
            ),
            SRC_113_ANNEX3,
        ),
    ),
    _O(
        key="art73-serious-incidents",
        title="Reporting of serious incidents",
        reference="Art 73(1)-(4); Art 26(5)",
        applies_from=ANNEX3_DATE,
        applies_from_by_version=_ORIGINAL_ANNEX3,
        summary=(
            "Providers report serious incidents to the market surveillance authority immediately on "
            "establishing a causal link (or its reasonable likelihood) and at the latest 15 days after "
            "the provider or deployer becomes aware; 10 days on a death; 2 days for a widespread "
            "infringement or serious disruption of critical infrastructure. Deployers inform the "
            "provider first (Art 26(5)) and report themselves if it cannot be reached."
        ),
        risk_tiers=HIGH,
        evidence=[INCIDENT_REGISTER, INCIDENT_DEADLINE],
        sources=(
            _src(
                "Art. 73(1)", "art_73",
                "Providers of high-risk AI systems placed on the Union market shall report any serious "
                "incident to the market surveillance authorities of the Member States where that "
                "incident occurred.",
            ),
            _src(
                "Art. 73(2)", "art_73",
                "not later than 15 days after the provider or, where applicable, the deployer, becomes "
                "aware of the serious incident.",
            ),
            _src(
                "Art. 73(3)", "art_73",
                "the report referred to in paragraph 1 of this Article shall be provided immediately, "
                "and not later than two days after the provider or, where applicable, the deployer "
                "becomes aware of that incident.",
            ),
            _src(
                "Art. 73(4)", "art_73",
                "but not later than 10 days after the date on which the provider or, where applicable, "
                "the deployer becomes aware of the serious incident.",
            ),
            SRC_113_ANNEX3,
        ),
    ),
    _O(
        key="annex1-embedded",
        title="High-risk duties for Annex I embedded systems",
        reference="Art 6(1), Annex I; Art 2(2)",
        applies_from=date(2028, 8, 2),
        applies_from_by_version=_ORIGINAL_ANNEX1,
        summary=(
            "High-risk duties where the AI system is a safety component of (or is itself) an Annex I "
            "product required to undergo third-party conformity assessment — medical devices, toys, "
            "lifts… For Annex I Section B products (incl. machinery since Reg. 2026/1744) only "
            "Art 6(1), Art 60a and Arts 102-112 apply (Art 2(2))."
        ),
        risk_tiers=HIGH,
        needs_review=True,
        review_note=(
            "Brutor cannot infer whether the system is embedded in an Annex I product — "
            "review and record the determination."
        ),
        evidence=[
            _declared(
                "declared.annex",
                "frameworks.eu-ai-act.annex",
                "The Annex I / Annex III determination is declared.",
                "The determination is the operator's; it is not evaluated.",
            )
        ],
        sources=(
            _src(
                "Art. 6(1)(a)", "art_6",
                "the AI system is intended to be used as a safety component of a product, or the AI "
                "system is itself a product, covered by the Union harmonisation legislation listed in Annex I;",
            ),
            SRC_113_ANNEX1,
            SRC_113_ORIGINAL_ANNEX1,
        ),
    ),
    _O(
        key="art53-gpai",
        title="GPAI model provider duties",
        reference="Art 53(1), Annex XI/XII",
        applies_from=date(2025, 8, 2),
        summary=(
            "Technical documentation, downstream information, copyright policy and "
            "training-content summary for general-purpose AI models (models placed on the market "
            "before 2 August 2025: by 2 August 2027, Art 111(3)). Attaches to the MODEL provider — "
            "this system is exposed through the GPAI models it binds."
        ),
        requires_gpai=GPAI,
        evidence=[
            _declared(
                "declared.gpai_documentation",
                "frameworks.eu-ai-act.gpai_documentation_reference",
                "The GPAI provider's downstream documentation is referenced.",
                "Model-provider duty; the documentation is not evaluated.",
            )
        ],
        sources=(
            _src(
                "Art. 53(1)(a)", "art_53",
                "draw up and keep up-to-date the technical documentation of the model, including its "
                "training and testing process and the results of its evaluation",
            ),
            _src(
                "Art. 53(1)(b)", "art_53",
                "draw up, keep up-to-date and make available information and documentation to providers "
                "of AI systems who intend to integrate the general-purpose AI model into their AI systems.",
            ),
            SRC_113_GPAI,
        ),
    ),
    _O(
        key="art55-gpai-systemic",
        title="Systemic-risk GPAI duties",
        reference="Art 55(1)",
        applies_from=date(2025, 8, 2),
        summary=(
            "Model evaluation including adversarial testing, assessment and mitigation of systemic "
            "risks, serious-incident tracking and reporting, and cybersecurity for GPAI models with "
            "systemic risk. Attaches to the MODEL provider."
        ),
        requires_gpai=GPAI_SYSTEMIC_RISK,
        evidence=[
            _declared(
                "declared.gpai_documentation",
                "frameworks.eu-ai-act.gpai_documentation_reference",
                "The GPAI provider's downstream documentation is referenced.",
                "Model-provider duty; the documentation is not evaluated.",
            )
        ],
        sources=(
            _src(
                "Art. 55(1)(a)", "art_55",
                "perform model evaluation in accordance with standardised protocols and tools reflecting "
                "the state of the art, including conducting and documenting adversarial testing of the model",
            ),
            SRC_113_GPAI,
        ),
    ),
    # -- obligations added after the first catalog ---------------------------
    _O(
        key="art6-4-non-high-risk-assessment",
        title="Annex III system assessed as not high-risk: documentation and registration",
        reference="Art 6(3)-(4); Art 49(2)",
        applies_from=ANNEX3_DATE,
        applies_from_by_version=_ORIGINAL_ANNEX3,
        summary=(
            "A provider who considers that an Annex III system is not high-risk under Art 6(3) "
            "documents that assessment before placing it on the market or putting it into service, "
            "provides it to national competent authorities on request, and registers itself and the "
            "system in the EU database (Art 49(2)). An Annex III system that profiles natural persons "
            "is always high-risk."
        ),
        risk_tiers=NOT_HIGH,
        roles=ANNEX3_SELF_ASSESSORS,
        needs_review=True,
        review_note=(
            "Applies only where the system is listed in Annex III and the provider concluded under "
            "Art 6(3) that it is not high-risk — declare the Annex III determination (`annex`) to "
            "confirm; a system outside Annex III is not in scope."
        ),
        evidence=[
            _declared(
                "declared.annex",
                "frameworks.eu-ai-act.annex",
                "The Annex I / Annex III determination is declared.",
                "The determination is the operator's; it is not evaluated.",
            ),
            _pointer("risk_assessment", "The Art 6(3) assessment is on file."),
            REGISTRATION,
        ],
        limit=(
            "The assessment is a document outside the gateway; its reasoning is not evaluated, and "
            "whether the system falls in Annex III is the operator's declaration."
        ),
        sources=(
            _src(
                "Art. 6(4)", "art_6",
                "A provider who considers that an AI system referred to in Annex III is not high-risk "
                "shall document its assessment before that system is placed on the market or put into "
                "service. Such provider shall be subject to the registration obligation set out in "
                "Article 49(2).",
            ),
            _src(
                "Art. 49(2)", "art_49",
                "Before placing on the market or putting into service an AI system for which the provider "
                "has concluded that it is not high-risk according to Article 6(3), that provider or, where "
                "applicable, the authorised representative shall register themselves and that system",
            ),
            _src(
                "Art. 6(3), third subparagraph", "art_6",
                "an AI system referred to in Annex III shall always be considered to be high-risk where "
                "the AI system performs profiling of natural persons.",
            ),
            SRC_113_ANNEX3,
        ),
    ),
    _O(
        key="art43-conformity-assessment",
        title="Conformity assessment",
        reference="Art 16(f), Art 43",
        applies_from=ANNEX3_DATE,
        applies_from_by_version=_ORIGINAL_ANNEX3,
        summary=(
            "Before placing on the market or putting into service, the provider has the system undergo "
            "the relevant conformity assessment procedure: internal control (Annex VI) for Annex III "
            "points 2 to 8; for point 1 (biometrics) Annex VI or a notified body (Annex VII) depending "
            "on the harmonised standards applied; for Annex I products, the sectoral procedure (Art 43(3))."
        ),
        risk_tiers=HIGH,
        roles=PROVIDER_SIDE,
        evidence=[
            _declared(
                "declared.conformity_assessment",
                "frameworks.eu-ai-act.conformity_assessment",
                "The conformity assessment route (internal control or notified body) is declared.",
                "A declared route is not a completed assessment; the procedure and its outcome are not evaluated.",
            )
        ],
        limit="Organisational duty, evidenced by declaration only.",
        sources=(
            _src(
                "Art. 16(f)", "art_16",
                "ensure that the high-risk AI system undergoes the relevant conformity assessment "
                "procedure as referred to in Article 43, prior to its being placed on the market or put "
                "into service;",
            ),
            _src(
                "Art. 43(2)", "art_43",
                "For high-risk AI systems referred to in points 2 to 8 of Annex III, providers shall follow "
                "the conformity assessment procedure based on internal control as referred to in Annex VI",
            ),
            SRC_113_ANNEX3,
        ),
    ),
    _O(
        key="art47-48-declaration-ce-marking",
        title="EU declaration of conformity and CE marking",
        reference="Art 16(g)-(h), Art 47, Art 48",
        applies_from=ANNEX3_DATE,
        applies_from_by_version=_ORIGINAL_ANNEX3,
        summary=(
            "The provider draws up an EU declaration of conformity for each high-risk system and keeps "
            "it at the disposal of national competent authorities for 10 years after placing on the "
            "market or putting into service, and affixes the CE marking (a digital marking for systems "
            "provided digitally)."
        ),
        risk_tiers=HIGH,
        roles=PROVIDER_SIDE,
        evidence=[_pointer("other", "The EU declaration of conformity is on file.")],
        limit="The declaration is evidenced by pointer only; the CE marking is not observed by the gateway.",
        sources=(
            _src(
                "Art. 47(1)", "art_47",
                "The provider shall draw up a written machine readable, physical or electronically signed "
                "EU declaration of conformity for each high-risk AI system, and keep it at the disposal of "
                "the national competent authorities for 10 years",
            ),
            _src(
                "Art. 16(h)", "art_16",
                "affix the CE marking to the high-risk AI system or, where that is not possible, on its "
                "packaging or its accompanying documentation",
            ),
            _src(
                "Art. 48(2)", "art_48",
                "For high-risk AI systems provided digitally, a digital CE marking shall be used",
            ),
            SRC_113_ANNEX3,
        ),
    ),
    _O(
        key="art26-4-input-data",
        title="Relevance of input data (deployer)",
        reference="Art 26(4)",
        applies_from=ANNEX3_DATE,
        applies_from_by_version=_ORIGINAL_ANNEX3,
        summary=(
            "To the extent the deployer exercises control over the input data, it ensures that input "
            "data is relevant and sufficiently representative in view of the system's intended purpose."
        ),
        risk_tiers=HIGH,
        roles=DEPLOYER_SIDE,
        evidence=[_pointer("data_governance", "A data-governance record is on file.")],
        limit="Input-data relevance is not evaluated by the gateway; evidenced by pointer only.",
        sources=(
            _src(
                "Art. 26(4)", "art_26",
                "to the extent the deployer exercises control over the input data, that deployer shall "
                "ensure that input data is relevant and sufficiently representative in view of the "
                "intended purpose of the high-risk AI system.",
            ),
            SRC_113_ANNEX3,
        ),
    ),
    _O(
        key="art50-3-emotion-biometric-notice",
        title="Emotion recognition and biometric categorisation notice",
        reference="Art 50(3), (5)",
        applies_from=date(2026, 8, 2),
        summary=(
            "Deployers of an emotion recognition or biometric categorisation system inform the natural "
            "persons exposed to it of its operation, at the latest at first exposure, and process the "
            "personal data under the GDPR / EUDPR / LED as applicable."
        ),
        roles=DEPLOYER_SIDE,
        needs_review=True,
        review_note=(
            "Applies only to deployers of an emotion recognition or biometric categorisation "
            "system (law-enforcement uses permitted by law excepted) — confirm the system is one."
        ),
        evidence=[NOTICE_TEXT],
        limit=(
            "The notice text is declared; whether each exposed person was informed, and the lawfulness "
            "of the processing, are not shown."
        ),
        sources=(
            _src(
                "Art. 50(3)", "art_50",
                "Deployers of an emotion recognition system or a biometric categorisation system shall "
                "inform the natural persons exposed thereto of the operation of the system",
            ),
            _src(
                "Art. 50(5)", "art_50",
                "shall be provided to the natural persons concerned in a clear and distinguishable manner "
                "at the latest at the time of the first interaction or exposure.",
            ),
            SRC_113_GENERAL,
        ),
    ),
    _O(
        key="art86-right-to-explanation",
        title="Explanation of individual decision-making",
        reference="Art 86(1), (3)",
        applies_from=ANNEX3_DATE,
        applies_from_by_version=_ORIGINAL_ANNEX3,
        summary=(
            "A person subject to a deployer's decision based on the output of an Annex III system "
            "(other than point 2) that produces legal or similarly significant effects they consider "
            "adverse may obtain from the deployer clear and meaningful explanations of the system's "
            "role and the main elements of the decision — to the extent not otherwise provided for "
            "under Union law."
        ),
        risk_tiers=HIGH,
        roles=DEPLOYER_SIDE,
        needs_review=True,
        review_note=(
            "Applies to decisions based on Annex III systems (not point 2) with legal or similarly "
            "significant effects — confirm the system is used for such decisions."
        ),
        evidence=[
            _declared(
                "declared.explanation_procedure",
                "frameworks.eu-ai-act.explanation_procedure_reference",
                "A procedure for answering explanation requests is declared.",
                "The procedure is organisational; explanations given are not observed.",
            )
        ],
        limit=(
            "Explanations are given outside the gateway; the run ledger can show the system's part in "
            "a decision, but no statement shows an explanation was requested or given."
        ),
        sources=(
            _src(
                "Art. 86(1)", "art_86",
                "shall have the right to obtain from the deployer clear and meaningful explanations of the "
                "role of the AI system in the decision-making procedure and the main elements of the "
                "decision taken.",
            ),
            _src(
                "Art. 86(3)", "art_86",
                "This Article shall apply only to the extent that the right referred to in paragraph 1 is "
                "not otherwise provided for under Union law.",
            ),
            SRC_113_ANNEX3,
        ),
    ),
]

#: Art 111(2) — the transitional rule every high-risk obligation carries.
SRC_111_2 = _src(
    "Art. 111(2)", "art_111",
    "that have been placed on the market or put into service before "
    "the date of application of Chapter III referred to in Article 113, only if, as from that date, "
    "those systems are subject to significant changes in their designs.",
)
SRC_111_2_PUBLIC = _src(
    "Art. 111(2)", "art_111",
    "the providers and deployers of high-risk AI systems intended to be used by public authorities shall "
    "take the necessary steps to comply with the requirements and obligations laid down in this "
    "Regulation by 2 August 2030.",
)
LEGACY_HIGH_RISK_NOTE = (
    " Transitional rule (Art 111(2)): a high-risk system placed on the market or put into service "
    "before the Chapter III date of application is caught only if its design changes significantly "
    "from that date; systems intended for use by public authorities must meet the obligations by "
    "2 August 2030."
)


def _with_legacy_rule(ob: Obligation) -> Obligation:
    return _replace(
        ob,
        summary=ob.summary + LEGACY_HIGH_RISK_NOTE,
        sources=ob.sources + (SRC_111_2, SRC_111_2_PUBLIC),
    )


OBLIGATIONS = [_with_legacy_rule(o) if o.risk_tiers == HIGH else o for o in OBLIGATIONS]


FRAMEWORK = Framework(
    id=FRAMEWORK_ID,
    title="EU AI Act",
    jurisdiction="EU",
    reference="Regulation (EU) 2024/1689, as amended by Regulation (EU) 2026/1744",
    kind=KIND_REGULATION,
    obligations=OBLIGATIONS,
    versions=(
        CatalogVersion(
            V_ORIGINAL,
            effective_from=date(2024, 8, 1),
            effective_to=OMNIBUS_IN_FORCE,
            note="Regulation (EU) 2024/1689 as published",
        ),
        CatalogVersion(
            V_OMNIBUS,
            effective_from=OMNIBUS_IN_FORCE,
            note="As amended by the Digital Omnibus on AI, Regulation (EU) 2026/1744",
        ),
    ),
    roles=frozenset(EU_AI_ACT_ROLES),
    classifications=frozenset(RISK_TIERS),
    role_field="role",
    classification_field="risk_tier",
    deadline_rules=MappingProxyType({
        "standard": timedelta(days=15),
        "death": timedelta(days=10),
        "widespread": timedelta(days=2),
    }),
    retention_floors=MappingProxyType({"high": 183}),
    # Every obligation cites the consolidated text (02024R1689-20260727),
    # checked 2026-10-16 (tests/test_compliance_catalog_provenance.py).
    verified_against_instrument=True,
    instrument_status=INSTRUMENT_FINAL,
    verified_on=CHECKED_ON,
    tag_key="eu_ai_act",
    # A component that contributes to a high-risk decision is
    # assessed with it (draft Commission classification guidelines).
    composite_classification=True,
)

#: Old keys → the obligations that replaced them.
KEY_ALIASES: Dict[str, list] = {}
for _ob in OBLIGATIONS:
    for _alias in _ob.aliases:
        KEY_ALIASES.setdefault(_alias, []).append(_ob.key)

__all__ = [
    "ART50_PROVIDERS",
    "DEPLOYER_SIDE",
    "FRAMEWORK",
    "FRAMEWORK_ID",
    "KEY_ALIASES",
    "OMNIBUS_IN_FORCE",
    "PROVIDER_SIDE",
    "V_OMNIBUS",
    "V_ORIGINAL",
]
