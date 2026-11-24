import subprocess
import sys
from pathlib import Path

import pytest

from mcli import hitl


@pytest.mark.parametrize("returncode, expected", [
    (6, "APPROVED"),
    (7, "DENIED"),
    (0, "UNAVAILABLE"),
    (1, "UNAVAILABLE"),
])
def test_windows_uses_native_helper_with_utf8_input(monkeypatch, returncode, expected):
    msg = "服务: mock\n工具: 测试"

    def run(command, **kwargs):
        helper = Path(hitl.__file__).resolve().with_name("_windows_dialog.py")
        assert command == [sys.executable, "-I", str(helper)]
        assert kwargs["input"] == msg.encode("utf-8")
        assert kwargs["timeout"] == hitl.CONFIRM_TIMEOUT_S
        return subprocess.CompletedProcess(command, returncode)

    monkeypatch.setattr(hitl.subprocess, "run", run)
    assert hitl._confirm_windows(msg) is getattr(hitl.Approval, expected)


@pytest.mark.parametrize("error, expected", [
    (subprocess.TimeoutExpired("dialog", 60), "TIMED_OUT"),
    (OSError("cannot start dialog"), "UNAVAILABLE"),
])
def test_dialog_errors_fail_closed_with_distinct_outcomes(monkeypatch, error, expected):
    def run(*args, **kwargs):
        raise error

    monkeypatch.setattr(hitl.platform, "system", lambda: "Windows")
    monkeypatch.setattr(hitl.subprocess, "run", run)
    assert hitl.confirm_sync("mock", "echo", {}) is getattr(hitl.Approval, expected)
