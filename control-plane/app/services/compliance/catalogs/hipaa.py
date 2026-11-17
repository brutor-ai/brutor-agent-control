# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# HIPAA Security Rule (`regulation`)

45 CFR Part 164 Subpart C (Security Rule), plus the Breach Notification
Rule's 60-day deadline (Subpart D, §164.404 to individuals and §164.410
business associate to covered entity). PHI never enters a record
— the first thing a HIPAA reviewer probes — so the audit-control and
integrity statements are about records that carry digests, never content.

Verified 2026-10-16 against the eCFR text of 45 CFR Part 164 as up to date
on 2026-10-14 (www.ecfr.gov; Subparts C and D unchanged since the eCFR
point-in-time baseline). The Security Rule NPRM (90 FR 898, 6 Jan 2025)
has NOT been finalized; nothing here is taken from it. Dates not in the CFR
text were checked in the Federal Register on govinfo: the Breach
Notification IFR (74 FR 42740) is effective 23 Sep 2009; the Omnibus Rule
(78 FR 5566) made business associates directly subject to the Security
Rule with a compliance date of 23 Sep 2013 — `applies_from` keeps the
covered-entity date (§164.318) for both roles.

Retention: §164.316(b)(2)(i) asks for six years (2190 days) of the
*documentation* the rule requires — policies, reports, declarations — not
of every log line. The floor is therefore scoped to documentation
artefacts (`scope: "documentation"`) and stated as such; it is
deliberately NOT a framework-wide `retention_floors` entry, which would
impose six years on every record. Not legal advice.
"""

from __future__ import annotations

from datetime import date, timedelta
from types import MappingProxyType

from app.services.compliance.catalogs import common as c
from app.services.compliance.catalogs.base import (
    INSTRUMENT_FINAL,
    KIND_REGULATION,
    CatalogVersion,
    Framework,
    Obligation,
    Source,
)

FRAMEWORK_ID = "hipaa"
CATALOG_VERSION = "2026-09"

#: Security Rule compliance date, §164.318 (health plans other than small
#: health plans, clearinghouses, providers; small health plans 20 Apr 2006).
SECURITY_RULE = date(2005, 4, 20)
#: Breach Notification Interim Final Rule effective date.
BREACH_RULE = date(2009, 9, 23)

#: §164.316(b)(2)(i) — six years.
DOCUMENTATION_RETENTION_DAYS = 2190

#: The text the catalog was checked against.
INSTRUMENT = "45 CFR Part 164 (eCFR, up to date as of 2026-10-14)"
CHECKED_ON = date(2026, 10, 16)
_ECFR = "https://www.ecfr.gov/current/title-45/section-"


def _src(section: str, para: str, excerpt: str) -> Source:
    provision = f"45 CFR §{section}{para}"
    return Source(INSTRUMENT, provision, f"{_ECFR}{section}#p-{section}{para}", CHECKED_ON, excerpt)


ROLES = frozenset({"covered_entity", "business_associate"})

_O = Obligation

OBLIGATIONS = [
    _O(
        key="164-308-a1-risk-analysis",
        title="Risk analysis",
        reference="45 CFR §164.308(a)(1)(ii)(A)",
        applies_from=SECURITY_RULE,
        summary=(
            "An accurate and thorough assessment is conducted of the potential risks and "
            "vulnerabilities to the confidentiality, integrity and availability of the ePHI held "
            "(a required implementation specification)."
        ),
        evidence=[
            c.declared(
                "declared.risk_analysis",
                "frameworks.hipaa.risk_analysis_reference",
                "The risk analysis covering this AI system is declared.",
                "The risk analysis is an organisational document; its accuracy and thoroughness "
                "are not evaluated.",
            )
        ],
        limit="The gateway cannot show that a risk analysis was performed; it records the declaration.",
        sources=(
            _src(
                "164.308",
                "(a)(1)(ii)(A)",
                "Risk analysis (Required). Conduct an accurate and thorough assessment of the potential "
                "risks and vulnerabilities to the confidentiality, integrity, and availability of "
                "electronic protected health information held by the covered entity or business associate.",
            ),
        ),
    ),
    _O(
        key="164-308-a1-risk-management",
        title="Risk management",
        reference="45 CFR §164.308(a)(1)(ii)(B)",
        applies_from=SECURITY_RULE,
        summary=(
            "Security measures sufficient to reduce risks and vulnerabilities to a reasonable and "
            "appropriate level under §164.306(a) are implemented (a required implementation "
            "specification)."
        ),
        evidence=[
            c.least_privilege_declared(),
            c.out_of_grant_refused(),
            c.declared(
                "declared.risk_management",
                "frameworks.hipaa.risk_management_reference",
                "The risk management plan and the measures it selects are declared.",
                "Whether the measures reduce risk to a reasonable and appropriate level is the "
                "entity's judgement, not evaluated.",
            ),
        ],
        limit="The gateway shows the access measures it enforces; measures outside it are declared only.",
        sources=(
            _src(
                "164.308",
                "(a)(1)(ii)(B)",
                "Risk management (Required). Implement security measures sufficient to reduce risks and "
                "vulnerabilities to a reasonable and appropriate level to comply with § 164.306(a).",
            ),
        ),
    ),
    _O(
        key="164-308-a1-activity-review",
        title="Information system activity review",
        reference="45 CFR §164.308(a)(1)(ii)(D)",
        applies_from=SECURITY_RULE,
        summary=(
            "Procedures are implemented to regularly review records of information system "
            "activity, such as audit logs, access reports and security incident tracking reports."
        ),
        sources=(
            _src(
                "164.308",
                "(a)(1)(ii)(D)",
                "Information system activity review (Required). Implement procedures to regularly "
                "review records of information system activity, such as audit logs, access reports, "
                "and security incident tracking reports.",
            ),
        ),
        evidence=[
            c.tagged_actions_recorded(
                "hipaa.phi",
                "Every action on a resource tagged as holding PHI produced a sealed record.",
            ),
            c.daily_health(),
        ],
    ),
    _O(
        key="164-308-a5-login-monitoring",
        title="Log-in monitoring (addressable)",
        reference="45 CFR §164.308(a)(5)(ii)(C)",
        applies_from=SECURITY_RULE,
        summary=(
            "Procedures for monitoring log-in attempts and reporting discrepancies, as part of the "
            "security awareness and training programme. Addressable (§164.306(d)(3)): implemented "
            "if reasonable and appropriate; otherwise the entity documents why and implements an "
            "equivalent alternative measure if reasonable and appropriate."
        ),
        evidence=[
            c.declared(
                "declared.login_monitoring",
                "frameworks.hipaa.login_monitoring_reference",
                "The log-in monitoring procedure, or the documented decision not to implement it, is declared.",
                "Log-in attempts to systems outside the gateway are not observed.",
            )
        ],
        limit=(
            "The gateway does not evaluate log-in attempts or discrepancy reports; the procedure "
            "is declared."
        ),
        sources=(
            _src(
                "164.308",
                "(a)(5)(ii)(C)",
                "Log-in monitoring (Addressable). Procedures for monitoring log-in attempts and "
                "reporting discrepancies.",
            ),
            _src(
                "164.306",
                "(d)(3)(ii)(B)(1)",
                "Document why it would not be reasonable and appropriate to implement the "
                "implementation specification; and",
            ),
        ),
    ),
    _O(
        key="164-308-a6-incident-procedures",
        title="Security incident procedures",
        reference="45 CFR §164.308(a)(6); Breach Notification Rule §164.404(b), §164.410(b)",
        applies_from=SECURITY_RULE,
        summary=(
            "Suspected or known security incidents are identified and responded to, harmful "
            "effects mitigated as practicable, and incidents and outcomes documented. A breach of "
            "unsecured PHI is notified without unreasonable delay and in no case later than 60 "
            "calendar days after discovery — by a covered entity to affected individuals "
            "(§164.404), by a business associate to the covered entity (§164.410)."
        ),
        sources=(
            _src(
                "164.308",
                "(a)(6)(ii)",
                "Identify and respond to suspected or known security incidents; mitigate, to the "
                "extent practicable, harmful effects of security incidents that are known to the "
                "covered entity or business associate; and document security incidents and their outcomes.",
            ),
            _src(
                "164.404",
                "(b)",
                "a covered entity shall provide the notification required by paragraph (a) of this "
                "section without unreasonable delay and in no case later than 60 calendar days after "
                "discovery of a breach.",
            ),
            _src(
                "164.410",
                "(b)",
                "a business associate shall provide the notification required by paragraph (a) of "
                "this section without unreasonable delay and in no case later than 60 calendar days "
                "after discovery of a breach.",
            ),
        ),
        evidence=[
            c.incident_register(),
            c.incident_deadline(
                FRAMEWORK_ID,
                "Breaches were notified within 60 days of discovery.",
            ),
        ],
        limit=(
            f"The 60-day breach deadline applies from {BREACH_RULE.isoformat()} (Breach Notification "
            "Rule). Notice to HHS (§164.408), to the media (§164.406) and a law-enforcement delay "
            "(§164.412) are outside what the gateway records."
        ),
    ),
    _O(
        key="164-308-a8-evaluation",
        title="Evaluation",
        reference="45 CFR §164.308(a)(8)",
        applies_from=SECURITY_RULE,
        summary=(
            "A periodic technical and nontechnical evaluation establishes the extent to which "
            "security policies and procedures meet the Security Rule, initially against the "
            "standards and subsequently in response to environmental or operational changes "
            "affecting the security of ePHI."
        ),
        evidence=[
            c.daily_health(),
            c.declared(
                "declared.security_evaluation",
                "frameworks.hipaa.security_evaluation_reference",
                "The most recent periodic security evaluation is declared.",
                "The evaluation is performed outside the platform; its scope and findings are not evaluated.",
            ),
        ],
        limit=(
            "The daily health check is a technical signal over the gateway's own records; the "
            "nontechnical evaluation is declared only."
        ),
        sources=(
            _src(
                "164.308",
                "(a)(8)",
                "Perform a periodic technical and nontechnical evaluation, based initially upon the "
                "standards implemented under this rule and, subsequently, in response to environmental "
                "or operational changes affecting the security of electronic protected health information",
            ),
        ),
    ),
    _O(
        key="164-308-b-business-associate-contracts",
        title="Business associate contracts",
        reference="45 CFR §164.308(b); §164.314(a)",
        applies_from=SECURITY_RULE,
        summary=(
            "A covered entity lets a business associate (and a business associate lets a "
            "subcontractor) create, receive, maintain or transmit ePHI on its behalf only with "
            "satisfactory assurances, documented in a written contract or other arrangement that "
            "meets §164.314(a)."
        ),
        evidence=[
            c.supplier_information_on_file(
                "Every dependency supplied by another legal entity has its written agreement on file."
            ),
            c.declared(
                "declared.business_associate_agreements",
                "frameworks.hipaa.business_associate_agreements_reference",
                "The business associate agreements covering this AI system are declared.",
                "Agreements are organisational documents; whether their terms meet §164.314(a) is "
                "not evaluated.",
            ),
        ],
        limit=(
            "The gateway can show that an agreement is on file for a declared dependency, not that "
            "the vendor handling ePHI is a business associate or that the contract terms suffice."
        ),
        sources=(
            _src(
                "164.308",
                "(b)(1)",
                "A covered entity may permit a business associate to create, receive, maintain, or "
                "transmit electronic protected health information on the covered entity's behalf only "
                "if the covered entity obtains satisfactory assurances, in accordance with § 164.314(a)",
            ),
            _src(
                "164.308",
                "(b)(3)",
                "Document the satisfactory assurances required by paragraph (b)(1) or (b)(2) of this "
                "section through a written contract or other arrangement with the business associate",
            ),
            _src(
                "164.314",
                "(a)(1)",
                "The contract or other arrangement required by § 164.308(b)(3) must meet the "
                "requirements of paragraph (a)(2)(i), (a)(2)(ii), or (a)(2)(iii) of this section, as applicable.",
            ),
        ),
    ),
    _O(
        key="164-312-a1-access-control",
        title="Access control",
        reference="45 CFR §164.312(a)(1)",
        applies_from=SECURITY_RULE,
        summary=(
            "Technical policies and procedures allow access to systems holding ePHI only to persons "
            "or software programs granted access rights under §164.308(a)(4)."
        ),
        sources=(
            _src(
                "164.312",
                "(a)(1)",
                "to allow access only to those persons or software programs that have been granted "
                "access rights as specified in § 164.308(a)(4).",
            ),
        ),
        evidence=[c.least_privilege_declared(), c.out_of_grant_refused()],
    ),
    _O(
        key="164-312-a2-unique-user-id",
        title="Unique user identification",
        reference="45 CFR §164.312(a)(2)(i)",
        applies_from=SECURITY_RULE,
        summary=(
            "A unique name and/or number is assigned for identifying and tracking user identity "
            "(a required implementation specification)."
        ),
        evidence=[
            c.human_identity(
                "Approvals were decided by individually authenticated principals, each recorded "
                "under its own identity."
            )
        ],
        limit=(
            "Covers identities the gateway authenticates (console accounts, API keys, agent "
            "identities); user accounts in the systems holding ePHI are outside it."
        ),
        sources=(
            _src(
                "164.312",
                "(a)(2)(i)",
                "Unique user identification (Required). Assign a unique name and/or number for "
                "identifying and tracking user identity.",
            ),
        ),
    ),
    _O(
        key="164-312-b-audit-controls",
        title="Audit controls",
        reference="45 CFR §164.312(b)",
        applies_from=SECURITY_RULE,
        summary=(
            "Hardware, software and/or procedural mechanisms record and examine activity in "
            "systems that contain or use ePHI."
        ),
        sources=(
            _src(
                "164.312",
                "(b)",
                "Implement hardware, software, and/or procedural mechanisms that record and examine "
                "activity in information systems that contain or use electronic protected health information.",
            ),
        ),
        evidence=[c.every_verdict_sealed(), c.witnessed()],
    ),
    _O(
        key="164-312-c1-integrity",
        title="Integrity",
        reference="45 CFR §164.312(c)(1)",
        applies_from=SECURITY_RULE,
        summary="Policies and procedures protect ePHI from improper alteration or destruction.",
        sources=(
            _src(
                "164.312",
                "(c)(1)",
                "Implement policies and procedures to protect electronic protected health information "
                "from improper alteration or destruction.",
            ),
        ),
        evidence=[c.integrity_checked(), c.chain_complete()],
        limit="Covers the gateway's records of access to ePHI; the ePHI stores themselves are outside it.",
    ),
    _O(
        key="164-312-d-authentication",
        title="Person or entity authentication",
        reference="45 CFR §164.312(d)",
        applies_from=SECURITY_RULE,
        summary="Procedures verify that a person or entity seeking access to ePHI is the one claimed.",
        sources=(
            _src(
                "164.312",
                "(d)",
                "Implement procedures to verify that a person or entity seeking access to electronic "
                "protected health information is the one claimed.",
            ),
        ),
        evidence=[c.human_identity()],
    ),
    _O(
        key="164-312-e1-transmission-security",
        title="Transmission security",
        reference="45 CFR §164.312(e)(1)",
        applies_from=SECURITY_RULE,
        summary=(
            "Technical security measures guard against unauthorized access to ePHI being transmitted "
            "over an electronic communications network. Integrity controls and encryption "
            "(§164.312(e)(2)) are addressable."
        ),
        evidence=[
            c.declared(
                "declared.transmission_security",
                "frameworks.hipaa.transmission_security_reference",
                "The transmission security measures (e.g. TLS on every hop carrying ePHI) are declared.",
                "Transport encryption is a deployment property; the gateway does not record it per request.",
            )
        ],
        limit="The gateway does not evidence the transport security of each hop; it is declared.",
        sources=(
            _src(
                "164.312",
                "(e)(1)",
                "Implement technical security measures to guard against unauthorized access to "
                "electronic protected health information that is being transmitted over an electronic "
                "communications network.",
            ),
        ),
    ),
    _O(
        key="164-316-b2-documentation-retention",
        title="Documentation retention (six years)",
        reference="45 CFR §164.316(b)(2)(i)",
        applies_from=SECURITY_RULE,
        summary=(
            "Documentation required by §164.316(b)(1) is retained for six years from the date of "
            "its creation or the date when it last was in effect, whichever is later."
        ),
        sources=(
            _src(
                "164.316",
                "(b)(2)(i)",
                "Retain the documentation required by paragraph (b)(1) of this section for 6 years "
                "from the date of its creation or the date when it last was in effect, whichever is later.",
            ),
        ),
        evidence=[
            c.retention_floor(
                DOCUMENTATION_RETENTION_DAYS,
                "Documentation artefacts (reports, declarations, documentation exports) are kept for six years.",
                scope="documentation",
                limit=(
                    "Applies to documentation artefacts only, not to every log record; policies "
                    "kept outside the platform are not covered."
                ),
            )
        ],
    ),
]

FRAMEWORK = Framework(
    id=FRAMEWORK_ID,
    title="HIPAA Security Rule",
    jurisdiction="US",
    reference="45 CFR Part 164 Subpart C; Breach Notification Rule §§164.404, 164.410",
    kind=KIND_REGULATION,
    obligations=OBLIGATIONS,
    versions=(CatalogVersion(CATALOG_VERSION, effective_from=SECURITY_RULE, note="Security Rule as in force"),),
    roles=ROLES,
    role_field="role",
    deadline_rules=MappingProxyType({"standard": timedelta(days=60)}),
    # Deliberately empty — the six-year floor is documentation-scoped (see module doc).
    retention_floors=MappingProxyType({}),
    verified_against_instrument=True,
    instrument_status=INSTRUMENT_FINAL,
    verified_on=CHECKED_ON,
    tag_key="hipaa",
)

__all__ = ["DOCUMENTATION_RETENTION_DAYS", "FRAMEWORK", "FRAMEWORK_ID"]
