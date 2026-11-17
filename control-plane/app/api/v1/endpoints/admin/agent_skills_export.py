# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Agent Skill Export — emit a SKILL.md-portable ZIP bundle.

Given a Brutor `agent_skill` row + its sibling resources/scripts/assets,
build a ZIP that drops cleanly into `~/.claude/skills/`, `~/.cursor/skills/`,
Codex, VS Code, Kiro, Goose — i.e. anywhere the Anthropic-style Agent
Skills spec is supported.

# Bundle layout

    incident-report/
        SKILL.md          # body w/ injected frontmatter (brutor-* fields)
        scripts/
            run.py
        references/
            CHANGELOG.md
        assets/
            template.txt   # text-as-is
            logo.png       # binary, was base64 in DB → decoded here

# Why frontmatter injection (not a sidecar manifest)

The spec stores everything an external client needs in SKILL.md's YAML
frontmatter. Sidecar files (.brutor-manifest.json etc.) get stripped
by every ingest tool that doesn't know about them. Injecting our
provenance fields keeps the bundle 100% spec-compliant AND traceable
back to its Brutor origin if it ever needs re-import or audit.

# Bundle hash

SHA-256 over the canonical (path, bytes) list of every file in the ZIP.
Same algorithm Obot uses for its InstallHash. The hash is also
emitted into the frontmatter as `brutor-bundle-sha256` so a future
re-import can detect tampering.
"""

from __future__ import annotations

import hashlib
import io
import re
import zipfile
from datetime import datetime, timezone
from typing import List, Tuple, Union

import base64

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import and_
from sqlalchemy.orm import Session

from app.constants.tenant import TenantResourceTypes
from app.core.admin_user.dependencies import (
    require_permission,
    require_tenant_resource_access,
)
from app.core.admin_user.permissions import Permission
from app.core.logging_system import BrutorLogger
from app.database import get_db
from app.database.models.admin import TenantAdmin
from app.core.admin_user.admin_principal import AdminPrincipal
from app.database.models.agent_skill import AgentSkill
from app.database.models.agent_skill_asset import AgentSkillAsset
from app.database.models.agent_skill_resource import AgentSkillResource
from app.database.models.agent_skill_script import AgentSkillScript


# Names that are reserved by the spec layout — we reject these as
# filenames at the manifest layer (in F1 ingest) but also defensively
# here so an old/buggy DB row can't generate a malformed ZIP.
_SAFE_FILENAME_RE = re.compile(r"^[A-Za-z0-9._\-]+$")


def create_agent_skills_export_router(logger: BrutorLogger) -> APIRouter:
    router = APIRouter(
        prefix="/admin/agent-skills",
        tags=["Agent Skills Admin"],
    )

    @router.post(
        "/{skill_id}/export",
        summary="Download skill as portable SKILL.md ZIP bundle",
        responses={
            200: {
                "content": {"application/zip": {}},
                "description": "ZIP archive containing SKILL.md + sibling files",
            }
        },
    )
    def export_skill(
        skill_id: str,
        db: Session = Depends(get_db),
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.AGENT_SKILLS)
        ),
        admin: AdminPrincipal = Depends(
            require_permission(Permission.AGENT_SKILL_READ)
        ),
    ):
        admin_row, tenant_id, _ = admin_and_tenant
        skill = (
            db.query(AgentSkill)
            .filter(
                and_(AgentSkill.id == skill_id, AgentSkill.tenant_id == tenant_id)
            )
            .first()
        )
        if not skill:
            raise HTTPException(status_code=404, detail="agent skill not found")

        resources: List[AgentSkillResource] = (
            db.query(AgentSkillResource)
            .filter(AgentSkillResource.skill_id == skill_id)
            .all()
        )
        scripts: List[AgentSkillScript] = (
            db.query(AgentSkillScript)
            .filter(AgentSkillScript.skill_id == skill_id)
            .all()
        )
        assets: List[AgentSkillAsset] = (
            db.query(AgentSkillAsset)
            .filter(AgentSkillAsset.skill_id == skill_id)
            .all()
        )

        # Build the canonical file list FIRST (sorted by path, bytes
        # included) so the bundle hash is reproducible regardless of
        # DB row iteration order.
        files = build_canonical_file_list(
            skill=skill,
            resources=resources,
            scripts=scripts,
            assets=assets,
        )

        bundle_sha = hash_canonical_files(files)

        # Now re-emit the SKILL.md with the bundle hash injected. We
        # know the SKILL.md entry is at index 0 — it was prepended.
        files[0] = (
            files[0][0],
            inject_frontmatter_extras(
                files[0][1].decode("utf-8"),
                skill=skill,
                bundle_sha=bundle_sha,
            ).encode("utf-8"),
        )

        zip_bytes = build_zip(skill.name, files)

        logger.info(
            "agent_skill exported",
            context={
                "skill_id": skill.id,
                "skill_name": skill.name,
                "version": skill.version,
                "tenant_id": tenant_id,
                "bundle_sha": bundle_sha[:16],  # log a prefix only
                "admin_id": admin_row.id,
            },
        )

        filename = f"{sanitize_filename(skill.name)}-{skill.version}.zip"
        return StreamingResponse(
            io.BytesIO(zip_bytes),
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "X-Brutor-Bundle-Sha256": bundle_sha,
            },
        )

    return router


# ----------------------------------------------------------------------
# File assembly
# ----------------------------------------------------------------------


def build_canonical_file_list(
    *,
    skill: AgentSkill,
    resources: List[AgentSkillResource],
    scripts: List[AgentSkillScript],
    assets: List[AgentSkillAsset],
) -> List[Tuple[str, bytes]]:
    """Return [(relative_path, bytes)] sorted deterministically.

    SKILL.md is prepended at index 0 so the caller can rewrite it
    after computing the bundle hash."""
    out: List[Tuple[str, bytes]] = []

    # SKILL.md goes first. The frontmatter injection happens AFTER we
    # know the bundle hash — caller rewrites this entry.
    skill_md = render_skill_md_initial(skill).encode("utf-8")
    out.append((f"{slug_dir(skill.name)}/SKILL.md", skill_md))

    slug = slug_dir(skill.name)

    for r in sorted(resources, key=lambda x: x.filename):
        path = export_path_for(slug, "references", r.filename)
        content = r.content or ""
        out.append((path, content.encode("utf-8")))

    for s in sorted(scripts, key=lambda x: x.filename):
        path = export_path_for(slug, "scripts", s.filename)
        content = s.script_content or ""
        out.append((path, content.encode("utf-8")))

    for a in sorted(assets, key=lambda x: x.filename):
        path = export_path_for(slug, "assets", a.filename)
        # Binary assets are stored base64 in the DB; text assets as-is.
        raw = a.content or ""
        if (a.encoding or "utf-8").lower() == "base64":
            try:
                payload = base64.b64decode(raw)
            except Exception:
                # Defensive: a malformed base64 should not 500 the
                # whole export. Drop the asset and warn — operator
                # sees the missing file in the ZIP and can re-upload.
                payload = b""
        else:
            payload = raw.encode("utf-8")
        out.append((path, payload))

    return out


def hash_canonical_files(files: List[Tuple[str, bytes]]) -> str:
    """SHA-256 over a stable serialisation: for each file, the path,
    a NUL separator, a 4-byte big-endian length, and the bytes. Stable
    across re-orderings (caller guarantees sort) and resistant to
    "two different bundles hash the same" collisions in the cheap way
    a length-less concat would risk.
    """
    h = hashlib.sha256()
    for path, content in files:
        h.update(path.encode("utf-8"))
        h.update(b"\x00")
        h.update(len(content).to_bytes(4, "big"))
        h.update(content)
    return h.hexdigest()


def build_zip(skill_name: str, files: List[Tuple[str, bytes]]) -> bytes:
    """Standard ZIP, deflate-compressed. Caller already canonicalised
    paths so the on-disk extraction order is reproducible."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path, content in files:
            info = zipfile.ZipInfo(path)
            # Pin the mtime so two exports of the same skill produce
            # byte-identical ZIPs. Picking a fixed epoch-ish date
            # avoids "the bundle differs because of timestamps" diffs
            # in operator workflows that track skill bundles in git.
            info.date_time = (2026, 1, 1, 0, 0, 0)
            info.external_attr = 0o644 << 16
            zf.writestr(info, content)
    return buf.getvalue()


