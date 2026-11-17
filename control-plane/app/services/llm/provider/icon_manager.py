# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Icon manager for `LlmProvider` rows — a thin subclass of
the generic `EntityIconManager`. The base class now lives in
`app.services.icons` and is reused for any entity that adopts the
five-column icon storage shape (provider rows, MCP servers,
agent skills, IdPs, …).

This file is kept for two reasons:

  1. Type-checked discoverability — the LlmProvider-specific
     class shows up in IDE imports under
     `app.services.llm.provider.icon_manager`, parallel to where
     other LLM-provider services live.
  2. A stable import path for the existing `endpoints/admin/llm_providers.py`
     wiring; collapsing it down to "use EntityIconManager
     directly" would mean churn across the endpoint file.

If we end up with a half-dozen entities all going through this
shape, the per-entity wrapper class can disappear and callers can
construct `EntityIconManager(model_class=…)` inline.
"""
from __future__ import annotations

from typing import Optional

from app.core.logging_system import BrutorLogger
from app.database.models.llm_provider import LlmProvider
from app.services.icons import EntityIconManager


class LlmProviderIconManager(EntityIconManager):
    """`EntityIconManager` bound to `LlmProvider`.

    All behaviour comes from the base class — no overrides today.
    Subclassing is here purely as a name-friendly shortcut so the
    endpoint code can write
    `LlmProviderIconManager(logger=…)` without supplying the
    model class every time.
    """

    def __init__(self, logger: Optional[BrutorLogger] = None) -> None:
        super().__init__(
            model_class=LlmProvider,
            logger_name="llm-provider-icon-manager",
            logger=logger,
        )
