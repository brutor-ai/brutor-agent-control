# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Descriptive signals over the run ledger

Four signals, all descriptive. They report what an AI System did; they
do not judge whether that was normal (that needs a baseline, which drift
detection learns). Everything here is a count, a sum or a
percentile an operator could recompute by hand.

Two honesty rules are enforced in code rather than left to the UI:

  * **Exhaustion is unknown until it is reported.** The proxy cannot see a
    framework's max-iteration cap, so a 0% exhaustion rate with no
    client-reported runs means "we don't know", not "healthy". The signal
    carries `exhaustion_reporting_available` so the UI can say so.

  * **The work is the population**. Every per-run average and
    percentile — cost per processed outcome, run length — is over `success`
    runs (THE classification, `ai_system_outcomes`), overall and per
    declared outcome / variant. An empty poll is *no-work overhead* (count
    and cost), never averaged into the work; errored, exhausted and
    deviating runs are *spent, nothing produced*. A policy block is an
    errored run (cause `blocked_by_policy`) — there is no blocked bucket.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from sqlalchemy import and_, case, func, not_, or_

from app.services.metrics_window import resolve_window
from sqlalchemy.orm import Session

from app.api.v1.models.admin.ai_system_run import (
    CostPerRunSignal,
    RunStatsResponse,
    RunTimeseriesPoint,
    RunTimeseriesResponse,
    ActionCountSignal,
    ContextSignal,
    ActionDistributionBucket,
    TerminalStateRow,
    TerminalStateSignal,
    StepErrorKindRow,
    StepErrorSignal,
    ToolErrorRow,
    ToolErrorSignal,
    ErrorCauseRow,
    ErrorCauseSignal,
    OutcomeActionRow,
    OutcomeCostRow,
    OutcomeRow,
    OutcomeSignal,
)
from app.database.models.ai_system_run import (
    AISystemRun,
    CHAIN_CLIENT_ASSERTED,
    CHAIN_OBSERVED_EXTERNAL,
    CHAIN_INTACT,
    MAX_REOPENS_BEFORE_FRAGMENTED,
    TERMINAL_ABANDONED,
    TERMINAL_EXHAUSTED,
)
from app.services.ai_system_outcomes import (
    RUN_CLASSES,
    RUN_DEVIATION,
    RUN_FAILED,
    RUN_NOT_DECISIVE,
    RUN_NO_WORK,
    RUN_SUCCESS,
    class_cond,
    run_class_sql,
    success_rate,
    waste_cond,
)
from app.database.models.proxy_log import ProxyLog

# Step-count buckets. Chosen so the interesting range for agent runs
# (roughly 1–30 steps) has resolution, with a long open-ended tail for the
# runaway loops that matter most and are rarest.
STEP_BUCKETS: Sequence[Tuple[int, Optional[int]]] = (
    (1, 2),
    (3, 5),
    (6, 10),
    (11, 20),
    (21, 50),
    (51, 100),
    (101, None),
)


# ---------------------------------------------------------------------------
# Step kinds — the gateway's own vocabulary (gateway-core `step_tally_sql`)
# ---------------------------------------------------------------------------

STEP_MODEL = "model"
STEP_TOOL = "tool"
STEP_SKILL = "skill"
STEP_AGENT = "agent"
STEP_OTHER = "other"
#: Display order: the calls a run cannot do without first.
STEP_KINDS = (STEP_MODEL, STEP_TOOL, STEP_SKILL, STEP_AGENT, STEP_OTHER)
#: gateway-core `TOOL_PROXY_TYPES`.
_TOOL_PROXY_TYPES = ("mcp", "virtual-mcp")
_SKILL_RUN_SUBTYPE = "skill_run"
_APPROVAL_SUBTYPE = "approval_required"


def _step_kind():
    """The kind of one proxy_logs row, as the run ledger counts it: a skill
    run arrives as proxy_type 'skill' or as an MCP call with subtype
    'skill_run', and is a skill either way, never a tool."""
    subtype = func.coalesce(ProxyLog.proxy_subtype, "")
    return case(
        (ProxyLog.proxy_type == "llm", STEP_MODEL),
        (or_(ProxyLog.proxy_type == "skill", subtype == _SKILL_RUN_SUBTYPE), STEP_SKILL),
        (ProxyLog.proxy_type.in_(_TOOL_PROXY_TYPES), STEP_TOOL),
        (ProxyLog.proxy_type == "a2a", STEP_AGENT),
        else_=STEP_OTHER,
    )


