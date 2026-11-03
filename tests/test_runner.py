import sys
from pathlib import Path

import pytest

from reporescue.runner import run_command, run_pytest


def test_run_command_success():
    res = run_command([sys.executable, "-c", "print('RepoRescue Runner')"])
    assert res.exit_code == 0
    assert res.passed is True
    assert "RepoRescue Runner" in res.stdout
    assert res.stderr == ""
    assert res.timed_out is False
    assert res.truncated is False
    assert res.duration_seconds >= 0


def test_run_command_failure_captured():
    script = "import sys; sys.stderr.write('fatal: something failed\\n'); sys.exit(42)"
    res = run_command([sys.executable, "-c", script])
    assert res.exit_code == 42
    assert res.passed is False
    assert "fatal: something failed" in res.stderr
    assert res.timed_out is False


def test_run_command_timeout():
    script = "import time; time.sleep(5)"
    res = run_command([sys.executable, "-c", script], timeout_seconds=0.2)
    assert res.timed_out is True
    assert res.passed is False
    assert res.exit_code == -1
    assert "timed out after 0.2s" in res.stderr


def test_run_command_output_truncation():
    script = "print('A' * 500, end='')"
    res = run_command([sys.executable, "-c", script], max_output_chars=50)
    assert res.truncated is True
    assert len(res.stdout) > 50  # Contains truncation notice
    assert "[... truncated 450 characters ...]" in res.stdout


def test_run_command_empty_rejected():
    with pytest.raises(ValueError, match="must not be empty"):
        run_command([])


def test_run_command_custom_cwd(tmp_path: Path):
    target_file = tmp_path / "marker.txt"
    target_file.write_text("ok", encoding="utf-8")
    script = "from pathlib import Path; print(Path('marker.txt').read_text())"
    res = run_command([sys.executable, "-c", script], cwd=tmp_path)
    assert res.passed is True
    assert res.stdout.strip() == "ok"


def test_run_pytest_helper(tmp_path: Path):
    # Create a minimal passing test
    test_file = tmp_path / "test_demo.py"
    test_file.write_text("def test_ok(): assert 1 + 1 == 2\n", encoding="utf-8")
    res = run_pytest(test_file, cwd=tmp_path, python_executable=sys.executable)
    assert res.passed is True
    assert "1 passed" in res.stdout