# ----------------------------------------------------------------------
# SKILL.md rendering
# ----------------------------------------------------------------------


def render_skill_md_initial(skill: AgentSkill) -> str:
    """First pass — frontmatter from the DB row + the stored body.

    Bundle-hash injection is a SECOND pass once the hash is known
    (build_canonical_file_list returns this version, caller rewrites
    it after hash_canonical_files()).
    """
    fm_lines = [
        "---",
        f"name: {skill.name}",
        f"description: {yaml_escape(skill.description)}",
    ]
    if skill.version:
        fm_lines.append(f"version: {skill.version}")
    if skill.license:
        fm_lines.append(f"license: {yaml_escape(skill.license)}")
    if skill.compatibility:
        # Compatibility is stored as a JSON list; emit as YAML flow seq.
        if isinstance(skill.compatibility, list):
            fm_lines.append(
                "compatibility: ["
                + ", ".join(yaml_escape(c) for c in skill.compatibility)
                + "]"
            )
    if skill.allowed_tools:
        # Spec stores allowed-tools as a space-delimited string; we
        # have a JSON list internally. Convert back.
        if isinstance(skill.allowed_tools, list):
            fm_lines.append(
                f"allowed-tools: {yaml_escape(' '.join(skill.allowed_tools))}"
            )

    # User-facing metadata block (the spec's `metadata:` field). We
    # only include keys that don't collide with our brutor-* namespace.
    meta = dict(skill.skill_metadata or {})
    if meta:
        fm_lines.append("metadata:")
        for k in sorted(meta.keys()):
            v = meta[k]
            if isinstance(v, (str, int, float, bool)):
                fm_lines.append(f"  {k}: {yaml_escape(str(v))}")

    fm_lines.append("---")
    fm = "\n".join(fm_lines) + "\n\n"
    body = skill.skill_md_content or ""
    return fm + body


