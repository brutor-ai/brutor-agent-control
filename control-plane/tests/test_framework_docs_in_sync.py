# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The docs.brutor.ai framework pages match the compliance catalogs.

`scripts/gen_framework_docs.py` renders every framework page (and the
generated blocks in the narrative compliance pages) from
`app/services/compliance/catalogs/`. This runs its `--check` mode, so a
catalog change that is not regenerated into the docs fails here.

Skipped when the docs checkout (`../brutor-ai-docs`) is not next to the
control plane — CI checks out this repo alone, so there the test always
skips; it runs on a developer machine with the platform checkout.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "gen_framework_docs.py"
DOCS = ROOT.parent / "brutor-ai-docs"


@pytest.mark.skipif(
    not (DOCS / "src" / "content" / "docs" / "compliance" / "frameworks").is_dir(),
    reason="brutor-ai-docs is not checked out next to the control plane (CI has no sibling checkout)",
)
def test_framework_docs_match_catalogs(capsys):
    spec = importlib.util.spec_from_file_location("gen_framework_docs", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    rc = module.main(["--check", "--docs", str(DOCS)])
    out = capsys.readouterr().out
    assert rc == 0, (
        "docs out of date with the catalogs — run "
        "`.venv-test/bin/python scripts/gen_framework_docs.py`:\n" + out
    )


def test_licensed_text_is_never_quoted():
    """A licensed framework's page carries fingerprints, never an excerpt
    (no blockquote at all). The catalog's own summaries are paraphrases and
    are rendered as written."""
    spec = importlib.util.spec_from_file_location("gen_framework_docs", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for fw in module.CATALOGS.values():
        if not module.is_licensed(fw):
            continue
        page = module.render_page(fw)
        for ob in fw.obligations:
            for src in ob.sources:
                if src.excerpt:
                    assert module.esc(src.excerpt) not in page, (fw.id, ob.key, src.provision)
        assert "\n  > " not in page, fw.id
