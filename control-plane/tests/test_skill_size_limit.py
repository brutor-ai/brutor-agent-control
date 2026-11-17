# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
A skill's size limit is one constant, `SKILL_MAX_BYTES`
(`app/constants/agent_skill.py`): 30 MB (30,000,000 bytes) of uncompressed
files per skill, Anthropic Agent Skills' limit. A skill of a zip import is
refused when what it unpacks to is larger: the uncompressed size is read
from the zip's directory before any file is decompressed, so a small zip
that unpacks to far more (a zip bomb) is never read into memory.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from app.constants import agent_skill as constants
from app.core.logging_system import BrutorLogger
from tests.admin_endpoint_harness import build_admin_harness


@pytest.fixture()
def h():
    from app.api.v1.endpoints.admin.agent_skills import create_agent_skills_admin_router

    harness = build_admin_harness([create_agent_skills_admin_router(logger=BrutorLogger("test-skill-size"))])
    yield harness
    harness.close()


def _zip(name: str, extra: bytes = b"") -> tuple:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{name}/SKILL.md", f"---\nname: {name}\ndescription: The {name} skill\n---\n# {name}\n")
        if extra:
            zf.writestr(f"{name}/references/big.md", extra)
    return (f"{name}.zip", buffer.getvalue(), "application/zip")


def _import(h, upload):
    r = h.client.post("/v1/admin/agent-skills/import", files=[("files", upload)], headers=h.headers())
    assert r.status_code == 200, r.text
    (result,) = r.json()["results"]
    return result


def test_the_limit_is_one_constant():
    assert constants.SKILL_MAX_BYTES == 30_000_000
    assert constants.SKILL_MAX_TEXT == "30 MB"
    assert (constants.ARCHIVE_MAX_BYTES, constants.ARCHIVE_MAX_SKILLS) == (300_000_000, 50)
    assert constants.ARCHIVE_MAX_TEXT == "300 MB"


def test_a_zip_within_the_limit_is_imported(h, monkeypatch):
    monkeypatch.setattr(constants, "SKILL_MAX_BYTES", 4096)
    assert _import(h, _zip("small", b"x" * 100))["success"] is True


def test_a_skill_larger_than_the_limit_is_refused(h, monkeypatch):
    monkeypatch.setattr(constants, "SKILL_MAX_BYTES", 2048)
    upload = _zip("random", __import__("random").Random(0).randbytes(4096))  # does not compress
    result = _import(h, upload)
    assert result["success"] is False and result["status"] == "refused" and result["reason"] == "too_large"
    assert "more than the per-skill limit (0.002048 MB)" in result["error"]


def test_a_zip_larger_than_the_archive_limit_is_refused_unopened(h, monkeypatch):
    monkeypatch.setattr(constants, "ARCHIVE_MAX_BYTES", 2048)
    upload = _zip("random", __import__("random").Random(0).randbytes(4096))
    result = _import(h, upload)
    assert result["status"] == "refused" and result["reason"] == "archive_too_large"
    assert "larger than the archive limit" in result["error"]


def test_a_zip_that_unpacks_past_the_limit_is_refused_unread(h, monkeypatch):
    monkeypatch.setattr(constants, "SKILL_MAX_BYTES", 64 * 1024)
    upload = _zip("bomb", b"\0" * (1024 * 1024))  # ~1 KiB zipped, 1 MiB unpacked
    assert len(upload[1]) < 64 * 1024
    read = []
    original = zipfile.ZipFile.open
    monkeypatch.setattr(zipfile.ZipFile, "open", lambda self, name, *a, **k: read.append(name) or original(self, name, *a, **k))
    result = _import(h, upload)
    assert result["success"] is False
    assert "unpacks to" in result["error"]
    assert read == []
