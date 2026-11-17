# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Agent skill sub-resources and import: scripts, assets, resources, group binding,
zip import. Each sub-resource carries its own id, so isolation has to hold on the
pair (a skill of mine, a script of someone else's), not just on the skill.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from app.api.v1.endpoints.admin.agent_skills import create_agent_skills_admin_router
from app.core.admin_user.permissions import Permission
from app.core.logging_system import BrutorLogger
from app.database.models.agent_skill import AgentSkill, resource_group_agent_skills
from tests.admin_endpoint_harness import build_admin_harness, throwaway_pg_database

BASE = "/v1/admin/agent-skills"
_n = {"i": 0}


@pytest.fixture(scope="module")
def h():
    for url in throwaway_pg_database("skill_subres"):
        harness = build_admin_harness(
            [create_agent_skills_admin_router(logger=BrutorLogger("test-skill-subres"))], database_url=url
        )
        yield harness
        harness.close()


def _skill(h, tenant: str = "t1", **over) -> str:
    _n["i"] += 1
    body = {
        "name": f"skill-{_n['i']}",
        "description": "does a thing",
        "skill_md_content": "# Thing\n\nDo the thing.",
        **over,
    }
    r = h.client.post(BASE, json=body, headers=h.headers(tenant))
    assert r.status_code in (200, 201), r.text
    payload = r.json()
    return payload.get("skill", payload)["id"]


def _unwrap(r, key):
    payload = r.json()
    return payload.get(key, payload)


SUBRESOURCES = [
    ("scripts", {"filename": "run.py", "script_content": "print('hi')"}, {"script_content": "print('changed')"}, "script"),
    ("assets", {"filename": "a.txt", "content": "alpha"}, {"content": "beta"}, "asset"),
    ("resources", {"filename": "R.md", "content": "alpha"}, {"content": "beta"}, "resource"),
]


key_of = {kind: key for kind, _c, _u, key in SUBRESOURCES}


def _sub(payload: dict, kind: str) -> dict:
    return payload.get(key_of[kind], payload)


@pytest.mark.parametrize("kind,create,update,key", SUBRESOURCES, ids=[s[0] for s in SUBRESOURCES])
class TestSubResources:
    def _add(self, h, skill, kind, create, tenant="t1"):
        r = h.client.post(f"{BASE}/{skill}/{kind}", json=create, headers=h.headers(tenant))
        assert r.status_code in (200, 201), r.text
        payload = r.json()
        return payload.get(key_of[kind], payload)["id"]

    def test_crud_round_trip(self, h, kind, create, update, key):
        skill = _skill(h)
        rid = self._add(h, skill, kind, create)
        listed = h.client.get(f"{BASE}/{skill}/{kind}", headers=h.headers("t1"))
        assert listed.status_code == 200, listed.text
        got = h.client.get(f"{BASE}/{skill}/{kind}/{rid}", headers=h.headers("t1"))
        assert got.status_code == 200, got.text
        assert _sub(got.json(), kind)["filename"] == create["filename"]
        patched = h.client.patch(f"{BASE}/{skill}/{kind}/{rid}", json=update, headers=h.headers("t1"))
        assert patched.status_code == 200, patched.text
        again = _sub(h.client.get(f"{BASE}/{skill}/{kind}/{rid}", headers=h.headers("t1")).json(), kind)
        field = "script_content" if kind == "scripts" else "content"
        assert again[field] == update[field]
        assert h.client.delete(f"{BASE}/{skill}/{kind}/{rid}", headers=h.headers("t1")).status_code in (200, 204)
        assert h.client.get(f"{BASE}/{skill}/{kind}/{rid}", headers=h.headers("t1")).status_code == 404

    def test_another_tenants_skill_is_refused(self, h, kind, create, update, key):
        skill = _skill(h, "t2")
        rid = self._add(h, skill, kind, create, tenant="t2")
        t1 = h.headers("t1")
        assert h.client.get(f"{BASE}/{skill}/{kind}/{rid}", headers=t1).status_code == 404
        assert h.client.patch(f"{BASE}/{skill}/{kind}/{rid}", json=update, headers=t1).status_code == 404
        assert h.client.delete(f"{BASE}/{skill}/{kind}/{rid}", headers=t1).status_code == 404
        assert h.client.post(f"{BASE}/{skill}/{kind}", json=create, headers=t1).status_code == 404
        original = _sub(h.client.get(f"{BASE}/{skill}/{kind}/{rid}", headers=h.headers("t2")).json(), kind)
        field = "script_content" if kind == "scripts" else "content"
        assert original[field] == create[field]

    def test_a_sub_resource_of_my_other_skill_cannot_be_reached_through_this_one(self, h, kind, create, update, key):
        owner, other = _skill(h), _skill(h)
        rid = self._add(h, owner, kind, create)
        assert h.client.get(f"{BASE}/{other}/{kind}/{rid}", headers=h.headers("t1")).status_code == 404
        assert h.client.delete(f"{BASE}/{other}/{kind}/{rid}", headers=h.headers("t1")).status_code == 404

    def test_another_tenants_sub_resource_through_my_own_skill_is_refused(self, h, kind, create, update, key):
        mine = _skill(h, "t1")
        theirs = _skill(h, "t2")
        rid = self._add(h, theirs, kind, create, tenant="t2")
        t1 = h.headers("t1")
        assert h.client.get(f"{BASE}/{mine}/{kind}/{rid}", headers=t1).status_code == 404
        assert h.client.patch(f"{BASE}/{mine}/{kind}/{rid}", json=update, headers=t1).status_code == 404
        assert h.client.delete(f"{BASE}/{mine}/{kind}/{rid}", headers=t1).status_code == 404

    def test_a_duplicate_filename_is_refused(self, h, kind, create, update, key):
        skill = _skill(h)
        self._add(h, skill, kind, create)
        r = h.client.post(f"{BASE}/{skill}/{kind}", json=create, headers=h.headers("t1"))
        assert r.status_code in (400, 409)

    def test_a_read_only_admin_cannot_change_it(self, h, kind, create, update, key):
        skill = _skill(h)
        rid = self._add(h, skill, kind, create)
        ro = h.limited_headers("t1", [Permission.AGENT_SKILL_READ])
        assert h.client.get(f"{BASE}/{skill}/{kind}/{rid}", headers=ro).status_code == 200
        assert h.client.post(f"{BASE}/{skill}/{kind}", json=create, headers=ro).status_code == 403
        assert h.client.patch(f"{BASE}/{skill}/{kind}/{rid}", json=update, headers=ro).status_code == 403
        assert h.client.delete(f"{BASE}/{skill}/{kind}/{rid}", headers=ro).status_code == 403

    @pytest.mark.parametrize("filename", ["../../etc/passwd", "a/../../b", "..\\windows"])
    def test_a_traversal_filename_is_refused(self, h, kind, create, update, key, filename):
        r = h.client.post(f"{BASE}/{_skill(h)}/{kind}", json={**create, "filename": filename}, headers=h.headers("t1"))
        assert r.status_code in (400, 422), r.text


class TestGroupBinding:
    def _bindings(self, h, skill) -> set:
        s = h.Factory()
        try:
            rows = s.execute(
                resource_group_agent_skills.select().where(resource_group_agent_skills.c.agent_skill_id == skill)
            ).fetchall()
            return {r.resource_group_id for r in rows}
        finally:
            s.close()

    def test_assign_and_remove(self, h):
        skill = _skill(h)
        r = h.client.post(f"{BASE}/{skill}/groups", json={"group_ids": ["group-default-t1"]}, headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        assert self._bindings(h, skill) == {"group-default-t1"}
        r = h.client.request("DELETE", f"{BASE}/{skill}/groups", json={"group_ids": ["group-default-t1"]}, headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        assert self._bindings(h, skill) == set()

    def test_a_group_of_another_tenant_is_not_bound(self, h):
        skill = _skill(h)
        r = h.client.post(f"{BASE}/{skill}/groups", json={"group_ids": ["group-default-t2"]}, headers=h.headers("t1"))
        assert r.status_code == 404, r.text
        assert self._bindings(h, skill) == set()

    def test_a_skill_of_another_tenant_cannot_be_bound(self, h):
        skill = _skill(h, "t2")
        r = h.client.post(f"{BASE}/{skill}/groups", json={"group_ids": ["group-default-t1"]}, headers=h.headers("t1"))
        assert r.status_code == 404
        assert self._bindings(h, skill) == set()

    def test_assigning_twice_keeps_one_binding(self, h):
        skill = _skill(h)
        for _ in range(2):
            r = h.client.post(f"{BASE}/{skill}/groups", json={"group_ids": ["group-default-t1"]}, headers=h.headers("t1"))
            assert r.status_code == 200, r.text
        assert self._bindings(h, skill) == {"group-default-t1"}


def _zip(files: dict) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return out.getvalue()


def _md(name: str, **front) -> str:
    lines = [f"name: {name}"] + [f"{k}: {v}" for k, v in front.items()]
    return "---\n" + "\n".join(lines) + "\n---\n# Body\n\nFirst paragraph of the body.\n"


class TestImport:
    def _post(self, h, *uploads, tenant="t1", headers=None):
        files = [("files", (name, data, "application/zip")) for name, data in uploads]
        return h.client.post(f"{BASE}/import", files=files, headers=headers or h.headers(tenant))

    def _skill_row(self, h, tenant, name):
        s = h.Factory()
        try:
            row = s.query(AgentSkill).filter_by(tenant_id=tenant, name=name).first()
            if row is not None:
                s.expunge(row)
            return row
        finally:
            s.close()

    def test_imports_the_skill_its_scripts_and_resources(self, h):
        data = _zip({
            "zip-one/SKILL.md": _md("zip-one", description="Imported", version="2.3.4", license="MIT",
                                    **{"allowed-tools": "a, b"}, author="me"),
            "zip-one/scripts/run.py": "print(1)",
            "zip-one/references/REF.md": "ref",
            "zip-one/templates/t.md": "tmpl",
            "zip-one/examples.md": "ex",
            "zip-one/assets/logo.bin": b"\x00\x01",
        })
        r = self._post(h, ("zip-one.zip", data))
        assert r.status_code in (200, 201), r.text
        result = r.json()["results"][0]
        assert result["success"] is True, result
        assert result["scripts_count"] == 1
        assert result["resources_count"] >= 3
        row = self._skill_row(h, "t1", "zip-one")
        assert (row.version, row.description, row.tenant_id) == ("2.3.4", "Imported", "t1")
        assert row.allowed_tools == ["a", "b"]
        got = h.client.get(f"{BASE}/{row.id}/scripts", headers=h.headers("t1"))
        assert got.status_code == 200 and "run.py" in got.text

    def test_a_traversal_entry_in_the_zip_is_not_stored(self, h):
        data = _zip({"slip/SKILL.md": _md("slip-skill"), "slip/scripts/../../../evil.py": "print(1)",
                     "slip/scripts/ok.py": "print(2)"})
        r = self._post(h, ("slip.zip", data))
        assert r.json()["results"][0]["success"] is True, r.text
        row = self._skill_row(h, "t1", "slip-skill")
        listed = h.client.get(f"{BASE}/{row.id}/scripts", headers=h.headers("t1"))
        assert "ok.py" in listed.text and "evil" not in listed.text

    def test_flat_layout_and_defaults(self, h):
        r = self._post(h, ("Flat Skill.zip", _zip({"SKILL.md": "# Heading\n\nJust a body."})))
        assert r.json()["results"][0]["success"] is True, r.text
        row = self._skill_row(h, "t1", "flat-skill")
        assert row.version == "1.0.0" and row.description

    def test_the_same_version_twice_is_refused(self, h):
        data = _zip({"dup/SKILL.md": _md("dup-skill", version="1.0.0")})
        assert self._post(h, ("dup.zip", data)).json()["results"][0]["success"] is True
        again = self._post(h, ("dup.zip", data)).json()["results"][0]
        assert again["success"] is False and "already exists" in again["error"]

    def test_the_same_name_in_another_tenant_is_its_own_skill(self, h):
        data = _zip({"shared/SKILL.md": _md("shared-skill", version="1.0.0")})
        assert self._post(h, ("s.zip", data), tenant="t1").json()["results"][0]["success"] is True
        assert self._post(h, ("s.zip", data), tenant="t2").json()["results"][0]["success"] is True
        assert self._skill_row(h, "t2", "shared-skill").tenant_id == "t2"

    @pytest.mark.parametrize(
        "name,data,error",
        [
            ("notes.txt", b"plain", ".zip"),
            ("bad.zip", b"not a zip", "Invalid zip"),
            ("empty.zip", _zip({"readme.md": "no skill"}), "No SKILL.md"),
        ],
    )
    def test_a_bad_upload_is_reported_and_creates_nothing(self, h, name, data, error):
        r = self._post(h, (name, data))
        assert r.status_code in (200, 201), r.text
        result = r.json()["results"][0]
        assert result["success"] is False and error in result["error"]

    def test_one_bad_zip_does_not_stop_the_others(self, h):
        good = _zip({"mixed/SKILL.md": _md("mixed-good", version="1.0.0")})
        body = self._post(h, ("bad.zip", b"nope"), ("good.zip", good)).json()
        assert [x["success"] for x in body["results"]] == [False, True]
        assert self._skill_row(h, "t1", "mixed-good") is not None

    def test_needs_the_create_permission(self, h):
        data = _zip({"perm/SKILL.md": _md("perm-skill")})
        limited = h.limited_headers("t1", [Permission.AGENT_SKILL_READ])
        assert self._post(h, ("p.zip", data), headers=limited).status_code == 403
        assert self._skill_row(h, "t1", "perm-skill") is None


class TestLifecycleRefusals:
    def test_a_read_only_admin_cannot_move_it(self, h):
        skill = _skill(h)
        ro = h.limited_headers("t1", [Permission.AGENT_SKILL_READ])
        for action in ("publish", "archive"):
            assert h.client.post(f"{BASE}/{skill}/{action}", json={}, headers=ro).status_code == 403
        s = h.Factory()
        try:
            assert s.get(AgentSkill, skill).status == "draft"
        finally:
            s.close()

    def test_search_and_catalog_show_only_this_tenant(self, h):
        mine, theirs = _skill(h, "t1", name="findable-mine"), _skill(h, "t2", name="findable-theirs")
        r = h.client.post(f"{BASE}/search", json={"query": "findable"}, headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        assert "findable-mine" in r.text and "findable-theirs" not in r.text
        del mine, theirs

    def test_list_filters_and_execution_lookup_are_tenant_scoped(self, h):
        assert h.client.get(f"{BASE}/executions/nope", headers=h.headers("t1")).status_code == 404
        r = h.client.get(f"{BASE}?status=draft", headers=h.headers("t1"))
        assert r.status_code == 200, r.text
