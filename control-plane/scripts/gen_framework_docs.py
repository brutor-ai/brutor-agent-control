#!/usr/bin/env python3
# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Generate the docs.brutor.ai framework pages from the compliance catalogs.

The catalogs (`app/services/compliance/catalogs/`) are the single source of
truth for every framework fact the docs state: offered or not and why, the
instrument and the date it was checked, each obligation with its provision,
dates, applicability, sources and evidence statements, and the deadline
rules. This script renders them, so a page cannot drift from its catalog.

Writes:

* `<docs>/src/content/docs/compliance/frameworks/<id>.mdx` — one page per
  catalog;
* every generated block in `<docs>/src/content/docs/**/*.mdx`, i.e. the text
  between `{/* BEGIN generated:<name> ... */}` and
  `{/* END generated:<name> */}` (block names: see `BLOCKS`).

Usage (from brutor-gateway-control):

    .venv-test/bin/python scripts/gen_framework_docs.py            # write
    .venv-test/bin/python scripts/gen_framework_docs.py --check    # exit 1 on drift
    .venv-test/bin/python scripts/gen_framework_docs.py --docs /path/to/brutor-ai-docs

The default docs checkout is the sibling `../brutor-ai-docs`. Output is
deterministic (no timestamps; catalog order). Licensed texts (`licensed_text`
or `instrument_status == "licensed"`) are never quoted: their sources render
as provision, link, check date and passage fingerprint only.

`tests/test_framework_docs_in_sync.py` runs `--check` when the docs checkout
exists next to the control plane.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import env_vars  # noqa: E402

# The application is imported, not run: its models want these to exist.
env_vars.DATABASE_URL.set_default("postgresql://docs:docs@localhost/docs")
env_vars.JWT_SECRET.set_default("gen-framework-docs-not-a-real-secret-00000")
env_vars.ENCRYPTION_KEY.set_default("a" * 43 + "=")

from app.services.compliance import offering  # noqa: E402
from app.services.compliance.catalogs import CATALOGS  # noqa: E402
from app.services.compliance.catalogs.base import (  # noqa: E402
    ANCHOR_AWARE,
    ANCHOR_CLASSIFIED,
    INSTRUMENT_LICENSED,
    EvidenceSpec,
    Framework,
    Obligation,
    Source,
)

DEFAULT_DOCS = ROOT.parent / "brutor-ai-docs"
PAGES_DIR = Path("src/content/docs/compliance/frameworks")
CONTENT_DIR = Path("src/content/docs")
CHECK_PAGE = "/compliance/how-frameworks-are-checked/"
API_INCIDENTS = "/v1/admin/compliance/incidents"

# ---------------------------------------------------------------------------
# Hand-written prose per framework. Facts in it must come from the catalog
# module (its docstring and entries); everything else on a page is rendered
# from the catalog itself. `intro` goes above the facts table, `after_table`
# below it, `sections` after the deadline rules.
# ---------------------------------------------------------------------------

