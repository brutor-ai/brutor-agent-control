# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Compliance tagging — Python ↔ Rust tagging-engine drift parity.

The audit-row tagger runs in both languages:

  * Python copy at `app/services/compliance/tagging.py` — used by
    the policy-engine YAML import/export, backfill scripts, and
    these unit tests.
  * Rust copy at `brutor-gateway-core/src/services/compliance.rs`
    — used by the live audit write path
    (`services::background_writer`).

A drift means a tenant audited by Python tooling sees different
tags than the live system writes — operators lose trust in the
dashboard. This suite reads both source files and asserts that
every tag constant + framework key + dispatch rule matches.

If you intentionally diverge (e.g. rename `soc2.cc6.7` because
AICPA renumbered the control), update BOTH files in the same
commit; this test will then pass against the new pair.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from tests.core_repo import core_source

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from app.services.compliance import tagging as py_tagging


@pytest.fixture(scope="module")
def rust_text() -> str:
    return core_source("services", "compliance.rs").read_text()


def _extract_rust_const_str(text: str, name: str) -> str:
    """Pull the literal value of a `pub const NAME: &str = "value";`
    declaration. Returns the raw string or raises if not found.
    The drift test uses this to compare Python module-level
    constants against the matching Rust ones."""
    m = re.search(
        rf'pub const {re.escape(name)}:\s*&str\s*=\s*"([^"]+)";',
        text,
    )
    if not m:
        raise AssertionError(f"Rust const {name} not found")
    return m.group(1)


# ---------------------------------------------------------------------------
# Tag constants
# ---------------------------------------------------------------------------


class TestTagConstantsMatchAcrossLanguages:
    @pytest.mark.parametrize(
        "py_name,rust_name",
        [
            ("TAG_SOC2_AUTH_EVENT", "TAG_SOC2_AUTH_EVENT"),
            ("TAG_SOC2_GUARDRAIL_BLOCK", "TAG_SOC2_GUARDRAIL_BLOCK"),
            ("TAG_SOC2_POLICY_DENY", "TAG_SOC2_POLICY_DENY"),
            ("TAG_SOC2_RESIDENCY_BLOCK", "TAG_SOC2_RESIDENCY_BLOCK"),
            ("TAG_SOC2_RATE_LIMIT_HIT", "TAG_SOC2_RATE_LIMIT_HIT"),
            ("TAG_GDPR_ART_30", "TAG_GDPR_ART_30"),
            ("TAG_HIPAA_PHI", "TAG_HIPAA_PHI"),
            ("TAG_HIPAA_RG_MARKED", "TAG_HIPAA_RG_MARKED"),
            ("TAG_ISO_42001", "TAG_ISO_42001"),
        ],
    )
    def test_tag_string_matches(self, rust_text: str, py_name: str, rust_name: str):
        """Each named tag must carry the same literal in both
        languages. A drift means audit rows written by Rust would
        not match queries written against the Python constants."""
        py_val = getattr(py_tagging, py_name)
        rust_val = _extract_rust_const_str(rust_text, rust_name)
        assert py_val == rust_val, (
            f"{py_name}: Python={py_val!r} vs Rust={rust_val!r}. "
            f"Update both source files in lockstep."
        )


# ---------------------------------------------------------------------------
# Framework key set
# ---------------------------------------------------------------------------


class TestFrameworkKeySetMatchesAcrossLanguages:
    @pytest.mark.parametrize(
        "py_key,rust_const",
        [
            ("gdpr_art_30", "FRAMEWORK_GDPR_ART_30"),
            ("soc2", "FRAMEWORK_SOC2"),
            ("hipaa", "FRAMEWORK_HIPAA"),
            ("eu_ai_act", "FRAMEWORK_EU_AI_ACT"),
            ("iso_42001", "FRAMEWORK_ISO_42001"),
        ],
    )
    def test_framework_constant_matches(self, rust_text: str, py_key: str, rust_const: str):
        # Pydantic FRAMEWORK_KEYS is the authoritative Python set;
        # confirm each entry has a matching Rust const whose
        # value is the same string.
        from app.api.v1.models.admin.compliance import FRAMEWORK_KEYS
        assert py_key in FRAMEWORK_KEYS, (
            f"FRAMEWORK_KEYS missing {py_key!r}"
        )
        rust_val = _extract_rust_const_str(rust_text, rust_const)
        assert py_key == rust_val, (
            f"Framework drift: Pydantic uses {py_key!r}, Rust "
            f"{rust_const} = {rust_val!r}"
        )


# ---------------------------------------------------------------------------
# Engine "Some(None) vs Some([])" semantics
# ---------------------------------------------------------------------------


