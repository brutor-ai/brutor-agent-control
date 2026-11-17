# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# ISO/IEC 42001:2023 catalog — Annex A controls (`standard`)

Checked on 2026-10-16 against a licensed copy of the full text of
ISO/IEC 42001:2023, first edition, 2023-12 (no amendment listed on iso.org
as of that date): clauses 4–10, Annex A (normative, Table A.1 — control
objectives and controls) and Annex B (implementation guidance). Every
control number and title below was checked against Table A.1; every
summary is this catalog's own paraphrase, not the standard's wording.

Fingerprint method. The standard is copyrighted against reproduction, so
no source quotes it: `Source.excerpt` is empty and each source instead
carries the SHA-256 of the passage it was checked against (after
`normalise_passage`), the passage's first six words as a locator, and its
length. Passages are taken from `pdftotext -enc UTF-8` (reading order, no
layout). Where a Table A.1 cell is split by the table layout in that
extraction, the source fingerprints the same control as restated under
Annex B (provision "B.x.y"). Anyone holding a licensed copy of the same
edition can re-check every fingerprint with:

    .venv-test/bin/python scripts/verify_licensed_sources.py iso-42001 <ISO_IEC_42001_2023.pdf>

An AI management system standard: the organisation determines and
implements its controls (its Statement of Applicability, clause 6.1.3,
selects them) and is certified; a system is not. Each entry here is a
control, or a clause, the gateway can produce evidence toward — the event
log (A.6.2.8), operation and monitoring (A.6.2.6), deployment discipline
(A.6.2.5), documentation (A.6.2.3, A.6.2.7, A.8.2) — and the
organisational controls (AI policy, roles, resources, impact assessment,
data, third parties) are evidenced by pointer and declaration only.

