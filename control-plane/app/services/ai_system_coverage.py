# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Coverage attestation — the population proof under every assurance claim.

Every other section of the Assurance Report answers "did the traffic we saw
behave?". None of them answers the prior question: **did we see the traffic?**

That gap is the one an auditor opens with. A report can be immaculate about
340,000 governed calls and still be worthless if the team also runs an
ungoverned side channel, because the report never claimed to be complete — it
just never said so either. Coverage attestation says so, in the report, with
numbers.

## The rule that makes this section honest

**Silence is never full coverage.** If no collector has ever reported, the
attestation is `unmeasured` — not 100%. This is the population-level form of
the platform's standing rule that absence of evidence is never green, and it
is the single most important line in this module: a tenant that has simply
never installed discovery must not be handed a full-marks coverage score it
did nothing to earn. That number would be the most misleading figure in the
entire report, precisely because it looks like the best one.

## Dismissals are disclosed, not hidden

An operator can raise the coverage percentage by dismissing assets. That is a
legitimate action — most estates contain genuinely irrelevant findings — but
it moves the denominator, so the count of dismissed assets is reported
alongside the percentage rather than silently subtracted from it. A reviewer
who wants to audit the dismissals can; one who does not at least knows they
happened.

## What "governed" means here

`DiscoveredAsset.governance_status` is reconciled by the platform against the
config tables, never taken from the collector's advisory hint. So `governed`
means "we found a matching configured route", not "the collector believed it
was fine".
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database.models.discovery import (
    GOV_GOVERNED,
    GOV_UNGOVERNED,
    GOV_UNKNOWN,
    STATUS_DISMISSED,
    CollectorKey,
    DiscoveredAsset,
)

# --- attestation bases ------------------------------------------------------
# Ordered worst-to-best; `_worst` picks the weakest that applies, matching the
# health verdict's worst-of discipline rather than averaging them.
BASIS_UNMEASURED = "unmeasured"  # no collector has ever reported
BASIS_STALE = "stale"  # collectors exist but have gone quiet
BASIS_MEASURED = "measured"  # at least one collector reported recently

_BASIS_RANK = {BASIS_UNMEASURED: 0, BASIS_STALE: 1, BASIS_MEASURED: 2}

# A collector that has not reported in this long is not evidence of the
# estate's *current* shape. Deliberately generous: network collectors run on
# scan schedules, and a day-old inventory is still a real inventory.
STALE_AFTER_DAYS = 7

# The three observation planes discovery can cover. Named because a coverage
# number computed over one plane says nothing about the others, and the most
# common real gap is a fully-instrumented network with zero endpoint
# visibility — laptops running local models nobody routes.
PLANE_NETWORK = "network"
PLANE_ENDPOINT = "endpoint"
PLANE_IMPORT = "import"

# Which asset types evidence which plane.
_ENDPOINT_TYPES = frozenset({"local_ai_client", "local_mcp_server"})


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    """Normalize for comparison: naive (SQLite) → UTC."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _worst(*bases: str) -> str:
    return min(bases, key=lambda b: _BASIS_RANK.get(b, 0))


def attest_coverage(
    db: Session,
    tenant_id: str,
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Attest what fraction of the discovered AI estate is routed.

    Returns the numbers *and* the basis on which they were computed, because
    "97% governed" and "97% governed, from a collector that stopped reporting
    six weeks ago" are different claims and only one of them is worth acting
    on.
    """
    now = now or datetime.now(timezone.utc)

    # --- did anyone actually look? -----------------------------------------
    keys: List[CollectorKey] = (
        db.query(CollectorKey).filter(CollectorKey.tenant_id == tenant_id).all()
    )
    active_keys = [k for k in keys if k.is_active]
    last_report = max(
        (_aware(k.last_used_at) for k in keys if k.last_used_at is not None),
        default=None,
    )

    if last_report is None:
        basis = BASIS_UNMEASURED
    elif now - last_report > timedelta(days=STALE_AFTER_DAYS):
        basis = BASIS_STALE
    else:
        basis = BASIS_MEASURED

    # --- the estate, by reconciled governance status ------------------------
    # GROUP BY rather than loading rows: an estate can be large and this runs
    # on every report build.
    counts_q = (
        db.query(DiscoveredAsset.governance_status, func.count(DiscoveredAsset.id))
        .filter(
            DiscoveredAsset.tenant_id == tenant_id,
            DiscoveredAsset.status != STATUS_DISMISSED,
        )
        .group_by(DiscoveredAsset.governance_status)
        .all()
    )
    counts = {status: int(n) for status, n in counts_q}
    governed = counts.get(GOV_GOVERNED, 0)
    ungoverned = counts.get(GOV_UNGOVERNED, 0)
    unknown = counts.get(GOV_UNKNOWN, 0)
    total = governed + ungoverned + unknown

    dismissed = int(
        db.query(func.count(DiscoveredAsset.id))
        .filter(
            DiscoveredAsset.tenant_id == tenant_id,
            DiscoveredAsset.status == STATUS_DISMISSED,
        )
        .scalar()
        or 0
    )

    # --- the percentage, or an honest refusal to give one -------------------
    # `unknown` counts against coverage. An asset we could not reconcile is
    # not a routed asset; folding it into the numerator would let an
    # unreliable reconcile pass inflate the headline figure.
    if basis == BASIS_UNMEASURED or total == 0:
        coverage_pct: Optional[float] = None
    else:
        coverage_pct = governed / total

    # --- which planes were actually observed --------------------------------
    observed_types = {
        t
        for (t,) in db.query(DiscoveredAsset.asset_type)
        .filter(
            DiscoveredAsset.tenant_id == tenant_id,
            DiscoveredAsset.status != STATUS_DISMISSED,
        )
        .distinct()
        .all()
    }
    planes_observed = sorted(
        {
            PLANE_ENDPOINT if t in _ENDPOINT_TYPES else PLANE_NETWORK
            for t in observed_types
        }
    )

    result: Dict[str, Any] = {
        "basis": basis,
        "coverage_pct": coverage_pct,
        "governed": governed,
        "ungoverned": ungoverned,
        "unknown": unknown,
        "total_discovered": total,
        "dismissed": dismissed,
        "collectors_registered": len(keys),
        "collectors_active": len(active_keys),
        "last_report_at": last_report.isoformat() if last_report else None,
        "planes_observed": planes_observed,
        "endpoint_plane_observed": PLANE_ENDPOINT in planes_observed,
    }
    result["statement"] = _statement(result)
    result["gaps"] = coverage_gaps(result)
    return result


def _statement(c: Dict[str, Any]) -> str:
    """One sentence an auditor can quote. Says what is covered *and* what the
    number rests on, because a percentage without its basis is a rumour."""
    if c["basis"] == BASIS_UNMEASURED:
        return (
            "Coverage is UNMEASURED. No discovery collector has ever reported "
            "for this tenant, so the platform cannot state what fraction of "
            "AI activity traverses the gateway. This report evidences the "
            "traffic that was routed; it makes no claim about traffic that "
            "was not."
        )
    if c["total_discovered"] == 0:
        return (
            "Discovery is active but has reported no AI assets, so no "
            "coverage ratio can be computed. This report evidences routed "
            "traffic only."
        )
    pct = c["coverage_pct"]
    stale = (
        " The most recent collector report is more than "
        f"{STALE_AFTER_DAYS} days old, so this reflects the estate as it was "
        "last observed, not as it is now."
        if c["basis"] == BASIS_STALE
        else ""
    )
    return (
        f"{pct:.0%} of the {c['total_discovered']} discovered AI assets are "
        f"routed through the gateway and therefore in scope for this report. "
        f"{c['ungoverned'] + c['unknown']} are not, and nothing in this "
        f"report evidences their behaviour.{stale}"
    )


def coverage_gaps(c: Dict[str, Any]) -> List[str]:
    """Coverage lines for the report's `not_covered` section.

    Separate from `_statement` so the report can carry both the headline and
    the itemised gaps without re-deriving either.
    """
    gaps: List[str] = []
    if c["basis"] == BASIS_UNMEASURED:
        gaps.append(
            "Coverage is unmeasured: no discovery collector has reported, so "
            "the platform cannot say whether ungoverned AI activity exists "
            "alongside the traffic evidenced here."
        )
        return gaps

    if c["basis"] == BASIS_STALE:
        gaps.append(
            f"The discovered-estate inventory is stale (no collector report "
            f"in over {STALE_AFTER_DAYS} days); assets added since then are "
            f"invisible to this attestation."
        )
    unrouted = c["ungoverned"] + c["unknown"]
    if unrouted:
        gaps.append(
            f"{unrouted} discovered AI asset(s) are not routed through the "
            f"gateway ({c['ungoverned']} confirmed ungoverned, "
            f"{c['unknown']} unreconciled). No behaviour, cost or policy "
            f"evidence exists for them."
        )
    if c["dismissed"]:
        gaps.append(
            f"{c['dismissed']} discovered asset(s) were dismissed by an "
            f"operator and excluded from the coverage denominator."
        )
    if not c["endpoint_plane_observed"]:
        gaps.append(
            "No endpoint-plane assets have been observed. Locally-run models "
            "and desktop MCP servers would not appear in this attestation "
            "unless an endpoint collector is deployed."
        )
    return gaps
