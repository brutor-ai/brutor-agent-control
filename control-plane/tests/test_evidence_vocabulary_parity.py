# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
WIRE CONTRACT pin — the action-record verdict vocabulary.

The Rust core writes `action_records.verdict` from
`brutor_evidence::record::verdict::ALL` and the control plane reads/filters it
through `RECORD_VERDICTS`. There is no compile-time link between the repos, so
both sides pin the SAME literals: the Rust half is
`crates/brutor-evidence/src/record.rs::tests::verdict_vocabulary_is_pinned`.
Changing a verdict means editing both pins and the contracts doc together.
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "x")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from app.database.models.action_record import (  # noqa: E402
    NON_DISPATCHING_VERDICTS,
    RECORD_KINDS,
    RECORD_VERDICTS,
)

PINNED_VERDICTS = [
    "adjudication",
    "assessment",
    "awaiting_human",
    "blocked",
    "denied",
    "engine_failure",
    "epoch_boundary",
    "errored",
    "executed",
    "expired",
    "planned",
    "recorded",
    "resolved",
    "timeout",
]


def test_verdict_vocabulary_matches_the_rust_pin():
    assert sorted(RECORD_VERDICTS) == PINNED_VERDICTS


def test_only_executed_errored_timeout_dispatch():
    assert sorted(RECORD_VERDICTS - NON_DISPATCHING_VERDICTS) == [
        "errored",
        "executed",
        "timeout",
    ]
    assert NON_DISPATCHING_VERDICTS <= RECORD_VERDICTS


def test_planned_is_a_non_dispatching_verdict():
    # A `planned` record attests the decision, never an effect (its effect is
    # `status: planned`, derived mode `not_applicable`).
    assert "planned" in NON_DISPATCHING_VERDICTS


def test_kinds_pinned():
    assert sorted(RECORD_KINDS) == ["decide", "fyi"]
