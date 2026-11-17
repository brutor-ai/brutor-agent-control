# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# The closed loop

Observability tells; this acts. Which means every test here is really
about one question: **what stops it acting wrongly?**

The rails, and the failure each one prevents:

  * cooldown — a standing drift is re-detected every sweep; without it a
    system ratchets from autonomous to suspended inside an afternoon
  * would-loosen — an incident response must never hand a degraded system
    more freedom than it had
  * blast radius — autonomy resolves min-wins *down* the tree, so a policy
    on a parent silently restricts every inheriting child
  * suspend opt-in — auto-killing production on a statistical signal loses
    more trust than any missed detection recovers
  * human restore — auto-restore flaps, because the signal clears *because*
    the system was throttled
  * consider-once — a finding with no matching policy must not be re-planned
    forever
  * the audit entry — an automatic config change with no record of why is
    indistinguishable from a bug
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.database.models.ai_system_drift import AISystemDriftEvent
from app.database.models.ai_system_drift_resolution import (
    AISystemBaselineEpoch,
    AISystemIncidentWindow,
)
from app.database.models.ai_system_response_policy import AISystemResponsePolicy
from app.database.models.resource_group import GroupType, ResourceGroup
from app.database.models.system_operation_log import (
    ACTION_DRIFT_RESPONSE,
    ACTION_DRIFT_RESPONSE_RESTORE,
    ACTOR_TYPE_SYSTEM,
    ACTOR_TYPE_TENANT_ADMIN,
    SYSTEM_ACTOR_DRIFT_RESPONSE,
    VALID_ACTOR_TYPES,
    SystemOperationLog,
)
from app.database.models.tenant import Tenant
from app.services.ai_system_response.actuator import (
    applicable_policies,
    apply_plan,
    inheriting_descendants,
    pending_drift_events,
    plan_for_drift,
    resolved_autonomy,
    restore,
)
from app.services.ai_system_response.evaluator import (
    REFUSED_ALREADY_RESPONDED,
    REFUSED_COOLDOWN,
    REFUSED_NO_CHANGE,
    REFUSED_NO_POLICY,
    REFUSED_WOULD_LOOSEN,
)

# The resource-group relationships load eagerly enough that a missing
# association surfaces as an opaque "no such table" from a lazy load.
_BY_NAME = [
    "llm_catalog",
    "llm_routing_group",
    "llm_providers",
    "llm_provider_catalog",
    "end_user_groups",
    "resource_group_end_user_groups",
]

REQUIRED_TABLES = None  # built lazily in the fixture


@pytest.fixture()
def db():
    from app.database.models.llm_model import LLModel

    tables = [Base.metadata.tables[n] for n in _BY_NAME] + [
        LLModel.__table__,
        Tenant.__table__,
        ResourceGroup.__table__,
        AISystemDriftEvent.__table__,
        AISystemResponsePolicy.__table__,
        SystemOperationLog.__table__,
        # The evaluator asks whether a finding's evidence was
        # excluded by a resolution (epochs, incident windows).
        AISystemBaselineEpoch.__table__,
        AISystemIncidentWindow.__table__,
        # 2026-11-07 — who an approval_required action does not hold.
        Base.metadata.tables["agent_identities"],
        Base.metadata.tables["end_users"],
        Base.metadata.tables["api_keys"],
        Base.metadata.tables["ai_system_runs"],
    ]

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _rec):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(bind=engine, tables=tables)
    session = sessionmaker(bind=engine, future=True)()
    session.add(
        Tenant(
            id="t1", name="t1", display_name="T1", organization_name="T1",
            primary_contact_email="a@b.c", is_active=True, is_default=False,
            is_trial=False, suspended=False, billing_plan="self_hosted", features={},
        )
    )
    session.commit()
    yield session
    session.close()
    engine.dispose()


def system(db, gid="sys", parent=None, inherits=True, autonomy=None):
    g = ResourceGroup(
        id=gid, tenant_id="t1", name=gid,
        hierarchy_level=0 if parent is None else 1,
        inherit_resources=inherits, is_active=True, is_system=False,
        group_type=GroupType.AI_SYSTEM, system_kind="agent",
        parent_group_id=parent, autonomy_level=autonomy,
    )
    db.add(g)
    db.flush()
    return g


