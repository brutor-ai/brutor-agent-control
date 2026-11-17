# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Rules about a day read the UTC day, never the host's local date.

The gateway stops calling a model on its retire date in UTC
(`retire_date > (now() AT TIME ZONE 'UTC')::date`), and the control plane
says the same through `availability._today()`. Every rule of the control
plane about a model's retire date reads that day: seeding a model that is
already retired switched off, the catalog's deprecation horizon, the
retirement warner's alerts. The obligation register reads the UTC day too:
its evidence matrix and the compliance overview date each action by its
UTC timestamp and split a window at UTC midnight when the catalog version
changes, and the board and a system's obligations
answer on the same day as the matrix in the same response.

Each case pins the clock where the host's local date and the UTC date
differ: 00:30 on the 16th at UTC+2 is 22:30 on the 15th in UTC, and 21:00
on the 15th at UTC-5 is 02:00 on the 16th in UTC.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from tests import utc_clock
from tests.test_admin_ai_obligations_http import COPILOT, harness  # noqa: F401 — fixture
from tests.test_llm_retirement import _seed_model, db  # noqa: F401 — fixture

AHEAD = dict(utc=datetime(2026, 11, 15, 22, 30), local_offset_hours=2)   # local 16th, UTC 15th
BEHIND = dict(utc=datetime(2026, 11, 16, 2, 0), local_offset_hours=-5)   # local 15th, UTC 16th
RETIRE = date(2026, 11, 16)


# --- seeding --------------------------------------------------------------------


@pytest.mark.parametrize("clock, served", [(AHEAD, True), (BEHIND, False)])
def test_a_seeded_model_is_switched_off_from_its_retire_date_in_utc(monkeypatch, clock, served):
    from app.database import init_llms

    utc_day, local_day = utc_clock.pin(monkeypatch, init_llms, **clock)
    assert utc_day != local_day
    cfg = init_llms.build_model_config(
        "openai", {"model_name": "gpt-retiring", "name": "Retiring", "retire_date": RETIRE, "enabled": True},
    )
    assert cfg["enabled"] is served


def test_the_deprecation_horizon_counts_from_the_utc_day(monkeypatch):
    from app.database import init_llms

    utc_day, _ = utc_clock.pin(monkeypatch, init_llms, **AHEAD)
    edge = utc_day + timedelta(days=init_llms.DEPRECATION_HORIZON_DAYS)
    assert init_llms.catalog_deprecated({"retire_date": edge}) is True
    assert init_llms.catalog_deprecated({"retire_date": edge + timedelta(days=1)}) is False


# --- the retirement warner ------------------------------------------------------


@pytest.mark.parametrize("clock, kind", [(AHEAD, "retirement_warning"), (BEHIND, "retirement_overdue")])
def test_the_warner_says_retired_from_the_retire_date_in_utc(monkeypatch, db, clock, kind):  # noqa: F811
    from app.services.llm import retirement_warner

    utc_clock.pin(monkeypatch, retirement_warner, **clock)
    _seed_model(db, retire_date=RETIRE, retirement_reminder_date=RETIRE - timedelta(days=14))
    [alert] = retirement_warner.run_once(db)
    assert alert.context["kind"] == kind


# --- the obligation register ----------------------------------------------------

# Article 12 applies from 2 December 2027: at 00:30 on that day at UTC+2 it
# is still 1 December in UTC.
ART12 = "art12-record-keeping"
EVE_OF_ART12 = dict(utc=datetime(2027, 12, 1, 22, 30), local_offset_hours=2)


@pytest.fixture
def obligations(monkeypatch, harness):  # noqa: F811
    from app.api.v1.endpoints.admin import ai_obligations

    # FastAPI reads the routes' annotations (`datetime` among them) on the
    # first request: one is made before the module's names are pinned.
    for path in ("/v1/admin/compliance/obligations", f"/v1/admin/ai-systems/{COPILOT}/obligations"):
        assert harness.client.get(path, params={"include_matrix": False}, headers=harness.headers()).status_code == 200
    utc_clock.pin(monkeypatch, ai_obligations, **EVE_OF_ART12)
    return harness


def test_the_board_answers_on_the_utc_day(obligations):
    r = obligations.client.get("/v1/admin/compliance/obligations",
                               params={"include_matrix": False}, headers=obligations.headers())
    assert r.status_code == 200, r.text
    board = r.json()
    assert board["as_of"] == "2027-12-01"
    art12 = next(o for o in board["obligations"] if o["key"] == ART12)
    assert art12["applies_from"] == "2027-12-02" and art12["in_force"] is False


def test_a_systems_obligations_answer_on_the_utc_day(obligations):
    r = obligations.client.get(f"/v1/admin/ai-systems/{COPILOT}/obligations",
                               headers=obligations.headers())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["as_of"] == "2027-12-01"
    assert next(o for o in body["obligations"] if o["key"] == ART12)["in_force"] is False
