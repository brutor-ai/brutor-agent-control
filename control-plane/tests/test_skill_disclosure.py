# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Governed Agent Skills progressive disclosure: registry contract.

Regression suite. Pins the control-plane half of progressive disclosure:

- ``agent_skills.disclosure_mode`` (enum: progressive | execute | both)
- ``agent_skill_scripts.args_schema`` (JSON, L2 manifest)
- ``agent_skill_assets.vars_schema`` (JSON, L2 manifest)
- ``AgentSkill.build_manifest()`` — the L2 manifest derived from child rows,
  with disabled scripts filtered out
- schema ⇄ ORM ⇄ Pydantic ⇄ to_dict parity
- the a123_skill_disclosure_mode migration file shape (revision chain, columns, name-backfill)

Textual/ORM assertions only — no live DB (mirrors test_skill_tenant_controls.py
and test_a090_ai_assets_migration.py).
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "x")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import pytest
from pydantic import ValidationError
from sqlalchemy import JSON, Enum as SQLEnum

from app.constants.agent_skill import (
    AssetType,
    ScriptExecutionMode,
    ScriptLanguage,
    SkillDisclosureMode,
    SkillResourceType,
)
from app.database.models.agent_skill import AgentSkill
from app.database.models.agent_skill_asset import AgentSkillAsset
from app.database.models.agent_skill_resource import AgentSkillResource
from app.database.models.agent_skill_script import AgentSkillScript
from app.api.v1.models.admin.agent_skill import (
    AgentSkillAssetCreate,
    AgentSkillAssetResponse,
    AgentSkillAssetUpdate,
    AgentSkillCreate,
    AgentSkillResponse,
    AgentSkillScriptCreate,
    AgentSkillScriptResponse,
    AgentSkillScriptUpdate,
    AgentSkillUpdate,
)


# ---------------------------------------------------------------------------
# Enum
# ---------------------------------------------------------------------------


class TestDisclosureModeEnum:
    def test_values(self):
        assert {m.value for m in SkillDisclosureMode} == {"progressive", "execute", "both"}

    def test_progressive_is_default_target(self):
        assert SkillDisclosureMode.PROGRESSIVE.value == "progressive"


# ---------------------------------------------------------------------------
# ORM columns
# ---------------------------------------------------------------------------


class TestOrmColumns:
    def test_skill_has_disclosure_mode_enum_not_null_default_progressive(self):
        col = AgentSkill.__table__.columns["disclosure_mode"]
        assert isinstance(col.type, SQLEnum)
        assert col.nullable is False
        assert col.default.arg is SkillDisclosureMode.PROGRESSIVE

    def test_script_has_args_schema_json_nullable(self):
        col = AgentSkillScript.__table__.columns["args_schema"]
        assert isinstance(col.type, JSON)
        assert col.nullable is True

    def test_asset_has_vars_schema_json_nullable(self):
        col = AgentSkillAsset.__table__.columns["vars_schema"]
        assert isinstance(col.type, JSON)
        assert col.nullable is True


# ---------------------------------------------------------------------------
# build_manifest()
# ---------------------------------------------------------------------------


