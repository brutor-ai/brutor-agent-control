# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# No keyword may be passed twice through a spread model dump

`Detail(**summary.model_dump(), turn_count=...)` raises
`TypeError: got multiple values for keyword argument 'turn_count'` the moment
`turn_count` is added to the summary model — at REQUEST time, on that one
route, with no import-time or type-check signal. That is exactly how the run
details endpoint once broke (every "Details" click returned 500).

This test finds every call in `app/` that spreads a Pydantic model's dump
(`**x.model_dump()`, or a local assigned from one) next to explicit keywords,
resolves which model was dumped, and fails if an explicit keyword is also a
field of that model. A call site whose dumped model cannot be resolved also
fails: override the keys on the dict instead (`base.update(k=v)`), or annotate
the variable/function so the model is knowable.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import os
import pathlib
import typing

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from pydantic import BaseModel
from tests.source_trees import python_sources

APP = pathlib.Path(__file__).resolve().parent.parent / "app"
DUMP_METHODS = {"model_dump", "dict"}


def _is_dump(node: ast.AST) -> bool:
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr in DUMP_METHODS)


def _model_from_annotation(ann, module) -> type | None:
    if ann is None:
        return None
    try:
        obj = eval(ast.unparse(ann), vars(module)) if isinstance(ann, ast.AST) else ann
    except Exception:
        return None
    if isinstance(obj, str):
        try:
            obj = eval(obj, vars(module))
        except Exception:
            return None
    origin = typing.get_origin(obj)
    if origin is typing.Union:  # Optional[Model]
        models = [a for a in typing.get_args(obj) if inspect.isclass(a) and issubclass(a, BaseModel)]
        return models[0] if len(models) == 1 else None
    return obj if inspect.isclass(obj) and issubclass(obj, BaseModel) else None


def _resolve_callee_return(call: ast.Call, module, fn_defs) -> type | None:
    """The model a call produces: a model constructor, or a function's return annotation."""
    name = ast.unparse(call.func)
    target = None
    try:
        target = eval(name, vars(module))
    except Exception:
        target = None
    if inspect.isclass(target) and issubclass(target, BaseModel):
        return target
    if callable(target):
        hints = getattr(target, "__annotations__", {})
        ret = hints.get("return")
        m = _model_from_annotation(ret, module) if ret is not None else None
        if m:
            return m
    # A nested/local function (closure inside a router factory): read its AST.
    fdef = fn_defs.get(name)
    if fdef is not None and fdef.returns is not None:
        return _model_from_annotation(fdef.returns, module)
    return None


def _resolve_dumped_model(expr: ast.AST, fn: ast.AST, module, fn_defs) -> type | None:
    """`expr` is the object whose .model_dump() is spread."""
    if isinstance(expr, ast.Call):
        return _resolve_callee_return(expr, module, fn_defs)
    if isinstance(expr, ast.Name):
        # A parameter with an annotation?
        for arg in getattr(fn, "args", ast.arguments(args=[], posonlyargs=[], kwonlyargs=[])).args + \
                getattr(fn, "args", ast.arguments(args=[], posonlyargs=[], kwonlyargs=[])).kwonlyargs:
            if arg.arg == expr.id:
                return _model_from_annotation(arg.annotation, module)
        # A local assigned from a model-producing call?
        for node in _own_nodes(fn):
            if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == expr.id for t in node.targets):
                if isinstance(node.value, ast.Call):
                    return _resolve_callee_return(node.value, module, fn_defs)
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                    and node.target.id == expr.id:
                return _model_from_annotation(node.annotation, module)
    return None


def _own_nodes(fn):
    """Nodes of `fn` excluding the bodies of functions nested inside it, so a
    call is attributed to its innermost function (router factories nest the
    handlers; the handler's parameters are what resolve `body`)."""
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        stack.extend(ast.iter_child_nodes(node))


def _spread_sites():
    for path in python_sources(APP):
        src = path.read_text()
        if "model_dump" not in src and ".dict(" not in src:
            continue
        tree = ast.parse(src)
        mod_name = ".".join(path.relative_to(APP.parent).with_suffix("").parts)
        fn_defs = {n.name: n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            dumped_locals = {}
            for node in _own_nodes(fn):
                if isinstance(node, ast.Assign) and _is_dump(node.value):
                    for t in node.targets:
                        if isinstance(t, ast.Name):
                            dumped_locals[t.id] = node.value.func.value
            for node in _own_nodes(fn):
                if not isinstance(node, ast.Call):
                    continue
                explicit = [k.arg for k in node.keywords if k.arg]
                if not explicit:
                    continue
                for k in node.keywords:
                    if k.arg is not None:
                        continue
                    if _is_dump(k.value):
                        yield path, mod_name, fn, fn_defs, node, k.value.func.value, explicit
                    elif isinstance(k.value, ast.Name) and k.value.id in dumped_locals:
                        yield path, mod_name, fn, fn_defs, node, dumped_locals[k.value.id], explicit


def test_no_keyword_is_passed_twice_through_a_spread_dump():
    problems, checked = [], 0
    for path, mod_name, fn, fn_defs, call, dumped_expr, explicit in _spread_sites():
        where = f"{path.relative_to(APP.parent)}:{call.lineno} {ast.unparse(call.func)}(**…)"
        # Only calls whose dumped object is (or may be) a Pydantic model matter;
        # ORM kwargs like LLModel(**fields) spread plain dicts, not dumps.
        module = importlib.import_module(mod_name)
        model = _resolve_dumped_model(dumped_expr, fn, module, fn_defs)
        if model is None:
            problems.append(f"{where}: cannot tell which model `{ast.unparse(dumped_expr)}` is; "
                            f"annotate it or override keys with dict.update()")
            continue
        checked += 1
        clash = sorted(set(explicit) & set(model.model_fields))
        if clash:
            problems.append(f"{where}: {clash} are fields of {model.__name__} and are passed again")
    assert not problems, "Spread-dump keyword collisions:\n  " + "\n  ".join(problems)
    assert checked >= 1, "the scanner found no spread sites; it is probably broken"
