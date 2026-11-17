# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

from app.env_vars import BRUTOR_GATEWAY_INTERNAL_URL, BRUTOR_GATEWAY_SERVICE_API_KEY
import enum
from typing import Literal

class ProxyType(str, enum.Enum):
    """Available Gateway Proxy types.

    WIRE CONTRACT: these values are written by the Rust core
    (`brutor-gateway-core/src/constants.rs::proxy`) into proxy_logs and read
    here. They are pinned in BOTH repos (Rust: constants.rs wire_contract tests;
    Python: tests/test_wire_contract.py). Do not query proxy_type with raw
    string literals — use these enum values so a rename stays consistent.
    """
    MCP = "mcp"
    VIRTUAL_MCP = "virtual-mcp"
    LLM = "llm"
    SKILL = "skill"
    OAUTH = "oauth"
    A2A = "a2a"
    # Written by the Enterprise gateway: the batch surface and the Portal API.
    BATCH = "batch"
    PORTAL = "portal"


# Every proxy type the gateway writes, in its order (`proxy::ALL`), and the
# type of a filter on them: a request names these and nothing else
# (`tests/test_proxy_types_are_the_gateways.py` reads the gateway's list).
PROXY_TYPES = tuple(member.value for member in ProxyType)
ProxyTypeName = Literal["mcp", "virtual-mcp", "llm", "skill", "oauth", "a2a", "batch", "portal"]


class ProxySubtype(str, enum.Enum):
    """proxy_logs.proxy_subtype values — the Rust↔Python wire contract.

    Written by the Rust core (`constants.rs::proxy_subtype`) and read here (e.g.
    agent-activity metrics filter on the SGP/authz subtypes). Mirror of the Rust
    module; pinned in both repos. Only the subtypes the control plane currently
    reads are enumerated; add others as the CP starts reading them.
    """
    AGENT_AUTHZ_BLOCK = "agent_authz_block"
    AGENT_AUTHZ_SHADOW = "agent_authz_shadow"
    SEMANTIC_POLICY_BLOCK = "semantic_policy_block"
    SEMANTIC_POLICY_SHADOW = "semantic_policy_shadow"
    APPROVAL_REQUIRED = "approval_required"
    #: An operator stopped this specific RUN. Deliberately
    #: distinct from AUTONOMY_BLOCK, which is the whole agent or AI System:
    #: "I stopped this one run" and "the system is suspended" are different
    #: answers to "why did this fail", and an operator needs to tell them
    #: apart in the audit trail. Mirrors `proxy_subtype::RUN_ABORTED` in
    #: `brutor-gateway-core/src/constants.rs`.
    RUN_ABORTED = "run_aborted"
    RESIDENCY_VIOLATION = "residency_violation"
    #: Autonomy denying a caller that is *not* an agent principal (a plain API
    #: key or human session against a suspended/restricted AI System). Agent
    #: callers land on AGENT_AUTHZ_BLOCK, where autonomy is the last gate of
    #: the agent-authz decision — kept distinct so "suspension stopped a
    #: script" and "grants stopped an agent" stay separable in the audit trail.
    AUTONOMY_BLOCK = "autonomy_block"
    #: Refusal subtypes the core's evidence writer seals as
    #: `denied` (or `engine_failure`); read here by the capture-gap and
    #: every-verdict-sealed resolvers. Mirrors `proxy_subtype::*` in
    #: `brutor-gateway-core/src/constants.rs`.
    #: A per-run ceiling (calls/tokens/cost/depth/duration) refused the step.
    RUN_CAP_REACHED = "run_cap_reached"
    #: An access-control refusal no more specific subtype names (skill not
    #: assigned / disabled / key-restricted, A2A card not bound to the group).
    ACCESS_DENIED = "access_denied"
    SKILL_QUOTA_EXCEEDED = "skill_quota_exceeded"
    SKILL_CONCURRENCY_LIMIT = "skill_concurrency_limit"
    #: A presented one-time approval token did not validate.
    APPROVAL_TOKEN_INVALID = "approval_token_invalid"
    #: A gate failed closed because its engine could not decide.
    ENGINE_FAILURE = "engine_failure"
    #: An A2A call refused BEFORE the upstream
    #: was contacted: the target is a shared service with
    #: `enforce_accepted_callers` and the calling AI System is not in its
    #: `intended_client_systems`. Read by the composition check
    #: `composition.unaccepted_caller`. The refusal is ALSO a `blocked` run
    #: segment when the refused caller declared its own edge to that card —
    #: the check reads both sources and counts such a caller once.
    #: Mirrors `proxy_subtype::UNACCEPTED_CALLER` in
    #: `brutor-gateway-core/src/constants.rs`.
    UNACCEPTED_CALLER = "unaccepted_caller"
    #: An LLM-surface governance policy refused the request before the
    #: provider was called: a request-parameter policy (chat / responses /
    #: completions / Anthropic / Gemini) or a per-mode media policy
    #: (embeddings dimension / batch size, image / speech / transcription /
    #: video whitelists and caps). Sealed `denied`. Mirrors
    #: `proxy_subtype::LLM_GOVERNANCE_BLOCK` in
    #: `brutor-gateway-core/src/constants.rs`.
    LLM_GOVERNANCE_BLOCK = "llm_governance_block"


