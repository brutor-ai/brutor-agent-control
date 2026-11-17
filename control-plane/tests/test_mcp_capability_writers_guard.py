# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Every write of `mcp_servers.capabilities` goes through the tool normaliser

The runtime gates on MCP tool `annotations` stored in this column and treats
an unannotated tool as side-effecting (fail closed). The 2026-09 bug: the
legacy discovery path serialised tools through a private whitelist
(`name`/`description`/`inputSchema`), so every `readOnlyHint` was lost and
read-only tools were blocked as writes. Nothing failed — the data was just
quietly poorer than the upstream's.

The canonical normaliser (`app/services/mcp/tool_snapshot.py`) is applied by
a SQLAlchemy `@validates("capabilities")` hook on `MCPServer`, which covers
ORM construction, attribute assignment, `setattr` and `update_capabilities`.
This test pins that hook behaviourally, then scans `app/` for the write
paths that bypass the ORM and therefore the hook:

  * raw SQL writing `mcp_servers` capabilities (`UPDATE` / `INSERT`),
  * Core `update(MCPServer)` / `insert(MCPServer)` `.values(...)`,
  * `Query.update({...})` on an `MCPServer` query,
  * `bulk_*_mappings(MCPServer, ...)`,
  * in-place mutation of `.capabilities` (subscript assignment, mutating
    method calls, `flag_modified(..., "capabilities")`).

