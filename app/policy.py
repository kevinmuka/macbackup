"""Internal macbackup policy module."""

from __future__ import annotations

import atexit
from collections import Counter
from dataclasses import asdict, dataclass
import fnmatch
import os
from pathlib import Path
import select
import shutil
import stat
import subprocess
import tempfile

from .i18n import localized, message, print


PATH_LISTS = ("plain-paths.txt", "private-paths.txt", "exclude-paths.txt")



PATTERN_FILE = "explicit-only-patterns.txt"



CONFIG_FILES = (*PATH_LISTS, PATTERN_FILE)



SETTINGS_TEMPLATE = '''# No passwords: credentials remain in the rclone configuration.
schema_version = 1
max_delete = 25

[policy]
private_directory_names = []
ignored_file_names = [".DS_Store"]
use_gitignore = false

[targets.google]
enabled = false
plain = "gdrive:MacBackup/plain"
private = "gdrive-private:"

[targets.proton]
enabled = false
plain = "pdrive:MacBackup/plain"
private = "pdrive-private:"
'''



class ConfigError(Exception):
    pass



class GitIgnoreMatcher:
    """Ask Git to evaluate only .gitignore files, without a repository index."""
    def __init__(self):
        executable = shutil.which('git')
        if executable is None:
            raise ConfigError('use_gitignore requires Git in PATH.')
        self.temporary = tempfile.TemporaryDirectory(prefix='macbackup-gitignore-')
        gitdir = str(Path(self.temporary.name) / 'repo.git')
        self.environment = {key: value for key, value in os.environ.items()
                            if not key.startswith('GIT_')}
        initialized = subprocess.run([executable, 'init', '--bare', '-q', gitdir],
                                     env=self.environment, capture_output=True)
        if initialized.returncode:
            self.temporary.cleanup()
            raise ConfigError('Could not initialize .gitignore checking: '
                              + initialized.stderr.decode('utf-8', 'replace').strip())
        command = [executable, '--git-dir', gitdir, '--work-tree', '/',
                   '-c', 'core.bare=false', '-c', 'core.excludesFile=/dev/null',
                   'check-ignore', '--no-index', '-z', '-v', '-n', '--stdin']
        try:
            self.process = subprocess.Popen(command, env=self.environment,
                                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE)
        except OSError as exc:
            self.temporary.cleanup()
            raise ConfigError(f'Could not start .gitignore checking: {exc}') from exc
        self.pending = b''
        self.cache: dict[Path, tuple[bool, str | None]] = {}
        atexit.register(self.close)

    def preload(self, paths: list[Path]) -> None:
        waiting = list(dict.fromkeys(path for path in paths if path not in self.cache))
        assert self.process.stdin is not None and self.process.stdout is not None
        try:
            for offset in range(0, len(waiting), 16):
                group = waiting[offset:offset + 16]
                # Resolve symlinked ancestors, but never follow an entry itself.
                physicals = [Path(os.path.realpath(path.parent)) / path.name for path in group]
                self.process.stdin.write(b''.join(os.fsencode(str(path)) + b'\0'
                                                  for path in physicals))
                self.process.stdin.flush()
                for path, physical in zip(group, physicals):
                    while self.pending.count(b'\0') < 4:
                        ready, _, _ = select.select([self.process.stdout], [], [], 30)
                        if not ready:
                            raise ConfigError('No response from Git during .gitignore checking.')
                        chunk = os.read(self.process.stdout.fileno(), 4096)
                        if not chunk:
                            detail = self.process.stderr.read().decode('utf-8', 'replace').strip()
                            raise ConfigError('.gitignore checking stopped: ' + detail)
                        self.pending += chunk
                    fields = self.pending.split(b'\0', 4)
                    self.pending = fields[4]
                    source, line, pattern, returned = [field.decode('utf-8', 'replace')
                                                       for field in fields[:4]]
                    if returned != str(physical):
                        raise ConfigError('Inconsistent .gitignore response from Git.')
                    ignored = bool(pattern) and not pattern.startswith('!')
                    if ignored and source and not source.startswith('/'):
                        source = '/' + source
                    rule = f'{source}:{line}:{pattern}' if ignored else None
                    self.cache[path] = (ignored, rule)
        except OSError as exc:
            raise ConfigError(f'Could not check .gitignore: {exc}') from exc

    def match(self, path: Path) -> tuple[bool, str | None]:
        self.preload([path])
        return self.cache[path]

    def close(self) -> None:
        process = getattr(self, 'process', None)
        if process is not None:
            try:
                if process.stdin is not None:
                    process.stdin.close()
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
                process.wait()
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
            self.process = None
        temporary = getattr(self, 'temporary', None)
        if temporary is not None:
            temporary.cleanup()
            self.temporary = None

    def __del__(self):
        self.close()