def policy(db, pid="p1", on="sys", level="restricted", **kwargs):
    actions = kwargs.pop("actions", None) or (
        [{"kind": "set_autonomy", "level": level}] if level else []
    )
    p = AISystemResponsePolicy(
        id=pid, tenant_id="t1", ai_system_id=on, name=pid, enabled=True,
        signal_kind="drift", severity_at_least="high", actions=actions, **kwargs
    )
    db.add(p)
    db.flush()
    return p


def finding(db, eid="d1", on="sys", severity="critical", drift_class="behavioural"):
    e = AISystemDriftEvent(
        id=eid, tenant_id="t1", ai_system_id=on, drift_class=drift_class,
        metric_key="step_count", severity=severity, attributed_cause="model_change",
        detected_at=datetime.now(timezone.utc), status="active",
    )
    db.add(e)
    db.flush()
    return e


def act(db, event):
    plan = plan_for_drift(db, event)
    apply_plan(db, event, plan)
    db.commit()
    return plan


# ---------------------------------------------------------------------------
# It acts
# ---------------------------------------------------------------------------


class TestItActs:
    def test_a_matching_policy_tightens_autonomy(self, db):
        sys = system(db, autonomy="autonomous")
        policy(db)
        plan = act(db, finding(db))

        assert plan.will_act
        db.refresh(sys)
        assert sys.autonomy_level == "restricted"

    def test_the_change_is_recorded_as_a_system_action(self, db):
        # An operator scanning the audit trail must be able to tell which
        # changes a human made and which the platform made.
        system(db, autonomy="autonomous")
        policy(db)
        act(db, finding(db))

        row = db.query(SystemOperationLog).one()
        assert row.action == ACTION_DRIFT_RESPONSE
        assert row.actor_type == ACTOR_TYPE_SYSTEM
        assert row.actor_admin_id == SYSTEM_ACTOR_DRIFT_RESPONSE

    def test_the_audit_entry_carries_the_triggering_finding(self, db):
        system(db, autonomy="autonomous")
        policy(db)
        act(db, finding(db, eid="d-trigger"))

        payload = db.query(SystemOperationLog).one().payload
        assert payload["drift_event_id"] == "d-trigger"
        assert payload["attributed_cause"] == "model_change"

    def test_the_previous_value_is_captured_so_the_action_is_reversible(self, db):
        # "Reversible" is only true if the inverse was recorded while the
        # previous state was still known.
        system(db, autonomy="autonomous")
        policy(db)
        act(db, finding(db))

        row = db.query(SystemOperationLog).one()
        assert row.before_state == {"autonomy_level": "autonomous"}
        assert row.after_state == {"autonomy_level": "restricted"}

    def test_the_finding_records_what_was_done(self, db):
        system(db, autonomy="autonomous")
        policy(db)
        event = finding(db)
        act(db, event)

        db.refresh(event)
        assert event.response_taken["policy_id"] == "p1"
        assert event.response_taken["actions"][0]["params"]["level"] == "restricted"

    def test_the_policy_records_that_it_fired(self, db):
        system(db, autonomy="autonomous")
        p = policy(db)
        act(db, finding(db))

        db.refresh(p)
        assert p.fire_count == 1
        assert p.last_fired_drift_event_id == "d1"
        assert p.last_fired_at is not None


# ---------------------------------------------------------------------------
# The rails
# ---------------------------------------------------------------------------


