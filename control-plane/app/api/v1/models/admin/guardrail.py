# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Guardrail Pydantic Models for API Requests/Responses
With tenant isolation and comprehensive content policy configuration
"""

from typing import Optional, List, Dict, Any, Literal
from pydantic import BaseModel, Field, field_validator, ConfigDict

from app.constants.guardrail_surfaces import GUARDRAIL_CHECK_SURFACES, unknown_surfaces_message


# =============================================================================
# Shared Validators
# =============================================================================

ALLOWED_ACTIONS = ["block", "redact", "warn", "allow"]
# Surfaces accepted by the test endpoint. Includes the per-mode LLM
# tokens (chat_input, embeddings_input, …) plus the non-LLM surface
# kinds (mcp, skill) which still take a direction.
ALLOWED_SURFACES = [
    # Per-mode LLM surfaces — direction encoded in the name.
    "chat_input", "chat_output",
    "embeddings_input",
    "image_gen_input", "image_gen_output",
    "audio_tts_input", "audio_tts_output",
    "audio_stt_input", "audio_stt_output",
    "video_gen_input", "video_gen_output",
    "batch_input", "batch_output",
    "moderation_input",
    # Non-LLM surface kinds — paired with a direction.
    "mcp", "skill",
    # A2A surfaces — direction encoded in the name.
    "a2a_inbound", "a2a_outbound",
]
ALLOWED_DIRECTIONS = ["input", "output"]
ALLOWED_PROVIDERS = [
    "lakera", "openai_moderation", "presidio", "bedrock", "custom"
]

# Per-check provider selectors. Must match the wire constants used
# by the Rust orchestrator (services/guardrails/orchestrator.rs).
ALLOWED_PII_PROVIDERS = ["built_in", "presidio"]
ALLOWED_PROMPT_INJECTION_PROVIDERS = ["built_in", "lakera_guard"]
ALLOWED_JAILBREAK_PROVIDERS = ["built_in", "lakera_guard"]
ALLOWED_TOXIC_CONTENT_PROVIDERS = ["openai_moderation", "built_in"]

# 12 categories the built-in PII detector ships with. Empty / null
# selection runs all of them.
ALLOWED_PII_CATEGORIES = [
    "email", "phone", "ssn", "credit_card", "iban",
    "ipv4", "ipv6",
    "openai_api_key", "anthropic_api_key", "aws_access_key",
    "jwt", "private_key",
]

# OpenAI Moderation categories. Per-category threshold maps must
# only key on names from this list.
ALLOWED_TOXIC_CATEGORIES = [
    "hate", "hate/threatening",
    "harassment", "harassment/threatening",
    "self-harm", "self-harm/intent", "self-harm/instructions",
    "sexual", "sexual/minors",
    "violence", "violence/graphic",
]

ALLOWED_BANNED_MATCH_MODES = ["substring", "word_boundary"]
# `banned::EntryAction`: the action of one entry of a banned list.
ALLOWED_BANNED_ENTRY_ACTIONS = ["block", "warn"]

# The surfaces a check can be put on: the names the gateway asks a check's
# surface list for (`app.constants.guardrail_surfaces`).
ALLOWED_CHECK_SURFACES = list(GUARDRAIL_CHECK_SURFACES)

# Secrets categories. Must match the
# Rust `SecretsDetector::known_categories()` exactly. Adding a
# category here without the Rust pair means the request validates
# but the runtime silently no-ops; adding it to Rust without
# Python means saves are rejected.
ALLOWED_SECRETS_CATEGORIES = [
    "aws_access_key",
    "aws_secret_key",
    "github_token",
    "openai_key",
    "gcp_service_account",
    "private_key_pem",
    "jwt",
    "generic_high_entropy",
]

# Per-check fail-open override keys. Must mirror the Rust check
# names so the orchestrator binding picks the override up.
ALLOWED_FAIL_OPEN_OVERRIDE_KEYS = [
    "pii", "prompt_injection", "jailbreak", "toxic_content",
    "banned_words", "banned_patterns", "secrets",
]


def _validate_action(v: Optional[str]) -> Optional[str]:
    """Validate action is in the allowed list."""
    if v is None:
        return v
    if v.lower() not in ALLOWED_ACTIONS:
        raise ValueError(f"Action must be one of: {', '.join(ALLOWED_ACTIONS)}")
    return v.lower()


# Per-check allow-lists for the keys an override entry may carry.
# Provider is omitted for secrets / banned_*. Threshold is meaningful
# for toxic_content (per-category model scores), secrets (forward-
# compat for a Presidio-style provider), and PII (forwarded to
# Presidio's /analyze.score_threshold; built-in regex ignores it).
# Words/patterns only for banned_*. The validator below rejects
# unknown keys per check so a typo in the admin UI gets caught at
# save time, not at runtime.
_PER_SURFACE_ALLOWED_KEYS = {
    "pii":               {"enabled", "action", "provider", "threshold"},
    "prompt_injection":  {"enabled", "action", "provider"},
    "jailbreak":         {"enabled", "action", "provider"},
    "toxic_content":     {"enabled", "action", "provider", "threshold"},
    "secrets":           {"enabled", "action", "threshold"},
    "banned_words":      {"enabled", "action", "words"},
    "banned_patterns":   {"enabled", "action", "patterns"},
}


_PER_SURFACE_PROVIDERS = {
    "pii": ALLOWED_PII_PROVIDERS,
    "prompt_injection": ALLOWED_PROMPT_INJECTION_PROVIDERS,
    "jailbreak": ALLOWED_JAILBREAK_PROVIDERS,
    "toxic_content": ALLOWED_TOXIC_CONTENT_PROVIDERS,
}


def _banned_pattern_problem(pattern: Any) -> Optional[str]:
    """Why a banned pattern cannot be saved; `None` for one that can.

    The gateway compiles banned patterns with the Rust crate `regex-lite`,
    which has no look-around, no backreferences and no Unicode classes; one
    it cannot compile is a check that cannot be built.
    `app/services/guardrails/regex_lite.py` says what it
    compiles. The control plane's own guardrail test runs the same
    patterns with Python's `re`, so a pattern is one both compile."""
    import re

    from app.services.guardrails import regex_lite

    problem = regex_lite.problem(pattern)
    if problem:
        return (
            f"the gateway cannot compile it: {problem}. Banned patterns are compiled with the Rust "
            "regex-lite syntax, which has no look-around, backreferences or Unicode classes"
        )
    try:
        re.compile(pattern)
    except re.error as e:
        return f"the control plane cannot compile it for its own guardrail test (Python re): {e}"
    return None


