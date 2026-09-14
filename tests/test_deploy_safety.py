"""Tests for scripts/deploy_safety.py — the apply_changes.sh deploy gate.

Regression: apply_changes.sh used to abort the deploy whenever ANY
full_pipeline_worker process existed, even when the worker ran as a subprocess
and would survive the API restart. That made the post-merge auto-deploy silently
do nothing (it even exited 0). The gate must now:

  * proceed when there is no active long-form generation;
  * proceed when every running job is a subprocess worker (KillMode=process);
  * abort (exit 1) when any running job is in-process, unless forced by
    SKIP_ACTIVE_WORKER_CHECK=true or confirmed interactively.
"""

import pytest

import scripts.deploy_safety as ds


class _FakeStdin:
    def __init__(self, tty: bool):
        self._tty = tty

    def isatty(self):
        return self._tty


# ═══════════════════════════════════════════════════════════════
# decide() — pure decision matrix
# ═══════════════════════════════════════════════════════════════

def test_no_jobs_proceeds():
    decision, _ = ds.decide(job_ids=[], kill_mode=None, use_subprocess=True)
    assert decision == "proceed"


def test_all_subprocess_proceeds():
    decision, reason = ds.decide(
        job_ids=[10904], use_subprocess=True, kill_mode="process",
        pid_fn=lambda jid: 1455569,
    )
    assert decision == "proceed"
    assert "subprocess" in reason


def test_in_process_aborts_by_default():
    decision, reason = ds.decide(
        job_ids=[10904], use_subprocess=True, kill_mode="process",
        pid_fn=lambda jid: None,
    )
    assert decision == "abort"
    assert "in-process" in reason


def test_in_process_forced_proceeds():
    decision, reason = ds.decide(
        job_ids=[10904], use_subprocess=True, kill_mode="process",
        force=True, pid_fn=lambda jid: None,
    )
    assert decision == "proceed"
    assert "forced" in reason


def test_in_process_confirmed_on_tty_proceeds():
    decision, _ = ds.decide(
        job_ids=[42], use_subprocess=True, kill_mode="process",
        isatty=True, confirm=lambda: "y", pid_fn=lambda jid: None,
    )
    assert decision == "proceed"


def test_legacy_mode_treats_all_as_in_process():
    # Even if a stray process matched, legacy mode has no survivable worker.
    decision, _ = ds.decide(
        job_ids=[7], use_subprocess=False, kill_mode="process",
        pid_fn=lambda jid: 999,
    )
    assert decision == "abort"


def test_subprocess_but_wrong_killmode_aborts():
    decision, reason = ds.decide(
        job_ids=[10904], use_subprocess=True, kill_mode="mixed",
        pid_fn=lambda jid: 1455569,
    )
    assert decision == "abort"
    assert "KillMode" in reason


def test_subprocess_unknown_killmode_proceeds():
    decision, _ = ds.decide(
        job_ids=[10904], use_subprocess=True, kill_mode=None,
        pid_fn=lambda jid: 1455569,
    )
    assert decision == "proceed"


def test_classify_mixed_jobs():
    modes = ds.classify_jobs([1, 2], pid_fn=lambda jid: 500 if jid == 1 else None)
    assert modes == {1: "subprocess", 2: "in_process"}


# ═══════════════════════════════════════════════════════════════
# main() — exit codes + env kill-switch
# ═══════════════════════════════════════════════════════════════

@pytest.fixture
def patch_main(monkeypatch):
    monkeypatch.setattr(ds, "_emit_skip_alert", lambda reason: None)
    monkeypatch.setattr(ds.sys, "stdin", _FakeStdin(False))
    monkeypatch.delenv("SKIP_ACTIVE_WORKER_CHECK", raising=False)

    def _setup(jobs, pid, kill_mode="process"):
        monkeypatch.setattr(ds, "running_longform_jobs", lambda: list(jobs))
        monkeypatch.setattr(ds, "worker_pid", lambda jid: pid)
        monkeypatch.setattr(ds, "use_subprocess_worker", lambda: True)
        monkeypatch.setattr(ds, "systemd_kill_mode", lambda unit="autotube-panel": kill_mode)

    return _setup


def test_main_safe_without_jobs(monkeypatch, patch_main):
    monkeypatch.setattr(ds, "running_longform_jobs", lambda: [])
    monkeypatch.setattr(ds, "use_subprocess_worker", lambda: True)
    assert ds.main([]) == 0


def test_main_proceeds_with_subprocess_worker(patch_main):
    patch_main(jobs=[10904], pid=1455569)
    assert ds.main([]) == 0


def test_main_aborts_in_process_and_exits_1(patch_main):
    patch_main(jobs=[10904], pid=None)
    assert ds.main([]) == 1


def test_main_forced_via_env(monkeypatch, patch_main):
    patch_main(jobs=[10904], pid=None)
    monkeypatch.setenv("SKIP_ACTIVE_WORKER_CHECK", "true")
    assert ds.main([]) == 0
