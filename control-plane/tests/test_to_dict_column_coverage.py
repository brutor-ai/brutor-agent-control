# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
`AISystemRun.to_dict()` must not fall behind its own table.

CP-21 — the model grew six columns over two revisions (subject-on-run, the
client-reported outcome, RAG collection ids, prompt fingerprint, the
user-agent hash) and none of them reached `to_dict()`. Nothing failed: the
serialiser returned a dict, the endpoint returned 200, and the fields were
simply absent. `RunSummary` already declared five of the six, so a response
built from `to_dict()` dropped them between the model and the wire.

This is the rule in CLAUDE.md — *"Pydantic response models strip undeclared
fields — always add new fields to both `to_dict()` and the response model"* —
seen from the other end, and it earns a test because the failure is invisible
at every layer.

Scoped to this one model on purpose. A first draft asserted the invariant
across all 111 models and reported 55 failures, most of which were an
artefact of the check itself (half the codebase writes `'id':` and the regex
only matched `"id":`). Several of the genuine ones are deliberate — a model
whose dict form is a summary, or which holds an encrypted secret it must
never serialise. A guard that is mostly wrong gets switched off, so this
covers the model where completeness IS the contract: the run row is the
assurance record, `RunSummary` mirrors it field for field, and a column
missing here is evidence that silently does not reach an auditor.
"""

import re
from pathlib import Path

MODEL = (
    Path(__file__).resolve().parent.parent
    / "app" / "database" / "models" / "ai_system_run.py"
)

# Columns deliberately kept out of the dict form, with the reason. An entry
# here is a claim that callers must never see the field — not a way to quiet
# the test.
EXCLUDED: set[str] = set()

COLUMN_RE = re.compile(r"^\s{4}(\w+)\s*=\s*Column\(", re.M)
# Quote-agnostic: this codebase uses both spellings.
KEY_RE = re.compile(r"""["'](\w+)["']\s*:""")


def _run_model_block() -> str:
    src = MODEL.read_text()
    start = src.index("class AISystemRun(Base):")
    nxt = src.find("\nclass ", start + 10)
    return src[start:nxt if nxt > 0 else len(src)]


def test_ai_system_run_to_dict_covers_every_column():
    block = _run_model_block()
    columns = COLUMN_RE.findall(block)
    keys = set(KEY_RE.findall(block[block.index("def to_dict"):]))

    assert columns, "no columns parsed — the model's shape changed, fix this test"

    missing = [c for c in columns if c not in keys and c not in EXCLUDED]
    assert not missing, (
        f"AISystemRun.to_dict() omits {missing}. A run column added without its "
        f"serialiser entry disappears between the database and the wire with "
        f"nothing failing — no error, no warning, a 200 with the field absent. "
        f"Add it to to_dict() and to RunSummary, or record it in EXCLUDED with "
        f"the reason it must not be exposed."
    )


def test_run_summary_declares_what_to_dict_emits():
    """The two halves of the CLAUDE.md rule have to agree.

    `RunSummary` is built from `to_dict()`, and Pydantic drops undeclared
    keys — so a field present in one and absent from the other is lost either
    way round. This checks the direction that loses data silently: emitted by
    the model, not declared by the response.
    """
    block = _run_model_block()
    emitted = set(KEY_RE.findall(block[block.index("def to_dict"):]))

    models_src = (
        Path(__file__).resolve().parent.parent
        / "app" / "api" / "v1" / "models" / "admin" / "ai_system_run.py"
    ).read_text()
    start = models_src.index("class RunSummary(BaseModel):")
    nxt = models_src.find("\nclass ", start + 10)
    summary = models_src[start:nxt if nxt > 0 else len(models_src)]
    declared = set(re.findall(r"^\s{4}(\w+)\s*:", summary, re.M))

    # RunSummary is a summary: it may legitimately omit bulky or internal
    # fields. What it must not do is omit one it is meant to carry, so this
    # asserts the known set rather than equality.
    must_carry = {
        "subject_id",
        "subject_kind",
        # The declared outcome and why a run failed.
        "outcome_id",
        "outcome_conformance",
        "outcome_counts_toward_baseline",
        "run_class",
        "error_cause",
        "rag_collection_ids",
        "prompt_fingerprint",
    }
    missing = sorted(f for f in must_carry if f in emitted and f not in declared)
    assert not missing, (
        f"RunSummary does not declare {missing}, which AISystemRun.to_dict() "
        f"emits. Pydantic strips undeclared keys, so these are dropped on the "
        f"way out and the endpoint reports success without them."
    )
