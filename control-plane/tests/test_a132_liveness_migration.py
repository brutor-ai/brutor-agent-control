# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# a132_liveness_expectations — migration drift suite

Pins the liveness schema and, more importantly, the `system_alerts`
category CHECK.

That CHECK is the part with teeth. The Rust liveness detector writes
alerts with `category = 'ai_system_liveness'` from a background task. If
the constraint were not widened, every one of those inserts would fail —
silently, in a loop nobody is watching, on the one detector whose entire
job is to notice silence. The irony would not make it easier to find.
"""

from __future__ import annotations



from app.database.models.ai_system_liveness import LIVENESS_DETECTORS, LIVENESS_MODES
from app.database.models.system_alert import CATEGORIES


#: Columns later migrations add to the table (each pinned by its own test).


class TestAlertCategoryCheck:
    """The constraint the Rust detector's inserts depend on."""

    def test_category_added_to_the_model_tuple(self):
        assert "ai_system_liveness" in CATEGORIES


class TestWireContractWithRust:
    """Values the Rust detector writes literally.

    Mirrored in `brutor-gateway-core/src/services/liveness.rs`, whose unit
    tests assert the same strings from the other side.
    """

    def test_modes(self):
        assert LIVENESS_MODES == {"continuous", "scheduled", "sporadic", "on_demand"}

    def test_detector_names(self):
        assert LIVENESS_DETECTORS == {
            "silence",
            "missed_schedule",
            "volume_collapse",
            "stall",
            "hard_fail_cliff",
        }

    def test_share_thresholds(self):
        from app.database.models.ai_system_liveness import (
            HARD_FAIL_SHARE,
            MIN_RUNS_FOR_SHARE_DETECTORS,
            STALL_ABANDONED_SHARE,
        )

        assert STALL_ABANDONED_SHARE == 0.5
        assert HARD_FAIL_SHARE == 0.8
        assert MIN_RUNS_FOR_SHARE_DETECTORS == 5
