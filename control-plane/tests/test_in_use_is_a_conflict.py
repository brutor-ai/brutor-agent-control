# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
"It is in use" has one status in the admin API: 409.

A delete that is refused because something else still depends on the row
is a conflict with the state of the configuration, not a malformed
request. Each such refusal is listed here by the sentence it answers
with, and answers 409; the routes themselves are exercised in their own
test files.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ENDPOINTS = Path(__file__).resolve().parents[1] / "app" / "api" / "v1" / "endpoints" / "admin"

# (module, the start of the refusal's sentence)
IN_USE = [
    ("admin_rbac.py", "admin(s) currently assigned to this role"),
    ("mcp_servers.py", "Cannot delete server. It is referenced by"),
    ("oauth_app_clients.py", "Cannot delete OAuth client used by"),
    ("idp_servers.py", "is in use and is not deleted"),
    ("llm_providers.py", "Cannot delete: {in_use} model(s) still reference"),
    ("llm_models.py", "Remove the model from all non-Default resource groups first."),
    ("llm_models.py", "from the routing group first."),
]
# The ones that answer with what uses the row, for a program to read
# (`detail.used_by`, `app.api.v1.models.admin.in_use`).
SAY_WHAT_USES_IT = {"admin_rbac.py", "mcp_servers.py", "oauth_app_clients.py", "idp_servers.py"}


@pytest.mark.parametrize("module, sentence", IN_USE, ids=[f"{m}:{s[:28]}" for m, s in IN_USE])
def test_an_in_use_refusal_is_a_409(module, sentence):
    source = (ENDPOINTS / module).read_text(encoding="utf-8")
    assert source.count(sentence) == 1, sentence
    at = source.index(sentence)
    before, after = source[max(0, at - 500):at], source[at:at + 600]
    raised = max(before.rfind("raise in_use("), before.rfind("raise HTTPException("))
    if raised != -1 and before[raised:].startswith("raise in_use("):
        status = "409"  # `in_use` answers nothing else
    elif raised != -1:
        status = re.findall(r"status_code=([\w.]+)", before[raised:])[0]
    else:
        # The sentence is built first and raised just after.
        status = re.findall(r"HTTPException\(status_code=([\w.]+)", after)[0]
    assert status in ("409", "status.HTTP_409_CONFLICT"), (module, sentence, status)
    if module in SAY_WHAT_USES_IT:
        assert "used_by" in source[at - 500:at + 900] or "raise in_use(" in before


def test_the_refusal_says_what_uses_the_row():
    from types import SimpleNamespace

    from app.api.v1.models.admin.in_use import InUseRefusal, in_use

    refused = in_use(
        "Cannot delete server.",
        virtual_servers=[SimpleNamespace(id="vs-1", name="Apps")],
        administrators=[SimpleNamespace(id="a-1", username="ana")],
        nothing=[],
    )
    assert refused.status_code == 409
    body = InUseRefusal.model_validate({"detail": refused.detail})
    assert refused.detail == {
        "message": "Cannot delete server.",
        "used_by": {
            "virtual_servers": [{"id": "vs-1", "name": "Apps"}],
            "administrators": [{"id": "a-1", "name": "ana"}],
        },
    }
    assert body.detail.used_by["virtual_servers"][0].name == "Apps"


def test_no_delete_route_answers_400_for_what_depends_on_the_row():
    """A net for the next one: inside a delete handler, a 400 whose sentence
    speaks of what is assigned, bound or in use."""
    depends = re.compile(r"(assigned|bound to|attached|in use|used by|referenc|still )", re.I)
    found = []
    roots = [ENDPOINTS.parents[4] / "app", ENDPOINTS.parents[4] / "enterprise" / "brutor_enterprise"]
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            lines = path.read_text(encoding="utf-8").split("\n")
            for i, line in enumerate(lines):
                if not re.search(r"status_code=(400|status\.HTTP_400_BAD_REQUEST)|HTTPException\(\s*400", line):
                    continue
                j = i
                while j > 0 and not re.match(r"\s*(async )?def ", lines[j]):
                    j -= 1
                if re.search(r"def (delete|remove)", lines[j]) and depends.search(" ".join(lines[i:i + 8])):
                    found.append(f"{path.name}:{i + 1}")
    assert found == []