def _step_failed():
    """The ledger's error: a failed call, except one held for a human
    (logged success = false, but governance working — not a failure)."""
    return and_(
        ProxyLog.success.is_(False),
        func.coalesce(ProxyLog.proxy_subtype, "") != _APPROVAL_SUBTYPE,
    )


def _run_steps_join(q):
    """Join a run query to its steps — the (tenant_id, root_task_id) index."""
    return q.select_from(AISystemRun).join(
        ProxyLog,
        and_(
            ProxyLog.tenant_id == AISystemRun.tenant_id,
            ProxyLog.root_task_id == AISystemRun.root_task_id,
        ),
    )


def _percentile_rank(n: int, pct: float) -> int:
    """Nearest-rank position (1-based) for a population of `n`.

    Deliberately not interpolating: an operator checking p95 against the
    raw list should find the value actually there, not one computed
    between two of them. The rank arithmetic (including Python's
    banker's rounding) is kept identical to the original in-memory
    implementation so historical numbers do not shift.
    """
    if n <= 1:
        return 1
    return max(1, min(n, int(round(pct * n + 0.5))))


def _rate(numerator: int, denominator: int) -> Optional[float]:
    """A rate, or None when the denominator is zero.

    None rather than 0.0 on purpose: "no runs" and "no failures" render
    identically as 0% and mean opposite things.
    """
    if denominator <= 0:
        return None
    return round(numerator / denominator, 6)


