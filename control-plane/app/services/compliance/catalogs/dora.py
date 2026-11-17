# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# DORA — Digital Operational Resilience Act (`regulation`)

Regulation (EU) 2022/2554, applicable from 17 January 2025 (Art 64). Its
duties in Chapters II–V fall on the financial entities of Art 2(1)(a)–(t)
(Art 2(2)); ICT third-party service providers (Art 2(1)(u)) are in scope
but are reached through the financial entity's contracts and, when
designated critical, the oversight framework of Art 31 ff. Every
obligation here is therefore scoped to the `financial_entity` role.

Checked on 2026-10-16 against the Official Journal text published by the
Publications Office (OJ L 333, 27.12.2022, p. 1), whose only EUR-Lex
consolidation is CELEX 02022R2554-20221227 — no amending act is in force on
that date (a 2023 Commission proposal, COM(2023) 360, proposes to amend it).
Incident-report time limits were checked against Commission Delegated
Regulation (EU) 2025/301 (RTS under Art 20(a)(ii), OJ L 2025/301,
20.2.2025, in force 12 March 2025); logging against Commission Delegated
Regulation (EU) 2024/1774, Art 12. Every obligation carries a `Source`
with a verbatim excerpt.

Major ICT-related incidents are reported in three stages (Art 19(4);
RTS 2025/301 Art 5(1)): an initial notification within 4 hours of
classifying the incident as major and no later than 24 hours after
becoming aware of it; an intermediate report within 72 hours of the
initial notification; a final report no later than one month after the
latest intermediate report. `DEADLINE_STAGES` anchors each stage as the
RTS does: the initial notification on the recorded classification time
(capped at 24 hours from awareness), the intermediate report on the
recorded submission of the initial notification, the final report (one
calendar month) on the recorded submission of the intermediate report.
Weekend and bank-holiday relief (Art 5(4)) is not modelled, and does not
apply to credit institutions for the initial and intermediate stages
(Art 5(5)). Deadlines are shown, never enforced. Not legal advice.
"""

from __future__ import annotations

from datetime import date, timedelta
from types import MappingProxyType

from app.services.compliance.catalogs import common as c
from app.services.compliance.catalogs.base import (
    ANCHOR_CLASSIFIED,
    INSTRUMENT_FINAL,
    KIND_REGULATION,
    CatalogVersion,
    DeadlineStage,
    Framework,
    Obligation,
    Source,
)

FRAMEWORK_ID = "dora"
CATALOG_VERSION = "2026-09"

#: Date of application (Art 64, second paragraph).
APPLIES = date(2025, 1, 17)

#: Duties in Chapters II–V are addressed to financial entities (Art 2(2)).
FINANCIAL_ENTITY = frozenset({"financial_entity"})

#: Staged reporting deadlines for a major ICT-related incident (Delegated
#: Regulation (EU) 2025/301, Art 5(1)), each on its own anchor: (a) initial
#: notification 4 h from classification as major, and no later than 24 h
#: from awareness (before classification is recorded, the 24 h bound is the
#: due time); (b) intermediate report 72 h from the SUBMISSION of the
#: initial notification; (c) final report one calendar month after the
#: submission of the intermediate report. Updated intermediate reports are
#: not modelled: the final report is counted from the recorded intermediate
#: submission, which is never later than the latest update — conservative.
DEADLINE_STAGES = (
    DeadlineStage("initial", ANCHOR_CLASSIFIED, within=timedelta(hours=4), cap_after_aware=timedelta(hours=24)),
    DeadlineStage("intermediate", "stage:initial", within=timedelta(hours=72)),
    DeadlineStage("final", "stage:intermediate", months=1),
)

#: The shortest period of each stage, kept for the registry seed and the
#: framework summary (`deadline_rules`). They are NOT due times — every due
#: time is computed from `DEADLINE_STAGES` and the incident's recorded
#: classification and submissions.
DEADLINE_RULES = MappingProxyType({
    "initial": timedelta(hours=4),
    "intermediate": timedelta(hours=72),
    "final": timedelta(days=30),
})

CHECKED = date(2026, 10, 16)

DORA = "Regulation (EU) 2022/2554 (DORA), OJ L 333, 27.12.2022 (consolidation 02022R2554-20221227)"
DORA_URL = "https://eur-lex.europa.eu/legal-content/EN/TXT/HTML/?uri=CELEX:32022R2554"
RTS_INCIDENT = (
    "Commission Delegated Regulation (EU) 2025/301 (RTS on content and time limits for major "
    "ICT-related incident reports)"
)
RTS_INCIDENT_URL = "https://eur-lex.europa.eu/legal-content/EN/TXT/HTML/?uri=CELEX:32025R0301"
RTS_RISK = "Commission Delegated Regulation (EU) 2024/1774 (RTS on ICT risk management tools)"
RTS_RISK_URL = "https://eur-lex.europa.eu/legal-content/EN/TXT/HTML/?uri=CELEX:32024R1774"


def _src(provision: str, article: int, excerpt: str, *, instrument: str = DORA, url: str = DORA_URL) -> Source:
    return Source(
        instrument=instrument,
        provision=provision,
        url=f"{url}#art_{article}",
        checked_on=CHECKED,
        excerpt=excerpt,
    )


_O = Obligation

OBLIGATIONS = [
    _O(
        # Key kept for API stability; the reference was wrong (Art 9(4) does
        # not mention logging). Monitoring is Art 9(1); logging is RTS Art 12.
        key="art9-4-logging-monitoring",
        title="ICT security — monitoring and logging",
        reference="Art 9(1); Delegated Regulation (EU) 2024/1774 Art 12",
        applies_from=APPLIES,
        roles=FINANCIAL_ENTITY,
        summary=(
            "The financial entity continuously monitors and controls the security and functioning "
            "of ICT systems and tools (Art 9(1)). The RTS on ICT risk management tools requires "
            "documented logging procedures, protocols and tools, including protection of logs "
            "against tampering, deletion and unauthorised access (Delegated Regulation (EU) "
            "2024/1774, Art 12)."
        ),
        sources=(
            _src(
                "Art. 9(1)",
                9,
                "financial entities shall continuously monitor and control the security and "
                "functioning of ICT systems and tools",
            ),
            _src(
                "Art. 12(1)",
                12,
                "Financial entities shall, as part of the safeguards against intrusions and data "
                "misuse, develop, document, and implement logging procedures, protocols and tools.",
                instrument=RTS_RISK,
                url=RTS_RISK_URL,
            ),
            _src(
                "Art. 12(2)(d)",
                12,
                "measures to protect logging systems and log information against tampering, "
                "deletion, and unauthorised access at rest, in transit, and, where relevant, in use;",
                instrument=RTS_RISK,
                url=RTS_RISK_URL,
            ),
        ),
        evidence=[c.every_verdict_sealed(), c.witnessed(), c.daily_health()],
        limit=(
            "Covers the traffic and decisions routed through the gateway. Logging of the "
            "entity's other ICT systems, clock synchronisation (RTS Art 12(2)(f)) and log "
            "retention periods it sets are outside what the gateway records. Entities under the "
            "simplified framework (Art 16) follow Title III of the RTS instead."
        ),
    ),
    _O(
        key="art9-4-access-change",
        title="ICT security — access and change management",
        reference="Art 9(4)(c), Art 9(4)(e)",
        applies_from=APPLIES,
        roles=FINANCIAL_ENTITY,
        summary=(
            "Physical or logical access to information and ICT assets is limited to what legitimate "
            "functions require (Art 9(4)(c)); changes to ICT systems follow documented change "
            "management so that all changes are recorded, tested, assessed and implemented in a "
            "controlled manner (Art 9(4)(e))."
        ),
        sources=(
            _src(
                "Art. 9(4)(c)",
                9,
                "implement policies that limit the physical or logical access to information assets "
                "and ICT assets to what is required for legitimate and approved functions and "
                "activities only",
            ),
            _src(
                "Art. 9(4)(e)",
                9,
                "in order to ensure that all changes to ICT systems are recorded, tested, assessed, "
                "approved, implemented and verified in a controlled manner;",
            ),
        ),
        evidence=[
            c.least_privilege_declared(),
            c.out_of_grant_refused(),
            c.promotions_sealed(),
            c.no_unapproved_widening(),
        ],
        limit=(
            "Covers access to, and configuration changes of, the AI system as held by the gateway; "
            "the entity's wider access and change-management policies are organisational."
        ),
    ),
    _O(
        key="art10-detection",
        title="Detection of anomalous activities",
        reference="Art 10(1)–(3)",
        applies_from=APPLIES,
        roles=FINANCIAL_ENTITY,
        summary=(
            "Mechanisms promptly detect anomalous activities, including ICT-related incidents, with "
            "multiple layers of control, alert thresholds and criteria that trigger incident "
            "response; resources are devoted to monitoring user activity and ICT anomalies. "
            "Guardrail and policy verdicts at the gateway are sealed records."
        ),
        sources=(
            _src(
                "Art. 10(1)",
                10,
                "Financial entities shall have in place mechanisms to promptly detect anomalous "
                "activities, in accordance with Article 17, including ICT network performance issues "
                "and ICT-related incidents",
            ),
            _src(
                "Art. 10(2)",
                10,
                "define alert thresholds and criteria to trigger and initiate ICT-related incident "
                "response processes",
            ),
        ),
        evidence=[c.out_of_grant_refused(), c.integrity_checked()],
        limit=(
            "Detection covers what passes through the gateway. Regular testing of detection "
            "mechanisms (Art 10(1), second subparagraph; Art 25) is not evidenced here."
        ),
    ),
    _O(
        key="art11-response-recovery",
        title="ICT response and recovery",
        reference="Art 11(2)(b)–(c), Art 11(8)",
        applies_from=APPLIES,
        roles=FINANCIAL_ENTITY,
        summary=(
            "The ICT business continuity policy is implemented through arrangements that respond to "
            "and resolve ICT-related incidents so as to limit damage, and activate containment "
            "measures without delay; records of activities before and during disruption events "
            "are kept readily accessible."
        ),
        sources=(
            _src(
                "Art. 11(2)(c)",
                11,
                "activate, without delay, dedicated plans that enable containment measures, processes "
                "and technologies suited to each type of ICT-related incident and prevent further "
                "damage",
            ),
            _src(
                "Art. 11(8)",
                11,
                "Financial entities shall keep readily accessible records of activities before and "
                "during disruption events when their ICT business continuity plans and ICT response "
                "and recovery plans are activated.",
            ),
        ),
        evidence=[c.suspend_available(), c.every_verdict_sealed()],
        limit=(
            "Shows the containment controls the gateway offers for this AI system (suspend, run "
            "abort) and its sealed activity records. The business continuity policy, business "
            "impact analysis and yearly testing of plans (Art 11(1), (5), (6)) are organisational "
            "and not evaluated."
        ),
    ),
    _O(
        key="art12-backup-restoration",
        title="Backup, restoration and recovery",
        reference="Art 12(1)–(2)",
        applies_from=APPLIES,
        roles=FINANCIAL_ENTITY,
        summary=(
            "Backup policies and procedures (scope and minimum frequency) and restoration and "
            "recovery procedures are developed and documented, and backup and restoration "
            "procedures are tested periodically."
        ),
        sources=(
            _src(
                "Art. 12(1)(a)",
                12,
                "backup policies and procedures specifying the scope of the data that is subject to "
                "the backup and the minimum frequency of the backup",
            ),
            _src(
                "Art. 12(2)",
                12,
                "Testing of the backup procedures and restoration and recovery procedures and methods "
                "shall be undertaken periodically.",
            ),
        ),
        evidence=[
            c.declared(
                "declared.ict_backup_policy",
                "frameworks.dora.backup_policy_reference",
                "The backup and restoration procedures covering this AI system are declared.",
                "Backups are taken outside the gateway; their existence, frequency and restore "
                "tests are not observed.",
            )
        ],
        limit="Organisational duty, evidenced by declaration only.",
    ),
    _O(
        key="art17-19-incident-reporting",
        title="ICT-related incident management and reporting",
        reference="Art 17, Art 19(1), Art 19(4); Delegated Regulation (EU) 2025/301 Art 5",
        applies_from=APPLIES,
        roles=FINANCIAL_ENTITY,
        summary=(
            "An ICT-related incident management process records all ICT-related incidents and "
            "significant cyber threats (Art 17). Major ICT-related incidents are reported to the "
            "competent authority in three stages (Art 19(4)): an initial notification within 4 "
            "hours of classification as major and no later than 24 hours after awareness; an "
            "intermediate report within 72 hours of the initial notification; a final report no "
            "later than one month after the latest intermediate report (RTS 2025/301, Art 5(1))."
        ),
        sources=(
            _src(
                "Art. 17(2)",
                17,
                "Financial entities shall record all ICT-related incidents and significant cyber "
                "threats.",
            ),
            _src(
                "Art. 19(1)",
                19,
                "Financial entities shall report major ICT-related incidents to the relevant "
                "competent authority as referred to in Article 46 in accordance with paragraph 4 of "
                "this Article.",
            ),
            _src(
                "Art. 5(1)(a)",
                5,
                "within four hours from the classification of the ICT-related incident as a major "
                "ICT-related incident and no later than 24 hours from the moment the financial "
                "entity has become aware of the ICT-related incident;",
                instrument=RTS_INCIDENT,
                url=RTS_INCIDENT_URL,
            ),
            _src(
                "Art. 5(1)(b)",
                5,
                "for the intermediate report: at the latest within 72 hours from the submission of "
                "the initial notification",
                instrument=RTS_INCIDENT,
                url=RTS_INCIDENT_URL,
            ),
            _src(
                "Art. 5(1)(c)",
                5,
                "for the final report: no later than one month after either the submission of the "
                "intermediate report, or, where applicable, after the latest updated intermediate "
                "report.",
                instrument=RTS_INCIDENT,
                url=RTS_INCIDENT_URL,
            ),
        ),
        evidence=[
            c.incident_register(),
            c.incident_deadline(
                FRAMEWORK_ID,
                "Major incidents' recorded report stages were each submitted within their RTS time "
                "limit, and no stage is overdue.",
                limit=(
                    "Each stage is measured from its own anchor as recorded on the incident: the "
                    "initial notification from the classification time the operator enters (and no "
                    "later than 24 hours from awareness; until classification is recorded only the "
                    "24-hour bound is shown), the intermediate and final reports from the recorded "
                    "submission of the previous stage. Classification and submission times are "
                    "operator-entered, not observed. Shown, not enforced."
                ),
            ),
        ],
        limit=(
            "The RTS time limits apply from 12 March 2025. Weekend and bank-holiday relief "
            "(RTS Art 5(4)) is not modelled and does not apply to credit institutions for the "
            "initial and intermediate stages (Art 5(5)); for other entities a deadline falling on "
            "a weekend or bank holiday may be later than shown. Updated intermediate reports are "
            "not modelled: the final report is counted from the recorded intermediate submission. Classification against the criteria of "
            "Art 18 and Delegated Regulation (EU) 2024/1772, and informing clients whose "
            "financial interests are affected (Art 19(3)), are outside what the gateway records."
        ),
    ),
    _O(
        key="art28-third-party-risk",
        title="ICT third-party risk and register of information",
        reference="Art 28(1), Art 28(3)",
        applies_from=APPLIES,
        roles=FINANCIAL_ENTITY,
        summary=(
            "ICT third-party risk is managed as an integral component of ICT risk within the ICT "
            "risk management framework (Art 28(1)), and a register of information on all "
            "contractual arrangements for ICT services from ICT third-party service providers is "
            "maintained and updated (Art 28(3)). Model and tool vendors behind an AI system are "
            "such providers."
        ),
        sources=(
            _src(
                "Art. 28(1)",
                28,
                "Financial entities shall manage ICT third-party risk as an integral component of ICT "
                "risk within their ICT risk management framework",
            ),
            _src(
                "Art. 28(3)",
                28,
                "financial entities shall maintain and update at entity level, and at sub-consolidated "
                "and consolidated levels, a register of information in relation to all contractual "
                "arrangements on the use of ICT services provided by ICT third-party service providers.",
            ),
        ),
        evidence=[
            c.declared(
                "declared.ict_third_party_register",
                "frameworks.dora.ict_third_party_register_reference",
                "Vendor-operated systems and the register of ICT arrangements are declared.",
                "The register of information is organisational; its content is not evaluated.",
            )
        ],
        limit="Organisational duty, evidenced by declaration only.",
    ),
    _O(
        key="art30-key-contractual-provisions",
        title="Key contractual provisions with ICT providers",
        reference="Art 30(1)–(3)",
        applies_from=APPLIES,
        roles=FINANCIAL_ENTITY,
        summary=(
            "Rights and obligations with each ICT third-party service provider are set out in "
            "writing in one document including the service level agreements, with the minimum "
            "elements of Art 30(2) and, for ICT services supporting critical or important "
            "functions, those of Art 30(3)."
        ),
        sources=(
            _src(
                "Art. 30(1)",
                30,
                "The rights and obligations of the financial entity and of the ICT third-party "
                "service provider shall be clearly allocated and set out in writing.",
            ),
        ),
        evidence=[c.supplier_information_on_file()],
        limit=(
            "Shows that each externally supplied dependency has its agreement on file, pinned by "
            "hash; whether the agreement contains the Art 30(2)/(3) elements is not evaluated."
        ),
    ),
]

FRAMEWORK = Framework(
    id=FRAMEWORK_ID,
    title="DORA",
    jurisdiction="EU",
    reference="Regulation (EU) 2022/2554 (Digital Operational Resilience Act)",
    kind=KIND_REGULATION,
    obligations=OBLIGATIONS,
    versions=(CatalogVersion(CATALOG_VERSION, effective_from=APPLIES, note="As applicable from 17 January 2025"),),
    roles=frozenset({"financial_entity", "ict_third_party_provider"}),
    role_field="financial_entity_type",
    deadline_rules=DEADLINE_RULES,
    deadline_stages=DEADLINE_STAGES,
    retention_floors=MappingProxyType({}),
    verified_against_instrument=True,
    instrument_status=INSTRUMENT_FINAL,
    verified_on=CHECKED,
)

__all__ = ["DEADLINE_RULES", "DEADLINE_STAGES", "FRAMEWORK", "FRAMEWORK_ID"]
