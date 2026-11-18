# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The CI jobs work on a fresh runner (in either layout, `tests/workflows.py`).

* The Rust schema fixture is dumped with the server's `pg_dump`. On the
  runner, `pg_dump` is Debian's wrapper, which runs the newest installed
  client, not the one the job installs; a newer `pg_dump` writes statements
  the fixture does not have (`SET transaction_timeout`), and the drift check
  fails on the tool, not on the schema.
* The test jobs have time for a cold run (no pip cache): the Community suite
  was cancelled at 76 % by a 45-minute timeout on its first run on a new
  runner.
"""

from __future__ import annotations

import re

import yaml

from tests import workflows


def _jobs() -> dict:
    return yaml.safe_load(workflows.workflow_text("ci.yml"))["jobs"]


def _runs(job: dict) -> list:
    return [str(step.get("run", "")) for step in job["steps"]]


def test_the_schema_fixture_is_dumped_with_the_servers_pg_dump():
    job = _jobs()["rust-schema-fixture"]
    major = re.fullmatch(r"postgres:(\d+)", job["services"]["postgres"]["image"]).group(1)
    runs = _runs(job)
    check = next(i for i, run in enumerate(runs) if "regen-schema.sh --check" in run)
    installs = [i for i, run in enumerate(runs[:check]) if f"apt-get install -y postgresql-client-{major}" in run]
    assert installs, "the server's client is installed"
    first_on_path = [
        i for i, run in enumerate(runs[:check]) if f'echo "/usr/lib/postgresql/{major}/bin" >> "$GITHUB_PATH"' in run
    ]
    assert first_on_path and first_on_path[-1] > installs[-1], (
        "the server's pg_dump is put first on PATH after it is installed and before the comparison"
    )


# A cold run of each suite, in minutes, measured on the first public run
# (ubuntu-latest, no pip cache): the Community suite stood at 76 % after 45
# minutes, about 60 for all of it. The Enterprise job runs the Community suite
# and the Enterprise Edition's own (about 30 % more tests). A timeout covers a
# cold run with half again as much room.
COLD_RUN_MINUTES = {"test-community": 60, "test-enterprise": 80}


def test_the_test_jobs_have_time_for_a_cold_run():
    jobs = _jobs()
    for job_id, minutes in COLD_RUN_MINUTES.items():
        if job_id not in jobs:
            # The public repository runs the Community job alone.
            assert workflows.layout() == workflows.MONOREPO and job_id == "test-enterprise", job_id
            continue
        assert jobs[job_id]["timeout-minutes"] >= minutes * 1.5, (job_id, jobs[job_id]["timeout-minutes"])