class TestCooldown:
    def test_a_second_finding_within_the_cooldown_does_not_fire(self, db):
        # A standing drift is re-detected every sweep. Without this a system
        # ratchets down four levels while an operator reads the first alert.
        system(db, autonomy="autonomous")
        policy(db)
        act(db, finding(db, eid="d1"))
        plan = act(db, finding(db, eid="d2"))

        assert not plan.will_act
        assert [r.code for r in plan.refusals] == [REFUSED_COOLDOWN]

    def test_a_finding_after_the_cooldown_fires_again(self, db):
        sys = system(db, autonomy="autonomous")
        p = policy(db, level="approval_required")
        act(db, finding(db, eid="d1"))

        p.last_fired_at = datetime.now(timezone.utc) - timedelta(seconds=7200)
        sys.autonomy_level = "autonomous"
        db.commit()

        assert act(db, finding(db, eid="d2")).will_act

    def test_the_refusal_is_still_recorded(self, db):
        # Silence after a critical finding is indistinguishable from a broken
        # feature; the UI has to be able to say *why* nothing happened.
        system(db, autonomy="autonomous")
        policy(db)
        act(db, finding(db, eid="d1"))
        event = finding(db, eid="d2")
        act(db, event)

        db.refresh(event)
        assert event.response_taken["refusals"][0]["code"] == REFUSED_COOLDOWN


class TestItRefusesToLoosen:
    def test_a_policy_targeting_more_freedom_than_current_does_not_fire(self, db):
        sys = system(db, autonomy="restricted")
        policy(db, level="approval_required")
        plan = act(db, finding(db))

        assert not plan.will_act
        assert [r.code for r in plan.refusals] == [REFUSED_WOULD_LOOSEN]
        db.refresh(sys)
        assert sys.autonomy_level == "restricted"

    def test_a_policy_targeting_the_current_level_is_a_no_op(self, db):
        system(db, autonomy="restricted")
        policy(db, level="restricted")
        plan = act(db, finding(db))

        assert not plan.will_act
        assert [r.code for r in plan.refusals] == [REFUSED_NO_CHANGE]

    def test_tightening_further_still_works(self, db):
        sys = system(db, autonomy="approval_required")
        policy(db, level="restricted")
        assert act(db, finding(db)).will_act
        db.refresh(sys)
        assert sys.autonomy_level == "restricted"


class TestBlastRadius:
    def test_inheriting_children_are_counted(self, db):
        # Autonomy resolves min-wins down the tree; an operator who arms a
        # policy on a team group without seeing that it covers eleven systems
        # will not trust this feature twice.
        parent = system(db, "parent", autonomy="autonomous")
        system(db, "child-a", parent="parent")
        system(db, "child-b", parent="parent")
        system(db, "grandchild", parent="child-a")

        assert inheriting_descendants(db, parent) == ["child-a", "child-b", "grandchild"]

    def test_a_non_inheriting_child_stops_the_walk(self, db):
        parent = system(db, "parent", autonomy="autonomous")
        system(db, "isolated", parent="parent", inherits=False)
        system(db, "below-isolated", parent="isolated")

        assert inheriting_descendants(db, parent) == []

    def test_the_plan_carries_the_radius(self, db):
        system(db, "parent", autonomy="autonomous")
        system(db, "child", parent="parent")
        policy(db, on="parent")
        plan = act(db, finding(db, on="parent"))

        assert plan.blast_radius.total == 2
        assert plan.blast_radius.is_wide

    def test_the_radius_is_recorded_on_the_audit_entry(self, db):
        system(db, "parent", autonomy="autonomous")
        system(db, "child", parent="parent")
        policy(db, on="parent")
        act(db, finding(db, on="parent"))

        radius = db.query(SystemOperationLog).one().payload["blast_radius"]
        assert radius["inherited_by"] == ["child"]

    def test_a_leaf_system_is_not_reported_as_wide(self, db):
        system(db, autonomy="autonomous")
        policy(db)
        plan = act(db, finding(db))
        assert not plan.blast_radius.is_wide

    def test_a_notify_only_policy_has_no_blast_radius(self, db):
        # Nothing is being restricted, so there is nothing to warn about.
        system(db, "parent", autonomy="autonomous")
        system(db, "child", parent="parent")
        policy(db, on="parent", level=None,
               actions=[{"kind": "notify", "targets": ["ops"]}])
        plan = act(db, finding(db, on="parent"))

        assert plan.will_act
        assert plan.blast_radius is None


