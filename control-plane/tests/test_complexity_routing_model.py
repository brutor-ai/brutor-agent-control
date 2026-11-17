# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Tests for the complexity-based routing data model (a140_complexity_routing):

  1. `complexity-based-routing` is an accepted `routing_strategy`, and the
     existing strategies still validate — the feature is additive.

  2. `complexity_floor_tier` accepts only the four tier strings. This is the
     wire contract shared with the Rust scorer
     (`services/complexity.rs::ComplexityTier::as_str`); a typo would silently
     disable the one control that stops a bad score reaching a weak model.

  3. `LLMRoutingGroupTier` validates its `tier` column against the same
     vocabulary.

  4. The new columns round-trip through `to_dict()` — Pydantic response models
     strip undeclared fields, so a field missing from `to_dict()` reads back as
     its default no matter what was stored.

Run: pytest tests/test_complexity_routing_model.py -v
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "x")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import pytest

from app.database.models.llm_routing_group import (
    COMPLEXITY_TIERS,
    LLMRoutingGroup,
    LLMRoutingGroupTier,
)


# ---------------------------------------------------------------------------
# 1. routing_strategy
# ---------------------------------------------------------------------------


def test_complexity_strategy_is_accepted():
    g = LLMRoutingGroup()
    g.routing_strategy = "complexity-based-routing"
    assert g.routing_strategy == "complexity-based-routing"


@pytest.mark.parametrize(
    "strategy",
    [
        "simple-shuffle",
        "least-busy",
        "usage-based-routing",
        "usage-based-routing-v2",
        "latency-based-routing",
        "cost-based-routing",
    ],
)
def test_existing_strategies_still_validate(strategy):
    """The new strategy is additive — nothing that worked before may break."""
    g = LLMRoutingGroup()
    g.routing_strategy = strategy
    assert g.routing_strategy == strategy


def test_unknown_strategy_still_rejected():
    g = LLMRoutingGroup()
    with pytest.raises(ValueError, match="routing_strategy must be one of"):
        g.routing_strategy = "vibes-based-routing"


# ---------------------------------------------------------------------------
# 2. complexity_floor_tier — the governance control
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tier", COMPLEXITY_TIERS)
def test_floor_tier_accepts_every_valid_tier(tier):
    g = LLMRoutingGroup()
    g.complexity_floor_tier = tier
    assert g.complexity_floor_tier == tier


@pytest.mark.parametrize("empty", [None, ""])
def test_floor_tier_empty_means_no_floor(empty):
    g = LLMRoutingGroup()
    g.complexity_floor_tier = empty
    assert g.complexity_floor_tier is None


@pytest.mark.parametrize("bad", ["bogus", "SIMPLE", "Simple", "cheap", "0"])
def test_floor_tier_rejects_anything_else(bad):
    """Case-sensitive on purpose: the stored value is compared verbatim
    against the Rust scorer's tier strings."""
    g = LLMRoutingGroup()
    with pytest.raises(ValueError, match="complexity_floor_tier must be one of"):
        g.complexity_floor_tier = bad


def test_tier_vocabulary_is_pinned():
    """WIRE CONTRACT with `ComplexityTier::as_str` in the Rust core.

    These strings are stored in `llm_routing_group_tier.tier`. Changing one
    silently repoints an operator's traffic, so it must be a deliberate,
    test-breaking act.
    """
    assert COMPLEXITY_TIERS == ("simple", "medium", "complex", "reasoning")


# ---------------------------------------------------------------------------
# 3. the tier mapping row
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tier", COMPLEXITY_TIERS)
def test_tier_row_accepts_valid_tiers(tier):
    row = LLMRoutingGroupTier()
    row.tier = tier
    assert row.tier == tier


def test_tier_row_rejects_invalid_tier():
    row = LLMRoutingGroupTier()
    with pytest.raises(ValueError, match="tier must be one of"):
        row.tier = "expensive"


def test_tier_row_to_dict_round_trips():
    row = LLMRoutingGroupTier()
    row.id = "t-1"
    row.routing_group_id = "rg-1"
    row.tier = "complex"
    row.llm_model_id = "m-1"
    row.position = 2

    assert row.to_dict() == {
        "id": "t-1",
        "routing_group_id": "rg-1",
        "tier": "complex",
        "llm_model_id": "m-1",
        "position": 2,
    }


# ---------------------------------------------------------------------------
# 4. to_dict surfaces the new fields
# ---------------------------------------------------------------------------


def test_to_dict_includes_complexity_fields():
    """Pydantic response models strip undeclared fields; a column missing from
    `to_dict()` reads back as its default whatever was stored. This is the
    recurring bug class in this codebase, so it gets a test."""
    g = LLMRoutingGroup()
    g.id = "rg-1"
    g.name = "tiered"
    g.routing_strategy = "complexity-based-routing"
    g.complexity_floor_tier = "medium"
    g.complexity_config = {"boundaries": {"simple_max": 0.2}}

    d = g.to_dict()
    assert d["complexity_floor_tier"] == "medium"
    assert d["complexity_config"] == {"boundaries": {"simple_max": 0.2}}


def test_to_dict_complexity_fields_default_to_none():
    """An untouched group must report no floor and no overrides — this is what
    makes the feature inert for every existing group."""
    g = LLMRoutingGroup()
    g.id = "rg-2"
    g.name = "ordinary"

    d = g.to_dict()
    assert d["complexity_floor_tier"] is None
    assert d["complexity_config"] is None
