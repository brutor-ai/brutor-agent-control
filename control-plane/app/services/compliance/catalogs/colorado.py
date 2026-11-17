# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Colorado — Automated Decision-Making Technology in Consequential Decisions
# (C.R.S. 6-1-1701 et seq.; `regulation`)

The framework id stays `colorado` (an API identifier). The law it tracks is
no longer the 2024 "Colorado AI Act" as enacted:

- SB 24-205 (2024) created part 17 of article 1 of title 6 (high-risk AI
  systems, developer/deployer risk management, impact assessments, AG
  notice of algorithmic discrimination). Its requirements never applied.
- SB25B-004 (signed 28 August 2025) extended its effective date to
  30 June 2026.
- SB 26-189 (signed 14 May 2026, 2026 Session Laws ch. 131) repealed and
  reenacted part 17 "with amendments" as "Automated Decision-Making
  Technology in Consequential Decisions". Section 5: the act "takes effect
  January 1, 2027" and "applies to consequential decisions made on or after
  January 1, 2027"; only the Attorney General rule-making provisions
  (6-1-1704(4), 6-1-1705(3)), 6-1-1706(6), the appropriation and the
  safety clause took effect on passage.

The reenacted part has no risk management programme, no impact assessment
and no Attorney General notification duty; the entries for those were
removed (and the 90-day deadline rule with them). Duties now are: developer
documentation and update notices to deployers (6-1-1702), deployer record
keeping for three years (6-1-1703), developer record keeping for three
years (6-1-1702(4)), point-of-interaction notice and post-adverse-outcome
disclosures within thirty days (6-1-1704), notices accessible to consumers
with disabilities or limited English proficiency (6-1-1704(8)), and
consumer correction / meaningful human review rights (6-1-1705).

Scope exemptions (6-1-1708) — not obligations, so not entries; every
entry's limit repeats them:

- (1)(a) an insurer (10-1-102(13)) and affiliates subject to 10-3-1104.9
  "are in compliance with this part 17 in the practice of insurance"
  ((2): not for insurer employment decisions; (1)(b): an insurer not so
  deemed gives the 6-1-1704(3) disclosures);
- (3)(a) 6-1-1701 to 6-1-1706 do not apply to a HIPAA covered entity, or
  its business associates for services rendered to it, except for
  consequential decisions about employment; (3)(b) a health-care provider
  only when operating from a Colorado location; such a covered entity
  instead gives patients a general notice of use of advanced technologies
  ((3)(c)) and, for financial-assistance eligibility, the (3)(d)
  disclosures;
- (4) 6-1-1701 to 6-1-1706 do not apply to an FDA-overseen medical device
  or a manufacturer's FDA-overseen research and development.

Equivalence rules inside 6-1-1704 (ECOA/FCRA adverse-action notices,
(6); FERPA processes, (9)) and 6-1-1705(2) (FERPA) are noted on the
entries they affect. The compliance profile cannot express any of these
exemptions as an applicability condition (obligations gate only on core
facts declared true); an exempt operator does not enable this framework.

