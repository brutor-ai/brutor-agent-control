# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Regression tests for the Datadog-style multi-value search rewrite
on /v1/admin/proxy-logs. Covers:

  * Pydantic plural fields validate as List[str|int].
  * LogQueryRequest.coalesce_filters() folds singular legacy fields
    into the plural list per column — back-compat invariant.
  * Empty / None plural fields drop out of the coalesced dict so the
    endpoint doesn't apply an empty IN() clause (which would force-
    empty the result).
  * compliance_tag (singular) + compliance_tags_any (plural) merge
    into a single `compliance_tags` key — the column the orchestrator
    special-cases with the JSONB `&&` overlap operator.

The actual SQL is covered by the e2e endpoint suite, which needs a
live Postgres for the JSONB operator. These pure-model tests pin the
contract the admin UI's FacetMultiSelect dropdowns depend on without
needing a DB at all.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.api.v1.models.admin.proxy_log import (
    FacetsRequest,
    LogQueryRequest,
    SuggestRequest,
)


class TestPluralFieldsAccepted:
    def test_singular_only_legacy(self):
        r = LogQueryRequest(server_id="srv-1", gateway_user_id="u-1")
        assert r.coalesce_filters() == {
            "server_id": ["srv-1"],
            "gateway_user_id": ["u-1"],
        }

    def test_plural_only_new(self):
        r = LogQueryRequest(server_ids=["srv-1", "srv-2"], gateway_user_ids=["u-1"])
        coalesced = r.coalesce_filters()
        assert coalesced["server_id"] == ["srv-1", "srv-2"]
        assert coalesced["gateway_user_id"] == ["u-1"]

    def test_singular_and_plural_merge(self):
        # When a callable both legacy (single) and new (list) field
        # arrive in the same body — possible during a client upgrade
        # — they merge, dedup, and the operator's explicit ordering
        # is preserved.
        r = LogQueryRequest(
            server_id="srv-1",
            server_ids=["srv-2", "srv-1", "srv-3"],
        )
        assert r.coalesce_filters()["server_id"] == ["srv-1", "srv-2", "srv-3"]

    def test_empty_plural_drops_out(self):
        r = LogQueryRequest(server_ids=[], gateway_user_ids=None)
        assert "server_id" not in r.coalesce_filters()
        assert "gateway_user_id" not in r.coalesce_filters()

    def test_int_plurals_typed(self):
        # http_status_codes is List[int]; Pydantic should reject
        # strings rather than silently coerce — keeps the IN() clause
        # type-safe.
        r = LogQueryRequest(http_status_codes=[200, 404])
        assert r.coalesce_filters()["http_status_code"] == [200, 404]

    def test_int_plural_rejects_strings(self):
        with pytest.raises(ValidationError):
            LogQueryRequest(http_status_codes=["not-an-int"])


class TestComplianceTagsCoalesce:
    def test_singular_compliance_tag(self):
        r = LogQueryRequest(compliance_tag="hipaa.phi")
        assert r.coalesce_filters() == {"compliance_tags": ["hipaa.phi"]}

    def test_plural_compliance_tags_any(self):
        r = LogQueryRequest(compliance_tags_any=["soc2.cc7.2", "eu_ai_act.high"])
        assert r.coalesce_filters() == {
            "compliance_tags": ["soc2.cc7.2", "eu_ai_act.high"],
        }

    def test_singular_and_plural_compliance_merge(self):
        r = LogQueryRequest(
            compliance_tag="hipaa.phi",
            compliance_tags_any=["soc2.cc7.2", "hipaa.phi"],
        )
        # `hipaa.phi` appears in both — coalesce_filters dedupes.
        assert r.coalesce_filters()["compliance_tags"] == [
            "hipaa.phi", "soc2.cc7.2",
        ]


class TestFacetsAndSuggestEnvelopes:
    """The Facets and Suggest request bodies extend LogQueryRequest
    so they CAN be sent with the operator's currently-active filter
    envelope — that's the whole "counts match the table" guarantee.
    These tests pin the new fields without breaking the inheritance."""

    def test_facets_carries_scope_envelope(self):
        r = FacetsRequest(
            server_ids=["srv-1"], hours_ago=4,
            fields=["server_id", "mcp_capability_name"],
            per_field_limit=25,
        )
        assert r.fields == ["server_id", "mcp_capability_name"]
        assert r.per_field_limit == 25
        assert r.coalesce_filters()["server_id"] == ["srv-1"]
        assert r.hours_ago == 4

    def test_facets_per_field_limit_clamped(self):
        # Pydantic ge=1 le=200 — anything outside is a 422 at the
        # endpoint layer.
        with pytest.raises(ValidationError):
            FacetsRequest(per_field_limit=0)
        with pytest.raises(ValidationError):
            FacetsRequest(per_field_limit=500)

    def test_suggest_requires_field_and_prefix(self):
        # Both are mandatory — the endpoint can't run a typeahead
        # against nothing.
        with pytest.raises(ValidationError):
            SuggestRequest(prefix="srv")
        with pytest.raises(ValidationError):
            SuggestRequest(field="server_id")

    def test_suggest_carries_scope_envelope(self):
        r = SuggestRequest(
            field="target_url",
            prefix="https://api.",
            server_ids=["srv-1"],
            success=True,
            suggest_limit=10,
        )
        assert r.field == "target_url"
        assert r.prefix == "https://api."
        assert r.suggest_limit == 10
        assert r.coalesce_filters()["server_id"] == ["srv-1"]
        assert r.success is True