class TestInheritance:
    def test_a_parents_policy_applies_to_a_child(self, db):
        # Otherwise an operator attaches the same policy to every system by
        # hand, and the ones they forget are the ones that drift.
        system(db, "parent")
        child = system(db, "child", parent="parent", autonomy="autonomous")
        policy(db, on="parent")

        assert [p.id for p in applicable_policies(db, child)] == ["p1"]

    def test_a_non_inheriting_child_does_not_get_it(self, db):
        system(db, "parent")
        child = system(db, "child", parent="parent", inherits=False, autonomy="autonomous")
        policy(db, on="parent")

        assert applicable_policies(db, child) == []

    def test_resolved_autonomy_is_min_wins_up_the_chain(self, db):
        system(db, "parent", autonomy="restricted")
        child = system(db, "child", parent="parent", autonomy="autonomous")
        assert resolved_autonomy(db, child) == "restricted"


class TestConsiderOnce:
    def test_a_finding_with_no_policy_is_marked_considered(self, db):
        # Without this it would be re-planned on every tick forever.
        system(db, autonomy="autonomous")
        event = finding(db)
        plan = act(db, event)

        assert [r.code for r in plan.refusals] == [REFUSED_NO_POLICY]
        db.refresh(event)
        assert event.response_taken is not None

    def test_a_considered_finding_is_not_picked_up_again(self, db):
        system(db, autonomy="autonomous")
        event = finding(db)
        act(db, event)
        assert [e.id for e in pending_drift_events(db)] == []

    def test_replanning_an_answered_finding_refuses(self, db):
        system(db, autonomy="autonomous")
        policy(db)
        event = finding(db)
        act(db, event)
        plan = plan_for_drift(db, event)

        assert not plan.will_act
        assert [r.code for r in plan.refusals] == [REFUSED_ALREADY_RESPONDED]

    def test_an_unanswered_finding_is_pending(self, db):
        system(db, autonomy="autonomous")
        finding(db)
        db.commit()
        assert [e.id for e in pending_drift_events(db)] == ["d1"]


class TestSuspendIsOptIn:
    def test_a_suspend_policy_without_opt_in_is_refused_at_write_time(self, db):
        # An operator should find this out when writing the policy, not
        # during an incident.
        from app.database.models.ai_system_response_policy import (
            PolicyValidationError,
            validate_actions,
        )

        with pytest.raises(PolicyValidationError):
            validate_actions(
                [{"kind": "set_autonomy", "level": "suspended"}], allow_suspend=False
            )

    def test_with_opt_in_it_fires(self, db):
        sys = system(db, autonomy="autonomous")
        policy(db, level="suspended", allow_suspend=True)
        assert act(db, finding(db)).will_act
        db.refresh(sys)
        assert sys.autonomy_level == "suspended"


class TestRestoreIsHuman:
    def test_restoring_requires_a_human_actor(self, db):
        # Auto-restore flaps: the signal clears *because* the system was
        # throttled, the throttle lifts, the signal returns.
        sys = system(db, autonomy="restricted")
        with pytest.raises(ValueError) as exc:
            restore(
                db, sys, actor_admin_id=SYSTEM_ACTOR_DRIFT_RESPONSE,
                actor_type=ACTOR_TYPE_SYSTEM, to_level="autonomous",
            )
        assert "flapping" in str(exc.value)

    def test_a_human_restore_is_recorded_separately(self, db):
        sys = system(db, autonomy="restricted")
        restore(
            db, sys, actor_admin_id="admin-1", actor_type=ACTOR_TYPE_TENANT_ADMIN,
            to_level="autonomous", drift_event_id="d1",
        )
        db.commit()

        row = db.query(SystemOperationLog).one()
        assert row.action == ACTION_DRIFT_RESPONSE_RESTORE
        assert row.actor_type == ACTOR_TYPE_TENANT_ADMIN
        assert row.before_state == {"autonomy_level": "restricted"}
        db.refresh(sys)
        assert sys.autonomy_level == "autonomous"