def _validate_banned_entries(entries: Any, key: str, where: str) -> None:
    """The entries of a banned list as the gateway reads them
    (`orchestrator.rs`: `parse_banned_words`, `parse_banned_patterns`): a
    string, or an object with `word` / `pattern` (a string) and an optional
    `action`. Anything else it counts as an entry it cannot read, and the
    check as one that cannot be built."""
    if not isinstance(entries, list):
        raise ValueError(f"{where} must be a list")
    for i, entry in enumerate(entries):
        if isinstance(entry, dict):
            value = entry.get(key)
            if "action" in entry and entry["action"] is not None:
                if entry["action"] not in ALLOWED_BANNED_ENTRY_ACTIONS:
                    raise ValueError(
                        f"{where}: the action of the entry at index {i} must be one of: "
                        + ", ".join(ALLOWED_BANNED_ENTRY_ACTIONS)
                    )
        else:
            value = entry
        if not isinstance(value, str):
            raise ValueError(
                f"{where}: the entry at index {i} must be a string or an object with a '{key}' string"
            )
        if key == "pattern":
            problem = _banned_pattern_problem(value)
            if problem:
                raise ValueError(f"{where}: invalid regex pattern at index {i}: {value!r} — {problem}")


def _validate_per_surface_override(
    v: Optional[Dict[str, Dict[str, Any]]],
    check_name: str,
) -> Optional[Dict[str, Dict[str, Any]]]:
    """Shape + content validator for a `<check>_per_surface` JSONB.

    Rejects:
      - surface tokens outside ALLOWED_CHECK_SURFACES
      - keys outside the per-check allow-list (catches typos like
        `provdier` and rejects `provider` on secrets where it's
        ignored at runtime — better to fail loud than silently
        no-op)
      - non-dict values
      - actions outside ALLOWED_ACTIONS

    None and empty dict are accepted — both mean "no overrides".
    """
    if v is None:
        return v
    if not isinstance(v, dict):
        raise ValueError(f"{check_name}_per_surface must be an object")

    allowed_keys = _PER_SURFACE_ALLOWED_KEYS[check_name]
    cleaned: Dict[str, Dict[str, Any]] = {}
    for surface_token, override in v.items():
        if surface_token not in ALLOWED_CHECK_SURFACES:
            raise ValueError(
                f"{check_name}_per_surface: unknown surface token "
                f"{surface_token!r}. Allowed: {', '.join(ALLOWED_CHECK_SURFACES)}"
            )
        if not isinstance(override, dict):
            raise ValueError(
                f"{check_name}_per_surface[{surface_token!r}] must be an object"
            )
        unknown = set(override.keys()) - allowed_keys
        if unknown:
            raise ValueError(
                f"{check_name}_per_surface[{surface_token!r}]: unknown keys "
                f"{sorted(unknown)}. Allowed: {sorted(allowed_keys)}"
            )
        if "action" in override:
            override["action"] = _validate_action(override["action"])
        # The per-surface override of a provider selector is a selector:
        # one the gateway does not know is a check it cannot evaluate.
        if override.get("provider") is not None and override["provider"] not in _PER_SURFACE_PROVIDERS[check_name]:
            raise ValueError(
                f"{check_name}_per_surface[{surface_token!r}].provider must be one of: "
                + ", ".join(_PER_SURFACE_PROVIDERS[check_name])
            )
        for key, entry_key in (("words", "word"), ("patterns", "pattern")):
            if override.get(key) is not None:
                _validate_banned_entries(
                    override[key], entry_key, f"{check_name}_per_surface[{surface_token!r}].{key}"
                )
        cleaned[surface_token] = override
    return cleaned


# =============================================================================
# Nested Configuration Models (used in both Request and Response)
# =============================================================================

class GuardrailSurfaceToggles(BaseModel):
    """Surface-level enable/disable toggles.

    One switch per surface: each kind of model request (chat, embeddings,
    images, audio and so on) and the MCP, skill and A2A surfaces. Defaults match the DB column defaults so a partial PATCH
    (omitting the field entirely) leaves the existing value untouched,
    but a fresh POST that sends the full block lands the operator's
    intent verbatim.
    """

    # Per-mode LLM surfaces. Each token matches a single
    # operational route in the Rust proxy — see SURFACE_METADATA in
    # the admin UI for the human labels.
    chat_input: bool = Field(True, description="Scan user prompts before chat/completions/responses/messages")
    chat_output: bool = Field(False, description="Scan model responses before returning to client; streamed responses follow streaming_output_mode")
    embeddings_input: bool = Field(True, description="Scan documents before embedding")
    image_gen_input: bool = Field(True, description="Scan prompts before image generation")
    image_gen_output: bool = Field(False, description="Scan generated images (reserved for future NSFW classifier)")
    audio_tts_input: bool = Field(True, description="Scan text before voice synthesis")
    audio_tts_output: bool = Field(False, description="Scan synthesised audio (reserved for future NSFW classifier)")
    audio_stt_input: bool = Field(True, description="Scan optional context prompt before transcription")
    audio_stt_output: bool = Field(True, description="Scan transcribed text")
    video_gen_input: bool = Field(True, description="Scan prompts before video generation")
    video_gen_output: bool = Field(False, description="Scan generated video (reserved for future NSFW classifier)")
    batch_input: bool = Field(True, description="Scan inputs of async batch jobs")
    batch_output: bool = Field(False, description="Scan outputs of async batch jobs")
    moderation_input: bool = Field(
        True,
        description=(
            "Scan moderation request bodies. Jailbreak / injection / "
            "toxic-content are auto-bypassed at the orchestrator on this "
            "surface — running safety classifiers against a moderation "
            "call recurses. PII + secrets still fire if enabled."
        ),
    )

    # MCP / skill / A2A — non-LLM surfaces.
    mcp_input: bool = Field(True, description="Check MCP tool call arguments")
    mcp_output: bool = Field(True, description="Check MCP tool call results")
    mcp_registration: bool = Field(
        True,
        description=(
            "Scan tool descriptions from MCP servers' tools/list "
            "responses for prompt-injection payloads at server "
            "registration and on every catalog refresh."
        ),
    )
    skill_input: bool = Field(True, description="Check skill execution inputs")
    skill_output: bool = Field(True, description="Check skill script output and template renders")
    a2a_inbound: bool = Field(
        False,
        description=(
            "Check incoming A2A v1.0 message bodies (tasks.send / "
            "message.send) against this guardrail. Default off — "
            "operator must opt the policy in once they want to "
            "scan inbound delegation traffic."
        ),
    )
    a2a_outbound: bool = Field(
        False,
        description=(
            "Check outgoing A2A delegations (the payload Brutor sends "
            "when one of its hosted agents calls a peer agent). On "
            "block the local caller receives a v1.0 PolicyBlocked "
            "structured error and the peer never sees the request."
        ),
    )


