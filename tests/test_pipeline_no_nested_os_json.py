"""Regression: Pipeline.run must not shadow module-level os/json imports."""

from __future__ import annotations

import ast
import types
from pathlib import Path

import src.services.pipeline as pipeline_mod


def test_pipeline_module_imports_os_and_json_at_top() -> None:
    assert hasattr(pipeline_mod, "os")
    assert hasattr(pipeline_mod, "json")
    src = Path(pipeline_mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    top_imports: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                top_imports.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            top_imports.add(node.module.split(".")[0])
    assert "os" in top_imports
    assert "json" in top_imports


def test_pipeline_run_has_no_local_os_or_json_bind() -> None:
    """Nested ``import os`` / ``import json`` inside run() caused UnboundLocalError."""
    src = Path(pipeline_mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    run_fn: ast.FunctionDef | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "Pipeline":
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "run":
                    run_fn = item
                    break
    assert run_fn is not None, "Pipeline.run not found"

    nested: list[str] = []

    def walk(node: ast.AST, *, inside_nested_def: bool = False) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                # Nested defs have their own scope; still forbid import os/json there.
                walk(child, inside_nested_def=True)
                continue
            if isinstance(child, ast.Import):
                for alias in child.names:
                    root = alias.name.split(".")[0]
                    if root in {"os", "json"}:
                        nested.append(f"line {child.lineno}: import {alias.name}")
            elif isinstance(child, ast.ImportFrom) and (child.module or "").split(".")[
                0
            ] in {"os", "json"}:
                nested.append(f"line {child.lineno}: from {child.module}")
            walk(child, inside_nested_def=inside_nested_def)

    walk(run_fn)
    assert nested == [], f"Pipeline.run must not locally import os/json: {nested}"

    code = compile(src, pipeline_mod.__file__ or "pipeline.py", "exec")

    def find_run(c: types.CodeType) -> types.CodeType | None:
        for const in c.co_consts:
            if isinstance(const, types.CodeType):
                if const.co_name == "run" and "self" in const.co_varnames:
                    return const
                found = find_run(const)
                if found is not None:
                    return found
        return None

    run_code = find_run(code)
    assert run_code is not None
    assert "os" not in run_code.co_varnames
    assert "json" not in run_code.co_varnames
