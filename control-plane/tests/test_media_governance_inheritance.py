# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Media governance inheritance in the control plane.

Media was the one surface where the control plane computed no inheritance
at all — the response models carried a comment saying so — while the Rust
proxy has always merged `media_global_governance` and `media_global_limits`
across the ancestor chain. An operator therefore saw no inherited media
policy on a child group while the runtime enforced one.

The merge here mirrors `media_field_rule` in the proxy field for field, and
`test_python_rust_merge_parity.py` fails if the two tables drift. These
tests pin the semantics themselves: which direction each kind of field
tightens in, and the two easy mistakes — treating an empty whitelist as
"nothing allowed" rather than "unconstrained", and treating `speed_min`
like every other numeric bound.
"""

from __future__ import annotations

import pytest

from app.api.v1.endpoints.admin.resource_groups import (
    _validate_media_governance_not_less_restrictive as relaxations,
)
from app.database.models.resource_group import (
    MEDIA_GOVERNANCE_FIELD_RULES,
    _media_governance_restrictive_merge,
)


def merge(child: dict, parent: dict) -> dict:
    """Merge a parent into a child-rooted accumulator, as the walk does."""
    import copy

    base = copy.deepcopy(child)
    _media_governance_restrictive_merge(base, parent)
    return base


class TestCeilings:
    def test_the_lower_ceiling_wins_whichever_level_set_it(self):
        assert merge(
            {"audio_speech": {"max_input_characters": 5000}},
            {"audio_speech": {"max_input_characters": 500}},
        ) == {"audio_speech": {"max_input_characters": 500}}
        assert merge(
            {"audio_speech": {"max_input_characters": 100}},
            {"audio_speech": {"max_input_characters": 500}},
        ) == {"audio_speech": {"max_input_characters": 100}}

    def test_a_field_only_the_parent_sets_is_inherited(self):
        assert merge({"audio_speech": {}}, {"audio_speech": {"max_n": 3}}) == {
            "audio_speech": {"max_n": 3}
        }

    def test_a_mode_only_the_parent_sets_is_inherited_whole(self):
        assert merge({}, {"video_generation": {"max_duration_seconds": 8}}) == {
            "video_generation": {"max_duration_seconds": 8}
        }

    def test_modes_do_not_leak_into_each_other(self):
        merged = merge(
            {"audio_speech": {"max_n": 9}},
            {"image_generation": {"max_n": 1}},
        )
        assert merged["audio_speech"]["max_n"] == 9
        assert merged["image_generation"]["max_n"] == 1


class TestFloors:
    """`speed_min` is the one media field where a HIGHER value is stricter."""

    def test_the_higher_floor_wins(self):
        assert merge(
            {"audio_speech": {"speed_min": 0.25}},
            {"audio_speech": {"speed_min": 0.8}},
        ) == {"audio_speech": {"speed_min": 0.8}}

    def test_a_child_may_raise_the_floor_further(self):
        assert merge(
            {"audio_speech": {"speed_min": 1.0}},
            {"audio_speech": {"speed_min": 0.8}},
        ) == {"audio_speech": {"speed_min": 1.0}}


class TestWhitelists:
    def test_whitelists_intersect(self):
        assert merge(
            {"audio_speech": {"voice_whitelist": ["alloy", "echo"]}},
            {"audio_speech": {"voice_whitelist": ["alloy", "fable"]}},
        ) == {"audio_speech": {"voice_whitelist": ["alloy"]}}

    def test_an_empty_child_list_means_unconstrained_not_forbidden(self):
        """Intersecting with an empty list would forbid everything — the
        opposite of what an unset whitelist means."""
        assert merge(
            {"audio_speech": {"voice_whitelist": []}},
            {"audio_speech": {"voice_whitelist": ["alloy"]}},
        ) == {"audio_speech": {"voice_whitelist": ["alloy"]}}

    def test_an_empty_parent_list_leaves_the_child_alone(self):
        assert merge(
            {"audio_speech": {"voice_whitelist": ["echo"]}},
            {"audio_speech": {"voice_whitelist": []}},
        ) == {"audio_speech": {"voice_whitelist": ["echo"]}}

    def test_a_disjoint_intersection_forbids_everything(self):
        assert merge(
            {"audio_speech": {"voice_whitelist": ["echo"]}},
            {"audio_speech": {"voice_whitelist": ["alloy"]}},
        ) == {"audio_speech": {"voice_whitelist": []}}

    def test_numeric_whitelists_work_too(self):
        assert merge(
            {"embedding": {"dim_whitelist": [256, 1536]}},
            {"embedding": {"dim_whitelist": [1536, 3072]}},
        ) == {"embedding": {"dim_whitelist": [1536]}}


class TestRequirements:
    def test_a_requirement_a_parent_set_cannot_be_dropped(self):
        assert merge(
            {"image_generation": {"require_image_input": False}},
            {"image_generation": {"require_image_input": True}},
        ) == {"image_generation": {"require_image_input": True}}

    def test_a_child_may_add_a_requirement(self):
        assert merge(
            {"image_generation": {"require_image_input": True}},
            {"image_generation": {"require_image_input": False}},
        ) == {"image_generation": {"require_image_input": True}}


class TestUnknownFields:
    def test_a_field_with_no_rule_is_inherited_but_never_guessed_at(self):
        """Guessing a direction for an unrecognised field would silently
        invent policy; the proxy leaves them alone and so does this."""
        merged = merge(
            {"audio_speech": {"future_field": 5}},
            {"audio_speech": {"future_field": 1, "other_new_field": 2}},
        )
        assert merged["audio_speech"]["future_field"] == 5, "child's value untouched"
        assert merged["audio_speech"]["other_new_field"] == 2, "inherited when absent"


class TestRelaxationRefusal:
    """What the PATCH endpoint refuses."""

    def test_raising_an_inherited_ceiling_is_refused(self):
        found = relaxations(
            {"audio_speech": {"max_input_characters": 5000}},
            {"audio_speech": {"max_input_characters": 500}},
        )
        assert len(found) == 1
        assert "audio_speech.max_input_characters" in found[0]
        assert "5000" in found[0] and "500" in found[0]

    def test_lowering_it_is_allowed(self):
        assert relaxations(
            {"audio_speech": {"max_input_characters": 100}},
            {"audio_speech": {"max_input_characters": 500}},
        ) == []

    def test_lowering_an_inherited_floor_is_refused(self):
        found = relaxations(
            {"audio_speech": {"speed_min": 0.2}},
            {"audio_speech": {"speed_min": 0.8}},
        )
        assert len(found) == 1
        assert "inherited minimum" in found[0]

    def test_widening_a_whitelist_is_refused_and_names_the_extras(self):
        found = relaxations(
            {"audio_speech": {"voice_whitelist": ["alloy", "echo"]}},
            {"audio_speech": {"voice_whitelist": ["alloy"]}},
        )
        assert len(found) == 1
        assert "echo" in found[0]

    def test_narrowing_a_whitelist_is_allowed(self):
        assert relaxations(
            {"audio_speech": {"voice_whitelist": ["alloy"]}},
            {"audio_speech": {"voice_whitelist": ["alloy", "echo"]}},
        ) == []

    def test_an_unconstrained_inherited_whitelist_constrains_nothing(self):
        assert relaxations(
            {"audio_speech": {"voice_whitelist": ["anything"]}},
            {"audio_speech": {"voice_whitelist": []}},
        ) == []

    def test_dropping_an_inherited_requirement_is_refused(self):
        found = relaxations(
            {"image_generation": {"require_image_input": False}},
            {"image_generation": {"require_image_input": True}},
        )
        assert len(found) == 1
        assert "cannot switch off" in found[0]

    def test_a_mode_the_child_does_not_touch_is_not_a_violation(self):
        assert relaxations(
            {"image_generation": {"max_n": 1}},
            {"audio_speech": {"max_input_characters": 100}},
        ) == []

    def test_nothing_inherited_means_nothing_to_violate(self):
        assert relaxations({"audio_speech": {"max_input_characters": 99999}}, None) == []
        assert relaxations(None, {"audio_speech": {"max_input_characters": 1}}) == []

    @pytest.mark.parametrize("field,rule", sorted(MEDIA_GOVERNANCE_FIELD_RULES.items()))
    def test_every_rule_kind_is_reachable_by_the_validator(self, field, rule):
        """A rule in the table that the validator never checks would be a
        field a child could quietly relax."""
        if rule == "whitelist":
            new, floor = {"m": {field: ["x"]}}, {"m": {field: ["y"]}}
        elif rule == "ceiling":
            new, floor = {"m": {field: 10}}, {"m": {field: 1}}
        elif rule == "floor":
            new, floor = {"m": {field: 1}}, {"m": {field: 10}}
        else:
            new, floor = {"m": {field: False}}, {"m": {field: True}}
        assert relaxations(new, floor), f"{field} ({rule}) is never refused"