def absolute_path(value: str) -> Path:
    if not value.startswith("/") or ".." in value.split("/"):
        raise ConfigError("An absolute path without '..' components is required.")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ConfigError("Paths cannot contain control characters.")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise ConfigError("The path cannot be represented correctly in UTF-8.") from exc
    return Path("/" + os.path.normpath(value).lstrip("/"))



def inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents



@dataclass(frozen=True)
class Rule:
    path: Path
    directory: bool
    source: str
    line: int

    def matches(self, path: Path) -> bool:
        return path == self.path or (self.directory and inside(path, self.path))



@dataclass(frozen=True)
class NamePattern:
    value: str
    directory: bool
    line: int

    def matches(self, name: str) -> bool:
        return fnmatch.fnmatchcase(name.casefold(), self.value.casefold())

    @property
    def display(self) -> str:
        return self.value + ('/' if self.directory else '')



def _glob_states(pattern: str, states: frozenset[int]) -> frozenset[int]:
    expanded = set(states)
    for position in sorted(states):
        while position < len(pattern) and pattern[position] == '*':
            position += 1
            expanded.add(position)
    return frozenset(expanded)



def _glob_step(pattern: str, states: frozenset[int], char: str) -> frozenset[int]:
    following = set()
    for position in states:
        if position == len(pattern):
            continue
        if pattern[position] == '*':
            following.add(position)
        elif pattern[position] == char:
            following.add(position + 1)
    return _glob_states(pattern, frozenset(following))



def _glob_relation(first: str, second: str) -> tuple[bool, bool, bool]:
    """Return overlap, first subset of second, second subset of first for * globs."""
    first, second = first.casefold(), second.casefold()
    alphabet = set(first + second) - {'*'}
    other = '\0'
    while other in alphabet:
        other = chr(ord(other) + 1)
    alphabet.add(other)
    initial = (_glob_states(first, frozenset({0})),
               _glob_states(second, frozenset({0})))
    pending = [initial]
    seen = {initial}
    first_subset = second_subset = True
    intersection = False
    while pending:
        left, right = pending.pop()
        left_accept, right_accept = len(first) in left, len(second) in right
        intersection |= left_accept and right_accept
        if left_accept and not right_accept:
            first_subset = False
        if right_accept and not left_accept:
            second_subset = False
        for char in alphabet:
            successor = (_glob_step(first, left, char), _glob_step(second, right, char))
            if successor not in seen:
                seen.add(successor)
                pending.append(successor)
    return intersection, first_subset, second_subset



@dataclass(frozen=True)
class Decision:
    action: str
    reason: str
    rule: str | None = None
    protected_pattern: str | None = None
    explicit: bool = False



@dataclass
class Entry:
    path: str
    kind: str
    size: int
    decision: Decision
    device: int
    inode: int

    def public_dict(self) -> dict:
        return {
            "path": self.path,
            "kind": self.kind,
            "size": self.size,
            **asdict(self.decision),
        }



@dataclass
class Scan:
    entries: list[Entry]
    errors: list[str]
    warnings: list[str]

    def summary(self) -> dict:
        counts = Counter(entry.decision.action for entry in self.entries)
        return {
            "plain": counts["plain"],
            "private": counts["private"],
            "skipped": counts["skip"],
            "finder_icons": sum(entry.kind == 'finder-icon' and entry.decision.action in ('plain', 'private') for entry in self.entries),
            "selected_bytes": sum(
                entry.size for entry in self.entries
                if entry.decision.action in ("plain", "private")
            ),
        }



