# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Pydantic models for /v1/admin/skill-sources REST API + ingest payload.

Models for the operator-facing CRUD endpoints + the internal
/v1/admin/skill-sources/{id}/sync-ingest endpoint the Rust scheduler
POSTs to with discovered skills.

# Why the ingest endpoint takes a typed payload instead of raw JSON

The Rust scheduler is doing the heavy lifting (cloning the repo,
walking the tree, parsing YAML frontmatter, hashing files). The
Python side trusts that input *structurally* (it's our own
gateway-core talking) but still validates field bounds so a buggy
scheduler can't poison the DB with 10MB description fields.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.database.models.skill_source import (
    ALLOWED_SYNC_STATUSES,
    DEFAULT_SYNC_INTERVAL_SECONDS,
    MAX_SYNC_INTERVAL_SECONDS,
    MIN_SYNC_INTERVAL_SECONDS,
)


# ----------------------------------------------------------------------
# Shared helpers
# ----------------------------------------------------------------------

_REPO_URL_RE = re.compile(
    r"^https://github\.com/[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+(?:\.git)?/?$"
)
_GIT_REF_RE = re.compile(r"^[A-Za-z0-9._\-/]{1,255}$")
_SKILL_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9\-]{0,62}[a-z0-9])?$")


def _validate_repo_url(value: str) -> str:
    """Today we only support github.com — the Rust scheduler walks
    GitHub's REST tree API. GitLab / Bitbucket / self-hosted git would
    each need its own walker; we'd rather fail loud at create-time
    than pretend the URL works and silently never sync.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError("repo_url must be a non-empty string")
    cleaned = value.strip().rstrip("/")
    if not _REPO_URL_RE.match(cleaned):
        raise ValueError(
            "repo_url must be an https://github.com/<owner>/<repo> URL "
            "(GitLab / Bitbucket / self-hosted not yet supported)"
        )
    return cleaned


def _validate_git_ref(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("git_ref must be a non-empty string")
    cleaned = value.strip()
    # Git's actual ref name rules are a maze; this regex catches the
    # 99% case (branch / tag / SHA) and rejects shell-metacharacter
    # injection attempts. Scheduler does final validation against the
    # GitHub API.
    if not _GIT_REF_RE.match(cleaned):
        raise ValueError(
            "git_ref must contain only alphanumerics, dots, hyphens, "
            "underscores, and slashes"
        )
    return cleaned


# ----------------------------------------------------------------------
# Request models
# ----------------------------------------------------------------------


class SkillSourceCreate(BaseModel):
    """Admin POST body to register a new skill source."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=255)
    repo_url: str = Field(..., max_length=1024)
    git_ref: str = Field(default="main", max_length=255)
    subpath: Optional[str] = Field(default=None, max_length=512)
    enabled: bool = Field(default=True)
    sync_interval_seconds: int = Field(
        default=DEFAULT_SYNC_INTERVAL_SECONDS,
        ge=MIN_SYNC_INTERVAL_SECONDS,
        le=MAX_SYNC_INTERVAL_SECONDS,
    )
    auto_publish: bool = Field(default=False)
    github_token_secret_ref: Optional[str] = Field(default=None, max_length=512)

    @field_validator("repo_url")
    @classmethod
    def _v_repo_url(cls, v: str) -> str:
        return _validate_repo_url(v)

    @field_validator("git_ref")
    @classmethod
    def _v_git_ref(cls, v: str) -> str:
        return _validate_git_ref(v)


class SkillSourceUpdate(BaseModel):
    """Admin PATCH body. All fields optional. `repo_url` + `git_ref`
    are immutable after create (drop the source and re-register if
    you want a different target — keeps re-ingest semantics simple)."""

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    subpath: Optional[str] = Field(default=None, max_length=512)
    enabled: Optional[bool] = None
    sync_interval_seconds: Optional[int] = Field(
        default=None,
        ge=MIN_SYNC_INTERVAL_SECONDS,
        le=MAX_SYNC_INTERVAL_SECONDS,
    )
    auto_publish: Optional[bool] = None
    github_token_secret_ref: Optional[str] = Field(default=None, max_length=512)


# ----------------------------------------------------------------------
# Response models
# ----------------------------------------------------------------------


