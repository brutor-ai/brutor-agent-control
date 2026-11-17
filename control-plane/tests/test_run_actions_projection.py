# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# A run's actions — THE projection (`app/services/run_actions.py`)

`GET /v1/admin/runs/{id}` is the one place a run's actions are numbered,
classified, targeted and attributed. The Runs timeline, Run details and the
Composition tab's run overlay all read it, so these tests pin the facts over
HTTP (Pydantic strips undeclared fields — a projection field the response
model does not declare never reaches the console):

  * numbering in the gateway's order, a delegate's steps nested under the A2A
    call that entered them (5a, 5b) even though they were logged BEFORE it;
  * outcome = the connector lines' classification (a parity test against
    `own_traffic`, the class guard: the two cannot drift apart);
  * target = the connector mapping; system = the own-rows rule;
  * metadata only: no body, argument or prompt reaches the payload, and a
    skill's script is named only when the skill's catalog registers it.
"""

from __future__ import annotations

import json
import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from datetime import datetime, timedelta, timezone

import pytest

from app.api.v1.endpoints.admin.ai_system_runs import create_ai_system_runs_router
from app.core.logging_system import BrutorLogger
from app.database.models.agent_card import AgentCard
from app.database.models.agent_skill import AgentSkill
from app.database.models.agent_skill_script import AgentSkillScript
from app.database.models.ai_system_dependency import AISystemDependency
from app.database.models.ai_system_run import AISystemRun
from app.database.models.llm_model import LLModel
from app.database.models.llm_provider import LlmProvider
from app.database.models.mcp_server import MCPServer
from app.database.models.proxy_log import ProxyLog
from app.database.models.resource_group import GroupType, ResourceGroup
from app.services import run_actions
from app.services.run_actions import number_actions, primary_target, sub_number
from tests.admin_endpoint_harness import build_admin_harness

TENANT = "t1"
SYS = "sys-root"
SVC = "sys-fraud"
RUN = "run-overlay"
ROOT = "root-overlay"
T0 = datetime(2026, 9, 29, 8, 0, 0, tzinfo=timezone.utc)
#: Planted in bodies and error text: must never reach the payload.
SECRET = "SENTINEL-applicant-income-4711"


def _group(gid: str, name: str) -> ResourceGroup:
    return ResourceGroup(
        id=gid, tenant_id=TENANT, name=name, description="d", hierarchy_level=0,
        inherit_resources=True, is_active=True, is_system=False,
        group_type=GroupType.AI_SYSTEM,
    )


def _log(i: int, seconds: float, **kw) -> ProxyLog:
    base = dict(
        id=f"log-{i:02d}", tenant_id=TENANT, timestamp=T0 + timedelta(seconds=seconds),
        root_task_id=ROOT, end_user_session_id="s", http_method="POST", endpoint="/",
        success=True, http_status_code=200, duration_ms=10 * i, resource_group_id=SYS,
        current_task_id=f"task-{i:02d}",
    )
    base.update(kw)
    return ProxyLog(**base)


# BRUTOR_PG_TESTS=1 runs this module on a throwaway Postgres database too
# (the projection's window function and CASE predicates on the real engine).
from tests.test_ai_system_runs_routes_smoke import database_url  # noqa: E402,F401


@pytest.fixture(scope="module")
def harness(database_url):  # noqa: F811
    h = build_admin_harness([create_ai_system_runs_router(BrutorLogger("run-actions"))],
                            logger_name="run-actions", database_url=database_url)

    def seed(s):
        s.add(_group(SYS, "Brutor Demo System"))
        s.add(_group(SVC, "Fraud Screener"))
        s.add(LlmProvider(id="prov-1", tenant_id=TENANT, key="openai", display_name="OpenAI"))
        s.flush()
        s.add(LLModel(id="llm-gpt", tenant_id=TENANT, provider_id="prov-1", name="GPT-5.2",
                      model_name="gpt-5.2"))
        s.add(MCPServer(id="mcp-apps", tenant_id=TENANT, name="Applications",
                        base_url="https://apps.example"))
        s.add(MCPServer(id="mcp-bureau", tenant_id=TENANT, name="Credit Bureau",
                        base_url="https://bureau.example"))
        s.add(AgentSkill(id="skill-aff", tenant_id=TENANT, name="affordability-check",
                         description="d", skill_md_content="# x", skill_md_hash="h"))
        s.add(AgentCard(id="card-fraud", tenant_id=TENANT, name="fraud-screener",
                        card_json={"name": "f", "capabilities": []}))
        s.flush()
        s.add(AgentSkillScript(
            id="ass-aff", tenant_id=TENANT, skill_id="skill-aff", filename="affordability.py",
            script_content="print(1)", script_hash="h", language="python",
            execution_mode="sandbox",
        ))
        s.add(AISystemDependency(
            id="dep-fraud", tenant_id=TENANT, ai_system_id=SYS, role="shared_service",
            depends_on_system_id=SVC, agent_card_id="card-fraud", criticality="critical",
            contributes_to_decision=True, assumptions={}, declared_by="admin",
        ))
        s.add(AISystemRun(
            id=RUN, tenant_id=TENANT, root_task_id=ROOT, ai_system_id=SYS,
            started_at=T0, ended_at=T0 + timedelta(seconds=30), terminal_state="completed",
            finalized_by="explicit", is_replay=False, action_count=9, subject_id="APP-1001",
            subject_kind="application", agent_impl_name="loan-agent", agent_impl_version="1.4.2",
            client_label="loan-portal",
        ))
        s.add(AISystemRun(
            id="run-other", tenant_id=TENANT, root_task_id="root-other", ai_system_id=SYS,
            started_at=T0 - timedelta(minutes=5), terminal_state="errored",
            finalized_by="idle", is_replay=False, action_count=1, client_label="batch_100%",
        ))
        s.flush()
        s.add_all([
            # 1 — a tool call, answered.
            _log(1, 0, proxy_type="mcp", proxy_subtype="json_rpc", server_id="mcp-apps",
                 mcp_capability_name="applications_get", step_id="s1",
                 step_name="Gather the application facts", turn_id="t1", turn_seq=1,
                 request_body_preview=json.dumps({"params": {"arguments": {"id": SECRET}}})),
            # 2 — held for a human approval decision.
            _log(2, 1, proxy_type="mcp", proxy_subtype="approval_required", success=False,
                 http_status_code=403, server_id="mcp-bureau",
                 mcp_capability_name="bureau_get_report", step_id="s1",
                 step_name="Gather the application facts", turn_id="t1", turn_seq=1),
            # 3 — a skill run through the Agent Skills server; the script is
            # read from the body only because the catalog registers it.
            _log(3, 2, proxy_type="mcp", proxy_subtype="skill_run",
                 server_id="system-agent-skill-server-default", skill_id="skill-aff",
                 mcp_capability_name="skills__run_script", step_id="s1",
                 step_name="Gather the application facts", turn_id="t1", turn_seq=1,
                 request_body_preview=json.dumps({"params": {"name": "skills__run_script",
                     "arguments": {"skill_name": "affordability-check",
                                   "script": "affordability.py",
                                   "args": {"income": SECRET}}}})),
            # 4 — a model call that retrieved from a knowledge base.
            _log(4, 3, proxy_type="llm", proxy_subtype="chat_completions", server_id="llm-gpt",
                 llm_model_used="gpt-5.2", rag_collection_ids="kb-policy", step_id="s2",
                 step_name="Assess risk and fraud", turn_id="t2", turn_seq=2,
                 request_body_preview=SECRET, response_body_preview=SECRET),
            # The delegate's own steps — logged BEFORE the A2A call that
            # entered them (the call is logged when it returns).
            _log(6, 4, proxy_type="llm", proxy_subtype="chat_completions", server_id="llm-gpt",
                 llm_model_used="gpt-5.2", resource_group_id=SVC, delegation_depth=1,
                 parent_task_id="task-a2a", current_task_id="task-06"),
            _log(7, 5, proxy_type="mcp", proxy_subtype="json_rpc", server_id="mcp-bureau",
                 mcp_capability_name="sanctions_check", resource_group_id=SVC,
                 delegation_depth=1, parent_task_id="task-a2a", guardrail_blocked=True,
                 success=False, http_status_code=403, error_message=SECRET),
            # 5 — the A2A call into the fraud screener.
            _log(5, 6, proxy_type="a2a", proxy_subtype="outbound", server_id="card-fraud",
                 mcp_capability_name="screening.fraud_sanctions", current_task_id="task-a2a",
                 a2a_task_state="completed", step_id="s2", step_name="Assess risk and fraud",
                 turn_id="t2", turn_seq=2),
            # 6 — a model call that failed.
            _log(8, 7, proxy_type="llm", proxy_subtype="responses", server_id="llm-gpt",
                 llm_model_used="gpt-5.2", success=False, http_status_code=502,
                 step_id="s3", step_name="Decide and record", turn_id="t3", turn_seq=3,
                 error_message=SECRET),
            # 7 — refused by a limit (429 = blocked).
            _log(9, 8, proxy_type="mcp", proxy_subtype="json_rpc", server_id="mcp-apps",
                 mcp_capability_name="applications_set_recommendation", success=False,
                 http_status_code=429, step_id="s3", step_name="Decide and record",
                 turn_id="t3", turn_seq=3),
        ])

    h.seed(seed)
    yield h
    h.close()


@pytest.fixture(scope="module")
def actions(harness):
    r = harness.client.get(f"/v1/admin/runs/{RUN}", headers=harness.headers(TENANT))
    assert r.status_code == 200, r.text
    return r.json()["actions"]


def by_number(actions):
    return {a["number"]: a for a in actions}


def test_numbers_follow_the_gateway_order_with_delegations_nested(actions):
    # The array stays in the gateway's order (timestamp, id) …
    assert [a["id"] for a in actions] == [
        "log-01", "log-02", "log-03", "log-04", "log-06", "log-07", "log-05", "log-08", "log-09"]
    # … and every action carries its number: the delegate's two steps are
    # 5a / 5b under the A2A call (5) that entered them.
    assert [a["number"] for a in actions] == ["1", "2", "3", "4", "5a", "5b", "5", "6", "7"]
    n = by_number(actions)
    assert (n["5a"]["entered_by"], n["5b"]["entered_by"], n["5"]["entered_by"]) == ("5", "5", None)
    assert (n["5a"]["nesting"], n["5"]["nesting"]) == (1, 0)


def test_outcomes_answered_error_blocked_held(actions):
    n = by_number(actions)
    assert {k: n[k]["outcome"] for k in n} == {
        "1": "answered", "2": "held", "3": "answered", "4": "answered", "5": "answered",
        "5a": "answered", "5b": "blocked", "6": "error", "7": "blocked",
    }


def test_targets_names_and_skill_script(actions):
    n = by_number(actions)
    assert (n["1"]["target_kind"], n["1"]["target_id"], n["1"]["target_name"]) == (
        "mcp", "mcp-apps", "Applications")
    # A skill run is the SKILL (the Agent Skills server is plumbing), with
    # the catalog's script filename.
    assert (n["3"]["target_kind"], n["3"]["target_id"], n["3"]["target_name"],
            n["3"]["skill_script"]) == ("skill", "skill-aff", "affordability-check",
                                        "affordability.py")
    assert (n["4"]["target_kind"], n["4"]["target_name"], n["4"]["kb_ids"]) == (
        "model", "GPT-5.2", ["kb-policy"])
    assert (n["5"]["target_kind"], n["5"]["target_id"], n["5"]["target_name"]) == (
        "a2a", "card-fraud", "fraud-screener")
    assert n["1"]["capability"] == "applications_get"


def test_system_attribution_is_the_own_rows_rule(actions):
    n = by_number(actions)
    assert all(n[k]["own"] and n[k]["system_id"] == SYS for k in ("1", "2", "3", "4", "5", "6"))
    for k in ("5a", "5b"):
        assert (n[k]["own"], n[k]["system_id"], n[k]["system_name"]) == (
            False, SVC, "Fraud Screener")
    assert (n["5"]["delegate_system_id"], n["5"]["delegate_system_name"]) == (
        SVC, "Fraud Screener")


def test_turns_asserted_and_inferred_travel_together(actions):
    n = by_number(actions)
    assert (n["1"]["turn_seq"], n["1"]["step_name"]) == (1, "Gather the application facts")
    # THE inference: a non-embedding model call opens a turn (running count).
    assert [a["inferred_turn"] for a in actions] == [0, 0, 0, 1, 2, 2, 2, 3, 3]


def test_metadata_only(actions):
    """No body, argument or prompt reaches the payload — the planted
    sentinel appears nowhere except the (capped) error text the Runs
    timeline has always carried, and no body-shaped key exists."""
    for a in actions:
        for key in ("request_body_preview", "response_body_preview", "request_headers",
                    "response_headers", "arguments", "prompt", "current_task_id",
                    "parent_task_id"):
            assert key not in a
        rest = {k: v for k, v in a.items() if k != "error_message"}
        assert SECRET not in json.dumps(rest)


def test_a_script_the_catalog_does_not_register_is_never_emitted(harness):
    """Argument content never leaks through `skill_script`: an unregistered
    value in the body is dropped, not echoed."""
    def seed(s):
        # On the OTHER system, so the parity test's window over SYS is exactly RUN.
        s.add(AISystemRun(id="run-script", tenant_id=TENANT, root_task_id="root-script",
                          ai_system_id=SVC, started_at=T0, terminal_state="completed",
                          finalized_by="explicit", is_replay=False, action_count=1))
        s.add(_log(40, 0, root_task_id="root-script", proxy_type="mcp", resource_group_id=SVC,
                   proxy_subtype="skill_run", skill_id="skill-aff",
                   server_id="system-agent-skill-server-default",
                   mcp_capability_name="skills__run_script",
                   request_body_preview=json.dumps({"params": {"arguments": {
                       "script": SECRET}}})))
    harness.seed(seed)
    r = harness.client.get("/v1/admin/runs/run-script", headers=harness.headers(TENANT))
    (a,) = r.json()["actions"]
    assert a["skill_script"] is None
    assert SECRET not in r.text


def test_outcome_is_the_connector_lines_classification(harness, actions):
    """The class guard: for every target, the projection's blocked / held /
    error counts equal what the Composition tab's connector lines count
    (`own_traffic`) over the same rows. A predicate added to one and not
    the other fails here."""
    from app.services.ai_system_connection_status import own_traffic

    db = harness.Factory()
    try:
        traffic, _ = own_traffic(db, TENANT, SYS, T0 - timedelta(days=1))
    finally:
        db.close()
    own = [a for a in actions if a["own"]]
    for (kind, key), ev in traffic.items():
        mine = [a for a in own if (kind == "kb" and key in a["kb_ids"])
                or (a["target_kind"] == kind and a["target_id"] == key)
                or (kind == "mcp" and a["proxy_type"] == "mcp" and key == "system-agent-skill-server-default"
                    and a["target_kind"] == "skill")]
        blocked = sum(a["outcome"] == "blocked" for a in mine)
        held = sum(a["outcome"] == "held" for a in mine)
        errors = sum(a["outcome"] == "error" for a in mine)
        assert (len(mine), blocked, held, errors) == (
            ev.calls, ev.blocked, ev.held, ev.errors), (kind, key)


def test_run_list_search_finds_a_run(harness):
    def ids(q):
        r = harness.client.get(f"/v1/admin/ai-systems/{SYS}/runs",
                               params={"period_hours": 0, "q": q},
                               headers=harness.headers(TENANT))
        assert r.status_code == 200, r.text
        return [x["id"] for x in r.json()["runs"]]

    assert set(ids("")) >= {RUN, "run-other"}
    assert ids("app-1001") == [RUN]           # subject, case-insensitive
    assert ids("LOAN-agent") == [RUN]         # release name
    assert ids("1.4.2") == [RUN]              # release version
    assert ids("overlay") == [RUN]            # run id / root task id substring
    assert ids("100%") == ["run-other"]       # % is a character, not a wildcard
    assert ids("_") == ["run-other"]          # so is _
    assert ids("no-such-run") == []


# ---------------------------------------------------------------------------
# The numbering, as a pure function
# ---------------------------------------------------------------------------


def test_sub_numbers():
    assert [sub_number("5", i) for i in (1, 2, 26, 27)] == ["5a", "5b", "5z", "5aa"]
    assert sub_number("5a", 1) == "5a.1" and sub_number("5a.1", 2) == "5a.1.2"


def test_a_delegation_inside_a_delegation_numbers_one_level_deeper():
    rows = [
        {"id": "x", "proxy_type": "llm", "current_task_id": "c0", "parent_task_id": None},
        {"id": "c-1", "proxy_type": "llm", "current_task_id": "c1", "parent_task_id": "tb"},
        {"id": "b-call", "proxy_type": "a2a", "current_task_id": "tb", "parent_task_id": "ta"},
        {"id": "a-call", "proxy_type": "a2a", "current_task_id": "ta", "parent_task_id": None},
    ]
    number_actions(rows)
    assert {r["id"]: r["number"] for r in rows} == {
        "x": "1", "a-call": "2", "b-call": "2a", "c-1": "2a.1"}


def test_a_task_link_that_loops_is_broken_not_followed():
    """A loop is not a delegation chain: the looping link is dropped (that
    row goes top-level) and every row still gets one unique number."""
    rows = [
        {"id": "a", "proxy_type": "a2a", "current_task_id": "ta", "parent_task_id": "tb"},
        {"id": "b", "proxy_type": "a2a", "current_task_id": "tb", "parent_task_id": "ta"},
    ]
    number_actions(rows)
    numbers = [r["number"] for r in rows]
    assert len(set(numbers)) == 2 and "1" in numbers


def test_primary_target_prefers_the_skill_and_lists_kbs():
    assert primary_target([("mcp", "sys-srv"), ("skill", "sk")]) == ("skill", "sk", [])
    assert primary_target([("model", "m"), ("kb", "k1"), ("kb", "k2")]) == ("model", "m", ["k1", "k2"])
    assert primary_target([]) == (None, None, [])


def test_the_turn_rule_is_the_trajectory_rule():
    """One string, two readers: the trajectory rollup's SQL embeds it."""
    from app.services import trajectory_search

    assert run_actions.OPENS_TURN_SQL is trajectory_search.OPENS_TURN_SQL
    assert trajectory_search.OPENS_TURN_SQL in str(trajectory_search._STEP_TURNS_SQL)
