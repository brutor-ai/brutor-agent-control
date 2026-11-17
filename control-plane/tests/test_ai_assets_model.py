# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# AiAsset model unit tests

Covers to_dict completeness (every column appears) and the @validates
rejection of out-of-vocabulary values.
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import pytest

from app.database.models.ai_asset import AiAsset


def _make(**overrides) -> AiAsset:
    base = dict(
        id="aiasset-1",
        tenant_id="t1",
        asset_kind="llm_model",
        ref_id="model-1",
        display_name="GPT-4o",
        source="manual",
    )
    base.update(overrides)
    return AiAsset(**base)


class TestToDict:
    def test_every_column_present(self):
        asset = _make(owner="alice", risk_tier="high", intended_use="chat",
                      tags=["pii", "prod"])
        d = asset.to_dict()
        expected = {
            "id", "tenant_id", "asset_kind", "ref_id", "display_name",
            "icon_url", "source_enabled", "observed",
            "owner", "lifecycle_stage", "risk_tier", "intended_use",
            "gpai_classification", "operated_by",
            "enforcement_mode", "tags", "status", "source",
            "created_at", "updated_at",
        }
        # Compare against the actual ORM columns so a future column add
        # that forgets to_dict is caught.
        model_cols = {c.name for c in AiAsset.__table__.columns}
        assert expected == model_cols
        assert set(d.keys()) == expected

    def test_tags_normalises_none_to_empty_list(self):
        asset = _make(tags=None)
        assert asset.to_dict()["tags"] == []

    def test_tags_round_trips_list(self):
        asset = _make(tags=["a", "b"])
        assert asset.to_dict()["tags"] == ["a", "b"]


class TestValidators:
    @pytest.mark.parametrize("bad", ["bogus", "model", ""])
    def test_bad_asset_kind_rejected(self, bad):
        with pytest.raises(ValueError):
            _make(asset_kind=bad)

    @pytest.mark.parametrize("bad", ["archived", "live", ""])
    def test_bad_lifecycle_rejected(self, bad):
        with pytest.raises(ValueError):
            _make(lifecycle_stage=bad)

    @pytest.mark.parametrize("bad", ["critical", "none", ""])
    def test_bad_risk_tier_rejected(self, bad):
        with pytest.raises(ValueError):
            _make(risk_tier=bad)

    def test_null_risk_tier_allowed(self):
        asset = _make(risk_tier=None)
        assert asset.risk_tier is None

    @pytest.mark.parametrize("bad", ["block", "audit", ""])
    def test_bad_enforcement_mode_rejected(self, bad):
        with pytest.raises(ValueError):
            _make(enforcement_mode=bad)

    @pytest.mark.parametrize("bad", ["pending", "orphan", ""])
    def test_bad_status_rejected(self, bad):
        with pytest.raises(ValueError):
            _make(status=bad)

    @pytest.mark.parametrize("bad", ["imported", "auto", ""])
    def test_bad_source_rejected(self, bad):
        with pytest.raises(ValueError):
            _make(source=bad)

    def test_good_values_accepted(self):
        asset = _make(
            asset_kind="agent_card",
            lifecycle_stage="deprecated",
            risk_tier="limited",
            enforcement_mode="enforce",
            status="flagged",
            source="discovered",
        )
        assert asset.asset_kind == "agent_card"
        assert asset.lifecycle_stage == "deprecated"
        assert asset.risk_tier == "limited"
        assert asset.enforcement_mode == "enforce"
        assert asset.status == "flagged"
        assert asset.source == "discovered"
