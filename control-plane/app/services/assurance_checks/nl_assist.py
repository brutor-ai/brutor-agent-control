# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
NL → check-spec compile assist — the last, smallest piece.

Natural language NEVER executes. This proposes a spec; the operator
reviews it line by line; nothing persists as active without explicit
approval through the normal create/enable flow. If the question cannot be
answered deterministically over run facts, the proposal says so and
offers tier B — the downgrade is visible, never silent.
"""

import json
import re
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app.constants.assurance import RUN_FACT_GUIDE
from app.database.models.tenant import Tenant
from app.services.assurance_checks import core_client
from app.services.assurance_checks.judge import JudgeExecutor

ASSIST_MAX_TOKENS = 700

_PROMPT_TEMPLATE = """You translate an operator's assurance question into a \
Brutor Continuous Assurance Check spec. Two tiers exist:

* tier "deterministic": a CEL boolean/number expression over a `run` object \
of per-run ledger facts. STRONGLY preferred — use it whenever the question \
is answerable from the facts below. Only the variable `run` exists. \
Allowed functions: has, size, exists, exists_one, all, filter, map, \
contains, matches, startsWith, endsWith, string, int, double, uint.
* tier "semantic": an LLM-judge rubric over run content. Use ONLY when the \
question needs to read message content (e.g. "did the assistant reveal the \
system prompt?").

`run` fields:
{fields}

Respond with ONLY a JSON object:
{{
  "tier": "deterministic" | "semantic",
  "reason": "<one sentence: why this tier>",
  "evaluator": {{"expression": "<CEL>"}} or {{"rubric": "<judge instructions>", "sample_rate": 1.0, "max_tokens": 300}},
  "output": "boolean" | "score" | "count" | "classification" | "extraction",
  "threshold": {{"kind": "value", "op": "eq", "value": true, "severity": "warning" | "critical"}} or null
}}

Operator's question: {question}
{scope_note}"""


def _fields_block() -> str:
    return "\n".join(f"- run.{k}: {v}" for k, v in RUN_FACT_GUIDE.items())


def compile_assist(
    db: Session,
    tenant_id: str,
    question: str,
    logger,
    *,
    scope_hint: Optional[str] = None,
    executor: Optional[JudgeExecutor] = None,
) -> Dict[str, Any]:
    """Propose a spec for an NL question. Returns
    ``{proposal, compile, downgraded, model}`` — never persists anything.
    """
    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    model = tenant.default_judge_model_id if tenant else None
    if not model:
        return {
            "proposal": None,
            "error": "No tenant default judge model configured — the compile "
            "assist uses a governed system model",
        }

    prompt = _PROMPT_TEMPLATE.format(
        fields=_fields_block(),
        question=question.strip(),
        scope_note=f"Scope hint: {scope_hint}" if scope_hint else "",
    )
    executor = executor or JudgeExecutor(logger)
    try:
        answer, _tokens = executor.judge_call(
            tenant_id, model, prompt, ASSIST_MAX_TOKENS
        )
    except Exception as exc:  # noqa: BLE001 — surfaced, not raised
        return {"proposal": None, "error": f"assist model call failed: {exc}"}

    match = re.search(r"\{.*\}", answer, re.DOTALL)
    if not match:
        return {"proposal": None, "error": "assist model returned no JSON"}
    try:
        proposal = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        return {"proposal": None, "error": f"assist model returned bad JSON: {exc}"}

    result: Dict[str, Any] = {
        "proposal": proposal,
        "model": model,
        "downgraded": proposal.get("tier") == "semantic",
        "compile": None,
    }

    # A proposed deterministic expression is immediately compile-checked so
    # the operator reviews a spec the evaluator has already accepted (or
    # sees exactly why it was refused).
    if proposal.get("tier") == "deterministic":
        expression = (proposal.get("evaluator") or {}).get("expression")
        if expression:
            try:
                result["compile"] = core_client.compile_expression(
                    tenant_id, expression, proposal.get("output")
                )
            except core_client.CoreUnavailable as exc:
                result["compile"] = {"ok": False, "errors": [str(exc)]}
    return result
