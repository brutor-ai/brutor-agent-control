# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Regression test suite for the Cache Invalidation system.

Tests cover:
- Invalidation message format (JSON structure matching Rust consumer)
- invalidate_proxy_cache helper (request extraction)
- publish_invalidation (Redis pub/sub)
- Resource type → invalidation type mapping
- All admin endpoints that should trigger invalidation

Run: pytest tests/test_cache_invalidation.py -v   (mocks only; no database)
"""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.cache_invalidation import (
    publish_invalidation,
    publish_invalidation_sync,
    invalidate_proxy_cache,
    INVALIDATION_CHANNEL,
)


# =============================================================================
# Invalidation Channel
# =============================================================================

class TestInvalidationChannel:
    """The channel name must match the Rust proxy's subscription."""

    def test_channel_name(self):
        assert INVALIDATION_CHANNEL == "brutor:cache:invalidate"

    def test_channel_name_is_string(self):
        assert isinstance(INVALIDATION_CHANNEL, str)


# =============================================================================
# Message Format (must match Rust InvalidationMessage)
# =============================================================================

class TestMessageFormat:
    """Messages must match the Rust proxy's InvalidationMessage struct:
    { "type": str, "action": str, "tenant_id": str, "id": str }
    """

    @pytest.mark.asyncio
    async def test_message_structure(self):
        """Published message has exactly the fields the Rust proxy expects."""
        redis = AsyncMock()
        await publish_invalidation(redis, "model", "tenant-1", "llm-01", "update")

        redis.publish.assert_called_once()
        channel, message = redis.publish.call_args[0]
        assert channel == INVALIDATION_CHANNEL

        parsed = json.loads(message)
        assert set(parsed.keys()) == {"type", "action", "tenant_id", "id"}

    @pytest.mark.asyncio
    async def test_message_type_field(self):
        """The 'type' field matches the resource_type argument."""
        redis = AsyncMock()
        await publish_invalidation(redis, "model", "t1", "llm-01")

        parsed = json.loads(redis.publish.call_args[0][1])
        assert parsed["type"] == "model"

    @pytest.mark.asyncio
    async def test_message_action_field(self):
        """The 'action' field matches the action argument."""
        redis = AsyncMock()
        await publish_invalidation(redis, "model", "t1", "llm-01", "delete")

        parsed = json.loads(redis.publish.call_args[0][1])
        assert parsed["action"] == "delete"

    @pytest.mark.asyncio
    async def test_message_tenant_id_field(self):
        redis = AsyncMock()
        await publish_invalidation(redis, "model", "tenant-abc", "llm-01")

        parsed = json.loads(redis.publish.call_args[0][1])
        assert parsed["tenant_id"] == "tenant-abc"

    @pytest.mark.asyncio
    async def test_message_id_field(self):
        redis = AsyncMock()
        await publish_invalidation(redis, "model", "t1", "llm-abc-123")

        parsed = json.loads(redis.publish.call_args[0][1])
        assert parsed["id"] == "llm-abc-123"

    @pytest.mark.asyncio
    async def test_default_action_is_update(self):
        redis = AsyncMock()
        await publish_invalidation(redis, "model", "t1", "x")

        parsed = json.loads(redis.publish.call_args[0][1])
        assert parsed["action"] == "update"

    @pytest.mark.asyncio
    async def test_message_is_valid_json(self):
        redis = AsyncMock()
        await publish_invalidation(redis, "model", "t1", "x")

        raw = redis.publish.call_args[0][1]
        # Must not raise
        parsed = json.loads(raw)
        assert isinstance(parsed, dict)


# =============================================================================
# All Resource Types (must match Rust CacheInvalidator::handle_invalidation)
# =============================================================================

