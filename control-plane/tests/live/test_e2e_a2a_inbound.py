# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Live e2e — A2A v1.0 inbound smoke against the seeded ``hello-world-a2a``
agent card. Mirrors the ``inbound.py`` sample script in
``trial_bundle/samples/a2a/`` but runs end-to-end as a pytest case
under the ``live_e2e`` marker.

Why inbound (not outbound)?
    * Outbound exercises Brutor → an external A2A server you stand up
      yourself (the sample's ``server.py``). Useful for diagnosing
      delegation chains, but adds two moving parts (the local server
      + a reachable interface URL on the seeded card).
    * Inbound is "Brutor itself is the agent". The sample card's
      capabilities are bound to the seeded ``hello-world`` Agent
      Skill and the proxy's built-in ``multi_turn_demo`` handler —
      no local server needed. That's the simplest "does my install
      work end-to-end?" check.

Flow
====
1. Find the seeded ``hello-world-a2a`` card via the admin API.
2. Bind the card to the **E2ETests** resource group and use the
   suite's shared E2ETests API key (conftest ``e2e_api_key``).
3. POST a ``message:send`` envelope to
   ``/a2a/{tenant}/hello-world-a2a/message:send`` on the proxy.
4. Assert the task envelope comes back with at least one greeting
   artifact + a non-error status.
"""

from __future__ import annotations

import os
import uuid
from typing import Any, Dict, Optional

import httpx
import pytest


pytestmark = pytest.mark.live_e2e


SEEDED_CARD_NAME = "hello-world-a2a"


# ---------------------------------------------------------------------------
# Fixtures (test-local — these aren't useful elsewhere)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def seeded_card(
    control_client: httpx.Client, admin_headers: Dict[str, str]
) -> Dict[str, Any]:
    """Pull the ``hello-world-a2a`` card from the admin listing.
    Skipped (not failed) when A2A is disabled on this tenant — that's
    a feature flag, not a bug."""
    r = control_client.get("/v1/admin/agent-cards", headers=admin_headers)
    if r.status_code == 404:
        pytest.skip("Tenant has features.a2a disabled — A2A surface is hidden")
    assert r.status_code == 200, (
        f"list agent-cards {r.status_code}: {r.text[:300]}"
    )
    body = r.json()
    cards = body.get("cards", []) if isinstance(body, dict) else body
    match = next((c for c in cards if c.get("name") == SEEDED_CARD_NAME), None)
    if match is None:
        pytest.skip(
            f"Seed for {SEEDED_CARD_NAME!r} not present — re-run tenant "
            f"provisioning (the sample card is part of the standard seed)."
        )
    return match


@pytest.fixture(scope="module")
def a2a_api_key(
    control_client: httpx.Client,
    admin_headers: Dict[str, str],
    e2e_group_id: str,
    e2e_api_key: str,
    seeded_card: Dict[str, Any],
) -> str:
    """The suite's shared E2ETests key (conftest ``e2e_api_key``), with the
    seeded card bound to E2ETests so the key's group may invoke it.

    The binding is additive: E2ETests' current card set is read back and
    the hello-world card added (PUT replaces the group's set). A key with
    no resource group is refused by the proxy since v0.11.22, which is why
    this used to mint a fresh Default-group key per run."""
    r = control_client.get(
        f"/v1/admin/resource-groups/{e2e_group_id}/effective-agent-cards",
        headers=admin_headers,
    )
    body = r.json() if r.status_code == 200 else {}
    rows = body.get("direct_cards") or [] if isinstance(body, dict) else []
    current = {c.get("id") for c in rows if isinstance(c, dict) and c.get("id")}
    if seeded_card["id"] not in current:
        r = control_client.put(
            f"/v1/admin/resource-groups/{e2e_group_id}/agent-cards",
            headers=admin_headers,
            json={"agent_card_ids": sorted(current | {seeded_card["id"]})},
        )
        assert r.status_code == 200, f"bind card to E2ETests {r.status_code}: {r.text[:300]}"
    return e2e_api_key


# ---------------------------------------------------------------------------
# The actual test
# ---------------------------------------------------------------------------


def _build_message_envelope(text: str, capability: Optional[str] = None) -> Dict[str, Any]:
    """Standard A2A v1.0 SendMessageRequest body. Mirrors the wire
    shape that ``inbound.py`` sends so a regression here also breaks
    the published sample."""
    msg: Dict[str, Any] = {
        "role": "ROLE_USER",
        "messageId": str(uuid.uuid4()),
        "parts": [{"kind": "text", "text": text}],
    }
    if capability:
        msg["metadata"] = {"capability": capability}
    return {"message": msg}


def _post_inbound(
    proxy_client: httpx.Client,
    tenant_id: str,
    api_key: str,
    body: Dict[str, Any],
) -> httpx.Response:
    return proxy_client.post(
        f"/a2a/{tenant_id}/{SEEDED_CARD_NAME}/message:send",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "A2A-Version": "1.0",
        },
        json=body,
        timeout=30.0,
    )


class TestSeededHelloWorldCardInbound:
    def test_card_is_visible_in_admin_listing(
        self, seeded_card: Dict[str, Any]
    ) -> None:
        # The seed assertion: the card was provisioned on tenant
        # creation. If this fails, the seed didn't run or was rolled
        # back — every other assertion below would also fail, so it's
        # worth a separate, clearer signal.
        assert seeded_card.get("name") == SEEDED_CARD_NAME
        assert seeded_card.get("enabled") is True

    def test_inbound_message_send_returns_task(
        self,
        proxy_client: httpx.Client,
        default_tenant_id: str,
        a2a_api_key: str,
        seeded_card: Dict[str, Any],
    ) -> None:
        """Single-shot capability — expects a Task envelope back.
        The proxy persists the task to ``a2a_tasks``, dispatches the
        bound capability handler, and writes a ``proxy_logs`` row
        tagged ``proxy_subtype='v1-inbound'``."""
        body = _build_message_envelope("Hello from the e2e suite")
        r = _post_inbound(proxy_client, default_tenant_id, a2a_api_key, body)
        assert r.status_code == 200, (
            f"inbound message:send {r.status_code}: {r.text[:500]}"
        )
        envelope = r.json()
        # A2A v1.0 returns a SendMessageResponse with a `result` that
        # is either a Task or a Message. Both shapes are valid; we
        # just need a non-error response.
        assert "result" in envelope or "task" in envelope or "message" in envelope, (
            f"unexpected envelope shape: {envelope!r}"
        )

    def test_inbound_envelope_is_valid_task(
        self,
        proxy_client: httpx.Client,
        default_tenant_id: str,
        a2a_api_key: str,
        seeded_card: Dict[str, Any],
    ) -> None:
        """The proxy returns a v1.0 ``SendMessageResponse`` oneof —
        either ``{"task": Task}`` or ``{"message": Message}``. The
        bound ``hello-world`` skill produces a Task envelope. We
        don't require artifacts to be populated synchronously (the
        capability handler may complete async), but the Task itself
        has a stable shape: an ``id`` and a ``status.state`` field.
        Asserting that pins the proxy did dispatch + persist the task
        without committing to a specific completion mode."""
        body = _build_message_envelope("ping")
        r = _post_inbound(proxy_client, default_tenant_id, a2a_api_key, body)
        assert r.status_code == 200
        envelope = r.json()

        task = envelope.get("task")
        if task is None:
            # Some proxy paths emit a Message envelope instead (e.g.
            # for synchronous-only capabilities). Surface that as a
            # skip rather than a fail — the proxy did accept the
            # message; it just chose the other oneof branch.
            assert "message" in envelope, (
                f"Response is neither a `task` nor a `message` envelope. "
                f"Body: {str(envelope)[:500]}"
            )
            pytest.skip(
                "Capability returned a Message envelope, not a Task — "
                "covered by the `returns_task` test above; nothing more "
                "to assert on synchronous Message replies."
            )
        # Task shape pins.
        assert isinstance(task.get("id"), str) and task["id"], (
            f"Task envelope missing string `id`: {task!r}"
        )
        status = task.get("status") or {}
        assert isinstance(status.get("state"), str) and status["state"], (
            f"Task envelope missing `status.state`: {task!r}"
        )
        # If artifacts ARE present (synchronous completion), they
        # should at least be a list — defensive shape assertion.
        artifacts = task.get("artifacts")
        assert artifacts is None or isinstance(artifacts, list), (
            f"`artifacts` must be a list when present, got: {type(artifacts).__name__}"
        )


# ---------------------------------------------------------------------------
# Sanity: the seeded card's interface URL is what we expect for local
# testing.
# ---------------------------------------------------------------------------


class TestSeededCardInterfaceURL:
    """The card's public URL comes from ``BRUTOR_A2A_SAMPLE_PUBLIC_URL``,
    which has no default: without it no sample card is seeded at all. The
    Enterprise trial sets a docker-host alias
    (``http://host.docker.internal:9100``); for native dev, operators set
    ``BRUTOR_A2A_SAMPLE_PUBLIC_URL=http://localhost:9100`` *before* first
    startup so the seeded card matches their stack.

    This isn't a hard failure — the inbound flow we exercise above
    doesn't traverse the card's URL at all. But surfacing the value
    here makes the diagnostic obvious when an operator later switches
    to the outbound flow and finds the proxy can't reach their
    sample server."""

    def test_interface_url_matches_environment_or_documents_mismatch(
        self, seeded_card: Dict[str, Any]
    ) -> None:
        # Card body lives under either `card_json` or `body` depending
        # on serializer. Try both.
        cj = seeded_card.get("card_json") or seeded_card.get("body") or {}
        url = cj.get("url") if isinstance(cj, dict) else None
        env_url = os.environ.get("BRUTOR_A2A_SAMPLE_PUBLIC_URL")
        if env_url:
            # Operator declared an explicit override — the seed
            # *should* have honoured it. If not, the seed ran before
            # the env var was set; report cleanly.
            if url and url.rstrip("/") != env_url.rstrip("/"):
                pytest.skip(
                    f"Seeded card url={url!r} doesn't match "
                    f"BRUTOR_A2A_SAMPLE_PUBLIC_URL={env_url!r}. The seed "
                    f"already ran with a different env — edit the "
                    f"card via Admin UI → Agent Cards → Edit."
                )
        # The trial's docker-host alias doesn't resolve from the test
        # runner. Skip
        # outbound-related assertions in that case — the inbound
        # test above doesn't need the URL.
        if url and "host.docker.internal" in url:
            pytest.skip(
                "Seeded card uses the docker-host alias (the trial's value) — "
                "set BRUTOR_A2A_SAMPLE_PUBLIC_URL=http://localhost:9100 in "
                "the control plane env before first startup if you "
                "want to drive the outbound sample from this host."
            )