Checked on 2026-10-16 against the Signed Act of SB 26-189 as published by
the Colorado General Assembly (leg.colorado.gov/bills/SB26-189, "Signed
Act" PDF, 05/14/2026). Every entry cites a verbatim excerpt (the act prints
new material in capitals; excerpts keep that). Entries use the crosswalk's
language: the gateway's records "can serve as evidence toward" a duty.
The Attorney General's rules due by 1 January 2027 (6-1-1704(4)(b),
6-1-1705(3)) were not published when this was checked. Not legal advice.
"""

from __future__ import annotations

from datetime import date
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

FRAMEWORK_ID = "colorado"
CATALOG_VERSION = "2026-09"

#: SB 26-189 section 5(1) and (3): takes effect, and applies to consequential
#: decisions made, on or after 1 January 2027. (Was 30 June 2026 under
#: SB25B-004 before SB 26-189 repealed and reenacted part 17.)
APPLIES = date(2027, 1, 1)

#: When every entry was checked against the published text.
CHECKED = date(2026, 10, 16)

#: Deployer records retention, 6-1-1703 ("not less than three years").
RECORD_RETENTION_DAYS = 3 * 365

#: The profile fact closest to "a covered ADMT used to materially influence
#: a consequential decision" (6-1-1701(5)). The statute's test is narrower
#: (covered domains, "materially influence"); the operator declares it.
CONSEQUENTIAL = frozenset({"automated_decisions_about_persons"})

_INSTRUMENT = (
    "Colorado SB 26-189 (2026 Sess. Laws ch. 131), repealing and reenacting "
    "C.R.S. Title 6, Article 1, Part 17 (Automated Decision-Making Technology in Consequential Decisions)"
)
#: The Signed Act PDF linked from leg.colorado.gov/bills/SB26-189.
_URL = "https://leg.colorado.gov/bill_files/116489/download"

_E = c.CROSSWALK_PREFIX

#: 6-1-1708 scope exemptions, repeated on every entry (see module doc).
_SCOPE = (
    "Not applicable (6-1-1708) to HIPAA covered entities and their business associates "
    "except for employment decisions, to FDA-overseen medical devices, or to insurers "
    "subject to 10-3-1104.9 in the practice of insurance."
)


def _scoped(text: str) -> str:
    return f"{text} {_SCOPE}"


def _src(provision: str, excerpt: str) -> Source:
    return Source(
        instrument=_INSTRUMENT,
        provision=provision,
        url=_URL,
        checked_on=CHECKED,
        excerpt=excerpt,
    )


_O = Obligation

OBLIGATIONS = [
    _O(
        # Key kept (API identifier). Was "disclosure of interaction with an AI
        # system" under SB 24-205; the reenacted 6-1-1704(1) is a notice that a
        # covered ADMT is used in a consequential decision.
        key="consumer-ai-disclosure",
        title="Notice that a covered ADMT is used in a consequential decision",
        reference="C.R.S. 6-1-1704(1)-(2)",
        applies_from=APPLIES,
        summary=(
            f"{_E} the deployer's clear and conspicuous notice, before a covered ADMT is used to "
            "materially influence a consequential decision, that it is used and how to obtain "
            "more information (a prominent public notice at points of consumer interaction is "
            "accepted)."
        ),
        roles=frozenset({"deployer"}),
        flags=CONSEQUENTIAL,
        evidence=[c.notice_before_first_turn(), c.notice_text()],
        limit=_scoped(
            "The gateway shows the AI notice on its own portal surface; notices given in the "
            "deployer's own channels, and whether a decision is consequential, are outside it. "
            "An ECOA/FCRA creditor notice (6-1-1704(6)) or a FERPA process (6-1-1704(9)) can "
            "stand in for this notice."
        ),
        sources=(
            _src(
                "C.R.S. 6-1-1704(1)",
                "PRIOR TO A DEPLOYER USING A COVERED ADMT TO MATERIALLY INFLUENCE A CONSEQUENTIAL "
                "DECISION, THE DEPLOYER SHALL PROVIDE A CLEAR AND CONSPICUOUS NOTICE TO A CONSUMER "
                "THAT THE DEPLOYER USED OR WILL USE A COVERED ADMT IN A CONSEQUENTIAL DECISION "
                "AFFECTING THE CONSUMER",
            ),
            _src(
                "SB 26-189 Section 5(3)",
                "This act applies to consequential decisions made on or after January 1, 2027.",
            ),
        ),
    ),
    _O(
        key="deployer-record-keeping",
        title="Deployer record keeping (three years)",
        reference="C.R.S. 6-1-1703",
        applies_from=APPLIES,
        summary=(
            f"{_E} retaining, for at least three years after a consequential decision, records "
            "reasonably necessary to demonstrate adherence to part 17: the retention floor and "
            "declared prunes over the gateway's records."
        ),
        roles=frozenset({"deployer"}),
        flags=CONSEQUENTIAL,
        evidence=[
            c.retention_floor(
                RECORD_RETENTION_DAYS,
                "The gateway's records of this system are retained for at least three years.",
                limit=_scoped(
                    "Covers the gateway's ledger rows and runs; records of the decision kept in "
                    "other systems are not covered."
                ),
            ),
            c.declared_prunes(),
        ],
        limit=_scoped("Which records are reasonably necessary is the deployer's judgement; it is not evaluated."),
        sources=(
            _src(
                "C.R.S. 6-1-1703",
                "A DEPLOYER SHALL RETAIN, FOR NOT LESS THAN THREE YEARS AFTER THE DATE OF A "
                "CONSEQUENTIAL DECISION OR FOR A LONGER PERIOD IF REQUIRED BY APPLICABLE STATE OR "
                "FEDERAL LAW, RECORDS REASONABLY NECESSARY TO DEMONSTRATE COMPLIANCE WITH THIS PART 17.",
            ),
        ),
    ),
    _O(
        key="adverse-outcome-disclosure",
        title="Post-adverse-outcome disclosure (thirty days)",
        reference="C.R.S. 6-1-1704(3)",
        applies_from=APPLIES,
        summary=(
            f"{_E} providing, within thirty days after a consequential decision with an adverse "
            "outcome, a plain-language description of the decision and the covered ADMT's role, "
            "how to request more information, and the consumer's rights under 6-1-1705."
        ),
        roles=frozenset({"deployer"}),
        flags=CONSEQUENTIAL,
        evidence=[
            c.declared(
                "declared.adverse_outcome_disclosure",
                "frameworks.colorado.adverse_outcome_disclosure_reference",
                "A post-adverse-outcome disclosure process is declared.",
                "Organisational duty; the disclosures and their timing are not evaluated.",
            ),
            c.witnessed("The gateway's record of the decision the disclosure describes is witnessed."),
        ],
        limit=_scoped(
            "The gateway does not know which decisions had an adverse outcome or whether a "
            "disclosure was sent; the Attorney General's rules on content were not yet published. "
            "An ECOA/FCRA creditor notice (6-1-1704(6)) or a FERPA process (6-1-1704(9)) can "
            "stand in for this disclosure."
        ),
        sources=(
            _src(
                "C.R.S. 6-1-1704(3)",
                "IF A DEPLOYER USES A COVERED ADMT TO MATERIALLY INFLUENCE A CONSEQUENTIAL DECISION "
                "THAT RESULTS IN AN ADVERSE OUTCOME FOR A CONSUMER, THE DEPLOYER SHALL PROVIDE WITHIN "
                "THIRTY DAYS AFTER MAKING THE DECISION:",
            ),
        ),
    ),
    _O(
        key="human-review-reconsideration",
        title="Meaningful human review and reconsideration on request",
        reference="C.R.S. 6-1-1705(1)(a)",
        applies_from=APPLIES,
        summary=(
            f"{_E} offering, on request after an adverse outcome, correction of inaccurate "
            "personal data and an opportunity for meaningful human review and reconsideration, "
            "to the extent commercially reasonable: human override and the record of its use."
        ),
        roles=frozenset({"deployer"}),
        flags=CONSEQUENTIAL,
        evidence=[c.override_available(), c.oversight_exercised()],
        limit=_scoped(
            "Shows human override and approval on the gateway; review of a decision after the "
            "fact, the reviewer's training (6-1-1701(15)) and data correction happen outside it. "
            "A FERPA deployer may use its existing FERPA processes (6-1-1705(2))."
        ),
        sources=(
            _src(
                "C.R.S. 6-1-1705(1)(a)(II)",
                "AN OPPORTUNITY FOR MEANINGFUL HUMAN REVIEW AND RECONSIDERATION OF THE "
                "CONSEQUENTIAL DECISION, TO THE EXTENT COMMERCIALLY REASONABLE.",
            ),
        ),
    ),
    _O(
        key="developer-documentation",
        title="Developer documentation and update notices to deployers",
        reference="C.R.S. 6-1-1702(1)-(2)",
        applies_from=APPLIES,
        summary=(
            f"{_E} the developer making available to each deployer the intended and known "
            "harmful uses, training-data categories, known limitations, and instructions for use, "
            "monitoring and meaningful human review, and notifying material updates."
        ),
        roles=frozenset({"developer"}),
        flags=CONSEQUENTIAL,
        evidence=[
            c.tech_docs(FRAMEWORK_ID),
            c.promotions_sealed("Changes to the system's active configuration are sealed with their author."),
        ],
        limit=_scoped(
            "A documentation skeleton and the change record; whether they reached each deployer, "
            "and their adequacy, are not evaluated."
        ),
        sources=(
            _src(
                "C.R.S. 6-1-1702(1)",
                "ON AND AFTER JANUARY 1, 2027, A DEVELOPER SHALL MAKE AVAILABLE TO EACH DEPLOYER OF "
                "A COVERED ADMT DEVELOPED BY THE DEVELOPER",
            ),
            _src(
                "C.R.S. 6-1-1702(2)(a)",
                "A DEVELOPER SHALL PROVIDE TO EACH DEPLOYER OF A COVERED ADMT DEVELOPED BY THE "
                "DEVELOPER A NOTICE OF MATERIAL UPDATES, INTENTIONAL AND SUBSTANTIAL MODIFICATIONS",
            ),
        ),
    ),
    _O(
        key="developer-record-keeping",
        title="Developer record keeping (three years)",
        reference="C.R.S. 6-1-1702(4)",
        applies_from=APPLIES,
        summary=(
            f"{_E} the developer retaining, for at least three years after a record is created, "
            "records reasonably necessary to demonstrate adherence to 6-1-1702, including system "
            "version identifiers, changelogs, and documentation and notices of material updates "
            "provided to deployers."
        ),
        roles=frozenset({"developer"}),
        flags=CONSEQUENTIAL,
        evidence=[
            c.retention_floor(
                RECORD_RETENTION_DAYS,
                "Documentation artefacts (documentation exports, declarations, reports) are kept for three years.",
                scope="documentation",
                limit=(
                    "Applies to documentation artefacts held by the platform only; changelogs and "
                    "notices kept elsewhere are not covered."
                ),
            ),
            c.promotions_sealed("Changes to the system's active configuration are sealed with their author."),
        ],
        limit=_scoped("Whether notices of material updates reached each deployer is not evaluated."),
        sources=(
            _src(
                "C.R.S. 6-1-1702(4)",
                "A DEVELOPER SHALL RETAIN, FOR NOT LESS THAN THREE YEARS AFTER THE CREATION OF A "
                "RECORD REQUIRED OR CREATED UNDER THIS SECTION OR FOR A LONGER PERIOD IF REQUIRED BY "
                "APPLICABLE STATE OR FEDERAL LAW, RECORDS REASONABLY NECESSARY TO DEMONSTRATE "
                "COMPLIANCE WITH THIS SECTION.",
            ),
        ),
    ),
    _O(
        key="notice-accessibility",
        title="Accessible notices and disclosures",
        reference="C.R.S. 6-1-1704(8)",
        applies_from=APPLIES,
        summary=(
            f"{_E} providing the notices and disclosures required by part 17 in a manner "
            "reasonably accessible to consumers with disabilities and consumers with limited "
            "English proficiency, consistent with applicable state and federal law."
        ),
        roles=frozenset({"deployer", "developer"}),
        flags=CONSEQUENTIAL,
        evidence=[
            c.declared(
                "declared.notice_accessibility",
                "frameworks.colorado.notice_accessibility_reference",
                "The approach to accessible and translated notices is declared.",
                "Declared by the operator; accessibility and translation are not evaluated.",
            ),
        ],
        limit=_scoped(
            "The gateway does not test notices for accessibility or language coverage."
        ),
        sources=(
            _src(
                "C.R.S. 6-1-1704(8)",
                "A DEPLOYER OR DEVELOPER SHALL PROVIDE THE NOTICES AND DISCLOSURES REQUIRED BY THIS "
                "PART 17 IN A MANNER THAT IS REASONABLY ACCESSIBLE TO CONSUMERS WITH DISABILITIES AND "
                "CONSUMERS WITH LIMITED ENGLISH PROFICIENCY, CONSISTENT WITH APPLICABLE STATE AND "
                "FEDERAL LAW.",
            ),
        ),
    ),
]

FRAMEWORK = Framework(
    id=FRAMEWORK_ID,
    title="Colorado ADMT law (SB 26-189)",
    jurisdiction="US-CO",
    reference=(
        "C.R.S. 6-1-1701 et seq., Automated Decision-Making Technology in Consequential Decisions "
        "(SB 24-205 as repealed and reenacted by SB 26-189)"
    ),
    kind=KIND_REGULATION,
    obligations=OBLIGATIONS,
    versions=(CatalogVersion(CATALOG_VERSION, effective_from=APPLIES, note="As reenacted by SB 26-189"),),
    roles=frozenset({"developer", "deployer"}),
    # The reenacted part has no incident notification duty (the SB 24-205
    # 90-day Attorney General notice was repealed).
    deadline_rules=MappingProxyType({}),
    # Deliberately empty — the three-year floor is a statement on the
    # record-keeping entry, not a tenant-wide floor (as HIPAA's six years).
    retention_floors=MappingProxyType({}),
    verified_against_instrument=True,
    instrument_status=INSTRUMENT_FINAL,
    verified_on=CHECKED,
)

__all__ = ["FRAMEWORK", "FRAMEWORK_ID", "RECORD_RETENTION_DAYS"]
