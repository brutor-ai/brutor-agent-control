# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The settings an administrator could save that this release does not enforce.

Each one is refused at save with a sentence saying it is not available in this
release (`not_available`), wherever it can be written: the admin API, a
Policy-as-Code bundle, a copy of a document. A response does not show it as
active. `tests/test_unenforced_controls_refused.py` and its Enterprise half
hold the list.

The checks here take the JSON documents as they are stored (a resource
group's `mcp_global_limits`, `media_global_limits`, ...) and say what in them
is not available, or None. The checks of a document's structure stay with
its validator; these run beside them.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional, Tuple


def not_available(setting: str, keep: str = "leave it unset", why: str = "") -> str:
    """The sentence of a refusal: `<setting> is not available in this
    release[: why]; <keep>.`"""
    reason = f": {why}" if why else ""
    return f"{setting} is not available in this release{reason}; {keep}."


# --- MCP limits -----------------------------------------------------------------------------------
# `mcp_global_limits`, a server binding's `limits` and an MCP server's
# `tenant_limits`. The gateway enforces the execution timeout, the two
# concurrency caps and the payload size (`app/services/mcp_exec_limits.py`
# holds their rule); these it will not enforce.
MCP_UNAVAILABLE: Tuple[Tuple[str, str, str], ...] = (
    ("execution", "memory_limit_mb", "a gateway cannot bound a remote server's memory"),
    ("execution", "cpu_limit_percent", "a gateway cannot bound a remote server's CPU"),
    ("data", "max_context_window_percent", "the gateway does not measure an MCP response against a context window"),
    ("data", "max_rows_per_query", "the gateway does not count the rows of an MCP response"),
    ("data", "max_items_per_response", "the gateway does not count the items of an MCP response"),
)


def mcp_limits_problem(limits: Any) -> Optional[str]:
    if not isinstance(limits, dict):
        return None
    for section, key, why in MCP_UNAVAILABLE:
        block = limits.get(section)
        if isinstance(block, dict) and block.get(key) is not None:
            return not_available(f"MCP limit {section}.{key}", why=why)
    return None


# --- Media limits and governance -----------------------------------------------------------------

MEDIA_MODES = ("audio_speech", "audio_transcription", "image_generation", "video_generation", "embedding")
MEDIA_WARNING_KEYS = ("daily_warning_percent", "monthly_warning_percent")


def media_limits_problem(limits: Any) -> Optional[str]:
    """`media_global_limits`: the monthly keys of every mode, the warning
    percentages, `hard_stop: false`, and every key of the `embedding` mode
    (embeddings are held by the group's LLM limits)."""
    if not isinstance(limits, dict):
        return None
    for mode, block in limits.items():
        if not isinstance(block, dict):
            continue
        set_keys = [key for key, value in block.items() if value is not None]
        if mode == "embedding" and set_keys:
            return not_available(
                f"Media limit embedding.{sorted(set_keys)[0]}",
                why="embeddings are counted against the group's LLM limits, not media limits",
            )
        for key in sorted(set_keys):
            if key.startswith("monthly_") and key != "monthly_warning_percent":
                return not_available(f"Media limit {mode}.{key}", why="media limits are counted per day")
            if key in MEDIA_WARNING_KEYS:
                return not_available(f"Media limit {mode}.{key}", why="no alert is raised for media usage")
        if block.get("hard_stop") is False:
            return not_available(
                f"Media limit {mode}.hard_stop: false",
                keep="leave it unset or true",
                why="every media limit blocks when it is reached",
            )
    return None


def media_governance_problem(governance: Any) -> Optional[str]:
    if not isinstance(governance, dict):
        return None
    embedding = governance.get("embedding")
    if isinstance(embedding, dict) and embedding.get("max_input_tokens_per_item") is not None:
        return not_available(
            "Media governance embedding.max_input_tokens_per_item",
            why="the gateway does not count the tokens of an embedding input",
        )
    transcription = governance.get("audio_transcription")
    if isinstance(transcription, dict) and transcription.get("require_prompt_guardrail") is False:
        return not_available(
            "Media governance audio_transcription.require_prompt_guardrail: false",
            keep="leave it unset or true",
            why="the guardrails always check the prompt of a transcription",
        )
    return None


# --- LLM ------------------------------------------------------------------------------------------


def llm_limits_problem(limits: Any) -> Optional[str]:
    budget = limits.get("budget") if isinstance(limits, dict) else None
    if isinstance(budget, dict) and budget.get("hard_stop") is False:
        return not_available(
            "LLM budget hard_stop: false",
            keep="leave it unset or true",
            why="every LLM budget blocks when it is reached",
        )
    return None


def llm_governance_problem(governance: Any) -> Optional[str]:
    """`max_input_tokens` is enforced only by refusing: under `clamp` (the
    default) an oversize input is sent with a warning."""
    ctx = governance.get("context_window") if isinstance(governance, dict) else None
    if isinstance(ctx, dict) and ctx.get("max_input_tokens") is not None and ctx.get("enforce", "clamp") != "reject":
        return not_available(
            "context_window.max_input_tokens with enforce 'clamp'",
            keep="set enforce to 'reject'",
            why="an input cannot be shortened, so 'clamp' would only warn",
        )
    return None


# --- A2A ------------------------------------------------------------------------------------------


def a2a_limits_problem(limits: Any) -> Optional[str]:
    invocations = limits.get("invocations") if isinstance(limits, dict) else None
    if not isinstance(invocations, dict):
        return None
    for key in MEDIA_WARNING_KEYS:
        if invocations.get(key) is not None:
            return not_available(f"A2A limit invocations.{key}", why="no alert is raised for A2A invocations")
    if invocations.get("hard_stop") is False:
        return not_available(
            "A2A limit invocations.hard_stop: false",
            keep="leave it unset or true",
            why="every A2A invocation cap blocks when it is reached",
        )
    return None


# --- Single settings ------------------------------------------------------------------------------

SKILL_POLICY = not_available(
    "A skill's own policy (tenant_governance)",
    keep="leave it empty",
    why=(
        "the gateway applies a group's skill governance, the skill's metadata and its "
        "assignment to a group, not a policy on the skill itself"
    ),
)

ASSET_ENFORCEMENT = not_available(
    "An AI asset enforcement mode other than 'off'",
    keep="leave it 'off'",
    why="the gateway does not read the asset register",
)

WORKFLOW_BUILDER = not_available(
    "The skill workflow builder",
    keep="run a skill through the gateway",
    why="a saved workflow is not run",
)


def first(problems: Iterable[Optional[str]]) -> Optional[str]:
    return next((problem for problem in problems if problem), None)
