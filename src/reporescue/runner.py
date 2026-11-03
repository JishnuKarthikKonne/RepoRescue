from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ExecutionResult:
    """Encapsulates the result of an isolated command execution."""

    command: list[str]
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False
    truncated: bool = False

    @property
    def passed(self) -> bool:
        """Convenience property for 0 exit code without timeout."""
        return self.exit_code == 0 and not self.timed_out


def _truncate_text(text: str, max_chars: int) -> tuple[str, bool]:
    """Truncates text if it exceeds max_chars and appends a notice."""
    if len(text) <= max_chars:
        return text, False
    truncated_count = len(text) - max_chars
    notice = f"\n[... truncated {truncated_count} characters ...]"
    return text[:max_chars] + notice, True


def run_command(
    cmd: Sequence[str],
    *,
    cwd: Path | str | None = None,
    timeout_seconds: float = 60.0,
    max_output_chars: int = 20_000,
    env: dict[str, str] | None = None,
) -> ExecutionResult:
    """Executes a command safely without shell=True, capturing metrics and output.

    Args:
        cmd: Sequence of command arguments (e.g., ["git", "status"]).
        cwd: Directory in which to execute the command.
        timeout_seconds: Maximum wall-clock time in seconds before terminating.
        max_output_chars: Maximum characters allowed before truncating stdout/stderr.
        env: Optional environment variables dictionary.

    Returns:
        ExecutionResult containing exit code, stdout, stderr, duration, and flags.

    Raises:
        ValueError: If cmd is empty or contains non-string items.
    """
    if not cmd:
        raise ValueError("Command sequence must not be empty.")
    cmd_list = [str(arg) for arg in cmd]

    working_dir = str(Path(cwd).resolve()) if cwd is not None else None
    process_env = os.environ.copy()
    if env:
        process_env.update(env)

    start_time = time.perf_counter()
    try:
        completed = subprocess.run(
            cmd_list,
            cwd=working_dir,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            shell=False,  # Strict invariant: never use shell=True
            env=process_env,
        )
        duration = time.perf_counter() - start_time
        stdout, out_trunc = _truncate_text(completed.stdout, max_output_chars)
        stderr, err_trunc = _truncate_text(completed.stderr, max_output_chars)

        return ExecutionResult(
            command=cmd_list,
            exit_code=completed.returncode,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=duration,
            timed_out=False,
            truncated=out_trunc or err_trunc,
        )

    except subprocess.TimeoutExpired as exc:
        duration = time.perf_counter() - start_time
        raw_stdout = (
            exc.stdout
            if isinstance(exc.stdout, str)
            else (exc.stdout or b"").decode("utf-8", "replace")
        )
        raw_stderr = (
            exc.stderr
            if isinstance(exc.stderr, str)
            else (exc.stderr or b"").decode("utf-8", "replace")
        )
        stdout, out_trunc = _truncate_text(raw_stdout, max_output_chars)
        stderr, err_trunc = _truncate_text(raw_stderr, max_output_chars)

        return ExecutionResult(
            command=cmd_list,
            exit_code=-1,
            stdout=stdout,
            stderr=stderr + f"\n[Command timed out after {timeout_seconds:.1f}s]",
            duration_seconds=duration,
            timed_out=True,
            truncated=out_trunc or err_trunc,
        )


def run_pytest(
    test_target: str | Path | None = None,
    *,
    cwd: Path | str | None = None,
    timeout_seconds: float = 60.0,
    extra_args: Sequence[str] | None = None,
    python_executable: str | Path | None = None,
) -> ExecutionResult:
    """Runs pytest safely within an isolated working directory.

    Args:
        test_target: Specific test file or directory path.
        cwd: Directory where pytest should be invoked (e.g., an isolated worktree).
        timeout_seconds: Maximum test run duration.
        extra_args: Additional pytest CLI arguments (e.g., ["-v", "-k", "test_foo"]).
        python_executable: Path to python binary (defaults to sys.executable).
    """
    py_exec = str(python_executable) if python_executable else sys.executable
    cmd = [py_exec, "-m", "pytest"]
    if test_target:
        cmd.append(str(test_target))
    if extra_args:
        cmd.extend(extra_args)

    return run_command(cmd, cwd=cwd, timeout_seconds=timeout_seconds)