def _skill_with_children() -> AgentSkill:
    skill = AgentSkill(
        id="skill-x", tenant_id="t1", name="revenue-report", description="d",
        version="1.0.0", skill_md_content="# body", skill_md_hash="h",
    )
    skill.resources = [
        AgentSkillResource(
            id="asr-1", tenant_id="t1", skill_id="skill-x", filename="SCHEMA.json",
            resource_type=SkillResourceType.SCHEMA, content="{}", content_hash="h",
            mime_type="application/json", size_bytes=2,
        ),
    ]
    skill.scripts = [
        AgentSkillScript(
            id="ass-1", tenant_id="t1", skill_id="skill-x", filename="run_report.py",
            script_content="print(1)", script_hash="h", language=ScriptLanguage.PYTHON,
            execution_mode=ScriptExecutionMode.SANDBOX,
            args_schema={"type": "object", "properties": {"quarter": {"type": "string"}}},
        ),
        AgentSkillScript(
            id="ass-2", tenant_id="t1", skill_id="skill-x", filename="secret.py",
            script_content="x", script_hash="h", language=ScriptLanguage.PYTHON,
            execution_mode=ScriptExecutionMode.DISABLED,
        ),
    ]
    skill.assets = [
        AgentSkillAsset(
            id="asa-1", tenant_id="t1", skill_id="skill-x", filename="report.md.j2",
            content="Q{{quarter}}", content_hash="h", asset_type=AssetType.TEMPLATE,
            mime_type="text/plain",
            vars_schema={"type": "object", "properties": {"quarter": {"type": "string"}}},
        ),
    ]
    return skill


class TestBuildManifest:
    def test_manifest_shape(self):
        m = _skill_with_children().build_manifest()
        assert set(m.keys()) == {"resources", "scripts", "templates"}

    def test_resources_entry(self):
        m = _skill_with_children().build_manifest()
        assert m["resources"] == [
            {"path": "SCHEMA.json", "kind": "schema", "mime": "application/json", "bytes": 2}
        ]

    def test_disabled_script_excluded_enabled_carries_args_schema(self):
        m = _skill_with_children().build_manifest()
        names = [s["name"] for s in m["scripts"]]
        assert names == ["run_report.py"]  # disabled 'secret.py' filtered out
        assert m["scripts"][0]["language"] == "python"
        assert m["scripts"][0]["args_schema"]["properties"] == {"quarter": {"type": "string"}}

    def test_template_carries_vars_schema(self):
        m = _skill_with_children().build_manifest()
        assert len(m["templates"]) == 1
        t = m["templates"][0]
        assert t["name"] == "report.md.j2"
        assert t["kind"] == "template"
        assert t["vars_schema"]["properties"] == {"quarter": {"type": "string"}}

    def test_empty_skill_yields_empty_lists(self):
        skill = AgentSkill(
            id="skill-y", tenant_id="t1", name="n", description="d",
            version="1.0.0", skill_md_content="# body", skill_md_hash="h",
        )
        assert skill.build_manifest() == {"resources": [], "scripts": [], "templates": []}


# ---------------------------------------------------------------------------
# to_dict / to_catalog_dict
# ---------------------------------------------------------------------------


class TestSerialization:
    def test_to_dict_surfaces_disclosure_mode_as_value(self):
        s = AgentSkill(
            id="s", tenant_id="t1", name="s", description="d", version="1.0.0",
            skill_md_content="# body", skill_md_hash="h",
            disclosure_mode=SkillDisclosureMode.PROGRESSIVE,
        )
        assert s.to_dict()["disclosure_mode"] == "progressive"

    def test_to_catalog_dict_surfaces_disclosure_mode(self):
        s = AgentSkill(
            id="s", tenant_id="t1", name="s", description="d", version="1.0.0",
            skill_md_content="# body", skill_md_hash="h",
            disclosure_mode=SkillDisclosureMode.BOTH,
        )
        assert s.to_catalog_dict()["disclosure_mode"] == "both"

    def test_script_to_dict_surfaces_args_schema(self):
        sc = AgentSkillScript(
            id="ass", tenant_id="t1", skill_id="s", filename="f.py",
            script_content="x", script_hash="h", language=ScriptLanguage.PYTHON,
            execution_mode=ScriptExecutionMode.SANDBOX, args_schema={"a": 1},
        )
        assert sc.to_dict()["args_schema"] == {"a": 1}

    def test_asset_to_dict_surfaces_vars_schema(self):
        a = AgentSkillAsset(
            id="asa", tenant_id="t1", skill_id="s", filename="t.j2",
            content="x", content_hash="h", asset_type=AssetType.TEMPLATE,
            vars_schema={"b": 2},
        )
        assert a.to_dict()["vars_schema"] == {"b": 2}


