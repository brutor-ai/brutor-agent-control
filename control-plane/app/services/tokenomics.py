# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Tokenomics — Big-T workload classification over the run ledger

Implements the Tokenomics Foundation's Big-T notation ("Big-O for
tokens") as a computed, first-class metric. Their central complaint is
that the multipliers deciding AI cost — model calls per task (k), agent
delegation depth (a) — are *hidden*, revealing themselves only on
invoices. Brutor's run ledger records both per task, so the class is
not estimated here; it is **measured**:

    T(n · k · a)   n = tasks (runs) · k = model calls per task
                   a = agent delegation depth

## Classification rules (documented, tested, deliberately simple)

Evaluated over the standard 30-day judgment window, in precedence
order:

1. **T(n·k·a)** — delegation is a real behavior, not an accident:
   ≥ `DELEGATION_SHARE_THRESHOLD` of finished runs delegated to
   another agent (a2a calls or delegation depth ≥ 1).
2. **T(n·k)** — the loop multiplies: median model calls per task
   (k_median) > `K_LINEAR_THRESHOLD`.
3. **T(1)** — cache-dominated: ≥ `CACHE_DOMINANT_SHARE` of actions
   were cache hits. Checked *after* the multiplicative classes: a
   chatty loop with a good cache is still a chatty loop.
4. **T(n)** — one-ish model call per task. The healthy default.

`T(log n)` (deterministic pre-filtering shrinking input before
inference) is **not classified**: retrieval shrinkage happens inside
the client's pipeline where the gateway cannot observe it. We class
what we measure, and say so.

`T(∞)` is likewise never asserted, but for a different reason, and the
distinction matters because the platform's honest claim is narrower
than "we prevent it":

* Agent **depth** is genuinely bounded. The A2A path enforces a hard
  `ABSOLUTE_MAX_DEPTH` plus a configurable `max_delegation_depth`, so
  the `a` term cannot run away.
* Loop **iteration** is bounded only by what an operator configured —
  per-group call, token and spend quotas, checked inline on every
  path. There is no per-run action cap: `action_count` is a rollup
  computed at finalization, not a live counter anything gates on. And
  dollar caps only block when `hard_stop` is set (it defaults off);
  call and token caps always do.
* Idle timeouts and envelopes are **not** enforcement. The idle
  timeout closes an abandoned run in the ledger, and a run spinning
  flat out is never idle. Envelopes are evaluated after the fact and
  raise findings.

So an unbounded loop shows up as a run with a large action count and a
breached envelope — which is a finding, not a class. Do not describe
T(∞) as structurally impossible.

## The explainers live here on purpose

Operator-facing explanation strings for every tokenomics concept ship
from this module (`EXPLAINERS`), served through the cards API and
rendered as info boxes in the console — and reusable by the Omni
metric layer later. One definition, every surface: a dashboard tooltip
and a chat answer must never describe the same term differently.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.services.ai_system_outcomes import RUN_NO_WORK, class_cond
from app.database.models.ai_system_run import AISystemRun

# ---------------------------------------------------------------------------
# Classes and thresholds
# ---------------------------------------------------------------------------

T_CONSTANT = "T(1)"
T_LINEAR = "T(n)"
T_MULTIPLICATIVE = "T(n·k)"
T_AGENTIC = "T(n·k·a)"

#: Worst-first rank, used by the worst-of subtree rollup.
T_CLASS_RANK: Dict[str, int] = {
    T_CONSTANT: 0,
    T_LINEAR: 1,
    T_MULTIPLICATIVE: 2,
    T_AGENTIC: 3,
}

#: Median model calls per task above which a workload stops being
#: "one call per task" in any honest reading. 2 rather than 1 because
#: a single follow-up (classify → answer) is ordinary linear practice.
K_LINEAR_THRESHOLD = 2.0

#: Share of finished runs that delegated (a2a / depth ≥ 1) for the
#: agentic class. 5%: below that, delegation is incidental; above it,
#: it is how the system works.
DELEGATION_SHARE_THRESHOLD = 0.05

