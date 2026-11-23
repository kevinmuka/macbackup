"""Internal macbackup state module."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import tempfile

from .i18n import localized_json, print
from .policy import ConfigError


@contextmanager
def operation_lock(state: Path):
    if state.is_symlink():
        raise ConfigError("The state directory cannot be a symlink.")
    state.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(state, 0o700)
    fd = os.open(state / "operation.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ConfigError("Another macbackup operation is already running.") from exc
        yield
    finally:
        os.close(fd)



def save_local_report(state: Path, filename: str, data: dict) -> None:
    """Replace the private local report without exposing a partial JSON file."""
    data['updated_at'] = datetime.now(timezone.utc).isoformat()
    fd, temporary = tempfile.mkstemp(prefix='report-', dir=state)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(localized_json(data), stream, ensure_ascii=False, indent=2)
        os.replace(temporary, state / filename)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)



def display_safe_path(original: str) -> str:
    def escape(match):
        return {'\r': r'\r', '\n': r'\n', '\t': r'\t'}.get(
            match.group(), '\\x%02x' % ord(match.group()))
    return re.sub(r'[\x00-\x1f\x7f]', escape, original)
