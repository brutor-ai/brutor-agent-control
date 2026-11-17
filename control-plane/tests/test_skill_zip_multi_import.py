# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
A skill zip may hold several skills: every top-level folder with a
`SKILL.md` is imported as its own skill, and each is held to the per-skill
limit (`SKILL_MAX_BYTES`). A single root `SKILL.md` is one skill, as before.
The archive is capped at `ARCHIVE_MAX_SKILLS` skills and `ARCHIVE_MAX_BYTES`
uncompressed, refused from the zip's directory before anything is
decompressed. The result names each skill as imported, refused (with the
reason) or skipped, and the files of the archive that belong to no skill
folder: they are reported, and never attached to a skill.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from app.constants import agent_skill as constants
from app.core.logging_system import BrutorLogger
from app.database.models.agent_skill import AgentSkill
from app.database.models.agent_skill_resource import AgentSkillResource
from tests.admin_endpoint_harness import build_admin_harness


@pytest.fixture()
def h():
    from app.api.v1.endpoints.admin.agent_skills import create_agent_skills_admin_router

    harness = build_admin_harness([create_agent_skills_admin_router(logger=BrutorLogger("test-skill-multi"))])
    harness.db = harness.Factory()
    yield harness
    harness.db.close()
    harness.close()


def _skill_md(name: str) -> str:
    return f"---\nname: {name}\ndescription: The {name} skill\n---\n# {name}\n"


def _zip(entries: dict, filename: str = "bundle.zip") -> tuple:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return (filename, buffer.getvalue(), "application/zip")


def _post(h, *uploads):
    r = h.client.post("/v1/admin/agent-skills/import", files=[("files", u) for u in uploads], headers=h.headers())
    assert r.status_code == 200, r.text
    return r.json()


def _by_folder(body) -> dict:
    return {item["folder"]: item for item in body["results"]}


def test_every_top_level_folder_with_a_skill_md_is_its_own_skill(h):
    body = _post(h, _zip({
        "alpha/SKILL.md": _skill_md("alpha"),
        "alpha/references/a.md": "about alpha",
        "beta/SKILL.md": _skill_md("beta"),
        "beta/scripts/run.py": "print('beta')",
        "gamma/SKILL.md": _skill_md("gamma"),
    }))
    results = _by_folder(body)
    assert set(results) == {"alpha", "beta", "gamma"}
    assert all(item["status"] == "imported" and item["success"] for item in results.values())
    assert (results["alpha"]["resources_count"], results["beta"]["scripts_count"]) == (1, 1)
    assert all(item["zip_filename"] == "bundle.zip" for item in results.values())
    assert (body["success_count"], body["refused_count"], body["skipped_count"]) == (3, 0, 0)
    names = {skill.name for skill in h.db.query(AgentSkill).all()}
    assert {"alpha", "beta", "gamma"} <= names
    # Each skill holds its own files only.
    alpha = h.db.query(AgentSkill).filter_by(name="alpha").one()
    assert [res.filename for res in h.db.query(AgentSkillResource).filter_by(skill_id=alpha.id)] == ["references/a.md"]


def test_a_single_root_skill_md_is_one_skill(h):
    body = _post(h, _zip({"SKILL.md": _skill_md("rooted"), "references/r.md": "x", "nested/SKILL.md": "not a skill"},
                         filename="rooted.zip"))
    (result,) = body["results"]
    assert result["status"] == "imported" and result["skill_name"] == "rooted" and result["folder"] == ""
    # Under a root SKILL.md everything is that skill's.
    assert result["resources_count"] == 2
    assert body["archives"][0]["unassigned_files"] == []


def test_files_of_no_skill_folder_are_reported_and_never_attached(h):
    body = _post(h, _zip({
        "README.md": "the bundle",
        "alpha/SKILL.md": _skill_md("alpha2"),
        "loose/notes.md": "no skill here",
        "__MACOSX/alpha/._SKILL.md": "mac metadata",
    }))
    (result,) = body["results"]
    assert result["status"] == "imported" and result["resources_count"] == 0
    (archive,) = body["archives"]
    assert archive["zip_filename"] == "bundle.zip"
    assert archive["unassigned_files"] == ["README.md", "loose/notes.md"]
    skill = h.db.query(AgentSkill).filter_by(name="alpha2").one()
    assert h.db.query(AgentSkillResource).filter_by(skill_id=skill.id).count() == 0