#: Share of actions served from cache for the constant class.
CACHE_DOMINANT_SHARE = 0.5

#: Judgment window (hours) — the same 30-day standard the verdict uses.
#: The class on a card describes *current behavior*, never "all time".
CLASSIFICATION_WINDOW_HOURS = 720

#: Below this many finished runs the class is not asserted at all —
#: a class computed over a handful of runs is noise wearing notation.
MIN_RUNS_FOR_CLASS = 20


def classify_workload(
    db: Session,
    tenant_id: str,
    ai_system_id: str,
    *,
    window_hours: int = CLASSIFICATION_WINDOW_HOURS,
) -> Dict[str, Any]:
    """Measure the Big-T class from the run ledger.

    Returns {t_class, k_median, a_max, delegation_share, cache_share,
    runs} with t_class None when there is not enough evidence
    (MIN_RUNS_FOR_CLASS) — absence of evidence is never a class.
    """
    since = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    base = [
        AISystemRun.tenant_id == tenant_id,
        AISystemRun.ai_system_id == ai_system_id,
        AISystemRun.is_replay.is_(False),
        AISystemRun.terminal_state.isnot(None),
        AISystemRun.started_at >= since,
        # A declared "nothing to do" run is no-work overhead, not
        # the workload: twenty empty polls must not make k look like 0.
        ~class_cond(RUN_NO_WORK),
    ]

    totals = (
        db.query(
            func.count(AISystemRun.id),
            func.sum(AISystemRun.action_count),
            func.sum(AISystemRun.cache_hit_count),
            func.max(AISystemRun.max_delegation_depth),
        )
        .filter(*base)
        .first()
    )
    runs, actions, cache_hits, a_max = (
        int(totals[0] or 0),
        int(totals[1] or 0),
        int(totals[2] or 0),
        int(totals[3] or 0),
    )

    if runs < MIN_RUNS_FOR_CLASS:
        return {
            "t_class": None,
            "k_median": None,
            "a_max": a_max,
            "delegation_share": None,
            "cache_share": None,
            "runs": runs,
        }

    delegated = (
        db.query(func.count(AISystemRun.id))
        .filter(*base)
        .filter(
            (AISystemRun.a2a_call_count > 0)
            | (AISystemRun.max_delegation_depth >= 1)
        )
        .scalar()
        or 0
    )
    delegation_share = delegated / runs

    # Portable median (works on SQLite and Postgres alike): order by the
    # value and take the middle row. One extra indexed query beats a
    # dialect fork.
    k_median = (
        db.query(AISystemRun.llm_call_count)
        .filter(*base)
        .order_by(AISystemRun.llm_call_count)
        .offset(runs // 2)
        .limit(1)
        .scalar()
        or 0
    )
    cache_share = (cache_hits / actions) if actions else 0.0

    if delegation_share >= DELEGATION_SHARE_THRESHOLD:
        t_class = T_AGENTIC
    elif float(k_median) > K_LINEAR_THRESHOLD:
        t_class = T_MULTIPLICATIVE
    elif cache_share >= CACHE_DOMINANT_SHARE:
        t_class = T_CONSTANT
    else:
        t_class = T_LINEAR

    return {
        "t_class": t_class,
        "k_median": float(k_median),
        "a_max": a_max,
        "delegation_share": round(delegation_share, 4),
        "cache_share": round(cache_share, 4),
        "runs": runs,
    }


def worst_class(*classes: Optional[str]) -> Optional[str]:
    """Worst-of for subtree rollups. None entries contribute nothing."""
    ranked = [c for c in classes if c in T_CLASS_RANK]
    if not ranked:
        return None
    return max(ranked, key=lambda c: T_CLASS_RANK[c])


# ---------------------------------------------------------------------------
# Operator-facing explainers — one definition, every surface
# ---------------------------------------------------------------------------

#: The full Big-T guide, opened from the dashboard's "Workload classes"
#: button. Info boxes stay terse and assume the operator has read this.
WORKLOAD_GUIDE: Dict[str, Any] = {
    "title": "Workload classes \u2014 the T(n\u00b7k\u00b7a) guide",
    "sections": [
        {
            "body": (
                "The workload classes on these cards come from Big-T "
                "Notation, the first published project of the Tokenomics "
                "Foundation \u2014 a vendor-neutral standards body hosted "
                "by the Linux Foundation, launched in early August 2026 "
                "with 29 founding organizations. The framework was "
                "authored by Dan Neff at Adobe and published in draft on "
                "the Foundation's site."
            ),
        },
        {
            "title": "The core idea",
            "body": (
                "Big-O notation never gave you an algorithm's exact "
                "runtime \u2014 it told you how runtime grows as input "
                "scales. Big-T applies the same discipline to tokens, "
                "classifying workloads by how consumption grows as usage "
                "scales, complexity increases, or autonomy expands. It is "
                "deliberately informal \u2014 no proofs, no master "
                "theorem \u2014 a shared vocabulary letting engineers, "
                "architects and finance teams reason about cost "
                "trajectories before committing to an architecture. The "
                "notation is T(n \u00b7 k \u00b7 a):"
            ),
            "points": [
                (
                    "n \u2014 requests or input size: how many requests "
                    "hit the workload and how large each input is; either "
                    "or both can be the scaling dimension."
                ),
                (
                    "k \u2014 model calls per request: reasoning steps, "
                    "multi-turn chains, tool calls that replay context. "
                    "The trap is that k is usually invisible \u2014 it "
                    "rarely appears in the request itself."
                ),
                (
                    "a \u2014 agent depth: sub-agents calling sub-agents, "
                    "where an orchestrator spawning workers multiplies "
                    "everything below it in the tree."
                ),
            ],
        },
        {
            "title": "Why these letters \u2014 T, n, k, a",
            "body": (
                "T is not time. In classical algorithm analysis T(n) "
                "denotes an algorithm's running time on an input of size "
                "n \u2014 the function Big-O bounds. Big-T keeps the "
                "letter but swaps the quantity: T means tokens consumed, "
                "borrowed for its Big-O heritage \u2014 and it happens "
                "to initial the new unit."
            ),
            "points": [
                (
                    "n is as standard as symbols get: the canonical "
                    "input-size variable across mathematics and CS, the "
                    "letter complexity notation is written in by default "
                    "\u2014 O(n), O(n log n), O(n\u00b2). \"How much "
                    "is coming in\" required no invention."
                ),
                (
                    "k is the conventional second parameter, used "
                    "wherever a problem has a secondary count that isn't "
                    "the input size \u2014 k-nearest neighbours, top-k "
                    "retrieval, parameterized complexity O(f(k)\u00b7"
                    "n^c). O(nk) is already a familiar shape, so "
                    "T(n\u00b7k) reads immediately as \"input size "
                    "times a per-item repetition factor.\""
                ),
                (
                    "a is the one invented letter \u2014 a mnemonic for "
                    "agent rather than prior notation (tree-shaped work "
                    "is classically O(b^d): branching factor and depth). "
                    "Note the liberty that comes with it: an "
                    "orchestration tree of depth a with branching factor "
                    "b has b^a leaves, so agent depth is genuinely "
                    "exponential \u2014 writing it as a multiplied third "
                    "term understates it, which is why the framework "
                    "calls this class \"the O(n\u00b2) of AI.\""
                ),
            ],
        },
        {
            "body": (
                "A theorist would add that k and a are usually bounded "
                "\u2014 a fixed reasoning budget, a max delegation depth "
                "\u2014 which makes them constants that strict Big-O "
                "would discard, collapsing everything to O(n). The "
                "framework's answer is that constants are bounded and "
                "reversible while growth is neither. So read "
                "T(n\u00b7k\u00b7a) as a multivariate cost expression "
                "borrowing complexity notation's letters for their "
                "communicative value, not an asymptotic bound in the "
                "formal sense. (The letter lineage above is our reading "
                "\u2014 the Foundation doesn't state it.)"
            ),
        },
        {
            "title": "The complexity ladder",
            "points": [
                (
                    "T(1) \u2014 Constant. The model isn't called per "
                    "request at all: a cache hit, a static lookup, an "
                    "embedding search \u2014 reuse of a precomputed "
                    "answer. Fix: cache and precompute aggressively."
                ),
                (
                    "T(log n) \u2014 Sublinear. Deterministic code "
                    "shrinks the input before the model sees it \u2014 "
                    "the class most organizations miss entirely. RAG done "
                    "right; code as the new cache layer. Published results "
                    "include SQL views cutting clinical data from roughly "
                    "240,000 tokens to 19,000 pre-inference, and retrieval "
                    "pipelines removing better than 99.9% of a "
                    "million-token corpus."
                ),
                (
                    "T(n) \u2014 Linear. One model call per request, cost "
                    "proportional to input size \u2014 summarize, answer, "
                    "translate. The healthy default, and what most budgets "
                    "implicitly assume. Watch for fixed per-call overhead "
                    "from large tool catalogs."
                ),
                (
                    "T(n\u00b7k) \u2014 Multiplicative. k model calls "
                    "per request, with k usually invisible. Extended "
                    "thinking can burn over a hundred thousand tokens "
                    "while producing a few hundred visible ones, and "
                    "non-composable tools that replay full context each "
                    "turn can make a five-step chain cost 10\u201320\u00d7 "
                    "the composed version. Fix: compose tool pipelines, "
                    "instrument for hidden k."
                ),
                (
                    "T(n\u00b7k\u00b7a) \u2014 Agent-multiplicative. An "
                    "orchestrator spawns sub-agents that spawn tool calls "
                    "\u2014 described as the O(n\u00b2) of AI: "
                    "architecturally powerful, economically dangerous if "
                    "unmonitored. The danger isn't delegation itself but "
                    "unbounded depth and unscoped context running "
                    "unwatched. Fix: bound depth, add circuit breakers and "
                    "budgets."
                ),
                (
                    "T(\u221e) \u2014 Unbounded. Loops with no "
                    "termination condition \u2014 uncapped "
                    "retry-on-failure, recursive self-rewriting \u2014 "
                    "which the framework notes exist today in production "
                    "systems lacking circuit breakers. Fix: hard "
                    "termination conditions, always."
                ),
            ],
        },
        {
            "title": "How the classes are meant to be used",
            "body": (
                "Two questions, in strict order: first, what class is "
                "this workload \u2014 an architecture decision that only "
                "architecture changes; second, how cheaply does it run "
                "within its class, which is where compression, routing, "
                "caching and serialization live. Classes are compared at "
                "a stated quality floor \u2014 quality is a precondition "
                "of the comparison, not a term in it. The governing "
                "question is whether a workload's class is justified by "
                "the value it produces. A higher class isn't wrong; it's "
                "a cost behaviour to weigh against return \u2014 a "
                "frontier agent shipping strategic features can earn its "
                "class, a convenience bot usually can't. The practical "
                "sequence: get token-level visibility first, classify "
                "your highest-spend workloads by reading the architecture "
                "rather than the invoice, change the class where "
                "architecture allows, then optimise within the class and "
                "govern."
            ),
            "points": [
                (
                    "A worked example summarising ten white papers four "
                    "ways drops from T(n\u00b7k) at $3.04 to T(n) at "
                    "$0.09 \u2014 over 97% reduction, roughly 34\u00d7, "
                    "from isolated contexts, right-sized model routing and "
                    "bounded output templates rather than any discount."
                ),
                (
                    "Architecture comparison: standard tool calls as "
                    "baseline T(n\u00b7k); code-generation mode running "
                    "one script in a sandbox at ~80% reduction and T(n); "
                    "filtered retrieval at 92\u201399.9% reduction and "
                    "T(log n)."
                ),
            ],
        },
        {
            "title": "Where a control plane fits",
            "body": (
                "The Foundation's five-layer stack puts L5 \u2014 "
                "routing and governance \u2014 at the top as the control "
                "plane in front of everything else: which model serves "
                "each request, budgets and quotas, guardrails on agent "
                "depth and retries \u2014 the only layer that can decide "
                "a request needs no model at all. Big-T's fifth lever is "
                "workload classification and governance itself, framed as "
                "alignment rather than restriction: tying the "
                "highest-consumption workloads to the highest-value "
                "outcomes. Big-T also names the skill pattern explicitly "
                "\u2014 a portable folder of instructions and scripts "
                "loaded only when relevant, so you pay for the "
                "instructions once instead of carrying them on every call."
            ),
        },
        {
            "title": "How Brutor measures your class",
            "body": (
                "Brutor doesn't estimate the class \u2014 it measures it "
                "from the run ledger: k is the median model calls per "
                "finished task, a is the maximum delegation depth, over "
                "the standard 30-day window. Fewer than 20 recent runs "
                "shows no class rather than a guess. T(log n) is never "
                "asserted \u2014 retrieval shrinkage happens inside your "
                "pipeline where the gateway cannot observe it. T(\u221e) "
                "is never asserted either: delegation depth is hard-capped "
                "by the gateway, and per-group call, token and spend "
                "quotas stop a runaway loop in flight \u2014 but only "
                "where you configured them, and dollar caps block only "
                "with hard stop enabled. An unbounded loop therefore "
                "arrives as a run with a large action count and a breached "
                "envelope: a finding, not a class. Two dashboard rules "
                "follow: "
                "windows scale measurements, never judgments (the class "
                "is pinned to the 30-day standard, so there is no "
                "cherry-picked window in which a troubled system looks "
                "\"all-time healthy\"); and rollups are worst-of, never "
                "averages (a department over nine healthy systems and one "
                "in violation says Violations)."
            ),
        },
        {
            "body": (
                "The full paper is at tokeneconomics.com; the assessment "
                "checklist and pricing calculator are listed as still in "
                "development."
            ),
        },
    ],
}

#: Shared footer on every per-class info box.
_CLASS_FOOTER = (
    "Measured from this system's run ledger over the 30-day window "
    "\u2014 not estimated. See the Workload classes guide for the full "
    "ladder."
)

EXPLAINERS: Dict[str, Any] = {
    "t_class": {
        "title": "Big-T workload class",
        "body": (
            "How this system's cost grows as usage scales (Tokenomics "
            "Foundation Big-T Notation), measured from its run ledger. On "
            "group cards the chip shows the worst class in the subtree. "
            "Fewer than 20 recent runs shows no class rather than a "
            "guess. Open the Workload classes guide at the top of the "
            "page for the full ladder."
        ),
    },
    "t_class::T(1)": {
        "title": "T(1) \u2014 Constant",
        "body": (
            "The model isn't being called per task: cache hits and reuse "
            "of precomputed answers dominate this system's actions "
            "(\u226550% served from cache), so cost stays near-constant "
            "as usage grows. Keep the cache binding healthy \u2014 a "
            "T(1) that loses its cache becomes whatever class the loop "
            "underneath really is."
        ),
        "sections": [{"body": _CLASS_FOOTER}],
    },
    "t_class::T(n)": {
        "title": "T(n) \u2014 Linear",
        "body": (
            "About one model call per finished task (median k \u2264 2), "
            "no significant delegation: cost grows with usage, "
            "predictably. The healthy default, and what most budgets "
            "implicitly assume. Watch fixed per-call overhead from large "
            "tool catalogs \u2014 it's the quiet way a linear workload "
            "gets expensive."
        ),
        "sections": [{"body": _CLASS_FOOTER}],
    },
    "t_class::T(n\u00b7k)": {
        "title": "T(n\u00b7k) \u2014 Multiplicative",
        "body": (
            "This system makes several model calls per task \u2014 k is "
            "the median, shown on the card, and it's the multiplier that "
            "never appears on a provider invoice: reasoning steps, "
            "multi-turn chains, tools replaying context each turn. Worth "
            "checking the loop is composed rather than replaying full "
            "context \u2014 a five-step chain can cost 10\u201320\u00d7 "
            "its composed version."
        ),
        "sections": [{"body": _CLASS_FOOTER}],
    },
    "t_class::T(n\u00b7k\u00b7a)": {
        "title": "T(n\u00b7k\u00b7a) \u2014 Agent-multiplicative",
        "body": (
            "This system delegates to other agents \u2014 a is the "
            "maximum depth, shown on the card \u2014 so an orchestrator "
            "multiplies everything below it. Architecturally powerful, "
            "economically dangerous if unmonitored; the danger isn't "
            "delegation itself but unbounded depth and unscoped context. "
            "Keep depth bounded and budgets and circuit breakers on "
            "\u2014 which is exactly what this gateway enforces."
        ),
        "sections": [{"body": _CLASS_FOOTER}],
    },
    "cost_per_task": {
        "title": "Cost per completed task",
        "body": (
            "Total spend divided by tasks that actually finished "
            "(completed or degraded) — the number a budget conversation "
            "turns on, and one provider dashboards cannot give you "
            "because they price API calls, not outcomes. When the "
            "system's contract declares a signed bound, it is shown "
            "beside the value and turns red when breached. At 'All time' "
            "this is a blended figure across eras."
        ),
    },
    "two_projections": {
        "title": "Spend vs unit economics",
        "body": (
            "Two views of the same money. Spend is the FinOps "
            "projection: what you were charged this window. Unit "
            "economics is the tokenomics projection: what a finished "
            "task costs and how that cost grows. They can move in "
            "opposite directions — spend up while efficiency improves — "
            "and both be right; the finding lives in the space between "
            "them, which is why they are shown together."
        ),
    },
    "worst_of": {
        "title": "Worst-of rollups",
        "body": (
            "Group cards aggregate their subtree: spend, runs and tokens "
            "are sums, but health, verdict and workload class are the "
            "WORST in the subtree, never an average. Nine healthy "
            "systems and one in violation reads Violations — averaging "
            "is how a dashboard reports a comfortable number for a "
            "subtree nobody should be comfortable about."
        ),
    },
    "findings": {
        "title": "Open findings",
        "body": (
            "Unresolved problems on record: drift findings, contract "
            "(envelope) breaches, liveness findings — anything with "
            "status active or acknowledged. Resolved and dismissed "
            "findings never count. The badge color follows the worst "
            "open severity. Click the badge to triage: on a system card "
            "it opens that system's Drift tab; on a group card it opens "
            "the alerts panel for the subtree."
        ),
    },
    "unattributed": {
        "title": "Unattributed runs",
        "body": (
            "Governed traffic that matched no AI System. It is recorded "
            "— never silently dropped — and surfaced here as a to-do "
            "list: attribute it to a system, or register the system it "
            "belongs to."
        ),
    },
    "observed_plane": {
        "title": "Observed, not governed",
        "body": (
            "Usage imported from provider enterprise APIs (ChatGPT, "
            "Claude, Copilot seats). It is costed and attributed in the "
            "same ledger as routed traffic and lands on the Resource "
            "Group it belongs to — but only observed: no guardrail, "
            "policy or budget was enforced on it. The card says how much "
            "of its spend that is, so the total never reads as governed."
        ),
    },
}


__all__ = [
    "CACHE_DOMINANT_SHARE",
    "CLASSIFICATION_WINDOW_HOURS",
    "DELEGATION_SHARE_THRESHOLD",
    "EXPLAINERS",
    "K_LINEAR_THRESHOLD",
    "MIN_RUNS_FOR_CLASS",
    "T_AGENTIC",
    "T_CLASS_RANK",
    "T_CONSTANT",
    "T_LINEAR",
    "T_MULTIPLICATIVE",
    "WORKLOAD_GUIDE",
    "classify_workload",
    "worst_class",
]