PROSE: Dict[str, Dict[str, str]] = {
    "eu-ai-act": {
        "intro": (
            "Regulation (EU) 2024/1689 as amended by the Digital Omnibus on AI, Regulation (EU) "
            "2026/1744 (in force 27 July 2026). Dates of application follow Art 113 as amended: "
            "Chapters I–II from **2 February 2025**, except the prohibitions the Omnibus added, "
            "which apply from **2 December 2026**; general-purpose AI models (Chapter V) from "
            "**2 August 2025**; Art 50 transparency from **2 August 2026** — the AI-interaction "
            "notice of Art 50(1) is the obligation in force today that the gateway can evidence "
            "directly; high-risk duties for Annex III systems from **2 December 2027** and for "
            "Annex I embedded systems from **2 August 2028**. Art 50(2) marking reaches systems "
            "placed on the market before 2 August 2026 by 2 December 2026."
        ),
        "after_table": (
            "The catalog has two versions. `2024-08` carries the dates of the Act as published and "
            "was in force until 27 July 2026; `2026-09` carries the dates as amended. A statement "
            "is evaluated against the version in force on the action's date, so a report over an "
            "older window keeps the older dates.\n\n"
            "Art 43, 47, 48, 49, 72, 73 and 86 sit outside Chapter III Sections 1–3 and so fall, as "
            "written, under the general 2 August 2026 date. They bind only systems classified "
            "high-risk, and the classification rules apply from the dates above, so the catalog "
            "dates them with Section 1."
        ),
        "sections": (
            "## How applicability is computed\n\n"
            "Each obligation is evaluated per AI System from the facts declared on its "
            "[compliance profile](/compliance/framework-registry/#the-compliance-profile) — the "
            "`eu-ai-act` block's `role` and `risk_tier` (written through to the resource group's "
            "`eu_ai_act_role` / `eu_ai_act_risk_tier` columns), the neutral-core flags such as "
            "`interacts_with_natural_persons`, and the GPAI classification of the models the system "
            "binds (flag models `gpai` / `gpai_systemic_risk` in the "
            "[Asset Register](/governance/ai-asset-registry/); the GPAI duties attach to the model).\n\n"
            "**Composite systems.** The catalog sets composite classification: a system is evaluated "
            "at its **effective** tier — the highest of its declared `risk_tier` and the tier of "
            "every AI System that declares a *contributing* dependency on it "
            "([composite systems](/compliance/framework-registry/#composite-systems)). A dependency "
            "declared non-contributing (with a sealed separability rationale) keeps its own tier; "
            "the declared tier stays visible as a claim.\n\n"
            "The board models *\"obligation X applies to system Y from date Z\"*, never "
            "*\"system Y is compliant\"*. Three statuses per (obligation, system):\n\n"
            "| Status | Meaning |\n|---|---|\n"
            "| **applies** | The system's declared facts put it in scope. |\n"
            "| **not applicable** | A declared fact rules it out (e.g. minimal risk → no high-risk duties). |\n"
            "| **undetermined** | The system has not declared the fact the rule needs. An undeclared "
            "role cannot rule an obligation *out* — the answer is \"declare it\", shown as a to-do, "
            "never a pass. |\n\n"
            "Obligations with a **Review** note name a determination the platform cannot make "
            "alone; record it on the profile."
        ),
    },
    "iso-42001": {
        "intro": (
            "ISO/IEC 42001:2023 is an AI management system **standard**: an organization "
            "determines its controls (its Statement of Applicability selects them) and is "
            "certified — a system is not. Each entry is a control or clause the gateway can produce "
            "evidence toward: the event log (A.6.2.8), operation and monitoring (A.6.2.6), "
            "deployment (A.6.2.5) and documentation (A.6.2.3, A.6.2.7, A.8.2). Organizational "
            "controls — AI policy, roles, resources, impact assessment, data, third parties — are "
            "evidenced by pointer and declaration only. Summaries are the catalog's own "
            "paraphrase, not the standard's wording."
        ),
        "after_table": (
            "**Licensed text.** ISO sells the standard and its copyright forbids reproducing it, so "
            "this page never quotes it. Each source names the clause or control, links ISO's "
            "official page and gives a passage fingerprint that anyone holding a licensed copy of "
            "the same edition can re-check ([how](" + CHECK_PAGE + "#licensed-texts)). Where the "
            "Annex A table layout splits a control in text extraction, the source fingerprints the "
            "same control as restated in Annex B (provision `B.x.y`).\n\n"
            "The standard sets no retention period: impact-assessment results are kept for a period "
            "the organization defines, so the catalog carries no retention floor. Applicability is "
            "estate-wide (no role or classification vocabulary); `frameworks.iso-42001.in_scope` "
            "scopes the management system. The ISO 42001 activity endpoint "
            "(`GET /v1/admin/compliance/iso-42001/activity`) is kept."
        ),
    },
    "soc2": {
        "intro": (
            "SOC 2 is an **attestation**: a service auditor examines a service organization's "
            "controls against the AICPA Trust Services Criteria and issues an opinion. The criteria "
            "are not law, and nothing here is that opinion. Each entry is a criterion the "
            "gateway's records can serve as evidence toward — what an auditor gets is a witnessed "
            "activity log with an independent timestamp. The common criteria (CC) apply to every "
            "SOC 2 report; the availability (A), confidentiality (C) and processing integrity (PI) "
            "criteria apply only when that category is in the report's scope."
        ),
        "after_table": (
            "**Licensed text.** The criteria are AICPA copyright, so this page never quotes them. "
            "Each source names the criterion, links the AICPA's official page and gives a passage "
            "fingerprint that anyone holding a licensed copy of the same edition can re-check "
            "([how](" + CHECK_PAGE + "#licensed-texts)). The 2022 revisions change points of "
            "focus only; the criteria are those of the 2017 TSC.\n\n"
            "**Not fingerprinted:** the `applies_from` date, 2018-12-15, is the AICPA's transition "
            "date for the 2017 criteria (reports for periods ending on or after that date). The "
            "2022 edition does not restate an effective date, so this date is not covered by a "
            "fingerprint.\n\n"
            "The retention floor `*: 365` days is the default attestation (examination) period, "
            "not a TSC requirement; declare a different period on the profile "
            "(`frameworks.soc2.attestation_period_days`). SOC 2 sets no incident reporting "
            "deadline: your own SLA applies."
        ),
    },
    "hipaa": {
        "intro": (
            "45 CFR Part 164 Subpart C (the Security Rule), plus the Breach Notification Rule's "
            "60-day deadline (Subpart D: §164.404 to individuals, §164.410 business associate to "
            "covered entity). **PHI never enters an action record** — identities, headers and "
            "bodies never enter it and content is digest-only — so the audit-control and integrity "
            "statements are about records that carry digests, never content. The Security Rule "
            "proposed rule of January 2025 (90 FR 898) has not been finalized; nothing here is "
            "taken from it."
        ),
        "after_table": (
            "The six-year documentation requirement (§164.316(b)(2)(i)) covers the "
            "*documentation* the rule requires — policies, reports, declarations — not every log "
            "line. It is expressed as a `retention.floor` statement scoped to documentation "
            "artifacts, so the catalog carries no framework-wide retention floor. Business "
            "associates became directly subject to the Security Rule on 23 September 2013; "
            "`applies from` keeps the covered-entity date for both roles."
        ),
    },
    "gdpr": {
        "intro": (
            "Regulation (EU) 2016/679 — the provisions an AI gateway's records bear on. The classic "
            "conflict between Art 17 erasure and an immutable log is resolved by the design: "
            "**identities and bodies never enter a record**, so erasure never needs to touch the "
            "evidence tables."
        ),
        "after_table": (
            "The Art 33 breach clock is 72 hours from awareness (`standard`). The Art 30 records "
            "of processing export (`GET /v1/admin/compliance/gdpr/article-30`) keeps its shape."
        ),
    },
    "dora": {
        "intro": (
            "Regulation (EU) 2022/2554, applicable from 17 January 2025. Its duties fall on the "
            "financial entities of Art 2(1)(a)–(t); ICT third-party service providers are reached "
            "through the entity's contracts, so every obligation is scoped to the "
            "`financial_entity` role. Incident time limits come from Commission Delegated "
            "Regulation (EU) 2025/301 and logging from Commission Delegated Regulation (EU) "
            "2024/1774, Art 12."
        ),
        "after_table": (
            "Major ICT-related incidents are reported in three stages, each on its own clock "
            "(Art 19(4); Delegated Regulation 2025/301, Art 5(1)). Weekend and bank-holiday relief "
            "(Art 5(4) of that regulation) is not modelled — it does not apply to credit "
            "institutions for the initial and intermediate stages (Art 5(5)); for other entities a "
            "deadline falling on a weekend or bank holiday may be later than shown. Updated "
            "intermediate reports are not modelled: the final report is counted from the recorded "
            "intermediate submission."
        ),
    },
    "nis2": {
        "intro": (
            "Directive (EU) 2022/2555. A Directive binds essential and important entities through "
            "national transposition, applied from 18 October 2024 (Art 41(1)); scope is set by "
            "Art 2 and Art 3. Significant incidents are reported in stages (Art 23(4)): an early "
            "warning, an incident notification and a final report."
        ),
        "after_table": (
            "Transposition differs between member states — check the national law that applies to "
            "you. Logging and monitoring detail is cross-checked against Commission Implementing "
            "Regulation (EU) 2024/2690, which applies only to the entities listed in its Art 1."
        ),
    },
    "nist-ai-rmf": {
        "intro": (
            "NIST AI 100-1, AI RMF 1.0 (January 2023) is **voluntary**: there is nothing to comply "
            "with, only practices an organization can show it follows. Entries use the crosswalk's "
            "language — the gateway's records *can serve as evidence toward* a subcategory; they "
            "never satisfy one. The Generative AI Profile (NIST AI 600-1) is not catalogued."
        ),
    },
    "colorado": {
        "intro": (
            "The registry id stays `colorado`, but the law it tracks is no longer the 2024 "
            "\"Colorado AI Act\" as enacted. SB 24-205 created part 17 of article 1 of title 6; "
            "its requirements never applied (SB25B-004 had deferred them to 30 June 2026). "
            "**SB 26-189** (signed 14 May 2026) repealed and reenacted part 17 as *Automated "
            "Decision-Making Technology in Consequential Decisions*; it applies to consequential "
            "decisions made on or after **1 January 2027**.\n\n"
            "The reenacted part has no risk-management programme, no impact assessment and no "
            "Attorney General notification duty, so there is no incident deadline. Its duties: "
            "developer documentation and update notices to deployers, developer and deployer "
            "record keeping for three years, point-of-interaction notice and post-adverse-outcome "
            "disclosures, accessible notices, and consumer correction and meaningful human review "
            "rights. The Attorney General's rules due by 1 January 2027 were not published when the "
            "catalog was checked."
        ),
        "after_table": (
            "**Exemptions (C.R.S. 6-1-1708) are not modelled.** Insurers, HIPAA covered entities "
            "and their business associates (except for employment decisions), and FDA-overseen "
            "medical devices are wholly or partly outside the part; each entry's limit repeats "
            "this. The compliance profile cannot express an exemption as an applicability "
            "condition — an exempt operator does not enable this framework."
        ),
    },
    "korea": {
        "intro": (
            "Korea's Framework Act on the Development of Artificial Intelligence and the Creation "
            "of a Foundation for Trust (Act No. 20676, in force 22 January 2026) and its "
            "Enforcement Decree: advance notice of high-impact and generative AI, labelling of "
            "generative outputs, the advance high-impact review, safety and reliability measures "
            "for high-impact AI, and the domestic agent of a foreign operator. Classification is "
            "declared in the profile's `frameworks.korea.classification` field.\n\n"
            "Not catalogued: the Art 35(1) fundamental-rights impact assessment (a duty to "
            "endeavour, not a mandatory duty) and the Art 32 safety duties (frontier-model duties). "
            "The Korean text on law.go.kr is authoritative; English excerpts are from KLRI's "
            "official translation of the original Act."
        ),
    },
    "brazil": {
        "intro": (
            "PL 2338/2023 is a **bill, not law**. It passed the Federal Senate in December 2024 and "
            "is pending in the Chamber of Deputies. There is no enacted text to check against, so "
            "the catalog is a placeholder: one entry recording the operator's own preliminary "
            "assessment, with an `applies from` date of 2099-12-31 so nothing reads as in force. "
            "It is replaced by a real catalog once the law is enacted and checked."
        ),
    },
}

