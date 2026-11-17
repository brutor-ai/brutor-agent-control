# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Agent implementation × assurance checks — the release facts in the tier-A
# check vocabulary

`RUN_FACT_KEYS` is the wire contract between the core (which builds the
`run` object tier-A CEL checks evaluate against,
`services/run_ledger.rs::RUN_FACT_KEYS`) and this repo (whose mirror feeds
the NL compile assist and the authoring reference panel). The run row has
carried the actor's declared release since `a187_agent_implementation`, but the vocabulary did
not, so "flag runs of any release but the approved one" could not be
written. These are the house pins: the keys are present on both sides, the
two lists are identical and sorted, and every key has a guide entry (a key
without one is a fact the assist never proposes).

The end-to-end evaluation — a check on `run.agent_impl_version` breaching on
finalize and in the backtest — is the core's
`tests/integration/agent_impl.rs::a_tier_a_check_on_agent_impl_version_works_end_to_end`.
"""

from __future__ import annotations

import re

import pytest

from app.constants.assurance import RUN_FACT_GUIDE, RUN_FACT_KEYS
from app.database.models.ai_system_run import AISystemRun
from tests.core_repo import core_source

RELEASE_FACTS = (
    "agent_impl_name",
    "agent_impl_version",
    "agent_impl_build",
    "agent_impl_trust",
    "implementation_fingerprint",
    "client_label",
)


@pytest.mark.parametrize("key", RELEASE_FACTS)
def test_the_release_facts_are_check_vocabulary(key):
    assert key in RUN_FACT_KEYS
    assert key in RUN_FACT_GUIDE
    # The fact is a column of the finalized run (what the backtest reloads).
    assert key in AISystemRun.__table__.c


def test_the_vocabulary_is_sorted_and_every_key_is_guided():
    assert tuple(RUN_FACT_KEYS) == tuple(sorted(RUN_FACT_KEYS)), "the pinned vocabulary stays sorted"
    assert len(set(RUN_FACT_KEYS)) == len(RUN_FACT_KEYS)
    assert set(RUN_FACT_GUIDE) == set(RUN_FACT_KEYS), (
        f"guide-only={sorted(set(RUN_FACT_GUIDE) - set(RUN_FACT_KEYS))}, "
        f"unguided={sorted(set(RUN_FACT_KEYS) - set(RUN_FACT_GUIDE))}"
    )


def test_the_trust_guide_names_the_column_vocabulary():
    documented = set(re.findall(r"'([a-z_]+)'", RUN_FACT_GUIDE["agent_impl_trust"]))
    check = next(
        c for c in AISystemRun.__table__.constraints if c.name == "ck_ai_system_runs_agent_impl_trust"
    )
    assert documented == set(re.findall(r"'([a-z_]+)'", str(check.sqltext)))


def test_the_core_pins_the_same_list():
    path = core_source("services", "run_ledger.rs")
    if not path.exists():
        pytest.fail(f"core checkout not found at {path} — the cross-repo pin cannot run")
    src = path.read_text()
    start = src.index("pub const RUN_FACT_KEYS")
    block = src[start : src.index("];", start)]
    assert tuple(re.findall(r'"([a-z0-9_]+)"', block)) == tuple(RUN_FACT_KEYS)
