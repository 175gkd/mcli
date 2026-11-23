"""Human-in-the-loop confirmation via native OS dialogs (Non-TTY safe).

Returns True only if the user explicitly approves. Timeout, denial, missing
dialog tooling, or headless environments all safely default to False.
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import shutil
import subprocess

CONFIRM_TIMEOUT_S = float(os.environ.get("MCLI_CONFIRM_TIMEOUT", 60))
_PREVIEW_LIMIT = 300


def _preview(server: str, tool: str, arguments: dict) -> str:
    args = json.dumps(arguments or {}, ensure_ascii=False, default=str)
    if len(args) > _PREVIEW_LIMIT:
        args = args[:_PREVIEW_LIMIT] + "...(truncated)"
    return args


def confirm_sync(server: str, tool: str, arguments: dict) -> bool:
    """Blocking native dialog. True = approved."""
    msg = (
        "mcli 权限确认\n\n"
        f"服务: {server}\n"
        f"工具: {tool}\n"
        f"参数: {_preview(server, tool, arguments)}\n\n"
        "是否允许执行该操作？"
    )
    system = platform.system()
    try:
        if system == "Windows":
            return _confirm_windows(msg)
        if system == "Darwin":
            return _confirm_macos(msg)
        if system == "Linux":
            return _confirm_linux(msg)
    except Exception:
        return False
    return False


def _confirm_windows(msg: str) -> bool:
    ps_msg = msg.replace("'", "''")
    script = (
        "Add-Type -AssemblyName PresentationFramework; "
        "$r = [System.Windows.MessageBox]::Show("
        f"'{ps_msg}', 'mcli permission', "
        "[System.Windows.MessageBoxButton]::YesNo, "
        "[System.Windows.MessageBoxImage]::Warning, "
        "[System.Windows.MessageBoxResult]::No); "
        "if ($r -eq 'Yes') { exit 0 } else { exit 1 }"
    )
    r = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        timeout=CONFIRM_TIMEOUT_S, capture_output=True)
    return r.returncode == 0


def _confirm_macos(msg: str) -> bool:
    osa_msg = msg.replace("\\", "\\\\").replace('"', '\\"')
    script = (
        f'display dialog "{osa_msg}" with title "mcli permission" '
        'buttons {"Deny", "Allow"} default button "Deny" with icon caution'
    )
    r = subprocess.run(["osascript", "-e", script],
                       timeout=CONFIRM_TIMEOUT_S, capture_output=True, text=True)
    return r.returncode == 0 and "Allow" in (r.stdout or "")


def _confirm_linux(msg: str) -> bool:
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False
    zenity = shutil.which("zenity")
    if not zenity:
        return False
    r = subprocess.run(
        [zenity, "--question", "--title=mcli permission",
         f"--text={msg}", "--default-cancel"],
        timeout=CONFIRM_TIMEOUT_S, capture_output=True)
    return r.returncode == 0


async def confirm(server: str, tool: str, arguments: dict) -> bool:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, confirm_sync, server, tool, arguments)
