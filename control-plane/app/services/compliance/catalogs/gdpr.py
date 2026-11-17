# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# GDPR (`regulation`)

Regulation (EU) 2016/679 — the provisions an AI gateway's records bear on.

Checked on 2026-10-16 against the EUR-Lex consolidated text
CELEX 02016R0679-20160504 (version 000.002, incorporating Corrigendum
OJ L 127, 23.5.2018, p. 2). That is the only consolidated version
published: no amending act is in force on that date (the Omnibus IV /
Digital Omnibus changes to Art 30(5) and elsewhere are proposals). Every
obligation carries a `Source` with a verbatim excerpt of the provision.

The classic conflict (Art 17 erasure against an immutable log) is resolved
by the design rather than argued: identities and bodies never enter a
record, and the erasure plan touches no evidence table. The Art 30
Records of Processing export keeps its existing shape and now cites
records. Not legal advice.
"""

from __future__ import annotations

from datetime import date, timedelta
from types import MappingProxyType

from app.services.compliance.catalogs import common as c
from app.services.compliance.catalogs.base import (
    KIND_REGULATION,
    CatalogVersion,
    Framework,
    INSTRUMENT_FINAL,
    Obligation,
    Source,
)

FRAMEWORK_ID = "gdpr"
CATALOG_VERSION = "2026-09"

#: Date of application (Art 99(2): "It shall apply from 25 May 2018.").
APPLIES = date(2018, 5, 25)

ROLES = frozenset({"controller", "processor", "joint_controller"})

#: Obligations the text addresses to the controller only (a joint controller
#: is a controller, Art 26). A processor's own duties are noted per entry.
CONTROLLER = frozenset({"controller", "joint_controller"})

INSTRUMENT = "Regulation (EU) 2016/679 (GDPR), consolidated text 02016R0679-20160504 incl. Corrigendum OJ L 127, 23.5.2018"
URL = "https://eur-lex.europa.eu/legal-content/EN/TXT/HTML/?uri=CELEX:02016R0679-20160504"
CHECKED = date(2026, 10, 16)


def _src(provision: str, anchor: str, excerpt: str) -> Source:
    return Source(
        instrument=INSTRUMENT,
        provision=provision,
        url=f"{URL}#{anchor}",
        checked_on=CHECKED,
        excerpt=excerpt,
    )


_O = Obligation

OBLIGATIONS = [
    _O(
        key="art5-2-accountability",
        title="Accountability",
        reference="Art 5(2)",
        applies_from=APPLIES,
        summary=(
            "The controller is responsible for, and able to demonstrate, compliance with the "
            "processing principles of Art 5(1)."
        ),
        roles=CONTROLLER,
        evidence=[c.witnessed(), c.tech_docs(FRAMEWORK_ID)],
        sources=(
            _src(
                "Art. 5(2)",
                "art_5",
                "The controller shall be responsible for, and be able to demonstrate compliance "
                "with, paragraph 1 (‘accountability’).",
            ),
        ),
    ),
    _O(
        key="art6-1-lawful-basis",
        title="Lawfulness of processing",
        reference="Art 6(1)",
        applies_from=APPLIES,
        summary=(
            "Processing is lawful only if and to the extent that at least one of the bases in "
            "Art 6(1)(a)–(f) applies (consent, contract, legal obligation, vital interests, public "
            "task, legitimate interests). The controller records the basis it relies on."
        ),
        roles=CONTROLLER,
        evidence=[
            c.declared(
                "declared.lawful_basis",
                "frameworks.gdpr.lawful_basis",
                "The lawful basis relied on for the system's processing is declared.",
                "The basis is the operator's declaration; whether it holds is not evaluated.",
            )
        ],
        limit="Whether a lawful basis exists is a legal determination the gateway cannot observe.",
        sources=(
            _src(
                "Art. 6(1)",
                "art_6",
                "Processing shall be lawful only if and to the extent that at least one of the "
                "following applies:",
            ),
        ),
    ),
    _O(
        key="art13-14-15-adm-information",
        title="Information about automated decision-making",
        reference="Art 13(2)(f), 14(2)(g), 15(1)(h)",
        applies_from=APPLIES,
        summary=(
            "Where automated decision-making, including profiling, referred to in Art 22(1) and "
            "(4) takes place, the controller informs the data subject of its existence and gives "
            "meaningful information about the logic involved and its significance and envisaged "
            "consequences — when data are obtained (Art 13), within the Art 14(3) periods when "
            "they are not obtained from the data subject, and on an access request (Art 15)."
        ),
        roles=CONTROLLER,
        flags=frozenset({"automated_decisions_about_persons"}),
        evidence=[
            c.notice_text(
                "The information given to data subjects about automated decision-making and the "
                "surface it is shown on are declared and sealed."
            ),
        ],
        limit=(
            "Information given outside the gateway (privacy notices, letters, access-request "
            "replies) is not observed; whether the explanation of the logic is meaningful is "
            "not evaluated."
        ),
        sources=(
            _src(
                "Art. 13(2)(f)",
                "art_13",
                "the existence of automated decision-making, including profiling, referred to in "
                "Article 22(1) and (4) and, at least in those cases, meaningful information about "
                "the logic involved",
            ),
            _src(
                "Art. 14(2)(g)",
                "art_14",
                "the existence of automated decision-making, including profiling, referred to in "
                "Article 22(1) and (4) and, at least in those cases, meaningful information about "
                "the logic involved",
            ),
            _src(
                "Art. 15(1)(h)",
                "art_15",
                "the existence of automated decision-making, including profiling, referred to in "
                "Article 22(1) and (4) and, at least in those cases, meaningful information about "
                "the logic involved",
            ),
        ),
    ),
    _O(
        key="art17-erasure",
        title="Right to erasure against an immutable log",
        reference="Art 17",
        applies_from=APPLIES,
        summary=(
            "The controller erases personal data without undue delay where a ground in Art 17(1) "
            "applies, subject to the exceptions in Art 17(3). Records carry no identities and no "
            "bodies, so erasure never needs to reach the evidence log."
        ),
        roles=CONTROLLER,
        evidence=[
            c.declared(
                "declared.erasure_procedure",
                "frameworks.gdpr.erasure_procedure_reference",
                "The erasure procedure is declared; it touches no evidence table.",
                "Erasure of data held outside the platform is not observed.",
            )
        ],
        sources=(
            _src(
                "Art. 17(1)",
                "art_17",
                "The data subject shall have the right to obtain from the controller the erasure of "
                "personal data concerning him or her without undue delay and the controller shall "
                "have the obligation to erase personal data without undue delay where one of the "
                "following grounds applies:",
            ),
        ),
    ),
    _O(
        key="art22-3-human-intervention",
        title="Human intervention in automated decisions",
        reference="Art 22(3)",
        applies_from=APPLIES,
        summary=(
            "Where a decision based solely on automated processing, including profiling, with "
            "legal or similarly significant effects is made because it is necessary for a "
            "contract or based on explicit consent (Art 22(2)(a), (c)), the controller implements "
            "suitable safeguards — at least the right to obtain human intervention, to express "
            "a point of view and to contest the decision. Decisions authorised by law (Art "
            "22(2)(b)) carry the safeguards that law lays down."
        ),
        roles=CONTROLLER,
        flags=frozenset({"automated_decisions_about_persons"}),
        evidence=[c.override_available(), c.oversight_exercised()],
        sources=(
            _src(
                "Art. 22(1)",
                "art_22",
                "The data subject shall have the right not to be subject to a decision based solely "
                "on automated processing, including profiling, which produces legal effects "
                "concerning him or her or similarly significantly affects him or her.",
            ),
            _src(
                "Art. 22(3)",
                "art_22",
                "In the cases referred to in points (a) and (c) of paragraph 2, the data controller "
                "shall implement suitable measures to safeguard the data subject's rights and "
                "freedoms and legitimate interests, at least the right to obtain human intervention "
                "on the part of the controller, to express his or her point of view and to contest "
                "the decision.",
            ),
        ),
    ),
    _O(
        key="art22-4-special-categories",
        title="Special categories of data in automated decisions",
        reference="Art 22(4)",
        applies_from=APPLIES,
        summary=(
            "Automated decisions permitted under Art 22(2) are not based on special categories "
            "of personal data (Art 9(1)) unless Art 9(2)(a) explicit consent or (g) substantial "
            "public interest applies and suitable safeguards are in place."
        ),
        roles=CONTROLLER,
        flags=frozenset({"automated_decisions_about_persons"}),
        evidence=[
            c.declared(
                "declared.adm_special_categories",
                "frameworks.gdpr.adm_special_categories_reference",
                "Whether special-category data enter the automated decision, and on which "
                "Art 9(2) ground and safeguards, is declared.",
                "A declaration; the content of inputs is not classified against Art 9(1).",
            )
        ],
        limit="The gateway does not classify request content as special-category data.",
        sources=(
            _src(
                "Art. 22(4)",
                "art_22",
                "Decisions referred to in paragraph 2 shall not be based on special categories of "
                "personal data referred to in Article 9(1), unless point (a) or (g) of Article 9(2) "
                "applies and suitable measures to safeguard the data subject's rights and freedoms "
                "and legitimate interests are in place.",
            ),
        ),
    ),
    _O(
        key="art25-by-design",
        title="Data protection by design and by default",
        reference="Art 25(1), (2)",
        applies_from=APPLIES,
        summary=(
            "The controller implements appropriate technical and organisational measures designed "
            "to implement the data-protection principles, and ensures that by default only "
            "personal data necessary for each specific purpose are processed. On the platform: "
            "the never-enters allow-list and digest-only defaults."
        ),
        roles=CONTROLLER,
        evidence=[
            c.declared(
                "declared.by_design",
                "frameworks.gdpr.dpbd_reference",
                "The data-protection-by-design measures are declared.",
                "The allow-list is a published design property; other measures are declared, not evaluated.",
            )
        ],
        sources=(
            _src(
                "Art. 25(1)",
                "art_25",
                "implement appropriate technical and organisational measures, such as "
                "pseudonymisation, which are designed to implement data-protection principles, such "
                "as data minimisation, in an effective manner",
            ),
            _src(
                "Art. 25(2)",
                "art_25",
                "The controller shall implement appropriate technical and organisational measures "
                "for ensuring that, by default, only personal data which are necessary for each "
                "specific purpose of the processing are processed.",
            ),
        ),
    ),
    _O(
        key="art28-1-processor-guarantees",
        title="Processors providing sufficient guarantees",
        reference="Art 28(1)",
        applies_from=APPLIES,
        summary=(
            "Where processing is carried out on its behalf (for example by a model or tool "
            "provider), the controller uses only processors providing sufficient guarantees to "
            "implement appropriate technical and organisational measures."
        ),
        roles=CONTROLLER,
        evidence=[
            c.supplier_information_on_file(
                "Every dependency supplied by another legal entity has the supplier's written "
                "information (such as a data processing agreement) on file."
            )
        ],
        limit=(
            "Whether the guarantees are sufficient, and the Art 28(3) contract terms, are not "
            "evaluated; processors reached outside the gateway are not seen."
        ),
        sources=(
            _src(
                "Art. 28(1)",
                "art_28",
                "Where processing is to be carried out on behalf of a controller, the controller "
                "shall use only processors providing sufficient guarantees to implement appropriate "
                "technical and organisational measures",
            ),
        ),
    ),
    _O(
        key="art30-records-of-processing",
        title="Records of processing activities",
        reference="Art 30(1), (2)",
        applies_from=APPLIES,
        summary=(
            "Controllers (Art 30(1)) and processors (Art 30(2)) maintain a written record of "
            "processing activities, made available to the supervisory authority on request; "
            "Art 30(5) exempts organisations employing fewer than 250 persons unless its "
            "conditions apply (the existing Art 30 export, now citing records)."
        ),
        evidence=[
            c.tagged_actions_recorded(
                "gdpr_art_30",
                "Every processing call the tagging engine marked Art 30 produced a sealed record.",
            )
        ],
        sources=(
            _src(
                "Art. 30(1)",
                "art_30",
                "Each controller and, where applicable, the controller's representative, shall "
                "maintain a record of processing activities under its responsibility.",
            ),
            _src(
                "Art. 30(2)",
                "art_30",
                "Each processor and, where applicable, the processor's representative shall maintain "
                "a record of all categories of processing activities carried out on behalf of a "
                "controller",
            ),
        ),
    ),
    _O(
        key="art32-integrity-testing",
        title="Security of processing — integrity and testing",
        reference="Art 32(1)(b), (d)",
        applies_from=APPLIES,
        summary=(
            "The controller and the processor implement measures appropriate to the risk, "
            "including as appropriate the ability to ensure the ongoing integrity of processing "
            "systems and a process for regularly testing the effectiveness of measures."
        ),
        evidence=[
            c.integrity_checked(),
            c.promotions_sealed(
                "Changes went through the lifecycle gate (replay suite before promotion).",
                limit="Shows the gate was used; the sufficiency of the tests is not evaluated.",
            ),
        ],
        sources=(
            _src(
                "Art. 32(1)(b)",
                "art_32",
                "the ability to ensure the ongoing confidentiality, integrity, availability and "
                "resilience of processing systems and services;",
            ),
            _src(
                "Art. 32(1)(d)",
                "art_32",
                "a process for regularly testing, assessing and evaluating the effectiveness of "
                "technical and organisational measures for ensuring the security of the processing.",
            ),
        ),
    ),
    _O(
        key="art33-breach-notification",
        title="Notification of a personal data breach",
        reference="Art 33(1), (5)",
        applies_from=APPLIES,
        summary=(
            "The controller notifies the competent supervisory authority without undue delay and, "
            "where feasible, within 72 hours of becoming aware of a breach, unless the breach is "
            "unlikely to result in a risk to the rights and freedoms of natural persons; a later "
            "notification gives reasons for the delay, and every breach is documented (Art 33(5)). "
            "A processor notifies the controller without undue delay (Art 33(2))."
        ),
        roles=CONTROLLER,
        evidence=[
            c.incident_register(),
            c.incident_deadline(FRAMEWORK_ID, "Breaches were notified within 72 hours of awareness."),
        ],
        sources=(
            _src(
                "Art. 33(1)",
                "art_33",
                "the controller shall without undue delay and, where feasible, not later than 72 "
                "hours after having become aware of it, notify the personal data breach to the "
                "supervisory authority competent in accordance with Article 55, unless the personal "
                "data breach is unlikely to result in a risk to the rights and freedoms of natural "
                "persons.",
            ),
            _src(
                "Art. 33(5)",
                "art_33",
                "The controller shall document any personal data breaches, comprising the facts "
                "relating to the personal data breach, its effects and the remedial action taken.",
            ),
        ),
    ),
    _O(
        key="art34-1-breach-communication",
        title="Communication of a personal data breach to the data subject",
        reference="Art 34(1), (2)",
        applies_from=APPLIES,
        summary=(
            "When a breach is likely to result in a high risk to the rights and freedoms of natural "
            "persons, the controller communicates it to the data subjects without undue delay, in "
            "clear and plain language; Art 34(3) sets the cases where this is not required. No "
            "fixed number of hours is set."
        ),
        roles=CONTROLLER,
        evidence=[
            c.incident_register(
                "A breach register is kept, recording the high-risk determination for each breach."
            )
        ],
        limit="Whether and when data subjects were told is not observed by the gateway.",
        sources=(
            _src(
                "Art. 34(1)",
                "art_34",
                "When the personal data breach is likely to result in a high risk to the rights and "
                "freedoms of natural persons, the controller shall communicate the personal data "
                "breach to the data subject without undue delay.",
            ),
        ),
    ),
    _O(
        key="art35-dpia",
        title="Data protection impact assessment",
        reference="Art 35(1), (3)",
        applies_from=APPLIES,
        summary=(
            "Prior to processing likely to result in a high risk to the rights and freedoms of "
            "natural persons, the controller carries out a DPIA — required in particular for a "
            "systematic and extensive evaluation based on automated processing, including "
            "profiling, on which decisions with legal or similarly significant effects are based "
            "(Art 35(3)(a))."
        ),
        roles=CONTROLLER,
        evidence=[c.pointer("dpia", "A current DPIA is on file.")],
        needs_review=True,
        review_note="Whether a DPIA is required depends on the processing — record the determination.",
        sources=(
            _src(
                "Art. 35(1)",
                "art_35",
                "is likely to result in a high risk to the rights and freedoms of natural persons, "
                "the controller shall, prior to the processing, carry out an assessment of the "
                "impact of the envisaged processing operations on the protection of personal data.",
            ),
            _src(
                "Art. 35(3)(a)",
                "art_35",
                "a systematic and extensive evaluation of personal aspects relating to natural "
                "persons which is based on automated processing, including profiling, and on which "
                "decisions are based that produce legal effects concerning the natural person or "
                "similarly significantly affect the natural person;",
            ),
        ),
    ),
    _O(
        key="art44-third-country-transfers",
        title="Transfers to third countries",
        reference="Art 44",
        applies_from=APPLIES,
        summary=(
            "A transfer of personal data to a third country or an international organisation "
            "(for example to a model provider outside the EEA) takes place only if the controller "
            "and processor meet the conditions of Chapter V (adequacy decision, appropriate "
            "safeguards or a derogation), including for onward transfers."
        ),
        evidence=[
            c.declared(
                "declared.transfer_mechanism",
                "frameworks.gdpr.transfer_mechanism_reference",
                "The third-country transfers and the Chapter V mechanism relied on for each are "
                "declared.",
                "A declaration; the mechanism's validity is not evaluated.",
            )
        ],
        limit=(
            "The gateway shows which provider endpoint a call was routed to, not where the "
            "provider processes or stores the data, nor the legal mechanism."
        ),
        sources=(
            _src(
                "Art. 44",
                "art_44",
                "shall take place only if, subject to the other provisions of this Regulation, the "
                "conditions laid down in this Chapter are complied with by the controller and "
                "processor, including for onward transfers",
            ),
        ),
    ),
]

FRAMEWORK = Framework(
    id=FRAMEWORK_ID,
    title="GDPR",
    jurisdiction="EU",
    reference="Regulation (EU) 2016/679",
    kind=KIND_REGULATION,
    obligations=OBLIGATIONS,
    versions=(CatalogVersion(CATALOG_VERSION, effective_from=APPLIES, note="As applicable from 25 May 2018"),),
    roles=ROLES,
    role_field="role",
    deadline_rules=MappingProxyType({"standard": timedelta(hours=72)}),
    retention_floors=MappingProxyType({}),
    verified_against_instrument=True,
    instrument_status=INSTRUMENT_FINAL,
    verified_on=CHECKED,
    tag_key="gdpr_art_30",
)

__all__ = ["FRAMEWORK", "FRAMEWORK_ID"]
