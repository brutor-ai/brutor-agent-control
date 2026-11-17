# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Framework catalog types

A framework is DATA: a set of dated obligations, each shown against
evidence statements (`EvidenceSpec`) that name a shared resolver
(`app/services/compliance/resolvers/`). This module holds only the types
and the pure applicability computation; every catalog module in this
package (`eu_ai_act.py`, `iso_42001.py`, …) builds one `Framework` and
exposes it as `FRAMEWORK`.

Honesty rules the types encode:

* applicability is dated (`applies_from`, per catalog version) and never a
  compliance boolean;
* an undeclared fact yields `undetermined` — an undeclared role, tier or
  flag cannot rule an obligation out;
* a catalog written from memory says so (`verified_against_instrument`).

Nothing here is legal advice; provision strings are pointers into the
instrument, not interpretations of it.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from types import MappingProxyType
from typing import Any, Dict, FrozenSet, List, Literal, Mapping, Optional, Sequence, Tuple

from app.database.models.ai_asset import GPAI, GPAI_SYSTEMIC_RISK

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

#: How an evidence statement is established. `recomputed` — derived
#: from the tenant log, ledger and config by a shared resolver; `judged` —
#: a tier-B template over sampled runs, with agreement; `indicator` —
#: movement, never a threshold; `declared` — an operator statement, sealed
#: but not checked.
BASIS_RECOMPUTED = "recomputed"
BASIS_JUDGED = "judged"
BASIS_INDICATOR = "indicator"
BASIS_DECLARED = "declared"
EVIDENCE_BASES = frozenset({BASIS_RECOMPUTED, BASIS_JUDGED, BASIS_INDICATOR, BASIS_DECLARED})
EvidenceBasis = Literal["recomputed", "judged", "indicator", "declared"]

#: Obligation applicability for one system.
STATUS_APPLIES = "applies"
STATUS_NOT_APPLICABLE = "not_applicable"
STATUS_UNDETERMINED = "undetermined"
APPLICABILITY_STATUSES = frozenset({STATUS_APPLIES, STATUS_NOT_APPLICABLE, STATUS_UNDETERMINED})

#: Framework kinds (mirrors `compliance_frameworks.kind`).
KIND_REGULATION = "regulation"
KIND_STANDARD = "standard"
KIND_ATTESTATION = "attestation"
KIND_VOLUNTARY = "voluntary"

#: Key in `Framework.retention_floors` that applies whatever the
#: classification (HIPAA's six-year documentation floor, SOC 2's period).
ANY_CLASSIFICATION = "*"

#: Appended to every provision string shown to a user.
NOT_LEGAL_ADVICE = "Not legal advice."

_EMPTY_PARAMS: Mapping[str, Any] = MappingProxyType({})
_EMPTY_DATES: Mapping[str, date] = MappingProxyType({})


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CatalogVersion:
    """One version of a catalog's text and its effective range.

    `effective_to` is exclusive; None = still in force.
    """

    catalog_version: str
    effective_from: date
    effective_to: Optional[date] = None
    note: Optional[str] = None

    def covers(self, day: date) -> bool:
        if day < self.effective_from:
            return False
        return self.effective_to is None or day < self.effective_to

    def to_dict(self) -> Dict[str, Any]:
        return {
            "catalog_version": self.catalog_version,
            "effective_from": self.effective_from.isoformat(),
            "effective_to": self.effective_to.isoformat() if self.effective_to else None,
            "note": self.note,
        }