class TestEngineSemanticsMatchAcrossLanguages:
    def test_python_no_frameworks_returns_none(self):
        # Python and Rust both treat "no profile" as the
        # engine-never-ran sentinel. The Rust assertion is in
        # `services::compliance::tests::no_frameworks_returns_none`;
        # this test pins the matching Python behaviour so the
        # two stay aligned.
        ctx = py_tagging.TaggingContext(frameworks=set())
        assert py_tagging.derive_tags(ctx) is None

    def test_python_declared_quiet_call_returns_empty_list(self):
        # Mirror of Rust's `declared_profile_but_quiet_call_returns_empty_vec`.
        ctx = py_tagging.TaggingContext(frameworks={"soc2"})
        result = py_tagging.derive_tags(ctx)
        assert result == [], (
            f"Quiet call with declared profile should be [], got {result!r}"
        )

    def test_iso_42001_always_fires_in_both_languages(self):
        # Both engines emit `iso_42001` on every call when the
        # framework is declared. Drift here would mean the audit
        # dashboard underreports activity for tenants under ISO.
        ctx = py_tagging.TaggingContext(frameworks={"iso_42001"})
        tags = py_tagging.derive_tags(ctx)
        assert tags == [py_tagging.TAG_ISO_42001]


# ---------------------------------------------------------------------------
# Rust source contains the same dispatch order
# ---------------------------------------------------------------------------


class TestOrchestratorFlagsMatchAcrossLanguages:
    """Verify the orchestrator outcome surface (the audit-row
    boolean columns) matches the Python TaggingContext field names. A
    rename in either layer would silently break the live
    tagger."""

    def test_rust_log_state_reads_orchestrator_flags(self, rust_text: str):
        # The Rust `derive_tags_from_log_state` reads these four
        # log fields. Confirm each one is referenced.
        for field in (
            "log.guardrail_blocked",
            "log.argument_policy_denied",
            "log.pii_detected",
            "log.is_auth_event",
        ):
            assert field in rust_text, (
                f"derive_tags_from_log_state missing read of {field}. "
                f"Phase B wiring requires reading all four "
                f"orchestrator flag columns from the log."
            )

    def test_python_tagging_context_carries_orchestrator_fields(self):
        # Phase A already added these to the Python context for
        # symmetry. Pin them so a drop on the Python side fails
        # this assertion rather than just producing wrong tags.
        from app.services.compliance.tagging import TaggingContext
        fields = {f.name for f in TaggingContext.__dataclass_fields__.values()}
        for required in (
            "guardrail_blocked",
            "argument_policy_denied",
            "has_pii",
            "has_phi",
            "is_auth_event",
        ):
            assert required in fields, (
                f"TaggingContext missing {required}; Python<->Rust drift"
            )


class TestDispatchOrderMatches:
    def test_rust_dispatch_order_matches_python(self, rust_text: str):
        """Both engines call the framework derivers in this order:
        gdpr → eu_ai_act → soc2 → hipaa → iso_42001.

        Order matters because the tags-array order is observable
        (the audit-row JSONB preserves it, integration tests
        sometimes compare arrays directly). A reordering in one
        language would produce subtle test failures in the other.
        """
        # Python dispatch order is the tuple
        # `_FRAMEWORK_DERIVERS`.
        py_order = [
            f.__name__ for f in py_tagging._FRAMEWORK_DERIVERS
        ]
        assert py_order == [
            "_tag_gdpr", "_tag_eu_ai_act", "_tag_soc2",
            "_tag_hipaa", "_tag_iso_42001",
        ]

        # Rust dispatch is inlined in `derive_tags`; scrape the
        # sequence of `tag_<framework>(...)` calls in body order.
        m = re.search(
            r"pub fn derive_tags\([^)]*\)\s*->\s*Option<Vec<String>>\s*\{(.+?)\n\}",
            rust_text,
            re.DOTALL,
        )
        assert m, "could not locate Rust derive_tags body"
        body = m.group(1)
        calls = re.findall(r"tag_(\w+)\(ctx", body)
        assert calls == ["gdpr", "eu_ai_act", "soc2", "hipaa", "iso_42001"], (
            f"Rust dispatch order drift: {calls}. Update Python "
            f"_FRAMEWORK_DERIVERS in lockstep."
        )


class TestRegistryUnification:
    """The tagging engines read their framework ids from the
    framework registry: one drift test instead of two constant lists."""

    def test_rust_framework_constants_equal_the_registry_tag_keys(self, rust_text: str):
        from app.database.models.compliance_framework import registry_tag_keys

        rust = set(re.findall(r'pub const FRAMEWORK_\w+:\s*&str\s*=\s*"([^"]+)";', rust_text))
        assert rust == set(registry_tag_keys().values())

    def test_python_engine_keys_come_from_the_registry(self):
        from app.api.v1.models.admin.compliance import FRAMEWORK_KEYS
        from app.database.models.compliance_framework import registry_tag_keys

        tags = registry_tag_keys()
        assert py_tagging.FRAMEWORK_GDPR == tags["gdpr"]
        assert py_tagging.FRAMEWORK_EU_AI_ACT == tags["eu-ai-act"]
        assert py_tagging.FRAMEWORK_SOC2 == tags["soc2"]
        assert py_tagging.FRAMEWORK_HIPAA == tags["hipaa"]
        assert py_tagging.FRAMEWORK_ISO_42001 == tags["iso-42001"]
        assert FRAMEWORK_KEYS == set(tags.values())
