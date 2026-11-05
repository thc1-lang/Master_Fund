from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _run_python(source: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", source],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )


def test_downloader_help_imports_without_pipeline_cycle():
    result = _run_python("import subprocess, sys; raise SystemExit(subprocess.call([sys.executable, '-m', 'qualitative_ir_downloader.main', '--help']))")
    assert result.returncode == 0, result.stderr


def test_pipeline_runner_help_imports_without_pipeline_cycle():
    result = _run_python("import subprocess, sys; raise SystemExit(subprocess.call([sys.executable, '-m', 'pipeline_runner', '--help']))")
    assert result.returncode == 0, result.stderr


def test_direct_module_imports_and_both_orders():
    snippets = (
        "from qualitative_ir_downloader.google_sheets_publisher import DEFAULT_QUALITATIVE_SPREADSHEET_ID; from pipeline_runner.issuer_resolution import canonical_identity_hash; assert DEFAULT_QUALITATIVE_SPREADSHEET_ID and callable(canonical_identity_hash)",
        "from pipeline_runner.issuer_resolution import canonical_identity_hash; from qualitative_ir_downloader.google_sheets_publisher import DEFAULT_QUALITATIVE_SPREADSHEET_ID; assert DEFAULT_QUALITATIVE_SPREADSHEET_ID and callable(canonical_identity_hash)",
    )
    for source in snippets:
        result = _run_python(source)
        assert result.returncode == 0, result.stderr