def read_lines(file: Path) -> list[tuple[int, str]]:
    try:
        # These files are user policy, not data selected for backup.
        lines = file.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"Could not read {file}: {exc}") from exc
    result = []
    for number, value in enumerate(lines, 1):
        if not value.strip() or value.startswith("#"):
            continue
        if value != value.strip():
            raise ConfigError(f"{file}:{number}: ambiguous leading or trailing spaces.")
        result.append((number, value))
    return result



class Policy:
    def __init__(self, config_dir: Path, home: Path):
        self.config_dir = config_dir
        self.home = home
        self.state_dir = home / ".local/state/macbackup"
        self.state_physical = Path(os.path.realpath(self.state_dir))
        self.plain = self._rules("plain-paths.txt")
        self.private = self._rules("private-paths.txt")
        self.excluded = self._rules("exclude-paths.txt")
        self.warnings: list[str] = []
        paths: dict[Path, Rule] = {}
        for rule in self.plain + self.private + self.excluded:
            prior = paths.get(rule.path)
            if prior is not None:
                raise ConfigError(f'{prior.source}:{prior.line} and {rule.source}:{rule.line}: '
                                  f'duplicate path: {rule.path}')
            paths[rule.path] = rule
        for root in self.roots:
            ancestor = next((excluded for excluded in self.excluded
                             if excluded.directory and excluded.path != root.path
                             and inside(root.path, excluded.path)), None)
            if ancestor is not None:
                self.warnings.append(message(
                    '{source}:{line}: {path} suspended by {other_source}:{other_line}: {other_path}.',
                    source=root.source, line=root.line, path=root.path,
                    other_source=ancestor.source, other_line=ancestor.line,
                    other_path=ancestor.path,
                ))
        try:
            import tomllib
        except ImportError as exc:
            raise ConfigError('Name rules require Python 3.11 or later.') from exc
        try:
            settings = tomllib.loads((config_dir / 'settings.toml').read_text())
        except (OSError, ValueError) as exc:
            raise ConfigError(f'Could not read settings.toml: {exc}') from exc
        policy_settings = settings.get('policy', {})
        target_names = settings.get('targets', {})
        if not isinstance(target_names, dict):
            raise ConfigError('settings.toml: targets must be a table.')
        self.mount_roots = tuple(
            home / 'Desktop' / (name if area == 'all' else f'{name}-{area}')
            for name in target_names for area in ('plain', 'private', 'all')
        )
        if not isinstance(policy_settings, dict):
            raise ConfigError('settings.toml: policy must be a table.')
        names = policy_settings.get('private_directory_names', [])
        if not isinstance(names, list) or any(
            not isinstance(name, str) or not name or name in ('.', '..')
            or name.strip() != name or '/' in name
            or any(char in name for char in '*?[]{}\\')
            or any(ord(char) < 32 or ord(char) == 127 for char in name)
            for name in names
        ):
            raise ConfigError('settings.toml: private_directory_names requires literal directory names.')
        if len({name.casefold() for name in names}) != len(names):
            raise ConfigError('settings.toml: private_directory_names contains duplicate names.')
        self.private_directory_names = {name.casefold() for name in names}
        ignored = policy_settings.get('ignored_file_names', [])
        if not isinstance(ignored, list) or any(
            not isinstance(name, str) or not name or name in ('.', '..')
            or '/' in name
            or any(char in name for char in '*?[]{}\\')
            or any(ord(char) < 32 or ord(char) == 127 for char in name)
            for name in ignored
        ):
            raise ConfigError('settings.toml: ignored_file_names requires literal file names.')
        if len({name.casefold() for name in ignored}) != len(ignored):
            raise ConfigError('settings.toml: ignored_file_names contains duplicate names.')
        self.ignored_file_names = {name.casefold() for name in ignored}
        use_gitignore = policy_settings.get('use_gitignore', False)
        if type(use_gitignore) is not bool:
            raise ConfigError('settings.toml: use_gitignore must be true or false.')
        self.patterns: list[NamePattern] = []
        for number, pattern in read_lines(config_dir / PATTERN_FILE):
            directory = pattern.endswith('/')
            name = pattern[:-1] if directory else pattern
            invalid = (
                not name or "/" in name or "**" in name
                or any(char in pattern for char in "?[]{}\\")
                or pattern.startswith(("+", "-"))
                or any(ord(char) < 32 or ord(char) == 127 for char in pattern)
            )
            if invalid:
                raise ConfigError(
                    f"{PATTERN_FILE}:{number}: only names and '*' are allowed, "
                    "with a trailing '/' for directories."
                )
            candidate = NamePattern(name, directory, number)
            if directory:
                for private_name in names:
                    if candidate.matches(private_name):
                        raise ConfigError(f'{PATTERN_FILE}:{number} ({candidate.display}) and '
                                          f'settings.toml:[policy].private_directory_names '
                                          f'({private_name}): redundant rules.')
            else:
                for ignored_name in ignored:
                    if candidate.matches(ignored_name):
                        raise ConfigError(f'{PATTERN_FILE}:{number} ({candidate.display}) and '
                                          f'settings.toml:[policy].ignored_file_names '
                                          f'({ignored_name}): overlapping protections.')
            for prior in self.patterns:
                if prior.directory != candidate.directory:
                    continue
                intersects, prior_subset, candidate_subset = _glob_relation(
                    prior.value, candidate.value)
                if prior_subset or candidate_subset:
                    raise ConfigError(f'{PATTERN_FILE}:{prior.line} ({prior.display}) and '
                                      f'{PATTERN_FILE}:{number} ({candidate.display}): '
                                      'redundant patterns.')
                if intersects:
                    self.warnings.append(f'{PATTERN_FILE}:{prior.line} ({prior.display}) and '
                                         f'{PATTERN_FILE}:{number} ({candidate.display}): '
                                         'partially overlapping patterns.')
            self.patterns.append(candidate)
        self.gitignore = GitIgnoreMatcher() if use_gitignore else None
        credential_roots = [
            home / ".config/rclone",
            home / "Library/Preferences/rclone",
        ]
        custom_config = os.environ.get('RCLONE_CONFIG')
        if custom_config:
            credential_roots.append(Path(os.path.abspath(custom_config)))
        self.credential_roots = tuple({
            *credential_roots,
            *(Path(os.path.realpath(root)) for root in credential_roots),
        })

    def _rules(self, filename: str) -> list[Rule]:
        result = []
        seen: dict[Path, int] = {}
        for line, value in read_lines(self.config_dir / filename):
            try:
                path = absolute_path(value)
            except ConfigError as exc:
                raise ConfigError(f"{filename}:{line}: {exc}") from exc
            if path == Path("/"):
                raise ConfigError(f"{filename}:{line}: the root '/' is not allowed.")
            if path in seen:
                raise ConfigError(f'{filename}:{seen[path]} and {filename}:{line}: '
                                  f'duplicate path: {path}')
            seen[path] = line
            directory = value.endswith('/')
            try:
                info = path.lstat()
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise ConfigError(f'{filename}:{line}: {path}: {exc}') from exc
            else:
                if stat.S_ISDIR(info.st_mode) != directory:
                    expected = 'directory with trailing /' if stat.S_ISDIR(info.st_mode) else 'file or symlink without trailing /'
                    raise ConfigError(f'{filename}:{line}: {path}: type mismatch; '
                                      f'expected {expected}.')
            result.append(Rule(path, directory, filename, line))
        return result

    @property
    def roots(self) -> list[Rule]:
        return self.plain + self.private

    def decide(self, path: Path, directory: bool = False,
               check_gitignore: bool = True) -> Decision:
        physical = Path(os.path.realpath(path))
        if inside(path, self.state_dir) or inside(physical, self.state_physical):
            return Decision("skip", "Operational state excluded automatically.", str(self.state_dir))
        mount_root = next((root for root in self.mount_roots if inside(path, root)), None)
        if mount_root:
            return Decision('skip', 'Mountpoint excluded automatically.', str(mount_root))
        excluded = next((rule for rule in self.excluded if rule.matches(path)), None)
        if excluded:
            return Decision("skip", "Absolute exclusion.", str(excluded.path))
        private = next((rule for rule in self.private if rule.matches(path)), None)
        plain = next((rule for rule in self.plain if rule.matches(path)), None)
        if not (private or plain):
            return Decision("skip", "Path not selected.")
        exact = any(rule.path == path for rule in self.roots)
        if not directory and path.name.casefold() in self.ignored_file_names and not exact:
            return Decision('skip', 'Filename ignored by policy.',
                            'settings.toml:[policy].ignored_file_names')
        named_directories = ([path] if directory else []) + list(path.parents)
        private_by_name = any(
            ancestor != Path('/')
            and ancestor.name.casefold() in self.private_directory_names
            and not any(rule.path == ancestor for rule in self.plain)
            for ancestor in named_directories
        )
        # Classify remote files using the same directory pruning as the local
        # scanner. Otherwise an older cloud copy under a newly protected
        # directory would be mistaken for a local deletion and archived.
        for ancestor in path.parents:
            if ancestor == Path('/'):
                break
            protected_ancestor = next(
                (pattern for pattern in self.patterns
                 if pattern.directory and pattern.matches(ancestor.name)), None
            )
            if (protected_ancestor and ancestor.name.casefold() not in self.private_directory_names
                    and not any(rule.path == ancestor for rule in self.roots)):
                return Decision(
                    "skip", "Protected parent directory: exact inclusion required.",
                    protected_pattern=protected_ancestor.display,
                )
        protected = next(
            (pattern for pattern in self.patterns
             if pattern.directory == directory and pattern.matches(path.name)), None
        )
        if protected and not exact and not (directory and path.name.casefold() in self.private_directory_names):
            return Decision(
                "skip", "Exact path inclusion required (explicit-only).",
                protected_pattern=protected.display,
            )
        if check_gitignore and self.gitignore is not None and not exact and not private_by_name:
            ignored, source = self.gitignore.match(path)
            if ignored:
                return Decision('skip', 'Excluded by .gitignore.', source)
        if private:
            return Decision("private", "Private inclusion.", str(private.path),
                            protected.display if protected else None, exact)
        if private_by_name:
            return Decision("private", "Directory encrypted by name.",
                            'settings.toml:[policy].private_directory_names',
                            protected.display if protected else None, exact)
        if not directory and any(inside(path, root) or inside(physical, root) for root in self.credential_roots):
            return Decision(
                "error", "rclone configuration selected as plain: add "
                "the path to private-paths.txt or exclude it.", str(plain.path)
            )
        return Decision("plain", "Plain inclusion.", str(plain.path),
                        protected.display if protected else None, exact)



