# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The per-skill limit (`SKILL_MAX_BYTES`, 30 MB) holds on every path that
writes a skill's files, not only on zip import: the resource, script and
asset routes (create and update), a change of SKILL.md, the skill-source
ingest, and the GitHub sync before it downloads anything. A write that would
make the skill's files larger than the limit is refused with the reason
`too_large` and the same words as the zip import ("more than the per-skill
limit (30 MB)"); nothing of it is stored.

A skill's size is the sum of its files as they unpack: SKILL.md, every
resource and script, every asset (a base64 asset counts its decoded bytes).
"""

from __future__ import annotations

import asyncio
import base64

import pytest

from app.constants import agent_skill as constants
from app.core.logging_system import BrutorLogger
from tests.admin_endpoint_harness import build_admin_harness

BASE = "/v1/admin/agent-skills"
LIMIT = 2048
LIMIT_TEXT = "more than the per-skill limit (0.002048 MB)"


@pytest.fixture()
def h(monkeypatch):
    from app.api.v1.endpoints.admin.agent_skills import create_agent_skills_admin_router

    monkeypatch.setattr(constants, "SKILL_MAX_BYTES", LIMIT)
    harness = build_admin_harness([create_agent_skills_admin_router(logger=BrutorLogger("test-skill-size-paths"))])
    yield harness
    harness.close()


def _skill(h, body: str = "# Thing\n\nDo the thing.") -> str:
    r = h.client.post(
        BASE,
        json={"name": "sized", "description": "does a thing", "skill_md_content": body},
        headers=h.headers(),
    )
    assert r.status_code in (200, 201), r.text
    payload = r.json()
    return payload.get("skill", payload)["id"]


def _refused(r):
    assert r.status_code == 413, r.text
    detail = r.json()["detail"]
    assert detail["reason"] == "too_large"
    assert LIMIT_TEXT in detail["error"]


KINDS = [
    ("resources", "resource", lambda n: {"filename": "references/R.md", "content": "r" * n}, lambda n: {"content": "r" * n}, "content"),
    ("scripts", "script", lambda n: {"filename": "scripts/run.py", "script_content": "#" * n}, lambda n: {"script_content": "#" * n}, "script_content"),
    ("assets", "asset", lambda n: {"filename": "assets/a.txt", "content": "a" * n}, lambda n: {"content": "a" * n}, "content"),
]


@pytest.mark.parametrize("kind,key,create,update,field", KINDS, ids=[k[0] for k in KINDS])
def test_create_past_the_limit_is_refused_and_nothing_is_stored(h, kind, key, create, update, field):
    skill = _skill(h)
    _refused(h.client.post(f"{BASE}/{skill}/{kind}", json=create(LIMIT), headers=h.headers()))
    listed = h.client.get(f"{BASE}/{skill}/{kind}", headers=h.headers()).json()
    assert listed["total"] == 0


@pytest.mark.parametrize("kind,key,create,update,field", KINDS, ids=[k[0] for k in KINDS])
def test_create_within_the_limit_is_stored(h, kind, key, create, update, field):
    skill = _skill(h)
    r = h.client.post(f"{BASE}/{skill}/{kind}", json=create(LIMIT // 2), headers=h.headers())
    assert r.status_code == 201, r.text


@pytest.mark.parametrize("kind,key,create,update,field", KINDS, ids=[k[0] for k in KINDS])
def test_update_past_the_limit_is_refused_and_the_file_is_unchanged(h, kind, key, create, update, field):
    skill = _skill(h)
    r = h.client.post(f"{BASE}/{skill}/{kind}", json=create(100), headers=h.headers())
    assert r.status_code == 201, r.text
    rid = r.json()[key]["id"]
    # Replacing the file counts the new size instead of the old one: just
    # under the limit is still accepted.
    ok = h.client.patch(f"{BASE}/{skill}/{kind}/{rid}", json=update(LIMIT - 200), headers=h.headers())
    assert ok.status_code == 200, ok.text
    _refused(h.client.patch(f"{BASE}/{skill}/{kind}/{rid}", json=update(LIMIT), headers=h.headers()))
    got = h.client.get(f"{BASE}/{skill}/{kind}/{rid}", headers=h.headers()).json()[key]
    assert len(got[field]) == LIMIT - 200


def test_files_add_up(h):
    skill = _skill(h)
    for i in range(3):
        r = h.client.post(
            f"{BASE}/{skill}/resources",
            json={"filename": f"references/{i}.md", "content": "x" * 600},
            headers=h.headers(),
        )
        assert r.status_code == 201, r.text
    _refused(
        h.client.post(f"{BASE}/{skill}/scripts", json={"filename": "scripts/s.py", "script_content": "#" * 600}, headers=h.headers())
    )


def test_a_base64_asset_counts_its_decoded_bytes(h):
    skill = _skill(h)
    raw = bytes(range(256)) * 7  # 1792 bytes, 2392 characters of base64
    r = h.client.post(
        f"{BASE}/{skill}/assets",
        json={"filename": "assets/b.bin", "content": base64.b64encode(raw).decode(), "encoding": "base64"},
        headers=h.headers(),
    )
    assert r.status_code == 201, r.text


def test_a_skill_md_change_past_the_limit_is_refused(h):
    skill = _skill(h)
    _refused(h.client.patch(f"{BASE}/{skill}", json={"skill_md_content": "# big\n" + "b" * LIMIT}, headers=h.headers()))


# --- skill sources: the ingest and the GitHub sync -------------------------------------


from tests.test_skill_sources import (  # noqa: E402
    SkillSource,
    SkillSourceIngestRequest,
    _ingest_discovered_skills,
    _make_discovered,
    _NullLogger,
    harness,  # noqa: F401 — the fixture
)
from app.api.v1.models.admin.skill_source import DiscoveredSkillFile  # noqa: E402
from app.database.models.agent_skill import AgentSkill  # noqa: E402


def test_ingest_refuses_a_discovered_skill_past_the_limit(harness, monkeypatch):  # noqa: F811
    monkeypatch.setattr(constants, "SKILL_MAX_BYTES", LIMIT)
    s = harness()
    try:
        src = s.query(SkillSource).filter_by(id="sks-test").one()
        big = _make_discovered(
            files=[DiscoveredSkillFile(path="skills/incident-report/references/big.md", kind="reference", content="x" * LIMIT)]
        )
        summary = _ingest_discovered_skills(
            db=s,
            source=src,
            payload=SkillSourceIngestRequest(commit_sha="commit-1", discovered=[big]),
            logger=_NullLogger(),
        )
        assert summary.created == 0 and summary.skipped == 1
        (error,) = summary.errors
        assert error.startswith("skills/incident-report/SKILL.md: refused: too_large: ")
        assert LIMIT_TEXT in error
        assert s.query(AgentSkill).count() == 0
    finally:
        s.close()


def test_ingest_counts_a_base64_file_decoded(harness, monkeypatch):  # noqa: F811
    monkeypatch.setattr(constants, "SKILL_MAX_BYTES", LIMIT)
    s = harness()
    try:
        src = s.query(SkillSource).filter_by(id="sks-test").one()
        raw = b"\x00\xff" * 900  # 1800 bytes, 2400 characters of base64
        skill = _make_discovered(
            files=[
                DiscoveredSkillFile(
                    path="skills/incident-report/assets/b.bin",
                    kind="asset",
                    content=base64.b64encode(raw).decode(),
                    encoding="base64",
                )
            ]
        )
        summary = _ingest_discovered_skills(
            db=s,
            source=src,
            payload=SkillSourceIngestRequest(commit_sha="commit-1", discovered=[skill]),
            logger=_NullLogger(),
        )
        assert summary.created == 1, summary.errors
    finally:
        s.close()


def _entries(sizes):
    from app.services.skill_source_sync.github_walker import TreeEntry

    entries = [TreeEntry(path="skills/big/SKILL.md", sha="sha-md", size=sizes[0])]
    entries += [TreeEntry(path=f"skills/big/references/{i}.md", sha=f"sha-{i}", size=n) for i, n in enumerate(sizes[1:])]
    return entries


def test_github_sync_refuses_a_skill_past_the_limit_before_downloading_it(monkeypatch):
    from app.services.skill_source_sync import scheduler
    from app.services.skill_source_sync.github_walker import SkillTooLargeError

    monkeypatch.setattr(constants, "SKILL_MAX_BYTES", LIMIT)
    fetched = []

    async def fetch_blob(client, owner, repo, sha, token):
        fetched.append(sha)
        return b"---\nname: big\ndescription: d\n---\nbody"

    monkeypatch.setattr(scheduler, "fetch_blob", fetch_blob)
    entries = _entries([100, 1000, 1000])
    with pytest.raises(SkillTooLargeError) as refused:
        asyncio.run(scheduler._build_discovered_skill(None, "o", "r", None, entries, entries[0]))
    assert refused.value.reason == "too_large"
    assert LIMIT_TEXT in str(refused.value)
    assert fetched == []


def test_github_sync_refuses_when_the_downloaded_files_are_past_the_limit(monkeypatch):
    from app.services.skill_source_sync import scheduler
    from app.services.skill_source_sync.github_walker import SkillTooLargeError

    monkeypatch.setattr(constants, "SKILL_MAX_BYTES", LIMIT)

    async def fetch_blob(client, owner, repo, sha, token):
        if sha == "sha-md":
            return b"---\nname: big\ndescription: d\n---\nbody"
        return b"y" * 1500

    monkeypatch.setattr(scheduler, "fetch_blob", fetch_blob)
    entries = _entries([None, None, None])  # GitHub did not say
    with pytest.raises(SkillTooLargeError) as refused:
        asyncio.run(scheduler._build_discovered_skill(None, "o", "r", None, entries, entries[0]))
    assert LIMIT_TEXT in str(refused.value)


def test_github_sync_reports_the_refused_skill_on_the_source(monkeypatch):
    from app.services.skill_source_sync import scheduler

    monkeypatch.setattr(constants, "SKILL_MAX_BYTES", LIMIT)
    monkeypatch.setattr(scheduler, "_claim_source", lambda source_id: ("https://github.com/o/r", "main", None, None))

    async def fetch_tree(client, owner, repo, ref, token):
        return "commit-1", _entries([100, 1000, 1000])

    async def fetch_blob(client, owner, repo, sha, token):
        raise AssertionError("nothing of a refused skill is downloaded")

    captured = {}
    monkeypatch.setattr(scheduler, "fetch_tree", fetch_tree)
    monkeypatch.setattr(scheduler, "fetch_blob", fetch_blob)
    monkeypatch.setattr(
        scheduler,
        "_ingest_and_finish",
        lambda source_id, commit_sha, discovered, refused=(): captured.update(discovered=discovered, refused=list(refused)),
    )
    asyncio.run(scheduler._sync_one_source(None, "sks-test"))
    assert captured["discovered"] == []
    (line,) = captured["refused"]
    assert line.startswith("skills/big/SKILL.md: refused: too_large: ")
    assert LIMIT_TEXT in line