A bypass site whose payload cannot be read statically (a non-literal dict,
`**kwargs`) fails too: rewrite it as an ORM assignment.
"""

from __future__ import annotations

import ast
import os
import pathlib
import re
from typing import List, Optional
from tests.source_trees import python_sources

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

APP = pathlib.Path(__file__).resolve().parent.parent / "app"

_RAW_SQL_WRITE = re.compile(
    r"\b(UPDATE\s+(public\.)?mcp_servers\b|INSERT\s+INTO\s+(public\.)?mcp_servers\b)",
    re.IGNORECASE,
)
_MUTATING_METHODS = {"update", "setdefault", "pop", "popitem", "clear", "append",
                     "extend", "insert", "remove", "__setitem__"}


# ---------------------------------------------------------------------------
# 1. The hook itself
# ---------------------------------------------------------------------------


RAW = {
    "tools": [{
        "name": "get_applicant",
        "description": "",
        "inputSchema": {"type": "object"},
        "annotations": {"readOnlyHint": True, "destructiveHint": None},
        "_meta": {"k": "v"},
        "x-vendor-junk": 1,
    }],
    "resources": [],
}
NORMALISED_TOOL = {
    "name": "get_applicant",
    "inputSchema": {"type": "object"},
    "annotations": {"readOnlyHint": True},
    "_meta": {"k": "v"},
}


def _server(**kw):
    from app.database.models.mcp_server import MCPServer
    return MCPServer(id="s1", tenant_id="t1", name="s", base_url="https://x.invalid", **kw)


def test_constructor_kwarg_is_normalised():
    s = _server(capabilities=RAW)
    assert s.capabilities["tools"] == [NORMALISED_TOOL]


def test_attribute_assignment_and_setattr_are_normalised():
    s = _server()
    s.capabilities = RAW
    assert s.capabilities["tools"] == [NORMALISED_TOOL]
    s2 = _server()
    setattr(s2, "capabilities", RAW)
    assert s2.capabilities["tools"] == [NORMALISED_TOOL]


def test_update_capabilities_is_normalised():
    s = _server()
    s.update_capabilities(RAW, {"name": "x", "version": "1"})
    assert s.capabilities["tools"] == [NORMALISED_TOOL]
    assert s.capabilities["resources"] == []


def test_none_passes_through():
    assert _server(capabilities=None).capabilities is None


# ---------------------------------------------------------------------------
# 2. No bypass of the hook in app/
# ---------------------------------------------------------------------------


def _src(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:  # pragma: no cover
        return ""


def _mentions_mcp_server(node: ast.AST) -> bool:
    return bool(re.search(r"\bMCPServer\b|\bmcp_servers\b", _src(node)))


def _payload_writes_capabilities(call: ast.Call) -> Optional[bool]:
    """True/False when the payload is readable, None when it is not."""
    unreadable = False
    for kw in call.keywords:
        if kw.arg is None:
            unreadable = True
        elif kw.arg == "capabilities":
            return True
    for arg in call.args:
        if isinstance(arg, ast.Dict):
            for k in arg.keys:
                if k is None:
                    unreadable = True
                elif re.search(r"capabilities", _src(k)):
                    return True
        else:
            unreadable = True
    return None if unreadable else False


def _string_constants(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node, node.value
        elif isinstance(node, ast.JoinedStr):
            text = "".join(
                v.value for v in node.values
                if isinstance(v, ast.Constant) and isinstance(v.value, str)
            )
            yield node, text


def _is_docstring(node: ast.AST, parents: dict) -> bool:
    parent = parents.get(node)
    return isinstance(parent, ast.Expr)


def scan(path: pathlib.Path, root: pathlib.Path = APP.parent) -> List[str]:
    rel = path.relative_to(root)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    parents = {}
    for p in ast.walk(tree):
        for c in ast.iter_child_nodes(p):
            parents[c] = p
    out: List[str] = []

    def flag(node: ast.AST, why: str) -> None:
        out.append(f"{rel}:{getattr(node, 'lineno', '?')}: {why}: {_src(node)[:120]}")

    for node, text in _string_constants(tree):
        if _is_docstring(node, parents) or not _RAW_SQL_WRITE.search(text):
            continue
        if "capabilities" in text:
            flag(node, "raw SQL writes mcp_servers.capabilities (bypasses the normaliser)")
        elif isinstance(node, ast.JoinedStr) and any(
            isinstance(v, ast.FormattedValue) for v in node.values
        ):
            flag(node, "raw SQL write to mcp_servers with an interpolated (unreadable) column list")

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            attr = node.func.attr
            recv = node.func.value
            # Core update(MCPServer).values(...) / insert(MCPServer).values(...)
            if attr == "values" and _mentions_mcp_server(recv):
                w = _payload_writes_capabilities(node)
                if w is None:
                    flag(node, "unreadable .values() payload on an MCPServer statement")
                elif w:
                    flag(node, "Core .values(capabilities=...) bypasses the normaliser")
            # session.query(MCPServer)...update({...})
            elif attr == "update" and _mentions_mcp_server(recv) and "query(" in _src(recv):
                w = _payload_writes_capabilities(node)
                if w is None:
                    flag(node, "unreadable Query.update() payload on an MCPServer query")
                elif w:
                    flag(node, "Query.update({capabilities: ...}) bypasses the normaliser")
            elif attr in {"bulk_update_mappings", "bulk_insert_mappings", "bulk_save_objects"} \
                    and node.args and _mentions_mcp_server(node.args[0]):
                flag(node, f"{attr} on MCPServer bypasses the normaliser")
            # x.capabilities.update(...) / x.capabilities["tools"].append(...)
            elif attr in _MUTATING_METHODS and re.search(r"\.capabilities\b", _src(recv)):
                flag(node, "in-place mutation of .capabilities bypasses the normaliser")
        if isinstance(node, ast.Call) and _src(node.func).endswith("flag_modified"):
            if len(node.args) >= 2 and "capabilities" in _src(node.args[1]):
                flag(node, "flag_modified(..., 'capabilities') implies in-place mutation")
        if isinstance(node, (ast.Assign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Subscript) and re.search(r"\.capabilities\b", _src(t.value)):
                    flag(node, "subscript assignment into .capabilities bypasses the normaliser")
    return out


def _app_files():
    return sorted(p for p in python_sources(APP) if "__pycache__" not in p.parts)


def test_no_capability_write_bypasses_the_normaliser():
    violations: List[str] = []
    for path in _app_files():
        violations.extend(scan(path))
    # Non-MCP models with their own `capabilities` column (virtual servers,
    # LLM catalogue) are not in scope: the patterns above only fire on
    # MCPServer / mcp_servers receivers, except the in-place-mutation rule,
    # which is receiver-agnostic on purpose. Allow-list those by file with
    # a reason, never by line.
    allowed_files = {
        # VirtualServer.capabilities is a selection map of references into
        # other servers ({server_id, name}), not an MCP tool list; its
        # model mutates it in place by design.
        "app/database/models/virtual_server.py",
    }
    violations = [v for v in violations if v.split(":", 1)[0] not in allowed_files]
    assert not violations, (
        "MCP capabilities written without the canonical normaliser "
        "(app/services/mcp/tool_snapshot.py). Assign through the ORM "
        "(server.capabilities = ... / update_capabilities) instead:\n  "
        + "\n  ".join(violations)
    )


# ---------------------------------------------------------------------------
# 3. The scanner catches what it claims to (so a green run means something)
# ---------------------------------------------------------------------------


def _scan_source(tmp_path, source: str) -> List[str]:
    probe = tmp_path / "probe.py"
    probe.write_text(source)
    return scan(probe, root=tmp_path)


def test_scanner_flags_each_bypass(tmp_path):
    cases = {
        "raw_sql": 'db.execute(text("UPDATE mcp_servers SET capabilities = :c WHERE id = :i"))',
        "raw_sql_interpolated": 'db.execute(text(f"UPDATE mcp_servers SET {cols} WHERE id = :i"))',
        "core_values": "db.execute(update(MCPServer).where(MCPServer.id == i).values(capabilities=c))",
        "core_values_unreadable": "db.execute(update(MCPServer).values(**payload))",
        "query_update": 'db.query(MCPServer).filter(MCPServer.id == i).update({"capabilities": c})',
        "query_update_attr": "db.query(MCPServer).filter(x).update({MCPServer.capabilities: c})",
        "bulk": "db.bulk_update_mappings(MCPServer, rows)",
        "subscript": 'server.capabilities["tools"] = tools',
        "mutation": 'server.capabilities["tools"].append(t)',
        "flag_modified": 'flag_modified(server, "capabilities")',
    }
    for name, src in cases.items():
        assert _scan_source(tmp_path, src), f"scanner missed the {name} bypass"


def test_scanner_passes_orm_writes(tmp_path):
    ok = "\n".join([
        "server.capabilities = caps",
        "server.update_capabilities(caps, info)",
        "MCPServer(id=i, capabilities=caps)",
        "setattr(server, 'capabilities', caps)",
        "caps = dict(server.capabilities or {})",
        'caps["tools"] = tools',
        "db.query(MCPServer).filter(x).update({MCPServer.enabled: False})",
        'db.execute(text("UPDATE mcp_servers SET enabled = false WHERE id = :i"))',
    ])
    assert _scan_source(tmp_path, ok) == []
