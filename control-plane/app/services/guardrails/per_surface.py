# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Per-surface override resolver.

# What this module does

Every built-in guardrail check (PII, prompt-injection, jailbreak,
toxic-content, secrets, banned-words, banned-patterns) carries one
scalar action / provider / threshold column for the whole check.
A nullable JSONB column per check that lets
operators override those scalars on a per-surface basis:

    pii_per_surface = {
        "chat_input":  {"action": "redact"},
        "chat_output": {"action": "block", "provider": "presidio"},
    }

At runtime, every dispatch site asks "what's the action / provider /
threshold for THIS check on THIS surface?". The resolver in this
module is the answer — it consults the per-surface map first, then
falls back to the scalar column, then to a documented system default.

# Why one helper

Three reasons to centralise the lookup rather than scatter
`config.get("per_surface", {}).get(surface, {}).get("action") or
config["action"]` chains across the codebase:

  1. **Consistency.** Every call site computes the SAME effective
     action for the SAME (config, check, surface, field) triple.
  2. **Audit hook.** If we later want to log "scalar overridden on
     surface X by tenant Y", there's one place to wire that.
  3. **Symmetry with Rust.** The Rust runtime has its own resolver
     (`brutor-gateway-core/src/services/guardrails/per_surface.rs`)
     with identical semantics. Python callers + Rust runtime
     resolve the same way; the tests pin that.

# Contract

The resolver NEVER returns `None`. Every field has a documented
fallback chain: per-surface override → scalar column → system default.
Callers can rely on getting a usable value.

The resolver is read-only: it never mutates `config` or the override
dict. Safe to call from any thread / coroutine.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Union

from app.constants.guardrail_surfaces import GUARDRAIL_CHECK_SURFACES


# The surfaces an override can be keyed by: the ones a check can be put on.
VALID_SURFACE_TOKENS = GUARDRAIL_CHECK_SURFACES


# Per-check tuple of (per_surface_attr_name, scalar_attr_name_map).
# Each entry tells the resolver where to read the JSONB and what
# the scalar fallback attribute is for each field. Extra fields
# that don't apply (e.g. provider on secrets, threshold on PII)
# return their `_default` sentinel below — callers consult only
# the fields they care about for their check.
#
# Shape: {check_name: {field: scalar_attr_or_None}}
#   None  → "no scalar — return the system default"
#   str   → name of the scalar attribute on GuardrailConfig
_CHECK_SPEC: Dict[str, Dict[str, Optional[str]]] = {
    "pii": {
        "enabled":   "pii_detection_enabled",
        "action":    "pii_action",
        "provider":  "pii_provider",
    },
    "prompt_injection": {
        "enabled":   "prompt_injection_enabled",
        "action":    "prompt_injection_action",
        "provider":  "prompt_injection_provider",
    },
    "jailbreak": {
        "enabled":   "jailbreak_detection_enabled",
        "action":    "jailbreak_action",
        "provider":  "jailbreak_provider",
    },
    "toxic_content": {
        "enabled":   "toxic_content_enabled",
        "action":    "toxic_content_action",
        "provider":  "toxic_content_provider",
        "threshold": "toxic_content_threshold",
    },
    "secrets": {
        "enabled":   "secrets_detection_enabled",
        "action":    "secrets_action",
        "threshold": "secrets_threshold",
        # No provider scalar — secrets has only the built-in detector
        # today. Per-surface entries claiming a provider are silently
        # ignored at runtime (the orchestrator never reads it).
    },
    "banned_words": {
        "enabled":   None,    # banned-words has no master enabled flag
                              # separate from the list being non-empty
        "action":    None,    # banned-words always blocks today
        "words":     None,    # backed by `banned_words` list, treated
                              # specially below — the per-surface map
                              # holds REPLACEMENT lists, not a scalar
    },
    "banned_patterns": {
        "enabled":   None,
        "action":    None,
        "patterns":  None,
    },
}


# Default fallback values when neither a per-surface override nor a
# scalar value is set. Pinned here so a single source of truth
# survives schema evolution.
_SYSTEM_DEFAULTS: Dict[str, Any] = {
    "enabled":   False,
    "action":    "warn",     # safest default — observe, don't break
    "provider":  "built_in", # bundled deterministic detectors
    "threshold": 0.8,        # 80% confidence — provider-dependent
}


class _Missing:
    """Sentinel marking 'no override present'. Distinct from None
    because None is a valid override value (operator could set
    `{action: null}` to mean 'use the scalar', which is the same
    as omitting the key — we treat both as missing)."""

_MISSING = _Missing()


def _config_attr(config: Any, attr: Optional[str]) -> Any:
    """Read a scalar attribute from the config. Handles both ORM
    instances (use getattr) and dict-shaped configs (use [key]) so
    the resolver works whether the caller has loaded the SQLAlchemy
    object or hydrated from JSON / ProxyLog snapshot."""
    if attr is None:
        return _MISSING
    if isinstance(config, dict):
        return config.get(attr, _MISSING)
    return getattr(config, attr, _MISSING)