class GuardrailBuiltinConfig(BaseModel):
    """Configuration for built-in regex-based content checks"""

    # PII Detection
    pii_enabled: bool = Field(True, description="Enable PII detection (email, phone, SSN, credit card, API keys)")
    pii_action: str = Field("redact", description="Action for PII violations")
    pii_provider: Optional[str] = Field(
        None,
        description=(
            "PII detector provider. 'built_in' (default when null) runs the "
            "in-process 12-category regex detector. 'presidio' calls a "
            "self-hosted Microsoft Presidio Analyzer — requires a Presidio "
            "entry in providers[] with a non-empty endpoint."
        ),
    )
    pii_categories: Optional[List[str]] = Field(
        None,
        description=(
            "Opt-in subset of PII categories to run. Null/empty runs all 12: "
            + ", ".join(ALLOWED_PII_CATEGORIES)
        ),
    )
    # Confidence threshold for the Presidio analyzer path.
    # Forwarded as `/analyze.score_threshold`. The built-in regex
    # detector ignores this field — there is no continuous score to
    # compare against (matches are deterministic regex + plausibility
    # checks). Null = use Presidio's default (typically 0.5).
    pii_threshold: Optional[float] = Field(
        None,
        ge=0.0, le=1.0,
        description=(
            "Confidence threshold (0.0–1.0) for the Presidio PII "
            "analyzer. Lower = more matches at higher false-positive "
            "rate. Ignored when pii_provider='built_in' (regex detector "
            "has no score). Null falls back to Presidio's own default."
        ),
    )

    # Prompt Injection
    prompt_injection_enabled: bool = Field(True, description="Enable prompt injection detection")
    prompt_injection_action: str = Field("block", description="Action for prompt injection violations")
    prompt_injection_sensitivity: float = Field(
        0.7,
        ge=0.0, le=1.0,
        description="Sensitivity threshold (0.0–1.0). Lower = more aggressive detection."
    )
    prompt_injection_provider: Optional[str] = Field(
        None,
        description="'built_in' (default) runs the pattern + heuristic ensemble. 'lakera_guard' requires a configured Lakera api_key in providers JSON.",
    )

    # Jailbreak
    jailbreak_enabled: bool = Field(True, description="Enable jailbreak attempt detection")
    jailbreak_action: str = Field("block", description="Action for jailbreak violations")
    jailbreak_provider: Optional[str] = Field(
        None,
        description="'built_in' (default) runs the persona + token-smuggling pattern bank. 'lakera_guard' reuses the Lakera client with a jailbreak filter.",
    )

    # Toxic Content
    toxic_content_enabled: bool = Field(False, description="Enable toxic content detection")
    toxic_content_action: str = Field("warn", description="Action for toxic content violations")
    toxic_content_threshold: float = Field(
        0.8,
        ge=0.0, le=1.0,
        description="Toxicity confidence threshold (0.0–1.0)"
    )
    toxic_content_provider: Optional[str] = Field(
        None,
        description="'openai_moderation' (default) calls /v1/moderations. 'built_in' falls back to the profanity wordlist.",
    )
    toxic_content_category_thresholds: Optional[Dict[str, float]] = Field(
        None,
        description=(
            "Per-category threshold map for OpenAI Moderation, e.g. "
            '{"hate":0.7,"harassment":0.8}. Empty/null falls back to '
            "toxic_content_threshold for every category."
        ),
    )

    # Custom Rules
    banned_words: List[str] = Field(default_factory=list, description="Words/phrases to block")
    banned_patterns: List[str] = Field(default_factory=list, description="Regex patterns to block")
    banned_words_match_mode: Optional[str] = Field(
        None,
        description="'substring' (default, back-compat) or 'word_boundary' (avoids Scunthorpe-problem false positives).",
    )

    # Per-check surface granularity. Null means
    # "use the migration default". The orchestrator intersects each
    # set with the config-level surface flags so an operator can't
    # accidentally fire a check on a surface the config doesn't even
    # cover. Empty list means the check fires on no surfaces
    # (effectively off) without losing the rest of its config.
    pii_surfaces: Optional[List[str]] = Field(
        None,
        description="Surfaces on which PII detection fires. Subset of " + ", ".join(ALLOWED_CHECK_SURFACES) + ".",
    )
    prompt_injection_surfaces: Optional[List[str]] = Field(
        None,
        description="Surfaces on which prompt-injection detection fires (typically input-side only).",
    )
    jailbreak_surfaces: Optional[List[str]] = Field(
        None,
        description="Surfaces on which jailbreak detection fires (typically input-side only).",
    )
    toxic_content_surfaces: Optional[List[str]] = Field(
        None,
        description="Surfaces on which toxic-content detection fires.",
    )
    banned_words_surfaces: Optional[List[str]] = Field(
        None,
        description="Surfaces on which banned-word matching fires.",
    )
    banned_patterns_surfaces: Optional[List[str]] = Field(
        None,
        description="Surfaces on which banned-pattern matching fires.",
    )

    # Secrets detection.
    # Off by default; opt-in per tenant.
    secrets_enabled: bool = Field(False, description="Enable secrets detection (AWS keys, GitHub tokens, OpenAI keys, GCP service accounts, PEM private keys, JWTs).")
    secrets_action: str = Field("redact", description="Action for secrets violations.")
    secrets_categories: Optional[List[str]] = Field(
        None,
        description=(
            "Opt-in subset of secrets categories. Null falls back to the "
            "7 deterministic detectors (excludes generic_high_entropy "
            "which has the highest FP rate). Allowed: "
            + ", ".join(ALLOWED_SECRETS_CATEGORIES)
        ),
    )
    secrets_surfaces: Optional[List[str]] = Field(
        None,
        description="Surfaces on which secrets detection fires.",
    )
    secrets_threshold: float = Field(
        0.8, ge=0.0, le=1.0,
        description=(
            "Provider confidence threshold. Reserved for the Presidio "
            "extension; the built-in detector is deterministic and "
            "ignores this value."
        ),
    )

    # ------------------------------------------------------------------
    # Per-surface overrides
    # ------------------------------------------------------------------
    # Each `<check>_per_surface` field is an optional map keyed by the
    # 8 surface tokens. Entries override the scalar fields above on a
    # per-surface basis. Missing surface = use scalar default; missing
    # field in an entry = use scalar default for that field.
    #
    # Per-check accepted keys:
    #   pii / prompt_injection / jailbreak:
    #       {enabled?, action?, provider?}
    #   toxic_content:
    #       {enabled?, action?, provider?, threshold?}
    #   secrets:
    #       {enabled?, action?, threshold?}     (provider ignored)
    #   banned_words:
    #       {enabled?, action?, words?}         (words REPLACE global list)
    #   banned_patterns:
    #       {enabled?, action?, patterns?}      (patterns REPLACE global list)
    #
    # Surface tokens that are unknown OR forbidden for this check are
    # rejected by the validator at the bottom of this class.
    pii_per_surface: Optional[Dict[str, Dict[str, Any]]] = Field(
        None,
        description=(
            "Per-surface overrides for PII. Shape: "
            "{surface_token: {enabled?, action?, provider?}}."
        ),
    )
    prompt_injection_per_surface: Optional[Dict[str, Dict[str, Any]]] = Field(
        None,
        description="Per-surface overrides for prompt-injection.",
    )
    jailbreak_per_surface: Optional[Dict[str, Dict[str, Any]]] = Field(
        None,
        description="Per-surface overrides for jailbreak.",
    )
    toxic_content_per_surface: Optional[Dict[str, Dict[str, Any]]] = Field(
        None,
        description=(
            "Per-surface overrides for toxic-content. "
            "Accepts {enabled?, action?, provider?, threshold?}."
        ),
    )
    secrets_per_surface: Optional[Dict[str, Dict[str, Any]]] = Field(
        None,
        description=(
            "Per-surface overrides for secrets. Accepts "
            "{enabled?, action?, threshold?}. `provider` is ignored — "
            "secrets has only the built-in detector today."
        ),
    )
    banned_words_per_surface: Optional[Dict[str, Dict[str, Any]]] = Field(
        None,
        description=(
            "Per-surface overrides for banned-words. Accepts "
            "{enabled?, action?, words?}. `words` REPLACES the global "
            "banned_words list for that surface (override, not merge)."
        ),
    )
    banned_patterns_per_surface: Optional[Dict[str, Dict[str, Any]]] = Field(
        None,
        description=(
            "Per-surface overrides for banned-patterns. Accepts "
            "{enabled?, action?, patterns?}. `patterns` REPLACES the "
            "global banned_patterns list for that surface."
        ),
    )

    # Validators
    @field_validator('pii_action', 'prompt_injection_action', 'jailbreak_action', 'toxic_content_action', 'secrets_action')
    @classmethod
    def validate_action(cls, v: str) -> str:
        return _validate_action(v)

    # Per-surface override validators. One per check so the
    # error message names the right field; under the hood they all
    # delegate to _validate_per_surface_override with the matching
    # check name (which gates which keys are allowed in the entry).
    @field_validator('pii_per_surface')
    @classmethod
    def _v_pii_per_surface(cls, v): return _validate_per_surface_override(v, "pii")

    @field_validator('prompt_injection_per_surface')
    @classmethod
    def _v_pi_per_surface(cls, v): return _validate_per_surface_override(v, "prompt_injection")

    @field_validator('jailbreak_per_surface')
    @classmethod
    def _v_jb_per_surface(cls, v): return _validate_per_surface_override(v, "jailbreak")

    @field_validator('toxic_content_per_surface')
    @classmethod
    def _v_toxic_per_surface(cls, v): return _validate_per_surface_override(v, "toxic_content")

    @field_validator('secrets_per_surface')
    @classmethod
    def _v_secrets_per_surface(cls, v): return _validate_per_surface_override(v, "secrets")

    @field_validator('banned_words_per_surface')
    @classmethod
    def _v_bw_per_surface(cls, v): return _validate_per_surface_override(v, "banned_words")

    @field_validator('banned_patterns_per_surface')
    @classmethod
    def _v_bp_per_surface(cls, v): return _validate_per_surface_override(v, "banned_patterns")

    @field_validator(
        'pii_surfaces',
        'prompt_injection_surfaces',
        'jailbreak_surfaces',
        'toxic_content_surfaces',
        'banned_words_surfaces',
        'banned_patterns_surfaces',
        'secrets_surfaces',
    )
    @classmethod
    def _validate_check_surfaces(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        if v is None:
            return v
        bad = [s for s in v if s not in ALLOWED_CHECK_SURFACES]
        if bad:
            raise ValueError(unknown_surfaces_message(bad))
        seen = set()
        deduped = []
        for s in v:
            if s not in seen:
                seen.add(s)
                deduped.append(s)
        return deduped

    @field_validator('secrets_categories')
    @classmethod
    def _validate_secrets_categories(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        if v is None:
            return v
        bad = [c for c in v if c not in ALLOWED_SECRETS_CATEGORIES]
        if bad:
            raise ValueError(
                f"Unknown secrets categories: {', '.join(bad)}. "
                f"Allowed: {', '.join(ALLOWED_SECRETS_CATEGORIES)}"
            )
        # De-dupe preserving order — operators sometimes paste a
        # category list with duplicates from copy-paste.
        seen = set()
        deduped = []
        for c in v:
            if c not in seen:
                seen.add(c)
                deduped.append(c)
        return deduped

    @field_validator('pii_provider')
    @classmethod
    def _validate_pii_provider(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if v not in ALLOWED_PII_PROVIDERS:
            raise ValueError(
                f"pii_provider must be one of: {', '.join(ALLOWED_PII_PROVIDERS)}"
            )
        return v

    @field_validator('prompt_injection_provider')
    @classmethod
    def _validate_pi_provider(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if v not in ALLOWED_PROMPT_INJECTION_PROVIDERS:
            raise ValueError(
                "prompt_injection_provider must be one of: "
                + ", ".join(ALLOWED_PROMPT_INJECTION_PROVIDERS)
            )
        return v

    @field_validator('jailbreak_provider')
    @classmethod
    def _validate_jb_provider(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if v not in ALLOWED_JAILBREAK_PROVIDERS:
            raise ValueError(
                "jailbreak_provider must be one of: "
                + ", ".join(ALLOWED_JAILBREAK_PROVIDERS)
            )
        return v

    @field_validator('toxic_content_provider')
    @classmethod
    def _validate_tc_provider(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if v not in ALLOWED_TOXIC_CONTENT_PROVIDERS:
            raise ValueError(
                "toxic_content_provider must be one of: "
                + ", ".join(ALLOWED_TOXIC_CONTENT_PROVIDERS)
            )
        return v

    @field_validator('pii_categories')
    @classmethod
    def _validate_pii_categories(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        if v is None:
            return v
        bad = [c for c in v if c not in ALLOWED_PII_CATEGORIES]
        if bad:
            raise ValueError(
                f"Unknown PII categories: {', '.join(bad)}. "
                f"Allowed: {', '.join(ALLOWED_PII_CATEGORIES)}"
            )
        # de-dupe, preserve order
        seen = set()
        deduped = []
        for c in v:
            if c not in seen:
                seen.add(c)
                deduped.append(c)
        return deduped

    @field_validator('toxic_content_category_thresholds')
    @classmethod
    def _validate_tc_thresholds(cls, v: Optional[Dict[str, float]]) -> Optional[Dict[str, float]]:
        if v is None:
            return v
        for cat, thr in v.items():
            if cat not in ALLOWED_TOXIC_CATEGORIES:
                raise ValueError(
                    f"Unknown toxic content category {cat!r}. "
                    f"Allowed: {', '.join(ALLOWED_TOXIC_CATEGORIES)}"
                )
            if not isinstance(thr, (int, float)):
                raise ValueError(
                    f"Threshold for {cat!r} must be a number"
                )
            if thr < 0.0 or thr > 1.0:
                raise ValueError(
                    f"Threshold for {cat!r} must be in [0.0, 1.0]"
                )
        return v

    @field_validator('banned_words_match_mode')
    @classmethod
    def _validate_match_mode(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if v not in ALLOWED_BANNED_MATCH_MODES:
            raise ValueError(
                "banned_words_match_mode must be one of: "
                + ", ".join(ALLOWED_BANNED_MATCH_MODES)
            )
        return v

    @field_validator('banned_patterns')
    @classmethod
    def validate_patterns(cls, v: List[str]) -> List[str]:
        """Each pattern is one the gateway can compile."""
        for i, pattern in enumerate(v):
            problem = _banned_pattern_problem(pattern)
            if problem:
                raise ValueError(f"Invalid regex pattern at index {i}: {pattern!r} — {problem}")
        return v


class GuardrailProviderConfig(BaseModel):
    """Configuration for an external guardrail provider.

    A provider has no `mode` and no `surfaces` of its own: a standalone
    provider (`custom`, `bedrock`) runs on every surface the guardrail
    configuration enables, and a per-check provider (`lakera`,
    `openai_moderation`, `presidio`) runs where its check's own surface
    list says. A stored entry that still carries either key is read
    without them.
    """

    model_config = {"extra": "ignore"}

    provider: str = Field(
        ...,
        description="Provider identifier: lakera, openai_moderation, presidio, bedrock, custom"
    )
    enabled: bool = Field(True, description="Whether this provider is active")
    api_key: Optional[str] = Field(None, description="Provider API key (encrypted at rest)")
    endpoint: Optional[str] = Field(None, description="Provider endpoint URL")
    config: Optional[Dict[str, Any]] = Field(
        None,
        description="Provider-specific configuration (e.g. language, score_threshold)"
    )

    @field_validator('provider')
    @classmethod
    def validate_provider(cls, v: str) -> str:
        if v.lower() not in ALLOWED_PROVIDERS:
            raise ValueError(f"Provider must be one of: {', '.join(ALLOWED_PROVIDERS)}")
        return v.lower()


# =============================================================================
# Request Models
# =============================================================================

class GuardrailConfigUpdate(BaseModel):
    """
    Request model for updating an existing guardrail configuration.

    This is a full replacement — all fields are applied on save.
    """

    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = Field(None, min_length=1, max_length=255, description="Human-readable name")
    description: Optional[str] = Field(None, max_length=1024, description="Admin-facing description")
    is_default: Optional[bool] = Field(None, description="Whether this is the tenant default config")
    priority: Optional[int] = Field(None, ge=0, le=1000, description="Priority (higher = runs first)")
    group_ids: Optional[List[str]] = Field(None, description="Resource group IDs to associate with")
    # Per-call scope filters. Apply only on calls
    # matching the named MCP server and/or tool name. NULL = applies
    # to all calls.
    scope_mcp_server_id: Optional[str] = Field(
        None,
        description="If set, this config only applies to calls on this MCP server (per-tool guardrail scope).",
    )
    scope_tool_name: Optional[str] = Field(
        None, max_length=255,
        description="If set, this config only applies to tools/call where params.name matches.",
    )
    enabled: bool = Field(False, description="Master switch — False disables ALL guardrails for this config")
    surfaces: Optional[GuardrailSurfaceToggles] = Field(None, description="Surface-level toggles")
    streaming_output_mode: Optional[Literal["auto", "sync", "async"]] = Field(
        None,
        description=(
            "How output guardrails behave on STREAMED (SSE) responses. "
            "'auto' (default): sync buffer-and-release when a redact "
            "output check is active (redaction works on streams; adds "
            "~one segment of latency), else async-interrupt (block-only, "
            "fastest). 'sync': always buffer-and-release. 'async': always "
            "interrupt-on-block. Non-streaming output is unaffected."
        ),
    )
    builtin: Optional[GuardrailBuiltinConfig] = Field(None, description="Built-in check configuration")
    providers: Optional[List[GuardrailProviderConfig]] = Field(
        None,
        description="External guardrail provider configurations"
    )
    max_check_timeout_ms: float = Field(
        2000.0,
        ge=100.0, le=30000.0,
        description="Max time to wait for guardrail checks (ms). A timeout is treated as a check failure, so fail_open decides: refuse (default) or log-and-allow."
    )
    fail_open: bool = Field(
        False,
        description=(
            "What happens when a check cannot run (provider error or timeout). "
            "False (default) refuses the call — a control that silently stopped "
            "running is worse than a refused request, and it is invisible in the "
            "assurance report. True logs the failure and allows the call."
        ),
    )
    fail_open_overrides: Optional[Dict[str, bool]] = Field(
        None,
        description=(
            "Per-check override of fail_open. Keys: "
            + ", ".join(ALLOWED_FAIL_OPEN_OVERRIDE_KEYS)
            + ". Values: true (fail-open) or false (fail-closed)."
        ),
    )
    log_all_checks: bool = Field(
        False,
        description="Log all guardrail checks, not just violations. Useful for debugging."
    )

    @field_validator('fail_open_overrides', mode='before')
    @classmethod
    def _validate_fail_open_overrides(cls, v):
        if v is None:
            return v
        if not isinstance(v, dict):
            raise ValueError("fail_open_overrides must be an object")
        for key, val in v.items():
            if key not in ALLOWED_FAIL_OPEN_OVERRIDE_KEYS:
                raise ValueError(
                    f"Unknown fail_open_overrides key {key!r}. "
                    f"Allowed: {', '.join(ALLOWED_FAIL_OPEN_OVERRIDE_KEYS)}"
                )
            # Reject non-bool values *before* Pydantic coerces them.
            if not isinstance(val, bool):
                raise ValueError(
                    f"fail_open_overrides[{key!r}] must be bool"
                )
        return v


class BreakGlassActivateRequest(BaseModel):
    """Declare a guardrail outage window.

    `reason` is required and non-trivial on purpose. An unexplained
    break-glass is indistinguishable from a misconfiguration when someone
    reviews the audit trail three months later, and the whole value of this
    record over a hardcoded fail-open is that it carries the why.
    """

    reason: str = Field(
        ...,
        min_length=10,
        max_length=2000,
        description=(
            "Why guardrails are being degraded to log-and-allow. Recorded on "
            "the audit trail and surfaced in the Assurance Report."
        ),
    )
    duration_minutes: Optional[int] = Field(
        None,
        ge=1,
        description=(
            "How long the window lasts. Defaults to 60 minutes and is clamped "
            "to the 8-hour maximum — clamped rather than rejected, because an "
            "operator mid-incident should get a valid window, not a 422."
        ),
    )


class GuardrailConfigCreateRequest(BaseModel):
    """
    Request model for creating a new guardrail configuration.
    Requires name; other fields have sensible defaults.
    """
    name: str = Field(..., min_length=1, max_length=255, description="Human-readable name (unique within tenant)")
    description: Optional[str] = Field(None, max_length=1024, description="Admin-facing description")
    is_default: bool = Field(False, description="Whether this is the tenant default config")
    priority: int = Field(0, ge=0, le=1000, description="Priority (higher = runs first)")
    group_ids: Optional[List[str]] = Field(None, description="Resource group IDs to associate with")
    # Per-call scope filters (see GuardrailConfigUpdate)
    scope_mcp_server_id: Optional[str] = Field(
        None,
        description="If set, this config only applies to calls on this MCP server.",
    )
    scope_tool_name: Optional[str] = Field(
        None, max_length=255,
        description="If set, this config only applies to tools/call where params.name matches.",
    )
    enabled: bool = Field(False, description="Master switch — False disables ALL guardrails for this config")
    surfaces: Optional[GuardrailSurfaceToggles] = Field(None, description="Surface-level toggles")
    streaming_output_mode: Optional[Literal["auto", "sync", "async"]] = Field(
        None,
        description=(
            "How output guardrails behave on STREAMED (SSE) responses. "
            "'auto' (default): sync buffer-and-release when a redact "
            "output check is active (redaction works on streams; adds "
            "~one segment of latency), else async-interrupt (block-only, "
            "fastest). 'sync': always buffer-and-release. 'async': always "
            "interrupt-on-block. Non-streaming output is unaffected."
        ),
    )
    builtin: Optional[GuardrailBuiltinConfig] = Field(None, description="Built-in check configuration")
    providers: Optional[List[GuardrailProviderConfig]] = Field(
        None,
        description="External guardrail provider configurations"
    )
    max_check_timeout_ms: float = Field(2000.0, ge=100.0, le=30000.0)
    fail_open: bool = Field(False)
    fail_open_overrides: Optional[Dict[str, bool]] = Field(
        None,
        description="Per-check override of fail_open (see GuardrailConfigUpdate for schema).",
    )
    log_all_checks: bool = Field(False)

    @field_validator('fail_open_overrides', mode='before')
    @classmethod
    def _validate_fail_open_overrides(cls, v):
        if v is None:
            return v
        if not isinstance(v, dict):
            raise ValueError("fail_open_overrides must be an object")
        for key, val in v.items():
            if key not in ALLOWED_FAIL_OPEN_OVERRIDE_KEYS:
                raise ValueError(
                    f"Unknown fail_open_overrides key {key!r}. "
                    f"Allowed: {', '.join(ALLOWED_FAIL_OPEN_OVERRIDE_KEYS)}"
                )
            # Reject non-bool values *before* Pydantic coerces them.
            if not isinstance(val, bool):
                raise ValueError(
                    f"fail_open_overrides[{key!r}] must be bool"
                )
        return v


class GuardrailGroupAssociationRequest(BaseModel):
    """Request model for setting resource group associations"""

    model_config = ConfigDict(extra="forbid")
    group_ids: List[str] = Field(..., description="Resource group IDs to associate with this config")


class GuardrailTestRequest(BaseModel):
    """Request model for testing content against configured guardrails"""
    content: str = Field(
        ...,
        min_length=1,
        max_length=100000,
        description="Content to check against guardrails"
    )
    surface: str = Field(
        "chat_input",
        description=(
            "Surface to simulate. Per-mode LLM tokens (chat_input, chat_output, "
            "embeddings_input, …) encode direction in the name; non-LLM kinds "
            "(mcp, skill) take a direction."
        ),
    )
    direction: str = Field(
        "input",
        description=(
            "Direction: input or output. Ignored when `surface` is a per-mode "
            "token that already encodes direction."
        ),
    )
    config_id: Optional[str] = Field(
        None,
        description="Specific guardrail config ID to test against. "
                    "When provided, bypasses user/group resolution and tests "
                    "this config directly (even if disabled). "
                    "When omitted, falls back to tenant default resolution."
    )

    @field_validator('surface')
    @classmethod
    def validate_surface(cls, v: str) -> str:
        if v.lower() not in ALLOWED_SURFACES:
            raise ValueError(f"Surface must be one of: {', '.join(ALLOWED_SURFACES)}")
        return v.lower()

    @field_validator('direction')
    @classmethod
    def validate_direction(cls, v: str) -> str:
        if v.lower() not in ALLOWED_DIRECTIONS:
            raise ValueError(f"Direction must be one of: {', '.join(ALLOWED_DIRECTIONS)}")
        return v.lower()


# =============================================================================
# Response Models — Nested
# =============================================================================

class GuardrailSurfaceResponse(BaseModel):
    """Surface toggles in response.

    Mirrors `GuardrailSurfaceToggles` (request) — every `*_enabled`
    column on the `guardrail_configs` table comes back here. Defaults
    cover legacy rows that predate a column's existence, so the field
    is always present in API responses even without a backfill.
    """

    # Per-mode LLM surfaces.
    chat_input: bool = True
    chat_output: bool = False
    embeddings_input: bool = True
    image_gen_input: bool = True
    image_gen_output: bool = False
    audio_tts_input: bool = True
    audio_tts_output: bool = False
    audio_stt_input: bool = True
    audio_stt_output: bool = True
    video_gen_input: bool = True
    video_gen_output: bool = False
    batch_input: bool = True
    batch_output: bool = False
    moderation_input: bool = True

    # MCP / skill / A2A.
    mcp_input: bool = True
    mcp_output: bool = True
    # Older configs predate the registration-time scan; default True so
    # the field is always present in API responses without needing a
    # back-population migration.
    mcp_registration: bool = True
    skill_input: bool = True
    skill_output: bool = True
    a2a_inbound: bool = False
    a2a_outbound: bool = False


class GuardrailPiiResponse(BaseModel):
    """PII detection settings in response"""
    enabled: bool
    action: str
    provider: str = "built_in"
    categories: Optional[List[str]] = None
    # Presidio /analyze.score_threshold. NULL means "use
    # Presidio's default"; ignored on built-in regex provider.
    threshold: Optional[float] = None
    surfaces: List[str] = Field(default_factory=list)
    # Per-surface override map. NULL = no overrides.
    per_surface: Optional[Dict[str, Dict[str, Any]]] = None


class GuardrailPromptInjectionResponse(BaseModel):
    """Prompt injection detection settings in response"""
    enabled: bool
    action: str
    sensitivity: Optional[float] = None
    surfaces: List[str] = Field(default_factory=list)
    provider: str = "built_in"
    per_surface: Optional[Dict[str, Dict[str, Any]]] = None


class GuardrailJailbreakResponse(BaseModel):
    """Jailbreak detection settings in response"""
    enabled: bool
    action: str
    provider: str = "built_in"
    surfaces: List[str] = Field(default_factory=list)
    per_surface: Optional[Dict[str, Dict[str, Any]]] = None


class GuardrailToxicContentResponse(BaseModel):
    """Toxic content detection settings in response"""
    enabled: bool
    action: str
    threshold: Optional[float] = None
    provider: str = "openai_moderation"
    category_thresholds: Optional[Dict[str, float]] = None
    surfaces: List[str] = Field(default_factory=list)
    per_surface: Optional[Dict[str, Dict[str, Any]]] = None


class GuardrailSecretsResponse(BaseModel):
    """Secrets detection settings in response"""
    enabled: bool
    action: str
    categories: List[str] = Field(default_factory=list)
    surfaces: List[str] = Field(default_factory=list)
    threshold: Optional[float] = None
    per_surface: Optional[Dict[str, Dict[str, Any]]] = None


class GuardrailBuiltinResponse(BaseModel):
    """Built-in checks in response"""
    pii: GuardrailPiiResponse
    prompt_injection: GuardrailPromptInjectionResponse
    jailbreak: GuardrailJailbreakResponse
    toxic_content: GuardrailToxicContentResponse
    banned_words: List[str] = Field(default_factory=list)
    banned_patterns: List[str] = Field(default_factory=list)
    banned_words_match_mode: str = "substring"
    banned_words_surfaces: List[str] = Field(default_factory=list)
    banned_patterns_surfaces: List[str] = Field(default_factory=list)
    # Per-surface word/pattern overrides at the builtin level
    # (banned_words_config / banned_patterns_config carry these in
    # policy-as-code; the response surface lifts them up next to the
    # global list so the admin UI can preload them.)
    banned_words_per_surface: Optional[Dict[str, Dict[str, Any]]] = None
    banned_patterns_per_surface: Optional[Dict[str, Dict[str, Any]]] = None
    secrets: Optional[GuardrailSecretsResponse] = None


class GuardrailProviderResponse(BaseModel):
    """A provider entry as it is answered: without `api_key` and without
    the secrets of `config`. A provider has no `mode` and no `surfaces` of
    its own: a standalone provider (`custom`, `bedrock`) runs on every
    surface the configuration enables, a per-check provider where its
    check's surface list says."""

    model_config = ConfigDict(extra="forbid")

    provider: str = Field(..., description="Provider identifier: lakera, openai_moderation, presidio, bedrock, custom")
    enabled: bool
    endpoint: Optional[str] = None
    config: Optional[Dict[str, Any]] = Field(
        None, description="The provider's settings, without its secrets (see `config_secrets_stored`)."
    )
    config_secrets_stored: Dict[str, bool] = Field(
        default_factory=dict,
        description=(
            "For each secret of `config` (aws_secret_access_key, aws_session_token): whether a value "
            "is stored. The values are never returned; a save that leaves one out keeps it."
        ),
    )
    # Note: api_key and the secrets of `config` are NOT included in the response


class GuardrailGroupInfo(BaseModel):
    """Minimal group info for guardrail config responses"""
    id: str
    name: str
    display_name: Optional[str] = None
    parent_group_id: Optional[str] = None
    is_active: Optional[bool] = None


# =============================================================================
# Full Configuration Response
# =============================================================================

class GuardrailConfigResponse(BaseModel):
    """
    Response model for guardrail configuration.
    Includes all fields except secrets.
    """
    id: str
    tenant_id: str
    name: str = Field("Default Guardrails", description="Human-readable name")
    description: Optional[str] = Field(None, description="Admin-facing description")
    is_default: bool = Field(False, description="Whether this is the tenant default config")
    priority: int = Field(0, description="Priority (higher = runs first)")
    # Per-call scope filters surfaced on responses so
    # the admin UI can show "this guardrail only runs on tool X" at
    # a glance.
    scope_mcp_server_id: Optional[str] = Field(
        None, description="If set, this config only applies to calls on this MCP server."
    )
    scope_tool_name: Optional[str] = Field(
        None, description="If set, this config only applies to tools/call where params.name matches."
    )
    group_ids: List[str] = Field(default_factory=list, description="Associated resource group IDs")
    groups: List[GuardrailGroupInfo] = Field(default_factory=list, description="Associated resource group details")
    streaming_output_mode: str = Field("auto", description="Streaming output guardrail posture: auto | sync | async")
    enabled: bool

    surfaces: GuardrailSurfaceResponse
    builtin: GuardrailBuiltinResponse
    providers: List[GuardrailProviderResponse] = Field(default_factory=list)

    max_check_timeout_ms: float
    fail_open: bool
    fail_open_overrides: Optional[Dict[str, bool]] = None
    log_all_checks: bool

    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    class Config:
        from_attributes = True


# =============================================================================
# Detail Response Wrapper (matches LLModelDetailResponse pattern)
# =============================================================================

class GuardrailConfigDetailResponse(BaseModel):
    """Response model for the guardrail config detail"""
    config: GuardrailConfigResponse = Field(..., description="Guardrail configuration")


class GuardrailConfigListResponse(BaseModel):
    """Response model for listing multiple guardrail configurations"""
    configs: List[GuardrailConfigResponse] = Field(default_factory=list, description="Guardrail configurations")
    total: int = Field(0, description="Total number of configs")


# =============================================================================
# Test Response Models
# =============================================================================

class GuardrailViolationResponse(BaseModel):
    """A single content policy violation"""
    type: str = Field(..., description="Violation type, e.g. prompt_injection, pii")
    provider: str = Field(..., description="Provider that detected it: builtin, lakera, etc.")
    confidence: float = Field(..., description="Detection confidence 0.0–1.0")
    action: str = Field(..., description="Action taken: block, redact, warn, allow")
    detail: Optional[str] = Field(None, description="Human-readable explanation")
    config_id: Optional[str] = Field(None, description="Config that triggered this violation")
    config_name: Optional[str] = Field(None, description="Config name that triggered this violation")


class GuardrailTestResponse(BaseModel):
    """Response from testing content against guardrails"""
    action: str = Field(..., description="Final action taken: block, redact, warn, allow")
    blocked: bool = Field(..., description="Whether the content was blocked")
    violations: List[GuardrailViolationResponse] = Field(default_factory=list)
    duration_ms: float = Field(..., description="Check duration in milliseconds")
    surface: str = Field(..., description="Surface that was checked")
    direction: str = Field(..., description="Direction: input or output")
    configs_evaluated: int = Field(0, description="Number of configs evaluated")
    config_id: Optional[str] = Field(None, description="Config ID tested (when specific config requested)")
    config_name: Optional[str] = Field(None, description="Config name tested (when specific config requested)")


# =============================================================================
# Provider Test-Connection (reachability + auth probe)
# =============================================================================

class GuardrailProviderTestRequest(BaseModel):
    """Probe an external guardrail provider for reachability + auth.

    The operator may be editing unsaved form values, so the provider
    config is carried in the body. Because `api_key` is deliberately
    excluded from the config GET response, a "Test connection" on an
    already-saved provider has no key to send — so when `api_key` is
    omitted the probe falls back to the saved key for
    (`config_id`, `provider`), read from the stored `providers[]` JSON.
    """
    provider: str = Field(..., description="presidio, bedrock, custom, openai_moderation, lakera")
    endpoint: Optional[str] = Field(None, description="Provider endpoint URL (as entered in the form)")
    api_key: Optional[str] = Field(None, description="Provider API key; if omitted, the saved key for config_id+provider is used")
    config: Optional[Dict[str, Any]] = Field(None, description="Provider-specific config (region, guardrail_id, auth_header, timeout_ms, …)")
    config_id: Optional[str] = Field(None, description="Saved guardrail config id — used to recover the api_key/endpoint when the form omits them")

    @field_validator('provider')
    @classmethod
    def validate_provider(cls, v: str) -> str:
        if v.lower() not in ALLOWED_PROVIDERS:
            raise ValueError(f"Provider must be one of: {', '.join(ALLOWED_PROVIDERS)}")
        return v.lower()


class GuardrailProviderTestResponse(BaseModel):
    """Result of a provider reachability + auth probe.

    `status` mirrors the LLM model health-check vocabulary so the admin
    UI can reuse the same green/red/amber affordance:
      * healthy  — reachable and authenticated
      * unhealthy — reached but rejected (bad key / 4xx / 5xx / bad config)
      * timeout  — no response within the probe window
    """
    provider: str = Field(..., description="Provider that was probed")
    status: str = Field(..., description="healthy | unhealthy | timeout")
    ok: bool = Field(..., description="True only when status == healthy")
    response_time_ms: float = Field(..., description="Round-trip latency of the probe")
    detail: Optional[str] = Field(None, description="Human-readable success detail (e.g. endpoint hit, guardrail name)")
    error_message: Optional[str] = Field(None, description="Failure reason when not healthy")


# =============================================================================
# Provider Info Response
# =============================================================================

class GuardrailProviderInfo(BaseModel):
    """Information about an available guardrail provider"""
    id: str = Field(..., description="Provider identifier")
    name: str = Field(..., description="Display name")
    description: str = Field(..., description="What this provider does")
    modes: List[str] = Field(..., description="Supported modes: pre_call, post_call")
    requires_api_key: bool = Field(..., description="Whether an API key is required")


class GuardrailProviderListResponse(BaseModel):
    """Response for listing available guardrail providers"""
    providers: List[GuardrailProviderInfo] = Field(..., description="Available guardrail providers")
