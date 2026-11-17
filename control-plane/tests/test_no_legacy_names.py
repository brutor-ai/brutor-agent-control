# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Names the product no longer carries

Brutor once routed through a third-party LLM gateway library; nothing of it is
used any more, and its name stayed behind in identifiers, an API field, a dead export endpoint,
column comments and ~150 comments and links. All of it was removed.
This keeps it out: of
the source, the migrations, the docs, the configuration examples — and of
the published API schema, where a competitor's name is the most visible.

The word is assembled here so this file does not contain it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORD = "lite" + "llm"
ALSO = "berri" + "ai"
PATTERN = re.compile(f"{WORD}|{ALSO}", re.IGNORECASE)

SCANNED = ("app", "enterprise", "alembic", "tests", "docs", "scripts", "openapi")
SUFFIXES = {".py", ".md", ".json", ".toml", ".yml", ".yaml", ".sh", ".ini", ".cfg", ".txt", ".example"}
TOP_LEVEL = ("pyproject.toml", "README.md", ".env.example", "docker-compose.yml", "Dockerfile",
             "main.py", "gunicorn.conf.py", "start_with_gunicorn.sh")


def _files():
    for name in SCANNED:
        base = ROOT / name
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if path.is_file() and (path.suffix in SUFFIXES or path.name.endswith(".example")) \
                    and "__pycache__" not in path.parts:
                yield path
    for name in TOP_LEVEL:
        path = ROOT / name
        if path.is_file():
            yield path


def test_the_name_is_nowhere_in_the_repository():
    hits = []
    for path in _files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if PATTERN.search(line):
                hits.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()[:100]}")
    assert not hits, "the legacy name is back:\n" + "\n".join(hits[:40])


def test_the_published_api_does_not_carry_it():
    """The Community document; the Enterprise one:
    `enterprise/tests/test_enterprise_openapi_document.py`."""
    spec = json.loads((ROOT / "tests" / "fixtures" / "openapi_snapshot_community.json").read_text())
    names = list(spec["components"]["schemas"]) + list(spec["paths"])
    for schema in spec["components"]["schemas"].values():
        names += list((schema.get("properties") or {}).keys())
    assert not [n for n in names if PATTERN.search(n)]


def test_the_scan_sees_the_word():
    assert PATTERN.search("Get " + "Lite" + "LLM" + " router configuration")
    assert PATTERN.search(f"https://docs.{WORD}.ai/docs/providers")
    assert not PATTERN.search("the gateway's router")
    assert len(list(_files())) > 1000