# ---------------------------------------------------------------------------
# MDX helpers
# ---------------------------------------------------------------------------

_ESC = str.maketrans({
    "\\": "\\\\", "{": "\\{", "}": "\\}", "<": "&lt;", ">": "&gt;",
    "*": "\\*", "_": "\\_", "[": "\\[", "]": "\\]",
})


def esc(text: str) -> str:
    """Catalog prose as safe MDX inline text (one line). Code spans the
    catalog writes (`like_this`) are kept as code; everything else is
    escaped."""
    parts = " ".join(str(text).split()).split("`")
    if len(parts) % 2 == 0:  # unbalanced backtick: treat all as text
        return "\\`".join(p.translate(_ESC) for p in parts)
    return "`".join(p if i % 2 else p.translate(_ESC) for i, p in enumerate(parts))


def cell(text: str) -> str:
    return esc(text).replace("|", "\\|")


def code(text: str) -> str:
    return f"`{text}`"


def yaml_str(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)


def is_licensed(fw: Framework) -> bool:
    return bool(fw.licensed_text) or fw.instrument_status == INSTRUMENT_LICENSED


def duration(td) -> str:
    hours = int(td.total_seconds() // 3600)
    if hours % 24 == 0 and hours >= 24:
        days = hours // 24
        label = f"{days} day" + ("s" if days != 1 else "")
        return f"{label} ({hours} h)" if days <= 3 else label
    return f"{hours} h"


def page_slug(fid: str) -> str:
    return f"/compliance/frameworks/{fid}/"


# ---------------------------------------------------------------------------
# Page sections
# ---------------------------------------------------------------------------


def status_aside(fw: Framework) -> str:
    reason = offering.unavailable_reason(fw)
    if reason is None:
        how = (
            "by passage fingerprint — the licensed text is never reproduced"
            if is_licensed(fw)
            else "with a verbatim excerpt"
        )
        return (
            '<Aside type="tip" title="Offered">\n'
            f"**Offered** — checked against {esc(fw.reference)}, {fw.verified_on.isoformat()}. "
            f"Every obligation below cites the provision it was checked against, {how}. "
            f"[How frameworks are checked]({CHECK_PAGE}).\n"
            "</Aside>"
        )
    return (
        '<Aside type="caution" title="Not offered">\n'
        f"**Not offered** — {esc(reason)} It cannot be enabled, evaluated, reported on, documented "
        "or used in incidents: the API refuses with HTTP 409 and this reason. A tenant that enabled "
        "it earlier keeps the stored setting, but it reads as off. This page shows the catalog only "
        f"to explain what is pending. [How frameworks are checked]({CHECK_PAGE}).\n"
        "</Aside>"
    )


NOT_LEGAL_ADVICE = (
    '<Aside type="note" title="Not legal advice">\n'
    "Provision strings are pointers into the instrument, not interpretations of it. Every statement "
    "below **can serve as evidence toward** an obligation; none of them says a system is compliant, "
    "and each one states what it does *not* show.\n"
    "</Aside>"
)


def versions_line(fw: Framework) -> str:
    parts = []
    current = fw.current_version
    for v in fw.versions:
        span = f"{code(v.catalog_version)} from {v.effective_from.isoformat()}"
        if v.effective_to:
            span += f" until {v.effective_to.isoformat()}"
        if v.catalog_version == current:
            span += " (current)"
        if v.note:
            span += f" — {cell(v.note)}"
        parts.append(span)
    return "; ".join(parts) or "unversioned"


def deadline_summary(fw: Framework) -> str:
    if fw.staged:
        return "staged: " + " → ".join(code(s.name) for s in fw.deadline_stages) + " (see below)"
    if not fw.deadline_rules:
        return "none"
    return ", ".join(f"{code(k)}: {duration(v)}" for k, v in fw.deadline_rules.items())


def facts_table(fw: Framework) -> str:
    offered = offering.is_offered(fw)
    rows: List[Tuple[str, str]] = [
        ("Registry id", code(fw.id)),
        ("Status", "**offered**" if offered else "**not offered** — see the banner above"),
        ("Kind", code(fw.kind)),
        ("Jurisdiction", cell(fw.jurisdiction)),
        ("Instrument", cell(fw.reference)),
        ("Instrument status", code(fw.instrument_status) + (" (licensed text)" if is_licensed(fw) else "")),
        ("Checked against the instrument", fw.verified_on.isoformat() if fw.verified_against_instrument and fw.verified_on else "no"),
        ("Catalog versions", versions_line(fw)),
    ]
    if fw.tag_key:
        rows.append(("Tagging key ([compliance tags](/compliance/tagging/))", code(fw.tag_key)))
    if fw.roles:
        rows.append((f"Roles ({code(f'frameworks.{fw.id}.{fw.role_field}')})",
                     ", ".join(code(r) for r in sorted(fw.roles))))
    if fw.classifications:
        rows.append((f"Classifications ({code(f'frameworks.{fw.id}.{fw.classification_field}')})",
                     ", ".join(code(c) for c in sorted(fw.classifications))))
    rows.append(("Incident deadlines", deadline_summary(fw)))
    floors = ", ".join(f"{code(k)}: {v} days" for k, v in sorted(fw.retention_floors.items()))
    rows.append(("Retention floors", floors or "none at classification level"))
    rows.append(("Obligations", str(len(fw.obligations))))
    return "| | |\n|---|---|\n" + "\n".join(f"| {k} | {v} |" for k, v in rows)


def _anchor_prose(anchor: str) -> str:
    if anchor == ANCHOR_AWARE:
        return f"awareness ({code('became_aware_at')})"
    if anchor == ANCHOR_CLASSIFIED:
        return f"classification of the incident ({code('classified_at')})"
    return f"submission of the {code(anchor.split(':', 1)[1])} stage"


def deadline_section(fw: Framework) -> str:
    if fw.staged:
        rows = []
        for st in fw.deadline_stages:
            period = f"{st.months} calendar month" + ("s" if st.months != 1 else "") if st.months else \
                f"{int(st.within.total_seconds() // 3600)} h"
            bound = f"no later than {int(st.cap_after_aware.total_seconds() // 3600)} h after awareness" \
                if st.cap_after_aware else "—"
            rows.append(f"| {code(st.name)} | {_anchor_prose(st.anchor)} | {period} | {bound} |")
        notes = [
            "Staged rules: every stage applies to a qualifying incident, and each stage's clock runs "
            "from its own anchor. Record each submission with "
            f"`POST {API_INCIDENTS}/{{id}}/submissions`; a stage is judged by its own recorded "
            "submission time. `reported_at` counts as the first stage's submission when none is "
            "recorded.",
        ]
        if any(st.anchor == ANCHOR_CLASSIFIED for st in fw.deadline_stages):
            notes.append(
                "Until `classified_at` is recorded, only the outer bound counted from awareness is "
                "shown, and the deadline row says it is waiting for classification."
            )
        if any(st.anchor_stage for st in fw.deadline_stages):
            notes.append(
                "A stage whose anchor stage has not been submitted has no due time yet (status "
                "`waiting`). Closing an incident does not satisfy an unsubmitted stage (status "
                "`closed_unsubmitted`). Calendar months keep the day of the month, clamped to the "
                "month's length (31 January + 1 month = 28/29 February)."
            )
        return (
            "## Deadline rules\n\n" + " ".join(notes) + "\n\n"
            "| Stage | Clock starts at | Due within | Outer bound |\n|---|---|---|---|\n"
            + "\n".join(rows) + "\n\n"
            "Deadlines are **computed and shown, never enforced** — see "
            "[incidents](/compliance/incidents-and-reports/#incidents)."
        )
    if not fw.deadline_rules:
        return ""
    rows = "\n".join(f"| {code(k)} | {duration(v)} |" for k, v in fw.deadline_rules.items())
    lead = (
        "Alternative rules: the incident's classification picks the rule when it names one, else "
        "`standard` applies."
        if len(fw.deadline_rules) > 1
        else "One rule, counted from awareness."
    )
    return (
        f"## Deadline rules\n\n{lead}\n\n| Rule | Deadline from `became_aware_at` |\n|---|---|\n{rows}\n\n"
        "Deadlines are **computed and shown, never enforced** — see "
        "[incidents](/compliance/incidents-and-reports/#incidents)."
    )


def applicability_line(ob: Obligation) -> str:
    parts = []
    if ob.risk_tiers:
        parts.append("classification " + ", ".join(code(t) for t in sorted(ob.risk_tiers)))
    if ob.roles:
        parts.append(("role " if len(ob.roles) == 1 else "roles ") + ", ".join(code(r) for r in sorted(ob.roles)))
    for flag in sorted(ob.flags or ()):
        parts.append(f"profile flag {code('core.' + flag)}")
    if ob.requires_gpai:
        parts.append(f"systems binding a {code(ob.requires_gpai)} model")
    if not parts:
        return "Applies to every system for which the framework is active."
    return "Applies to: " + "; ".join(parts) + "."


def source_item(src: Source, licensed: bool, offered: bool) -> str:
    head = f"- [{esc(src.provision)}]({src.url}) — {esc(src.instrument)}"
    day = src.checked_on.isoformat()
    if licensed:
        if src.excerpt_sha256:
            return (f"{head} · checked against a licensed copy on {day}; passage fingerprint "
                    f"`{src.excerpt_sha256[:12]}`…")
        return f"{head} · consulted {day} (licensed text, not reproduced)"
    verb = "checked" if offered else "consulted"
    item = f"{head} · {verb} {day}"
    if src.excerpt:
        item += "\n\n  > " + esc(src.excerpt)
    return item


def spec_row(spec: EvidenceSpec) -> str:
    resolver = code(spec.resolver)
    if spec.params:
        resolver += " " + code(json.dumps(dict(spec.params), sort_keys=True, ensure_ascii=False))
    statement = cell(spec.statement)
    if spec.applies_to_kinds:
        statement += " (only for " + ", ".join(code(k) for k in sorted(spec.applies_to_kinds)) + ")"
    limit = cell(spec.limit) if spec.limit else "—"
    return f"| {statement} | {spec.basis} | {resolver} | {limit} |"


def obligation_section(fw: Framework, ob: Obligation) -> str:
    offered = offering.is_offered(fw)
    licensed = is_licensed(fw)
    meta = [code(ob.key), esc(ob.reference), f"applies from **{ob.applies_from.isoformat()}**"]
    if ob.introduced_in:
        meta.append(f"introduced in catalog {code(ob.introduced_in)}")
    for ver, day in sorted(ob.applies_from_by_version.items()):
        meta.append(f"earlier catalog: {code(ver)} → {day.isoformat()}")
    if ob.aliases:
        meta.append("formerly " + ", ".join(code(a) for a in sorted(ob.aliases)))
    out = [f"### {esc(ob.title)}", " · ".join(meta), f"{esc(ob.summary)} {applicability_line(ob)}"]
    if ob.needs_review and ob.review_note:
        out.append(f"**Review:** {esc(ob.review_note)}")
    if ob.limit:
        out.append(f"**What the gateway cannot show:** {esc(ob.limit)}")
    if ob.sources:
        label = "**Checked against:**" if offered else "**Sources consulted (not a line-by-line check):**"
        sep = "\n\n" if any(s.excerpt for s in ob.sources) and not licensed else "\n"
        out.append(label + "\n\n" + sep.join(source_item(s, licensed, offered) for s in ob.sources))
    elif not offered:
        out.append("**Checked against:** nothing yet — not checked against a published text.")
    rows = "\n".join(spec_row(s) for s in ob.evidence)
    out.append("| Statement | Basis | Resolver | Does not show |\n|---|---|---|---|\n" + rows)
    return "\n\n".join(out)


RELATED = (
    "## Related\n\n"
    f"- [How frameworks are checked]({CHECK_PAGE}) — what \"offered\" means and how sources are kept\n"
    "- [Framework registry & evidence bases](/compliance/framework-registry/) — how catalogs, "
    "resolvers and the version in force work\n"
    "- [The Compliance console](/compliance/console/) — the Obligations matrix for this framework\n"
    "- [Incidents, reports & documentation](/compliance/incidents-and-reports/)"
)


def render_page(fw: Framework) -> str:
    prose = PROSE.get(fw.id, {})
    desc = (
        f"The {fw.title} catalog in Brutor's framework registry: whether it is offered, the "
        "obligations with their dates and the official provisions they were checked against, the "
        "evidence statements each is shown against, and their limits. Not legal advice."
    )
    parts = [
        f"---\ntitle: {yaml_str(fw.title)}\ndescription: {yaml_str(desc)}\n---",
        "import { Aside } from '@astrojs/starlight/components';",
        "{/* Generated by brutor-gateway-control/scripts/gen_framework_docs.py from the catalog "
        f"app/services/compliance/catalogs/ ({fw.id}). Do not edit by hand. */}}",
        status_aside(fw),
        NOT_LEGAL_ADVICE,
    ]
    if prose.get("intro"):
        parts.append(prose["intro"])
    parts.append(facts_table(fw))
    if prose.get("after_table"):
        parts.append(prose["after_table"])
    dl = deadline_section(fw)
    if dl:
        parts.append(dl)
    if prose.get("sections"):
        parts.append(prose["sections"])
    aliased = [(a, o.key) for o in fw.obligations for a in sorted(o.aliases)]
    parts.append(
        "## Obligations and their evidence\n\n"
        "Each obligation lists where it was checked and the statements it is shown against. The "
        "basis is one of **recomputed**, **judged**, **indicator** or **declared** "
        "([what the bases mean](/compliance/framework-registry/#four-bases)); the resolver is from "
        "the [shared library](/compliance/framework-registry/#the-shared-resolver-library)."
        + (
            " Earlier obligation keys still resolve through "
            "`GET /v1/admin/compliance/obligations/{key}`: "
            + ", ".join(f"{code(a)} → {code(k)}" for a, k in aliased) + "."
            if aliased else ""
        )
    )
    for ob in fw.obligations:
        parts.append(obligation_section(fw, ob))
    parts.append(RELATED)
    return "\n\n".join(parts) + "\n"


# ---------------------------------------------------------------------------
# Generated blocks inside hand-written pages
# ---------------------------------------------------------------------------


def block_framework_status() -> str:
    rows = []
    for fw in CATALOGS.values():
        offered = offering.is_offered(fw)
        how = "licensed text, by fingerprint" if is_licensed(fw) else "official text, verbatim excerpts"
        status = (f"**offered** — checked {fw.verified_on.isoformat()} ({how})" if offered
                  else f"**not offered** — {cell(offering.unavailable_reason(fw) or '')}")
        rows.append(
            f"| [{cell(fw.title)}]({page_slug(fw.id)}) | {code(fw.kind)} | {cell(fw.jurisdiction)} | "
            f"{code(fw.instrument_status)} | {status} | {len(fw.obligations)} |"
        )
    return ("| Framework | Kind | Jurisdiction | Instrument | Status | Obligations |\n"
            "|---|---|---|---|---|---|\n" + "\n".join(rows))


def block_offered_summary() -> str:
    offered = [fw for fw in CATALOGS.values() if offering.is_offered(fw)]
    others = [fw for fw in CATALOGS.values() if not offering.is_offered(fw)]
    licensed = [fw for fw in offered if is_licensed(fw)]
    links = ", ".join(f"[{esc(fw.title)}]({page_slug(fw.id)})" for fw in offered)
    text = (f"**{len(offered)} frameworks are offered**, each checked against its published text"
            + (f" ({len(licensed)} licensed "
               + ("text" if len(licensed) == 1 else "texts")
               + " — " + ", ".join(esc(fw.title) for fw in licensed)
               + " — by passage fingerprint)" if licensed else "")
            + f": {links}.")
    if others:
        text += " Listed but not offered: " + "; ".join(
            f"[{esc(fw.title)}]({page_slug(fw.id)}) ({fw.instrument_status})" for fw in others) + "."
    return text


BLOCKS: Dict[str, Callable[[], str]] = {
    "framework-status": block_framework_status,
    "offered-summary": block_offered_summary,
}

_BLOCK_RE = re.compile(
    r"(\{/\* BEGIN generated:(?P<name>[a-z-]+)[^*]*\*/\}\n)(?P<body>.*?)(\{/\* END generated:(?P=name) \*/\})",
    re.S,
)


def fill_blocks(text: str) -> str:
    def repl(m: re.Match) -> str:
        name = m.group("name")
        if name not in BLOCKS:
            raise SystemExit(f"unknown generated block {name!r}")
        return m.group(1) + BLOCKS[name]() + "\n" + m.group(4)

    return _BLOCK_RE.sub(repl, text)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def expected_files(docs: Path) -> Dict[Path, str]:
    out: Dict[Path, str] = {}
    for fid, fw in CATALOGS.items():
        out[docs / PAGES_DIR / f"{fid}.mdx"] = render_page(fw)
    for path in sorted((docs / CONTENT_DIR).rglob("*.mdx")):
        if path in out:
            continue
        text = path.read_text(encoding="utf-8")
        if "BEGIN generated:" in text:
            out[path] = fill_blocks(text)
    return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--docs", type=Path, default=DEFAULT_DOCS, help="brutor-ai-docs checkout")
    ap.add_argument("--check", action="store_true", help="exit 1 when a page differs from the catalogs")
    args = ap.parse_args(argv)
    docs: Path = args.docs.resolve()
    if not (docs / PAGES_DIR).is_dir():
        print(f"docs checkout not found: {docs / PAGES_DIR}", file=sys.stderr)
        return 2
    files = expected_files(docs)
    stale = sorted(p for p in (docs / PAGES_DIR).glob("*.mdx") if p not in files)
    drift = [p for p, body in files.items() if not p.exists() or p.read_text(encoding="utf-8") != body]
    if args.check:
        for p in drift:
            print(f"out of date: {p.relative_to(docs)}")
        for p in stale:
            print(f"no catalog for page: {p.relative_to(docs)}")
        if drift or stale:
            print("run: .venv-test/bin/python scripts/gen_framework_docs.py", file=sys.stderr)
            return 1
        print(f"framework docs in sync ({len(files)} files)")
        return 0
    for p in drift:
        p.write_text(files[p], encoding="utf-8")
        print(f"wrote {p.relative_to(docs)}")
    for p in stale:
        print(f"warning: no catalog for {p.relative_to(docs)} (left in place)", file=sys.stderr)
    print(f"{len(drift)} of {len(files)} files changed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