class RunSignalService:
    """Computes the descriptive signal set.

    Aggregation is pushed into SQL: a year of runs must never be
    materialised as ORM rows for what is arithmetically a handful of
    sums, counts and two percentiles (that pattern made Mission Control
    unusable at 340k proxy_logs before the same rewrite there). The only
    rows ever fetched are (a) percentile probes via ORDER BY + OFFSET —
    exact nearest-rank semantics, identical on PG and SQLite — and (b)
    the `tool_error_types` JSON of runs that actually had tool errors.
    """

    def compute(
        self,
        db: Session,
        tenant_id: str,
        *,
        ai_system_id: Optional[str] = None,
        period_hours: int = 24,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ) -> RunStatsResponse:
        now = datetime.now(timezone.utc)
        window_start, window_end = resolve_window(period_hours, start, end)

        # Replays are simulations against recorded traffic. They
        # must never move an operational number.
        conds = [
            AISystemRun.tenant_id == tenant_id,
            AISystemRun.is_replay.is_(False),
        ]
        if ai_system_id:
            conds.append(AISystemRun.ai_system_id == ai_system_id)
        if window_start is not None:
            conds.append(AISystemRun.started_at >= window_start)
        if window_end is not None:
            conds.append(AISystemRun.started_at < window_end)

        def _q(*cols):
            q = db.query(*cols)
            for c in conds:
                q = q.filter(c)
            return q

        def _one(cond) -> object:
            return func.sum(case((cond, 1), else_=0))

        cost = func.coalesce(AISystemRun.total_cost_usd, 0)
        tokens = func.coalesce(AISystemRun.total_tokens, 0)
        actions_col = func.coalesce(AISystemRun.action_count, 0)
        # THE classification (`ai_system_outcomes`): the work population is
        # `success`; no-work is overhead; failed + deviation is waste.
        work_cond = class_cond(RUN_SUCCESS)
        no_work_cond = class_cond(RUN_NO_WORK)
        wasted_cond = waste_cond()

        step_bucket_cols = [
            _one(
                and_(
                    work_cond,
                    and_(actions_col >= lower, actions_col <= upper)
                    if upper is not None
                    else actions_col >= lower,
                )
            )
            for lower, upper in STEP_BUCKETS
        ]

        agg = _q(
            func.count(AISystemRun.id),                                        # 0 total
            func.coalesce(func.sum(cost), 0),                                  # 1 total cost
            _one(work_cond),                                                   # 2 processed
            func.coalesce(func.sum(case((work_cond, cost), else_=0)), 0),      # 3 processed cost
            func.coalesce(func.sum(case((wasted_cond, cost), else_=0)), 0),    # 4 wasted cost
            func.coalesce(func.sum(tokens), 0),                                # 5 tokens
            func.coalesce(func.sum(case((work_cond, tokens), else_=0)), 0),    # 6 processed tokens
            func.coalesce(func.sum(func.coalesce(AISystemRun.tool_call_count, 0)), 0),   # 7
            func.coalesce(func.sum(func.coalesce(AISystemRun.tool_error_count, 0)), 0),  # 8
            _one(AISystemRun.terminal_state == TERMINAL_EXHAUSTED),            # 9
            _one(AISystemRun.terminal_state == TERMINAL_ABANDONED),            # 10
            _one(AISystemRun.finalized_by == "explicit"),                      # 11
            _one(AISystemRun.chain_integrity == CHAIN_INTACT),                 # 12
            _one(AISystemRun.chain_integrity == CHAIN_CLIENT_ASSERTED),        # 13
            _one(func.coalesce(AISystemRun.reopened_count, 0) > MAX_REOPENS_BEFORE_FRAGMENTED),  # 14
            func.max(case((work_cond, actions_col), else_=None)),              # 15 max work actions
            _one(AISystemRun.chain_integrity == CHAIN_OBSERVED_EXTERNAL),      # 16
            _one(no_work_cond),                                                # 17 no-work runs
            func.coalesce(func.sum(case((no_work_cond, cost), else_=0)), 0),   # 18 no-work cost
            *step_bucket_cols,                                                 # 19..
        ).one()
        bucket_base = 19

        total = int(agg[0] or 0)
        processed = int(agg[2] or 0)

        def _value_at(order_expr, offset: int, extra_cond=None) -> Optional[float]:
            """The value at a 0-based position in the ordered population —
            one indexed probe instead of materialising the population."""
            q = _q(order_expr)
            if extra_cond is not None:
                q = q.filter(extra_cond)
            row = q.order_by(order_expr.asc()).offset(offset).limit(1).first()
            return float(row[0]) if row is not None else None

        def _median_of(order_expr, n: int, extra_cond=None) -> Optional[float]:
            if n <= 0:
                return None
            if n % 2:
                return _value_at(order_expr, n // 2, extra_cond)
            lo = _value_at(order_expr, n // 2 - 1, extra_cond)
            hi = _value_at(order_expr, n // 2, extra_cond)
            if lo is None or hi is None:
                return None
            return (lo + hi) / 2

        def _pct_of(order_expr, n: int, pct: float, extra_cond=None) -> Optional[float]:
            if n <= 0:
                return None
            return _value_at(order_expr, _percentile_rank(n, pct) - 1, extra_cond)

        # Per processed outcome (full id): the work, never blended across
        # different kinds of work (Simpson's paradox).
        per_outcome = (
            _q(
                AISystemRun.outcome_id,
                func.count(AISystemRun.id),
                func.coalesce(func.sum(cost), 0),
                func.max(actions_col),
            )
            .filter(work_cond)
            .group_by(AISystemRun.outcome_id)
            .all()
        )
        cost_rows: List[OutcomeCostRow] = []
        action_rows: List[OutcomeActionRow] = []
        for outcome_id, n, subtotal, max_actions in sorted(
            per_outcome, key=lambda r: (-int(r[1]), r[0] or "")
        ):
            n = int(n)
            only = and_(work_cond, AISystemRun.outcome_id == outcome_id)
            cost_rows.append(OutcomeCostRow(
                outcome_id=outcome_id or "",
                runs=n,
                cost_usd=round(float(subtotal or 0), 8),
                cost_per_run=round(float(subtotal or 0) / n, 8) if n else None,
                median_cost_usd=_median_of(cost, n, only),
                p95_cost_usd=_pct_of(cost, n, 0.95, only),
            ))
            action_rows.append(OutcomeActionRow(
                outcome_id=outcome_id or "",
                runs=n,
                median_actions=_median_of(actions_col, n, only),
                p95_actions=_pct_of(actions_col, n, 0.95, only),
                max_actions=int(max_actions) if max_actions is not None else None,
            ))

        cost_signal = CostPerRunSignal(
            processed_runs=processed,
            total_cost_usd=round(float(agg[1] or 0), 8),
            cost_per_processed_outcome=(
                round(float(agg[3] or 0) / processed, 8) if processed else None
            ),
            median_cost_per_processed_outcome=_median_of(cost, processed, work_cond),
            p95_cost_per_processed_outcome=_pct_of(cost, processed, 0.95, work_cond),
            by_outcome=cost_rows,
            wasted_cost_usd=round(float(agg[4] or 0), 8),
            no_work_runs=int(agg[17] or 0),
            no_work_cost_usd=round(float(agg[18] or 0), 8),
            total_tokens=int(agg[5] or 0),
            tokens_per_processed_outcome=(
                round(int(agg[6] or 0) / processed, 2) if processed else None
            ),
        )

        buckets = [
            ActionDistributionBucket(lower=lower, upper=upper, count=int(agg[bucket_base + i] or 0))
            for i, (lower, upper) in enumerate(STEP_BUCKETS)
        ]
        exhausted = int(agg[9] or 0)
        action_signal = ActionCountSignal(
            runs=processed,
            median_actions=_median_of(actions_col, processed, work_cond),
            p95_actions=_pct_of(actions_col, processed, 0.95, work_cond),
            max_actions=int(agg[15]) if processed and agg[15] is not None else None,
            distribution=buckets,
            exhausted_runs=exhausted,
            exhaustion_rate=_rate(exhausted, total),
            # Exhaustion is only ever client-reported. If nothing in the
            # window was closed explicitly, a 0% rate carries no
            # information at all.
            exhaustion_reporting_available=bool(int(agg[11] or 0)),
            abandoned_runs=int(agg[10] or 0),
            finished_runs=total,
            by_outcome=action_rows,
        )

        context_signal = self._context_signal(
            _q, work_cond, processed, _median_of, _pct_of
        )

        tool_signal = self._tool_error_signal_sql(
            _q, total_calls=int(agg[7] or 0), total_errors=int(agg[8] or 0)
        )
        step_signal = self._step_error_signal_sql(db, conds)
        outcome_signal = self._outcome_signal_sql(_q, total=total)
        terminal_signal = self._terminal_state_signal_sql(
            _q, total=total, rate=outcome_signal.success_rate
        )
        cause_signal = self._error_cause_signal_sql(_q)

        signed = int(agg[12] or 0)
        asserted = int(agg[13] or 0)
        external = int(agg[16] or 0)
        # Coverage and provenance, separately. `observed_pct` is the number
        # that qualifies every signal above it — how much of the traffic we
        # saw completely. `chain_integrity_pct` (signed share) and
        # `asserted_pct` split that coverage by provenance, because an
        # auditor asking "can the gateway *prove* these steps belong
        # together" deserves a different number from an operator asking
        # "are these figures computed over everything that happened".
        # Ingested runs count as observed (complete as reported) and are
        # disclosed separately, because "we saw it" and "we were told" are
        # different claims with different authors.
        chain = {
            "chain_integrity_pct": round(signed / total, 6) if total else None,
            "observed_pct": round((signed + asserted + external) / total, 6) if total else None,
            "asserted_pct": round(asserted / total, 6) if total else None,
            "external_pct": round(external / total, 6) if total else None,
        }

        return RunStatsResponse(
            ai_system_id=ai_system_id,
            period_hours=period_hours,
            window_start=window_start.isoformat() if window_start else None,
            window_end=(window_end or now).isoformat(),
            cost=cost_signal,
            actions=action_signal,
            context=context_signal,
            tool_errors=tool_signal,
            step_errors=step_signal,
            terminal_states=terminal_signal,
            outcomes=outcome_signal,
            error_causes=cause_signal,
            **chain,
            fragmented_runs=int(agg[14] or 0),
        )

    # ------------------------------------------------------------------
    # Context size — described here, judged by the drift engine
    # ------------------------------------------------------------------

    def _context_signal(self, _q, work_cond, processed, _median_of, _pct_of) -> ContextSignal:
        peak = AISystemRun.peak_context_tokens
        growth = AISystemRun.context_growth
        utilization = AISystemRun.peak_context_utilization
        measured, max_peak, utilized, max_utilization = (
            _q(
                func.count(peak),
                func.max(peak),
                func.count(utilization),
                func.max(utilization),
            )
            .filter(work_cond)
            .one()
        )
        measured = int(measured or 0)
        utilized = int(utilized or 0)
        has_peak = and_(work_cond, peak.isnot(None))
        has_growth = and_(work_cond, growth.isnot(None))
        has_utilization = and_(work_cond, utilization.isnot(None))
        growth_runs = int(_q(func.count(growth)).filter(work_cond).scalar() or 0)
        return ContextSignal(
            runs=processed,
            measured_runs=measured,
            median_peak_tokens=_median_of(peak, measured, has_peak),
            p95_peak_tokens=_pct_of(peak, measured, 0.95, has_peak),
            max_peak_tokens=int(max_peak) if max_peak is not None else None,
            median_growth=_median_of(growth, growth_runs, has_growth),
            p95_growth=_pct_of(growth, growth_runs, 0.95, has_growth),
            utilization_measured_runs=utilized,
            p95_utilization=_pct_of(utilization, utilized, 0.95, has_utilization),
            max_utilization=float(max_utilization) if max_utilization is not None else None,
        )

    # ------------------------------------------------------------------
    # Signal 3 — tool error rate by tool
    # ------------------------------------------------------------------

    def _tool_error_signal_sql(
        self, _q, *, total_calls: int, total_errors: int
    ) -> ToolErrorSignal:
        # `tool_error_types` is keyed by error type, not by tool: the step
        # rollup does not carry per-tool error attribution, and inventing
        # one by dividing a run's errors across the tools it happened to
        # call would produce numbers that look precise and are not.
        # Reported per error type, labelled as such.
        #
        # Only the JSON column of runs that recorded error types is
        # fetched — the healthy majority never leaves the database.
        by_type: Dict[str, int] = {}
        for (types,) in _q(AISystemRun.tool_error_types).filter(
            AISystemRun.tool_error_types.isnot(None)
        ):
            for et, count in (types or {}).items():
                by_type[et] = by_type.get(et, 0) + int(count)

        rows = [
            ToolErrorRow(
                tool=et,
                calls=total_calls,
                errors=count,
                error_rate=round(count / total_calls, 6) if total_calls else 0.0,
                error_types={et: count},
            )
            for et, count in sorted(by_type.items(), key=lambda kv: -kv[1])
        ]

        return ToolErrorSignal(
            total_tool_calls=total_calls,
            total_tool_errors=total_errors,
            overall_error_rate=_rate(total_errors, total_calls),
            by_tool=rows,
        )

    # ------------------------------------------------------------------
    # Step errors by kind — model, tool, skill, agent
    # ------------------------------------------------------------------

    def _step_error_signal_sql(self, db: Session, conds) -> StepErrorSignal:
        kind = _step_kind()
        failed = _step_failed()
        status = case((failed, ProxyLog.http_status_code), else_=None)
        q = _run_steps_join(
            db.query(kind, status, func.count(ProxyLog.id), func.sum(case((failed, 1), else_=0)))
        )
        for c in conds:
            q = q.filter(c)
        rows = q.group_by(kind, status).all()
        calls: Dict[str, int] = {}
        errors: Dict[str, int] = {}
        codes: Dict[str, Dict[str, int]] = {}
        for k, code, n, n_failed in rows:
            calls[k] = calls.get(k, 0) + int(n)
            n_failed = int(n_failed or 0)
            if n_failed:
                errors[k] = errors.get(k, 0) + n_failed
                key = str(code) if code is not None else "none"
                codes.setdefault(k, {})[key] = codes.get(k, {}).get(key, 0) + n_failed
        by_kind = [
            StepErrorKindRow(
                kind=k,
                calls=calls[k],
                errors=errors.get(k, 0),
                error_rate=_rate(errors.get(k, 0), calls[k]),
                status_codes=dict(sorted(codes.get(k, {}).items(), key=lambda kv: -kv[1])),
            )
            for k in STEP_KINDS
            if calls.get(k)
        ]
        total_calls = sum(calls.values())
        total_errors = sum(errors.values())
        return StepErrorSignal(
            total_calls=total_calls,
            total_errors=total_errors,
            overall_error_rate=_rate(total_errors, total_calls),
            by_kind=by_kind,
        )

    # ------------------------------------------------------------------
    # Signal 4 — terminal-state mix
    # ------------------------------------------------------------------

    def _outcome_signal_sql(self, _q, *, total: int) -> OutcomeSignal:
        """Runs by reported outcome, conformance and run class."""
        cls = run_class_sql()
        rows = (
            _q(
                AISystemRun.outcome_id,
                AISystemRun.outcome_conformance,
                AISystemRun.outcome_counts_toward_baseline,
                cls,
                func.count(AISystemRun.id),
                func.coalesce(func.sum(AISystemRun.total_cost_usd), 0),
            )
            .group_by(
                AISystemRun.outcome_id,
                AISystemRun.outcome_conformance,
                AISystemRun.outcome_counts_toward_baseline,
                cls,
            )
            .all()
        )
        by_class: Dict[str, int] = {c: 0 for c in RUN_CLASSES}
        out: List[OutcomeRow] = []
        for outcome_id, conformance, counts, run_class, n, subtotal in rows:
            n = int(n)
            by_class[run_class] = by_class.get(run_class, 0) + n
            out.append(OutcomeRow(
                outcome_id=outcome_id,
                conformance=conformance,
                counts_toward_baseline=counts,
                run_class=run_class,
                runs=n,
                share=_rate(n, total) or 0.0,
                cost_usd=round(float(subtotal or 0), 8),
            ))
        out.sort(key=lambda r: (-r.runs, r.outcome_id or "", r.run_class))
        return OutcomeSignal(
            total_runs=total,
            success_rate=(
                round(success_rate(by_class), 6) if success_rate(by_class) is not None else None
            ),
            by_class=by_class,
            by_outcome=out,
        )

    def _error_cause_signal_sql(self, _q) -> ErrorCauseSignal:
        """Failed runs (errored / exhausted) by error cause.
        A failed run without a recorded cause is listed as `unknown`."""
        counts: Dict[str, int] = {}
        for cause, n in (
            _q(AISystemRun.error_cause, func.count(AISystemRun.id))
            .filter(class_cond(RUN_FAILED))
            .group_by(AISystemRun.error_cause)
        ):
            key = cause or "unknown"
            counts[key] = counts.get(key, 0) + int(n)
        failed = sum(counts.values())
        return ErrorCauseSignal(
            failed_runs=failed,
            by_cause=[
                ErrorCauseRow(cause=c, runs=n, share=_rate(n, failed) or 0.0)
                for c, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
            ],
        )

    def _terminal_state_signal_sql(
        self, _q, *, total: int, rate: Optional[float]
    ) -> TerminalStateSignal:
        counts: Dict[str, int] = {
            state: int(count)
            for state, count in _q(
                AISystemRun.terminal_state, func.count(AISystemRun.id)
            ).group_by(AISystemRun.terminal_state)
        }

        rows = [
            TerminalStateRow(
                state=state,
                count=count,
                share=round(count / total, 6) if total else 0.0,
            )
            for state, count in sorted(counts.items(), key=lambda kv: -kv[1])
        ]

        return TerminalStateSignal(
            total_runs=total,
            by_state=rows,
            # THE success rate, not completed / total.
            success_rate=rate,
        )


    # ------------------------------------------------------------------
    # Bucketed signals for charts
    # ------------------------------------------------------------------

    def timeseries(
        self,
        db: Session,
        tenant_id: str,
        *,
        ai_system_id: str,
        period_hours: int = 24,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ) -> RunTimeseriesResponse:
        """The signals over time, one grouped query.

        Hourly buckets at 48h and under, daily above — `func.date` and
        `extract('hour')` both compile on PG and SQLite, unlike
        date_trunc. Sparse: quiet buckets are omitted and the client
        zero-fills, because an empty bucket is data, not a gap.
        """
        window_start, window_end = resolve_window(period_hours, start, end)
        hourly = (
            window_start is not None
            and (datetime.now(timezone.utc) - window_start) <= timedelta(hours=48)
        )

        conds = [
            AISystemRun.tenant_id == tenant_id,
            AISystemRun.ai_system_id == ai_system_id,
            AISystemRun.is_replay.is_(False),
        ]
        if window_start is not None:
            conds.append(AISystemRun.started_at >= window_start)
        if window_end is not None:
            conds.append(AISystemRun.started_at < window_end)

        day = func.date(AISystemRun.started_at)
        group_cols = (
            (day, func.extract("hour", AISystemRun.started_at))
            if hourly
            else (day,)
        )

        def _class_sum(run_class: str):
            return func.coalesce(func.sum(case((class_cond(run_class), 1), else_=0)), 0)

        work_cond = class_cond(RUN_SUCCESS)
        q = db.query(
            *group_cols,
            func.count(AISystemRun.id),
            _class_sum(RUN_SUCCESS),
            _class_sum(RUN_NO_WORK),
            _class_sum(RUN_DEVIATION),
            _class_sum(RUN_FAILED),
            _class_sum(RUN_NOT_DECISIVE),
            func.coalesce(func.sum(AISystemRun.total_cost_usd), 0.0),
            func.coalesce(
                func.sum(case((work_cond, AISystemRun.total_cost_usd), else_=0.0)),
                0.0,
            ),
            func.coalesce(func.sum(AISystemRun.tool_call_count), 0),
            func.coalesce(func.sum(AISystemRun.tool_error_count), 0),
            # Over the work population only: AVG ignores the NULLs.
            func.avg(case((work_cond, func.coalesce(AISystemRun.action_count, 0)), else_=None)),
        )
        for c in conds:
            q = q.filter(c)
        rows = q.group_by(*group_cols).order_by(*group_cols).all()

        # Model calls per bucket of the RUN's start, so a point's model
        # error rate describes the same runs as its other numbers.
        model_q = _run_steps_join(
            db.query(
                *group_cols,
                func.count(ProxyLog.id),
                func.sum(case((_step_failed(), 1), else_=0)),
            )
        ).filter(_step_kind() == STEP_MODEL)
        for c in conds:
            model_q = model_q.filter(c)
        model_by_bucket = {
            tuple(str(v) for v in r[:-2]): (int(r[-2] or 0), int(r[-1] or 0))
            for r in model_q.group_by(*group_cols).all()
        }

        points: List[RunTimeseriesPoint] = []
        n_keys = len(group_cols)
        for row in rows:
            if hourly:
                bucket_day, bucket_hour = row[0], row[1]
                bucket_start = f"{str(bucket_day)[:10]}T{int(bucket_hour):02d}:00:00Z"
            else:
                bucket_day = row[0]
                bucket_start = f"{str(bucket_day)[:10]}T00:00:00Z"
            (runs, n_success, n_no_work, n_deviation, n_failed, n_not_decisive,
             cost, processed_cost, calls, errors, avg_actions) = row[n_keys:]
            model_calls, model_errors = model_by_bucket.get(
                tuple(str(v) for v in row[:n_keys]), (0, 0)
            )
            n_success = int(n_success)
            points.append(
                RunTimeseriesPoint(
                    bucket_start=bucket_start,
                    runs=int(runs),
                    success=n_success,
                    no_work=int(n_no_work),
                    deviation=int(n_deviation),
                    failed=int(n_failed),
                    not_decisive=int(n_not_decisive),
                    cost_usd=round(float(cost or 0.0), 6),
                    processed_cost_usd=round(float(processed_cost or 0.0), 6),
                    cost_per_processed_outcome=(
                        round(float(processed_cost or 0.0) / n_success, 6)
                        if n_success
                        else None
                    ),
                    tool_calls=int(calls),
                    tool_errors=int(errors),
                    tool_error_rate=_rate(int(errors), int(calls)),
                    model_calls=model_calls,
                    model_errors=model_errors,
                    model_error_rate=_rate(model_errors, model_calls),
                    avg_actions=(
                        round(float(avg_actions), 2)
                        if avg_actions is not None
                        else None
                    ),
                )
            )

        return RunTimeseriesResponse(
            ai_system_id=ai_system_id,
            period_hours=period_hours,
            bucket="hour" if hourly else "day",
            window_start=window_start.isoformat() if window_start else None,
            window_end=window_end.isoformat() if window_end else None,
            points=points,
        )


run_signal_service = RunSignalService()
