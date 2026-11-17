# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Brutor's system MCP tools carry the same annotations in both planes

The gateway classifies a tool's risk from the capabilities the control plane
stores (`VERB_TOOLS`); agents see the core's live `tools/list`. Both are pinned
to one fixture in the core repo. Without annotations every verb was treated as
side-effecting, so an `approval_required` system could not even list skills.
"""

from __future__ import annotations

import json

from app.constants.system_server import AgentSkillServer
from tests.core_repo import core_repo_path


def _fixture() -> dict:
    return json.loads((core_repo_path() / "tests" / "fixtures" / "system_tool_annotations.json").read_text())


def test_verb_tools_match_the_shared_fixture():
    want = _fixture()["agent_skills"]
    tools = {t["name"]: t for t in AgentSkillServer.VERB_TOOLS}
    verbs = {k for k in want if k.startswith("skills__")}
    assert set(tools) == verbs, "add the verb to the fixture (and the core) in the same change"
    for name in verbs:
        assert tools[name].get("annotations") == want[name], name


def test_only_run_script_acts():
    for t in AgentSkillServer.VERB_TOOLS:
        read_only = t["annotations"].get("readOnlyHint") is True
        assert read_only == (t["name"] != "skills__run_script"), t["name"]


def test_search_tools_match_the_shared_fixture():
    from app.constants.system_server import SearchServer

    want = _fixture()["web_search"]
    names = {SearchServer.TOOL_WEB, SearchServer.TOOL_NEWS, SearchServer.TOOL_EXTRACT, SearchServer.TOOL_IMAGES}
    assert names == set(want), "add the tool to the fixture (and the core) in the same change"
    for name in names:
        assert SearchServer.ANNOTATIONS == want[name], name