def file_kind(info: os.stat_result) -> str:
    if stat.S_ISDIR(info.st_mode):
        return "directory"
    if stat.S_ISREG(info.st_mode):
        return "file"
    if stat.S_ISLNK(info.st_mode):
        return "symlink"
    return "special"



def init_config(directory: Path, home: Path) -> list[str]:
    templates = {
        "plain-paths.txt": "# Directories have trailing /; files and symlinks do not. No active inclusions.\n"
        f"# {home}/Desktop/\n# {home}/Documents/\n# {home}/.config/\n# {home}/.dotfiles/\n",
        "private-paths.txt": "# Directories have trailing /; files and symlinks do not. No active inclusions.\n"
        f"# {home}/.ssh/\n# {home}/.config/rclone/\n",
        "exclude-paths.txt": "# Absolute exclusions; directories have trailing /, files do not. No active exclusions.\n",
        PATTERN_FILE: "# Directory names have trailing /; file/symlink names do not. Exact inclusion required.\n.env\n.env.*\n.env/\n.env.*/\n",
    }
    templates["settings.toml"] = SETTINGS_TEMPLATE
    if any(os.path.lexists(directory / name) for name in templates):
        raise ConfigError("Configuration already exists, even partially; no files overwritten.")
    if directory.is_symlink():
        raise ConfigError("The configuration directory cannot be a symlink for init.")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    created = []
    try:
        for name, contents in templates.items():
            path = directory / name
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            created.append(path)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(localized(contents))
    except OSError:
        for path in created:
            path.unlink(missing_ok=True)
        raise
    return [str(path) for path in created]