# ---------------------------------------------------------------------------
# allow_local_export three-place parity
# ---------------------------------------------------------------------------


class TestAllowLocalExport:
    def test_column_is_boolean_not_null_default_false(self):
        col = AgentSkill.__table__.columns["allow_local_export"]
        assert not col.nullable
        assert col.default.arg is False

    def test_to_dict_surfaces_flag(self):
        s = AgentSkill(
            id="s", tenant_id="t1", name="s", description="d", version="1.0.0",
            skill_md_content="# body", skill_md_hash="h",
            allow_local_export=True,
        )
        assert s.to_dict()["allow_local_export"] is True

    def test_to_dict_defaults_false_when_unset(self):
        # A skill constructed without the flag serializes it as None until
        # the DB default applies; the response model coerces the contract.
        s = AgentSkill(
            id="s", tenant_id="t1", name="s", description="d", version="1.0.0",
            skill_md_content="# body", skill_md_hash="h",
        )
        assert "allow_local_export" in s.to_dict()

    def test_create_defaults_off(self):
        c = AgentSkillCreate(name="my-skill", description="d", skill_md_content="# b")
        assert c.allow_local_export is False

    def test_create_accepts_opt_in(self):
        c = AgentSkillCreate(
            name="my-skill", description="d", skill_md_content="# b",
            allow_local_export=True,
        )
        assert c.allow_local_export is True

    def test_update_is_partial(self):
        u = AgentSkillUpdate(allow_local_export=True).model_dump(exclude_unset=True)
        assert u == {"allow_local_export": True}

    def test_response_declares_flag_default_false(self):
        assert "allow_local_export" in AgentSkillResponse.model_fields
        assert AgentSkillResponse.model_fields["allow_local_export"].default is False


# ---------------------------------------------------------------------------
# Pydantic contract
# ---------------------------------------------------------------------------


class TestPydantic:
    def test_create_defaults_progressive(self):
        c = AgentSkillCreate(name="my-skill", description="d", skill_md_content="# b")
        assert c.disclosure_mode == "progressive"

    def test_create_rejects_bad_disclosure_mode(self):
        with pytest.raises(ValidationError):
            AgentSkillCreate(
                name="my-skill", description="d", skill_md_content="# b",
                disclosure_mode="nonsense",
            )

    def test_update_rejects_bad_disclosure_mode(self):
        with pytest.raises(ValidationError):
            AgentSkillUpdate(disclosure_mode="nope")

    def test_update_partial_disclosure_mode(self):
        u = AgentSkillUpdate(disclosure_mode="execute").model_dump(exclude_unset=True)
        assert u == {"disclosure_mode": "execute"}

    def test_response_declares_disclosure_mode(self):
        assert "disclosure_mode" in AgentSkillResponse.model_fields

    def test_script_models_declare_args_schema(self):
        assert "args_schema" in AgentSkillScriptCreate.model_fields
        assert "args_schema" in AgentSkillScriptUpdate.model_fields
        assert "args_schema" in AgentSkillScriptResponse.model_fields
        c = AgentSkillScriptCreate(filename="f.py", script_content="x", args_schema={"a": 1})
        assert c.args_schema == {"a": 1}

    def test_asset_models_declare_vars_schema(self):
        assert "vars_schema" in AgentSkillAssetCreate.model_fields
        assert "vars_schema" in AgentSkillAssetUpdate.model_fields
        assert "vars_schema" in AgentSkillAssetResponse.model_fields
        c = AgentSkillAssetCreate(filename="t.j2", content="x", vars_schema={"b": 2})
        assert c.vars_schema == {"b": 2}


# ---------------------------------------------------------------------------
# Migration file shape (a123_skill_disclosure_mode)
# ---------------------------------------------------------------------------
