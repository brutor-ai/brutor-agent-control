# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Live GOVERNANCE suite — UI verification. Beyond asserting the audit row via the
API, this proves the **Admin Console renders the governed outcome**: it makes a
deterministic governed call (an LLM prompt-injection block → a 403 guardrail
row), then drives the console (System Logs → Proxy Logs) via the Playwright
admin harness in brutor-video-gen and asserts the most-recent row on screen is
that call, showing its 403 + guardrail block.

Best-effort: skips cleanly if Node / the admin UI / brutor-video-gen aren't
available (this is a UI smoke, not a hard gate).

Run:  pytest tests/live/governance/test_ui.py -m live_e2e
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from .helpers import llm_chat
from .test_llm import INJECTION, ROUTING_GROUP

UI_SCRIPT = Path(__file__).parent / "ui_assert_proxy_log.mjs"


def _run_ui_probe(timeout: float = 120.0) -> dict:
    node = os.environ.get("BRUTOR_NODE", "node")
    try:
        proc = subprocess.run(
            [node, str(UI_SCRIPT)], capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        pytest.skip("node not found — install Node or set BRUTOR_NODE")
    except subprocess.TimeoutExpired:
        pytest.skip("UI probe timed out (admin UI unreachable?)")
    lines = [l for l in proc.stdout.splitlines() if l.strip().startswith("{")]
    if not lines:
        pytest.skip(f"UI probe produced no JSON — admin UI / playwright unavailable: "
                    f"{(proc.stderr or proc.stdout)[-300:]}")
    return json.loads(lines[-1])


def test_console_renders_governed_row(proxy_client, base_scaffolding):
    """Make a governed call, then confirm the console's Proxy Logs shows that
    exact row with its 403 + guardrail block."""
    call = llm_chat(proxy_client, base_scaffolding["SALES_KEY"], model_id=ROUTING_GROUP,
                    messages=[{"role": "user", "content": INJECTION}])
    assert call.status == 403, f"expected a guardrail block to render; call was {call.status}"
    time.sleep(2.0)  # let the batched proxy-log writer flush before the UI reads

    res = _run_ui_probe()
    if not res.get("ok"):
        pytest.skip(f"UI harness error: {res.get('error')}")

    text = res.get("text", "")
    corrs = res.get("correlation_ids", [])
    assert call.corr in corrs or call.corr in text, \
        "the call's row is not the most-recent one rendered in the console Proxy Logs"
    assert "403" in text, "console detail should show the 403 status"
    assert "guardrail" in text.lower() or "block" in text.lower(), \
        "console detail should show the guardrail block"
