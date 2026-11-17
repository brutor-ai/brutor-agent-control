# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
HTTP-level tests for the Agent Skills admin API (/v1/admin/agent-skills).

These tests exercise the wire contract through a real app + auth + SQLite.

What is worth defending at this boundary:

  * **The lifecycle is a one-way ratchet with guarded exits.** DRAFT skills
    can be edited and deleted freely; a PUBLISHED skill must be deprecated
    and archived before deletion — governed capability does not vanish while
    agents may be using it.
  * **Sub-files are versioned parts of the skill.** Resources and scripts
    hang off the skill and die with it.
  * **Tenant isolation** — a skill id from another tenant reads as 404,
    never as someone else's content.
"""

import pytest

from tests.admin_endpoint_harness import build_admin_harness

from app.api.v1.endpoints.admin.agent_skills import create_agent_skills_admin_router
from app.core.logging_system import BrutorLogger


@pytest.fixture()
def h():
    harness = build_admin_harness(
        [create_agent_skills_admin_router(logger=BrutorLogger("test-skills"))],
        logger_name="test-skills",
    )
    yield harness
    harness.close()


def _mk(name="refund-check", **over):
    body = {
        "name": name,
        "description": "Applies the refund policy to a request",
        "skill_md_content": "# Refund policy\n\nApply the policy steps in order.",
    }
    body.update(over)
    return body


def _unwrap(payload):
    """Detail responses wrap the skill: {"skill": {...}, ...}."""
    if isinstance(payload, dict) and "skill" in payload:
        return payload["skill"]
    return payload


def _create(h, tenant="t1", name="refund-check", **over):
    r = h.client.post(
        "/v1/admin/agent-skills", json=_mk(name, **over), headers=h.headers(tenant)
    )
    assert r.status_code in (200, 201), r.text
    return _unwrap(r.json())


# ---------------------------------------------------------------------------
# CRUD basics
# ---------------------------------------------------------------------------


class TestCrud:
    def test_create_returns_a_draft_skill(self, h):
        body = _create(h)
        assert body["name"] == "refund-check"
        assert body["status"].lower() == "draft"

    def test_a_hostile_name_is_422(self, h):
        r = h.client.post(
            "/v1/admin/agent-skills",
            json=_mk("Bad Name!"),
            headers=h.headers(),
        )
        assert r.status_code == 422

    def test_get_by_id_round_trips(self, h):
        sid = _create(h)["id"]
        r = h.client.get(f"/v1/admin/agent-skills/{sid}", headers=h.headers())
        assert r.status_code == 200, r.text
        assert _unwrap(r.json())["id"] == sid

    def test_unknown_id_is_404(self, h):
        r = h.client.get("/v1/admin/agent-skills/nope", headers=h.headers())
        assert r.status_code == 404

    def test_list_contains_the_created_skill(self, h):
        sid = _create(h)["id"]
        r = h.client.get("/v1/admin/agent-skills", headers=h.headers())
        assert r.status_code == 200, r.text
        payload = r.json()
        # The listing is a dict keyed by skill id: {"skills": {id: {...}}}.
        items = list(payload["skills"].values())
        assert any(s["id"] == sid for s in items)

    def test_update_changes_the_description(self, h):
        sid = _create(h)["id"]
        r = h.client.patch(
            f"/v1/admin/agent-skills/{sid}",
            json={"description": "Now with extra caution"},
            headers=h.headers(),
        )
        assert r.status_code == 200, r.text
        assert _unwrap(r.json())["description"] == "Now with extra caution"

    def test_draft_delete_is_allowed(self, h):
        sid = _create(h)["id"]
        r = h.client.delete(f"/v1/admin/agent-skills/{sid}", headers=h.headers())
        assert r.status_code in (200, 204), r.text
        assert (
            h.client.get(
                f"/v1/admin/agent-skills/{sid}", headers=h.headers()
            ).status_code
            == 404
        )

    def test_manifest_is_served(self, h):
        sid = _create(h)["id"]
        r = h.client.get(
            f"/v1/admin/agent-skills/{sid}/manifest", headers=h.headers()
        )
        assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# Lifecycle ratchet
# ---------------------------------------------------------------------------


class TestLifecycle:
    def _publish(self, h, sid):
        r = h.client.post(
            f"/v1/admin/agent-skills/{sid}/publish",
            json={"skill_id": sid},
            headers=h.headers(),
        )
        assert r.status_code == 200, r.text
        return _unwrap(r.json())

    def test_publish_then_deprecate_then_archive_then_delete(self, h):
        sid = _create(h)["id"]
        assert self._publish(h, sid)["status"].lower() == "published"

        r = h.client.post(
            f"/v1/admin/agent-skills/{sid}/deprecate",
            json={"skill_id": sid, "reason": "superseded by v2"},
            headers=h.headers(),
        )
        assert r.status_code == 200, r.text
        assert _unwrap(r.json())["status"].lower() == "deprecated"

        r = h.client.post(
            f"/v1/admin/agent-skills/{sid}/archive",
            json={"skill_id": sid},
            headers=h.headers(),
        )
        assert r.status_code == 200, r.text

        r = h.client.delete(f"/v1/admin/agent-skills/{sid}", headers=h.headers())
        assert r.status_code in (200, 204), r.text

    def test_a_published_skill_cannot_be_deleted_directly(self, h):
        sid = _create(h)["id"]
        self._publish(h, sid)
        r = h.client.delete(f"/v1/admin/agent-skills/{sid}", headers=h.headers())
        assert r.status_code in (400, 409), r.text

    def test_deprecation_requires_a_reason(self, h):
        sid = _create(h)["id"]
        self._publish(h, sid)
        r = h.client.post(
            f"/v1/admin/agent-skills/{sid}/deprecate",
            json={"skill_id": sid},
            headers=h.headers(),
        )
        assert r.status_code == 422

    def test_bulk_delete_skips_published_and_reports_it(self, h):
        draft = _create(h, name="draft-skill")["id"]
        pub = _create(h, name="published-skill")["id"]
        self._publish(h, pub)
        r = h.client.post(
            "/v1/admin/agent-skills/bulk-delete",
            json={"skill_ids": [draft, pub]},
            headers=h.headers(),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert draft in body.get("deleted", body.get("deleted_ids", []))
        skipped = body.get("skipped", [])
        assert any(s.get("skill_id") == pub for s in skipped)

    def test_validate_endpoint_runs(self, h):
        sid = _create(h)["id"]
        r = h.client.post(
            f"/v1/admin/agent-skills/{sid}/validate",
            json={"skill_id": sid},
            headers=h.headers(),
        )
        assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# Resources and scripts
# ---------------------------------------------------------------------------


class TestSubFiles:
    def test_resource_crud_round_trip(self, h):
        sid = _create(h)["id"]
        r = h.client.post(
            f"/v1/admin/agent-skills/{sid}/resources",
            json={"filename": "REFERENCE.md", "content": "# Reference"},
            headers=h.headers(),
        )
        assert r.status_code in (200, 201), r.text
        rid = _unwrap(r.json()).get("id") or r.json().get("resource", {}).get("id")

        r = h.client.get(
            f"/v1/admin/agent-skills/{sid}/resources", headers=h.headers()
        )
        assert r.status_code == 200
        listing = r.json()
        items = listing if isinstance(listing, list) else listing.get(
            "resources", listing.get("items", [])
        )
        assert any(x["id"] == rid for x in items)

        r = h.client.patch(
            f"/v1/admin/agent-skills/{sid}/resources/{rid}",
            json={"content": "# Reference v2"},
            headers=h.headers(),
        )
        assert r.status_code == 200, r.text

        r = h.client.delete(
            f"/v1/admin/agent-skills/{sid}/resources/{rid}", headers=h.headers()
        )
        assert r.status_code in (200, 204), r.text

    def test_script_creation_and_listing(self, h):
        sid = _create(h)["id"]
        r = h.client.post(
            f"/v1/admin/agent-skills/{sid}/scripts",
            json={
                "filename": "check.py",
                "script_content": "print('ok')",
                "language": "python",
            },
            headers=h.headers(),
        )
        assert r.status_code in (200, 201), r.text
        r = h.client.get(
            f"/v1/admin/agent-skills/{sid}/scripts", headers=h.headers()
        )
        assert r.status_code == 200, r.text

    def test_resource_on_unknown_skill_is_404(self, h):
        r = h.client.post(
            "/v1/admin/agent-skills/nope/resources",
            json={"filename": "X.md", "content": "x"},
            headers=h.headers(),
        )
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Discovery surfaces
# ---------------------------------------------------------------------------


class TestDiscovery:
    def test_catalog_lists_skills(self, h):
        _create(h)
        r = h.client.get("/v1/admin/agent-skills/catalog", headers=h.headers())
        assert r.status_code == 200, r.text

    def test_search_finds_by_name_fragment(self, h):
        sid = _create(h, name="findable-skill")["id"]
        r = h.client.post(
            "/v1/admin/agent-skills/search",
            json={"name": "findable"},
            headers=h.headers(),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        found = body["skills"] if isinstance(body.get("skills"), list) else list(
            body.get("skills", {}).values()
        )
        assert any(x["id"] == sid for x in found)

    def test_executions_listing_is_empty_but_valid(self, h):
        sid = _create(h)["id"]
        r = h.client.get(
            f"/v1/admin/agent-skills/{sid}/executions", headers=h.headers()
        )
        assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


class TestIsolation:
    def test_another_tenant_cannot_read_the_skill(self, h):
        sid = _create(h, tenant="t1")["id"]
        r = h.client.get(f"/v1/admin/agent-skills/{sid}", headers=h.headers("t2"))
        assert r.status_code == 404

    def test_another_tenant_cannot_delete_the_skill(self, h):
        sid = _create(h, tenant="t1")["id"]
        r = h.client.delete(
            f"/v1/admin/agent-skills/{sid}", headers=h.headers("t2")
        )
        assert r.status_code == 404
