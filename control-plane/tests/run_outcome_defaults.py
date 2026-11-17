# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Test helper: the declared-outcome columns a finalized run carries.

Under THE run classification (`app/services/ai_system_outcomes.py`) a
`completed` run is a success only when it ended in a declared outcome that
counts toward the baseline; without one it is a deviation. Fixtures that
seed "a completed run" mean "a run that did its job", so they spread
`**work_outcome(state)` into the `AISystemRun(...)` they build.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

COMPLETED = ("completed", "completed_degraded")


def work_outcome(state: Optional[str], outcome_id: str = "task_completed") -> Dict[str, Any]:
    if state in COMPLETED:
        return {
            "outcome_id": outcome_id,
            "outcome_conformance": "declared",
            "outcome_counts_toward_baseline": True,
        }
    return {"outcome_conformance": "unreported"}
