"""Regression tests for standalone worker completion bookkeeping."""

import ast
from pathlib import Path


WORKER_PATH = Path(__file__).resolve().parents[1] / "api/services/full_pipeline_worker.py"


def test_upload_retryable_flag_is_initialized_before_upload_branch():
    tree = ast.parse(WORKER_PATH.read_text())
    run_job = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "run_job")
    assignments = [
        node.lineno
        for node in ast.walk(run_job)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "_upload_retryable_fail" for target in node.targets)
    ]
    skip_upload = next(
        node for node in ast.walk(run_job)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "skip_upload" for target in node.targets)
    )

    assert assignments, "the completion flag must have a default value"
    assert min(assignments) < skip_upload.lineno
