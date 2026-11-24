"""Human-in-the-loop confirmation via native OS dialogs (Non-TTY safe).

Only an explicit approval permits execution.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
from enum import StrEnum
from pathlib import Path

CONFIRM_TIMEOUT_S = float(os.environ.get("MCLI_CONFIRM_TIMEOUT", 60))
_PREVIEW_LIMIT = 300
logger = logging.getLogger(__name__)


class Approval(StrEnum):
    APPROVED = "approved"
    DENIED = "user_denied"
    TIMED_OUT = "approval_timeout"
    UNAVAILABLE = "approval_unavailable"


def _preview(server: str, tool: str, arguments: dict) -> str:
    args = json.dumps(arguments or {}, ensure_ascii=False, default=str)
    if len(args) > _PREVIEW_LIMIT:
        args = args[:_PREVIEW_LIMIT] + "...(truncated)"
    return args


def confirm_sync(server: str, tool: str, arguments: dict) -> Approval:
    """Show the native dialog and return its outcome."""
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
        return Approval.TIMED_OUT
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("Approval dialog failed for %s.%s (%s)", server, tool, type(exc).__name__)
        return Approval.UNAVAILABLE
    logger.warning("Approval dialogs are unavailable on %s", system)
    return Approval.UNAVAILABLE


def _confirm_windows(msg: str) -> Approval:
    from ._windows_dialog import IDNO, IDYES

    helper = Path(__file__).resolve().with_name("_windows_dialog.py")
    result = subprocess.run(
        [sys.executable, "-I", str(helper)],
        input=msg.encode("utf-8"),
        timeout=CONFIRM_TIMEOUT_S,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode == IDYES:
        return Approval.APPROVED
    if result.returncode == IDNO:
        logger.info("Windows approval denied")
        return Approval.DENIED
    logger.warning("Windows approval dialog unavailable (exit %s)", result.returncode)
    return Approval.UNAVAILABLE


def _confirm_macos(msg: str) -> Approval:
    osa_msg = msg.replace("\\", "\\\\").replace('"', '\\"')
    script = (
        f'display dialog "{osa_msg}" with title "mcli permission" '
        'buttons {"Deny", "Allow"} default button "Deny" with icon caution'
    )
    r = subprocess.run(["osascript", "-e", script],
                       timeout=CONFIRM_TIMEOUT_S, capture_output=True, text=True)
    if r.returncode != 0:
        logger.warning("macOS approval dialog failed (exit %s)", r.returncode)
        return Approval.UNAVAILABLE
    button = r.stdout.strip()
    if button == "button returned:Allow":
        return Approval.APPROVED
    if button == "button returned:Deny":
        logger.info("macOS approval denied")
        return Approval.DENIED
    logger.warning("macOS approval dialog returned an unexpected response")
    return Approval.UNAVAILABLE


def _confirm_linux(msg: str) -> Approval:
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        logger.warning("Linux approval unavailable: no display")
        return Approval.UNAVAILABLE
    zenity = shutil.which("zenity")
    if not zenity:
        logger.warning("Linux approval unavailable: zenity missing")
        return Approval.UNAVAILABLE
    r = subprocess.run(
        [zenity, "--question", "--title=mcli permission",
         f"--text={msg}", "--default-cancel"],
        timeout=CONFIRM_TIMEOUT_S, capture_output=True)
    if r.returncode == 0:
        return Approval.APPROVED
    if r.returncode == 1 and not r.stderr:
        logger.info("Linux approval denied")
        return Approval.DENIED
    logger.warning("Linux approval dialog failed (exit %s)", r.returncode)
    return Approval.UNAVAILABLE


async def confirm(server: str, tool: str, arguments: dict) -> Approval:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, confirm_sync, server, tool, arguments)
