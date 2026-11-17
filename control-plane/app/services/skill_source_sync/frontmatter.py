# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
SKILL.md frontmatter parser.

The Anthropic Agent Skills spec stores per-skill metadata in YAML
frontmatter delimited by `---` lines at the top of SKILL.md. This
module splits the raw file into (Frontmatter dict, markdown body)
and normalises the known fields (`allowed-tools` → `allowed_tools`).

Kept dependency-free aside from PyYAML, which is already in the
control plane requirements.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple

import yaml


_FRONTMATTER_RE = re.compile(
    r"^---\s*\n(.*?)\n---\s*\n?(.*)$",
    re.DOTALL,
)


class FrontmatterError(ValueError):
    """Raised when SKILL.md is missing or has malformed frontmatter."""


def parse_skill_md(content: str) -> Tuple[Dict[str, Any], str]:
    """Split a SKILL.md file into (frontmatter dict, markdown body).

    The frontmatter must be at the very top of the file, delimited by
    `---` lines. The body is everything after the second `---`.

    Returns (frontmatter, body). `frontmatter` keys are normalised:
    `allowed-tools` (the spec's kebab-case) is renamed to
    `allowed_tools` for snake-case consistency with the rest of the
    codebase. `allowed_tools` may arrive as either a YAML list OR a
    space/comma-separated string; we normalise to a list here.

    Raises FrontmatterError if the file has no frontmatter at all OR
    if the YAML inside the frontmatter is malformed. Missing
    *individual* fields (e.g. no `version`) is fine — callers can
    apply defaults at their layer.
    """
    if not content.strip():
        raise FrontmatterError("SKILL.md is empty")

    m = _FRONTMATTER_RE.match(content)
    if not m:
        raise FrontmatterError(
            "SKILL.md is missing the YAML frontmatter block "
            "(expected `---` on the first line)"
        )

    raw_yaml, body = m.group(1), m.group(2)
    try:
        fm = yaml.safe_load(raw_yaml) or {}
        if not isinstance(fm, dict):
            raise FrontmatterError(
                "YAML frontmatter must be a mapping, not "
                f"{type(fm).__name__}"
            )
    except yaml.YAMLError as e:
        # Lenient fallback. The Agent Skills client-implementation guide
        # recommends tolerating common authoring slips rather than
        # dropping the whole skill — the canonical case is an unquoted
        # value that itself contains a colon, e.g.
        #     description: Use when: drafting Word documents
        # which strict YAML reads as a nested mapping and rejects. We
        # re-parse line-by-line, treating everything after the first
        # `key:` as a literal string. If even that yields nothing
        # usable, surface the original error.
        fm = _lenient_parse(raw_yaml)
        if not fm:
            raise FrontmatterError(
                f"YAML frontmatter is malformed: {e}"
            ) from e

    # Normalise allowed-tools → allowed_tools, and accept either list
    # or whitespace/comma-separated string form. The Anthropic spec
    # uses kebab-case in the YAML; the rest of our codebase uses
    # snake_case.
    allowed = fm.pop("allowed-tools", None)
    if allowed is None:
        allowed = fm.get("allowed_tools")
    fm["allowed_tools"] = _normalise_string_list(allowed)

    # Same normalisation for compatibility — accept list-of-strings or
    # a single CSV-ish string.
    fm["compatibility"] = _normalise_string_list(fm.get("compatibility"))

    return fm, body


_TOP_KEY_RE = re.compile(r"^([A-Za-z][\w-]*):\s?(.*)$")
_NESTED_KEY_RE = re.compile(r"^\s+([A-Za-z][\w-]*):\s?(.*)$")
_LIST_ITEM_RE = re.compile(r"^\s*-\s+(.*)$")


def _strip_scalar(value: str) -> str:
    """Trim a scalar value: drop surrounding quotes and trailing
    comments are intentionally NOT stripped (a `#` may be legitimate
    content in a description). Just unwrap matching quotes."""
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _lenient_parse(raw_yaml: str) -> Dict[str, Any]:
    """Best-effort recovery parser for SKILL.md frontmatter that strict
    YAML rejected. Handles the shapes that appear in real-world skills:

      * top-level `key: scalar` (value may contain unescaped colons)
      * top-level `key:` followed by indented `- item` list entries
      * top-level `key:` followed by an indented `subkey: value` map
        (e.g. `metadata:`)

    Anything it can't classify is skipped rather than aborting. Returns
    a dict (possibly empty); the caller decides whether empty means
    "give up and report the original YAML error".
    """
    out: Dict[str, Any] = {}
    current_key: Optional[str] = None
    current_kind: Optional[str] = None  # "list" | "map"

    for line in raw_yaml.splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue

        # Indented list item under the current key.
        li = _LIST_ITEM_RE.match(line)
        if li and current_key is not None and line[0] in (" ", "\t"):
            if current_kind != "list":
                out[current_key] = []
                current_kind = "list"
            out[current_key].append(_strip_scalar(li.group(1)))
            continue

        # Indented `subkey: value` under the current key (nested map).
        nm = _NESTED_KEY_RE.match(line)
        if nm and current_key is not None:
            if current_kind != "map":
                out[current_key] = {}
                current_kind = "map"
            if isinstance(out[current_key], dict):
                out[current_key][nm.group(1)] = _strip_scalar(nm.group(2))
            continue

        # Top-level `key: ...`.
        tm = _TOP_KEY_RE.match(line)
        if tm:
            key, val = tm.group(1), tm.group(2)
            if val.strip() == "":
                # Container — its kind is decided by the lines that
                # follow. Default to None until we see a child.
                current_key, current_kind = key, None
                out.setdefault(key, "")
            else:
                out[key] = _strip_scalar(val)
                current_key, current_kind = None, None
            continue
        # Unclassifiable line — ignore and keep going.

    # Drop placeholder empty-string containers that never got children.
    return {k: v for k, v in out.items() if v != ""}


def _normalise_string_list(value: Any) -> Optional[list]:
    """Accept a list, a string, or None. Return a clean list[str] or
    None. Empty strings collapse to None to avoid storing `[]` when
    the upstream author left the field blank."""
    if value is None:
        return None
    if isinstance(value, list):
        result = [str(v).strip() for v in value if str(v).strip()]
        return result or None
    if isinstance(value, str):
        # Accept either commas OR whitespace as separators — both are
        # used in the wild in Anthropic-style SKILL.md files.
        parts = [p.strip() for p in re.split(r"[,\s]+", value) if p.strip()]
        return parts or None
    # Anything else (dict, int, etc.) is a frontmatter authoring
    # error — squash to None and let the caller flag it.
    return None