class AgentAuthzDecision(str, enum.Enum):
    """proxy_logs.agent_authz_decision values — Rust↔Python wire contract.

    Produced by the Rust `agent_authz::Decision::as_str`
    (`constants.rs::agent_decision`) and read here in the agent-activity metrics.
    """
    ALLOW = "allow"
    DENY = "deny"
    APPROVAL_REQUIRED = "approval_required"


#: Header a client sets on its last proxy call to close the run it belongs to
#: ("explicit" finalization). Mirrored in the Rust core as
#: `routes::runs::RUN_END_HEADER`; the value lands in
#: `proxy_logs.run_end_state` and is consumed by the run ledger.
RUN_END_HEADER = "X-Brutor-Run-End"


class RunTerminalState(str, enum.Enum):
    """proxy_logs.run_end_state / ai_system_runs.terminal_state values.

    Rust↔Python wire contract: written by the Rust core
    (`services::run_ledger::terminal`) and read here. `X-Brutor-Run-End`
    accepts exactly these, plus the literal ``true`` meaning "ended, infer the
    outcome".

    ``EXHAUSTED`` is deliberately only ever set from an explicit client signal:
    from the proxy we can see that a run stopped, not whether the framework
    stopped it because it hit an iteration cap or because the client crashed.
    A run that simply goes quiet is ``ABANDONED`` and says so.
    """
    COMPLETED = "completed"
    COMPLETED_DEGRADED = "completed_degraded"
    EXHAUSTED = "exhausted"
    ERRORED = "errored"
    ABANDONED = "abandoned"
    CANCELLED = "cancelled"


class GuardrailSurface(str, enum.Enum):
    """Guardrail `surface` tokens — the Rust↔Python wire contract.

    WIRE CONTRACT: mirror of
    `brutor-gateway-core/src/constants.rs::guardrail_surface`. The control
    plane writes these strings into the guardrail `*_surfaces` JSON arrays
    and `*_per_surface` objects; the Rust proxy reads them back and matches
    them against a closed set of arms.

    Why they are pinned rather than typed inline: the proxy's per-surface
    gate answers "not enabled" for a token it does not recognise. A typo is
    therefore not an error — it silently switches every check on that surface
    OFF. Pinning the vocabulary in both repos makes a drift a test failure
    instead of a governance hole discovered in an audit.

    Only the per-mode LLM tokens are shared verbatim with the proxy's gate.
    The non-LLM surfaces are named differently on the two sides on purpose:
    the gate takes a *kind* (``mcp``, ``skill``) plus a direction, while the
    per-check ``surfaces:`` lists take the already-directional token
    (``mcp_input``, ``mcp_output``) — see ALLOWED_CHECK_SURFACES in
    `enterprise/brutor_enterprise/policy_as_code/models/policy.py`, which stays the validator of record
    for what an operator may store.
    """
    CHAT_INPUT = "chat_input"
    CHAT_OUTPUT = "chat_output"
    EMBEDDINGS_INPUT = "embeddings_input"
    IMAGE_GEN_INPUT = "image_gen_input"
    IMAGE_GEN_OUTPUT = "image_gen_output"
    AUDIO_TTS_INPUT = "audio_tts_input"
    AUDIO_TTS_OUTPUT = "audio_tts_output"
    AUDIO_STT_INPUT = "audio_stt_input"
    AUDIO_STT_OUTPUT = "audio_stt_output"
    VIDEO_GEN_INPUT = "video_gen_input"
    VIDEO_GEN_OUTPUT = "video_gen_output"
    BATCH_INPUT = "batch_input"
    BATCH_OUTPUT = "batch_output"
    MODERATION_INPUT = "moderation_input"
    #: Non-LLM gate kinds. Paired with a direction on the Rust side.
    MCP = "mcp"
    SKILL = "skill"
    A2A_INBOUND = "a2a_inbound"
    A2A_OUTBOUND = "a2a_outbound"


# ---------------------------------------------------------------------------
# Where the control plane reaches the Core Proxy
# ---------------------------------------------------------------------------

# Default for `BRUTOR_GATEWAY_INTERNAL_URL`, used by every control-plane→core call:
# LLM health probes, embeddings for KB ingestion, audit-chain verification,
# assurance compile/backtest, the governed judge, cache invalidation.
#
# Single-sourced because the ten call sites had drifted into two mutually
# exclusive defaults: nine used loopback and `knowledge_base_service` used
# `http://core:8100`. The Docker service name never resolves on a native
# `python main.py` run and loopback never leaves the control-plane
# container, so whichever way the variable was left unset, exactly one group
# broke — and the odd one out was KB ingestion, which fails in native dev
# while everything else works.
#
# Loopback wins as the default because it is the one that is right when
# nothing is configured: every containerised deployment sets the variable
# explicitly (trial compose, ECS task definitions), so the default only ever
# serves a developer running both processes on their own machine.
DEFAULT_PROXY_INTERNAL_URL = BRUTOR_GATEWAY_INTERNAL_URL.default


def proxy_internal_url() -> str:
    """Base URL of the Core Gateway, without a trailing slash."""
    return BRUTOR_GATEWAY_INTERNAL_URL.require().rstrip("/")


def gateway_service_api_key() -> str:
    """The API key the control plane presents to the Core Gateway on every
    call it makes to it; empty when none is configured."""
    return BRUTOR_GATEWAY_SERVICE_API_KEY.string() or ""
