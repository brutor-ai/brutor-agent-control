# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Measurement changes — platform upgrades that change how a metric is MEASURED

A drift finding says "this AI System behaves differently". When the platform
itself changes how a metric is computed — a new definition, a new source
column, a new normalisation — every system's samples shift on the day the
upgrade lands, and the detector cannot tell that apart from the agent
changing. On the Brutor Demo System, a release changed a run's client label
from the raw User-Agent (`python-httpx/0.28.1`) to its product name
(`python-httpx`); `client_mix` drift fired three times and a response policy
demoted the system once — for a change nobody made to the agent.

**Rule: a change that alters how a metric is measured must add an entry
here.** The control plane applies each entry
once per installation, at startup after migrations
(`app/services/measurement_changes.apply_pending`): every AI System that has
a baseline for one of the entry's metrics (on one of its bases) gets a
baseline epoch of kind `measurement_change` starting at the moment the entry
applied, and `platform_measurement_changes` records that it did. The core
honours the epoch like any other (the baseline re-learns from post-change
samples, so the shift cannot fire), and the assurance report lists the epoch
with its reason.

The core pins what "how a metric is measured" means: the sample queries, the
per-metric extraction and the client-label derivation are hashed by
`drift::measurement` (`tests` there), and the hash is tied to the newest
entry id here (`MEASUREMENT_REGISTRY_HEAD`, pinned in both repos). Changing a
metric's definition without a new entry fails the core's test; the
control-plane test `tests/test_measurement_changes.py` fails if the two
heads disagree.

APPEND-ONLY: never edit or remove an entry once released (an installation
may already have applied it); a correction is a new entry.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple


@dataclass(frozen=True)
class MeasurementChange:
    #: Stable, sortable id (`mNNNN_<slug>`, ≤ 64 chars).
    id: str
    #: Core metric keys (`drift::metric`) whose measurement changed.
    metric_keys: Tuple[str, ...]
    #: Baseline bases affected (`runs`, `segments`).
    bases: Tuple[str, ...]
    #: Shown on the epoch, in the console and in the assurance report.
    reason: str
    #: The platform release that changed the measurement.
    introduced_in_version: str


REGISTRY: Tuple[MeasurementChange, ...] = (
    MeasurementChange(
        id="m0001_client_label_product",
        metric_keys=("client_mix",),
        bases=("runs",),
        reason=(
            "A run's client label became the X-Brutor-Client header, else the "
            "User-Agent's product name without its version (previously the raw User-Agent, "
            "e.g. 'python-httpx/0.28.1'). client_mix is measured differently; what called "
            "the system did not change."
        ),
        introduced_in_version="0.11.1",
    ),
    MeasurementChange(
        id="m0002_declared_outcomes",
        metric_keys=(
            "action_count", "cost_per_run", "duration_ms", "tool_call_count",
            "max_delegation_depth", "tool_error_rate", "exhaustion_rate", "tool_mix",
            "terminal_state_mix", "model_mix", "first_prompt_tokens", "input_bytes",
            "client_mix", "outcome_mix", "error_cause_mix",
        ),
        bases=("runs", "segments"),
        reason=(
            "Runs are judged on declared outcomes — bucket 'all' excludes no-work "
            "runs, continuous metrics are also learned per outcome id, a gateway refusal is "
            "'errored' (not 'blocked_policy') and a refused delegation is a 'failed' segment "
            "(not 'blocked')."
        ),
        introduced_in_version="0.11.28",
    ),
    MeasurementChange(
        id="m0003_step_clock_start",
        metric_keys=("duration_ms",),
        bases=("runs", "segments"),
        reason=(
            "A run's and a segment's clock now start when the first call STARTED "
            "(its proxy-log stamp minus its duration) instead of at that call's completion "
            "stamp. Measured stamp to stamp, every one-call run and segment read 0 ms and "
            "every longer one was short by its first call. duration_ms is measured "
            "differently; how long the system works did not change."
        ),
        introduced_in_version="0.11.30",
    ),
    MeasurementChange(
        id="m0004_held_delegations",
        metric_keys=(
            "action_count", "llm_call_count", "tool_call_count", "total_tokens",
            "cost_per_run", "duration_ms", "tool_mix", "model_mix",
            "terminal_state_mix", "error_rate", "tool_error_rate",
        ),
        bases=("segments",),
        reason=(
            "A delegation stopped by a hold for a person is no longer a sample of the "
            "service it was sent to: it is governance waiting on someone, not the "
            "service's work. A hold on the service's inbound side used to be stored as "
            "the service failing to answer and counted in its baselines; a hold before "
            "the call left now has a segment of its own, kept out of them too. The "
            "service's behaviour did not change."
        ),
        introduced_in_version="0.11.31",
    ),
    MeasurementChange(
        id="m0005_first_prompt_context",
        metric_keys=("first_prompt_tokens",),
        bases=("runs",),
        reason=(
            "The size of the task as posed now counts cached tokens on every provider. "
            "Anthropic reports them beside the prompt, so a cached system prompt used to "
            "read as a much smaller task there, and shrink as caching improved. It also "
            "skips embeddings and response-cache hits, which never put the task in front "
            "of a model. first_prompt_tokens is measured differently; the tasks did not change."
        ),
        introduced_in_version="0.11.31",
    ),
)

#: The newest entry — the core pins the same id beside the hash of its
#: metric definitions (`drift::measurement::MEASUREMENT_REGISTRY_HEAD`).
MEASUREMENT_REGISTRY_HEAD = REGISTRY[-1].id

#: Who a measurement-change epoch is declared by (`declared_by`).
DECLARED_BY_PLATFORM = "platform"

__all__ = [
    "DECLARED_BY_PLATFORM",
    "MEASUREMENT_REGISTRY_HEAD",
    "MeasurementChange",
    "REGISTRY",
]
