# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
What the install seed and tenant provisioning write, recorded for each
edition.

`tests/seed_recorder.py` runs a first start, a restart and the provisioning
of a second tenant on an empty database, in a process of its own, once as
the full composition and once as the Community composition. What each phase
wrote is held against its recording, `tests/fixtures/seed_recording_community.json`
here and `enterprise/tests/fixtures/seed_recording_enterprise.json` in the
Enterprise half: the write statements in order, and the rows added to each table.

It was first recorded when the Enterprise seed and provisioning steps were
still in the built-in lists; they are now registered by an extension,
*after* the built-in ones. That order is accepted only where the seed
recorder shows identical rows: this is that recorder. A difference in `writes` alone is a step that moved; a difference
in `rows` is a different database.

Runs on SQLite, which the seed supports. What it cannot show: behaviour that
exists on Postgres only (the edition-install tests run the real install there).

To re-record on purpose:
    BRUTOR_UPDATE_SEED_RECORDING=1 pytest tests/test_seed_recording.py
"""

from __future__ import annotations

import difflib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests import seed_recorder
from tests.edition_boundary import REPO, control_plane_boundary, enterprise_only_tables

FIXTURES = Path(__file__).parent / "fixtures"
UPDATE_ENV = "BRUTOR_UPDATE_SEED_RECORDING"
COMPOSITIONS = ("enterprise", "community")
PHASES = ("install", "second_start", "second_tenant")


def _fixture(composition: str) -> Path:
    """The Community recording. (The Enterprise one is under `enterprise/`;
    the Enterprise half says where.)"""
    assert composition == "community", composition
    return FIXTURES / f"seed_recording_{composition}.json"


def _record(composition: str, tmp_path_factory) -> str:
    output = tmp_path_factory.mktemp("seed") / f"{composition}.json"
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(REPO)}
    env.pop("COV_CORE_SOURCE", None)  # the recorder is not part of the coverage run
    proc = subprocess.run(
        [sys.executable, str(Path(seed_recorder.__file__)), composition, str(output)],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert f"RECORDED {composition}" in proc.stdout, proc.stderr[-4000:]
    return output.read_text(encoding="utf-8")


# This file's composition. The Enterprise composition is recorded, and its
# recording examined, by `enterprise/tests/test_seed_recording.py`: what this
# file asserts does not depend on whether the checkout has `enterprise/`.
HERE = ("community",)


@pytest.fixture(scope="module", params=HERE)
def run(request, tmp_path_factory):
    return request.param, _record(request.param, tmp_path_factory)


@pytest.fixture(scope="module")
def recorded():
    return {c: json.loads(_fixture(c).read_text(encoding="utf-8")) for c in HERE}


@pytest.fixture(scope="module")
def recording(run):
    """Where the recording of the composition that ran is kept."""
    return _fixture(run[0])


def test_the_seed_writes_what_was_recorded(run, recording, tmp_path):
    composition, current = run
    path = recording
    if os.getenv(UPDATE_ENV) == "1":
        path.write_text(current, encoding="utf-8")
        pytest.skip(f"{UPDATE_ENV}=1 set: recording regenerated at {path}. Commit it.")
    assert path.exists(), f"no recording at {path}; record it with {UPDATE_ENV}=1"
    expected = path.read_text(encoding="utf-8")
    if current != expected:
        actual = tmp_path / f"seed_recording_{composition}.actual.json"
        actual.write_text(current, encoding="utf-8")
        diff = list(
            difflib.unified_diff(
                expected.splitlines(), current.splitlines(), "recorded", "now", lineterm="", n=0
            )
        )
        shown = "\n".join(line[:400] for line in diff[:60])
        pytest.fail(
            f"the {composition} composition's seed no longer writes what was recorded "
            f"({len(diff)} diff lines; the run is in {actual}).\n"
            "A difference under \"writes\" only: a step moved. Under \"rows\": a different database.\n"
            f"If INTENTIONAL, review it and re-record with {UPDATE_ENV}=1.\n{shown}"
        )


@pytest.mark.parametrize("composition", HERE)
def test_a_recording_has_the_three_phases(recorded, composition):
    assert tuple(recorded[composition]) == PHASES
    install = recorded[composition]["install"]
    assert len(install["writes"]) > 50 and len(install["rows"]) > 25
    for change in install["rows"].values():
        assert change["removed"] == []


@pytest.mark.parametrize("composition", HERE)
def test_a_restart_writes_nothing(recorded, composition):
    """The seed is idempotent: the second start issues no write statement."""
    assert recorded[composition]["second_start"] == {"writes": [], "rows": {}}


@pytest.mark.parametrize("composition", HERE)
def test_provisioning_a_second_tenant_only_adds_rows(recorded, composition):
    second = recorded[composition]["second_tenant"]
    assert second["rows"]
    for table, change in second["rows"].items():
        assert change["removed"] == [], table
    # No row of the first tenant goes, and nothing is deleted. (The sample
    # agent card's seed, which cleared a new card's capabilities before
    # writing them, seeds nothing where BRUTOR_A2A_SAMPLE_PUBLIC_URL is unset,
    # as in the recorder's environment.)
    assert [w[:2] for w in second["writes"] if w[0] == "DELETE"] == []


def test_the_community_seed_writes_no_enterprise_only_table(recorded):
    """Otherwise a Community Edition would refuse its own database at the
    next start (the edition guard)."""
    enterprise_tables = set(enterprise_only_tables(control_plane_boundary()))
    for phase in PHASES:
        body = recorded["community"][phase]
        assert sorted(set(body["rows"]) & enterprise_tables) == [], phase
        assert sorted({w[1] for w in body["writes"]} & enterprise_tables) == [], phase


# ---------------------------------------------------------------------------
# The masks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, masked",
    [
        ("role-0123456789abcdef", "role-<hex>"),
        ("3f2b8c1e-1111-4222-8333-444455556666", "<uuid>"),
        ("llm-01M74R93C44A9JRVFAJ372TJT4", "llm-<ulid>"),
        ("2026-11-11T07:28:16.123456+00:00", "<timestamp>"),
        ("$2b$12$" + "a" * 53, "<bcrypt>"),
        ("gAAAAAB" + "x" * 40 + "==", "<fernet>"),
        ("group-default-default", "group-default-default"),
        ("deadbeef", "deadbeef"),
        ({"b": ["01M74R93C44A9JRVFAJ372TJT4"], "a": 1}, {"a": 1, "b": ["<ulid>"]}),
        ('{"id": "01M74R93C44A9JRVFAJ372TJT4"}', {"id": "<ulid>"}),
        (None, None),
        (7, 7),
    ],
)
def test_mask(value, masked):
    assert seed_recorder._mask(value) == masked


def test_a_long_text_is_recorded_as_its_length_and_digest():
    text = "x" * (seed_recorder.LONG_TEXT + 1)
    masked = seed_recorder._mask(text)
    assert masked.startswith(f"<text length={len(text)} sha256=")
    assert seed_recorder._mask(text + "y") != masked


def test_the_community_tree_holds_the_community_recording_alone():
    """The Enterprise composition's recording is the Enterprise Edition's
    (`enterprise/tests/fixtures/`): outside `enterprise/` there is one
    recording, this file's."""
    found = sorted(
        path.relative_to(REPO).as_posix()
        for path in REPO.rglob("seed_recording_*.json")
        if path.relative_to(REPO).parts[0] not in ("enterprise", ".venv", "node_modules", "mcp-server-builds")
    )
    assert found == ["tests/fixtures/seed_recording_community.json"]
