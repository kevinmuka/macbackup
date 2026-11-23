"""Internal macbackup scan module."""

from __future__ import annotations

from collections import defaultdict
import os
from pathlib import Path
from typing import TYPE_CHECKING

from .i18n import print
from .icons import FINDER_ICON
from .policy import ConfigError, Decision, Entry, Policy, Scan, absolute_path, file_kind, inside

if TYPE_CHECKING:
    from .ui import MountPulse


def scan_policy(policy: Policy, pulse: MountPulse | None = None) -> Scan:
    result = Scan([], [], list(policy.warnings))
    seen: set[Path] = set()

    def visit(path: Path, parent_fd: int | None = None, child: str | None = None) -> None:
        if path in seen:
            return
        seen.add(path)
        if pulse is not None:
            pulse.completed += 1
        # Absolute exclusions are authoritative, even for missing included roots.
        preliminary = policy.decide(path, directory=True, check_gitignore=False)
        if preliminary.action == "skip" and preliminary.rule:
            result.entries.append(Entry(str(path), "excluded-subtree", 0, preliminary, 0, 0))
            return
        try:
            info = (os.stat(child, dir_fd=parent_fd, follow_symlinks=False)
                    if parent_fd is not None else path.lstat())
            kind = file_kind(info)
            if path.name == FINDER_ICON:
                if kind != 'file':
                    result.errors.append(f"{path.parent}: Icon\\r must be a regular file.")
                    return
                kind = 'finder-icon'
            decision = policy.decide(path, directory=kind == "directory")
            if (policy.gitignore is not None and decision.action in ('plain', 'private')
                    and (any(rule.path == path for rule in policy.roots)
                         or (kind == 'directory'
                             and path.name.casefold() in policy.private_directory_names
                             and decision.action == 'private'))):
                ignored, source = policy.gitignore.match(path)
                if ignored:
                    direct = next((rule for rule in policy.roots if rule.path == path), None)
                    overriding = (f'{direct.source}:{direct.line}' if direct else
                                  'settings.toml:[policy].private_directory_names')
                    result.warnings.append(f'{path}: {overriding} overrides '
                                           f'.gitignore {source}.')
            if decision.action == "error":
                result.errors.append(f"{path}: {decision.reason}")
                return
            if kind == "directory":
                if decision.action == "skip":
                    if decision.protected_pattern or decision.reason == 'Excluded by .gitignore.':
                        result.entries.append(Entry(str(path), "excluded-subtree", 0, decision, 0, 0))
                    return
                # Anchor traversal to directory descriptors and refuse replaced
                # symlinks. Explicit root paths may have symlinked ancestors.
                flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                fd = (os.open(child, flags, dir_fd=parent_fd)
                      if parent_fd is not None else os.open(path, flags))
                try:
                    opened = os.fstat(fd)
                    if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                        raise OSError("The directory changed during the check.")
                    with os.scandir(fd) as iterator:
                        names = sorted(entry.name for entry in iterator)
                    valid_names = []
                    for name in names:
                        try:
                            absolute_path(str(path)) if name == FINDER_ICON else absolute_path(str(path / name))
                        except ConfigError as exc:
                            result.errors.append(f"Name cannot be represented in {path}: {exc}")
                            continue
                        valid_names.append(name)
                    if policy.gitignore is not None:
                        policy.gitignore.preload([path / name for name in valid_names])
                    for name in valid_names:
                        visit(path / name, fd, name)
                finally:
                    os.close(fd)
                return
            if kind == "special":
                if any(rule.path == path for rule in policy.roots):
                    result.errors.append(f"{path}: unsupported special source.")
                else:
                    result.warnings.append(f"{path}: socket, FIFO or device skipped.")
                    decision = Decision("skip", "Unsupported special file.")
            if decision.action in ("plain", "private"):
                if kind in ("file", "finder-icon") and not os.access(path, os.R_OK):
                    result.errors.append(f"{path}: unreadable file.")
                if kind == "file" and path.name.endswith(".rclonelink"):
                    result.errors.append(f"{path}: name reserved for rclone link representation.")
                if kind == "symlink":
                    result.warnings.append(f"{path}: symlink; target not traversed.")
                if decision.action == "plain" and decision.protected_pattern:
                    result.warnings.append(f"{path}: protected name EXPLICITLY allowed as plain.")
            result.entries.append(Entry(
                str(path), kind, info.st_size, decision, info.st_dev, info.st_ino
            ))
        except OSError as exc:
            result.errors.append(f"{path}: {exc}")

    for rule in sorted(policy.roots, key=lambda item: (len(item.path.parts), str(item.path))):
        visit(rule.path)

    identities: dict[tuple[int, int], list[Entry]] = defaultdict(list)
    for entry in result.entries:
        if entry.kind in ("file", "finder-icon"):
            identities[(entry.device, entry.inode)].append(entry)
    for aliases in identities.values():
        selected = [entry for entry in aliases if entry.decision.action in ("plain", "private")]
        if {entry.decision.action for entry in selected} == {"plain", "private"}:
            result.errors.append(
                "Same file reached as both plain and private: "
                + ", ".join(entry.path for entry in selected)
            )
        protected = [entry for entry in aliases if entry.decision.protected_pattern]
        for entry in selected:
            if protected and not entry.decision.explicit and not entry.decision.protected_pattern:
                result.errors.append(
                    f"{entry.path}: alias of a protected filename; "
                    "this path also requires exact inclusion."
                )
    result.entries.sort(key=lambda entry: entry.path)
    result.errors = sorted(set(result.errors))
    result.warnings = sorted(set(result.warnings))
    return result


