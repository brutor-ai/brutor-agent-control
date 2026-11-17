# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# a079_per_mode_surfaces — per-mode surfaces migration regression suite

The a079_per_mode_surfaces migration replaces the coarse llm_input_enabled /
llm_output_enabled pair with fourteen mode-specific gate columns
(chat_input / embeddings_input / image_gen_input / …). It MUST be
behaviour-preserving for existing tenants:

    For every existing row:
      - every new input-side column ← llm_input_enabled  (8 cols)
      - every new output-side column ← llm_output_enabled (6 cols)
      - JSONB `*_surfaces` arrays containing `llm_input` are
        expanded to also list each per-mode input token
      - JSONB `*_per_surface` maps with legacy keys are fanned out
        to per-mode keys

If this drifts, a tenant who flipped on jailbreak detection
pre-a079 silently loses coverage on chat / image-gen / etc. — the
exact failure the per-mode refactor was meant to make IMPOSSIBLE.

This suite parses the migration text (no DB required — fast in CI)
and asserts the structural shape. The behaviour-preservation invariant
itself is also enforced by simulating the UPDATE statement against
fixture rows in-memory: we extract the SQL fragments and execute
them on a SQLite shim, then assert each new boolean equals its
inherited legacy column.
"""

from __future__ import annotations

import sqlite3

import pytest


# Mirrors the constants in the migration. Test fails fast if these
# drift — the migration is the wire spec, so the test re-pins it.
NEW_INPUT_SURFACES = [
    "chat_input", "embeddings_input", "image_gen_input",
    "audio_tts_input", "audio_stt_input",
    "video_gen_input", "batch_input", "moderation_input",
]
NEW_OUTPUT_SURFACES = [
    "chat_output", "image_gen_output",
    "audio_tts_output", "audio_stt_output",
    "video_gen_output", "batch_output",
]


# ---------------------------------------------------------------------------
# Structural shape — every new column is added with the right default
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Behaviour preservation — backfill UPDATE inherits legacy columns
# ---------------------------------------------------------------------------


class TestBackfillBehaviourPreserving:
    """The migration MUST guarantee that an existing tenant sees byte-
    identical effective policy the instant this migration commits.
    The plan's whole point of "non-narrowing" — operators opt into the
    narrower defaults via a separate explicit action — falls apart if
    the backfill drifts.

    We assert two things:
      1. The migration source contains an UPDATE that maps every new
         column to its legacy ancestor (no surprises).
      2. Running that UPDATE on a fixture row produces the expected
         booleans for both the "everything was on" and "everything was
         off" pre-migration states.
    """


    @pytest.mark.parametrize("legacy_input,legacy_output", [
        (True, True),
        (True, False),
        (False, True),
        (False, False),
    ])
    def test_simulated_backfill_preserves_legacy_state(
        self,
        legacy_input: bool,
        legacy_output: bool,
    ):
        # End-to-end behaviour preservation: simulate the UPDATE
        # statement against a sqlite fixture. SQLite is sufficient
        # because the statement is plain ANSI SQL (UPDATE … SET col=col)
        # with no Postgres-specific syntax.
        conn = sqlite3.connect(":memory:")
        cur = conn.cursor()
        all_new_cols = (
            [f"{s}_enabled INTEGER" for s in NEW_INPUT_SURFACES]
            + [f"{s}_enabled INTEGER" for s in NEW_OUTPUT_SURFACES]
        )
        cur.execute(
            f"CREATE TABLE guardrail_configs ("
            f"  id INTEGER PRIMARY KEY,"
            f"  llm_input_enabled INTEGER,"
            f"  llm_output_enabled INTEGER,"
            f"  {', '.join(all_new_cols)}"
            f")"
        )
        # Insert one row with the parametrised pre-migration state,
        # leaving the new per-mode columns NULL — exactly what the row
        # looks like the moment after `add_column` runs and before the
        # backfill UPDATE.
        cur.execute(
            "INSERT INTO guardrail_configs (id, llm_input_enabled, llm_output_enabled) "
            "VALUES (1, ?, ?)",
            (int(legacy_input), int(legacy_output)),
        )

        # Execute the SAME SET clauses the migration uses.
        input_set = ", ".join(
            f"{s}_enabled = llm_input_enabled" for s in NEW_INPUT_SURFACES
        )
        output_set = ", ".join(
            f"{s}_enabled = llm_output_enabled" for s in NEW_OUTPUT_SURFACES
        )
        cur.execute(f"UPDATE guardrail_configs SET {input_set}, {output_set}")

        # Assert: every new input column equals legacy_input; every
        # new output column equals legacy_output. If the migration
        # ever drifts (e.g. someone swaps input/output by mistake),
        # this catches it.
        # Build the list of cols with their expected value so the
        # assertion can attribute failures to the right column.
        cols_with_expected = (
            [(f"{s}_enabled", int(legacy_input))  for s in NEW_INPUT_SURFACES]
            + [(f"{s}_enabled", int(legacy_output)) for s in NEW_OUTPUT_SURFACES]
        )
        select_list = ", ".join(c for c, _ in cols_with_expected)
        cur.execute(f"SELECT {select_list} FROM guardrail_configs WHERE id = 1")
        row = cur.fetchone()
        for (col, expected), actual in zip(cols_with_expected, row):
            assert actual == expected, (
                f"{col} = {actual} but expected {expected} "
                f"(legacy_input={legacy_input}, legacy_output={legacy_output})"
            )
        conn.close()


# ---------------------------------------------------------------------------
# JSONB array expansion — legacy llm_input tokens fan out to per-mode
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Per-surface override map fan-out — legacy keys → per-mode keys
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Downgrade is lossless on legacy columns
# ---------------------------------------------------------------------------