def _per_surface_map(config: Any, check: str) -> Dict[str, Dict[str, Any]]:
    """Return the per-surface override map for `check`, or empty
    dict if NULL / missing.

    Reads `<check>_per_surface` on the config (ORM or dict). When
    the resulting value is the JSON null (None), we coerce to {} so
    downstream lookups don't need to guard against None.
    """
    attr = f"{check}_per_surface"
    v = _config_attr(config, attr)
    if v is _MISSING or v is None:
        return {}
    if not isinstance(v, dict):
        # Defensive: a corrupted row could in principle store a
        # non-dict here (it shouldn't — Pydantic validates on save).
        # Treat as "no overrides" rather than crashing the dispatch.
        return {}
    return v


def resolve(
    config: Any,
    check: str,
    surface: str,
    field: str,
    *,
    system_default: Any = _MISSING,
) -> Any:
    """Return the effective value of `field` for the `check` on the
    given `surface`.

    Resolution order:
      1. `config.<check>_per_surface[surface][field]` — explicit
         override for this surface.
      2. Scalar attribute `config.<check>_<field>` (e.g.
         `config.pii_action`) — applies to every surface that has
         no override.
      3. `system_default` argument if the caller supplied one.
      4. Built-in default from `_SYSTEM_DEFAULTS`.
      5. None as the last resort.

    Args:
        config:    GuardrailConfig ORM row OR dict from `to_dict()`.
        check:     Built-in check name (see _CHECK_SPEC keys).
        surface:   8-token surface name.
        field:     'enabled' | 'action' | 'provider' | 'threshold'
                    | 'words' | 'patterns'.
        system_default: Optional caller-supplied fallback before the
                    built-in default kicks in.

    Returns the effective value, never raises (out-of-spec inputs
    fall back to defaults so a misconfigured row can't kill the
    dispatch path).
    """
    if check not in _CHECK_SPEC:
        # Unknown check name — return the caller default if any,
        # else the system default for the field. This shouldn't
        # happen in practice; the check name is fixed at the call
        # site, not user-controlled.
        return _resolve_default(field, system_default)

    if surface not in VALID_SURFACE_TOKENS:
        # Same logic — out-of-spec surface, fall back gracefully.
        return _resolve_default(field, system_default)

    # 1. per-surface override
    overrides = _per_surface_map(config, check)
    entry = overrides.get(surface)
    if isinstance(entry, dict) and field in entry and entry[field] is not None:
        return entry[field]

    # 2. scalar column
    spec = _CHECK_SPEC[check]
    scalar_attr = spec.get(field)
    scalar = _config_attr(config, scalar_attr)
    if scalar is not _MISSING and scalar is not None:
        return scalar

    # 3 + 4. caller default → built-in default
    return _resolve_default(field, system_default)


def _resolve_default(field: str, system_default: Any) -> Any:
    """Pick the right fallback for `field`. Caller-supplied
    `system_default` wins; otherwise consult `_SYSTEM_DEFAULTS`."""
    if system_default is not _MISSING:
        return system_default
    return _SYSTEM_DEFAULTS.get(field)


def resolve_words(config: Any, surface: str) -> List[str]:
    """banned_words helper. Returns the effective word list for the
    given surface — the per-surface entry's `words` list REPLACES
    the global `banned_words` list when present, otherwise the
    global list applies. Empty list when neither has values.

    Replacement (not merge) is the documented contract — operators
    who want a surface-specific list can specify it cleanly;
    operators who want everything shared just leave per-surface
    unset.
    """
    overrides = _per_surface_map(config, "banned_words")
    entry = overrides.get(surface)
    if isinstance(entry, dict):
        words = entry.get("words")
        if isinstance(words, list):
            return list(words)
    global_words = _config_attr(config, "banned_words")
    if isinstance(global_words, list):
        return list(global_words)
    return []


def resolve_patterns(config: Any, surface: str) -> List[str]:
    """banned_patterns helper. Same replacement semantics as
    resolve_words — per-surface `patterns` list REPLACES global
    `banned_patterns` for that surface."""
    overrides = _per_surface_map(config, "banned_patterns")
    entry = overrides.get(surface)
    if isinstance(entry, dict):
        patterns = entry.get("patterns")
        if isinstance(patterns, list):
            return list(patterns)
    global_patterns = _config_attr(config, "banned_patterns")
    if isinstance(global_patterns, list):
        return list(global_patterns)
    return []


def has_any_per_surface_override(config: Any) -> bool:
    """Cheap "does this config have ANY per-surface override at
    all?" check. Useful for the admin UI to show a "Per-surface
    overrides active" badge and for the diff path to avoid useless
    walks when no overrides are configured.
    """
    for check in _CHECK_SPEC:
        if _per_surface_map(config, check):
            return True
    return False