class TestPolicySelection:
    def test_the_most_restrictive_matching_policy_wins(self, db):
        # Taking the first match would make behaviour depend on row order;
        # taking the least restrictive would let a broad "notify on any
        # drift" policy shadow a narrow "restrict on cost drift" one.
        sys = system(db, autonomy="autonomous")
        policy(db, pid="p-mild", level="approval_required")
        policy(db, pid="p-strict", level="restricted")
        plan = act(db, finding(db))

        assert plan.policy_id == "p-strict"
        db.refresh(sys)
        assert sys.autonomy_level == "restricted"

    def test_a_severity_below_the_floor_does_not_fire(self, db):
        system(db, autonomy="autonomous")
        policy(db)
        plan = act(db, finding(db, severity="medium"))
        assert [r.code for r in plan.refusals] == [REFUSED_NO_POLICY]

    def test_a_drift_class_filter_is_respected(self, db):
        system(db, autonomy="autonomous")
        policy(db, drift_class="cost")
        plan = act(db, finding(db, drift_class="behavioural"))
        assert not plan.will_act


class TestActorVocabulary:
    def test_system_is_a_valid_actor_type(self, db):
        assert ACTOR_TYPE_SYSTEM in VALID_ACTOR_TYPES

    def test_a_system_row_is_accepted_on_the_fresh_install_path(self, db):
        # The a134_baselines_drift lesson: the model's CHECK is what `create_all` builds, and
        # a hard-coded list there is how fresh installs end up rejecting
        # exactly the rows a new feature writes.
        db.add(
            SystemOperationLog(
                id="sol-x", tenant_id="t1",
                actor_admin_id=SYSTEM_ACTOR_DRIFT_RESPONSE,
                actor_type=ACTOR_TYPE_SYSTEM, action=ACTION_DRIFT_RESPONSE,
            )
        )
        db.commit()
        assert db.query(SystemOperationLog).count() == 1


# ---------------------------------------------------------------------------
# What an action does not cover
# ---------------------------------------------------------------------------


def plain_key(db, kid="k1", group="sys", agent=None):
    from app.database.models.api_key import APIKey

    db.add(APIKey(
        id=kid, tenant_id="t1", key_hash=f"h-{kid}", key_prefix="brt_", name=kid,
        scope="global", group_id=group, agent_id=agent, enabled=True, revoked=False,
    ))
    db.flush()


def recent_run(db, rid, actor_type, on="sys"):
    from app.database.models.ai_system_run import AISystemRun

    now = datetime.now(timezone.utc)
    db.add(AISystemRun(
        id=rid, tenant_id="t1", root_task_id=rid, ai_system_id=on,
        started_at=now, ended_at=now, terminal_state="completed",
        finalized_by="idle", actor_type=actor_type,
    ))
    db.flush()


class TestWhatAnActionDoesNotCover:
    def test_approval_required_says_which_callers_it_does_not_hold(self, db):
        # The policy fired and recorded the action; a plain key kept posting
        # data out. The record must say so.
        system(db, autonomy="autonomous")
        policy(db, level="approval_required")
        plain_key(db)
        recent_run(db, "r1", "api_key")
        recent_run(db, "r2", "agent_identity")
        event = finding(db)
        act(db, event)

        note = event.response_taken["limitations"][0]
        assert note["code"] == "approval_required_skips_non_agent_callers"
        assert note["unbound_active_keys"] == 1
        assert (note["unbound_recent_runs"], note["recent_runs"]) == (1, 2)
        assert "their calls continue" in note["detail"]
        log = db.query(SystemOperationLog).one()
        assert log.payload["limitations"] == [note]

    def test_no_note_when_every_caller_is_an_agent(self, db):
        system(db, autonomy="autonomous")
        policy(db, level="approval_required")
        recent_run(db, "r1", "agent_identity")
        event = finding(db)
        act(db, event)
        assert event.response_taken["limitations"] == []

    def test_restricted_binds_everyone_and_needs_no_note(self, db):
        system(db, autonomy="autonomous")
        policy(db, level="restricted")
        plain_key(db)
        event = finding(db)
        act(db, event)
        assert event.response_taken["limitations"] == []

