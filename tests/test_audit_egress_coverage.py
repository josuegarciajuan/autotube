"""La auditoría estática de egress debe estar en verde."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_audit_egress_coverage_green():
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "audit_egress_coverage.py")],
        cwd=str(ROOT), capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr
