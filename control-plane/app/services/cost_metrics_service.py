# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
FinOps cost-attribution service for Mission Control's Cost tab.

Reads the unified `usage_logs` ledger (governed inline + observed imported
rows share it) and produces:
  * cost/token breakdowns by provider / model / team (resource group) / user
  * the Governed vs Observed plane split
  * per-resource-group budget burn-down vs `llm_global_limits.budget`, with a
    naive month-end run-rate projection

usage_logs is the right source here (proxy_metrics lacks provider / resource
group / user / source-as-column dimensions). The plane filter maps onto
`usage_logs.source`: 'gateway' (governed) vs 'import:%' (observed).
"""

from __future__ import annotations

import calendar
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.core.logging_system import BrutorLogger
from app.database.models.end_user import EndUser
from app.database.models.resource_group import ResourceGroup
from app.database.models.usage_log import UsageLog
from app.services.metrics_window import group_id_filter, previous_window, resolve_window

PLANE_GOVERNED = "governed"
PLANE_OBSERVED = "observed"


def _apply_plane(query, plane: Optional[str]):
    """Filter a usage_logs query by provenance plane. governed => source
    'gateway'; observed => source like 'import:%'; all/None => no filter."""
    if not plane or plane.lower() == "all":
        return query
    p = plane.lower()
    if p == PLANE_GOVERNED:
        return query.filter(UsageLog.source == "gateway")
    if p == PLANE_OBSERVED:
        return query.filter(UsageLog.source.like("import:%"))
    return query


class CostMetricsService:
    """Read-only FinOps aggregations over usage_logs."""

    def __init__(self, logger: Optional[BrutorLogger] = None):
        self.logger = logger or BrutorLogger("cost-metrics-service")

    # ------------------------------------------------------------------ #
    # Cost breakdown
    # ------------------------------------------------------------------ #
    def breakdown(
        self,
        db: Session,
        tenant_id: str,
        period_hours: int = 720,
        plane: Optional[str] = None,
        top_n: int = 10,
        group_id: Optional[str] = None,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        compare: bool = False,
    ) -> Dict[str, Any]:
        """Cost + tokens grouped by provider / model / team / user, plus totals
        and the governed-vs-observed split.

        When ``group_id`` is set, every figure is scoped to the requests
        attributed to that Resource Group (Design Note 002: an AI System is just a
        Resource Group with group_type='ai_system', so per-system scoping is
        per-RG scoping).

        ``start``/``end`` select an explicit calendar window (overrides
        ``period_hours``). With ``compare=True`` the response additionally
        carries the equal-length preceding window — totals plus per-row
        previous cost/requests — so the UI can answer "what changed?"
        without a second round-trip. All-time windows get no comparison:
        there is no "previous everything"."""
        window_start, window_end = resolve_window(period_hours, start, end)

        def _base(ws, we) -> List[Any]:
            conds = [UsageLog.tenant_id == tenant_id]
            if ws is not None:
                conds.append(UsageLog.created_at >= ws)
            if we is not None:
                conds.append(UsageLog.created_at < we)
            gf = group_id_filter(UsageLog.group_id, group_id)
            if gf is not None:
                conds.append(gf)
            return conds

        base = _base(window_start, window_end)

        cost = func.coalesce(func.sum(UsageLog.estimated_cost), 0.0)
        toks = func.coalesce(func.sum(UsageLog.total_tokens), 0)
        reqs = func.count(UsageLog.id)

        def _rows(group_cols, extra_filters=None, join_user=False, join_group=False) -> List[Any]:
            q = db.query(*group_cols, cost, toks, reqs)
            if join_user:
                q = q.outerjoin(EndUser, EndUser.id == UsageLog.user_id)
            if join_group:
                q = q.outerjoin(ResourceGroup, ResourceGroup.id == UsageLog.group_id)
            q = q.filter(*base)
            for f in (extra_filters or []):
                q = q.filter(f)
            q = _apply_plane(q, plane)
            q = q.group_by(*group_cols).order_by(cost.desc()).limit(top_n)
            return q.all()

        by_provider = [
            {"key": r[0], "cost": float(r[-3]), "tokens": int(r[-2]), "requests": int(r[-1])}
            for r in _rows([UsageLog.provider], [UsageLog.provider.isnot(None)])
        ]
        by_model = [
            {"key": r[0], "cost": float(r[-3]), "tokens": int(r[-2]), "requests": int(r[-1])}
            for r in _rows([UsageLog.model_name], [UsageLog.model_name.isnot(None)])
        ]
        # Team = resource group. Prefer the RG's authoritative current name
        # (display_name → name) joined live, then the denormalised
        # usage_logs.group_name, then the id. Imported usage rows don't carry
        # group_name, so the join is what makes "By Team" show real names.
        by_team = [
            {"key": r[0], "name": (r[1] or r[2] or r[3] or r[0]),
             "cost": float(r[-3]), "tokens": int(r[-2]), "requests": int(r[-1])}
            for r in _rows(
                [UsageLog.group_id, ResourceGroup.display_name,
                 ResourceGroup.name, UsageLog.group_name],
                [UsageLog.group_id.isnot(None)],
                join_group=True,
            )
        ]
        by_user = [
            {"key": r[0], "name": (r[1] or r[2] or f"user:{r[0]}"),
             "cost": float(r[-3]), "tokens": int(r[-2]), "requests": int(r[-1])}
            for r in _rows([UsageLog.user_id, EndUser.username, EndUser.email],
                           [UsageLog.user_id.isnot(None)], join_user=True)
        ]
        # Totals for the selected plane.
        tq = _apply_plane(db.query(cost, toks, reqs).filter(*base), plane).one()
        totals = {"cost": float(tq[0]), "tokens": int(tq[1]), "requests": int(tq[2])}

        # Governed vs Observed split (always both, regardless of the selected
        # plane — this is the two-plane hero).
        def _plane_totals(p: str) -> Dict[str, Any]:
            r = _apply_plane(db.query(cost, toks, reqs).filter(*base), p).one()
            return {"cost": float(r[0]), "tokens": int(r[1]), "requests": int(r[2])}

        by_source = {
            "governed": _plane_totals(PLANE_GOVERNED),
            "observed": _plane_totals(PLANE_OBSERVED),
        }

        # ── The preceding equal-length window (compare=True) ─────────────
        # Per-dimension previous values are fetched only for the keys the
        # current window surfaced (IN-list, one grouped query per
        # dimension): the question is "how did MY top rows move", not a
        # second full top-N that may rank differently.
        previous: Optional[Dict[str, Any]] = None
        if compare:
            prev_start, prev_end = previous_window(window_start, window_end)
            if prev_start is not None:
                prev_base = _base(prev_start, prev_end)

                pt = _apply_plane(db.query(cost, toks, reqs).filter(*prev_base), plane).one()

                def _prev_map(col, keys, join_user=False, join_group=False) -> Dict[Any, Dict[str, Any]]:
                    if not keys:
                        return {}
                    q = db.query(col, cost, reqs)
                    if join_user:
                        q = q.outerjoin(EndUser, EndUser.id == UsageLog.user_id)
                    if join_group:
                        q = q.outerjoin(ResourceGroup, ResourceGroup.id == UsageLog.group_id)
                    q = q.filter(*prev_base).filter(col.in_(keys))
                    q = _apply_plane(q, plane).group_by(col)
                    return {
                        r[0]: {"cost": float(r[1]), "requests": int(r[2])}
                        for r in q.all()
                    }

                dims = (
                    ("by_provider", UsageLog.provider, by_provider, {}),
                    ("by_model", UsageLog.model_name, by_model, {}),
                    ("by_team", UsageLog.group_id, by_team, {"join_group": True}),
                    ("by_user", UsageLog.user_id, by_user, {"join_user": True}),
                )
                for _name, col, rows, kw in dims:
                    pm = _prev_map(col, [r["key"] for r in rows], **kw)
                    for r in rows:
                        p = pm.get(r["key"])
                        # 0.0, not None: a key absent from the previous
                        # window genuinely spent nothing then — that IS
                        # the delta ("new spend"), not missing data.
                        r["prev_cost"] = p["cost"] if p else 0.0
                        r["prev_requests"] = p["requests"] if p else 0

                previous = {
                    "start": prev_start.isoformat(),
                    "end": prev_end.isoformat() if prev_end else None,
                    "totals": {
                        "cost": float(pt[0]),
                        "tokens": int(pt[1]),
                        "requests": int(pt[2]),
                    },
                }

        return {
            "period_hours": period_hours,
            "window": {
                "start": window_start.isoformat() if window_start else None,
                "end": window_end.isoformat() if window_end else None,
            },
            "plane": plane or "all",
            "totals": totals,
            "by_source": by_source,
            "by_provider": by_provider,
            "by_model": by_model,
            "by_team": by_team,
            "by_user": by_user,
            "previous": previous,
            "currency": "USD",
        }

    # ------------------------------------------------------------------ #
    # Monthly LLM economics series
    # ------------------------------------------------------------------ #
    def monthly(
        self,
        db: Session,
        tenant_id: str,
        months: int = 12,
        plane: Optional[str] = None,
        group_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Per-calendar-month LLM cost/requests/tokens for the last
        ``months`` months, plus the derived unit cost per 1,000 requests.

        Feeds Mission Control's "LLM Economics" tab charts: reduction work
        (smart routing, semantic cache) shows up as a falling unit-cost
        line even while the volume bars keep growing — the one FinOps
        story a period-total can't tell. LLM rows only: media/MCP rows
        either bill in different units or carry no cost, and mixing them
        in would flatten the very signal this series exists to show.

        The month bucket is computed SQL-side. `to_char` exists only on
        PostgreSQL; unit tests run on SQLite, whose spelling is
        `strftime` — hence the explicit dialect switch rather than a
        portable-looking expression that would quietly break one of the
        two.
        """
        months = max(1, min(int(months or 12), 36))
        now = datetime.now(timezone.utc)
        # First day of the window: month_start minus (months-1) whole months.
        year = now.year
        month = now.month - (months - 1)
        while month <= 0:
            month += 12
            year -= 1
        window_start = datetime(year, month, 1, tzinfo=timezone.utc)

        dialect = db.get_bind().dialect.name
        if dialect == "postgresql":
            month_expr = func.to_char(UsageLog.created_at, "YYYY-MM")
        else:
            month_expr = func.strftime("%Y-%m", UsageLog.created_at)

        cost = func.coalesce(func.sum(UsageLog.estimated_cost), 0.0)
        toks = func.coalesce(func.sum(UsageLog.total_tokens), 0)
        reqs = func.count(UsageLog.id)

        q = db.query(month_expr.label("month"), cost, toks, reqs).filter(
            UsageLog.tenant_id == tenant_id,
            UsageLog.resource_type == "llm",
            UsageLog.created_at >= window_start,
        )
        gf = group_id_filter(UsageLog.group_id, group_id)
        if gf is not None:
            q = q.filter(gf)
        q = _apply_plane(q, plane)
        rows = q.group_by("month").order_by("month").all()

        data = [
            {
                "month": r[0],
                "cost_usd": round(float(r[1]), 6),
                "tokens": int(r[2]),
                "requests": int(r[3]),
                "unit_cost_per_1k_usd": round(float(r[1]) / int(r[3]) * 1000.0, 4)
                if int(r[3]) else 0.0,
            }
            for r in rows
        ]
        return {
            "months": months,
            "plane": plane or "all",
            "group_id": group_id,
            "currency": "USD",
            "data": data,
        }

    # ------------------------------------------------------------------ #
    # Cost composition series (stacked-area feed)
    # ------------------------------------------------------------------ #
    def series(
        self,
        db: Session,
        tenant_id: str,
        dimension: str = "provider",
        period_hours: int = 2160,
        plane: Optional[str] = None,
        group_id: Optional[str] = None,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        top_n: int = 5,
    ) -> Dict[str, Any]:
        """Daily cost per dimension value (provider / model / team) — the
        feed for the Cost tab's composition-over-time area chart.

        Always DAILY buckets: a year is ≤366 points per key, a trivial
        payload, and the client rolls days up to weeks/months with the
        same grain rule every other dashboard uses — putting the rollup
        in SQL would just duplicate that rule in a second language.
        Keys beyond the window's top ``top_n`` collapse into ``"other"``
        so the chart's band count stays readable and the bands still sum
        to the real total (a top-N chart that silently drops the tail
        reads as "covered everything" when it didn't).
        """
        col = {
            "provider": UsageLog.provider,
            "model": UsageLog.model_name,
            "team": UsageLog.group_id,
        }.get(dimension)
        if col is None:
            raise ValueError(f"unknown dimension: {dimension!r}")

        window_start, window_end = resolve_window(period_hours, start, end)

        conds = [UsageLog.tenant_id == tenant_id, col.isnot(None)]
        if window_start is not None:
            conds.append(UsageLog.created_at >= window_start)
        if window_end is not None:
            conds.append(UsageLog.created_at < window_end)
        gf = group_id_filter(UsageLog.group_id, group_id)
        if gf is not None:
            conds.append(gf)

        cost = func.coalesce(func.sum(UsageLog.estimated_cost), 0.0)
        reqs = func.count(UsageLog.id)

        # Top keys by window cost — everything else becomes "other".
        top_q = _apply_plane(db.query(col, cost).filter(*conds), plane)
        top_keys = [
            r[0] for r in top_q.group_by(col).order_by(cost.desc()).limit(top_n).all()
        ]

        dialect = db.get_bind().dialect.name
        if dialect == "postgresql":
            day_expr = func.to_char(UsageLog.created_at, "YYYY-MM-DD")
        else:
            day_expr = func.strftime("%Y-%m-%d", UsageLog.created_at)

        q = _apply_plane(
            db.query(day_expr.label("day"), col, cost, reqs).filter(*conds), plane
        ).group_by("day", col).order_by("day")

        points: Dict[str, Dict[str, Dict[str, float]]] = {}
        for day, key, c, n in q.all():
            slot = points.setdefault(day, {})
            k = key if key in top_keys else "other"
            agg = slot.setdefault(k, {"cost": 0.0, "requests": 0})
            agg["cost"] += float(c)
            agg["requests"] += int(n)

        # Team keys are RG ids — resolve display names once.
        names: Dict[str, str] = {}
        if dimension == "team" and top_keys:
            for gid, disp, name in (
                db.query(ResourceGroup.id, ResourceGroup.display_name, ResourceGroup.name)
                .filter(ResourceGroup.id.in_(top_keys))
                .all()
            ):
                names[gid] = disp or name or gid

        return {
            "dimension": dimension,
            "grain": "day",
            "plane": plane or "all",
            "keys": [
                {"key": k, "name": names.get(k, k)} for k in top_keys
            ] + ([{"key": "other", "name": "Other"}] if any(
                "other" in day_vals for day_vals in points.values()
            ) else []),
            "window": {
                "start": window_start.isoformat() if window_start else None,
                "end": window_end.isoformat() if window_end else None,
            },
            "data": [
                {
                    "date": day,
                    "values": {
                        k: {"cost": round(v["cost"], 6), "requests": v["requests"]}
                        for k, v in day_vals.items()
                    },
                }
                for day, day_vals in sorted(points.items())
            ],
            "currency": "USD",
        }

    # ------------------------------------------------------------------ #
    # Savings — cost avoided by the reducers
    # ------------------------------------------------------------------ #
    def savings(
        self,
        db: Session,
        tenant_id: str,
        period_hours: int = 2160,
        group_id: Optional[str] = None,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """What the reducers avoided spending, honestly labelled.

        Two components, deliberately kept apart because their evidence
        differs in kind:

          * **Gateway cache** (response + semantic): hits never reached a
            provider, so their avoided cost is an *estimate* — hits × the
            window's average cost of a comparable non-cached LLM request.
            The basis is returned so the arithmetic is checkable.
          * **Provider prompt cache**: the provider's own reported
            discount, summed from the per-request ledger — *measured*,
            not estimated.

        Governed traffic only (proxy_logs is the inline plane); imported
        telemetry carries no cache signals to reason from.
        """
        from app.database.models.proxy_log import ProxyLog

        window_start, window_end = resolve_window(period_hours, start, end)

        conds = [ProxyLog.tenant_id == tenant_id, ProxyLog.proxy_type == "llm"]
        if window_start is not None:
            conds.append(ProxyLog.timestamp >= window_start)
        if window_end is not None:
            conds.append(ProxyLog.timestamp < window_end)
        gf = group_id_filter(ProxyLog.resource_group_id, group_id)
        if gf is not None:
            conds.append(gf)

        hit = ProxyLog.llm_cache_hit.is_(True)
        cost = func.coalesce(ProxyLog.llm_estimated_cost_usd, 0.0)

        row = (
            db.query(
                func.count(ProxyLog.id),                                     # 0 llm reqs
                func.sum(case((hit, 1), else_=0)),                           # 1 cache hits
                func.coalesce(func.sum(case((~hit, cost), else_=0.0)), 0.0),  # 2 non-cached cost
                func.sum(case((~hit, 1), else_=0)),                          # 3 non-cached reqs
                func.coalesce(func.sum(func.coalesce(ProxyLog.llm_tokens_saved, 0)), 0),   # 4
                func.coalesce(func.sum(func.coalesce(ProxyLog.prompt_cache_savings_usd, 0.0)), 0.0),  # 5
            )
            .filter(*conds)
            .one()
        )

        total_llm = int(row[0] or 0)
        hits = int(row[1] or 0)
        non_cached_cost = float(row[2] or 0.0)
        non_cached_reqs = int(row[3] or 0)
        tokens_saved = int(row[4] or 0)
        prompt_cache_usd = float(row[5] or 0.0)

        avg_noncached = (non_cached_cost / non_cached_reqs) if non_cached_reqs else None
        cache_avoided = (hits * avg_noncached) if (hits and avg_noncached is not None) else 0.0

        return {
            "window": {
                "start": window_start.isoformat() if window_start else None,
                "end": window_end.isoformat() if window_end else None,
            },
            "plane": "governed",
            "llm_requests": total_llm,
            "gateway_cache": {
                "hits": hits,
                "hit_rate": round(hits / total_llm, 4) if total_llm else None,
                "tokens_saved": tokens_saved,
                "estimated_avoided_usd": round(cache_avoided, 4),
                # The basis makes the estimate auditable: hits × this number.
                "basis_avg_cost_per_noncached_request_usd": (
                    round(avg_noncached, 6) if avg_noncached is not None else None
                ),
                "estimated": True,
            },
            "provider_prompt_cache": {
                "savings_usd": round(prompt_cache_usd, 4),
                "measured": True,
            },
            "total_avoided_usd": round(cache_avoided + prompt_cache_usd, 4),
            "currency": "USD",
        }


    # ------------------------------------------------------------------ #
    # FOCUS export — the FinOps Open Cost & Usage Specification
    # ------------------------------------------------------------------ #
    #: FOCUS 1.x column subset this export conforms to, in spec casing.
    #: Deliberately a documented SUBSET: every column we emit follows the
    #: spec's semantics; columns we cannot answer truthfully (SKU pricing,
    #: region, commitment discounts) are omitted rather than filled with
    #: guesses — FOCUS consumers treat missing columns as "not provided",
    #: which is honest, where fabricated values would not be.
    FOCUS_COLUMNS = [
        "BilledCost", "EffectiveCost", "BillingAccountId", "BillingAccountName",
        "BillingCurrency", "BillingPeriodStart", "BillingPeriodEnd",
        "ChargePeriodStart", "ChargePeriodEnd", "ChargeCategory",
        "ChargeDescription", "ConsumedQuantity", "ConsumedUnit",
        "InvoiceIssuerName", "ProviderName", "PublisherName",
        "ResourceId", "ResourceName", "ResourceType",
        "ServiceCategory", "ServiceName", "SubAccountId", "SubAccountName",
        "Tags",
    ]

    def focus_rows(
        self,
        db: Session,
        tenant_id: str,
        period_hours: int = 720,
        plane: Optional[str] = None,
        group_id: Optional[str] = None,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ):
        """Daily-grain cost rows mapped onto FOCUS columns.

        One row per (day × provider × model × resource group × resource
        type): the granularity every FinOps platform ingests comfortably,
        while a year of raw per-request rows (~350k+) would be an
        anti-social default. Mapping:

          BilledCost/EffectiveCost  = summed estimated_cost (we meter one
                                      effective price; no separate billed
                                      vs amortised view exists here)
          BillingAccount            = the Brutor tenant
          SubAccount                = the resource group (AI System / team)
          Provider/Publisher/Issuer = the upstream model provider
          Resource                  = the model
          ConsumedQuantity/Unit     = tokens (LLM) or calls (other types)
          ChargePeriod              = the UTC day; BillingPeriod = its month
          Tags                      = provenance plane (governed/observed)

        Yields dicts keyed by FOCUS_COLUMNS.
        """
        import calendar as _cal
        import json as _json

        window_start, window_end = resolve_window(period_hours, start, end)

        conds = [UsageLog.tenant_id == tenant_id]
        if window_start is not None:
            conds.append(UsageLog.created_at >= window_start)
        if window_end is not None:
            conds.append(UsageLog.created_at < window_end)
        gf = group_id_filter(UsageLog.group_id, group_id)
        if gf is not None:
            conds.append(gf)

        dialect = db.get_bind().dialect.name
        if dialect == "postgresql":
            day_expr = func.to_char(UsageLog.created_at, "YYYY-MM-DD")
        else:
            day_expr = func.strftime("%Y-%m-%d", UsageLog.created_at)

        cost = func.coalesce(func.sum(UsageLog.estimated_cost), 0.0)
        toks = func.coalesce(func.sum(UsageLog.total_tokens), 0)
        reqs = func.count(UsageLog.id)
        governed = func.sum(case((UsageLog.source == "gateway", 1), else_=0))

        q = (
            db.query(
                day_expr.label("day"),
                UsageLog.provider,
                UsageLog.model_name,
                UsageLog.resource_type,
                UsageLog.group_id,
                func.coalesce(ResourceGroup.display_name, ResourceGroup.name,
                              UsageLog.group_name).label("group_label"),
                cost, toks, reqs, governed,
            )
            .outerjoin(ResourceGroup, ResourceGroup.id == UsageLog.group_id)
            .filter(*conds)
        )
        q = _apply_plane(q, plane)
        q = q.group_by(
            "day", UsageLog.provider, UsageLog.model_name,
            UsageLog.resource_type, UsageLog.group_id, "group_label",
        ).order_by("day")

        for day, provider, model, rtype, gid, glabel, c, t, n, gov in q.yield_per(1000):
            y, m, d = (int(x) for x in day.split("-"))
            month_end_day = _cal.monthrange(y, m)[1]
            is_llm = (rtype or "llm") == "llm"
            plane_tag = ("governed" if int(gov or 0) == int(n) else
                         "observed" if int(gov or 0) == 0 else "mixed")
            yield {
                "BilledCost": round(float(c), 6),
                "EffectiveCost": round(float(c), 6),
                "BillingAccountId": tenant_id,
                "BillingAccountName": tenant_id,
                "BillingCurrency": "USD",
                "BillingPeriodStart": f"{y:04d}-{m:02d}-01T00:00:00Z",
                "BillingPeriodEnd": f"{y:04d}-{m:02d}-{month_end_day:02d}T23:59:59Z",
                "ChargePeriodStart": f"{day}T00:00:00Z",
                "ChargePeriodEnd": f"{day}T23:59:59Z",
                "ChargeCategory": "Usage",
                "ChargeDescription": (
                    f"{model or rtype or 'usage'} via Brutor Agent Control"
                ),
                "ConsumedQuantity": int(t) if is_llm else int(n),
                "ConsumedUnit": "tokens" if is_llm else "calls",
                "InvoiceIssuerName": provider or "unknown",
                "ProviderName": provider or "unknown",
                "PublisherName": provider or "unknown",
                "ResourceId": model or (rtype or "unknown"),
                "ResourceName": model or (rtype or "unknown"),
                "ResourceType": rtype or "llm",
                "ServiceCategory": "AI and Machine Learning",
                "ServiceName": f"{provider or 'unknown'} ({rtype or 'llm'})",
                "SubAccountId": gid or "",
                "SubAccountName": glabel or "",
                "Tags": _json.dumps({"brutor/plane": plane_tag}),
            }


    # ------------------------------------------------------------------ #
    # Budget burn-down + projection
    # ------------------------------------------------------------------ #
    def budgets(self, db: Session, tenant_id: str) -> Dict[str, Any]:
        """Per-resource-group monthly/daily budget burn-down vs
        `llm_global_limits.budget`, with a naive run-rate projection. Mirrors
        the advisory budget-alert math (subtree spend over usage_logs) but is
        read-only and returns every RG that carries a budget."""
        now = datetime.now(timezone.utc)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        days_in_month = calendar.monthrange(now.year, now.month)[1]
        # Fractional days elapsed (>=1 hour floor so day-1 doesn't divide by ~0).
        days_elapsed = max((now - month_start).total_seconds() / 86400.0, 1.0 / 24)
        hours_elapsed = max((now - day_start).total_seconds() / 3600.0, 1.0 / 60)

        groups = (
            db.query(ResourceGroup)
            .filter(
                ResourceGroup.tenant_id == tenant_id,
                ResourceGroup.llm_global_limits.isnot(None),
            )
            .all()
        )

        items: List[Dict[str, Any]] = []
        for rg in groups:
            # llm_global_limits is an untyped JSON column; besides a proper
            # object it has been observed holding JSON null and even an
            # array (a `null::jsonb || object` concat produces one). One
            # malformed group must degrade to "no budget", not 500 the
            # whole burn-down panel.
            limits = rg.llm_global_limits
            if not isinstance(limits, dict):
                continue
            budget = (limits.get("budget") or {})
            if not isinstance(budget, dict):
                continue
            for period, period_start, limit_key, warn_key, projected in (
                ("monthly", month_start, "monthly_limit_usd",
                 "monthly_warning_percent", True),
                ("daily", day_start, "daily_limit_usd",
                 "daily_warning_percent", False),
            ):
                limit = budget.get(limit_key)
                if not limit or float(limit) <= 0:
                    continue
                limit = float(limit)
                warn = int(budget.get(warn_key) or 80)
                spend = self._subtree_spend(db, tenant_id, rg.hierarchy_path, period_start)
                util = (spend / limit) * 100.0 if limit else 0.0
                if projected:
                    proj = spend / days_elapsed * days_in_month
                else:
                    proj = spend / hours_elapsed * 24.0
                items.append({
                    "group_id": rg.id,
                    "group_name": rg.name,
                    "period": period,
                    "limit_usd": round(limit, 2),
                    "spend_usd": round(spend, 2),
                    "utilization_pct": round(util, 1),
                    "warning_pct": warn,
                    "over_warning": util >= warn,
                    "projected_usd": round(proj, 2),
                    "projected_over": proj > limit,
                })

        items.sort(key=lambda i: i["utilization_pct"], reverse=True)
        return {"budgets": items, "currency": "USD"}

    def _subtree_spend(
        self, db: Session, tenant_id: str, hpath: Optional[str], since: datetime
    ) -> float:
        """Current-period spend over an RG subtree (the RG + descendants),
        matched via usage_logs.ancestry_path. Mirrors the advisory budget
        evaluator so Cost-tab burn-down and the alerts agree."""
        if not hpath:
            return 0.0
        total = (
            db.query(func.coalesce(func.sum(UsageLog.estimated_cost), 0.0))
            .filter(
                UsageLog.tenant_id == tenant_id,
                UsageLog.created_at >= since,
                (UsageLog.ancestry_path == hpath)
                | (UsageLog.ancestry_path.like(hpath + "/%")),
            )
            .scalar()
        )
        return float(total or 0.0)