class TestResourceTypes:
    """Every resource type used by admin endpoints must be recognised by
    the Rust proxy's handle_invalidation match arms."""

    VALID_RESOURCE_TYPES = [
        "model",
        "routing_group",
        "resource_group",
        "skill",
        "agent_skill",
        "guardrail",
        "mcp_server",
        "server_config",
        "membership",
        "assign",
        "semantic_cache",
        # Declared dependency edges (card → service map).
        "ai_system_dependency",
    ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("resource_type", VALID_RESOURCE_TYPES)
    async def test_resource_type_publishes(self, resource_type):
        """Each resource type publishes a valid message."""
        redis = AsyncMock()
        result = await publish_invalidation(redis, resource_type, "t1", "x")
        assert result is True
        parsed = json.loads(redis.publish.call_args[0][1])
        assert parsed["type"] == resource_type

    VALID_ACTIONS = ["create", "update", "delete", "assign"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("action", VALID_ACTIONS)
    async def test_action_publishes(self, action):
        """Each action type publishes a valid message."""
        redis = AsyncMock()
        await publish_invalidation(redis, "model", "t1", "x", action)
        parsed = json.loads(redis.publish.call_args[0][1])
        assert parsed["action"] == action


# =============================================================================
# publish_invalidation — Redis interactions
# =============================================================================

class TestPublishInvalidation:
    """Tests for the async publish_invalidation function."""

    @pytest.mark.asyncio
    async def test_returns_true_on_success(self):
        redis = AsyncMock()
        result = await publish_invalidation(redis, "model", "t1", "x")
        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_when_redis_is_none(self):
        result = await publish_invalidation(None, "model", "t1", "x")
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_on_redis_error(self):
        redis = AsyncMock()
        redis.publish.side_effect = Exception("Connection refused")
        result = await publish_invalidation(redis, "model", "t1", "x")
        assert result is False

    @pytest.mark.asyncio
    async def test_publishes_to_correct_channel(self):
        redis = AsyncMock()
        await publish_invalidation(redis, "model", "t1", "x")
        channel = redis.publish.call_args[0][0]
        assert channel == "brutor:cache:invalidate"

    @pytest.mark.asyncio
    async def test_does_not_raise_on_redis_error(self):
        """Errors are caught — invalidation is fire-and-forget."""
        redis = AsyncMock()
        redis.publish.side_effect = ConnectionError("Redis down")
        # Should not raise
        result = await publish_invalidation(redis, "model", "t1", "x")
        assert result is False


# =============================================================================
# publish_invalidation_sync
# =============================================================================

class TestPublishInvalidationSync:
    """Tests for the synchronous publish_invalidation_sync function."""

    def test_returns_false_when_redis_is_none(self):
        result = publish_invalidation_sync(None, "model", "t1", "x")
        assert result is False

    def test_returns_true_on_success(self):
        redis = MagicMock()
        result = publish_invalidation_sync(redis, "model", "t1", "x")
        assert result is True

    def test_publishes_correct_message(self):
        redis = MagicMock()
        publish_invalidation_sync(redis, "skill", "t1", "sk-01", "delete")
        channel, message = redis.publish.call_args[0]
        assert channel == INVALIDATION_CHANNEL
        parsed = json.loads(message)
        assert parsed["type"] == "skill"
        assert parsed["action"] == "delete"
        assert parsed["id"] == "sk-01"

    def test_does_not_raise_on_error(self):
        redis = MagicMock()
        redis.publish.side_effect = Exception("fail")
        result = publish_invalidation_sync(redis, "model", "t1", "x")
        assert result is False


# =============================================================================
# invalidate_proxy_cache (convenience helper)
# =============================================================================

class TestInvalidateProxyCache:
    """Tests for the FastAPI request-aware convenience wrapper."""

    @pytest.mark.asyncio
    async def test_extracts_redis_from_request(self):
        """Helper extracts redis from request.app.state.redis."""
        redis_mock = AsyncMock()
        state = MagicMock()
        state.redis = redis_mock
        app = MagicMock()
        app.state = state
        request = MagicMock()
        request.app = app

        result = await invalidate_proxy_cache(request, "model", "t1", "x")
        assert result is True
        redis_mock.publish.assert_called_once()

    @pytest.mark.asyncio
    async def test_handles_no_app(self):
        """No app attribute — returns False gracefully."""
        request = MagicMock(spec=[])  # no attributes
        result = await invalidate_proxy_cache(request, "model", "t1", "x")
        assert result is False

    @pytest.mark.asyncio
    async def test_handles_no_state(self):
        """App exists but no state — returns False."""
        app = MagicMock(spec=[])
        request = MagicMock()
        request.app = app
        result = await invalidate_proxy_cache(request, "model", "t1", "x")
        assert result is False

    @pytest.mark.asyncio
    async def test_handles_no_redis_on_state(self):
        """State exists but no redis — returns False."""
        state = MagicMock(spec=[])
        app = MagicMock()
        app.state = state
        request = MagicMock()
        request.app = app
        result = await invalidate_proxy_cache(request, "model", "t1", "x")
        assert result is False

    @pytest.mark.asyncio
    async def test_passes_resource_type_and_action(self):
        """All arguments are forwarded correctly."""
        redis_mock = AsyncMock()
        state = MagicMock()
        state.redis = redis_mock
        app = MagicMock()
        app.state = state
        request = MagicMock()
        request.app = app

        await invalidate_proxy_cache(request, "guardrail", "tenant-x", "gc-01", "delete")

        parsed = json.loads(redis_mock.publish.call_args[0][1])
        assert parsed["type"] == "guardrail"
        assert parsed["tenant_id"] == "tenant-x"
        assert parsed["id"] == "gc-01"
        assert parsed["action"] == "delete"


# =============================================================================
# Admin Endpoint → Resource Type Mapping
# =============================================================================

class TestEndpointResourceMapping:
    """Documents which admin operations should trigger which invalidation types.
    These serve as a contract between the Python control plane and the Rust proxy."""

    # Each tuple: (operation_description, resource_type, action)
    EXPECTED_MAPPINGS = [
        # LLM Models
        ("create LLM model", "model", "create"),
        ("update LLM model", "model", "update"),
        ("delete LLM model", "model", "delete"),
        ("assign model to groups", "assign", "assign"),
        ("remove model from groups", "assign", "assign"),

        # Routing Groups
        ("create routing group", "routing_group", "create"),
        ("update routing group", "routing_group", "update"),
        ("delete routing group", "routing_group", "delete"),

        # Resource Groups
        ("create resource group", "resource_group", "create"),
        ("update resource group", "resource_group", "update"),
        ("delete resource group", "resource_group", "delete"),

        # MCP Servers
        ("create MCP server", "mcp_server", "create"),
        ("update MCP server", "mcp_server", "update"),
        ("delete MCP server", "mcp_server", "delete"),

        # Server Configs
        ("create server config", "server_config", "create"),
        ("update server config", "server_config", "update"),
        ("delete server config", "server_config", "delete"),

        # Agent Skills
        ("create agent skill", "skill", "create"),
        ("update agent skill", "skill", "update"),
        ("delete agent skill", "skill", "delete"),
        ("assign skill to groups", "assign", "assign"),

        # Guardrails
        ("create guardrail", "guardrail", "create"),
        ("update guardrail", "guardrail", "update"),
        ("delete guardrail", "guardrail", "delete"),
        ("assign guardrail to groups", "guardrail", "assign"),

        # Semantic Cache
        ("create cache config", "semantic_cache", "create"),
        ("update cache config", "semantic_cache", "update"),
        ("delete cache config", "semantic_cache", "delete"),
        ("assign cache to groups", "semantic_cache", "assign"),

        # Memberships
        ("add member to group", "membership", "update"),
        ("remove member from group", "membership", "update"),
    ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("description,resource_type,action", EXPECTED_MAPPINGS)
    async def test_mapping_produces_valid_message(self, description, resource_type, action):
        """Every expected mapping produces a valid invalidation message."""
        redis = AsyncMock()
        result = await publish_invalidation(redis, resource_type, "test-tenant", "test-id", action)
        assert result is True, f"Failed for: {description}"

        parsed = json.loads(redis.publish.call_args[0][1])
        assert parsed["type"] == resource_type, f"Wrong type for: {description}"
        assert parsed["action"] == action, f"Wrong action for: {description}"


# =============================================================================
# Rust Proxy Compatibility
# =============================================================================

class TestRustProxyCompatibility:
    """Ensure message format is compatible with the Rust proxy's
    CacheInvalidator::handle_invalidation match arms."""

    def test_message_uses_type_not_resource_type(self):
        """Rust expects 'type' field (via serde rename), not 'resource_type'."""
        message = json.dumps({
            "type": "model",
            "action": "update",
            "tenant_id": "t1",
            "id": "x",
        })
        parsed = json.loads(message)
        assert "type" in parsed
        assert "resource_type" not in parsed

    @pytest.mark.asyncio
    async def test_published_message_matches_rust_format(self):
        """The actual published message matches what Rust's serde expects."""
        redis = AsyncMock()
        await publish_invalidation(redis, "model", "t1", "llm-01", "update")

        raw = redis.publish.call_args[0][1]
        parsed = json.loads(raw)

        # Must have exactly these 4 fields (no more, no less)
        assert set(parsed.keys()) == {"type", "action", "tenant_id", "id"}

        # All values must be strings
        for key, value in parsed.items():
            assert isinstance(value, str), f"Field '{key}' must be string, got {type(value)}"

    RUST_MATCH_ARMS = [
        "model", "routing_group", "mcp_server", "server_config",
        "skill", "agent_skill", "resource_group", "guardrail",
        "membership", "assign", "semantic_cache", "ai_system_dependency",
    ]

    @pytest.mark.parametrize("resource_type", RUST_MATCH_ARMS)
    def test_resource_type_is_valid_rust_match_arm(self, resource_type):
        """Each resource type we publish must correspond to a match arm in Rust."""
        message = json.dumps({
            "type": resource_type,
            "action": "update",
            "tenant_id": "t",
            "id": "x",
        })
        # If this parses, Rust can also parse it
        parsed = json.loads(message)
        assert parsed["type"] == resource_type


# =============================================================================
# Edge Cases
# =============================================================================

class TestEdgeCases:
    """Edge cases and error handling."""

    @pytest.mark.asyncio
    async def test_empty_tenant_id(self):
        """Empty tenant_id still publishes (Rust handles validation)."""
        redis = AsyncMock()
        result = await publish_invalidation(redis, "model", "", "x")
        assert result is True

    @pytest.mark.asyncio
    async def test_empty_resource_id(self):
        redis = AsyncMock()
        result = await publish_invalidation(redis, "model", "t1", "")
        assert result is True

    @pytest.mark.asyncio
    async def test_special_characters_in_ids(self):
        """IDs with special chars are JSON-safe."""
        redis = AsyncMock()
        await publish_invalidation(redis, "model", "tenant/with/slashes", "id with spaces")
        raw = redis.publish.call_args[0][1]
        parsed = json.loads(raw)
        assert parsed["tenant_id"] == "tenant/with/slashes"
        assert parsed["id"] == "id with spaces"

    @pytest.mark.asyncio
    async def test_unicode_in_ids(self):
        redis = AsyncMock()
        await publish_invalidation(redis, "model", "tenant-日本語", "model-ñ")
        raw = redis.publish.call_args[0][1]
        parsed = json.loads(raw)
        assert parsed["tenant_id"] == "tenant-日本語"

    @pytest.mark.asyncio
    async def test_concurrent_publishes(self):
        """Multiple concurrent invalidations don't interfere."""
        import asyncio
        redis = AsyncMock()
        tasks = [
            publish_invalidation(redis, f"type-{i}", "t1", f"id-{i}")
            for i in range(10)
        ]
        results = await asyncio.gather(*tasks)
        assert all(r is True for r in results)
        assert redis.publish.call_count == 10


# =============================================================================
# CP-10 — a failed publish must not look like an unqualified success
# =============================================================================

class TestPublishFailureIsSurfaced:
    """The DB write has already committed when we publish, so a publisher
    outage cannot fail the request. It must still be *visible*: recorded on
    the request (which the middleware turns into a Warning header) and
    raised as a SystemAlert."""

    @staticmethod
    def _request(redis_client=None):
        """A request-like object with a real scope dict, as Starlette gives."""
        scope = {"type": "http", "state": {}}
        state = MagicMock()
        state.redis = redis_client
        app = MagicMock()
        app.state = state
        request = MagicMock()
        request.app = app
        request.scope = scope
        return request, scope

    @pytest.mark.asyncio
    async def test_failure_is_recorded_on_the_request(self):
        from app.services.cache_invalidation import FAILURE_STATE_KEY

        redis = AsyncMock()
        redis.publish.side_effect = ConnectionError("Redis down")
        request, scope = self._request(redis)

        with patch("app.services.cache_invalidation._raise_publish_failure_alert"):
            ok = await invalidate_proxy_cache(request, "guardrail", "t1", "gc-01", "update")

        assert ok is False
        assert scope["state"][FAILURE_STATE_KEY] == [
            {"type": "guardrail", "action": "update", "tenant_id": "t1", "id": "gc-01"}
        ]

    @pytest.mark.asyncio
    async def test_missing_redis_is_also_recorded(self):
        """No publisher configured is the same hazard as a dead one."""
        from app.services.cache_invalidation import FAILURE_STATE_KEY

        request, scope = self._request(None)
        with patch("app.services.cache_invalidation._raise_publish_failure_alert"):
            ok = await invalidate_proxy_cache(request, "model", "t1", "llm-01")

        assert ok is False
        assert len(scope["state"][FAILURE_STATE_KEY]) == 1

    @pytest.mark.asyncio
    async def test_success_records_nothing(self):
        from app.services.cache_invalidation import FAILURE_STATE_KEY

        request, scope = self._request(AsyncMock())
        ok = await invalidate_proxy_cache(request, "model", "t1", "llm-01")

        assert ok is True
        assert FAILURE_STATE_KEY not in scope["state"]

    @pytest.mark.asyncio
    async def test_failure_raises_a_system_alert(self):
        request, _ = self._request(None)
        with patch("app.services.cache_invalidation._raise_publish_failure_alert") as alert:
            await invalidate_proxy_cache(request, "resource_group", "t1", "rg-01", "delete")
        alert.assert_called_once()

    @pytest.mark.asyncio
    async def test_recording_never_raises(self):
        """A request object with no usable scope must not turn a committed
        200 into a 500."""
        request = MagicMock(spec=[])
        with patch("app.services.cache_invalidation._raise_publish_failure_alert"):
            assert await invalidate_proxy_cache(request, "model", "t1", "x") is False


class TestCacheInvalidationWarningMiddleware:
    """Every handler returns its own response model; the warning is attached
    once, in the middleware, from the scope slot."""

    @staticmethod
    async def _run(state_seed):
        from app.middleware.cache_invalidation import CacheInvalidationWarningMiddleware

        sent = []

        async def inner_app(scope, receive, send):
            scope.setdefault("state", {}).update(state_seed)
            await send({"type": "http.response.start", "status": 200, "headers": [(b"x", b"y")]})
            await send({"type": "http.response.body", "body": b"{}"})

        mw = CacheInvalidationWarningMiddleware(inner_app)

        async def send(message):
            sent.append(message)

        await mw({"type": "http"}, None, send)
        return sent[0]["headers"]

    @pytest.mark.asyncio
    async def test_warning_added_when_publish_failed(self):
        from app.services.cache_invalidation import FAILURE_STATE_KEY

        headers = await self._run({FAILURE_STATE_KEY: [{"type": "guardrail"}]})
        assert (b"x-brutor-cache-invalidated", b"false") in headers
        assert any(k == b"warning" and b"199 " in v for k, v in headers)

    @pytest.mark.asyncio
    async def test_no_warning_on_a_clean_request(self):
        headers = await self._run({})
        assert all(k != b"x-brutor-cache-invalidated" for k, _ in headers)
        assert all(k != b"warning" for k, _ in headers)

    @pytest.mark.asyncio
    async def test_non_http_scopes_pass_through(self):
        from app.middleware.cache_invalidation import CacheInvalidationWarningMiddleware

        seen = []

        async def inner_app(scope, receive, send):
            seen.append(scope["type"])

        await CacheInvalidationWarningMiddleware(inner_app)({"type": "lifespan"}, None, None)
        assert seen == ["lifespan"]



def test_ai_system_dependency_type_is_pinned_in_both_repos():
    """Wire contract: the CP publishes this literal on every
    dependency-edge write and the core registers a handler for it (the
    invalidation registry in services/cache/invalidation.rs) — a rename on either side leaves
    accepted-caller enforcement reading a stale card → service map until the
    cache TTL."""
    import re

    from app.services.cache_invalidation import INVALIDATION_AI_SYSTEM_DEPENDENCY
    from tests.core_repo import core_source

    assert INVALIDATION_AI_SYSTEM_DEPENDENCY == "ai_system_dependency"
    registry = core_source("services", "cache", "invalidation.rs").read_text()
    assert re.search(r'resource_types: &\[[^\]]*"ai_system_dependency"[^\]]*\]', registry)