@dataclass(frozen=True)
class EvidenceSpec:
    """One evidence statement an obligation is shown against.

    `resolver` is a shared resolver id or `judge.<template>`;
    `limit` is the honest line about what the statement does NOT show.
    """

    key: str
    statement: str
    basis: EvidenceBasis
    resolver: str
    params: Mapping[str, Any] = _EMPTY_PARAMS
    #: Restrict the statement to systems of these classifications
    #: (framework vocabulary); None = every system the obligation reaches.
    applies_to_kinds: Optional[FrozenSet[str]] = None
    limit: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "statement": self.statement,
            "basis": self.basis,
            "resolver": self.resolver,
            "params": dict(self.params),
            "applies_to_kinds": sorted(self.applies_to_kinds) if self.applies_to_kinds else None,
            "limit": self.limit,
        }

    @property
    def identity(self) -> Tuple[str, Tuple[Tuple[str, str], ...]]:
        """`(resolver, params)` — two specs with the same identity produce
        the same statement result under any framework."""
        return (self.resolver, tuple(sorted((k, repr(v)) for k, v in self.params.items())))


#: `Framework.instrument_status` — what the catalog can be checked against.
#: final: the published, in-force text (statute, regulation, standard).
#: draft: the instrument is not final (a bill, a decree not yet issued);
#:        nothing can be verified until it is.
#: licensed: the text exists but is sold (ISO); it is checked only by
#:        someone holding a copy.
INSTRUMENT_FINAL = "final"
INSTRUMENT_DRAFT = "draft"
INSTRUMENT_LICENSED = "licensed"


def normalise_passage(text: str) -> str:
    """Whitespace-collapsed passage, the form licensed-text fingerprints hash.

    PDF extraction breaks lines and hyphenates differently per tool; collapsing
    runs of whitespace (and removing a hyphen at a line break) makes the
    fingerprint reproducible from any text extraction of the same edition.
    """
    import re

    text = re.sub(r"-\s*\n\s*", "", text)
    return re.sub(r"\s+", " ", text).strip()


@dataclass(frozen=True)
class Source:
    """Where an obligation was checked: the published text, not a summary.

    A catalog may call itself verified only when every obligation carries at
    least one of these (`tests/test_compliance_catalog_provenance.py`).
    """

    #: The instrument, e.g. "Regulation (EU) 2022/2554".
    instrument: str
    #: The exact provision checked, e.g. "Art. 19(4)".
    provision: str
    #: The official publication (EUR-Lex, eCFR, NIST, the legislature).
    url: str
    #: When the entry was checked against that text.
    checked_on: date
    #: A short verbatim passage of the operative text, as proof it was read.
    #: Empty for a licensed text (ISO, AICPA): its copyright forbids
    #: reproducing it, so the proof is the fingerprint below instead.
    excerpt: str
    #: Licensed texts only: SHA-256 of the passage after `normalise_passage`,
    #: the passage's first words (a locator, short enough to cite) and its
    #: length in normalised characters. `scripts/verify_licensed_sources.py`
    #: recomputes it from a licensed copy, so the check can be repeated by
    #: anyone holding the text without the product reproducing it.
    excerpt_sha256: str = ""
    excerpt_starts_with: str = ""
    excerpt_length: int = 0


@dataclass(frozen=True)
class Obligation:
    key: str
    title: str
    reference: str
    applies_from: date
    summary: str
    #: Classification applicability (EU: risk tier). None = any.
    risk_tiers: Optional[FrozenSet[str]] = None
    #: Role applicability. None = any role.
    roles: Optional[FrozenSet[str]] = None
    #: Neutral-core facts (`core.<flag>`) that must be declared true.
    flags: Optional[FrozenSet[str]] = None
    #: 'gpai' | 'gpai_systemic_risk' — attaches to the MODEL track.
    requires_gpai: Optional[str] = None
    needs_review: bool = False
    review_note: Optional[str] = None
    evidence: List[EvidenceSpec] = field(default_factory=list)
    #: The catalog version the obligation first appears in (None = all).
    introduced_in: Optional[str] = None
    #: `applies_from` under an earlier catalog version (replay).
    applies_from_by_version: Mapping[str, date] = _EMPTY_DATES
    #: What the gateway cannot show for this obligation at all.
    limit: Optional[str] = None
    #: Earlier keys this obligation replaces (API backwards compatibility).
    aliases: FrozenSet[str] = frozenset()
    #: Where this obligation was checked against the published text.
    sources: Tuple[Source, ...] = ()

    def applies_from_in(self, catalog_version: Optional[str]) -> date:
        if catalog_version and catalog_version in self.applies_from_by_version:
            return self.applies_from_by_version[catalog_version]
        return self.applies_from


