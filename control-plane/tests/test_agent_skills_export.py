# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for SKILL.md-portable export (F2).

Covers the pure helpers in `agent_skills_export.py`:
  * canonical file-list assembly
  * bundle hash determinism (same inputs → same hex digest)
  * frontmatter injection: SKILL.md gains brutor-* provenance keys
    while keeping the spec-required name/description/etc.
  * ZIP build is deterministic (mtime pinned) and round-trippable
  * source-tracked skills carry brutor-source-* keys; manual skills don't
  * sanitize_filename / safe_filename strip unsafe paths

These exercise the pure module-level functions directly so we don't
need a FastAPI TestClient; the route layer is just plumbing on top.

Run: pytest tests/test_agent_skills_export.py -v
"""

from __future__ import annotations

import io
import os
import zipfile

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from datetime import datetime, timezone

import pytest

import app.database.models  # noqa: F401 — registers all tables

from app.api.v1.endpoints.admin.agent_skills_export import (  # noqa: E402
    build_canonical_file_list,
    build_zip,
    hash_canonical_files,
    inject_frontmatter_extras,
    render_skill_md_initial,
    safe_filename,
    sanitize_filename,
    slug_dir,
    yaml_escape,
)
from app.constants.agent_skill import (  # noqa: E402
    AssetType,
    ScriptExecutionMode,
    ScriptLanguage,
    SkillResourceType,
    SkillStatus,
)
from app.database.models.agent_skill import AgentSkill  # noqa: E402
from app.database.models.agent_skill_asset import AgentSkillAsset  # noqa: E402
from app.database.models.agent_skill_resource import AgentSkillResource  # noqa: E402
from app.database.models.agent_skill_script import AgentSkillScript  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers — build a fake skill graph without a DB
# ---------------------------------------------------------------------------


def _make_skill(
    *,
    skill_id="ask-1",
    tenant_id="t1",
    name="incident-report",
    description="Generate an incident report.",
    version="1.0.0",
    body="# Incident Report\nFollow the template.\n",
    license_id="MIT",
    allowed_tools=None,
    compatibility=None,
    metadata=None,
    source_repo_id=None,
    source_commit_sha=None,
    source_path=None,
):
    s = AgentSkill(
        id=skill_id,
        tenant_id=tenant_id,
        name=name,
        description=description,
        version=version,
        license=license_id,
        allowed_tools=allowed_tools,
        compatibility=compatibility,
        skill_md_content=body,
        skill_md_hash="x" * 64,  # not used by export
        skill_metadata=metadata,
        status=SkillStatus.PUBLISHED,
        enabled=True,
        source_repo_id=source_repo_id,
        source_commit_sha=source_commit_sha,
        source_path=source_path,
    )
    return s


def _make_resource(skill_id, filename, content):
    return AgentSkillResource(
        id=f"asr-{filename}",
        tenant_id="t1",
        skill_id=skill_id,
        filename=filename,
        resource_type=SkillResourceType.REFERENCE,
        content=content,
        content_hash="y" * 64,
        size_bytes=len(content),
    )


def _make_script(skill_id, filename, content, lang=ScriptLanguage.PYTHON):
    return AgentSkillScript(
        id=f"ass-{filename}",
        tenant_id="t1",
        skill_id=skill_id,
        filename=filename,
        script_content=content,
        script_hash="z" * 64,
        language=lang,
        execution_mode=ScriptExecutionMode.SANDBOX,
    )


def _make_asset(skill_id, filename, content, encoding="utf-8"):
    return AgentSkillAsset(
        id=f"asa-{filename}",
        tenant_id="t1",
        skill_id=skill_id,
        filename=filename,
        asset_type=AssetType.DATA,
        content=content,
        content_hash="w" * 64,
        encoding=encoding,
        size_bytes=len(content),
    )


# ---------------------------------------------------------------------------
# Filename safety helpers
# ---------------------------------------------------------------------------


def test_sanitize_filename_strips_unsafe_chars():
    # Trailing underscores are stripped (cosmetic — avoids "name_.zip"
    # filenames). The trailing '!' becomes '_' and is then trimmed.
    assert sanitize_filename("Incident Report!") == "Incident_Report"
    assert sanitize_filename("legit-name") == "legit-name"
    assert sanitize_filename("") == "skill"
    assert sanitize_filename("/etc/passwd") == "etc_passwd"


def test_safe_filename_blocks_path_traversal():
    # A nested path collapses to the basename.
    assert safe_filename("../../etc/passwd") == "passwd"
    # Reserved chars get munged but the file isn't dropped.
    assert "/" not in safe_filename("foo/bar.txt")


def test_slug_dir_matches_skill_name_after_sanitisation():
    assert slug_dir("legit") == "legit"
    assert slug_dir("with space") == "with_space"


# ---------------------------------------------------------------------------
# YAML escape
# ---------------------------------------------------------------------------


def test_yaml_escape_bare_for_plain_values():
    assert yaml_escape("plain") == "plain"
    assert yaml_escape("alpha-numeric_123") == "alpha-numeric_123"


def test_yaml_escape_quotes_special_chars():
    assert yaml_escape("has: colon").startswith('"')
    assert yaml_escape("has # hash").startswith('"')
    assert yaml_escape("a\nb") == '"a\\nb"'


def test_yaml_escape_none_renders_empty_string():
    assert yaml_escape(None) == '""'


# ---------------------------------------------------------------------------
# render_skill_md_initial
# ---------------------------------------------------------------------------


def test_render_skill_md_initial_includes_required_frontmatter():
    s = _make_skill()
    rendered = render_skill_md_initial(s)
    assert rendered.startswith("---\n")
    # Frontmatter required fields
    assert "name: incident-report" in rendered
    assert "description: Generate an incident report." in rendered
    assert "version: 1.0.0" in rendered
    assert "license: MIT" in rendered
    # Body is preserved
    assert "# Incident Report" in rendered


def test_render_skill_md_initial_emits_allowed_tools_as_space_string():
    s = _make_skill(allowed_tools=["Bash(git:*)", "Read", "Write"])
    rendered = render_skill_md_initial(s)
    # The spec stores allowed-tools as space-delimited; the value is YAML-
    # escaped (quoted) because it contains a colon.
    assert 'allowed-tools: "Bash(git:*) Read Write"' in rendered


def test_render_skill_md_initial_emits_metadata_block():
    s = _make_skill(metadata={"author": "Jane Doe", "tier": "premium"})
    rendered = render_skill_md_initial(s)
    assert "metadata:" in rendered
    assert "author:" in rendered
    assert "tier:" in rendered


# ---------------------------------------------------------------------------
# inject_frontmatter_extras
# ---------------------------------------------------------------------------


def test_inject_frontmatter_extras_adds_brutor_keys():
    s = _make_skill()
    initial = render_skill_md_initial(s)
    out = inject_frontmatter_extras(
        initial,
        skill=s,
        bundle_sha="deadbeef" * 8,  # 64-char hex
    )
    assert "brutor-skill-id: ask-1" in out
    assert "brutor-tenant-id: t1" in out
    assert "brutor-skill-version: 1.0.0" in out
    assert "brutor-bundle-sha256: " + "deadbeef" * 8 in out
    # brutor-exported-at: ISO8601 UTC stamp
    assert "brutor-exported-at: " in out
    # Original frontmatter still present
    assert "name: incident-report" in out
    # Body still appended
    assert "# Incident Report" in out


def test_inject_frontmatter_extras_skips_source_keys_for_manual_skill():
    s = _make_skill()  # no source_repo_id
    out = inject_frontmatter_extras(
        render_skill_md_initial(s),
        skill=s,
        bundle_sha="a" * 64,
    )
    assert "brutor-source-repo-id" not in out
    assert "brutor-source-commit-sha" not in out
    assert "brutor-source-path" not in out


def test_inject_frontmatter_extras_includes_source_keys_for_synced_skill():
    s = _make_skill(
        source_repo_id="sks-anthropic",
        source_commit_sha="cafef00d" * 5,
        source_path="skills/incident-report/SKILL.md",
    )
    out = inject_frontmatter_extras(
        render_skill_md_initial(s),
        skill=s,
        bundle_sha="b" * 64,
    )
    assert "brutor-source-repo-id: sks-anthropic" in out
    assert "brutor-source-commit-sha: " + ("cafef00d" * 5) in out
    assert "brutor-source-path: skills/incident-report/SKILL.md" in out


# ---------------------------------------------------------------------------
# build_canonical_file_list
# ---------------------------------------------------------------------------


def test_canonical_file_list_orders_deterministically():
    s = _make_skill()
    # Pass children in reversed order to verify sort applies.
    resources = [_make_resource(s.id, "z.md", "z"), _make_resource(s.id, "a.md", "a")]
    scripts = [_make_script(s.id, "run.py", "print('hi')")]
    assets = [_make_asset(s.id, "tpl.txt", "hello")]
    files = build_canonical_file_list(
        skill=s,
        resources=resources,
        scripts=scripts,
        assets=assets,
    )
    paths = [p for p, _ in files]
    # SKILL.md is always first.
    assert paths[0].endswith("/SKILL.md")
    # References sorted alphabetically (a.md before z.md)
    ref_paths = [p for p in paths if "references/" in p]
    assert ref_paths == sorted(ref_paths)


def test_canonical_file_list_decodes_base64_assets():
    import base64

    s = _make_skill()
    # Pretend we have a base64-encoded binary asset.
    raw = b"\x89PNG\x0d\x0a\x1a\x0a"
    encoded = base64.b64encode(raw).decode("ascii")
    assets = [_make_asset(s.id, "logo.png", encoded, encoding="base64")]
    files = build_canonical_file_list(
        skill=s, resources=[], scripts=[], assets=assets
    )
    asset_entry = [(p, c) for p, c in files if "logo.png" in p][0]
    assert asset_entry[1] == raw  # base64 round-trip preserved


# ---------------------------------------------------------------------------
# hash_canonical_files
# ---------------------------------------------------------------------------


def test_hash_canonical_files_is_deterministic():
    files = [
        ("a/b/SKILL.md", b"hello"),
        ("a/b/scripts/run.py", b"print(1)"),
    ]
    h1 = hash_canonical_files(files)
    h2 = hash_canonical_files(list(files))  # different list object, same content
    assert h1 == h2
    assert len(h1) == 64  # SHA-256 hex


def test_hash_canonical_files_distinguishes_content_changes():
    h1 = hash_canonical_files([("a/SKILL.md", b"hello")])
    h2 = hash_canonical_files([("a/SKILL.md", b"goodbye")])
    assert h1 != h2


def test_hash_canonical_files_distinguishes_path_changes():
    # Same bytes at a different path must hash differently — otherwise
    # an attacker could rename a malicious script to look like an
    # innocent one without changing the hash.
    h1 = hash_canonical_files([("a/SKILL.md", b"x")])
    h2 = hash_canonical_files([("b/SKILL.md", b"x")])
    assert h1 != h2


# ---------------------------------------------------------------------------
# build_zip + full round-trip
# ---------------------------------------------------------------------------


def test_build_zip_round_trip_preserves_layout():
    s = _make_skill()
    resources = [_make_resource(s.id, "CHANGELOG.md", "# Changelog")]
    scripts = [_make_script(s.id, "run.py", "print('hi')")]
    assets = [_make_asset(s.id, "tpl.txt", "Template body")]

    files = build_canonical_file_list(
        skill=s, resources=resources, scripts=scripts, assets=assets
    )
    bundle_sha = hash_canonical_files(files)
    # Rewrite SKILL.md with the bundle hash (matches the endpoint flow)
    files[0] = (
        files[0][0],
        inject_frontmatter_extras(
            files[0][1].decode("utf-8"), skill=s, bundle_sha=bundle_sha
        ).encode("utf-8"),
    )
    zip_bytes = build_zip(s.name, files)

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = sorted(zf.namelist())
        assert "incident-report/SKILL.md" in names
        assert "incident-report/scripts/run.py" in names
        assert "incident-report/references/CHANGELOG.md" in names
        assert "incident-report/assets/tpl.txt" in names

        # SKILL.md must carry the brutor-* frontmatter
        with zf.open("incident-report/SKILL.md") as f:
            body = f.read().decode("utf-8")
            assert "brutor-skill-id: ask-1" in body
            assert f"brutor-bundle-sha256: {bundle_sha}" in body


def test_build_zip_is_byte_deterministic_across_runs():
    s = _make_skill()
    files = build_canonical_file_list(
        skill=s, resources=[], scripts=[], assets=[]
    )
    z1 = build_zip(s.name, files)
    z2 = build_zip(s.name, files)
    assert z1 == z2, "two builds with identical inputs must produce byte-identical ZIPs"
