# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Connection status — the Composition tab's connector lines

The Simple graph draws a line from the gateway to every model, MCP server,
skill, knowledge base and delegation agent the AI System uses, and one from
the system to the gateway. Each line is coloured by the connection's status:

  * ``ok``        — calls in the window, nothing wrong (green)
  * ``degraded``  — some degradation (orange)
  * ``down``      — communication stopped or blocked (red)
  * ``idle``      — no calls in the window and no negative health signal
                    (grey). No evidence is NOT "all good".

## The rules — ONE function: `connection_status`

Evidence is of two kinds: this system's own traffic to the resource in the
composition window (`TrafficEvidence`) and what the platform already knows
about the resource's health (`HealthSignal`, from `*_signals` below).
The call-count part is THE down rule every surface shares
(`ai_system_down_rule.latest_calls_verdict`, one fact, one computation) — on-demand
liveness and dependency health apply the same rule to the same idea of
"the latest calls":

  1. **down** — a health signal says down (disabled, retired, archived,
     unhealthy / circuit breaker open, not answering), OR the most recent
     ``DOWN_AFTER_CONSECUTIVE_FAILURES`` (3) calls ALL failed or were
     blocked.
  2. **degraded** — a health signal says degraded, OR a call among the
     most recent 3 failed or was blocked, OR (errors + blocked) / calls >=
     ``DEGRADED_FAILURE_RATE`` (5 %) in the window, OR a hold lapsed (below).
  3. **ok** — at least one call in the window, and none of the above.
  4. **idle** — no calls in the window, and no negative health signal.

Thresholds live in `ai_system_down_rule` (pinned in
`tests/test_ai_system_connection_status.py`).

## Where the traffic comes from

Resources: the proxy_logs rows of this system's OWN runs — the own-rows
rule conformance uses (`ai_system_conformance.own_rows`), so a delegate's
calls inside the root's runs are never read as the root's. Keys:

  * model — ``proxy_type='llm'``, ``server_id`` = the llm_model id
  * MCP server — ``proxy_type='mcp'``, ``server_id`` = the mcp_servers id
    (the composition binding is a server_config; mapped through it)
  * skill — a skill RUN: ``proxy_type='skill'`` or a run through the
    Agent Skills system MCP server (``proxy_subtype='skill_run'``), keyed
    by ``skill_id`` (model/tool calls a skill initiated also carry
    ``skill_id`` and are not runs)
  * knowledge base — any row whose ``rag_collection_ids`` lists it
  * external / card-only delegation — ``proxy_type='a2a'``, ``server_id``
    = the agent card

A row is **blocked** when the gateway refused it (guardrail block,
argument-policy deny, agent-authz deny, a refusal subtype the evidence
writer seals as denied — `records.DENY_SUBTYPES`, or HTTP 429 from a
limit); it is an **error** when it is not blocked and failed
(``success`` false or HTTP >= 400).

A row **held** for a human approval decision is counted on its own — never
as blocked, an error or a failure in the latest-N: a hold is the approval gate working. Its approval request
(``custom_metadata.approval_request_id``, stamped by the core) decides what
it means. Decided (approved, consumed, rejected) or still pending inside its
timeout: all good. **Lapsed** — expired, pending past ``expires_at`` (before
the core's sweeper rewrites it), or no request on record: nobody sorted it
in time, and the line is degraded. A lapsed hold is a warning, never down.

Delegation to an AI System (a resolved shared service): the run segments
this system's calls entered (`CompositionIndex.pairs` — the SAME stats the
dependency node shows), classified by `ai_system_down_rule.
classify_segment_outcome`: answered / rejected (the delegate declined — an
answer) = ok, failed / timed out = error, a gateway refusal (stored as
`failed` + `blocked_by_policy`, read back as `blocked`) = blocked. Its
health signal is the dependency status
(`CompositionIndex.dependency_status`) — read, never recomputed.

The system → gateway line is the system's stored health
(`system_connection`): healthy → ok; degraded / drifting → degraded;
silent / suspended / stalled → down; learning / unknown / none → idle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import and_, case, exists, func, or_
from sqlalchemy.orm import Session

