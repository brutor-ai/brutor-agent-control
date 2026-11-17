# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Measurement changes declare their own epochs

Found live on the Brutor Demo System: a platform release changed how a
run's client label is measured (raw User-Agent → product name). `client_mix`
drift fired three times and a response policy demoted the system once — the
platform had changed its ruler, and the detector read it as the agent
changing.

Pinned here, each as a class:
  * the registry is well-formed, append-only, names only metric keys the core
    defines, and its head is the id the core pins beside the hash of its
    metric definitions (`drift::measurement`);
  * applying writes one `measurement_change` epoch per (system, metric,
    basis) that has a baseline on an affected metric/basis — and nothing
    for other metrics, bases or systems — records that it applied, and
    never applies again (restart, second worker);
  * a failure applies nothing and is retried at the next start;
  * startup calls it after migrations and seeding, inside the init lock;
  * the epoch is honoured like any epoch (the control plane's exclusion
    floor) and the baselines endpoint and the assurance report show it with
    its reason.
"""

from __future__ import annotations

import ast
import re
from datetime import datetime, timedelta, timezone
from itertools import count
from pathlib import Path

import pytest

from app.api.v1.endpoints.admin.ai_system_drift import create_ai_system_drift_router
from app.constants.drift_resolution import EPOCH_KINDS, KIND_MEASUREMENT_CHANGE
from app.constants.measurement_changes import (
    DECLARED_BY_PLATFORM,
    MEASUREMENT_REGISTRY_HEAD,
    REGISTRY,
    MeasurementChange,
)
from app.core.logging_system import BrutorLogger
from app.database.models.ai_system_drift import BASES, AISystemBaseline
from app.database.models.ai_system_drift_resolution import AISystemBaselineEpoch
from app.database.models.platform_measurement_change import PlatformMeasurementChange
from app.database.models.resource_group import GroupType, ResourceGroup
from app.services import measurement_changes as mc
from tests.admin_endpoint_harness import build_admin_harness
from tests.core_repo import core_source
from tests.source_trees import python_sources

ROOT = Path(__file__).resolve().parents[1]
_n = count(1)

#: Released ids, in order. APPEND-ONLY: an installation may already have
#: applied any of these, so an edit or removal would leave it inconsistent.
RELEASED = ("m0001_client_label_product",)


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------


def _core_metric_keys() -> set:
    src = core_source("services", "drift", "mod.rs").read_text()
    block = src.split("pub mod metric {", 1)[1].split("\n}\n", 1)[0]
    return set(re.findall(r'pub const [A-Z_]+: &str = "([a-z_]+)";', block))


class TestRegistry:
    def test_append_only(self):
        ids = tuple(m.id for m in REGISTRY)
        assert ids[: len(RELEASED)] == RELEASED, (
            "a released measurement change was edited, reordered or removed; add a new "
            "entry instead"
        )

    def test_ids_unique_sorted_and_bounded(self):
        ids = [m.id for m in REGISTRY]
        assert len(ids) == len(set(ids))
        assert ids == sorted(ids)
        for i in ids:
            assert re.fullmatch(r"m\d{4}_[a-z0-9_]+", i) and len(i) <= 64, i

    def test_entries_name_real_metrics_and_bases(self):
        known = _core_metric_keys()
        assert "client_mix" in known and len(known) >= 10, "core metric scan is broken"
        for m in REGISTRY:
            assert m.metric_keys and set(m.metric_keys) <= known, m.id
            assert m.bases and set(m.bases) <= set(BASES), m.id
            assert m.reason.strip() and len(m.reason) >= 40, m.id
            assert re.fullmatch(r"\d+\.\d+\.\d+", m.introduced_in_version), m.id

    def test_head_is_pinned_by_the_core(self):
        """The core hashes how metrics are measured and ties the hash to this
        id. Changing a definition fails the core's test until a registry entry
        is added here and the head bumped in both repos."""
        src = core_source("services", "drift", "measurement.rs").read_text()
        m = re.search(r'pub const MEASUREMENT_REGISTRY_HEAD: &str = "([^"]+)";', src)
        assert m, "the core no longer pins MEASUREMENT_REGISTRY_HEAD"
        assert m.group(1) == MEASUREMENT_REGISTRY_HEAD == REGISTRY[-1].id
        assert "MEASUREMENT_DEFINITIONS_SHA256" in src

    def test_the_client_label_change_is_registered(self):
        m = next(x for x in REGISTRY if x.id == "m0001_client_label_product")
        assert m.metric_keys == ("client_mix",) and m.bases == ("runs",)
        assert "X-Brutor-Client" in m.reason

    def test_the_epoch_kind_is_in_the_vocabulary(self):
        assert KIND_MEASUREMENT_CHANGE == "measurement_change"
        assert KIND_MEASUREMENT_CHANGE in EPOCH_KINDS
        check = next(
            c for c in AISystemBaselineEpoch.__table__.constraints
            if getattr(c, "name", None) == "ck_ai_system_baseline_epochs_kind"
        )
        assert "'measurement_change'" in str(check.sqltext)
        AISystemBaselineEpoch(kind=KIND_MEASUREMENT_CHANGE)  # validator accepts it

    def test_the_rule_is_documented_where_migrations_are(self):
        text = (ROOT / "alembic" / "README.md").read_text()
        assert "measurement_changes.py" in text and "measured" in text, (
            "alembic/README.md (where schema changes are documented) must state: a "
            "change that alters how a metric is measured adds a registry entry"
        )


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------


@pytest.fixture()
def h():
    log = BrutorLogger("test-measurement-changes")
    harness = build_admin_harness(
        [create_ai_system_drift_router(log)], logger_name="test-measurement-changes",
    )
    yield harness
    harness.close()


def _system(h, tenant="t1"):
    gid = f"sys-{next(_n)}"
    h.seed(lambda s: s.add(ResourceGroup(
        id=gid, tenant_id=tenant, name=gid, description=gid, hierarchy_level=0,
        inherit_resources=True, is_active=True, is_system=False,
        group_type=GroupType.AI_SYSTEM, system_kind="agent",
    )))
    return gid


def _baseline(h, gid, metric, basis="runs", tenant="t1", caller="all"):
    h.seed(lambda s: s.add(AISystemBaseline(
        id=f"base-{next(_n)}", tenant_id=tenant, ai_system_id=gid, metric_key=metric,
        basis=basis, caller_system_id=caller, sample_count=80, state="ready",
    )))


def _epochs(h):
    s = h.Factory()
    try:
        return [
            (e.ai_system_id, e.metric_key, e.basis, e.kind, e.measurement_change_id,
             e.caller_system_id, e.declared_by)
            for e in s.query(AISystemBaselineEpoch).order_by(
                AISystemBaselineEpoch.ai_system_id, AISystemBaselineEpoch.metric_key)
        ]
    finally:
        s.close()


def _apply(h, **kw):
    s = h.Factory()
    try:
        return mc.apply_pending(s, platform_version="test", **kw)
    finally:
        s.close()


ENTRY = REGISTRY[0]


class TestApply:
    def test_every_affected_baseline_gets_an_epoch_and_nothing_else(self, h):
        a, b, c = _system(h), _system(h), _system(h)
        _baseline(h, a, "client_mix")
        _baseline(h, a, "action_count")          # other metric — untouched
        _baseline(h, b, "client_mix", "segments")  # other basis — untouched
        _baseline(h, c, "client_mix")
        _baseline(h, c, "client_mix", caller="sys-caller")  # per-caller row: one 'all' epoch covers it

        done = _apply(h, registry=(ENTRY,))

        assert [d.id for d in done] == [ENTRY.id]
        assert done[0].epochs_written == 2
        rows = _epochs(h)
        assert {(r[0], r[1], r[2]) for r in rows} == {(a, "client_mix", "runs"), (c, "client_mix", "runs")}
        for r in rows:
            assert r[3:] == (KIND_MEASUREMENT_CHANGE, ENTRY.id, "all", DECLARED_BY_PLATFORM)

    def test_applies_once_ever(self, h):
        a = _system(h)
        _baseline(h, a, "client_mix")
        first = _apply(h, registry=(ENTRY,))
        _baseline(h, _system(h), "client_mix")  # a baseline learned after it applied
        second = _apply(h, registry=(ENTRY,))
        assert [d.epochs_written for d in first] == [1]
        assert second == []
        assert len(_epochs(h)) == 1

        s = h.Factory()
        try:
            row = s.get(PlatformMeasurementChange, ENTRY.id)
            assert row.epochs_written == 1 and row.platform_version == "test"
            assert row.metric_keys == ["client_mix"] and row.reason == ENTRY.reason
        finally:
            s.close()

    def test_a_fresh_installation_records_it_with_no_epochs(self, h):
        done = _apply(h, registry=(ENTRY,))
        assert [(d.id, d.epochs_written) for d in done] == [(ENTRY.id, 0)]
        _baseline(h, _system(h), "client_mix")
        assert _apply(h, registry=(ENTRY,)) == []
        assert _epochs(h) == []

    def test_the_epoch_starts_when_it_applied(self, h):
        a = _system(h)
        _baseline(h, a, "client_mix")
        at = datetime(2026, 9, 29, 9, 30, tzinfo=timezone.utc)
        _apply(h, registry=(ENTRY,), now=at)
        s = h.Factory()
        try:
            e = s.query(AISystemBaselineEpoch).one()
            got = e.starts_at if e.starts_at.tzinfo else e.starts_at.replace(tzinfo=timezone.utc)
            assert got == at
        finally:
            s.close()

    def test_a_failure_applies_nothing_and_retries(self, h, monkeypatch):
        a = _system(h)
        _baseline(h, a, "client_mix")
        broken = MeasurementChange(
            id="m9999_broken", metric_keys=("client_mix",), bases=("runs",),
            reason="x" * 40, introduced_in_version="9.9.9",
        )
        real = AISystemBaselineEpoch.__init__

        def boom(self, *args, **kwargs):
            raise RuntimeError("simulated")

        monkeypatch.setattr(AISystemBaselineEpoch, "__init__", boom)
        assert _apply(h, registry=(broken,)) == []
        monkeypatch.setattr(AISystemBaselineEpoch, "__init__", real)
        s = h.Factory()
        try:
            assert s.get(PlatformMeasurementChange, broken.id) is None
        finally:
            s.close()
        assert [d.epochs_written for d in _apply(h, registry=(broken,))] == [1]

    def test_the_epoch_is_honoured_like_any_epoch(self, h):
        """The control plane's exclusion floor reads it (the core's is
        kind-blind by construction, pinned there)."""
        from app.services.ai_system_judgement_exclusion import epoch_floor

        a = _system(h)
        _baseline(h, a, "client_mix")
        at = datetime(2026, 9, 29, 9, 30, tzinfo=timezone.utc)
        _apply(h, registry=(ENTRY,), now=at)
        s = h.Factory()
        try:
            floor = epoch_floor(s, "t1", a, "client_mix", basis="runs", caller_system_id="all")
            floor = floor if floor is None or floor.tzinfo else floor.replace(tzinfo=timezone.utc)
            assert floor == at
        finally:
            s.close()

    def test_baselines_endpoint_and_report_show_it_with_its_reason(self, h):
        from app.services.ai_system_judgement_exclusion import exclusion_summary

        a = _system(h)
        _baseline(h, a, "client_mix")
        _apply(h, registry=(ENTRY,))
        r = h.client.get(f"/v1/admin/ai-systems/{a}/baselines", headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        epoch = r.json()["baselines"][0]["epoch"]
        assert epoch["kind"] == KIND_MEASUREMENT_CHANGE
        assert epoch["measurement_change_id"] == ENTRY.id
        assert epoch["reason"] == ENTRY.reason

        s = h.Factory()
        try:
            summary = exclusion_summary(
                s, "t1", a, since=datetime.now(timezone.utc) - timedelta(days=30))
        finally:
            s.close()
        [change] = summary["measurement_changes"]
        assert change["id"] == ENTRY.id
        assert change["metric_keys"] == ["client_mix"] and change["bases"] == ["runs"]
        assert change["reason"] == ENTRY.reason
        assert change["introduced_in_version"] == ENTRY.introduced_in_version


class TestStartupHook:
    def test_init_database_applies_after_migrations_and_seeding(self):
        """Static: inside the advisory lock, after `seed_initial_data()`."""
        src = (ROOT / "app" / "database" / "__init__.py").read_text()
        body = src.split("def init_database", 1)[1].split("\ndef ", 1)[0]
        seed = body.index("seed_initial_data()")
        hook = body.index("apply_pending_at_startup()")
        unlock = body.index("pg_advisory_unlock")
        assert seed < hook < unlock

    def test_the_hook_never_raises(self, monkeypatch):
        def boom(_db):
            raise RuntimeError("db down")

        monkeypatch.setattr(mc, "apply_pending", boom)
        mc.apply_pending_at_startup()  # logs, does not raise

    def test_only_the_service_writes_measurement_change_epochs(self):
        offenders = []
        for p in python_sources((ROOT / "app")):
            if p.name in ("measurement_changes.py", "drift_resolution.py"):
                continue
            tree = ast.parse(p.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Name) and node.id == "KIND_MEASUREMENT_CHANGE":
                    if p.name not in ("ai_system_judgement_exclusion.py",):
                        offenders.append(str(p.relative_to(ROOT)))
        assert offenders == [], offenders
