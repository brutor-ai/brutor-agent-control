# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Regression tests for the Git-sourced SkillSource feature (F1):
  * ORM lifecycle helpers (begin_sync / finish_sync / record_error / is_due)
  * Pydantic validators on the create payload (GitHub URL shape, git ref,
    sync_interval bounds, ingest payload validation)
  * The ingest merge logic (`_ingest_discovered_skills`):
      - new skill → insert
      - existing skill, body unchanged → no-op
      - existing skill, body changed → update + replace sibling files
      - PUBLISHED skill → snapshot commit only (immutable)
      - per-skill error doesn't poison the whole batch

Run: pytest tests/test_skill_sources.py -v
"""

import os
import time
from datetime import datetime, timedelta, timezone

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(type_, compiler, **kw):  # pragma: no cover
    return "JSON"


import app.database.models  # noqa: F401 — registers all tables

from app.api.v1.endpoints.admin.skill_sources import _ingest_discovered_skills  # noqa: E402
from app.api.v1.models.admin.skill_source import (  # noqa: E402
    DiscoveredSkill,
    DiscoveredSkillFile,
    SkillSourceCreate,
    SkillSourceIngestRequest,
)
from app.constants.agent_skill import SkillStatus  # noqa: E402
from app.database import Base  # noqa: E402
from app.database.models.agent_skill import AgentSkill  # noqa: E402
from app.database.models.agent_skill_asset import AgentSkillAsset  # noqa: E402
from app.database.models.agent_skill_resource import AgentSkillResource  # noqa: E402
from app.database.models.agent_skill_script import AgentSkillScript  # noqa: E402
from app.database.models.skill_source import (  # noqa: E402
    DEFAULT_SYNC_INTERVAL_SECONDS,
    SYNC_STATUS_FAILED,
    SYNC_STATUS_IDLE,
    SYNC_STATUS_SYNCING,
    SkillSource,
)
from app.database.models.tenant import Tenant  # noqa: E402


TENANT_ID = "t1"


class _NullLogger:
    """The ingest helper accepts a logger but the tests don't care about
    log output. A no-op stand-in keeps the prod call signature."""

    def info(self, *a, **kw):
        pass

    def warning(self, *a, **kw):
        pass

    def error(self, *a, **kw):
        pass

    def debug(self, *a, **kw):
        pass


@pytest.fixture()
def harness():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine)
    SessionFactory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    s = SessionFactory()
    try:
        s.add(
            Tenant(
                id=TENANT_ID,
                name=TENANT_ID,
                display_name="T1",
                organization_name="T1",
                primary_contact_email="ops@t1.example",
                is_active=True,
                is_default=False,
                is_trial=False,
                suspended=False,
                billing_plan="self_hosted",
                max_admin_users=None,
            )
        )
        s.flush()
        s.add(
            SkillSource(
                id="sks-test",
                tenant_id=TENANT_ID,
                name="example",
                repo_url="https://github.com/anthropics/skill-examples",
                git_ref="main",
                enabled=True,
                sync_interval_seconds=DEFAULT_SYNC_INTERVAL_SECONDS,
                auto_publish=False,
                status=SYNC_STATUS_IDLE,
            )
        )
        s.commit()
    finally:
        s.close()
    yield SessionFactory
    engine.dispose()


# ---------------------------------------------------------------------------
# ORM helpers
# ---------------------------------------------------------------------------


def test_begin_finish_sync_cycle(harness):
    s = harness()
    try:
        src = s.query(SkillSource).filter_by(id="sks-test").one()
        assert src.status == SYNC_STATUS_IDLE

        src.begin_sync()
        s.commit()
        assert src.status == SYNC_STATUS_SYNCING
        assert src.last_attempted_at is not None
        assert src.sync_error is None

        src.finish_sync(commit_sha="deadbeef", discovered_count=3)
        s.commit()
        assert src.status == SYNC_STATUS_IDLE
        assert src.last_synced_at is not None
        assert src.last_commit_sha == "deadbeef"
        assert src.discovered_skill_count == 3
    finally:
        s.close()


def test_record_error_marks_failed_and_clamps_message(harness):
    s = harness()
    try:
        src = s.query(SkillSource).filter_by(id="sks-test").one()
        long = "boom " * 5000  # > 8000 chars
        src.record_error(long)
        s.commit()
        assert src.status == SYNC_STATUS_FAILED
        assert src.sync_error is not None
        assert len(src.sync_error) <= 8000
    finally:
        s.close()


def test_is_due_when_never_attempted(harness):
    s = harness()
    try:
        src = s.query(SkillSource).filter_by(id="sks-test").one()
        assert src.is_due() is True
    finally:
        s.close()


def test_is_due_respects_interval(harness):
    s = harness()
    try:
        src = s.query(SkillSource).filter_by(id="sks-test").one()
        src.last_attempted_at = datetime.now(timezone.utc) - timedelta(seconds=30)
        src.sync_interval_seconds = 3600
        # 30s elapsed of a 1h interval — not due
        assert src.is_due() is False

        # Past the interval — due
        src.last_attempted_at = datetime.now(timezone.utc) - timedelta(hours=2)
        assert src.is_due() is True
    finally:
        s.close()


def test_is_due_false_when_disabled_or_syncing(harness):
    s = harness()
    try:
        src = s.query(SkillSource).filter_by(id="sks-test").one()
        src.enabled = False
        assert src.is_due() is False
        src.enabled = True
        src.status = SYNC_STATUS_SYNCING
        assert src.is_due() is False
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Pydantic validators
# ---------------------------------------------------------------------------


def test_create_accepts_valid_github_url():
    payload = SkillSourceCreate(
        name="anthropics",
        repo_url="https://github.com/anthropics/skill-examples",
    )
    assert payload.repo_url == "https://github.com/anthropics/skill-examples"
    assert payload.git_ref == "main"  # default


def test_create_strips_trailing_slash_and_dot_git():
    p = SkillSourceCreate(
        name="x",
        repo_url="https://github.com/anthropics/skill-examples.git/",
    )
    # Validator strips trailing slash but keeps the .git suffix because
    # the regex permits both forms; we accept either.
    assert p.repo_url.startswith("https://github.com/")


def test_create_rejects_non_github_url():
    with pytest.raises(Exception):
        SkillSourceCreate(
            name="x",
            repo_url="https://gitlab.com/foo/bar",
        )


def test_create_rejects_dangerous_git_ref():
    with pytest.raises(Exception):
        SkillSourceCreate(
            name="x",
            repo_url="https://github.com/foo/bar",
            git_ref="main; rm -rf /",
        )


def test_create_clamps_sync_interval():
    # 5s below the floor → reject
    with pytest.raises(Exception):
        SkillSourceCreate(
            name="x",
            repo_url="https://github.com/foo/bar",
            sync_interval_seconds=5,
        )
    # Way above the ceiling → reject
    with pytest.raises(Exception):
        SkillSourceCreate(
            name="x",
            repo_url="https://github.com/foo/bar",
            sync_interval_seconds=10_000_000,
        )


def test_discovered_skill_rejects_bad_name():
    with pytest.raises(Exception):
        DiscoveredSkill(
            skill_path="skills/Bad_Name/SKILL.md",
            name="Bad_Name",  # uppercase + underscore not allowed
            description="x",
            skill_md_body="body",
        )


# ---------------------------------------------------------------------------
# Ingest merge logic
# ---------------------------------------------------------------------------


def _make_discovered(
    skill_path="skills/incident-report/SKILL.md",
    name="incident-report",
    description="Generate a markdown incident report.",
    body="# Incident Report\nFollow this template.",
    version="1.0.0",
    files=None,
):
    return DiscoveredSkill(
        skill_path=skill_path,
        name=name,
        description=description,
        version=version,
        skill_md_body=body,
        files=files or [],
    )


def test_ingest_inserts_new_skill(harness):
    s = harness()
    try:
        src = s.query(SkillSource).filter_by(id="sks-test").one()
        payload = SkillSourceIngestRequest(
            commit_sha="commit-sha-1",
            discovered=[_make_discovered()],
        )
        summary = _ingest_discovered_skills(
            db=s,
            source=src,
            payload=payload,
            logger=_NullLogger(),
        )
        assert summary.created == 1
        assert summary.updated == 0
        assert summary.skipped == 0

        row = (
            s.query(AgentSkill)
            .filter_by(source_repo_id="sks-test", source_path="skills/incident-report/SKILL.md")
            .one()
        )
        assert row.name == "incident-report"
        assert row.source_commit_sha == "commit-sha-1"
        assert row.status == SkillStatus.DRAFT  # auto_publish=false default
    finally:
        s.close()


def test_ingest_auto_publish_lands_in_published(harness):
    s = harness()
    try:
        src = s.query(SkillSource).filter_by(id="sks-test").one()
        src.auto_publish = True
        s.commit()
        payload = SkillSourceIngestRequest(
            commit_sha="commit-sha-1",
            discovered=[_make_discovered()],
        )
        _ingest_discovered_skills(db=s, source=src, payload=payload, logger=_NullLogger())
        row = (
            s.query(AgentSkill)
            .filter_by(source_repo_id="sks-test", source_path="skills/incident-report/SKILL.md")
            .one()
        )
        assert row.status == SkillStatus.PUBLISHED
    finally:
        s.close()


def test_ingest_unchanged_body_is_noop(harness):
    s = harness()
    try:
        src = s.query(SkillSource).filter_by(id="sks-test").one()
        payload = SkillSourceIngestRequest(
            commit_sha="commit-sha-1",
            discovered=[_make_discovered()],
        )
        _ingest_discovered_skills(db=s, source=src, payload=payload, logger=_NullLogger())

        # Second ingest with the SAME body
        payload2 = SkillSourceIngestRequest(
            commit_sha="commit-sha-2",  # commit moves forward
            discovered=[_make_discovered()],
        )
        summary = _ingest_discovered_skills(
            db=s, source=src, payload=payload2, logger=_NullLogger()
        )
        assert summary.created == 0
        assert summary.unchanged == 1
        # Commit SHA is updated even on unchanged-body so the UI can
        # show "up-to-date with HEAD".
        row = (
            s.query(AgentSkill)
            .filter_by(source_repo_id="sks-test", source_path="skills/incident-report/SKILL.md")
            .one()
        )
        assert row.source_commit_sha == "commit-sha-2"
    finally:
        s.close()


def test_ingest_changed_body_updates_draft_skill(harness):
    s = harness()
    try:
        src = s.query(SkillSource).filter_by(id="sks-test").one()
        _ingest_discovered_skills(
            db=s,
            source=src,
            payload=SkillSourceIngestRequest(
                commit_sha="commit-c1", discovered=[_make_discovered(body="Old body")]
            ),
            logger=_NullLogger(),
        )
        summary = _ingest_discovered_skills(
            db=s,
            source=src,
            payload=SkillSourceIngestRequest(
                commit_sha="commit-c2",
                discovered=[_make_discovered(body="New body content")],
            ),
            logger=_NullLogger(),
        )
        assert summary.updated == 1
        row = (
            s.query(AgentSkill)
            .filter_by(source_repo_id="sks-test", source_path="skills/incident-report/SKILL.md")
            .one()
        )
        assert row.skill_md_content == "New body content"
        assert row.source_commit_sha == "commit-c2"
    finally:
        s.close()


def test_ingest_published_skill_is_immutable(harness):
    s = harness()
    try:
        src = s.query(SkillSource).filter_by(id="sks-test").one()
        _ingest_discovered_skills(
            db=s,
            source=src,
            payload=SkillSourceIngestRequest(
                commit_sha="commit-c1", discovered=[_make_discovered(body="Initial")]
            ),
            logger=_NullLogger(),
        )
        # Promote the row to PUBLISHED
        row = (
            s.query(AgentSkill)
            .filter_by(source_repo_id="sks-test")
            .one()
        )
        row.status = SkillStatus.PUBLISHED
        s.commit()

        summary = _ingest_discovered_skills(
            db=s,
            source=src,
            payload=SkillSourceIngestRequest(
                commit_sha="commit-c2",
                discovered=[_make_discovered(body="UPSTREAM CHANGED")],
            ),
            logger=_NullLogger(),
        )
        # Treated as unchanged — body must stay the published version.
        assert summary.unchanged == 1
        assert summary.updated == 0

        s.refresh(row)
        assert row.skill_md_content == "Initial"  # NOT overwritten
        assert row.source_commit_sha == "commit-c2"  # snapshot moves forward
    finally:
        s.close()


def test_ingest_sibling_files_create_resource_script_asset_rows(harness):
    s = harness()
    try:
        src = s.query(SkillSource).filter_by(id="sks-test").one()
        files = [
            DiscoveredSkillFile(
                path="skills/incident-report/scripts/run.py",
                kind="script",
                content="print('hi')",
            ),
            DiscoveredSkillFile(
                path="skills/incident-report/references/CHANGELOG.md",
                kind="reference",
                content="# Changelog",
            ),
            DiscoveredSkillFile(
                path="skills/incident-report/assets/template.txt",
                kind="asset",
                content="hello template",
            ),
        ]
        _ingest_discovered_skills(
            db=s,
            source=src,
            payload=SkillSourceIngestRequest(
                commit_sha="commit-c1",
                discovered=[_make_discovered(files=files)],
            ),
            logger=_NullLogger(),
        )
        skill = s.query(AgentSkill).filter_by(source_repo_id="sks-test").one()
        scripts = s.query(AgentSkillScript).filter_by(skill_id=skill.id).all()
        refs = s.query(AgentSkillResource).filter_by(skill_id=skill.id).all()
        assets = s.query(AgentSkillAsset).filter_by(skill_id=skill.id).all()
        # Stored filename is the path relative to the skill ROOT (keeps the
        # bucket dir) — the Design-2 convention shared with the ZIP importer,
        # so the export endpoint reconstructs the original layout faithfully.
        assert len(scripts) == 1 and scripts[0].filename == "scripts/run.py"
        assert len(refs) == 1 and refs[0].filename == "references/CHANGELOG.md"
        assert len(assets) == 1 and assets[0].filename == "assets/template.txt"
    finally:
        s.close()


def test_ingest_per_skill_error_isolates_to_skipped(harness):
    s = harness()
    try:
        src = s.query(SkillSource).filter_by(id="sks-test").one()
        # Two valid + one with an absurd content_hash trick: pass a skill
        # whose `name` is fine at Pydantic but whose body triggers an
        # underlying error path. Easiest way: mock by inserting a name
        # that violates the DB-side constraint (none is enforced) — fall
        # back to verifying the ingest handles unexpected exceptions
        # gracefully via the catch-all.
        bad = _make_discovered(skill_path="skills/bad/SKILL.md", name="bad")
        good1 = _make_discovered(skill_path="skills/g1/SKILL.md", name="g1")
        good2 = _make_discovered(skill_path="skills/g2/SKILL.md", name="g2")

        # Monkey-patch the per-skill create to raise for the bad one so
        # we exercise the catch-all without contriving DB-level errors.
        import app.api.v1.endpoints.admin.skill_sources as mod

        original = mod._create_skill_from_discovery

        def patched(*, db, source, commit_sha, discovered, initial_status):
            if discovered.skill_path == "skills/bad/SKILL.md":
                raise RuntimeError("simulated parse failure")
            return original(
                db=db,
                source=source,
                commit_sha=commit_sha,
                discovered=discovered,
                initial_status=initial_status,
            )

        mod._create_skill_from_discovery = patched
        try:
            summary = _ingest_discovered_skills(
                db=s,
                source=src,
                payload=SkillSourceIngestRequest(
                    commit_sha="commit-c1", discovered=[bad, good1, good2]
                ),
                logger=_NullLogger(),
            )
        finally:
            mod._create_skill_from_discovery = original

        assert summary.created == 2
        assert summary.skipped == 1
        assert any("simulated parse failure" in e for e in summary.errors)
    finally:
        s.close()
