# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Every model the code names is a model the seeded catalog has.

The catalog (`app/database/provider_data/*_models_init.py`) changes when a
provider retires a model. Three kinds of name can be left behind and go on
naming a model that is no longer there:

* the models seeded enabled on every new tenant (one per modality);
* a default written into the code (the judge-model preference of tenant
  provisioning named `gpt-5-mini`, `gpt-4o-mini` and `claude-haiku-4-5`, none
  of them a catalog id, so it never matched and fell through silently);
* any other model id written as a literal in `app/` or the Enterprise package.

This guard reads the code, not a list: every string literal that is, as a
whole, a model id of a family the catalog knows must be a catalog id.
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from app.database.init_llms import get_default_llm_configurations
from tests.source_trees import APP, REPO, python_sources

PROVIDER_DATA = APP / "database" / "provider_data"

# A whole string that is a model id: a family prefix followed by a version.
# Sentences, provider keys ("gemini", "mistral") and route tags
# ("claude-desktop-export") do not match.
MODEL_ID = re.compile(
    r"^(?:[a-z_]+/)*(?:"
    r"gpt-\d[\w.-]*|gpt-image-[\w.-]+|gpt-transcribe|o[134](?:-mini|-pro)?"
    r"|claude-(?:opus|sonnet|haiku|fable)-[\w.-]+"
    r"|gemini-(?:\d|pro|flash|embedding|nano)[\w.-]*"
    r"|text-embedding-[\w.-]+|whisper-[\w.-]+|tts-1(?:-hd)?|dall-e-\d"
    r"|kimi-k[\w.-]+|deepseek-(?:v\d|chat|reasoner|flash)[\w.-]*|grok-\d[\w.-]*"
    r"|nomic-embed-[\w.-]+|mistral-(?:large|medium|small)-[\w.-]+"
    r"|command-r[\w.-]*|rerank-[\w.-]+|voyage-[\w.-]+"
    r")$"
)

# The nine models a new tenant gets enabled, one per modality and provider
# baseline.
DEFAULT_ENABLED = {
    ("openai", "gpt-5.5"),
    ("openai", "gpt-5.2"),
    ("openai", "text-embedding-3-small"),
    ("openai", "gpt-image-2.6-flare"),
    ("openai", "tts-1"),
    ("openai", "gpt-transcribe"),
    ("openrouter", "openrouter/auto"),
    ("ollama", "nomic-embed-text"),
    ("fal_ai", "luma/agent/ray/v3.2/text-to-video"),
}


def _catalog():
    return get_default_llm_configurations()


def _docstrings(tree: ast.AST) -> set:
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                found.add(id(body[0].value))
    return found


def model_id_literals(paths):
    """(file, line, literal) for every string literal that is a model id."""
    hits = []
    for path in paths:
        if PROVIDER_DATA in Path(path).parents:
            continue
        tree = ast.parse(Path(path).read_text(encoding="utf-8"))
        skip = _docstrings(tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in skip
                and MODEL_ID.match(node.value)
            ):
                hits.append((str(Path(path).relative_to(REPO)), node.lineno, node.value))
    return hits


def test_every_model_id_written_in_the_code_is_in_the_seeded_catalog():
    names = {m["model_name"] for m in _catalog()}
    missing = [
        f"{file}:{line}: {literal!r}"
        for file, line, literal in model_id_literals(python_sources(APP))
        if literal not in names
    ]
    assert not missing, (
        "the code names model ids the seeded catalog does not have "
        "(a default that can never match, or a retired model):\n  "
        + "\n  ".join(missing)
    )


def test_the_scan_finds_the_defaults_the_code_names():
    """The guard is only worth something if it sees the defaults: the
    embedding default and the judge preference of tenant provisioning."""
    found = {
        (file, literal) for file, _line, literal in model_id_literals(python_sources(APP))
    }
    assert ("app/composition/provisioning.py", "text-embedding-3-small") in found
    judge = [
        literal for file, literal in found
        if file == "app/composition/provisioning.py" and literal != "text-embedding-3-small"
    ]
    assert judge, "no judge-model preference found in app/composition/provisioning.py"


def test_the_judge_preference_names_chat_models_of_the_catalog():
    from app.composition.provisioning import DEFAULT_JUDGE_MODEL_CANDIDATES

    chat = {m["model_name"] for m in _catalog() if m.get("mode", "chat") == "chat"}
    assert DEFAULT_JUDGE_MODEL_CANDIDATES, "no judge-model preference"
    for name in DEFAULT_JUDGE_MODEL_CANDIDATES:
        assert name in chat, f"judge preference {name!r} is not a chat model of the catalog"


def test_the_models_enabled_by_default_are_the_nine_of_the_catalog_decision():
    enabled = {(m["provider"], m["model_name"]) for m in _catalog() if m.get("enabled")}
    assert enabled == DEFAULT_ENABLED


def test_a_model_name_is_seeded_once_per_provider():
    seen = set()
    twice = []
    for m in _catalog():
        key = (m["provider"], m["model_name"])
        if key in seen:
            twice.append(key)
        seen.add(key)
    assert not twice, f"seeded twice: {twice}"


def test_the_pattern_tells_a_model_id_from_other_strings():
    for literal in (
        "gpt-6-luna", "gpt-4o-mini", "o3", "o4-mini", "claude-haiku-4-5",
        "claude-opus-5-5", "gemini-3.8-flash", "text-embedding-3-small",
        "whisper-1", "tts-1", "kimi-k3", "azure/gpt-5.2", "gpt-image-2.6-flare",
    ):
        assert MODEL_ID.match(literal), literal
    for literal in (
        "gemini", "mistral", "claude-desktop-export", "gpt", "openai",
        "Search by model config name like 'gpt-4o'", "o3 is retired",
    ):
        assert not MODEL_ID.match(literal), literal


def test_the_models_the_live_suite_drives_are_in_the_seeded_catalog():
    """`tests/live/conftest.py` names the models the live suite calls
    (`E2E_MODELS`, per provider, and the four `E2E_*_MODEL` defaults). The
    live suite checks them against a running control plane; this holds them
    to the seed without one, so a catalog change cannot leave them behind."""
    tree = ast.parse((REPO / "tests" / "live" / "conftest.py").read_text(encoding="utf-8"))
    assigned = {}
    for node in tree.body:
        target = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target = node.target.id
        elif isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            target = node.targets[0].id
        if target and target.startswith("E2E_") and node.value is not None:
            try:
                assigned[target] = ast.literal_eval(node.value)
            except ValueError:
                pass
    assert "E2E_MODELS" in assigned, "tests/live/conftest.py no longer assigns E2E_MODELS"
    catalog = {(m["provider"], m["model_name"]) for m in _catalog()}
    names = {name for _provider, name in catalog}
    missing = [
        f"{provider}/{name}"
        for provider, models in assigned["E2E_MODELS"].items()
        for name in models
        if (provider, name) not in catalog
    ]
    defaults = {k: v for k, v in assigned.items() if k.endswith("_MODEL") and isinstance(v, str)}
    assert len(defaults) >= 4, defaults
    missing += [f"{k}={v}" for k, v in defaults.items() if v not in names]
    assert not missing, f"the live suite names models the seed does not have: {missing}"