def inject_frontmatter_extras(
    skill_md: str,
    *,
    skill: AgentSkill,
    bundle_sha: str,
) -> str:
    """Inject Brutor provenance keys into the existing frontmatter
    block. Matches Obot's `obot-skill-id` pattern — same idea, our
    namespace.

    The first `---` ... `---` is the frontmatter; we re-render it
    with our extras prepended (closer to the top so editors that
    summarise the first ~20 lines surface the provenance).
    """
    lines = skill_md.split("\n")
    if not lines or lines[0].strip() != "---":
        # Defensive — render_skill_md_initial always emits frontmatter.
        # If something upstream changed, append a plain block.
        return skill_md

    # Find the closing `---`.
    close_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            close_idx = i
            break
    if close_idx is None:
        return skill_md

    now = datetime.now(timezone.utc).isoformat()
    extras = [
        f"brutor-skill-id: {skill.id}",
        f"brutor-tenant-id: {skill.tenant_id}",
        f"brutor-skill-version: {skill.version}",
        f"brutor-exported-at: {now}",
        f"brutor-bundle-sha256: {bundle_sha}",
    ]
    # Add source provenance if present (skill was discovered from
    # a SkillSource).
    if skill.source_repo_id:
        extras.append(f"brutor-source-repo-id: {skill.source_repo_id}")
    if skill.source_commit_sha:
        extras.append(f"brutor-source-commit-sha: {skill.source_commit_sha}")
    if skill.source_path:
        extras.append(f"brutor-source-path: {skill.source_path}")

    new_lines = (
        [lines[0]]                     # opening ---
        + extras                        # our provenance block
        + lines[1:close_idx]            # original frontmatter contents
        + lines[close_idx:]             # closing --- + body
    )
    return "\n".join(new_lines)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def sanitize_filename(name: str) -> str:
    """Map a skill name to a filesystem-safe form. Same constraints
    as Obot — lowercase letters, digits, hyphens, underscores."""
    return re.sub(r"[^a-zA-Z0-9._\-]", "_", name).strip("_") or "skill"


