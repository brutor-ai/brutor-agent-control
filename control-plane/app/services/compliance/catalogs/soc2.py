# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# SOC 2 — AICPA Trust Services Criteria (`attestation`)

SOC 2 is an attestation: a service auditor examines a service
organisation's controls against the Trust Services Criteria (TSC) and
issues an opinion. The criteria are what those controls should meet; they
are not law, and nothing here is that opinion. Each entry is a criterion
the gateway's records can serve as evidence toward — what an auditor gets
is a witnessed activity log with an independent timestamp, not
screenshots. The common criteria (CC) apply to every SOC 2 report; the
availability (A), confidentiality (C) and processing integrity (PI)
criteria apply only when that category is in the report's scope.

Retention floor = the attestation period. The TSC set no retention period;
the floor is what a Type 2 examination period needs from the records
(anchored to PI1.5's point of focus on archived, protected system records).
The default is 365 days (`ATTESTATION_PERIOD_DAYS`); a customer on a
shorter or longer period declares it on the profile
(`frameworks.soc2.attestation_period_days`) and the statement's
`floor_days` param is the default the Evidence Report states. SOC 2 sets no
incident reporting deadline: the customer's own SLA applies, so
`deadline_rules` is empty and the deadline statement says so.

Checked against the published criteria on 2026-10-16. Edition: "2017 Trust
Services Criteria for Security, Availability, Processing Integrity,
Confidentiality, and Privacy (With Revised Points of Focus — 2022)" (TSP
section 100), AICPA Assurance Services Executive Committee, from a licensed
copy of the AICPA PDF
(https://www.aicpa-cima.com/resources/download/2017-trust-services-criteria-with-revised-points-of-focus-2022).
The 2022 revisions change points of focus only; the criteria are those of
the 2017 TSC.

The text is AICPA copyright, so no criterion is quoted here. Every source
carries a fingerprint instead: the SHA-256 of the criterion statement
(whitespace-normalised, `base.normalise_passage`), its first six words and
its length, over `pdftotext -enc UTF-8` reading-order output. Anyone with
the same edition can re-check every fingerprint:

    .venv-test/bin/python scripts/verify_licensed_sources.py soc2 /path/to/Trust-services-criteria.pdf

`applies_from` (15 Dec 2018) is the AICPA's transition date for the 2017
TSC (reports for periods ending on or after that date). The 2022 edition
does not restate an effective date, so that date is not covered by a
fingerprint.

Not legal or audit advice.
"""

from __future__ import annotations

from datetime import date
from types import MappingProxyType

from app.services.compliance.catalogs import common as c
from app.services.compliance.catalogs.base import (
    ANY_CLASSIFICATION,
    INSTRUMENT_FINAL,
    KIND_ATTESTATION,
    CatalogVersion,
    Framework,
    Obligation,
    Source,
)

FRAMEWORK_ID = "soc2"
CATALOG_VERSION = "2026-09"

#: Default attestation period (days) — the SOC 2 Type II retention floor.
ATTESTATION_PERIOD_DAYS = 365

#: The 2017 Trust Services Criteria are required for reports covering
#: periods ending on or after 15 December 2018 (revised points of focus 2022).
TSC_2017_REQUIRED = date(2018, 12, 15)

_INSTRUMENT = (
    "AICPA 2017 Trust Services Criteria for Security, Availability, Processing Integrity, "
    "Confidentiality, and Privacy (With Revised Points of Focus — 2022), TSP section 100"
)
_URL = "https://www.aicpa-cima.com/resources/download/2017-trust-services-criteria-with-revised-points-of-focus-2022"
_CHECKED = date(2026, 10, 16)


def _tsc(provision: str, sha256: str, starts_with: str, length: int) -> Source:
    """A fingerprinted TSC passage (licensed text: never quoted)."""
    return Source(
        instrument=_INSTRUMENT,
        provision=provision,
        url=_URL,
        checked_on=_CHECKED,
        excerpt="",
        excerpt_sha256=sha256,
        excerpt_starts_with=starts_with,
        excerpt_length=length,
    )


# Criterion statements (fingerprints over the 2022 edition).
_CC4_1 = _tsc("CC4.1", "1c891955df5b98194306f0311c833c9ed8f9fb0e709b948597d57ad3e594ee3a",
              "COSO Principle 16: The entity selects,", 182)
_CC4_2 = _tsc("CC4.2", "edc939dfd87e7f06ad6819be6e0def58b2b5c08b748733cfa7fd3402319421ad",
              "COSO Principle 17: The entity evaluates", 236)
_CC6_1 = _tsc("CC6.1", "f27633c1e2de90c856288550dc78118caedc9734396e10a90cdf5061718189ed",
              "The entity implements logical access security", 193)
_CC6_6 = _tsc("CC6.6", "4d70ec17a7a771e0037884dbd3993a26977fa96bbd0cc1b4ebf481353ad8e311",
              "The entity implements logical access security", 125)
_CC6_7 = _tsc("CC6.7", "284c5c01576ccb4b03b033ef25bc9ba8ac18ce5bdd1cdf0d2494bcf82f1ce2be",
              "The entity restricts the transmission, movement,", 223)
_CC6_8 = _tsc("CC6.8", "b11a8d547f4ca0f3feb2c3b66b3d83b54378a35c42e51420d104b8ef8dfd2758",
              "The entity implements controls to prevent", 152)
_CC7_2 = _tsc("CC7.2", "c66fa5bd6fc52ad93e6b11dffecbc13fb7d5c773bdbda666d63ddfd5cd7adb4c",
              "The entity monitors system components and", 289)
_CC7_3 = _tsc("CC7.3", "81a543f341215e163ce9cd81b6cb9e8f735f6baae3e6c383fe0714075ad415bd",
              "The entity evaluates security events to", 219)
_CC7_4 = _tsc("CC7.4", "c5c1a252c2a8c4b9e7a2a130a39c2afad6d608475ef8ce4f793466a9b7511dfe",
              "The entity responds to identified security", 188)
_CC8_1 = _tsc("CC8.1", "599d3c8159e62bf5c314ddccd18097970b5c9a2d2079b0b49d2bb328cc14fbba",
              "The entity authorizes, designs, develops or", 190)
_CC9_2 = _tsc("CC9.2", "a9e2d5ec107fba8d5a51200760d2161b3fab3e1cf3df0302fd7d9b959fe35ca4",
              "The entity assesses and manages risks", 84)
_A1_2 = _tsc("A1.2", "a4ed1385a7587c8e052571719035cf31b3748efe6b3916176d9871792e776431",
             "The entity authorizes, designs, develops or", 221)
_C1_1 = _tsc("C1.1", "705c7ef2122374a5a1ee7c38a4eea54e9912e51fca8958edfd7fcaf7aecd6b07",
             "The entity identifies and maintains confidential", 120)
_C1_2 = _tsc("C1.2", "4aa545d8ca35c6bbc30bb5c2850f329c88418dda15923dcabad2a4c4d89daa11",
             "The entity disposes of confidential information", 107)
_PI1_4 = _tsc("PI1.4", "93c1de8a20885f6d82bbc05e1375ce4d41991cf97ff3a208d6ca8d82c5d54e33",
              "The entity implements policies and procedures", 183)
_PI1_5 = _tsc("PI1.5", "7220cc7b4b7d27b1278d3622151a045e3f6e2fab132a6a66e5b14219576348da",
              "The entity implements policies and procedures", 204)
# PI1.5 point of focus "Archives and Protects System Records".
_PI1_5_ARCHIVES = _tsc(
    "PI1.5, point of focus: Archives and Protects System Records",
    "cf256aea504e945d002ad71668ae6c243c80a6d3f7b3a49c77b8b279c9355599",
    "Archives and Protects System Records —",
    192,
)

_O = Obligation

OBLIGATIONS = [
    _O(
        key="cc4-monitoring-activities",
        title="Monitoring activities",
        reference="CC4.1, CC4.2",
        applies_from=TSC_2017_REQUIRED,
        summary=(
            "The entity runs ongoing and/or separate evaluations to find out whether its internal "
            "control components are present and functioning, and reports control deficiencies "
            "promptly to whoever must correct them, including senior management and the board "
            "where appropriate."
        ),
        evidence=[c.daily_health(), c.witnessed()],
        sources=(_CC4_1, _CC4_2),
    ),
    _O(
        key="cc6-1-logical-access",
        title="Logical access security",
        reference="CC6.1",
        applies_from=TSC_2017_REQUIRED,
        summary=(
            "The entity puts logical access security software, infrastructure and architecture "
            "in place over protected information assets so that security events do not "
            "compromise them."
        ),
        evidence=[c.least_privilege_declared(), c.human_identity()],
        sources=(_CC6_1,),
    ),
    _O(
        key="cc6-6-cc6-7-boundaries",
        title="System boundaries and transmission",
        reference="CC6.6, CC6.7",
        applies_from=TSC_2017_REQUIRED,
        summary=(
            "The entity applies logical access measures against threats from outside its system "
            "boundaries (CC6.6), and limits the transmission, movement and removal of information "
            "to authorised internal and external users and processes, protecting it while it "
            "moves (CC6.7)."
        ),
        evidence=[
            c.out_of_grant_refused(),
            c.tagged_actions_recorded(
                "soc2.cc6.7",
                "Every data-movement refusal (residency block) produced a sealed record.",
            ),
        ],
        limit=(
            "The gateway shows the policy denials and residency blocks on traffic routed through "
            "it; network perimeter controls and encryption in transit outside the gateway are not "
            "visible here."
        ),
        sources=(_CC6_6, _CC6_7),
    ),
    _O(
        key="cc6-8-unauthorised-change",
        title="Unauthorised or malicious software",
        reference="CC6.8",
        applies_from=TSC_2017_REQUIRED,
        summary=(
            "The entity has controls that prevent, or detect and act on, the introduction of "
            "unauthorised or malicious software."
        ),
        evidence=[c.out_of_grant_refused(), c.no_unapproved_widening()],
        limit=(
            "The gateway does not scan for malware. It shows only that tools and models outside "
            "the AI system's grant could not be reached and that the grant was not widened "
            "outside the lifecycle gate."
        ),
        sources=(_CC6_8,),
    ),
    _O(
        key="cc7-2-anomaly-monitoring",
        title="Monitoring for anomalies",
        reference="CC7.2",
        applies_from=TSC_2017_REQUIRED,
        summary=(
            "Watch the system's components for unusual behaviour that could come from an attack, "
            "a disaster or a mistake and would affect what the organisation has committed to, then "
            "work out which of those signals are security events."
        ),
        evidence=[
            c.tagged_actions_recorded(
                "soc2.cc7.2",
                "Every guardrail block the tagging engine marked CC7.2 produced a sealed record.",
            ),
            c.every_verdict_sealed(),
            c.integrity_checked(),
        ],
        sources=(_CC7_2,),
    ),
    _O(
        key="cc7-3-cc7-4-incidents",
        title="Security events and incident response",
        reference="CC7.3, CC7.4",
        applies_from=TSC_2017_REQUIRED,
        summary=(
            "Security events are assessed to see which are real incidents, with action taken to "
            "head them off or deal with them (CC7.3); confirmed incidents are handled under a set "
            "response plan that covers understanding, containing, fixing and telling the right "
            "people about them (CC7.4)."
        ),
        evidence=[
            c.incident_register(),
            c.suspend_available(),
            c.incident_deadline(
                FRAMEWORK_ID,
                "Incidents were reported within the customer-defined SLA.",
                limit=(
                    "SOC 2 sets no reporting deadline; without a declared SLA this statement "
                    "cannot be evaluated. Deadlines are shown, not enforced."
                ),
            ),
        ],
        sources=(_CC7_3, _CC7_4),
    ),
    _O(
        key="cc8-1-change-management",
        title="Change management",
        reference="CC8.1",
        applies_from=TSC_2017_REQUIRED,
        summary=(
            "Changes to infrastructure, data, software and procedures are authorised, designed or "
            "acquired, configured, documented, tested, signed off and implemented by the entity."
        ),
        evidence=[c.promotions_sealed(), c.no_unapproved_widening()],
        sources=(_CC8_1,),
    ),
    _O(
        key="cc9-2-vendor-risk",
        title="Vendor and business partner risk",
        reference="CC9.2",
        applies_from=TSC_2017_REQUIRED,
        summary=(
            "The entity assesses and manages the risks that come from its vendors and business "
            "partners — for an AI system, among others the model providers and external agents "
            "or tools it depends on."
        ),
        evidence=[c.supplier_information_on_file(), c.contributing_dependencies_assessed()],
        limit=(
            "The gateway lists the dependencies it routes to and the supplier information on file; "
            "the entity's vendor risk assessment itself is not evaluated."
        ),
        sources=(_CC9_2,),
    ),
    _O(
        key="a1-2-recovery-infrastructure",
        title="Backup and recovery infrastructure (availability)",
        reference="A1.2",
        applies_from=TSC_2017_REQUIRED,
        summary=(
            "Where availability is in the report's scope: the entity puts in place, operates, "
            "maintains and monitors environmental protections, software, data backup processes "
            "and recovery infrastructure in support of its objectives."
        ),
        evidence=[
            c.declared(
                "declared.recovery",
                "frameworks.soc2.recovery_reference",
                "Backup and recovery arrangements for the AI system are declared.",
                "Declared by the operator; backups and recovery infrastructure are outside the "
                "gateway and are not observed.",
            )
        ],
        limit="The gateway cannot show backups, environmental protections or recovery infrastructure.",
        sources=(_A1_2,),
    ),
    _O(
        key="c1-1-confidentiality",
        title="Confidential information",
        reference="C1.1",
        applies_from=TSC_2017_REQUIRED,
        summary=(
            "Where confidentiality is in the report's scope: the entity identifies the "
            "confidential information it holds and maintains it in line with its "
            "confidentiality objectives."
        ),
        evidence=[
            c.declared(
                "declared.confidentiality",
                "frameworks.soc2.confidentiality_reference",
                "The confidentiality commitments and the never-enters allow-list are declared.",
                "The gateway's records carry digests and an admitted-field allow-list, never bodies "
                "or identities — a published design property; the customer's own commitments are "
                "declared, not evaluated.",
            )
        ],
        sources=(_C1_1,),
    ),
    _O(
        key="c1-2-confidential-disposal",
        title="Disposal of confidential information",
        reference="C1.2",
        applies_from=TSC_2017_REQUIRED,
        summary=(
            "Where confidentiality is in the report's scope: confidential information is "
            "destroyed or removed when the organisation's confidentiality commitments call for it."
        ),
        evidence=[c.declared_prunes()],
        limit=(
            "Covers the gateway's own logs only: each removal is a declared prune. Disposal of "
            "confidential information held elsewhere is not visible here."
        ),
        sources=(_C1_2,),
    ),
    _O(
        key="pi1-4-pi1-5-processing-integrity",
        title="Processing integrity of outputs and stored items",
        reference="PI1.4, PI1.5",
        applies_from=TSC_2017_REQUIRED,
        summary=(
            "Where processing integrity is in the report's scope: the entity has policies and "
            "procedures so that output is delivered or made available completely, accurately and "
            "on time against its specifications (PI1.4), and so that inputs, items in processing "
            "and outputs are stored completely, accurately and on time against the system's "
            "specifications (PI1.5)."
        ),
        evidence=[c.integrity_checked(), c.oversight_exercised()],
        limit=(
            "The accuracy of a model's output is not recomputable; whether the gateway's record "
            "of it was altered afterwards is."
        ),
        sources=(_PI1_4, _PI1_5),
    ),
    _O(
        key="pi1-5-system-records-archived",
        aliases=frozenset({"attestation-period-retention"}),
        title="System records archived and protected for the examination period",
        reference="PI1.5 (point of focus: archives and protects system records)",
        applies_from=TSC_2017_REQUIRED,
        summary=(
            "Where processing integrity is in the report's scope, PI1.5's points of focus call for "
            "system records to be archived and the archives kept safe from loss, corruption or "
            "destruction that would make them unusable. The TSC set no retention period; a Type 2 "
            "examination samples records across its whole period, so the floor here is that period "
            "(365 days by default)."
        ),
        evidence=[
            c.retention_floor(
                ATTESTATION_PERIOD_DAYS,
                "Logs are kept for at least the attestation period (365 days by default).",
                limit=(
                    "The period is the customer's examination period, not a TSC requirement; the "
                    "default is 365 days and a different period is declared on the profile. Record "
                    "identifiers, statements and tree heads are permanent."
                ),
            ),
            c.declared_prunes(),
            c.integrity_checked(),
        ],
        sources=(_PI1_5, _PI1_5_ARCHIVES),
    ),
]

FRAMEWORK = Framework(
    id=FRAMEWORK_ID,
    title="SOC 2 (AICPA Trust Services Criteria)",
    jurisdiction="US",
    reference="AICPA Trust Services Criteria 2017 (revised points of focus 2022)",
    kind=KIND_ATTESTATION,
    obligations=OBLIGATIONS,
    versions=(
        CatalogVersion(CATALOG_VERSION, effective_from=TSC_2017_REQUIRED, note="TSC 2017, points of focus 2022"),
    ),
    deadline_rules=MappingProxyType({}),
    retention_floors=MappingProxyType({ANY_CLASSIFICATION: ATTESTATION_PERIOD_DAYS}),
    # Checked against a licensed copy of the AICPA text; sources are
    # fingerprints (see module doc), never quotations.
    licensed_text=True,
    instrument_status=INSTRUMENT_FINAL,
    verified_against_instrument=True,
    verified_on=date(2026, 10, 16),
    tag_key="soc2",
)

__all__ = ["ATTESTATION_PERIOD_DAYS", "FRAMEWORK", "FRAMEWORK_ID"]
