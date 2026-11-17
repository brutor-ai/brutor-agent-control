# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The migration history has exactly ONE head.

Two heads mean two revisions share a parent: `alembic upgrade head` refuses
to run, and startup (which upgrades to head under the advisory lock) fails
on every installation. Per-migration tests used to assert "I am the single
head", which goes stale the moment the next migration lands; this one holds
for every future migration.
"""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

ROOT = Path(__file__).resolve().parent.parent


def _scripts() -> ScriptDirectory:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    return ScriptDirectory.from_config(cfg)


def test_the_history_has_exactly_one_head():
    heads = _scripts().get_heads()
    assert len(heads) == 1, f"migration history forked: heads={heads}"


def test_the_detector_sees_a_fork(tmp_path):
    # Proven against the shape it forbids: two revisions on one parent.
    versions = tmp_path / "versions"
    versions.mkdir()
    (tmp_path / "script.py.mako").write_text("")
    for rev, down in (("base_rev", None), ("left_rev", "base_rev"), ("right_rev", "base_rev")):
        (versions / f"{rev}.py").write_text(
            f"revision = {rev!r}\ndown_revision = {down!r}\n"
            "branch_labels = None\ndepends_on = None\n"
            "def upgrade():\n    pass\n\ndef downgrade():\n    pass\n"
        )
    cfg = Config()
    cfg.set_main_option("script_location", str(tmp_path))
    assert len(ScriptDirectory.from_config(cfg).get_heads()) == 2