def slug_dir(name: str) -> str:
    """Top-level directory inside the ZIP. Must match the skill name
    per the spec (the validator on ingest also enforces this)."""
    return sanitize_filename(name)


def safe_filename(name: str) -> str:
    """Reject path-traversal and reserved tokens. Caller already
    validated at write time; defensive recheck here.

    NOTE: this flattens to the basename and is retained only for
    back-compat callers. Bundled-file export uses
    `safe_relative_path()` instead so nested files (e.g.
    `scripts/office/foo.py`) keep their directory structure on
    round-trip."""
    base = name.rsplit("/", 1)[-1]
    if not _SAFE_FILENAME_RE.match(base):
        # Replace any unsafe chars with underscores rather than rejecting
        # outright — losing a file from the bundle is worse than munging
        # its name. Truncate to 255 to keep within typical FS limits.
        base = re.sub(r"[^A-Za-z0-9._\-]", "_", base)[:255]
    return base


def safe_relative_path(name: str) -> str:
    """Sanitise a stored filename into a safe relative path, PRESERVING
    internal directory separators so nested bundled files round-trip.

    Blocks path traversal (`..`, leading `/`, backslashes) and munges
    unsafe characters per-segment. Empty / all-stripped paths collapse
    to a safe placeholder so a file is never silently dropped.

    Examples:
        'scripts/office/foo.py'  → 'scripts/office/foo.py'
        'agents/grader.md'       → 'agents/grader.md'
        '../etc/passwd'          → 'etc/passwd'   (traversal stripped)
        '/abs/path'              → 'abs/path'
    """
    # Normalise separators, drop empties, drop traversal + current-dir.
    parts = []
    for seg in name.replace("\\", "/").split("/"):
        seg = seg.strip()
        if seg in ("", ".", ".."):
            continue
        if not _SAFE_FILENAME_RE.match(seg):
            seg = re.sub(r"[^A-Za-z0-9._\-]", "_", seg)[:255]
        parts.append(seg)
    return "/".join(parts) or "file"


def export_path_for(slug: str, bucket: str, stored_filename: str) -> str:
    """Build the in-ZIP path for one bundled file.

    `stored_filename` may be:
      * root-relative incl. bucket dir — `scripts/office/foo.py`,
        `agents/grader.md` (source-sync + ZIP importer convention)
      * a bare basename — `foo.py` (manual create/edit convention)

    If it already contains a directory separator we treat it as
    root-relative and emit `<slug>/<filename>` verbatim. Otherwise we
    prefix the bucket directory so a manually-added basename still lands
    in the right place (`<slug>/<bucket>/<filename>`)."""
    rel = safe_relative_path(stored_filename)
    if "/" in rel:
        return f"{slug}/{rel}"
    return f"{slug}/{bucket}/{rel}"


def yaml_escape(value: str) -> str:
    """Cheap YAML scalar emission — quote if the value has YAML-special
    chars, else emit bare. We deliberately don't pull in PyYAML for
    one-direction emission; the formats we emit are simple key: value.
    """
    if value is None:
        return '""'
    s = str(value)
    if (
        ":" in s
        or "#" in s
        or s.startswith(("-", "?", "&", "*", "!", "|", ">", "'", '"', "%", "@", "`"))
        or s.strip() != s
        or "\n" in s
    ):
        # Double-quote with backslash escaping.
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
    return s
