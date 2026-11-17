# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# A skill's size, and its limit on every write path

A skill may hold at most `SKILL_MAX_BYTES` (30 MB) of files as they unpack:
SKILL.md, every resource and script, every asset (a base64 asset counts its
decoded bytes). The zip import, the resource / script / asset routes, a
change of SKILL.md, the skill-source ingest and the GitHub sync all refuse
a skill past it with the reason `too_large` and the words of `message`.
"""

from __future__ import annotations

import binascii
import base64
from typing import Iterable, Optional, Tuple

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.constants import agent_skill as skill_constants

REASON = "too_large"


def content_bytes(content: Optional[str], encoding: Optional[str] = None) -> int:
    """The bytes a stored file unpacks to: its UTF-8 text, or the decoded
    bytes of a base64 file."""
    if not content:
        return 0
    if (encoding or "").lower() == "base64":
        try:
            return len(base64.b64decode(content, validate=False))
        except (binascii.Error, ValueError):
            pass
    return len(content.encode("utf-8"))


def message(label: str, size: int, verb: str = "would hold") -> str:
    """The refusal's words, the same on every path."""
    return (
        f"The skill '{label}' {verb} {size} bytes, more than the per-skill limit "
        f"({skill_constants.SKILL_MAX_TEXT})"
    )


def too_large(size: int) -> bool:
    return size > skill_constants.SKILL_MAX_BYTES


def stored_bytes(db: Session, skill, *, without: Tuple[str, Optional[str]] = ("", None)) -> int:
    """The bytes the skill's stored files unpack to. `without` = (kind, id)
    leaves out one file (the one a write replaces); kind is `resource`,
    `script`, `asset` or `skill_md`."""
    from app.database.models.agent_skill_asset import AgentSkillAsset
    from app.database.models.agent_skill_resource import AgentSkillResource
    from app.database.models.agent_skill_script import AgentSkillScript

    kind, row_id = without
    total = 0 if kind == "skill_md" else content_bytes(skill.skill_md_content)

    def _rows(model, *columns):
        query = db.query(model.id, *columns).filter(model.skill_id == skill.id, model.tenant_id == skill.tenant_id)
        return query.all()

    for row_id_, content in _rows(AgentSkillResource, AgentSkillResource.content):
        if not (kind == "resource" and row_id_ == row_id):
            total += content_bytes(content)
    for row_id_, content in _rows(AgentSkillScript, AgentSkillScript.script_content):
        if not (kind == "script" and row_id_ == row_id):
            total += content_bytes(content)
    for row_id_, content, encoding in _rows(AgentSkillAsset, AgentSkillAsset.content, AgentSkillAsset.encoding):
        if not (kind == "asset" and row_id_ == row_id):
            total += content_bytes(content, encoding)
    return total


def refuse_if_too_large(
    db: Session,
    skill,
    new_bytes: int,
    *,
    replacing: Tuple[str, Optional[str]] = ("", None),
) -> None:
    """Refuse (413, reason `too_large`) a write that would make `skill`
    hold more than the limit: `new_bytes` added, the file `replacing`
    left out."""
    total = stored_bytes(db, skill, without=replacing) + new_bytes
    if too_large(total):
        raise HTTPException(
            status_code=413,
            detail={"reason": REASON, "error": message(skill.name, total)},
        )


def discovered_bytes(skill_md: str, files: Iterable) -> int:
    """The bytes a discovered skill (SKILL.md body and its files) unpacks to."""
    return content_bytes(skill_md) + sum(content_bytes(f.content, f.encoding) for f in files)
