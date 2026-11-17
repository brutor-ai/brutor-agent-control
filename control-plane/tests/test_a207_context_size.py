# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
a207_context_size — context size: the migration, the models and the run read path.
"""

from __future__ import annotations



from app.api.v1.models.admin.ai_system_run import ContextSignal, RunDetail
from app.database.models.ai_system_run import AISystemRun
from app.database.models.proxy_log import ProxyLog

RUN_COLUMNS = ("peak_context_tokens", "peak_context_utilization", "context_growth")


def test_the_models_declare_what_the_migration_adds():
    assert "llm_context_tokens" in ProxyLog.__table__.columns
    for name in RUN_COLUMNS:
        column = AISystemRun.__table__.columns[name]
        assert column.nullable, f"{name}: NULL is 'not measured', so it must stay nullable"
        assert column.server_default is None, f"{name}: a default would read as a measured value"


def test_every_declared_field_reaches_the_response_models():
    # "Pydantic strips undeclared fields": the to_dict and the model must both
    # carry them, or the console shows nothing while the database is right.
    run = AISystemRun(peak_context_tokens=5, peak_context_utilization=0.5, context_growth=2.0)
    as_dict = run.to_dict()
    for name in RUN_COLUMNS:
        assert name in as_dict
        assert name in RunDetail.model_fields


def test_an_empty_signal_says_not_measured_rather_than_zero():
    signal = ContextSignal()
    assert signal.measured_runs == 0
    assert signal.median_peak_tokens is None
    assert signal.p95_utilization is None