class SkillSourceResponse(BaseModel):
    """Full skill-source row for admin UI display."""

    model_config = ConfigDict(extra="ignore")

    id: str
    tenant_id: str
    name: str
    repo_url: str
    git_ref: str
    subpath: Optional[str] = None
    enabled: bool
    sync_interval_seconds: int
    auto_publish: bool
    status: str
    last_synced_at: Optional[str] = None
    last_attempted_at: Optional[str] = None
    last_commit_sha: Optional[str] = None
    sync_error: Optional[str] = None
    discovered_skill_count: int = 0
    github_token_secret_ref: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    created_by: Optional[str] = None


class SkillSourceListResponse(BaseModel):
    sources: List[SkillSourceResponse]
    total: int


# ----------------------------------------------------------------------
# Ingest payload (Rust scheduler → Python)
# ----------------------------------------------------------------------


class DiscoveredSkillFile(BaseModel):
    """One file the scheduler found within a discovered skill directory.
    `kind` mirrors the on-disk layout: SKILL.md, scripts/, references/,
    assets/.
    """

    model_config = ConfigDict(extra="forbid")

    path: str = Field(..., min_length=1, max_length=1024)
    # `skill_md` | `script` | `reference` | `asset`
    kind: str = Field(..., min_length=1, max_length=32)
    # Plain text OR base64 for binary assets (caller sets encoding).
    content: str = Field(..., max_length=10_000_000)  # 10 MB hard cap
    encoding: str = Field(default="utf-8", max_length=16)
    mime_type: Optional[str] = Field(default=None, max_length=200)


class DiscoveredSkill(BaseModel):
    """One discovered skill = one SKILL.md + sibling files. The
    scheduler has already parsed the YAML frontmatter, so we get
    typed fields here instead of having to re-parse YAML server-side.
    """

    model_config = ConfigDict(extra="forbid")

    # The relative path of the SKILL.md within the repo, used as the
    # stable key for matching re-syncs to existing rows.
    skill_path: str = Field(..., min_length=1, max_length=1024)
    # Parsed frontmatter:
    name: str = Field(..., min_length=1, max_length=64)
    description: str = Field(..., min_length=1, max_length=1024)
    version: Optional[str] = Field(default=None, max_length=50)
    license: Optional[str] = Field(default=None, max_length=100)
    compatibility: Optional[List[str]] = None
    allowed_tools: Optional[List[str]] = None
    skill_metadata: Optional[Dict[str, Any]] = None
    # Body text below the frontmatter:
    skill_md_body: str = Field(..., max_length=5_000_000)  # 5 MB
    # Sibling files keyed by relative path:
    files: List[DiscoveredSkillFile] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _v_name(cls, v: str) -> str:
        # Same constraint the agent_skills table enforces — 1-64 chars,
        # lowercase a-z/0-9/hyphens, no leading/trailing hyphen.
        if not _SKILL_NAME_RE.match(v):
            raise ValueError(
                "skill name must be 1-64 chars of lowercase a-z, 0-9, "
                "or hyphens (no leading/trailing/consecutive hyphens)"
            )
        return v


class SkillSourceIngestRequest(BaseModel):
    """The full payload the scheduler posts after walking a repo.

    `commit_sha` is the SHA the scheduler resolved the ref to — every
    skill below carries that same SHA into `agent_skills.source_commit_sha`.
    If the scheduler can't resolve the ref it should report an error
    via /sync-error instead of posting this with a null sha.
    """

    model_config = ConfigDict(extra="forbid")

    commit_sha: str = Field(..., min_length=4, max_length=64)
    discovered: List[DiscoveredSkill] = Field(default_factory=list)
    # Skill paths that exist in the DB tagged to this source but were
    # NOT found in this sync run. Caller may decide to mark them
    # deprecated rather than delete (we want to preserve execution
    # history). For now, the Python side just reports them in the
    # response — actual deprecation is a follow-up admin action.
    removed_paths: List[str] = Field(default_factory=list)


class SkillSourceIngestSummary(BaseModel):
    """What the ingest endpoint returns to the scheduler."""

    model_config = ConfigDict(extra="ignore")

    source_id: str
    commit_sha: str
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0
    errors: List[str] = Field(default_factory=list)


class SkillSourceSyncErrorRequest(BaseModel):
    """Posted by the scheduler when a sync attempt fails before it
    can produce an ingest payload (network error, parse error, etc.).
    Records the failure on the row so the admin UI shows it."""

    model_config = ConfigDict(extra="forbid")

    message: str = Field(..., min_length=1, max_length=8000)