Retention: the standard sets no period. Impact-assessment results are kept
for a period the organisation defines (A.5.3, clause 8.4) and the Annex B
guidance on event logs (B.6.2.8) defers to the intended use and the
organisation's own retention policies, so the catalog carries no retention
floor. Applicability is estate-wide (no role or classification
vocabulary); `frameworks.iso-42001.in_scope` scopes the AIMS. Not legal or
certification advice.
"""

from __future__ import annotations

from datetime import date

from app.services.compliance.catalogs import common as c
from app.services.compliance.catalogs.base import (
    INSTRUMENT_FINAL,
    KIND_STANDARD,
    CatalogVersion,
    Framework,
    Obligation,
    Source,
)

FRAMEWORK_ID = "iso-42001"
CATALOG_VERSION = "2026-09"

#: Publication of ISO/IEC 42001:2023 (first edition, 2023-12).
PUBLISHED = date(2023, 12, 18)

#: Checked against the licensed full text.
CHECKED = date(2026, 10, 16)

#: ISO's catalogue page for ISO/IEC 42001:2023.
_ISO_URL = "https://www.iso.org/standard/81230.html"
_INSTRUMENT = "ISO/IEC 42001:2023 (first edition, 2023-12)"

#: Passage fingerprints: provision -> (sha256, first six words, length) of
#: the normalised passage in the licensed text. No ISO text is stored.
_FINGERPRINTS = {
    "Clause 5.2": ("8c357b72bb6649dd8216d3614409bf64b7984f1b6bcdc5b60b683e1260617732", "Top management shall establish an AI", 49),
    "Clause 6.1.3 f)": ("f2e8de7bdbf384496d1d5b730cd7b1d4d8b7d5c1c58f772e174ed016b06b3f0c", "produce a statement of applicability that", 160),
    "Clause 6.1.4": ("9e123d3018e1768c51ef68ba58d686e27872a73d87c54b767cf01376e97ce148", "The organization shall define a process", 215),
    "Clause 8.4": ("8cd2cfcf1254a931aeb459b4e712cf70a25d1178a5b89b1bd4b5ba0f320759bb", "The organization shall perform AI system", 150),
    "A.2.2": ("21f3347c4db9f1f8a2e726a492734328696baa7d108c0396104a792a56f0c777", "The organization shall document a policy", 82),
    "A.2.3": ("7b33ce2f8c9d7c38a0042a3622345cd0ba0f8d72f25fb1289e92751d0f7073a2", "The organization shall determine where other", 143),
    "A.2.4": ("9f166c9a5406d01ac2bc0eb5456b111cdaaeee13ef5e074b035de944d26da488", "The AI policy shall be reviewed", 144),
    "A.3.2": ("b64b879a8a92c8d715d8f160a1f1f66d351a475c160be396b44859aee673b60e", "Roles and responsibilities for AI shall", 108),
    "A.3.3": ("079f7ca8dac02a575837ffe5ec4391b4cd9f9280c81ef6ef7092c94b4c8aded6", "The organization shall define and put", 161),
    "A.4.2": ("a07cd7371ed6a7ecf32419a2eca9d7ac12d7f593dd9159ffbc4aaf5990971540", "The organization shall identify and document", 191),
    "A.4.3": ("55cab933f72b4cebcd32eb804a935de62bb09ea1f90df0f661000980df25ce1e", "As part of resource identification, the", 132),
    "A.4.4": ("11c96c7cc8cd358228c9f4030748613405da7971d0f9489d28382b1b95dca5d2", "As part of resource identification, the", 135),
    "A.4.5": ("fad09e596ea4c15daa626ea566dc750a57f545cadf209c83a6864d148bd5b4bd", "As part of resource identification, the", 148),
    "A.4.6": ("7c5441d9dc882485d6ee90e1b4288fcda60f88c86d7a5f830391813ce27e844e", "As part of resource identification, the", 300),
    "A.5.2": ("d61663e560eee7ce07958afec1c259e60c2e7b29ebe8dc0a824f1025400148d0", "The organization shall establish a process", 206),
    "A.5.3": ("6550c7c7577c181d073685365ba9907562714ca0800df246d3a19e37f8b22e96", "The organization shall document the results", 116),
    "A.5.4": ("8d0c4997c1dbfc46d99e83e602fa3f509c97863ad125c988e1cf8a6eefa6f83f", "The organization shall assess and document", 154),
    "A.5.5": ("4bea97f58be3a354c8c2001792ee0c6b2e95a9b6c77c83678ec70b91c65af704", "The organization shall assess and document", 122),
    "B.6.1.2": ("0ba3f7d4a1cecf7f308b0c6a55de901d881828695fa55bbdd2cafbb644f1bbbe", "The organization should identify and document", 217),
    "A.6.1.3": ("85d2e308abf2c31a58086ab6eeec425386c13a36dc3a80a446c5857eba226fc1", "The organization shall define and document", 126),
    "A.6.2.2": ("c2bb7bf6055962a1a98b8718738f72ac4b0f1f0927601eec739c1128986e608e", "The organization shall specify and document", 121),
    "B.6.2.3": ("591b304189cee41490800e5bc9d4393cee5691c825cc09f7d39cea688c67a37b", "The organization should document the AI", 157),
    "A.6.2.4": ("3d234d9c0600e92d15a5af6231678458e90feddcd4ded4fe94473c90915377a2", "The organization shall define and document", 133),
    "A.6.2.5": ("ca5beb8ade5f4a085561802aa9acc4ef8781e093852740f3930ddbd15f9cd99f", "The organization shall document a deployment", 119),
    "A.6.2.6": ("d1710be8cbebbc9eb8001a8428702e9699f9030d4738b5c5cbe81f7bca3d4390", "The organization shall define and document", 210),
    "A.6.2.7": ("e2195b60f18800ab102508a0e7267314c810800b646239b8f4324ea730d8fab8", "The organization shall determine what AI", 254),
    "A.6.2.8": ("8c40ad6d7ec4a4ecca561777e69f6910c186ee08ffb65d98f9b84be4296e39e3", "The organization shall determine at which", 174),
    "B.6.2.8": ("980d501f1c9d9b38dfd6c13063c519010e6c58fa1a8711752b19a43463e101ff", "Event logs should be kept for", 147),
    "B.7.2": ("abd2db3f09a360f347890c12997d55809abc81ac55435e54ce71443b746f01b7", "The organization should define, document and", 122),
    "A.7.3": ("f8a6992dd7dd976e1795152f38a7f8695dfadcc0d4b2f48560a24572d5a77f04", "The organization shall determine and document", 121),
    "A.7.4": ("40efbfd24a6b308bc8693d6f48851be9c88d2d34a339f9426a48f16cf1ed2ca6", "The organization shall define and document", 160),
    "A.7.5": ("94c32c4feb37f0be2f7bc5375f28726e5df29a322864bfe66938ede65471a894", "The organization shall define and document", 164),
    "A.7.6": ("1d9c05dba1d1d30dd09010d53c4545494849bcf36478bd58d8f6d6d4b65aee66", "The organization shall define and document", 132),
    "A.8.2": ("ca7addf68a047552b51488e379516cd98b62e39f78b35eea7bad99cbda5b1f30", "The organization shall determine and provide", 97),
    "B.8.2": ("925764df156b40199e88f1732e2d51643faefb71c8f3bf1c7e16f4feffa4ad3e", "general notifications to users that they", 101),
    "A.8.3": ("b1640f2b50d1c027e756466cfe2bb34c9aa964709fad7f5c6d9c3d37b1098274", "The organization shall provide capabilities for", 110),
    "A.8.4": ("975cbb51e9eea556dddea88549bd953292219e740064fac3c9d4d66c12a19001", "The organization shall determine and document", 107),
    "A.8.5": ("cf0957fa9db737c9a5cdac98bf536c7849d359d1b1dd3acd1f75a04ba162b780", "The organization shall determine and document", 131),
    "B.9.2": ("9c858835c561eb9b7175ac08a52fb0c77571f57defcff267490f1674c26cf3f6", "The organization should define and document", 96),
    "B.9.3": ("c32edd9ec3a5a39c423451344393e043f490945c0423cbe23fad8fcd48277cfc", "The organization should identify and document", 100),
    "B.9.3 (human oversight)": ("7b831b639893a6b9ce489d940bbd5eb42dd60440ba722db5235fc992b904b489", "The organization should determine at which", 139),
    "A.9.4": ("71876e20b1d3fba1f4266f735c923314ff54ba145190e45357c39aa0ac76f6f2", "The organization shall ensure that the", 140),
    "A.10.2": ("5d9d2ee309dc668d02118451bb27848aa8f899ffc39f5c58d4f9d50c1f741e8e", "The organization shall ensure that responsibilities", 179),
    "A.10.3": ("1037a29aa928060d9ecd12c158e98f6dcecfe7aa1a39aede99428746f2774bbc", "The organization shall establish a process", 218),
    "A.10.4": ("14555ed7199cf2fa0ee5ba66d779e4036e53098348c66c1cc4249fcaa28956cc", "The organization shall ensure that its", 149),
}


def _src(*provisions: str) -> tuple:
    """Sources for the given provisions, each cited by fingerprint only."""
    out = []
    for p in provisions:
        sha, starts, length = _FINGERPRINTS[p]
        out.append(Source(
            instrument=_INSTRUMENT,
            provision=p,
            url=_ISO_URL,
            checked_on=CHECKED,
            excerpt="",
            excerpt_sha256=sha,
            excerpt_starts_with=starts,
            excerpt_length=length,
        ))
    return tuple(out)


def _declared(field: str, statement: str, limit: str) -> c.EvidenceSpec:
    return c.declared(f"declared.{field}", f"frameworks.{FRAMEWORK_ID}.{field}", statement, limit)


_O = Obligation

OBLIGATIONS = [
    # -- Clause 6.1.3 -------------------------------------------------------
    _O(
        key="c6-1-3-statement-of-applicability",
        title="Statement of applicability",
        reference="Clause 6.1.3 f)",
        applies_from=PUBLISHED,
        sources=_src("Clause 6.1.3 f)"),
        summary=(
            "As part of AI risk treatment the organisation produces a statement of applicability "
            "listing the controls it needs, with reasons for including or excluding each."
        ),
        evidence=[
            _declared(
                "statement_of_applicability_reference",
                "A reference to the organisation's statement of applicability is declared.",
                "The statement is an organisational document; which controls it selects is not evaluated.",
            ),
        ],
        limit="Management-system clause, evidenced by declaration only.",
    ),
    # -- A.2 Policies related to AI -----------------------------------------
    _O(
        key="a2-ai-policy",
        title="AI policy",
        reference="Annex A.2.2–A.2.4; Clause 5.2",
        applies_from=PUBLISHED,
        sources=_src("A.2.2", "A.2.3", "A.2.4", "Clause 5.2"),
        summary=(
            "A documented policy for developing or using AI systems, aligned with the "
            "organisation's other policies and reviewed at planned intervals or when needed."
        ),
        evidence=[
            _declared(
                "ai_policy_reference",
                "A reference to the organisation's AI policy and its last review is declared.",
                "The policy is organisational; its content and review are not evaluated.",
            ),
        ],
        limit="Organisational control, evidenced by declaration only.",
    ),
    # -- A.3 Internal organization ------------------------------------------
    _O(
        key="a3-2-roles-responsibilities",
        title="AI roles and responsibilities",
        reference="Annex A.3.2",
        applies_from=PUBLISHED,
        sources=_src("A.3.2"),
        summary="Roles and responsibilities for AI are defined and allocated to suit the organisation.",
        evidence=[
            _declared(
                "ai_roles_reference",
                "A reference to the allocation of AI roles and responsibilities is declared.",
                "Role allocation is organisational; the gateway sees only the console roles it enforces.",
            ),
            c.override_available("The people who can override or stop this system are assigned."),
        ],
    ),
    _O(
        key="a3-3-reporting-concerns",
        title="Reporting of concerns",
        reference="Annex A.3.3",
        applies_from=PUBLISHED,
        sources=_src("A.3.3"),
        summary=(
            "A process exists, across the life cycle, for raising concerns about the "
            "organisation's role with respect to an AI system."
        ),
        evidence=[
            _declared(
                "concern_reporting_process_reference",
                "The process for reporting concerns is declared.",
                "The reporting channel is organisational; its use is not observable at the gateway.",
            ),
        ],
        limit="Organisational control, evidenced by declaration only.",
    ),
    # -- A.4 Resources for AI systems ---------------------------------------
    _O(
        key="a4-resources",
        title="Resources for AI systems",
        reference="Annex A.4.2–A.4.6",
        applies_from=PUBLISHED,
        sources=_src("A.4.2", "A.4.3", "A.4.4", "A.4.5", "A.4.6"),
        summary=(
            "The resources the AI system needs at each life cycle stage are identified and "
            "documented: data, tooling, system and computing resources, and people and their "
            "competences."
        ),
        evidence=[
            c.least_privilege_declared(
                "The models, tools and servers the system may use are bound and sealed in its grants."
            ),
            _declared(
                "resource_documentation_reference",
                "A reference to the system's resource documentation is declared.",
                "Data, computing and human resources outside the gateway are not observable.",
            ),
        ],
        limit="The gateway records the tooling it brokers; other resources are evidenced by declaration only.",
    ),
    # -- A.5 Assessing impacts of AI systems --------------------------------
    _O(
        key="a5-impact-assessment",
        title="AI system impact assessment",
        reference="Annex A.5.2–A.5.5; Clauses 6.1.4, 8.4",
        applies_from=PUBLISHED,
        sources=_src("A.5.2", "A.5.3", "A.5.4", "A.5.5", "Clause 6.1.4", "Clause 8.4"),
        summary=(
            "A process to assess the potential consequences of the AI system for individuals, "
            "groups and society over its life cycle; assessments are performed at planned "
            "intervals or on significant change, and their results documented and kept for a "
            "period the organisation defines."
        ),
        evidence=[
            c.pointer("impact_assessment", "A current AI system impact assessment is on file."),
            _declared(
                "impact_assessment_process_reference",
                "The impact assessment process is declared.",
                "The process is organisational; the gateway cannot observe it.",
            ),
        ],
        limit="Organisational control, evidenced by pointer and declaration only.",
    ),
    # -- A.6.1 Management guidance for AI system development ----------------
    _O(
        key="a6-1-2-development-objectives",
        title="Objectives for responsible development of AI system",
        reference="Annex A.6.1.2",
        applies_from=PUBLISHED,
        sources=_src("B.6.1.2"),
        summary=(
            "Objectives for responsible development are identified and documented, and measures "
            "to meet them are built into the development life cycle."
        ),
        evidence=[
            _declared(
                "development_objectives_reference",
                "The objectives for responsible development are declared.",
                "The objectives are organisational; whether development meets them is not evaluated.",
            ),
        ],
        limit="Organisational control, evidenced by declaration only.",
    ),
    _O(
        key="a6-1-3-responsible-development",
        title="Processes for responsible AI system design and development",
        reference="Annex A.6.1.3",
        applies_from=PUBLISHED,
        sources=_src("A.6.1.3"),
        summary=(
            "The organisation defines and documents its processes for responsible design and "
            "development — the gateway shows the contract lifecycle gate that every change to "
            "the system's grants goes through."
        ),
        evidence=[
            _declared(
                "development_process_reference",
                "The responsible-development process is declared.",
                "The process is organisational; only its gate at the gateway is observable.",
            ),
            c.no_unapproved_widening(),
        ],
    ),
    # -- A.6.2 AI system life cycle -----------------------------------------
    _O(
        key="a6-2-2-requirements",
        title="AI system requirements and specification",
        reference="Annex A.6.2.2",
        applies_from=PUBLISHED,
        sources=_src("A.6.2.2"),
        summary=(
            "Requirements for a new AI system, or a material enhancement, are specified and "
            "documented — here, the hash-pinned contract minted from the system's declared "
            "intended use and grants."
        ),
        evidence=[
            c.promotions_sealed(
                "The contract that specifies the system was promoted with a sealed record naming its approver.",
                limit="Shows the specification was signed off; the quality of the intended-use text is not evaluated.",
            )
        ],
    ),
    _O(
        key="a6-2-3-design-documentation",
        title="Documentation of AI system design and development",
        reference="Annex A.6.2.3",
        applies_from=PUBLISHED,
        sources=_src("B.6.2.3"),
        summary=(
            "The design and development of the AI system are documented against the "
            "organisation's objectives, requirements and specification criteria."
        ),
        evidence=[
            c.tech_docs(
                FRAMEWORK_ID,
                "A documentation export of the system as configured exists for the active contract version, "
                "with its digest sealed.",
            ),
            _declared(
                "design_documentation_reference",
                "A reference to the design and development documentation is declared.",
                "Design choices made outside the gateway (model training, architecture) are not observable.",
            ),
        ],
    ),
    _O(
        key="a6-2-4-verification-validation",
        title="AI system verification and validation",
        reference="Annex A.6.2.4",
        applies_from=PUBLISHED,
        sources=_src("A.6.2.4"),
        summary="Verification and validation measures for the system are defined and documented, with criteria for their use.",
        evidence=[
            c.promotions_sealed(
                "Contract promotions went through the lifecycle gate (replay suite before promotion).",
                limit="Shows the gate was used; whether the replay suite is sufficient is not evaluated.",
            )
        ],
    ),
    _O(
        key="a6-2-5-deployment",
        title="AI system deployment",
        reference="Annex A.6.2.5",
        applies_from=PUBLISHED,
        sources=_src("A.6.2.5"),
        summary="A deployment plan is documented, and the relevant requirements are met before the system is deployed.",
        evidence=[c.promotions_sealed(), c.no_unapproved_widening()],
    ),
    _O(
        key="a6-2-6-operation-monitoring",
        title="AI system operation and monitoring",
        reference="Annex A.6.2.6",
        applies_from=PUBLISHED,
        sources=_src("A.6.2.6"),
        summary=(
            "The elements for ongoing operation are defined and documented — at least system "
            "and performance monitoring, repairs, updates and support."
        ),
        evidence=[
            c.daily_health(),
            c.suspend_available(
                "Suspension and run-abort controls, part of responding to errors and failures, are "
                "available and every use of them was sealed."
            ),
        ],
    ),
    _O(
        key="a6-2-7-technical-documentation",
        title="AI system technical documentation",
        reference="Annex A.6.2.7",
        applies_from=PUBLISHED,
        sources=_src("A.6.2.7"),
        summary=(
            "The organisation determines what technical documentation each category of interested "
            "party (such as users, partners, supervisory authorities) needs, and provides it in a "
            "suitable form."
        ),
        evidence=[c.tech_docs(FRAMEWORK_ID)],
    ),
    _O(
        key="a6-2-8-event-logs",
        title="AI system recording of event logs",
        reference="Annex A.6.2.8",
        applies_from=PUBLISHED,
        sources=_src("A.6.2.8", "B.6.2.8"),
        summary=(
            "The organisation decides in which life cycle phases event logging is enabled — at "
            "minimum while the system is in use. No retention period is set; the guidance ties it "
            "to the intended use and the organisation's retention policies."
        ),
        evidence=[
            c.every_verdict_sealed(),
            c.witnessed(),
            c.chain_complete(),
            c.declared_prunes(),
        ],
    ),
    # -- A.7 Data for AI systems --------------------------------------------
    _O(
        key="a7-data-management",
        title="Data for AI systems (management, acquisition, quality, provenance, preparation)",
        reference="Annex A.7.2–A.7.6",
        applies_from=PUBLISHED,
        sources=_src("B.7.2", "A.7.3", "A.7.4", "A.7.5", "A.7.6"),
        summary=(
            "Data management processes for developing the system, details of how data is acquired "
            "and selected, data quality requirements, a process for recording provenance, and "
            "criteria for data preparation are defined and documented."
        ),
        evidence=[
            c.pointer("data_governance", "A data-governance record is on file."),
            _declared(
                "data_management_reference",
                "The data management process is declared.",
                "Training and reference data sit outside the gateway.",
            ),
        ],
        limit="Organisational control, evidenced by pointer and declaration only.",
    ),
    # -- A.8 Information for interested parties -----------------------------
    _O(
        key="a8-2-user-information",
        title="System documentation and information for users",
        reference="Annex A.8.2",
        applies_from=PUBLISHED,
        sources=_src("A.8.2", "B.8.2"),
        summary=(
            "The organisation determines and provides the information users of the system need; "
            "the guidance gives telling users they are dealing with an AI system as one example."
        ),
        evidence=[c.tech_docs(FRAMEWORK_ID), c.notice_text()],
    ),
    _O(
        key="a8-3-external-reporting",
        title="External reporting",
        reference="Annex A.8.3",
        applies_from=PUBLISHED,
        sources=_src("A.8.3"),
        summary="Interested parties have a way to report adverse impacts of the AI system.",
        evidence=[
            _declared(
                "adverse_impact_reporting_channel",
                "The channel through which interested parties report adverse impacts is declared.",
                "The channel is organisational; reports that never reach the register are not observable.",
            ),
            c.incident_register("Adverse impacts that are reported are recorded in the incident register."),
        ],
    ),
    _O(
        key="a8-4-incident-communication",
        title="Communication of incidents",
        reference="Annex A.8.4",
        applies_from=PUBLISHED,
        sources=_src("A.8.4"),
        summary="A plan for communicating incidents to users of the AI system is determined and documented.",
        evidence=[c.incident_register()],
    ),
    _O(
        key="a8-5-interested-parties-information",
        title="Information for interested parties",
        reference="Annex A.8.5",
        applies_from=PUBLISHED,
        sources=_src("A.8.5"),
        summary=(
            "The organisation determines and documents what it is obliged to report about the AI "
            "system to interested parties — the gateway's monthly Evidence Report is one such "
            "output, built on a witnessed log."
        ),
        evidence=[
            _declared(
                "interested_party_reporting_reference",
                "The organisation's reporting obligations to interested parties are declared.",
                "Which obligations apply is the organisation's determination; it is not evaluated.",
            ),
            c.witnessed(
                "The log the monthly Evidence Report is built from is countersigned by an independent witness."
            ),
        ],
        limit="The Evidence Reports themselves are listed on the Reports page.",
    ),
    # -- A.9 Use of AI systems ----------------------------------------------
    _O(
        key="a9-2-responsible-use",
        title="Processes for responsible use of AI systems",
        reference="Annex A.9.2",
        applies_from=PUBLISHED,
        sources=_src("B.9.2"),
        summary=(
            "Processes for using AI systems responsibly are defined and documented; the guidance "
            "names considerations such as required sign-offs, cost, sourcing and legal "
            "requirements."
        ),
        evidence=[
            _declared(
                "responsible_use_process_reference",
                "The responsible-use process is declared.",
                "The process is organisational; only its sign-off gate at the gateway is observable.",
            ),
            c.promotions_sealed(
                "Putting the system into use went through the lifecycle gate with a sealed record naming its approver.",
            ),
        ],
    ),
    _O(
        key="a9-3-a9-4-intended-use",
        title="Objectives for responsible use and intended use",
        reference="Annex A.9.3, A.9.4",
        applies_from=PUBLISHED,
        sources=_src("B.9.3", "B.9.3 (human oversight)", "A.9.4"),
        summary=(
            "Objectives for responsible use are identified and documented — the guidance includes "
            "deciding where meaningful human oversight belongs — and the system is used according "
            "to its intended uses and accompanying documentation."
        ),
        evidence=[
            c.out_of_grant_refused(),
            c.least_privilege_declared(),
            c.judged("per_instructions", "Sampled runs stay within the declared intended use."),
            c.override_available(),
            c.oversight_exercised(),
            c.not_rubber_stamp(),
        ],
    ),
    # -- A.10 Third-party and customer relationships ------------------------
    _O(
        key="a10-third-parties",
        title="Third-party and customer relationships",
        reference="Annex A.10.2–A.10.4",
        applies_from=PUBLISHED,
        sources=_src("A.10.2", "A.10.3", "A.10.4"),
        summary=(
            "Life cycle responsibilities are allocated between the organisation, its partners, "
            "suppliers, customers and third parties; supplier offerings are checked against the "
            "organisation's responsible approach; customer expectations are considered."
        ),
        evidence=[
            c.declared(
                "declared.operator_role",
                "core.operator_role",
                "The organisation's role for this system (builder, operator, vendor) is declared.",
                "Supplier management is organisational; contracts with suppliers are not evaluated.",
            ),
            # The same statement the EU AI Act cites for
            # Art 25(4): one evidence log, many audits.
            c.supplier_information_on_file(),
        ],
        limit="Organisational control, evidenced by declaration and supplier pointers.",
    ),
]

FRAMEWORK = Framework(
    id=FRAMEWORK_ID,
    title="ISO/IEC 42001:2023",
    jurisdiction="Global",
    reference="ISO/IEC 42001:2023 Artificial intelligence management system, Annex A",
    kind=KIND_STANDARD,
    obligations=OBLIGATIONS,
    versions=(CatalogVersion(CATALOG_VERSION, effective_from=PUBLISHED, note="First edition (2023)"),),
    deadline_rules={},
    retention_floors={},
    verified_against_instrument=True,
    instrument_status=INSTRUMENT_FINAL,
    verified_on=CHECKED,
    licensed_text=True,
    tag_key="iso_42001",
)

__all__ = ["FRAMEWORK", "FRAMEWORK_ID"]