#: `DeadlineStage.anchor` values (besides `stage:<name>`).
ANCHOR_AWARE = "aware"
ANCHOR_CLASSIFIED = "classified"
ANCHOR_STAGE_PREFIX = "stage:"


def add_months(moment: datetime, months: int) -> datetime:
    """Calendar months, the day clamped to the target month's length
    (31 Jan + 1 month = 28/29 Feb) — never a fixed number of days."""
    total = moment.month - 1 + months
    year, month = moment.year + total // 12, total % 12 + 1
    day = min(moment.day, calendar.monthrange(year, month)[1])
    return moment.replace(year=year, month=month, day=day)


@dataclass(frozen=True)
class DeadlineStage:
    """One stage of a staged reporting regime (DORA, NIS2): what its clock
    runs from and how long it runs.

    `anchor` is `aware` (the incident's awareness time), `classified` (when
    the incident was classified as major / significant) or `stage:<name>`
    (the SUBMISSION of an earlier stage). The period is `within` or
    `months` (calendar months, `add_months`). `cap_after_aware` is an
    outer bound counted from awareness that applies whatever the anchor
    ("4 h from classification and no later than 24 h from awareness").
    """

    name: str
    anchor: str
    within: Optional[timedelta] = None
    months: Optional[int] = None
    cap_after_aware: Optional[timedelta] = None

    def __post_init__(self) -> None:
        if (self.within is None) == (self.months is None):
            raise ValueError(f"stage {self.name!r}: exactly one of within / months")
        if self.anchor not in (ANCHOR_AWARE, ANCHOR_CLASSIFIED) and not self.anchor.startswith(
            ANCHOR_STAGE_PREFIX
        ):
            raise ValueError(f"stage {self.name!r}: unknown anchor {self.anchor!r}")

    @property
    def anchor_stage(self) -> Optional[str]:
        """The earlier stage whose submission starts this clock, if any."""
        if self.anchor.startswith(ANCHOR_STAGE_PREFIX):
            return self.anchor[len(ANCHOR_STAGE_PREFIX):]
        return None

    def due_from(self, anchor_at: datetime) -> datetime:
        if self.months is not None:
            return add_months(anchor_at, self.months)
        assert self.within is not None
        return anchor_at + self.within

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "anchor": self.anchor,
            "within_hours": int(self.within.total_seconds() // 3600) if self.within is not None else None,
            "months": self.months,
            "cap_after_aware_hours": (
                int(self.cap_after_aware.total_seconds() // 3600) if self.cap_after_aware is not None else None
            ),
        }


@dataclass(frozen=True)
class Framework:
    id: str
    title: str
    jurisdiction: str
    reference: str
    obligations: List[Obligation] = field(default_factory=list)
    kind: str = KIND_REGULATION
    #: Oldest first. Empty = one unversioned catalog.
    versions: Sequence[CatalogVersion] = ()
    #: Role / classification vocabularies of this framework.
    roles: FrozenSet[str] = frozenset()
    classifications: FrozenSet[str] = frozenset()
    #: Where the system's role / classification is read from in the
    #: profile's `frameworks.<id>` block.
    role_field: str = "role"
    classification_field: str = "risk_tier"
    #: Incident reporting deadline per incident classification. For a
    #: staged framework (`deadline_stages` set) these are only the earliest
    #: bound of each stage (registry seed / summary / `deadline_for`); every
    #: due time is computed from `deadline_stages`.
    deadline_rules: Mapping[str, timedelta] = MappingProxyType({})
    #: Staged reporting regimes, in order: each stage's clock and anchor.
    #: Empty = alternatives regime (`deadline_rules` from awareness).
    deadline_stages: Tuple[DeadlineStage, ...] = ()
    #: Retention floor (days) per system classification; `*` = any.
    retention_floors: Mapping[str, int] = MappingProxyType({})
    #: True only when every obligation carries a `Source` from the published
    #: text and the instrument is final (pinned by
    #: `tests/test_compliance_catalog_provenance.py`). Unverified is the default.
    verified_against_instrument: bool = False
    #: final | draft | licensed — see INSTRUMENT_*.
    instrument_status: str = INSTRUMENT_FINAL
    #: When the catalog as a whole was last checked against the instrument.
    verified_on: Optional[date] = None
    #: The text is sold or copyrighted against reproduction (ISO, AICPA): its
    #: sources carry fingerprints, not excerpts. A licensed text checked
    #: against a full copy is `final` and can be verified and offered.
    licensed_text: bool = False
    #: Key in `compliance_frameworks`'s tagging vocabulary, if any.
    tag_key: Optional[str] = None
    #: Classification follows CONTRIBUTING dependency edges:
    #: a system is evaluated at its EFFECTIVE tier, max(own declared, the
    #: tier of every system whose decision it contributes to). Data, not
    #: code: the EU AI Act catalog sets it (draft Commission high-risk
    #: classification guidelines, May 2026); a framework whose text does not
    #: carry classification along a value chain leaves it off.
    composite_classification: bool = False

    def __post_init__(self) -> None:
        seen: List[str] = []
        for stage in self.deadline_stages:
            ref = stage.anchor_stage
            if ref is not None and ref not in seen:
                raise ValueError(f"{self.id}: stage {stage.name!r} anchors on {ref!r}, not an earlier stage")
            seen.append(stage.name)

    @property
    def staged(self) -> bool:
        return bool(self.deadline_stages)

    def deadline_stage(self, name: str) -> Optional[DeadlineStage]:
        return next((s for s in self.deadline_stages if s.name == name), None)

    # -- versions -----------------------------------------------------------

    @property
    def current_version(self) -> Optional[str]:
        return self.versions[-1].catalog_version if self.versions else None

    def version_on(self, day: date) -> Optional[CatalogVersion]:
        """The catalog version in force on `day` (None when the day
        predates every version, or the catalog is unversioned)."""
        for v in self.versions:
            if v.covers(day):
                return v
        return None

    def segments(self, start: date, end: date) -> List[Tuple[Optional[str], date, date]]:
        """Split `[start, end]` (inclusive days) at version boundaries:
        `[(catalog_version, seg_start, seg_end)]`. Days before the first
        version fall in a `None` segment."""
        if end < start:
            return []
        if not self.versions:
            return [(None, start, end)]
        out: List[Tuple[Optional[str], date, date]] = []
        cursor = start
        while cursor <= end:
            v = self.version_on(cursor)
            if v is None:
                nxt = next((x.effective_from for x in self.versions if x.effective_from > cursor), None)
                seg_end = min(end, (nxt - timedelta(days=1)) if nxt else end)
                out.append((None, cursor, seg_end))
            else:
                seg_end = end if v.effective_to is None else min(end, v.effective_to - timedelta(days=1))
                out.append((v.catalog_version, cursor, seg_end))
            cursor = seg_end + timedelta(days=1)
        return out

    def obligations_in(self, catalog_version: Optional[str]) -> List[Obligation]:
        """Obligations present in a catalog version (introduced at or
        before it). `None` = the current catalog."""
        if catalog_version is None or not self.versions:
            return list(self.obligations)
        order = [v.catalog_version for v in self.versions]
        if catalog_version not in order:
            return list(self.obligations)
        idx = order.index(catalog_version)
        return [
            o for o in self.obligations
            if o.introduced_in is None
            or (o.introduced_in in order and order.index(o.introduced_in) <= idx)
        ]

    # -- lookup -------------------------------------------------------------

    def obligation(self, key: str) -> Optional[Obligation]:
        return next((o for o in self.obligations if o.key == key), None)

    def resolve_key(self, key: str) -> List[Obligation]:
        """The obligation(s) a key names: itself, or every obligation that
        lists it as an alias (a renamed or split earlier key)."""
        own = self.obligation(key)
        if own is not None:
            return [own]
        return [o for o in self.obligations if key in o.aliases]

    # -- floors / deadlines -------------------------------------------------

    def retention_floor_for(self, classification: Optional[str]) -> Optional[int]:
        values = []
        if ANY_CLASSIFICATION in self.retention_floors:
            values.append(self.retention_floors[ANY_CLASSIFICATION])
        if classification and classification in self.retention_floors:
            values.append(self.retention_floors[classification])
        return max(values) if values else None

    def deadline_for(self, incident_classification: Optional[str]) -> Optional[timedelta]:
        if not self.deadline_rules:
            return None
        if incident_classification and incident_classification in self.deadline_rules:
            return self.deadline_rules[incident_classification]
        if "standard" in self.deadline_rules:
            return self.deadline_rules["standard"]
        return min(self.deadline_rules.values())

    def deadline_rules_dict(self) -> Dict[str, Dict[str, Any]]:
        return {
            k: {"hours": int(v.total_seconds() // 3600), "days": round(v.total_seconds() / 86400, 3)}
            for k, v in self.deadline_rules.items()
        }

    def to_summary(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "kind": self.kind,
            "jurisdiction": self.jurisdiction,
            "reference": self.reference,
            "catalog_version": self.current_version,
            "versions": [v.to_dict() for v in self.versions],
            "roles": sorted(self.roles),
            "classifications": sorted(self.classifications),
            "deadline_rules": self.deadline_rules_dict(),
            "deadline_stages": [s.to_dict() for s in self.deadline_stages],
            "retention_floors": dict(self.retention_floors),
            "verified_against_instrument": self.verified_against_instrument,
            "composite_classification": self.composite_classification,
            "obligation_count": len(self.obligations),
        }


# ---------------------------------------------------------------------------
# Applicability (pure)
# ---------------------------------------------------------------------------


@dataclass
class SystemContext:
    """The declared facts an applicability rule may reference.

    `risk_tier` / `role` are the system's classification and role IN THE
    FRAMEWORK being evaluated; `flags` are the neutral-core booleans
    (`interacts_with_natural_persons` …), `None` = undeclared.
    """

    risk_tier: Optional[str]
    role: Optional[str]
    gpai_models: List[str] = field(default_factory=list)
    gpai_systemic_models: List[str] = field(default_factory=list)
    flags: Dict[str, Optional[bool]] = field(default_factory=dict)
    #: The tier the system DECLARES. `risk_tier` is the tier
    #: it is evaluated at: the effective tier under a framework with
    #: `composite_classification`, else the declared one.
    risk_tier_declared: Optional[str] = None
    #: The systems whose decisions raised `risk_tier` above the declared tier
    #: (`[{system_id, system_name, tier}]`; empty when not raised).
    risk_tier_sources: List[Dict[str, Any]] = field(default_factory=list)


_FLAG_PROSE = {
    "interacts_with_natural_persons": "whether the system interacts with natural persons",
    "generates_synthetic_content": "whether the system generates synthetic content",
    "automated_decisions_about_persons": "whether the system makes automated decisions about persons",
}


def applicability(obligation: Obligation, ctx: SystemContext) -> Tuple[str, List[str]]:
    """`(status, reasons)` for one obligation against one system.

    Not-applicable wins over undetermined: a fact that rules the
    obligation out settles it even while another fact is undeclared.
    """
    status = STATUS_APPLIES
    reasons: List[str] = []
    undetermined: List[str] = []

    if obligation.requires_gpai is not None:
        models = (
            ctx.gpai_systemic_models
            if obligation.requires_gpai == GPAI_SYSTEMIC_RISK
            else ctx.gpai_models + ctx.gpai_systemic_models
        )
        if models:
            reasons.append("binds GPAI model(s): " + ", ".join(sorted(models)[:5]))
        else:
            return STATUS_NOT_APPLICABLE, ["no bound model carries this GPAI classification"]

    if obligation.risk_tiers is not None:
        if ctx.risk_tier is None:
            undetermined.append("risk tier not declared — classify the system")
        elif ctx.risk_tier in obligation.risk_tiers:
            reasons.append(f"risk tier is {ctx.risk_tier}")
        else:
            return STATUS_NOT_APPLICABLE, [
                f"risk tier {ctx.risk_tier} is outside {sorted(obligation.risk_tiers)}"
            ]

    if obligation.roles is not None:
        if ctx.role is None:
            undetermined.append("regulated role not declared — declare it")
        elif ctx.role in obligation.roles:
            reasons.append(f"role is {ctx.role}")
        else:
            return STATUS_NOT_APPLICABLE, [f"role {ctx.role} is outside this duty set"]

    for flag in sorted(obligation.flags or ()):
        value = ctx.flags.get(flag)
        prose = _FLAG_PROSE.get(flag, flag)
        if value is None:
            undetermined.append(f"not declared {prose} — declare it on the compliance profile")
        elif value:
            reasons.append(f"declared: {flag}")
        else:
            return STATUS_NOT_APPLICABLE, [f"declared not applicable: {flag} is false"]

    if undetermined:
        return STATUS_UNDETERMINED, undetermined + reasons
    if not reasons:
        reasons.append("applies to any AI system")
    return status, reasons


def evaluate(
    obligation: Obligation,
    ctx: SystemContext,
    *,
    today: date,
    catalog_version: Optional[str] = None,
) -> Dict[str, object]:
    """One obligation against one system's declared facts (the original shape,
    kept for the existing board and per-system endpoints)."""
    status, reasons = applicability(obligation, ctx)
    applies_from = obligation.applies_from_in(catalog_version)
    return {
        "key": obligation.key,
        "title": obligation.title,
        "reference": obligation.reference,
        "summary": obligation.summary,
        "applies_from": applies_from.isoformat(),
        "in_force": today >= applies_from,
        "status": status,
        "reason": "; ".join(reasons),
        "needs_review": obligation.needs_review,
        "review_note": obligation.review_note,
        "limit": obligation.limit,
        "aliases": sorted(obligation.aliases),
        "evidence": [spec.to_dict() for spec in obligation.evidence],
    }


__all__ = [
    "ANY_CLASSIFICATION",
    "APPLICABILITY_STATUSES",
    "BASIS_DECLARED",
    "BASIS_INDICATOR",
    "BASIS_JUDGED",
    "BASIS_RECOMPUTED",
    "ANCHOR_AWARE",
    "ANCHOR_CLASSIFIED",
    "ANCHOR_STAGE_PREFIX",
    "CatalogVersion",
    "DeadlineStage",
    "EVIDENCE_BASES",
    "EvidenceBasis",
    "EvidenceSpec",
    "Framework",
    "GPAI",
    "GPAI_SYSTEMIC_RISK",
    "KIND_ATTESTATION",
    "KIND_REGULATION",
    "KIND_STANDARD",
    "KIND_VOLUNTARY",
    "NOT_LEGAL_ADVICE",
    "Obligation",
    "STATUS_APPLIES",
    "STATUS_NOT_APPLICABLE",
    "STATUS_UNDETERMINED",
    "SystemContext",
    "add_months",
    "applicability",
    "evaluate",
]
