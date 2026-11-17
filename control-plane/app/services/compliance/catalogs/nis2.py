# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# NIS2 Directive (`regulation`)

Directive (EU) 2022/2555. A Directive: its obligations bind essential and
important entities through national transposition, which Member States
had to adopt by 17 October 2024 and apply from 18 October 2024 (Art 41(1)).
Scope is set by Art 2 (Annex I/II sectors, generally medium-sized and
above) and Art 3 (essential vs important entities). Significant incidents
are reported in stages (Art 23(4)): an early warning within 24 hours of
awareness, an incident notification within 72 hours, and a final report
not later than one month after the SUBMISSION of the incident
notification. `DEADLINE_STAGES` anchors each stage that way (the final
report on the recorded notification submission, in calendar months).
Deadlines are shown, never enforced; national transpositions may differ.

Checked on 2026-10-16 against the EUR-Lex text CELEX 32022L2555 (OJ L 333,
27.12.2022); its only consolidated version, 02022L2555-20221227, is the
original text — no amending act is in force on that date. Art 21(2)
logging/monitoring detail is cross-checked against Commission
Implementing Regulation (EU) 2024/2690 (CELEX 32024R2690), which applies
to the digital-infrastructure / digital-provider entities listed in its
Art 1 only. Every obligation carries a `Source` with a verbatim excerpt.
Not legal advice.
"""

from __future__ import annotations

from datetime import date, timedelta
from types import MappingProxyType

from app.services.compliance.catalogs import common as c
from app.services.compliance.catalogs.base import (
    ANCHOR_AWARE,
    KIND_REGULATION,
    CatalogVersion,
    DeadlineStage,
    Framework,
    INSTRUMENT_FINAL,
    Obligation,
    Source,
)

FRAMEWORK_ID = "nis2"
CATALOG_VERSION = "2026-09"

#: Member States apply the transposing measures from 18 October 2024 (Art 41(1)).
APPLIES = date(2024, 10, 18)

INSTRUMENT = "Directive (EU) 2022/2555 (NIS2), OJ L 333, 27.12.2022"
URL = "https://eur-lex.europa.eu/legal-content/EN/TXT/HTML/?uri=CELEX:32022L2555"
IR_INSTRUMENT = "Commission Implementing Regulation (EU) 2024/2690"
IR_URL = "https://eur-lex.europa.eu/legal-content/EN/TXT/HTML/?uri=CELEX:32024R2690"
CHECKED = date(2026, 10, 16)

#: Shared by every summary: a Directive binds through national law.
TRANSPOSED = "Binding through the national law transposing the Directive, which may differ."


def _src(provision: str, excerpt: str, *, url: str = URL, anchor: str = "", instrument: str = INSTRUMENT) -> Source:
    return Source(
        instrument=instrument,
        provision=provision,
        url=f"{url}#{anchor}" if anchor else url,
        checked_on=CHECKED,
        excerpt=excerpt,
    )


#: Art 23(4): early warning 24 h and incident notification 72 h from
#: becoming aware of the significant incident; final report one calendar
#: month after the SUBMISSION of the incident notification (Art 23(4)(d)).
#: Trust service providers notify within 24 h (Art 23(4), second
#: subparagraph) — not modelled; see the obligation's limit.
DEADLINE_STAGES = (
    DeadlineStage("early_warning", ANCHOR_AWARE, within=timedelta(hours=24)),
    DeadlineStage("notification", ANCHOR_AWARE, within=timedelta(hours=72)),
    DeadlineStage("final", "stage:notification", months=1),
)

#: The shortest period of each stage, kept for the registry seed and the
#: framework summary. NOT due times — those come from `DEADLINE_STAGES`.
DEADLINE_RULES = MappingProxyType({
    "early_warning": timedelta(hours=24),
    "notification": timedelta(hours=72),
    "final": timedelta(days=30),
})

_O = Obligation

OBLIGATIONS = [
    _O(
        key="art20-governance",
        title="Governance — management body approval, oversight and training",
        reference="Art 20(1), (2)",
        applies_from=APPLIES,
        summary=(
            "The management bodies of essential and important entities approve the Art 21 "
            "cybersecurity risk-management measures, oversee their implementation and can be held "
            "liable for infringements of Art 21; their members are required to follow training. "
            + TRANSPOSED
        ),
        evidence=[
            c.declared(
                "declared.management_body_approval",
                "frameworks.nis2.management_body_approval_reference",
                "The management body's approval of the risk-management measures, and the "
                "members' training, are declared with a reference to the record.",
                "A declaration; the approval and training happen outside the platform and are "
                "not observed.",
            )
        ],
        limit="Management-body decisions, oversight and training are not observable by the gateway.",
        sources=(
            _src(
                "Art. 20(1)",
                "the management bodies of essential and important entities approve the "
                "cybersecurity risk-management measures taken by those entities in order to comply "
                "with Article 21, oversee its implementation",
                anchor="art_20",
            ),
            _src(
                "Art. 20(2)",
                "Member States shall ensure that the members of the management bodies of essential "
                "and important entities are required to follow training",
                anchor="art_20",
            ),
        ),
    ),
    _O(
        key="art21-2-logging-monitoring",
        title="Cybersecurity risk-management measures",
        reference="Art 21(1), (2)",
        applies_from=APPLIES,
        summary=(
            "Essential and important entities take appropriate and proportionate technical, "
            "operational and organisational measures to manage risks to their network and "
            "information systems, including at least risk-analysis and security policies, "
            "incident handling, business continuity, supply-chain security, assessment of the "
            "effectiveness of the measures, cryptography, access control and multi-factor "
            "authentication (Art 21(2)(a)–(j)). Logging and monitoring is specified for the "
            "digital entities covered by Implementing Regulation (EU) 2024/2690. " + TRANSPOSED
        ),
        evidence=[c.every_verdict_sealed(), c.witnessed(), c.daily_health()],
        limit=(
            "The gateway shows the records of the AI traffic it carries; the entity's wider "
            "network and information systems, policies and continuity measures are not observed."
        ),
        sources=(
            _src(
                "Art. 21(1)",
                "Member States shall ensure that essential and important entities take appropriate "
                "and proportionate technical, operational and organisational measures to manage the "
                "risks posed to the security of network and information systems",
                anchor="art_21",
            ),
            _src(
                "Art. 21(2)(f)",
                "policies and procedures to assess the effectiveness of cybersecurity "
                "risk-management measures;",
                anchor="art_21",
            ),
            _src(
                "Annex, point 3.2.1",
                "The relevant entities shall lay down procedures and use tools to monitor and log "
                "activities on their network and information systems to detect events that could "
                "be considered as incidents and respond accordingly to mitigate the impact.",
                url=IR_URL,
                instrument=IR_INSTRUMENT,
            ),
        ),
    ),
    _O(
        key="art21-2-d-supply-chain",
        title="Supply-chain security",
        reference="Art 21(2)(d), 21(3)",
        applies_from=APPLIES,
        summary=(
            "The risk-management measures include supply-chain security, covering the security "
            "aspects of the relationships with direct suppliers and service providers (such as "
            "model and tool providers); in choosing them, entities take into account each "
            "supplier's specific vulnerabilities, the quality of its products and cybersecurity "
            "practices, and the coordinated EU risk assessments of critical supply chains "
            "(Art 22(1)). " + TRANSPOSED
        ),
        evidence=[c.supplier_information_on_file(), c.contributing_dependencies_assessed()],
        limit=(
            "Covers suppliers declared as dependencies of the AI system; the assessment of their "
            "cybersecurity practices is not evaluated, and suppliers outside the gateway are not seen."
        ),
        sources=(
            _src(
                "Art. 21(2)(d)",
                "supply chain security, including security-related aspects concerning the "
                "relationships between each entity and its direct suppliers or service providers;",
                anchor="art_21",
            ),
            _src(
                "Art. 21(3)",
                "entities take into account the vulnerabilities specific to each direct supplier and "
                "service provider and the overall quality of products and cybersecurity practices of "
                "their suppliers and service providers, including their secure development procedures.",
                anchor="art_21",
            ),
        ),
    ),
    _O(
        key="art23-incident-reporting",
        title="Reporting obligations",
        reference="Art 23(1), (4)",
        applies_from=APPLIES,
        summary=(
            "Essential and important entities notify significant incidents to the CSIRT or "
            "competent authority in stages: an early warning within 24 hours of becoming aware, "
            "an incident notification within 72 hours, an intermediate report on request, and a "
            "final report not later than one month after the incident notification (a progress "
            "report instead if the incident is ongoing). " + TRANSPOSED
        ),
        evidence=[
            c.incident_register(),
            c.incident_deadline(
                FRAMEWORK_ID,
                "Significant incidents' recorded report stages were each submitted within their "
                "Art 23(4) time limit, and no stage is overdue.",
                limit=(
                    "Early warning and incident notification are measured from awareness; the final "
                    "report from the recorded submission of the incident notification (one calendar "
                    "month). Submission times are operator-entered, not observed. Trust service "
                    "providers' 24-hour notification limit (Art 23(4), second subparagraph) is not "
                    "modelled. National law may set other deadlines. Shown, not enforced."
                ),
            ),
        ],
        sources=(
            _src(
                "Art. 23(1)",
                "Each Member State shall ensure that essential and important entities notify, "
                "without undue delay, its CSIRT or, where applicable, its competent authority in "
                "accordance with paragraph 4 of any incident that has a significant impact on the "
                "provision of their services",
                anchor="art_23",
            ),
            _src(
                "Art. 23(4)(a)",
                "without undue delay and in any event within 24 hours of becoming aware of the "
                "significant incident, an early warning",
                anchor="art_23",
            ),
            _src(
                "Art. 23(4)(b)",
                "without undue delay and in any event within 72 hours of becoming aware of the "
                "significant incident, an incident notification",
                anchor="art_23",
            ),
            _src(
                "Art. 23(4)(d)",
                "a final report not later than one month after the submission of the incident "
                "notification under point (b)",
                anchor="art_23",
            ),
        ),
    ),
    _O(
        key="art23-recipient-notification",
        title="Informing recipients of the service",
        reference="Art 23(1), (2)",
        applies_from=APPLIES,
        summary=(
            "Where appropriate, entities notify, without undue delay, the recipients of their "
            "services of significant incidents likely to adversely affect the provision of those "
            "services; where applicable, they communicate to recipients potentially affected by a "
            "significant cyber threat the measures or remedies they can take, and where "
            "appropriate the threat itself. " + TRANSPOSED
        ),
        evidence=[
            c.declared(
                "declared.recipient_notification",
                "frameworks.nis2.recipient_notification_procedure_reference",
                "The procedure for informing recipients of the service about significant incidents "
                "and cyber threats is declared.",
                "A declaration; communications to recipients are not observed.",
            )
        ],
        limit="Whether and when recipients were informed is not observed by the gateway.",
        sources=(
            _src(
                "Art. 23(1)",
                "Where appropriate, entities concerned shall notify, without undue delay, the "
                "recipients of their services of significant incidents that are likely to adversely "
                "affect the provision of those services.",
                anchor="art_23",
            ),
            _src(
                "Art. 23(2)",
                "communicate, without undue delay, to the recipients of their services that are "
                "potentially affected by a significant cyber threat any measures or remedies that "
                "those recipients are able to take in response to that threat.",
                anchor="art_23",
            ),
        ),
    ),
]

FRAMEWORK = Framework(
    id=FRAMEWORK_ID,
    title="NIS2",
    jurisdiction="EU",
    reference="Directive (EU) 2022/2555 (NIS2)",
    kind=KIND_REGULATION,
    obligations=OBLIGATIONS,
    versions=(CatalogVersion(CATALOG_VERSION, effective_from=APPLIES, note="As transposed from 18 October 2024"),),
    roles=frozenset({"essential_entity", "important_entity"}),
    role_field="entity_type",
    deadline_rules=DEADLINE_RULES,
    deadline_stages=DEADLINE_STAGES,
    retention_floors=MappingProxyType({}),
    verified_against_instrument=True,
    instrument_status=INSTRUMENT_FINAL,
    verified_on=CHECKED,
)

__all__ = ["DEADLINE_RULES", "DEADLINE_STAGES", "FRAMEWORK", "FRAMEWORK_ID"]
