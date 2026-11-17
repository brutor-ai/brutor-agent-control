# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Per-session cost ceiling, driven through the real request path.

Everything else about this feature is covered by unit tests: the decision
table in `session_budget.rs`, the ledger SQL against the live schema, the
watermark semantics in `test_proxy_log_session_filter.py`. None of that
proves the gate is actually *wired* — that the header reaches the proxy,
that the envelope term is read, that spend accumulates against the derived
session key, and that a call is genuinely refused. That is what this does.

The scenario is the one the feature exists for:

    A conversation is capped at a fraction of a cent. Two messages later it
    has spent more than that, and the third is refused — while a *different*
    conversation on the same system, same user, same model, sails through.

The cap is set absurdly low (a few calls' worth) on purpose. A realistic $25 ceiling would need
hundreds of real provider calls to reach; the enforcement path is identical
either way, and the point is to exercise the gate, not to spend money.

Requires: running stack + one provider key. See tests/live/README.md.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from typing import Any, Dict, List, Optional

import httpx
import pytest


# The gate RESERVES each call's worst case
# against the ceiling before dispatch and settles to the actual cost after:
# input is bounded at the prompt plus a 1024-token overhead allowance, and
# output is squeezed to fit the remaining headroom (refused below 256
# tokens). A fixed $0.000001 cap therefore refused even the first message
# ("$0.0000 of $0.0000"), which the old read-then-decide gate let through.
#
# So the cap is derived from the model's price: room for one call's input
# bound plus ~1000 output tokens. The first call asks for a long answer and
# is squeezed to ~1000 tokens; once it has spent most of that, a second call
# no longer fits even at the 256-token minimum, and is refused.
CAP_INPUT_TOKENS = 1100
CAP_OUTPUT_TOKENS = 1000
LONG_PROMPT = "List every integer from 1 to 3000, one per line, nothing else."
LONG_MAX_TOKENS = 4000

POSTGRES_CONTAINER = os.environ.get("POSTGRES_CONTAINER", "brutor-postgres")
PG_USER = os.environ.get("POSTGRES_USER", "brutor")
PG_DB = os.environ.get("POSTGRES_DB", "ai_gateway")


@pytest.fixture(scope="module")
def live_chat_model(e2e_chat_model: str) -> str:
    """A chat model the trial user can actually call — the first reachable
    E2E_MODELS chat model (conftest)."""
    return e2e_chat_model


def _psql(sql: str) -> str:
    """Run one statement in the stack's Postgres container.

    The envelope is set directly rather than through
    `PUT /v1/admin/ai-systems/{id}/envelope` because that route is
    AI-System-scoped (422 on a plain resource group), and the group the
    trial user's traffic actually routes through is E2ETests.
    Enforcement reads `resource_groups.operating_envelope` for ANY group,
    so this exercises the real gate — it just sets the input the way the
    admin API cannot for this group type.
    """
    out = subprocess.run(
        ["docker", "exec", POSTGRES_CONTAINER, "psql", "-U", PG_USER,
         "-d", PG_DB, "-tAc", sql],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        pytest.skip(f"cannot reach the stack's database: {out.stderr[:200]}")
    return out.stdout.strip()


def _envelope(group_id: str) -> Optional[str]:
    """The group's envelope as JSON text, or None when the column is NULL.

    NULL must round-trip as NULL: this used to COALESCE to '{}' and restore
    that, silently turning "no envelope" into an empty declared one."""
    out = _psql(
        f"SELECT COALESCE(operating_envelope::text, '<null>') "
        f"FROM resource_groups WHERE id = '{group_id}'"
    )
    return None if out in ("", "<null>") else out


def _set_envelope(group_id: str, envelope_json: Optional[str]) -> None:
    if envelope_json is None:
        value = "NULL"
    else:
        value = "'" + envelope_json.replace("'", "''") + "'::jsonb"
    _psql(
        f"UPDATE resource_groups SET operating_envelope = {value} "
        f"WHERE id = '{group_id}'"
    )


def _chat(
    proxy_client: httpx.Client,
    headers: Dict[str, str],
    model: str,
    session_id: str | None,
    text: str = "Say OK.",
    max_tokens: int = 8,
) -> httpx.Response:
    h = dict(headers)
    if session_id:
        h["X-Brutor-Session-Id"] = session_id
    return proxy_client.post(
        "/v1/proxy/llm/chat/completions",
        headers=h,
        json={
            "model": model,
            "messages": [{"role": "user", "content": text}],
            "max_tokens": max_tokens,
        },
        timeout=120.0,
    )


@pytest.fixture(scope="module")
def session_cap_usd(
    live_chat_model: str, e2e_models: Dict[str, List[Dict[str, Any]]]
) -> float:
    """A ceiling worth one squeezed long call (see CAP_* above), priced from
    the configured model's per-million-token rates."""
    row = next(
        (m for rows in e2e_models.values() for m in rows
         if m.get("model_name") == live_chat_model),
        None,
    )
    assert row, f"{live_chat_model} not among the provisioned E2E models"
    in_rate = float(row.get("cost_per_input_token") or 0)
    out_rate = float(row.get("cost_per_output_token") or 0)
    assert in_rate > 0 and out_rate > 0, f"{live_chat_model} has no price: {row}"
    return (CAP_INPUT_TOKENS * in_rate + CAP_OUTPUT_TOKENS * out_rate) / 1_000_000


@pytest.fixture
def capped_group(e2e_group_id: str, session_cap_usd: float):
    """Put a tiny session ceiling on the trial group, then restore it.

    Restoring in a finally is not optional: leaving a tiny cap behind
    would refuse every later test in the suite with an error that looks
    nothing like the cause.
    """
    original = _envelope(e2e_group_id)
    capped = json.loads(original) if original else {}
    capped["max_cost_per_session_usd"] = session_cap_usd
    _set_envelope(e2e_group_id, json.dumps(capped))
    # The gate caches caps briefly; give the change a moment to land.
    time.sleep(1)
    try:
        yield e2e_group_id
    finally:
        _set_envelope(e2e_group_id, original)


@pytest.mark.live_e2e
def test_session_ceiling_refuses_once_the_conversation_is_over_budget(
    proxy_client: httpx.Client,
    trial_headers: Dict[str, str],
    live_chat_model: str,
    capped_group: str,
):
    """The gate fires — and only for the conversation that overspent."""
    session_a = f"e2e-session-{uuid.uuid4()}"

    first = _chat(
        proxy_client, trial_headers, live_chat_model, session_a,
        text=LONG_PROMPT, max_tokens=LONG_MAX_TOKENS,
    )
    # The first call is allowed: the conversation has spent nothing and the
    # call's reserved worst case fits under the cap (see CAP_* above).
    assert first.status_code == 200, f"first call should pass: {first.text[:300]}"

    # Second call: the first one's cost is now on the ledger, so the ceiling
    # is already breached. Spend is read from the LEDGER, and proxy logs are
    # written by a batching background writer — so the first call's cost is
    # not visible the instant it returns. Poll rather than sleep once, so a
    # fast writer finishes early.
    #
    # The window here is deliberately SHORTER than the session cache TTL
    # (30s), and that is the whole point of this assertion.
    #
    # Session spend is read through a short-TTL Redis cache. The first message
    # of a conversation reads the ledger before its own row exists and caches
    # the honest answer — zero. If nothing then adds each call's cost to that
    # cached total, every later message inside the TTL reads the zero back and
    # the ceiling cannot fire: the conversation spends without limit until the
    # key expires. That is exactly the bug this test exists to catch, and a
    # generous poll window would hide it by simply outwaiting the TTL and then
    # passing for the wrong reason.
    deadline = time.monotonic() + 20  # < the 30s cache TTL, on purpose
    blocked = None
    while time.monotonic() < deadline:
        r = _chat(proxy_client, trial_headers, live_chat_model, session_a)
        if r.status_code == 429:
            blocked = r
            break
        time.sleep(2)
    assert blocked is not None, (
        "session ceiling never fired within the cache TTL — the cached "
        "session total is not tracking spend, so a conversation can exceed "
        "its ceiling freely until the cache expires"
    )

    body = blocked.text
    assert "max_cost_per_session_usd" in body or "conversation" in body.lower(), (
        f"429 did not identify the session ceiling: {body[:300]}"
    )


@pytest.mark.live_e2e
def test_a_different_conversation_is_unaffected(
    proxy_client: httpx.Client,
    trial_headers: Dict[str, str],
    live_chat_model: str,
    capped_group: str,
):
    """The cap is scoped to ONE conversation, not to the user or the group.

    This is the assertion that separates a session ceiling from the daily
    quota it would otherwise be indistinguishable from: burn one chat's
    budget, and a brand-new chat still works.
    """
    burned = f"e2e-session-{uuid.uuid4()}"
    for _ in range(10):
        if _chat(
            proxy_client, trial_headers, live_chat_model, burned,
            text=LONG_PROMPT, max_tokens=LONG_MAX_TOKENS,
        ).status_code == 429:
            break
        time.sleep(2)

    fresh = f"e2e-session-{uuid.uuid4()}"
    r = _chat(proxy_client, trial_headers, live_chat_model, fresh)
    assert r.status_code == 200, (
        f"a NEW conversation must not inherit another's spend: "
        f"{r.status_code} {r.text[:300]}"
    )


@pytest.mark.live_e2e
def test_traffic_without_a_session_is_never_gated(
    proxy_client: httpx.Client,
    trial_headers: Dict[str, str],
    live_chat_model: str,
    capped_group: str,
):
    """No asserted session → ungated, even under a near-zero ceiling.

    Fail-open is deliberate: service-to-service callers have no conversation,
    and a cap they cannot scope to must not refuse them. If this ever starts
    failing, the feature has begun blocking traffic nobody asked it to.
    """
    r = _chat(proxy_client, trial_headers, live_chat_model, session_id=None)
    assert r.status_code == 200, (
        f"unsessioned traffic must not be gated: {r.status_code} {r.text[:300]}"
    )
