# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Strict validation entry point for A2A v1.0 Agent Card documents.

Callers hand us a `dict` (freshly parsed JSON, or a card taken from
the database) and receive either a fully-validated
`AgentCard` instance or an `InvalidV1CardError` with a
human-readable reason that's safe to surface in the admin UI.

The Pydantic models already do the heavy lifting — unknown-field
rejection, type coercion, required fields, oneof "exactly one"
enforcement. This thin wrapper:

  1. Normalises the Pydantic error payload into a single flat
     message (the admin UI doesn't want to render the nested error
     list verbatim).
  2. Is the single import site the admin endpoints use, so future
     extra cross-field checks (e.g. "interface URLs must be
     https"; "declared extensions must be registered") land in one
     place rather than being scattered.
"""

from __future__ import annotations

from typing import Any, Dict

from pydantic import ValidationError

from app.services.a2a_v1.models import AgentCard


class InvalidV1CardError(ValueError):
    """Raised when a dict cannot be validated as an A2A v1.0 AgentCard.

    The message is safe for end-user display — no Pydantic stack
    traces, no framework noise.
    """

    pass


def validate_card_v1(card_json: Dict[str, Any]) -> AgentCard:
    """Validate a card dict against the v1.0 schema.

    Returns:
        A fully-typed [`AgentCard`] instance.

    Raises:
        InvalidV1CardError: with a one-line reason summarising the
            first problem found. The full Pydantic error tree is
            preserved in `__cause__` for server-side logging.
    """
    if not isinstance(card_json, dict):
        raise InvalidV1CardError(
            f"card_json must be a JSON object, got {type(card_json).__name__}"
        )
    try:
        return AgentCard.model_validate(card_json)
    except ValidationError as e:
        # Take the first error, render just enough to be actionable
        # in the admin UI without dumping the whole Pydantic tree.
        first = e.errors()[0] if e.errors() else None
        if first:
            loc = ".".join(str(x) for x in first.get("loc", ()))
            msg = first.get("msg", "validation failed")
            kind = first.get("type", "invalid")
            summary = (
                f"{loc}: {msg} ({kind})"
                if loc
                else f"{msg} ({kind})"
            )
        else:
            summary = "validation failed"
        wrapped = InvalidV1CardError(summary)
        wrapped.__cause__ = e
        raise wrapped