def split_layout(policy: Policy, root: Path, pulse: MountPulse | None = None) -> dict:
    if not root.is_dir() or root.is_symlink():
        raise ConfigError('layout requires a real local directory.')
    if policy.decide(root, directory=True).action != 'plain':
        raise ConfigError('layout requires a directory selected as plain.')
    scan = scan_policy(policy, pulse)
    if scan.errors:
        return {'scope': 'local-only', 'ok': False, 'path': str(root),
                'errors': scan.errors, 'warnings': scan.warnings, 'private_subtrees': []}
    groups: dict[str, dict] = {}
    plain_count = 0
    private_count = 0
    action_cache: dict[Path, str] = {}

    def directory_action(path: Path) -> str:
        if path not in action_cache:
            action_cache[path] = policy.decide(path, directory=True).action
        return action_cache[path]

    for entry in scan.entries:
        path = Path(entry.path)
        if not inside(path, root) or entry.decision.action not in ('plain', 'private'):
            continue
        if entry.decision.action == 'plain':
            plain_count += 1
            continue
        private_count += 1
        boundary = path
        while boundary.parent != boundary and inside(boundary.parent, root):
            parent_action = directory_action(boundary.parent)
            if parent_action == 'plain':
                key = str(boundary)
                item = groups.setdefault(key, {
                    'path': key, 'kind': entry.kind if boundary == path else 'directory',
                    'private_items': 0, 'selected_bytes': 0,
                    'logical_destination': 'private/current' + key,
                })
                item['private_items'] += 1
                item['selected_bytes'] += entry.size
                break
            if parent_action != 'private':
                break
            boundary = boundary.parent
    return {'scope': 'local-only', 'ok': True, 'path': str(root),
            'plain_items': plain_count, 'private_items': private_count,
            'private_subtrees': [groups[key] for key in sorted(groups)],
            'warnings': scan.warnings,
            'note': 'Current local classification; no cloud access. Historical copies are not listed.'}


def selected_names(scan: Scan, area: str) -> set[str]:
    return {entry.path.lstrip("/") + (".rclonelink" if entry.kind == "symlink" else "")
            for entry in scan.entries if entry.decision.action == area and entry.kind != 'finder-icon'}
