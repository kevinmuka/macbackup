"""Internal macbackup inventory module."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile

from .i18n import print
from .icons import ICON_SIDECAR
from .policy import ConfigError, absolute_path
from .transport import TransportError, rclone_call, remote_join, transport_env
from .ui import MountPulse, mount_input_wait


class JsonArrayObjectCounter:
    """Count complete top-level objects in a streamed JSON array."""

    def __init__(self):
        self.count = 0
        self.array_depth = 0
        self.object_depth = 0
        self.in_string = False
        self.escaped = False

    def feed(self, chunk: bytes) -> None:
        for byte in chunk:
            if self.in_string:
                if self.escaped:
                    self.escaped = False
                elif byte == 92:  # backslash
                    self.escaped = True
                elif byte == 34:  # quote
                    self.in_string = False
                continue
            if byte == 34:
                self.in_string = True
            elif byte == 91:
                self.array_depth += 1
            elif byte == 93:
                self.array_depth -= 1
            elif byte == 123:
                self.object_depth += 1
            elif byte == 125:
                self.object_depth -= 1
                if self.array_depth == 1 and self.object_depth == 0:
                    self.count += 1



def mount_inventory(root: str, label: str, input_fd: int | None = None,
                    live_count: bool = False, options: tuple[str, ...] = ('--recursive', '--links'),
                    activity: MountPulse | None = None) -> list[dict]:
    executable = shutil.which('rclone')
    if executable is None:
        raise ConfigError('rclone is not available in PATH.')
    own_pulse = activity is None
    pulse = activity or MountPulse(f'Inventory {label}', show_count='entries' if live_count else False)
    starting_count = pulse.completed
    if activity is not None:
        pulse.label = f'Inventory {label}'
        pulse.total = None
        pulse.show_count = 'entries'
    counter = JsonArrayObjectCounter() if live_count or activity is not None else None
    offset = 0
    with tempfile.TemporaryFile(mode='w+t', encoding='utf-8') as output, \
            tempfile.TemporaryFile(mode='w+t', encoding='utf-8') as errors:
        try:
            process = subprocess.Popen(
                [executable, '--ask-password=false', 'lsjson', root, *options],
                stdout=output, stderr=errors, env=transport_env(),
                start_new_session=True)
        except OSError as exc:
            if own_pulse:
                pulse.done('error')
            raise TransportError(f'Could not start rclone: {exc}') from exc
        try:
            while process.poll() is None:
                size = os.fstat(output.fileno()).st_size
                if counter is not None and size > offset:
                    counter.feed(os.pread(output.fileno(), size - offset, offset))
                    offset = size
                    pulse.completed = starting_count + counter.count
                if own_pulse:
                    pulse.draw(size)
                    mount_input_wait(input_fd, 0.2)
                else:
                    try:
                        process.wait(timeout=0.2)
                    except subprocess.TimeoutExpired:
                        pass
            code = process.wait()
        except KeyboardInterrupt as exc:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            if own_pulse:
                pulse.done('interrupted')
            if live_count or not own_pulse:
                raise
            raise ConfigError('Mount interrupted by user.') from exc
        output.seek(0)
        errors.seek(0)
        raw, detail = output.read(), errors.read()
        try:
            empty = raw.strip() in ('', '[', '[]') or json.loads(raw) == []
        except ValueError:
            empty = False
        if code == 3 and empty and 'directory not found' in detail.lower():
            raw = '[]'
        elif code != 0:
            if own_pulse:
                pulse.done('error')
            raise TransportError(f'Inventory {label}: rclone exited with code {code}: '
                                 + detail.strip()[-1200:])
    try:
        items = json.loads(raw)
    except ValueError as exc:
        if own_pulse:
            pulse.done('error')
        raise TransportError(f'Inventory {label}: invalid JSON response.') from exc
    if not isinstance(items, list):
        if own_pulse:
            pulse.done('error')
        raise TransportError(f'Inventory {label}: unexpected format.')
    pulse.completed = starting_count + len(items)
    if own_pulse:
        pulse.done(f'{len(items)} entries read')
    return items



def mount_current_conflicts(roots: dict[str, str], target: str = '',
                            input_fd: int | None = None) -> None:
    """Refuse a merged view if an object could mask one from the other area."""
    inventories = []
    for area in ('plain', 'private'):
        objects = {}
        for item in mount_inventory(remote_join(roots[area], 'current'),
                                    f'{target}/{area}' if target else area, input_fd,
                                    live_count=True):
            path = item.get('Path')
            if (not isinstance(path, str) or not path or path.startswith('/')
                    or any(part in ('', '.', '..') for part in path.split('/'))):
                raise TransportError('The cloud returned an invalid path; mount blocked.')
            try:
                absolute_path('/' + path)
            except ConfigError as exc:
                raise TransportError('The cloud returned an invalid path; mount blocked.') from exc
            if path in objects:
                raise TransportError('Cloud contains duplicate paths; mount blocked.')
            if type(item.get('IsDir')) is not bool:
                raise TransportError('Cloud returned an invalid path type; mount blocked.')
            objects[path] = item['IsDir']
        inventories.append(objects)
    plain, private = inventories
    conflicts = current_conflict_paths(plain, private)
    if conflicts:
        examples = ', '.join('/' + path for path in conflicts[:5])
        remaining = f' (and {len(conflicts) - 5} more)' if len(conflicts) > 5 else ''
        raise ConfigError(
            f'current contains {len(conflicts)} paths conflicting between plain and private: '
            f'{examples}{remaining}. The combined volume was not mounted; '
            'browse the areas with --area plain and --area private.')



def remote_current_objects(root: str, inventory_label: str | None = None) -> dict[str, bool]:
    """Read current once, keeping directories for cross-area collision checks."""
    items = (mount_inventory(root, inventory_label, live_count=True)
             if inventory_label is not None else
             json.loads(rclone_call("lsjson", root, "--recursive", "--links", missing_ok=True)))
    objects = {}
    for item in items:
        path = item.get("Path")
        if not isinstance(path, str) or not path or path.startswith("/") or any(part in ("", ".", "..") for part in path.split("/")):
            raise TransportError("The cloud returned an invalid path; operation blocked.")
        absolute_path("/" + path)
        if path in objects:
            raise TransportError("The cloud contains duplicate paths; resolve them before backup.")
        if type(item.get('IsDir')) is not bool:
            raise TransportError("The cloud returned an invalid path type; operation blocked.")
        objects[path] = item['IsDir']
    return objects



def current_conflict_paths(plain: dict[str, bool], private: dict[str, bool]) -> list[str]:
    """Directories may overlap; any shared path involving a file may not."""
    return sorted(path for path in plain.keys() & private.keys()
                  if not plain[path] or not private[path])


def find_remote_copies(targets: dict, requested: str, subtree: bool, area_filter: str,
                       activity: MountPulse | None = None) -> list[dict]:
    """List exact current/history objects, independent of the active selector."""
    wanted = requested.lstrip('/')
    matches = []
    for target, areas in targets.items():
        for area, root in areas.items():
            if area_filter != 'all' and area != area_filter:
                continue
            for kind in ('files', 'macos-icons'):
                prefix = root if kind == 'files' else remote_join(root, 'macos-icons')
                for location in ('current', 'history'):
                    inventory_root = remote_join(prefix, location)
                    if activity is not None:
                        listing = mount_inventory(
                            inventory_root, f'{target}/{area}/{kind}/{location}',
                            options=('--recursive', '--files-only', '--links', '--hash', '--original'),
                            activity=activity)
                    else:
                        listing = json.loads(rclone_call(
                            'lsjson', inventory_root, '--recursive', '--files-only',
                            '--links', '--hash', '--original', missing_ok=True,
                        ))
                    seen = set()
                    for item in listing:
                        relative = item.get('Path')
                        if not isinstance(relative, str) or not relative or relative.startswith('/') or any(
                            part in ('', '.', '..') for part in relative.split('/')
                        ):
                            raise TransportError('Cloud inventory contains an invalid path.')
                        absolute_path('/' + relative)
                        if relative in seen:
                            raise TransportError('Duplicate cloud paths; forget blocked.')
                        seen.add(relative)
                        if location == 'history':
                            version, sep, logical = relative.partition('/')
                            if not sep or not version:
                                raise TransportError('History version has an invalid path.')
                        else:
                            version, logical = None, relative
                        if kind == 'macos-icons':
                            if not logical.endswith(ICON_SIDECAR):
                                raise TransportError('Unexpected item in the icon area.')
                            logical = logical[:-len(ICON_SIDECAR)]
                            selected = subtree and (logical == wanted or logical.startswith(wanted + '/'))
                        else:
                            if logical.endswith('.rclonelink'):
                                logical = logical[:-len('.rclonelink')]
                            selected = (logical == wanted or subtree and logical.startswith(wanted + '/'))
                        if not selected:
                            continue
                        size = item.get('Size')
                        if type(size) is not int or size < 0:
                            raise TransportError('Invalid cloud size; forget blocked.')
                        hashes = item.get('Hashes') or {}
                        if not isinstance(hashes, dict):
                            raise TransportError('Invalid cloud hash; forget blocked.')
                        matches.append({
                            'target': target, 'area': area, 'kind': kind,
                            'location': location, 'version': version,
                            'remote_path': remote_join(inventory_root, relative),
                            'relative': relative, 'size': size,
                            'modtime': item.get('ModTime'), 'id': item.get('ID'),
                            'original_id': item.get('OrigID'),
                            'hashes': hashes,
                        })
                        if activity is not None:
                            activity.matches += 1
    matches.sort(key=lambda item: (item['target'], item['area'], item['remote_path']))
    return matches
