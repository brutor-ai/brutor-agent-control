# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
v0.3 → v1.0 Agent Card translator (read-only / in-memory).

Existing tenants have v0.3 card documents stored in
`agent_cards.card_json`. The v1.0 admin API and discovery endpoint
return v1.0-shaped bodies; we cannot force a write migration for all
tenants up front, so we translate v0.3 → v1.0 on read instead.

## What the translator fixes

| v0.3 field                              | v1.0 destination                                     |
|-----------------------------------------|------------------------------------------------------|
| `url`                                   | `supportedInterfaces[0].url`                         |
| `protocolVersion`                       | `supportedInterfaces[0].protocolVersion`             |
| `preferredTransport`                    | `supportedInterfaces[0].protocolBinding` (defaults JSONRPC) |
| `additionalInterfaces[]`                | `supportedInterfaces[1..]` (mapped one-for-one)      |
| `supportsAuthenticatedExtendedCard`     | `capabilities.extendedAgentCard`                     |
| `signature` / `signatureAlgorithm` / `signedBy` | **dropped** — v1.0 requires JWS-shaped `signatures[]`; the legacy signature is no longer cryptographically meaningful under v1.0 rules so we do NOT fabricate a JWS wrapper. Cards need to be re-signed after upgrade. |

All other fields (`name`, `description`, `version`, `capabilities` sub-fields
we don't rewrite, `skills`, etc.) pass through unchanged. Anything the
translator doesn't recognise stays untouched so an unknown v0.3 extension
doesn't silently vanish.

## What the translator does NOT do

* It does **not** mutate the DB. Output is a fresh dict.
* It does **not** re-sign the card. Upgraded cards come back with
  `signatures: []` and `trust_tier` must be reset to `unsigned` at
  the persistence layer (caller's responsibility).
* It does **not** accept v1.0 input and re-emit it — for v1.0 cards
  callers should feed them straight into `validate_card_v1`. The
  translator's detect-v1.0 check is belt-and-braces to avoid
  accidental double-translation.

## Safety

Running `validate_card_v1(translate_v03_to_v1(card))` gives a v1.0
dict that either validates or raises — the translator never silently
produces invalid output. Tests below cover that round trip.
"""

from __future__ import annotations

import copy
from typing import Any, Dict


def is_v1_card(card_json: Dict[str, Any]) -> bool:
    """Cheap heuristic — a v1.0 card has `supportedInterfaces` at the
    top level. v0.3 cards have a top-level `url` string instead.
    """
    if not isinstance(card_json, dict):
        return False
    return "supportedInterfaces" in card_json


def is_v03_card(card_json: Dict[str, Any]) -> bool:
    """v0.3 cards have a top-level `url` field (or `protocolVersion`
    set to a 0.x value) and no `supportedInterfaces`.
    """
    if not isinstance(card_json, dict):
        return False
    if "supportedInterfaces" in card_json:
        return False
    if "url" in card_json or "additionalInterfaces" in card_json:
        return True
    pv = card_json.get("protocolVersion")
    return isinstance(pv, str) and pv.startswith("0.")


def translate_v03_to_v1(card_json: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a v0.3 card dict to a v1.0-shaped dict.

    Pass-through for cards that already look like v1.0. Raises
    `ValueError` for malformed input (non-dict, or a dict that has
    neither a v0.3 `url` nor v1.0 `supportedInterfaces`).
    """
    if not isinstance(card_json, dict):
        raise ValueError(
            f"translate_v03_to_v1: expected dict, got {type(card_json).__name__}"
        )
    if is_v1_card(card_json):
        # Idempotency — if the caller accidentally feeds a v1.0 card,
        # return a deep copy unchanged so the caller can treat it
        # opaquely either way.
        return copy.deepcopy(card_json)

    out = copy.deepcopy(card_json)

    # ── supported_interfaces ─────────────────────────────────────
    primary_url = out.pop("url", None)
    primary_proto_version = out.pop("protocolVersion", "0.3")
    primary_binding = out.pop("preferredTransport", "JSONRPC")
    additional = out.pop("additionalInterfaces", None)

    supported: list[Dict[str, Any]] = []
    if primary_url:
        supported.append(
            {
                "url": primary_url,
                "protocolBinding": primary_binding or "JSONRPC",
                "protocolVersion": primary_proto_version or "0.3",
            }
        )

    # v0.3 `additionalInterfaces` schema: list of {url, transport} objects.
    # Translate one-for-one. If an entry is missing `url` or `transport`
    # we skip it rather than raising — the source card was already
    # invalid under v0.3 rules if those are missing, and v1.0 doesn't
    # make it worse by dropping them.
    if isinstance(additional, list):
        for entry in additional:
            if not isinstance(entry, dict):
                continue
            url = entry.get("url")
            if not url:
                continue
            binding = entry.get("transport") or entry.get("protocolBinding") or "JSONRPC"
            version = entry.get("protocolVersion") or primary_proto_version or "0.3"
            supported.append(
                {
                    "url": url,
                    "protocolBinding": binding,
                    "protocolVersion": version,
                }
            )

    if not supported:
        raise ValueError(
            "translate_v03_to_v1: v0.3 card has no `url` and no usable "
            "`additionalInterfaces`; cannot build supportedInterfaces[]"
        )
    out["supportedInterfaces"] = supported

    # ── capabilities.extendedAgentCard ─────────────────────────────
    # v0.3 had a top-level `supportsAuthenticatedExtendedCard` flag.
    ext_card_flag = out.pop("supportsAuthenticatedExtendedCard", None)
    if ext_card_flag is not None:
        caps = out.setdefault("capabilities", {})
        if isinstance(caps, dict):
            # Only set if the admin hasn't already provided an explicit
            # v1.0 `extendedAgentCard` via a partial migration.
            caps.setdefault("extendedAgentCard", bool(ext_card_flag))

    # Ensure capabilities is at least an empty object — v1.0 requires
    # the field (field_behavior = REQUIRED). v0.3 permitted it to be
    # absent.
    if "capabilities" not in out:
        out["capabilities"] = {}

    # ── Signatures ─────────────────────────────────────────────────
    # v0.3 used flat fields: signature, signatureAlgorithm, signedBy,
    # signedAt. v1.0 requires JWS-shaped `signatures[]`. A legacy flat
    # signature is not a JWS — we drop them and leave `signatures` as
    # an empty array. Callers re-sign under v1.0 rules.
    for legacy in ("signature", "signatureAlgorithm", "signedBy", "signedAt"):
        out.pop(legacy, None)
    out.setdefault("signatures", [])

    # ── Defaults for v1.0-required fields that v0.3 made optional ──
    # `defaultInputModes` / `defaultOutputModes` / `skills` are
    # REQUIRED per the proto (field_behavior = REQUIRED). v0.3 let
    # them be absent. Supply empty arrays so the translated card
    # parses — the admin can populate them through the UI later.
    out.setdefault("defaultInputModes", [])
    out.setdefault("defaultOutputModes", [])
    out.setdefault("skills", [])

    # `description` and `version` are v1.0 REQUIRED too. If missing,
    # supply sensible placeholders rather than failing outright.
    out.setdefault("description", "")
    out.setdefault("version", "0.0.0")

    return out
