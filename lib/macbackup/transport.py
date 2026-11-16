"""Internal macbackup transport module."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import select
import shutil
import signal
import subprocess
import sys

from .i18n import message, print
from .policy import ConfigError
from .ui import ProgressDisplay


class TransportError(Exception):
    pass



class TransferProblem(TransportError):
    def __init__(self, message: str, stats: dict | None = None):
        super().__init__(message)
        self.stats = stats



class TransferInterrupted(Exception):
    def __init__(self, stats: dict | None = None):
        self.stats = stats



def rclone_progress_call(arguments: list[str], home: Path, depth: int, label: str = '') -> dict | None:
    executable = shutil.which('rclone')
    if executable is None:
        raise ConfigError('rclone is not available in PATH.')
    try:
        process = subprocess.Popen(
            [executable, '--ask-password=false', *arguments],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            env=transport_env(),
        )
    except OSError as exc:
        raise TransferProblem(f'Could not start rclone: {exc}') from exc
    latest = None
    errors: list[str] = []
    try:
        assert process.stderr is not None
        pending = b''
        with ProgressDisplay(home, depth, label) as display:
            while True:
                watched = [process.stderr.fileno()]
                if display.interactive:
                    watched.append(sys.stdin.fileno())
                ready, _, _ = select.select(watched, [], [], 0.5)
                if display.interactive and sys.stdin.fileno() in ready:
                    display.key(os.read(sys.stdin.fileno(), 32))
                if process.stderr.fileno() not in ready:
                    continue
                chunk = os.read(process.stderr.fileno(), 65536)
                if not chunk:
                    break
                pending += chunk
                while b'\n' in pending:
                    raw, pending = pending.split(b'\n', 1)
                    line = raw.decode('utf-8', 'replace')
                    try:
                        event = json.loads(line)
                    except ValueError:
                        if line.strip():
                            errors.append(line.rstrip())
                            errors = errors[-5:]
                        continue
                    stats = event.get('stats') if isinstance(event, dict) else None
                    if isinstance(stats, dict):
                        latest = stats
                        display.update(stats)
                    elif isinstance(event, dict) and event.get('level') in ('error', 'fatal'):
                        message = str(event.get('msg', line.rstrip()))
                        errors.append(message)
                        errors = errors[-5:]
                        if not display.interactive:
                            print(message, file=sys.stderr, flush=True)
            code = process.wait()
    except KeyboardInterrupt as exc:
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.communicate()
        raise TransferInterrupted(latest) from exc
    finally:
        if process.stderr is not None:
            process.stderr.close()
    if code:
        raise TransferProblem(f'rclone exited with code {code}: '
                              + ('; '.join(errors)[-1500:] if errors else 'see messages above.'), latest)
    return latest



def transfer_metrics(stats: dict | None, complete: bool) -> dict:
    fields = {'transferred_files': 'transfers', 'transferred_bytes': 'bytes',
              'checked_files': 'checks', 'listed_items': 'listed',
              'renamed_files': 'renames', 'server_side_moves': 'serverSideMoves',
              'server_side_move_bytes': 'serverSideMoveBytes'}
    if not isinstance(stats, dict) or any(type(stats.get(name)) is not int or stats[name] < 0
                                          for name in ('transfers', 'bytes', 'checks')):
        return {name: None for name in fields} | {'counts_complete': False}
    return {name: stats[key] if type(stats.get(key)) is int and stats[key] >= 0 else None
            for name, key in fields.items()} | {'counts_complete': complete}



def transport_env() -> dict[str, str]:
    # Ambient rclone filter/delete overrides must not change the selected scope.
    allowed = {"RCLONE_CONFIG", "RCLONE_CONFIG_PASS", "RCLONE_PASSWORD_COMMAND"}
    return {key: value for key, value in os.environ.items()
            if not key.startswith("RCLONE_") or key in allowed}



def rclone_call(*args: str, missing_ok: bool = False, allowed_codes: tuple = (0,)) -> str:
    executable = shutil.which("rclone")
    if executable is None:
        raise ConfigError("rclone is not available in PATH.")
    try:
        result = subprocess.run(
            [executable, "--ask-password=false", *args],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=transport_env(),
        )
    except OSError as exc:
        raise TransportError(f"Could not start rclone: {exc}") from exc
    if result.returncode not in allowed_codes:
        if missing_ok and result.returncode == 3:
            try:
                empty = (result.stdout.strip() in ("", "[", "[]")
                         or json.loads(result.stdout) == [])
            except ValueError:
                empty = False
            if empty and "directory not found" in result.stderr.lower():
                return "[]"
        detail = result.stderr.strip()[-3000:] if result.stderr else 'see rclone messages in the terminal.'
        raise TransportError(f"rclone exited with code {result.returncode}: " + detail)
    return result.stdout or ''



def load_targets(config: Path, target: str, require_enabled: bool = True) -> tuple[dict, int]:
    try:
        import tomllib
    except ImportError as exc:
        raise ConfigError("rclone operations require Python 3.11 or later.") from exc
    try:
        settings = tomllib.loads((config / "settings.toml").read_text())
    except (OSError, ValueError) as exc:
        raise ConfigError(f"Could not read settings.toml: {exc}") from exc
    if type(settings.get("schema_version")) is not int or settings.get("schema_version") != 1:
        raise ConfigError("settings.toml: schema_version must be 1.")
    limit = settings.get("max_delete", 25)
    if type(limit) is not int or not 0 <= limit <= 10000:
        raise ConfigError("max_delete must be an integer from 0 to 10000.")
    names = ("google", "proton") if target == "all" else (target,)
    configured = settings.get("targets", {})
    if not isinstance(configured, dict):
        raise ConfigError("settings.toml: targets must be a table.")
    chosen = {}
    for name in names:
        item = configured.get(name, {})
        if not isinstance(item, dict):
            raise ConfigError(f"settings.toml: targets.{name} must be a table.")
        if require_enabled and item.get("enabled") is not True:
            raise ConfigError(f"Target {name} is not enabled in settings.toml.")
        for area in ("plain", "private"):
            value = item.get(area)
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+:[^\x00-\x1f\x7f]*", value):
                raise ConfigError(f"Target {name}/{area} is invalid.")
            tail = value.split(":", 1)[1]
            if ":" in tail or tail.startswith("/") or ".." in tail.split("/"):
                raise ConfigError(f"Target {name}/{area}: use a safe relative remote path.")
            if area == "plain" and not tail.rstrip("/").endswith("/plain"):
                raise ConfigError("The plain area must be dedicated and end with '/plain'.")
        chosen[name] = {area: item[area].rstrip("/") for area in ("plain", "private")}
    remotes = {item["name"]: item["type"] for item in json.loads(rclone_call("listremotes", "--json"))}
    for name, areas in chosen.items():
        for area, remote in areas.items():
            key = remote.split(":", 1)[0]
            if key not in remotes:
                raise ConfigError(f"Remote {key} is not configured. Complete rclone config first.")
            if area == "private" and remotes[key] != "crypt":
                raise ConfigError(f"{name}/private must use a crypt remote; transfer blocked.")
            if area == "plain":
                features = json.loads(rclone_call("backend", "features", key + ":"))
                if not features.get("Hashes"):
                    raise ConfigError(f"{name}/plain does not provide hashes for a reliable comparison; operation blocked.")
    return chosen, limit



def remote_join(root: str, relative: str) -> str:
    return root + ("" if root.endswith(":") else "/") + relative


def write_manifest(directory: Path, names: list[str]) -> Path:
    path = directory / "files.list0"
    with path.open("xb") as stream:
        os.chmod(path, 0o600)
        for name in names:
            stream.write(name.encode("utf-8") + b"\0")
    return path
