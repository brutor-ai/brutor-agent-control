# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# a068_secrets_detection — secrets-detection migration regression suite

The secrets detection check is implemented across three places
that have to agree on the default category set + surface list:

  1. Alembic migration  (alembic/versions/...a068_secrets_detection.py)
  2. SQLAlchemy model   (app/database/models/guardrail_config.py)
  3. Rust detector      (brutor-gateway-core/src/services/guardrails/secrets.rs
                          — `SecretsDetector::default_categories()`)

A drift would mean "first-write through the model" gets a
different category subset than "row migrated by a068_secrets_detection and read
later". This suite locks the three in lockstep — Python model
constants come from the running code, Rust constants come from
parsing the source file (the Rust source is the authoritative
list for the runtime).
"""

from __future__ import annotations

import re

from tests.core_repo import core_source

import pytest

from app.database.models.guardrail_config import (
    ALL_SECRETS_CATEGORIES,
    DEFAULT_SECRETS_CATEGORIES,
)


@pytest.fixture(scope="module")
def rust_text() -> str:
    return core_source("services", "guardrails", "secrets.rs").read_text()


# ---------------------------------------------------------------------------
# Schema shape
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Default category set — Python model ↔ migration backfill
# ---------------------------------------------------------------------------


class TestDefaultsAgreeAcrossModelMigrationAndRust:

    def test_default_excludes_generic_high_entropy(self):
        # Pin the RFC's deliberate carve-out: generic_high_entropy
        # has the highest FP rate so it's opt-in only. If a future
        # commit adds it to the default set we want a test failure
        # explaining the choice.
        assert "generic_high_entropy" not in DEFAULT_SECRETS_CATEGORIES, (
            "generic_high_entropy is opt-in (it flags too much ordinary text to "
            "be on by default) — adding it to the defaults needs a deliberate decision"
        )

    def test_all_categories_superset_of_default(self):
        # ALL_SECRETS_CATEGORIES is the validator allow-list; it
        # must include every default. (Reverse subset isn't true —
        # generic_high_entropy is in ALL but not DEFAULT.)
        for cat in DEFAULT_SECRETS_CATEGORIES:
            assert cat in ALL_SECRETS_CATEGORIES, (
                f"default category {cat} missing from ALL_SECRETS_CATEGORIES"
            )

    def test_rust_default_categories_match_python(self, rust_text: str):
        # Parse `pub fn default_categories() -> &'static [&'static str] { &[ ... ] }`
        # body and compare to the Python list. The Rust source is
        # the runtime authority — Python must reflect it.
        m = re.search(
            r"pub fn default_categories\(\) -> &'static \[&'static str\] \{\s*&\[\s*((?:\"[^\"]*\",\s*)+)\]",
            rust_text,
            re.MULTILINE | re.DOTALL,
        )
        assert m, "could not parse default_categories() body in Rust"
        rust_defaults = re.findall(r'"([^"]*)"', m.group(1))
        assert sorted(rust_defaults) == sorted(DEFAULT_SECRETS_CATEGORIES), (
            f"Rust default categories ({sorted(rust_defaults)}) drift from "
            f"Python DEFAULT_SECRETS_CATEGORIES ({sorted(DEFAULT_SECRETS_CATEGORIES)})"
        )

    def test_rust_known_categories_match_python_all(self, rust_text: str):
        # Same drift check for the full validator list.
        m = re.search(
            r"pub fn known_categories\(\) -> &'static \[&'static str\] \{\s*&\[\s*((?:\"[^\"]*\",\s*)+)\]",
            rust_text,
            re.MULTILINE | re.DOTALL,
        )
        assert m, "could not parse known_categories() body in Rust"
        rust_known = re.findall(r'"([^"]*)"', m.group(1))
        assert sorted(rust_known) == sorted(ALL_SECRETS_CATEGORIES), (
            f"Rust known categories ({sorted(rust_known)}) drift from "
            f"Python ALL_SECRETS_CATEGORIES ({sorted(ALL_SECRETS_CATEGORIES)})"
        )