# THE down rule's thresholds — defined once, in `ai_system_down_rule`.
from app.services.ai_system_down_rule import (
    CALL_ANSWERED,
    CALL_BLOCKED,
    CALLS_DEGRADED,
    CALLS_DOWN,
    DEGRADED_FAILURE_RATE,
    DOWN_AFTER_CONSECUTIVE_FAILURES,
    classify_segment_outcome,
    failure_rate_degraded,
    latest_calls_verdict,
)
from app.services.ai_system_judgement_exclusion import PURPOSE_RATE, judged_run

STATUS_OK = "ok"
STATUS_DEGRADED = "degraded"
STATUS_DOWN = "down"
STATUS_IDLE = "idle"
CONNECTION_STATUSES = (STATUS_OK, STATUS_DEGRADED, STATUS_DOWN, STATUS_IDLE)


#: One recent call's outcome.
OUTCOME_OK = "ok"
OUTCOME_ERROR = "error"
OUTCOME_BLOCKED = "blocked"
#: Held for a human approval decision — counted apart, never a failure.
OUTCOME_HELD = "held"


@dataclass(frozen=True)
class HealthSignal:
    """A negative health fact about a resource (never a positive one: a
    healthy probe is not evidence that THIS system's calls work)."""

    level: str  # STATUS_DOWN | STATUS_DEGRADED
    reason: str


@dataclass
class TrafficEvidence:
    calls: int = 0
    errors: int = 0
    blocked: int = 0
    #: Held for human approval (any decision); not part of `blocked`.
    held: int = 0
    #: Of `held`: lapsed — nobody decided before the timeout.
    held_lapsed: int = 0
    last_call_at: Optional[datetime] = None
    #: The latest outcomes, newest first (at most
    #: DOWN_AFTER_CONSECUTIVE_FAILURES are needed).
    recent: List[Tuple[datetime, str]] = field(default_factory=list)

    def add(self, other: "TrafficEvidence") -> None:
        self.calls += other.calls
        self.errors += other.errors
        self.blocked += other.blocked
        self.held += other.held
        self.held_lapsed += other.held_lapsed
        if other.last_call_at and (self.last_call_at is None or other.last_call_at > self.last_call_at):
            self.last_call_at = other.last_call_at
        merged = sorted(self.recent + other.recent, key=lambda r: r[0], reverse=True)
        self.recent = merged[:DOWN_AFTER_CONSECUTIVE_FAILURES]


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def window_phrase(window_hours: int) -> str:
    if window_hours % 24 == 0:
        days = window_hours // 24
        return "the last day" if days == 1 else f"the last {days} days"
    return "the last hour" if window_hours == 1 else f"the last {window_hours} hours"