def test_each_skill_is_held_to_the_per_skill_limit(h, monkeypatch):
    monkeypatch.setattr(constants, "SKILL_MAX_BYTES", 2048)
    body = _post(h, _zip({
        "small/SKILL.md": _skill_md("small"),
        "big/SKILL.md": _skill_md("big"),
        "big/references/blob.md": "y" * 4096,
    }))
    results = _by_folder(body)
    assert results["small"]["status"] == "imported"
    assert results["big"]["status"] == "refused" and results["big"]["success"] is False
    assert "unpacks to" in results["big"]["error"] and "per-skill limit" in results["big"]["error"]
    assert results["big"]["reason"] == "too_large"
    assert not h.db.query(AgentSkill).filter_by(name="big").count()
    assert (body["success_count"], body["refused_count"]) == (1, 1)


def test_an_existing_skill_is_skipped_and_the_others_go_on(h):
    _post(h, _zip({"dup/SKILL.md": _skill_md("dup")}))
    body = _post(h, _zip({"dup/SKILL.md": _skill_md("dup"), "fresh/SKILL.md": _skill_md("fresh")}))
    results = _by_folder(body)
    assert results["dup"]["status"] == "skipped" and results["dup"]["reason"] == "already_exists"
    assert "already exists" in results["dup"]["error"]
    assert results["fresh"]["status"] == "imported"
    assert (body["success_count"], body["skipped_count"], body["refused_count"]) == (1, 1, 0)


def test_too_many_skills_refuse_the_archive_unread(h, monkeypatch):
    monkeypatch.setattr(constants, "ARCHIVE_MAX_SKILLS", 2)
    upload = _zip({f"s{i}/SKILL.md": _skill_md(f"s{i}") for i in range(3)})
    opened = []
    original = zipfile.ZipFile.open
    monkeypatch.setattr(zipfile.ZipFile, "open", lambda self, name, *a, **k: opened.append(name) or original(self, name, *a, **k))
    body = _post(h, upload)
    (result,) = body["results"]
    assert result["status"] == "refused" and result["reason"] == "too_many_skills"
    assert "3 skills" in result["error"]
    assert opened == []
    assert not h.db.query(AgentSkill).filter(AgentSkill.name.in_(["s0", "s1", "s2"])).count()


def test_an_archive_that_unpacks_past_its_cap_is_refused_unread(h, monkeypatch):
    monkeypatch.setattr(constants, "ARCHIVE_MAX_BYTES", 64 * 1024)
    upload = _zip({"a/SKILL.md": _skill_md("a"), "a/zero.bin": b"\0" * (1024 * 1024)})
    opened = []
    original = zipfile.ZipFile.open
    monkeypatch.setattr(zipfile.ZipFile, "open", lambda self, name, *a, **k: opened.append(name) or original(self, name, *a, **k))
    assert len(upload[1]) < 64 * 1024
    (result,) = _post(h, upload)["results"]
    assert result["status"] == "refused" and result["reason"] == "archive_too_large"
    assert "unpacks to" in result["error"]
    assert opened == []


def test_a_member_larger_than_its_declared_size_is_refused(h, monkeypatch):
    """A zip whose directory understates a member's size: the bounded reader
    stops at the declared size and the skill is refused."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("liar/SKILL.md", _skill_md("liar"))
        zf.writestr("liar/references/x.md", "z" * 1000)
    original = zipfile.ZipFile.infolist

    def understated(self):
        infos = original(self)
        for info in infos:
            if info.filename.endswith("x.md"):
                info.file_size = 10
        return infos

    monkeypatch.setattr(zipfile.ZipFile, "infolist", understated)
    (result,) = _post(h, ("liar.zip", buffer.getvalue(), "application/zip"))["results"]
    assert result["status"] == "refused" and result["reason"] == "invalid_archive"
    assert not h.db.query(AgentSkill).filter_by(name="liar").count()


def test_a_zip_without_any_skill_md_is_refused(h):
    (result,) = _post(h, _zip({"docs/readme.md": "x"}, filename="none.zip"))["results"]
    assert result["status"] == "refused" and result["reason"] == "no_skill"


def test_the_list_states_the_limits(h):
    r = h.client.get("/v1/admin/agent-skills", headers=h.headers())
    assert r.status_code == 200, r.text
    assert r.json()["limits"] == {
        "skill_max_bytes": constants.SKILL_MAX_BYTES,
        "archive_max_bytes": constants.ARCHIVE_MAX_BYTES,
        "archive_max_skills": constants.ARCHIVE_MAX_SKILLS,
    }
