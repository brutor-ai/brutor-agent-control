# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# The published audit-row contract matches the code

`docs.brutor.ai/reference/audit-row/` is what a third party implements a
verifier from: the canonical field order and the list of columns outside the
chain, with the reason for each. The code's lists are pinned across the two
repos (`test_audit_chain_v3.py`, `test_audit_chain_v4.py`); this pins the
published page to them — the one format, its field order, its vectors — so a
format change that forgets the docs fails here
instead of shipping a page that sends an external verifier to the wrong
bytes.

The docs live in a sibling repository (`brutor-ai-docs`, or
`BRUTOR_DOCS_REPO`). When it is not checked out — CI for this repo alone —
the suite skips; the docs repo's own build is where the page ships.
"""

from __future__ import annotations

import os
import re
from collections import defaultdict
from pathlib import Path

import pytest

from app.services.audit_chain_canonical import (
    CANONICAL_VERSION,
    CHAIN_EXCLUDED_COLUMNS,
    KNOWN_VERSIONS,
    V3_FIELDS,
    V4_ADDED_FIELDS,
    V4_FIELDS,
    hash_row,
)


def _docs_repo() -> Path:
    override = os.environ.get("BRUTOR_DOCS_REPO")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[1].parent / "brutor-ai-docs"


PAGE = _docs_repo() / "src" / "content" / "docs" / "reference" / "audit-row.mdx"

pytestmark = pytest.mark.skipif(
    not PAGE.exists(), reason=f"docs repo not checked out ({PAGE}); the page is pinned where both repos are present"
)

_TICKED = re.compile(r"`([a-z][a-z0-9_]*)`")


@pytest.fixture(scope="module")
def page() -> str:
    return PAGE.read_text(encoding="utf-8")


def _paragraph(page: str, marker: str) -> str:
    lines = [line for line in page.splitlines() if line.startswith(marker)]
    assert len(lines) == 1, f"expected exactly one paragraph starting {marker!r}, found {len(lines)}"
    return lines[0]


def _section(page: str, heading: str) -> str:
    start = page.index(heading)
    nxt = re.search(r"^#{1,4} ", page[start + len(heading) :], re.M)
    return page[start : start + len(heading) + nxt.start()] if nxt else page[start:]


class TestFieldOrder:
    def test_the_documented_scalar_order_is_the_code_order(self, page):
        documented = _TICKED.findall(_paragraph(page, "**Scalar fields, in hash order.**"))
        assert tuple(documented) == V3_FIELDS

    def test_the_documented_order_is_the_code_order(self, page):
        scalars = _TICKED.findall(_paragraph(page, "**Scalar fields, in hash order.**"))
        records = _TICKED.findall(_paragraph(page, "**Then the JSON decision records:**"))
        assert tuple(records) == V4_ADDED_FIELDS
        assert tuple(scalars + records) == V4_FIELDS

    def test_the_documented_counts_match(self, page):
        assert len(V4_ADDED_FIELDS) == 6
        assert (
            f"The covered fields are {len(V4_FIELDS)}: {len(V3_FIELDS)} scalar fields, "
            f"then the six JSON decision records." in page
        )

    def test_the_page_documents_the_one_format_the_verifier_computes(self, page):
        # The verifier computes one format; the page must not describe
        # another as verifiable.
        assert KNOWN_VERSIONS == (CANONICAL_VERSION,)
        assert f'bytes = "{CANONICAL_VERSION}" 0x00' in page
        assert re.findall(r'bytes = "(v\d+)" 0x00', page) == [CANONICAL_VERSION]
        assert f"The canonical bytes carry a format tag, `{CANONICAL_VERSION}`" in page
        assert "**v3 field order.**" not in page and "NUL-terminated" not in page

    def test_the_documented_vectors_are_what_the_reference_verifier_computes(self, page):
        from tests.test_audit_chain_v4 import VECTOR_PREV, vector_sparse, vector_sparse_json

        sparse = hash_row(vector_sparse(0), VECTOR_PREV)
        assert f"hashes to `{sparse}`" in page
        assert f"hashes to `{hash_row(vector_sparse_json(), VECTOR_PREV)}`" in page
        documented = re.findall(r"hashes to `([0-9a-f]{64})`", page)
        assert len(documented) == 2, documented


class TestExclusionTable:
    def _rows(self, page):
        section = _section(page, "#### What is outside, and why")
        rows = [line for line in section.splitlines() if line.startswith("| ") and not line.startswith("| ---")]
        assert rows and rows[0].startswith("| Reason"), "exclusion table header moved"
        out = []
        for line in rows[1:]:
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            assert len(cells) == 2, f"malformed exclusion row: {line}"
            out.append((cells[0], frozenset(_TICKED.findall(cells[1]))))
        return out

    def test_every_excluded_column_is_documented_exactly_once(self, page):
        documented = [c for _, cols in self._rows(page) for c in cols]
        assert len(documented) == len(set(documented)), "a column is listed under two reasons"
        assert set(documented) == set(CHAIN_EXCLUDED_COLUMNS), (
            f"undocumented={sorted(set(CHAIN_EXCLUDED_COLUMNS) - set(documented))}, "
            f"documented but not excluded={sorted(set(documented) - set(CHAIN_EXCLUDED_COLUMNS))}"
        )

    def test_the_documented_grouping_is_the_code_grouping(self, page):
        # One table row per exclusion reason, holding exactly that reason's
        # columns — a column moved between reasons in code must move here.
        by_reason = defaultdict(set)
        for column, reason in CHAIN_EXCLUDED_COLUMNS.items():
            by_reason[reason].add(column)
        assert {cols for _, cols in self._rows(page)} == {frozenset(c) for c in by_reason.values()}

    def test_no_covered_column_is_documented_as_excluded(self, page):
        documented = {c for _, cols in self._rows(page) for c in cols}
        assert not documented & set(V4_FIELDS)