def _plural(n: int, one: str, many: Optional[str] = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _traffic_line(t: TrafficEvidence) -> str:
    """"12 calls, 0 errors" (+ ", 1 blocked" / ", 2 held" when any were)."""
    line = f"{_plural(t.calls, 'call')}, {_plural(t.errors, 'error')}"
    if t.blocked:
        line += f", {t.blocked} blocked"
    if t.held:
        line += f", {t.held} held"
    return line


def _failed_phrase(errors: int, blocked: int) -> str:
    if blocked == 0:
        return "failed"
    if errors == 0:
        return "blocked by policy"
    return "failed or were blocked"


def connection_status(
    traffic: Optional[TrafficEvidence],
    signals: Iterable[HealthSignal] = (),
    *,
    window_hours: int,
) -> Dict[str, Any]:
    """THE connection status — see the module docstring for the rules.

    Returns the `CompositionConnection` wire shape: `{status, reason, calls,
    errors, blocked, last_call_at}`.
    """
    t = traffic or TrafficEvidence()
    signals = list(signals)
    status = STATUS_IDLE
    reason = ""

    down = [s for s in signals if s.level == STATUS_DOWN]
    degraded = [s for s in signals if s.level == STATUS_DEGRADED]
    recent = [o for _, o in t.recent[:DOWN_AFTER_CONSECUTIVE_FAILURES]]
    verdict = latest_calls_verdict(o in (OUTCOME_ERROR, OUTCOME_BLOCKED) for o in recent)
    bad = t.errors + t.blocked

    signal: Optional[HealthSignal] = None
    if down:
        signal = down[0]
    elif verdict == CALLS_DOWN:
        n_blocked = sum(1 for o in recent if o == OUTCOME_BLOCKED)
        status = STATUS_DOWN
        reason = (
            f"Last {DOWN_AFTER_CONSECUTIVE_FAILURES} calls "
            f"{_failed_phrase(len(recent) - n_blocked, n_blocked)}"
        )
    elif degraded:
        signal = degraded[0]
    elif failure_rate_degraded(t.calls, bad):
        status = STATUS_DEGRADED
        reason = f"{bad} of {_plural(t.calls, 'call')} {_failed_phrase(t.errors, t.blocked)}"
    elif t.held_lapsed:
        status = STATUS_DEGRADED
        reason = (
            f"{_plural(t.held_lapsed, 'hold')} expired without a decision · {_traffic_line(t)}"
        )
    elif verdict == CALLS_DEGRADED:
        n_blocked = sum(1 for o in recent if o == OUTCOME_BLOCKED)
        n_bad = sum(1 for o in recent if o in (OUTCOME_ERROR, OUTCOME_BLOCKED))
        status = STATUS_DEGRADED
        reason = (
            f"{n_bad} of the last {_plural(len(recent), 'call')} "
            f"{_failed_phrase(n_bad - n_blocked, n_blocked)} · {_traffic_line(t)}"
        )
    elif t.calls:
        status, reason = STATUS_OK, _traffic_line(t)
    else:
        status, reason = STATUS_IDLE, f"No calls in {window_phrase(window_hours)}"

    if signal is not None:
        # A health-signal verdict still says what the traffic was, so the
        # tooltip never hides the evidence behind the verdict.
        status = signal.level
        reason = f"{signal.reason} · " + (
            _traffic_line(t) if t.calls else f"no calls in {window_phrase(window_hours)}"
        )

    return {
        "status": status,
        "reason": reason,
        "calls": t.calls,
        "errors": t.errors,
        "blocked": t.blocked,
        "held": t.held,
        "held_lapsed": t.held_lapsed,
        "last_call_at": t.last_call_at.isoformat() if t.last_call_at else None,
    }


# ---------------------------------------------------------------------------
# Health signals — what the platform already knows, per resource type
# ---------------------------------------------------------------------------


def _probe_signals(health_status: Optional[str], error: Optional[str], failures: Optional[int]) -> List[HealthSignal]:
    """The shared health-probe vocabulary of llm_model and mcp_servers
    (written by the core's live health updater, circuit breaker included:
    an open breaker writes `unhealthy` / 'Circuit breaker open')."""
    if health_status == "unhealthy":
        detail = f": {error}" if error else ""
        return [HealthSignal(STATUS_DOWN, f"Unhealthy{detail}")]
    if health_status == "degraded":
        n = int(failures or 0)
        detail = f" ({_plural(n, 'consecutive failure')})" if n else ""
        return [HealthSignal(STATUS_DEGRADED, f"Health degraded{detail}")]
    return []


def model_signals(model: Any, provider: Any = None, *, today: Optional[date] = None) -> List[HealthSignal]:
    if model is None:
        return [HealthSignal(STATUS_DOWN, "The model no longer exists")]
    out: List[HealthSignal] = []
    if model.enabled is False:
        out.append(HealthSignal(STATUS_DOWN, "Model disabled"))
    if provider is not None and provider.enabled is False:
        name = getattr(provider, "display_name", None) or getattr(provider, "key", None) or "provider"
        out.append(HealthSignal(STATUS_DOWN, f"Provider {name} disabled"))
    retire = getattr(model, "retire_date", None)
    if retire is not None and retire <= (today or datetime.now(timezone.utc).date()):
        out.append(HealthSignal(STATUS_DOWN, f"Retired on {retire.isoformat()}"))
    if model.health_status == "requires_tier":
        out.append(HealthSignal(STATUS_DOWN, "Not available on the configured API key's tier"))
    out.extend(_probe_signals(model.health_status, model.health_check_error,
                              model.consecutive_health_failures))
    return out


def mcp_signals(config: Any, server: Any) -> List[HealthSignal]:
    if config is None or server is None:
        return [HealthSignal(STATUS_DOWN, "The MCP server no longer exists")]
    out: List[HealthSignal] = []
    if config.enabled is False:
        out.append(HealthSignal(STATUS_DOWN, "Disabled for this tenant"))
    if server.enabled is False:
        out.append(HealthSignal(STATUS_DOWN, "MCP server disabled"))
    out.extend(_probe_signals(server.health_status, server.health_check_error,
                              server.consecutive_health_failures))
    return out


def skill_signals(skill: Any) -> List[HealthSignal]:
    if skill is None:
        return [HealthSignal(STATUS_DOWN, "The skill no longer exists")]
    status = getattr(skill.status, "value", skill.status)
    out: List[HealthSignal] = []
    if skill.enabled is False:
        out.append(HealthSignal(STATUS_DOWN, "Skill disabled"))
    if status == "archived":
        out.append(HealthSignal(STATUS_DOWN, "Skill archived"))
    elif status == "deprecated":
        out.append(HealthSignal(STATUS_DEGRADED, "Skill deprecated"))
    return out


def kb_signals(collection: Any) -> List[HealthSignal]:
    if collection is None:
        return [HealthSignal(STATUS_DOWN, "The knowledge base no longer exists")]
    if collection.enabled is False:
        return [HealthSignal(STATUS_DOWN, "Knowledge base disabled")]
    return []


#: Failing signals that mean the dependency is not answering at all
#: (`ai_system_dependency_health._OUTAGE_SIGNALS`).
_DELEGATION_DOWN_SIGNALS = {
    "liveness": "Not answering (liveness)",
    "autonomy": "Suspended",
}


def delegation_signals(health: str, failing_signal: Optional[str]) -> List[HealthSignal]:
    """The dependency status (`CompositionIndex.dependency_status`)
    as a health signal. healthy / idle / unknown / assumed say nothing
    negative; traffic decides."""
    if health != "degraded":
        return []
    if failing_signal in _DELEGATION_DOWN_SIGNALS:
        return [HealthSignal(STATUS_DOWN, _DELEGATION_DOWN_SIGNALS[failing_signal])]
    if failing_signal == "dependency":
        return [HealthSignal(STATUS_DEGRADED, "A critical dependency of this agent is failing")]
    return [HealthSignal(STATUS_DEGRADED, f"Failing signal: {failing_signal or 'unknown'}")]


# The system's stored health state → its line.
SYSTEM_STATE_STATUS = {
    "healthy": STATUS_OK,
    "degraded": STATUS_DEGRADED,
    "drifting": STATUS_DEGRADED,
    "silent": STATUS_DOWN,
    "suspended": STATUS_DOWN,
    "stalled": STATUS_DOWN,
    "failing": STATUS_DOWN,
    # No verdict: baselines still learning, or too little evidence to score.
    # The line is then drawn from the system's own traffic by THE connection
    # rule — grey only when there were no calls. Mapping these to idle drew
    # "no calls" over a system calling the gateway fine.
    "learning": None,
    "unknown": None,
}


def system_connection(
    health_state: Optional[str],
    reasons: Any,
    traffic: Optional[TrafficEvidence],
    *,
    window_hours: int,
) -> Dict[str, Any]:
    """The system → gateway line: its STORED health (the verdict every
    other surface shows), with its own traffic totals for the tooltip. A
    stored state that gives no verdict (learning, unknown, none yet) draws
    the line from that traffic, by the rule every other line uses."""
    t = traffic or TrafficEvidence()
    status = SYSTEM_STATE_STATUS.get(health_state or "")
    if status is None:
        status = connection_status(t, (), window_hours=window_hours)["status"]
    first = next((r for r in (reasons or []) if isinstance(r, str) and r.strip()), None)
    if health_state is None:
        head = "No health computed yet"
    else:
        head = f"Health: {health_state}"
    reason = f"{head} — {first}" if first else head
    reason += " · " + (_traffic_line(t) if t.calls else f"no calls in {window_phrase(window_hours)}")
    return {
        "status": status,
        "reason": reason,
        "calls": t.calls,
        "errors": t.errors,
        "blocked": t.blocked,
        "held": t.held,
        "held_lapsed": t.held_lapsed,
        "last_call_at": t.last_call_at.isoformat() if t.last_call_at else None,
    }


# ---------------------------------------------------------------------------
# Traffic — the system's own proxy_logs rows in the window
# ---------------------------------------------------------------------------

KIND_MODEL = "model"
KIND_MCP = "mcp"
KIND_SKILL = "skill"
KIND_KB = "kb"
KIND_A2A = "a2a"


def blocked_expr():
    """THE "the gateway refused this row" predicate (see the module
    docstring). Shared by the connector lines (`own_traffic`) and the run
    overlay's per-action outcome (`run_actions`), so a row one surface counts
    as blocked the other cannot read as answered."""
    from app.database.models.proxy_log import ProxyLog
    from app.services.compliance.resolvers.records import DENY_SUBTYPES

    # Every operand is NULL-safe: `NOT (… OR NULL)` is NULL, and a NULL
    # `blocked` would silently drop the row from the error count too.
    return or_(
        func.coalesce(ProxyLog.guardrail_blocked, False).is_(True),
        func.coalesce(ProxyLog.argument_policy_denied, False).is_(True),
        func.coalesce(ProxyLog.agent_authz_decision, "") == "deny",
        func.coalesce(ProxyLog.proxy_subtype, "").in_(DENY_SUBTYPES),
        # A call held for human approval did not go through: it is stopped
        # until someone decides. Counted as blocked on its own, not only when
        # the writer also happens to set another block flag on the row.
        held_expr(),
        func.coalesce(ProxyLog.http_status_code, 0) == 429,
    )


def held_expr():
    """A row held for a human approval decision — the run overlay's distinct
    hold marker, and counted on its own by the line (`own_traffic`)."""
    from app.database.models.proxy_log import ProxyLog
    from app.constants.proxy import ProxySubtype

    return func.coalesce(ProxyLog.proxy_subtype, "") == ProxySubtype.APPROVAL_REQUIRED.value


def refused_expr():
    """Blocked, less holds: what the LINE counts as blocked. `blocked_expr`
    keeps holds (a held row is not an error, and the run overlay tells the
    two apart with `held_expr`)."""
    return and_(blocked_expr(), ~held_expr())


def hold_sorted_in_time_expr(tenant_id_col, approval_request_id, now: datetime):
    """THE "this hold was sorted in time" predicate, for any row that names
    the approval request it waits on: the request is decided (approved,
    consumed, rejected) or still pending inside ``expires_at``. A request that
    expired, lapsed or is not on record is NOT sorted — the hold then warns.
    Same reading as `app.constants.approvals.decision`; proxy-log holds and
    held delegations (run segments) both read it."""
    from app.constants import approvals
    from app.database.models.tool_approval_request import ToolApprovalRequest as Apr

    return exists().where(
        Apr.tenant_id == tenant_id_col,
        Apr.id == approval_request_id,
        or_(
            Apr.status.in_(approvals.DECIDED_STATUSES),
            and_(Apr.status == approvals.PENDING, Apr.expires_at >= now),
        ),
    )


def lapsed_hold_expr(now: datetime):
    """A held proxy-log row nobody decided before its timeout (its request is
    ``custom_metadata.approval_request_id``)."""
    from app.database.models.proxy_log import ProxyLog

    sorted_in_time = hold_sorted_in_time_expr(
        ProxyLog.tenant_id, ProxyLog.custom_metadata["approval_request_id"].as_string(), now
    )
    return and_(held_expr(), ~sorted_in_time)


def held_segment_expr(model):
    """A run segment stopped only by a hold: stored as the gateway
    stop (`failed` + `blocked_by_policy`) that also names the approval
    request it waits on."""
    from app.services.ai_system_down_rule import SEGMENT_REFUSED, segment_call_outcome_sql

    return and_(
        model.held_approval_request_id.isnot(None),
        segment_call_outcome_sql(model) == SEGMENT_REFUSED,
    )


def lapsed_held_segment_expr(model, now: datetime):
    """A held segment whose hold nobody decided before its timeout."""
    return and_(
        held_segment_expr(model),
        ~hold_sorted_in_time_expr(model.tenant_id, model.held_approval_request_id, now),
    )


def error_expr(blocked=None):
    """THE "this row failed" predicate: not blocked, and unsuccessful or
    HTTP >= 400."""
    from app.database.models.proxy_log import ProxyLog

    blocked = blocked if blocked is not None else blocked_expr()
    return and_(
        ~blocked,
        or_(
            func.coalesce(ProxyLog.success, True).is_(False),
            func.coalesce(ProxyLog.http_status_code, 0) >= 400,
        ),
    )


def connection_keys(
    ptype: Optional[str],
    subtype: Optional[str],
    server: Optional[str],
    skill: Optional[str],
    rag: Optional[str],
    capability: Optional[str],
) -> List[Tuple[str, str]]:
    """THE row → connector mapping: which `(kind, id)` connections one
    proxy_logs row travelled. The lines count a row under every key it
    returns; the run overlay places the row's badge by the same keys."""
    from app.constants.system_server import SKILL_RUN_TOOL
    from app.services.compliance.resolvers.records import SUBTYPE_SKILL_RUN

    out: List[Tuple[str, str]] = []
    if ptype == "llm" and server:
        out.append((KIND_MODEL, server))
    elif ptype == "mcp" and server:
        out.append((KIND_MCP, server))
    elif ptype == "a2a" and server:
        out.append((KIND_A2A, server))
    # A skill RUN ATTEMPT (not a model/tool call a skill initiated, which
    # also carries skill_id): the Skill Executor's own row, or a call to
    # the Agent Skills run verb — whether it ran (`skill_run`), was held
    # for approval or was refused. A held or refused run is the SKILL
    # being blocked; counting only completed runs showed a skill OK while
    # every run of it was held.
    if skill and (
        ptype == "skill"
        or subtype == SUBTYPE_SKILL_RUN
        or (ptype == "mcp" and capability == SKILL_RUN_TOOL)
    ):
        out.append((KIND_SKILL, skill))
    for kb in (rag or "").split(","):
        if kb.strip():
            out.append((KIND_KB, kb.strip()))
    return out


def own_traffic(
    db: Session, tenant_id: str, ai_system_id: str, window_start: datetime,
    *, now: Optional[datetime] = None,
) -> Tuple[Dict[Tuple[str, str], TrafficEvidence], TrafficEvidence]:
    """`({(kind, key): evidence}, totals)` over the system's own rows in the
    window. Two queries: per-partition aggregates, and the latest
    DOWN_AFTER_CONSECUTIVE_FAILURES rows of each partition (a resource's
    global latest-N is inside the union of its partitions' latest-N)."""
    from app.database.models.ai_system_run import AISystemRun
    from app.database.models.proxy_log import ProxyLog
    from app.services.ai_system_conformance import own_rows

    blocked = blocked_expr()
    # Holds are counted apart (module docstring): `blocked` keeps them only
    # so that a held row is never an error.
    is_blocked = case((refused_expr(), 1), else_=0)
    is_error = case((error_expr(blocked), 1), else_=0)
    is_held = case((held_expr(), 1), else_=0)
    is_lapsed = case((lapsed_hold_expr(now or datetime.now(timezone.utc)), 1), else_=0)
    part = (ProxyLog.proxy_type, ProxyLog.proxy_subtype, ProxyLog.server_id, ProxyLog.skill_id,
            ProxyLog.rag_collection_ids, ProxyLog.mcp_capability_name)
    in_window = ProxyLog.timestamp >= window_start
    # A row is graded through its run: a run inside an incident
    # window (of the system, or tenant-wide) is excluded whole from the
    # line's rate and THE down rule's latest-N; it stays in the ledger.
    judged = judged_run(AISystemRun, purpose=PURPOSE_RATE)

    by_key: Dict[Tuple[str, str], TrafficEvidence] = {}
    totals = TrafficEvidence()
    keys = connection_keys

    for ptype, subtype, server, skill, rag, capability, n, errors, n_blocked, n_held, n_lapsed, last in (
        own_rows(db, tenant_id, ai_system_id, *part, func.count(ProxyLog.id),
                 func.sum(is_error), func.sum(is_blocked), func.sum(is_held),
                 func.sum(is_lapsed), func.max(ProxyLog.timestamp))
        .filter(in_window, judged)
        .group_by(*part)
        .all()
    ):
        ev = TrafficEvidence(
            calls=int(n or 0), errors=int(errors or 0), blocked=int(n_blocked or 0),
            held=int(n_held or 0), held_lapsed=int(n_lapsed or 0), last_call_at=_aware(last),
        )
        totals.add(ev)
        for k in keys(ptype, subtype, server, skill, rag, capability):
            by_key.setdefault(k, TrafficEvidence()).add(ev)

    rn = func.row_number().over(partition_by=part, order_by=(ProxyLog.timestamp.desc(), ProxyLog.id.desc()))
    sub = (
        own_rows(db, tenant_id, ai_system_id, *part, ProxyLog.timestamp.label("ts"),
                 is_error.label("err"), is_blocked.label("blk"), is_held.label("hld"),
                 rn.label("rn"))
        .filter(in_window, judged)
        .subquery()
    )
    for ptype, subtype, server, skill, rag, capability, ts, err, blk, hld in (
        db.query(sub.c.proxy_type, sub.c.proxy_subtype, sub.c.server_id, sub.c.skill_id,
                 sub.c.rag_collection_ids, sub.c.mcp_capability_name, sub.c.ts, sub.c.err,
                 sub.c.blk, sub.c.hld)
        .filter(sub.c.rn <= DOWN_AFTER_CONSECUTIVE_FAILURES)
        .all()
    ):
        outcome = (OUTCOME_HELD if hld else OUTCOME_BLOCKED if blk
                   else OUTCOME_ERROR if err else OUTCOME_OK)
        ts = _aware(ts)
        # The system line's latest-N: its global latest calls are inside the
        # union of every partition's latest-N.
        totals.add(TrafficEvidence(recent=[(ts, outcome)]))
        for k in keys(ptype, subtype, server, skill, rag, capability):
            ev = by_key.setdefault(k, TrafficEvidence())
            ev.recent = sorted(ev.recent + [(ts, outcome)], key=lambda r: r[0], reverse=True)[
                :DOWN_AFTER_CONSECUTIVE_FAILURES
            ]
    return by_key, totals


def segment_evidence(calls: int, by_outcome: Dict[str, int], last_seen_at: Optional[datetime],
                     recent_outcomes: Sequence[Tuple[datetime, str]], *, held: int = 0,
                     held_lapsed: int = 0,
                     recent_held: Sequence[bool] = ()) -> TrafficEvidence:
    """Run-segment stats (a delegation to an AI System) as traffic, by THE
    classification (`classify_segment_outcome` over call tokens): answered
    and rejected are answers, blocked (the gateway refused — stored
    as `failed` + `blocked_by_policy`) is blocked, failed and timed out
    are errors. A held delegation (a blocked segment that names its
    approval request) is counted apart — `held` of the blocked, with
    `held_lapsed` of those nobody decided in time; `recent_held` flags the
    held ones among `recent_outcomes` (module docstring)."""
    line_outcome = {CALL_ANSWERED: OUTCOME_OK, CALL_BLOCKED: OUTCOME_BLOCKED}

    def outcome(o: str) -> str:
        return line_outcome.get(classify_segment_outcome(o), OUTCOME_ERROR)

    def count(cls: str) -> int:
        return sum(int(n) for o, n in by_outcome.items() if classify_segment_outcome(o) == cls)

    stopped = count(CALL_BLOCKED)
    answered = count(CALL_ANSWERED)
    held = min(held, stopped)
    flags = list(recent_held) + [False] * len(recent_outcomes)
    return TrafficEvidence(
        calls=calls,
        errors=max(0, calls - answered - stopped),
        blocked=stopped - held,
        held=held,
        held_lapsed=min(held_lapsed, held),
        last_call_at=_aware(last_seen_at),
        recent=[
            (ts, OUTCOME_HELD if flags[i] else outcome(o))
            for i, (ts, o) in enumerate(recent_outcomes)
        ][:DOWN_AFTER_CONSECUTIVE_FAILURES],
    )


def pair_evidence(st: Any) -> TrafficEvidence:
    """A delegation pair's (`CompositionIndex.PairStats`) judged stats as
    the line's traffic, holds counted apart."""
    return segment_evidence(
        st.judged_calls, st.judged_by_outcome, st.last_seen_at, st.recent,
        held=st.judged_held, held_lapsed=st.judged_held_lapsed, recent_held=st.recent_held,
    )


# ---------------------------------------------------------------------------
# Attaching statuses to the Composition payload
# ---------------------------------------------------------------------------


def attach_connections(
    db: Session,
    index: Any,
    system: Any,
    resources: Dict[str, List[Dict[str, Any]]],
    dependencies: List[Dict[str, Any]],
    observed_undeclared: List[Dict[str, Any]],
    dependency_statuses: Dict[str, Tuple[str, Optional[str]]],
) -> Dict[str, Any]:
    """Set `connection` on every resource, delegation node and observed
    undeclared dependency (in place); return the system's `system_status`."""
    from app.database.models.agent_skill import AgentSkill
    from app.database.models.kb_collection import KbCollection
    from app.database.models.llm_model import LLModel
    from app.database.models.llm_provider import LlmProvider
    from app.database.models.mcp_server import MCPServer
    from app.database.models.server_config import ServerConfig

    tenant_id = system.tenant_id
    window_hours = index.window_hours
    window_start = index.now - timedelta(hours=window_hours)
    traffic, totals = own_traffic(db, tenant_id, system.id, window_start)

    def ids(key: str) -> List[str]:
        return [r["id"] for r in resources.get(key, []) if r.get("id")]

    def status(kind: str, key: Optional[str], signals: List[HealthSignal]) -> Dict[str, Any]:
        return connection_status(traffic.get((kind, key or "")), signals, window_hours=window_hours)

    model_ids = ids("models")
    models = {m.id: m for m in db.query(LLModel).filter(LLModel.id.in_(model_ids))} if model_ids else {}
    provider_ids = {m.provider_id for m in models.values() if m.provider_id}
    providers = (
        {p.id: p for p in db.query(LlmProvider).filter(LlmProvider.id.in_(provider_ids))}
        if provider_ids else {}
    )
    for r in resources.get("models", []):
        m = models.get(r["id"])
        r["connection"] = status(
            KIND_MODEL, r["id"], model_signals(m, providers.get(m.provider_id) if m else None))

    cfg_ids = ids("mcp_servers")
    configs = (
        {c.id: c for c in db.query(ServerConfig).filter(
            ServerConfig.tenant_id == tenant_id, ServerConfig.id.in_(cfg_ids))}
        if cfg_ids else {}
    )
    server_ids = {c.mcp_server_id for c in configs.values() if c.mcp_server_id}
    servers = (
        {s.id: s for s in db.query(MCPServer).filter(MCPServer.id.in_(server_ids))}
        if server_ids else {}
    )
    for r in resources.get("mcp_servers", []):
        cfg = configs.get(r["id"])
        srv = servers.get(cfg.mcp_server_id) if cfg else None
        r["connection"] = status(KIND_MCP, srv.id if srv else None, mcp_signals(cfg, srv))

    skill_ids = ids("skills")
    skills = (
        {s.id: s for s in db.query(AgentSkill).filter(AgentSkill.id.in_(skill_ids))}
        if skill_ids else {}
    )
    for r in resources.get("skills", []):
        r["connection"] = status(KIND_SKILL, r["id"], skill_signals(skills.get(r["id"])))

    kb_ids = ids("knowledge_bases")
    kbs = (
        {k.id: k for k in db.query(KbCollection).filter(KbCollection.id.in_(kb_ids))}
        if kb_ids else {}
    )
    for r in resources.get("knowledge_bases", []):
        r["connection"] = status(KIND_KB, r["id"], kb_signals(kbs.get(r["id"])))

    for d in dependencies:
        health, failing = dependency_statuses.get(d.get("id") or "", (d.get("health"), None))
        signals = delegation_signals(health, failing)
        target = d.get("depends_on_system_id")
        if target:
            st = index.pair(system.id, target)
            ev = pair_evidence(st)
            d["connection"] = connection_status(ev, signals, window_hours=window_hours)
        else:
            d["connection"] = status(KIND_A2A, d.get("agent_card_id"), signals)

    for o in observed_undeclared:
        st = index.pair(system.id, o["system_id"])
        ev = pair_evidence(st)
        o["connection"] = connection_status(ev, (), window_hours=window_hours)

    row = index.health_rows.get(system.id)
    return system_connection(
        row.health_state if row is not None else None,
        row.reasons if row is not None else None,
        totals,
        window_hours=window_hours,
    )


__all__ = [
    "CONNECTION_STATUSES",
    "DEGRADED_FAILURE_RATE",
    "DOWN_AFTER_CONSECUTIVE_FAILURES",
    "HealthSignal",
    "STATUS_DEGRADED",
    "STATUS_DOWN",
    "STATUS_IDLE",
    "STATUS_OK",
    "SYSTEM_STATE_STATUS",
    "TrafficEvidence",
    "attach_connections",
    "blocked_expr",
    "connection_keys",
    "error_expr",
    "held_expr",
    "lapsed_hold_expr",
    "hold_sorted_in_time_expr",
    "held_segment_expr",
    "lapsed_held_segment_expr",
    "refused_expr",
    "connection_status",
    "delegation_signals",
    "kb_signals",
    "mcp_signals",
    "model_signals",
    "own_traffic",
    "segment_evidence",
    "skill_signals",
    "system_connection",
    "window_phrase",
]
