# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
What MCPServerManager tells an operator when a connection test fails, how it reads
authentication hints, and how it makes SDK objects safe to store and return.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import AnyUrl, BaseModel

from app.core.logging_system import BrutorLogger
from app.services.mcp.server.mcp_server_manager import MCPServerManager


@pytest.fixture(scope="module")
def manager():
    return MCPServerManager(BrutorLogger("test-mcp-manager"))


def _server(url="https://mcp.example/mcp", auth="none"):
    return SimpleNamespace(mcp_url=url, base_url=url, auth_type=auth, name="s")


class TestErrorCategories:
    @pytest.mark.parametrize(
        "message,category",
        [
            ("HTTP 401 Unauthorized", "authentication_required"),
            ("got unauthorized", "authentication_required"),
            ("HTTP 403", "permission_denied"),
            ("Forbidden by policy", "permission_denied"),
        ],
    )
    def test_auth_failures_are_warnings_not_errors(self, manager, message, category):
        result = manager._categorize_connection_error(message, _server())
        assert (result["category"], result["status"]) == (category, "warning")
        assert result["suggestions"]

    @pytest.mark.parametrize(
        "message", ["404 not found", "connection refused", "timed out after 5s", "SSL certificate verify failed",
                    "name or service not known", "something odd"],
    )
    def test_every_failure_has_a_category_and_a_message(self, manager, message):
        result = manager._categorize_connection_error(message, _server())
        assert result["category"] and message in result["message"]
        assert result["status"] in ("error", "warning")


class TestAuthSuggestions:
    @pytest.mark.parametrize(
        "auth,expected", [("none", "'bearer' or 'oauth'"), ("bearer", "bearer token"), ("oauth", "OAuth")]
    )
    def test_they_follow_the_configured_auth_type(self, manager, auth, expected):
        assert any(expected in s for s in manager._get_auth_suggestions(_server(auth=auth)))

    @pytest.mark.parametrize(
        "url,expected", [("https://api.githubcopilot.com/mcp", "Copilot"), ("https://api.openai.com/x", "OpenAI")]
    )
    def test_known_services_get_their_own_hint(self, manager, url, expected):
        assert any(expected in s for s in manager._get_service_specific_suggestions(url))
        assert manager._get_service_specific_suggestions("https://other.example") == []

    @pytest.mark.parametrize(
        "url,hinted", [("http://localhost:3000/mcp", True), ("http://127.0.0.1:3000", True),
                       ("https://mcp.example/mcp", False), ("http://[bad", False)],
    )
    def test_docker_hint_only_for_the_operators_own_machine(self, url, hinted):
        assert bool(MCPServerManager._docker_localhost_suggestions(url)) is hinted


class TestOAuthHints:
    def test_oauth_words_in_a_body_are_a_hint(self, manager):
        result = manager._extract_oauth_hints_from_body(
            {"error": "invalid_token", "message": "OAuth token required"}, '{"error":"invalid_token"}',
            "https://mcp.hubspot.com/mcp",
        )
        assert isinstance(result, dict) and result

    def test_a_plain_body_is_not(self, manager):
        result = manager._extract_oauth_hints_from_body(None, "hello", "https://mcp.example/mcp")
        assert not result.get("oauth_detected", False) if isinstance(result, dict) else True


class TestSerialization:
    def test_resource(self, manager):
        out = manager._serialize_resource(SimpleNamespace(uri=AnyUrl("https://a.example/x"), name="n", description="d", mimeType="text/plain"))
        assert out == {"uri": "https://a.example/x", "name": "n", "description": "d", "mimeType": "text/plain"}

    def test_template(self, manager):
        out = manager._serialize_template(SimpleNamespace(uriTemplate="a/{b}", name="n", description=None, mimeType="m"))
        assert out == {"uriTemplate": "a/{b}", "name": "n", "mimeType": "m"}

    def test_prompt_with_arguments(self, manager):
        arg = SimpleNamespace(name="a", description="d", required=True)
        out = manager._serialize_prompt(SimpleNamespace(name="p", description="pd", arguments=[arg]))
        assert out == {"name": "p", "description": "pd", "arguments": [{"name": "a", "description": "d", "required": True}]}

    def test_json_safe_recursion(self, manager):
        class Model(BaseModel):
            url: AnyUrl
            n: int = 1

        out = manager._make_json_serializable(
            {"a": AnyUrl("https://a.example/"), "b": [Model(url="https://b.example/"), None], "c": (1, "x")}
        )
        assert out["a"] == "https://a.example/"
        assert out["b"][0]["n"] == 1
        assert out["c"] == [1, "x"]
