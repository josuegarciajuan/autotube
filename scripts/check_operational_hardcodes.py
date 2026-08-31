#!/usr/bin/env python3
"""Reject channel/deployment identity embedded in operational source code.

The check intentionally scans source rather than importing modules, so it is
safe to run in CI without credentials, a database, or a running service.
Historical tests/fixtures and the permanently disabled shorts-link backfill
are outside the operational surface.  A one-line, auditable whitelist is
available for migrations and fixtures::

    # hardcode-gate: allow channel_literal migration
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


CHANNEL_RE = re.compile(r"^canal\d+$")
RULES = {
    "channel_literal": "concrete channel slug",
    "channel_id_literal": "concrete channel id comparison",
    "channel_config_import": "legacy per-channel config import",
    "channel_map": "slug-to-account/project map",
    "production_path": "hardcoded deployment path",
    "channel_default": "channel-specific default",
}
WHITELIST_RE = re.compile(r"#\s*hardcode-gate:\s*allow\s+([\w, -]+)")


@dataclass(frozen=True)
class Finding:
    path: Path
    line: int
    rule: str
    detail: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.rule}: {self.detail}"


def _allowed(source_lines: list[str], line: int, rule: str) -> bool:
    """Allow a rule on its annotated line (or the immediately following line)."""
    for candidate in (line, line - 1):
        if candidate < 1 or candidate > len(source_lines):
            continue
        match = WHITELIST_RE.search(source_lines[candidate - 1])
        if match and any(part.strip().split()[0] == rule for part in match.group(1).split(",") if part.strip()):
            return True
    return False


def _finding(path: Path, lines: list[str], line: int, rule: str, detail: str):
    if rule in RULES and not _allowed(lines, line, rule):
        return Finding(path, line, rule, detail)
    return None


def _is_docstring(node: ast.AST, parent: ast.AST | None) -> bool:
    return isinstance(node, ast.Expr) and isinstance(getattr(node, "value", None), ast.Constant) and isinstance(node.value.value, str)


def scan_file(path: Path) -> list[Finding]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    findings: list[Finding] = []

    for line_no, line in enumerate(lines, 1):
        if "/root/autotube" in line:
            item = _finding(path, lines, line_no, "production_path", "use config.settings.PROJECT_ROOT or runtime_paths()")
            if item:
                findings.append(item)

    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError as exc:
        return findings + [Finding(path, exc.lineno or 1, "syntax_error", str(exc))]

    parent_nodes: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parent_nodes[child] = parent

    for node in ast.walk(tree):
        if _is_docstring(node, parent_nodes.get(node)):
            continue
        if isinstance(node, ast.ImportFrom) and node.module and re.fullmatch(r"config\.canal\d+_config", node.module):
            item = _finding(path, lines, node.lineno, "channel_config_import", node.module)
            if item:
                findings.append(item)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            positional_defaults = list(node.args.defaults)
            keyword_defaults = [default for default in node.args.kw_defaults if default is not None]
            for default in positional_defaults + keyword_defaults:
                if isinstance(default, ast.Constant) and isinstance(default.value, str) and CHANNEL_RE.fullmatch(default.value):
                    item = _finding(path, lines, default.lineno, "channel_default", repr(default.value))
                    if item:
                        findings.append(item)
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if CHANNEL_RE.fullmatch(node.value):
                parent = parent_nodes.get(node)
                # String values in channel config modules are identity data,
                # not operational routing. Those modules are intentionally
                # excluded by scan_paths, but this keeps scan_file useful.
                if not (path.name.startswith("canal") and path.name.endswith("_config.py")):
                    rule = "channel_default" if isinstance(parent, ast.keyword) and parent.arg == "default" else "channel_literal"
                    item = _finding(path, lines, node.lineno, rule, repr(node.value))
                    if item:
                        findings.append(item)
            if "/root/autotube" in node.value:
                item = _finding(path, lines, node.lineno, "production_path", repr(node.value))
                if item and not any(f.line == item.line and f.rule == item.rule for f in findings):
                    findings.append(item)
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name):
            if node.left.id in {"slug", "channel", "canal"} and any(
                isinstance(op, (ast.Eq, ast.NotEq)) for op in node.ops
            ):
                if any(isinstance(c, ast.Constant) and isinstance(c.value, int) for c in node.comparators):
                    item = _finding(path, lines, node.lineno, "channel_id_literal", "channel identity compared to a numeric literal")
                    if item:
                        findings.append(item)
        if isinstance(node, ast.Dict):
            keys = [k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)]
            if any(CHANNEL_RE.fullmatch(key) for key in keys):
                item = _finding(path, lines, node.lineno, "channel_map", "dictionary keyed by a concrete channel slug")
                if item:
                    findings.append(item)
    return findings


def _is_operational(path: Path) -> bool:
    relative = path.as_posix()
    if any(part in relative.split("/") for part in ("tests", "fixtures", "docs")):
        return False
    if path.name == Path(__file__).name or path.name == "backfill_shorts_links.py":
        return False
    if path.name.startswith("canal") and path.name.endswith("_config.py"):
        return False
    return path.suffix == ".py" and any(relative.startswith(prefix) for prefix in ("api/", "config/", "database/", "pipeline/", "scripts/")) or path.name in {"main.py", "orchestrator.py"}


def scan_paths(paths: Iterable[Path]) -> list[Finding]:
    findings: list[Finding] = []
    for raw_path in paths:
        path = Path(raw_path)
        if path.is_file():
            findings.extend(scan_file(path))
        elif path.is_dir():
            findings.extend(scan_file(candidate) for candidate in sorted(path.rglob("*.py")) if _is_operational(candidate))
    # The directory branch above returns lists; flatten while retaining order.
    flattened: list[Finding] = []
    for finding in findings:
        flattened.extend(finding if isinstance(finding, list) else [finding])
    return flattened


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path, help="files/directories; defaults to the repository")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parent.parent
    paths = args.paths or [root]
    findings = scan_paths(paths)
    for finding in findings:
        print(finding)
    print(f"Operational hardcode gate: {len(findings)} finding(s)", file=sys.stderr)
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
