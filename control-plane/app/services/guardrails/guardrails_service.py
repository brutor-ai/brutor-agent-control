# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Guardrails Service with Resource Group Resolution

Unified content safety layer for all three proxy surfaces:

    LLM  → check_input (prompt) + check_output (response)
    MCP  → check_input (tool args) + check_output (tool results)
    Skill → check_input (params) + check_output (rendered output)

    - Runs one named GuardrailConfig against content, for the admin Test button.
    - Runtime enforcement lives in the Rust core; the verdict here is the one
      that config would produce there.
"""

import time
import asyncio
import httpx

from app.services.outbound_http import outbound_async_client
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List
from enum import Enum

from sqlalchemy.orm import Session

from app.core.logging_system import BrutorLogger
from app.services.guardrails.content_filter import (
    ContentFilter,
    ContentPolicy,
    FilterResult,
    FilterAction,
    ViolationType,
)
from app.database.models.guardrail_config import GuardrailConfig


# =============================================================================
# Types
# =============================================================================

class GuardrailAction(str, Enum):
    """Action to take on content — ordered by severity."""
    ALLOW = "allow"
    WARN = "warn"
    REDACT = "redact"
    BLOCK = "block"


class GuardrailSurface(str, Enum):
    LLM = "llm"
    MCP = "mcp"
    SKILL = "skill"


@dataclass
class Violation:
    """A single content policy violation."""
    type: str                          # e.g. "prompt_injection", "pii"
    provider: str                      # "builtin" or external provider name
    confidence: float                  # 0.0 – 1.0
    action: GuardrailAction
    detail: Optional[str] = None
    redacted_text: Optional[str] = None
    config_id: Optional[str] = None    # Which GuardrailConfig triggered this
    config_name: Optional[str] = None  # Human-readable name


@dataclass
class GuardrailCheckResult:
    """
    Merged result of all guardrail checks.

    Most restrictive action wins: BLOCK > REDACT > WARN > ALLOW
    """
    action: GuardrailAction
    violations: List[Violation] = field(default_factory=list)
    filtered_content: Optional[str] = None   # Set when action == REDACT
    original_content: str = ""
    duration_ms: float = 0.0
    surface: str = ""
    direction: str = ""
    configs_evaluated: int = 0               # How many configs were checked

    @property
    def blocked(self) -> bool:
        return self.action == GuardrailAction.BLOCK

    @property
    def redacted(self) -> bool:
        return self.action == GuardrailAction.REDACT

    @property
    def has_violations(self) -> bool:
        return len(self.violations) > 0

    @property
    def block_reason(self) -> str:
        if not self.blocked:
            return ""
        reasons = [v.detail or v.type for v in self.violations
                   if v.action == GuardrailAction.BLOCK]
        return "; ".join(reasons) if reasons else "Content policy violation"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action.value,
            "blocked": self.blocked,
            "violations": [
                {"type": v.type, "provider": v.provider,
                 "confidence": v.confidence, "action": v.action.value,
                 "detail": v.detail, "config_id": v.config_id,
                 "config_name": v.config_name}
                for v in self.violations
            ],
            "duration_ms": round(self.duration_ms, 2),
            "surface": self.surface,
            "direction": self.direction,
            "configs_evaluated": self.configs_evaluated,
        }


# Action priority: BLOCK > REDACT > WARN > ALLOW
_ACTION_PRIORITY = {
    GuardrailAction.ALLOW: 0,
    GuardrailAction.WARN: 1,
    GuardrailAction.REDACT: 2,
    GuardrailAction.BLOCK: 3,
}

# Map ContentFilter enums → our types
_FILTER_ACTION_MAP = {
    FilterAction.ALLOW: GuardrailAction.ALLOW,
    FilterAction.BLOCK: GuardrailAction.BLOCK,
    FilterAction.REDACT: GuardrailAction.REDACT,
    FilterAction.WARN: GuardrailAction.WARN,
    FilterAction.MODERATE: GuardrailAction.WARN,
}

_VIOLATION_TYPE_MAP = {
    ViolationType.PROMPT_INJECTION: "prompt_injection",
    ViolationType.PII: "pii",
    ViolationType.TOXIC_CONTENT: "toxic_content",
    ViolationType.JAILBREAK_ATTEMPT: "jailbreak",
    ViolationType.EXCESSIVE_TOKENS: "excessive_tokens",
    ViolationType.BANNED_CONTENT: "banned_content",
    ViolationType.CUSTOM_POLICY: "custom_policy",
}

# Map string actions from DB config → FilterAction
_CONFIG_ACTION_TO_FILTER = {
    "block": FilterAction.BLOCK,
    "redact": FilterAction.REDACT,
    "warn": FilterAction.WARN,
    "allow": FilterAction.ALLOW,
}


# =============================================================================
# Service
# =============================================================================

class GuardrailsService:
    """
    Runs a named guardrail config against content for the admin Test button.

    Usage:
        service = GuardrailsService(logger=logger)
        result = await service.check_input(
            content=text, surface="chat_input", tenant_id=tenant_id, config_id=config_id, db=db,
        )
    """

    def __init__(self, logger: Optional[BrutorLogger] = None):
        self.logger = logger or BrutorLogger("guardrails-service")


    # =========================================================================
    # PUBLIC API
    # =========================================================================

    async def check_input(
        self,
        content: str,
        surface: str,
        tenant_id: str,
        db: Session,
        config_id: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> GuardrailCheckResult:
        """
        Check INPUT content before processing.

        LLM:   User prompt text (concatenated messages)
        MCP:   Tool call arguments (JSON serialised)
        Skill: Input parameters (JSON serialised)

        Args:
            content: Text to check
            surface: an LLM route (e.g. "chat_input"), "mcp" or "skill"
            tenant_id: Owning tenant
            db: Database session
            config_id: The config to test. Without one, nothing is evaluated.
            context: Optional extra context for providers
        """
        return await self._run_checks(content, surface, "input", tenant_id, db, config_id, context)

    async def check_output(
        self,
        content: str,
        surface: str,
        tenant_id: str,
        db: Session,
        config_id: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> GuardrailCheckResult:
        """
        Check OUTPUT content before returning to client.

        LLM:   Model response text
        MCP:   Tool call result content
        Skill: Rendered template / script output
        """
        return await self._run_checks(content, surface, "output", tenant_id, db, config_id, context)

    # =========================================================================
    # CORE ENGINE
    # =========================================================================

    async def _run_checks(
        self,
        content: str,
        surface: str,
        direction: str,
        tenant_id: str,
        db: Session,
        config_id: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> GuardrailCheckResult:
        """Run all applicable configs' checks and merge results."""
        start = time.time()

        # ── 1. Resolve which configs apply ───────────────────────────
        if config_id:
            # Direct load: admin test against a specific config.  We intentionally skip
            # the 'enabled' filter so admins can test disabled configs.
            config = (
                db.query(GuardrailConfig)
                .filter(
                    GuardrailConfig.id == config_id,
                    GuardrailConfig.tenant_id == tenant_id,
                )
                .first()
            )
            if not config:
                return GuardrailCheckResult(
                    action=GuardrailAction.ALLOW,
                    original_content=content,
                    surface=surface,
                    direction=direction,
                    configs_evaluated=0,
                )
            # Detach so it survives session close
            for col in config.__table__.columns:
                getattr(config, col.key, None)
            if hasattr(config, 'groups'):
                _ = list(config.groups or [])
            db.expunge(config)
            configs = [config]
        else:
            configs = []

        if not configs:
            return GuardrailCheckResult(
                action=GuardrailAction.ALLOW,
                original_content=content,
                surface=surface,
                direction=direction,
                configs_evaluated=0,
            )

        all_violations: List[Violation] = []
        filtered_content = content
        configs_evaluated = 0

        # ── 2. Run checks for each applicable config ─────────────────
        for config in configs:
            # Skip a surface the config does not cover. Its master switch does
            # not apply, or a config could not be tried before it is enabled.
            if not config.surface_flag(surface, direction):
                continue

            configs_evaluated += 1

            try:
                # Built-in checks
                builtin_violations, builtin_filtered = self._run_builtin_checks(
                    content, direction, config,
                )
                # Tag violations with source config
                for v in builtin_violations:
                    v.config_id = config.id
                    v.config_name = config.name
                all_violations.extend(builtin_violations)

                # Detection stays on the original (a redaction by an earlier
                # config must not hide content from a later, stricter one), but
                # redactions compound: taking only the last config's output
                # would hand back what the earlier ones removed.
                if builtin_filtered != content:
                    if filtered_content == content:
                        filtered_content = builtin_filtered
                    else:
                        _, filtered_content = self._run_builtin_checks(
                            filtered_content, direction, config,
                        )

                # External provider checks (parallel)
                providers = config.get_active_providers(surface)
                if providers:
                    provider_violations = await self._run_provider_checks(
                        content, direction, providers,
                        timeout_ms=config.max_check_timeout_ms or 2000.0,
                        fail_open=config.fail_open if config.fail_open is not None else True,
                        context=context,
                    )
                    for v in provider_violations:
                        v.config_id = config.id
                        v.config_name = config.name
                    all_violations.extend(provider_violations)

            except Exception as e:
                self.logger.error("Guardrail check error", context={
                    "config_id": config.id,
                    "config_name": config.name,
                    "surface": surface, "direction": direction,
                    "tenant_id": tenant_id, "error": str(e),
                }, exc_info=True)

                fail_open = config.fail_open if config.fail_open is not None else True
                if not fail_open:
                    all_violations.append(Violation(
                        type="check_error", provider="system",
                        confidence=1.0, action=GuardrailAction.BLOCK,
                        detail=f"Guardrail check failed: {str(e)}",
                        config_id=config.id, config_name=config.name,
                    ))

            # Short-circuit: if already blocked, no need to check more configs
            for v in all_violations:
                if v.action == GuardrailAction.BLOCK:
                    duration_ms = (time.time() - start) * 1000
                    return GuardrailCheckResult(
                        action=GuardrailAction.BLOCK,
                        violations=all_violations,
                        original_content=content,
                        duration_ms=duration_ms,
                        surface=surface,
                        direction=direction,
                        configs_evaluated=configs_evaluated,
                    )

        # ── 3. Merge: most restrictive action wins ───────────────────
        final_action = GuardrailAction.ALLOW
        for v in all_violations:
            if _ACTION_PRIORITY.get(v.action, 0) > _ACTION_PRIORITY.get(final_action, 0):
                final_action = v.action

        duration_ms = (time.time() - start) * 1000

        result = GuardrailCheckResult(
            action=final_action,
            violations=all_violations,
            filtered_content=filtered_content if final_action == GuardrailAction.REDACT else None,
            original_content=content,
            duration_ms=duration_ms,
            surface=surface,
            direction=direction,
            configs_evaluated=configs_evaluated,
        )

        # Log
        log_all = any(c.log_all_checks for c in configs if c.enabled)
        if result.has_violations or log_all:
            log_fn = self.logger.warning if result.has_violations else self.logger.debug
            log_fn("Guardrail check completed", context={
                "tenant_id": tenant_id,
                "surface": surface,
                "direction": direction,
                "action": final_action.value,
                "violations": len(all_violations),
                "violation_types": [v.type for v in all_violations],
                "configs_evaluated": configs_evaluated,
                "duration_ms": round(duration_ms, 2),
            })

        return result

    # =========================================================================
    # CONFIG RESOLUTION (Resource Group aware)
    # =========================================================================

    # =========================================================================
    # BUILT-IN CHECKS (wraps existing ContentFilter)
    # =========================================================================

    def _run_builtin_checks(
        self, content: str, direction: str, config: GuardrailConfig,
    ) -> tuple:
        """
        Run built-in ContentFilter checks.

        Returns (violations, filtered_content).
        """
        # Build ContentPolicy from GuardrailConfig
        policy_kwargs = config.to_content_policy_kwargs()

        # Map config action strings → FilterAction enums
        policy_kwargs["pii_action"] = _CONFIG_ACTION_TO_FILTER.get(
            config.pii_action or "redact", FilterAction.REDACT)
        policy_kwargs["injection_action"] = _CONFIG_ACTION_TO_FILTER.get(
            config.prompt_injection_action or "block", FilterAction.BLOCK)
        policy_kwargs["toxic_action"] = _CONFIG_ACTION_TO_FILTER.get(
            config.toxic_content_action or "warn", FilterAction.WARN)
        policy_kwargs["jailbreak_action"] = _CONFIG_ACTION_TO_FILTER.get(
            config.jailbreak_action or "block", FilterAction.BLOCK)

        policy = ContentPolicy(**policy_kwargs)
        content_filter = ContentFilter(policy=policy, logger=self.logger)

        # Run the appropriate filter
        if direction == "input":
            filter_result: FilterResult = content_filter.filter_input(content)
        else:
            filter_result: FilterResult = content_filter.filter_output(content)

        # Map violation type → its configured action from the DB config
        _violation_action_map = {
            "pii": _CONFIG_ACTION_TO_FILTER.get(config.pii_action or "redact", FilterAction.REDACT),
            "prompt_injection": _CONFIG_ACTION_TO_FILTER.get(config.prompt_injection_action or "block", FilterAction.BLOCK),
            "jailbreak": _CONFIG_ACTION_TO_FILTER.get(config.jailbreak_action or "block", FilterAction.BLOCK),
            "toxic_content": _CONFIG_ACTION_TO_FILTER.get(config.toxic_content_action or "warn", FilterAction.WARN),
            "banned_content": FilterAction.BLOCK,
            "excessive_tokens": FilterAction.BLOCK,
        }

        # Convert violations — each gets its OWN configured action
        violations = []
        for vtype in filter_result.violations:
            vtype_str = _VIOLATION_TYPE_MAP.get(vtype, vtype.value)
            per_violation_action = _violation_action_map.get(vtype_str)
            if per_violation_action:
                action = _FILTER_ACTION_MAP.get(per_violation_action, GuardrailAction.WARN)
            else:
                action = _FILTER_ACTION_MAP.get(filter_result.action, GuardrailAction.WARN)
            violations.append(Violation(
                type=vtype_str,
                provider="builtin",
                confidence=filter_result.confidence,
                action=action,
                detail=None,
            ))

        return violations, filter_result.filtered_content

    # =========================================================================
    # EXTERNAL PROVIDER CHECKS (parallel with timeout)
    # =========================================================================

    async def _run_provider_checks(
        self,
        content: str,
        direction: str,
        providers: List[Dict[str, Any]],
        timeout_ms: float,
        fail_open: bool,
        context: Optional[Dict[str, Any]] = None,
    ) -> List[Violation]:
        """Run external provider checks in parallel with timeout."""
        timeout_sec = timeout_ms / 1000.0
        violations: List[Violation] = []

        tasks = []
        task_providers = []
        for pconf in providers:
            mode = pconf.get("mode", "both")
            if mode == "pre_call" and direction == "output":
                continue
            if mode == "post_call" and direction == "input":
                continue
            tasks.append(self._check_with_provider(content, direction, pconf, context))
            task_providers.append(pconf)

        if not tasks:
            return violations

        try:
            results = await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=timeout_sec,
            )
        except asyncio.TimeoutError:
            self.logger.warning("Provider checks timed out", context={
                "timeout_ms": timeout_ms, "provider_count": len(tasks),
            })
            if not fail_open:
                violations.append(Violation(
                    type="timeout", provider="external",
                    confidence=1.0, action=GuardrailAction.BLOCK,
                    detail=f"Guardrail checks timed out after {timeout_ms}ms",
                ))
            return violations

        for i, result in enumerate(results):
            if isinstance(result, Exception):
                pname = task_providers[i].get("provider", "unknown")
                self.logger.warning("Provider check failed", context={
                    "provider": pname, "error": str(result),
                })
                if not fail_open:
                    violations.append(Violation(
                        type="provider_error", provider=pname,
                        confidence=1.0, action=GuardrailAction.BLOCK,
                        detail=f"Provider '{pname}' failed: {str(result)}",
                    ))
            elif isinstance(result, list):
                violations.extend(result)

        return violations

    async def _check_with_provider(
        self, content: str, direction: str,
        pconf: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None,
    ) -> List[Violation]:
        """Dispatch to a specific external provider."""
        provider = pconf.get("provider", "")

        dispatch = {
            "lakera": self._check_lakera,
            "openai_moderation": self._check_openai_moderation,
            "presidio": self._check_presidio,
            "bedrock": self._check_bedrock,
            "custom": self._check_custom_webhook,
        }

        handler = dispatch.get(provider)
        if not handler:
            self.logger.warning("Unknown guardrail provider", context={"provider": provider})
            return []

        if provider == "custom":
            return await handler(content, direction, pconf, context)
        return await handler(content, pconf)

    # =========================================================================
    # PROVIDER IMPLEMENTATIONS
    # =========================================================================

    async def _check_lakera(self, content: str, pconf: Dict[str, Any]) -> List[Violation]:
        """Lakera Guard — ML-based prompt injection detection."""
        endpoint = pconf.get("endpoint", "https://api.lakera.ai/v2/guard")
        async with outbound_async_client(timeout=5.0) as client:
            resp = await client.post(
                endpoint,
                json={"input": content},
                headers={"Authorization": f"Bearer {pconf.get('api_key', '')}"},
            )
            resp.raise_for_status()
            data = resp.json()

        violations = []
        for category, flagged in data.get("categories", {}).items():
            if flagged:
                violations.append(Violation(
                    type=f"lakera_{category}", provider="lakera",
                    confidence=data.get("category_scores", {}).get(category, 0.9),
                    action=GuardrailAction.BLOCK,
                    detail=f"Lakera flagged: {category}",
                ))
        return violations

    async def _check_openai_moderation(self, content: str, pconf: Dict[str, Any]) -> List[Violation]:
        """OpenAI Moderation — free toxic content detection."""
        async with outbound_async_client(timeout=5.0) as client:
            resp = await client.post(
                "https://api.openai.com/v1/moderations",
                json={"input": content},
                headers={"Authorization": f"Bearer {pconf.get('api_key', '')}"},
            )
            resp.raise_for_status()
            data = resp.json()

        violations = []
        for result in data.get("results", []):
            if result.get("flagged"):
                categories = result.get("categories", {})
                scores = result.get("category_scores", {})
                for cat, is_flagged in categories.items():
                    if is_flagged:
                        violations.append(Violation(
                            type=f"openai_{cat}", provider="openai_moderation",
                            confidence=scores.get(cat, 0.9),
                            action=GuardrailAction.BLOCK,
                            detail=f"OpenAI moderation flagged: {cat}",
                        ))
        return violations

    async def _check_presidio(self, content: str, pconf: Dict[str, Any]) -> List[Violation]:
        """Microsoft Presidio — PII detection/anonymisation."""
        endpoint = pconf.get("endpoint", "http://localhost:5002/analyze")
        presidio_cfg = pconf.get("config", {})
        threshold = presidio_cfg.get("score_threshold", 0.7)

        async with outbound_async_client(timeout=5.0) as client:
            resp = await client.post(
                endpoint,
                json={"text": content, "language": presidio_cfg.get("language", "en")},
            )
            resp.raise_for_status()
            entities = resp.json()

        violations = []
        for entity in entities:
            score = entity.get("score", 0)
            if score >= threshold:
                violations.append(Violation(
                    type=f"pii_{entity.get('entity_type', 'unknown').lower()}",
                    provider="presidio", confidence=score,
                    action=GuardrailAction.REDACT,
                    detail=f"PII: {entity.get('entity_type')} (score: {score:.2f})",
                ))
        return violations

    async def _check_bedrock(self, content: str, pconf: Dict[str, Any]) -> List[Violation]:
        """AWS Bedrock Guardrails — placeholder."""
        self.logger.debug("Bedrock guardrails not yet implemented")
        return []

    async def _check_custom_webhook(
        self, content: str, direction: str,
        pconf: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None,
    ) -> List[Violation]:
        """Custom webhook endpoint."""
        endpoint = pconf.get("endpoint", "")
        if not endpoint:
            return []

        headers = {}
        custom_cfg = pconf.get("config", {})
        if custom_cfg.get("api_key"):
            headers["Authorization"] = f"Bearer {custom_cfg['api_key']}"

        async with outbound_async_client(timeout=5.0) as client:
            resp = await client.post(
                endpoint,
                json={"content": content, "direction": direction, "context": context or {}},
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()

        return [
            Violation(
                type=v.get("type", "custom"), provider="custom",
                confidence=v.get("confidence", 0.9),
                action=GuardrailAction(v.get("action", "block")),
                detail=v.get("detail"),
            )
            for v in data.get("violations", [])
        ]


# =============================================================================
# Helpers for extracting text from proxy-specific structures
# =============================================================================
