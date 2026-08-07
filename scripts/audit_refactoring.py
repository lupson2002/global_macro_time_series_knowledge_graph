#!/usr/bin/env python3
"""Measure Python module size and coupling before refactoring."""
from __future__ import annotations

import argparse
import ast
import json
from dataclasses import asdict, dataclass
from pathlib import Path


BRANCH_NODES = (
    ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.IfExp,
    ast.BoolOp, ast.comprehension, ast.Match,
)


@dataclass(frozen=True)
class FunctionMetric:
    name: str
    line: int
    lines: int
    decision_points: int


@dataclass(frozen=True)
class ModuleMetric:
    path: str
    lines: int
    functions: int
    internal_imports: int
    largest_function: FunctionMetric | None

    @property
    def risk_score(self) -> int:
        largest_lines = self.largest_function.lines if self.largest_function else 0
        decisions = self.largest_function.decision_points if self.largest_function else 0
        return (
            min(self.lines // 100, 5)
            + min(largest_lines // 40, 5)
            + min(decisions // 5, 5)
            + min(self.internal_imports, 5)
        )


def _function_metric(node: ast.FunctionDef | ast.AsyncFunctionDef) -> FunctionMetric:
    end_line = getattr(node, "end_lineno", node.lineno)
    decisions = sum(isinstance(child, BRANCH_NODES) for child in ast.walk(node))
    return FunctionMetric(node.name, node.lineno, end_line - node.lineno + 1, decisions)


def analyze_file(path: Path, root: Path) -> ModuleMetric:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    functions = [
        _function_metric(node)
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    internal_imports = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            internal_imports += node.module.split(".")[0] in {"src", "scripts"}
        elif isinstance(node, ast.Import):
            internal_imports += sum(
                alias.name.split(".")[0] in {"src", "scripts"} for alias in node.names
            )
    return ModuleMetric(
        path=str(path.relative_to(root)),
        lines=len(source.splitlines()),
        functions=len(functions),
        internal_imports=internal_imports,
        largest_function=max(functions, key=lambda item: item.lines, default=None),
    )


def audit(root: Path) -> list[ModuleMetric]:
    paths = [root / "main.py"]
    for directory in (root / "src", root / "scripts"):
        if directory.exists():
            paths.extend(directory.rglob("*.py"))
    return sorted(
        (analyze_file(path, root) for path in paths if path.is_file()),
        key=lambda item: (item.risk_score, item.lines, item.path),
        reverse=True,
    )


def _as_payload(metrics: list[ModuleMetric]) -> list[dict]:
    return [{**asdict(metric), "risk_score": metric.risk_score} for metric in metrics]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--limit", type=int, default=15)
    args = parser.parse_args(argv)
    metrics = audit(args.root.resolve())[: max(0, args.limit)]
    if args.json:
        print(json.dumps(_as_payload(metrics), ensure_ascii=False, indent=2))
        return 0
    print("risk  lines imports largest function                 module")
    for metric in metrics:
        largest = metric.largest_function
        function = "-" if largest is None else (
            f"{largest.name} ({largest.lines}L/{largest.decision_points}D)"
        )
        print(
            f"{metric.risk_score:>4} {metric.lines:>6} {metric.internal_imports:>7} "
            f"{function:<32} {metric.path}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
