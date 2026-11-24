"""Human-in-the-loop confirmation via native OS dialogs (Non-TTY safe).

Returns True only if the user explicitly approves. Timeout, denial, missing
dialog tooling, or headless environments all safely default to False.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import platform
import shutil
import subprocess

CONFIRM_TIMEOUT_S = float(os.environ.get("MCLI_CONFIRM_TIMEOUT", 60))
_PREVIEW_LIMIT = 300
logger = logging.getLogger(__name__)


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
    except subprocess.TimeoutExpired:
        logger.warning("Approval timed out for %s.%s", server, tool)
        return False
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("Approval dialog failed for %s.%s (%s)", server, tool, type(exc).__name__)
        return False
    logger.warning("Approval dialogs are unavailable on %s", system)
    return False


def _confirm_windows(msg: str) -> bool:
    encoded = base64.b64encode(msg.encode("utf-8")).decode("ascii")
    script = (
        "$ErrorActionPreference = 'Stop'; "
        "Add-Type -AssemblyName PresentationFramework; "
        f"$msg = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded}')); "
        "$r = [System.Windows.MessageBox]::Show("
        "$msg, 'mcli permission', "
        "[System.Windows.MessageBoxButton]::YesNo, "
        "[System.Windows.MessageBoxImage]::Warning, "
        "[System.Windows.MessageBoxResult]::No); "
        "if ($r -eq 'Yes') { exit 0 } else { exit 3 }"
    )
    r = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-STA", "-Command", script],
        timeout=CONFIRM_TIMEOUT_S, capture_output=True)
    if r.returncode == 3:
        logger.info("Windows approval denied")
    elif r.returncode != 0:
        logger.warning("Windows approval dialog failed (exit %s)", r.returncode)
    return r.returncode == 0


def _confirm_macos(msg: str) -> bool:
    osa_msg = msg.replace("\\", "\\\\").replace('"', '\\"')
    script = (
        f'display dialog "{osa_msg}" with title "mcli permission" '
        'buttons {"Deny", "Allow"} default button "Deny" with icon caution'
    )
    r = subprocess.run(["osascript", "-e", script],
                       timeout=CONFIRM_TIMEOUT_S, capture_output=True, text=True)
    if r.returncode != 0:
        logger.warning("macOS approval dialog failed (exit %s)", r.returncode)
        return False
    approved = r.stdout.strip() == "button returned:Allow"
    if not approved:
        logger.info("macOS approval denied")
    return approved


def _confirm_linux(msg: str) -> bool:
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        logger.warning("Linux approval unavailable: no display")
        return False
    zenity = shutil.which("zenity")
    if not zenity:
        logger.warning("Linux approval unavailable: zenity missing")
        return False
    r = subprocess.run(
        [zenity, "--question", "--title=mcli permission",
         f"--text={msg}", "--default-cancel"],
        timeout=CONFIRM_TIMEOUT_S, capture_output=True)
    if r.returncode == 1 and not r.stderr:
        logger.info("Linux approval denied")
    elif r.returncode != 0:
        logger.warning("Linux approval dialog failed (exit %s)", r.returncode)
    return r.returncode == 0


async def confirm(server: str, tool: str, arguments: dict) -> bool:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, confirm_sync, server, tool, arguments)
