# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
a215_batch_job_agent — `batch_jobs.agent_id`.

A batch job submitted with a workload agent's own token has no API key and no
end user. The table had no column for the agent, so the gateway wrote
`username = "agent:<id>"` as a stand-in. The job now names its agent in a
column of its own, indexed as the table's other owner columns are
(`tenant_id, <owner>, created_at`). Nullable; no data is migrated.
"""

from __future__ import annotations


import sqlalchemy as sa

from app.database.models.batch_job import BatchJob

INDEX = "ix_batch_job_tenant_agent"


def test_the_column():
    column = BatchJob.__table__.columns["agent_id"]
    assert column.nullable is True
    assert isinstance(column.type, sa.String) and column.type.length == 255
    # Like `api_key_id`: the job's history outlives the agent, so no foreign key.
    assert not column.foreign_keys


def test_it_is_indexed_as_the_other_owner_columns_are():
    indexes = {i.name: [c.name for c in i.columns] for i in BatchJob.__table__.indexes}
    assert indexes["ix_batch_job_tenant_user"] == ["tenant_id", "user_id", "created_at"]
    assert indexes[INDEX] == ["tenant_id", "agent_id", "created_at"]


def test_to_dict_and_the_summary_carry_it():
    counts = dict(total_requests=1, completed_requests=0, failed_requests=0)
    job = BatchJob(id="btj-1", tenant_id="t1", provider="openai", model="m", status="pending", agent_id="agent-1",
                   **counts)
    assert job.to_dict()["agent_id"] == "agent-1"
    assert job.to_summary_dict()["agent_id"] == "agent-1"
    keyed = BatchJob(id="btj-2", tenant_id="t1", provider="openai", model="m", status="pending", **counts)
    assert keyed.to_dict()["agent_id"] is None
