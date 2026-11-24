"""Configuration-scoped state shared by the CLI and daemon."""

import hashlib
import os
import sys
from contextlib import contextmanager
from pathlib import Path

from .config import find_config_path


def state_dir() -> Path:
    if override := os.environ.get("MCLI_STATE_DIR"):
        base = Path(override)
    elif sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "mcli"
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "mcli"
    config = os.path.normcase(str(find_config_path().resolve()))
    return base / hashlib.sha256(os.fsencode(config)).hexdigest()[:32]


def make_state_dir() -> Path:
    path = state_dir()
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    if os.name != "nt" and (
        path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077
    ):
        raise PermissionError(f"daemon state requires an owner-only directory: {path}")
    return path


@contextmanager
def daemon_lock(path: Path):
    # The OS releases the lock even if the process exits unexpectedly.
    with path.open("a+b") as lock:
        lock.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, PermissionError):
            yield False
        else:
            yield True
