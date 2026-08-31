"""Contract tests for the operational hardcode gate."""

from pathlib import Path

from scripts.check_operational_hardcodes import scan_paths

ROOT = Path(__file__).resolve().parent.parent


def test_gate_rejects_channel_specific_operational_code(tmp_path: Path):
    source = tmp_path / "bad.py"
    source.write_text(
        'if slug == "canal2":\n'
        '    return Path("/root/autotube/output")\n',
        encoding="utf-8",
    )

    findings = scan_paths([source])

    assert {finding.rule for finding in findings} >= {"channel_literal", "production_path"}


def test_gate_allows_explicit_fixture_whitelist(tmp_path: Path):
    source = tmp_path / "fixture.py"
    source.write_text(
        '# hardcode-gate: allow channel_literal fixture\n'
        'EXPECTED = "canal2"\n',
        encoding="utf-8",
    )

    assert scan_paths([source]) == []


def test_gate_rejects_legacy_config_import_and_account_map(tmp_path: Path):
    source = tmp_path / "bad.py"
    source.write_text(
        "from config.canal2_config import CANAL_NAME\n"
        'ACCOUNTS = {"canal2": "example-account"}\n',
        encoding="utf-8",
    )

    findings = scan_paths([source])

    assert {finding.rule for finding in findings} >= {"channel_config_import", "channel_map"}


def test_gate_rejects_channel_specific_function_default(tmp_path: Path):
    source = tmp_path / "bad.py"
    source.write_text('def run(channel="canal2"):\n    return channel\n', encoding="utf-8")

    assert any(finding.rule == "channel_default" for finding in scan_paths([source]))


def test_repository_passes_operational_hardcode_gate():
    assert scan_paths([ROOT]) == []
