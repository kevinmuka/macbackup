"""Internal macbackup restore operations."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid

from ..i18n import print
from ..inventory import find_remote_copies
from ..policy import ConfigError, absolute_path
from ..state import operation_lock
from ..transport import TransportError, load_targets, transport_env
from ..ui import operation_activity, progress_quantity, ui


def restore_operation(args, config: Path, home: Path) -> int:
    if args.path.endswith('/'):
        raise ConfigError('restore supports one file at a time; provide the exact file path.')
    requested = absolute_path(args.path)
    destination = absolute_path(args.to)
    if destination == requested:
        raise ConfigError('Do not restore directly over the original path.')
    if destination.is_symlink():
        raise ConfigError('The destination cannot be a symlink.')
    if not destination.parent.is_dir():
        raise ConfigError('The destination directory must already exist.')
    for ancestor in (destination.parent, *destination.parent.parents):
        if ancestor.is_symlink():
            raise ConfigError('The destination path crosses a symlink.')
    if destination.exists() and not args.replace:
        raise ConfigError('Destination already exists; choose another name or use --replace.')
    targets, _ = load_targets(config, args.target, require_enabled=False)
    with operation_lock(home / '.local/state/macbackup'):
        with operation_activity('Searching for version to restore', report_non_tty=True,
                                show_count='entries') as activity:
            activity.matches = 0
            items = [item for item in find_remote_copies(targets, str(requested), False,
                                                       args.area, activity)
                     if item['kind'] == 'files' and item['target'] == args.target
                     and item['area'] == args.area and (item['version'] or 'current') == args.version]
        if len(items) != 1:
            raise ConfigError('Version missing or ambiguous; consult macbackup versions.')
        item = items[0]
        is_link = item['relative'].endswith('.rclonelink')
        if is_link and not args.links:
            raise ConfigError('This version is a symlink; add --links to recreate it.')
        if args.links and not is_link:
            raise ConfigError('--links is only valid for a symlink.')
        if is_link and item['size'] > 64 * 1024:
            raise ConfigError('Symlink exceeds 64 KiB; restore blocked.')
        executable = shutil.which('rclone')
        if executable is None:
            raise ConfigError('rclone is not available in PATH.')
        fd, temporary = tempfile.mkstemp(prefix='.macbackup-restore-', dir=destination.parent)
        try:
            with os.fdopen(fd, 'wb') as output:
                os.fchmod(output.fileno(), 0o600)
                with operation_activity('Restoring file') as activity:
                    if activity is not None:
                        activity.byte_fd = output.fileno()
                        activity.byte_total = item['size']
                    transfer = subprocess.run(
                        [executable, '--ask-password=false', 'cat', item['remote_path'],
                         '--retries', '1'], stdout=output, stderr=subprocess.PIPE,
                        text=True, env=transport_env(),
                    )
                output.flush()
                os.fsync(output.fileno())
            if transfer.returncode:
                raise TransportError('Download failed: ' + transfer.stderr.strip()[-1500:])
            if os.path.getsize(temporary) != item['size']:
                raise TransportError('Downloaded size differs from inventory; no file restored.')
            supported = {'md5': 'md5', 'sha1': 'sha1', 'sha-1': 'sha1',
                         'sha256': 'sha256', 'sha-256': 'sha256'}
            expected = {name: value for name, value in item['hashes'].items()
                        if name.lower() in supported and isinstance(value, str) and value}
            if expected:
                digesters = {name: hashlib.new(supported[name.lower()]) for name in expected}
                with open(temporary, 'rb') as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                        for hasher in digesters.values():
                            hasher.update(chunk)
                if any(digesters[name].hexdigest().lower() != value.lower()
                       for name, value in expected.items()):
                    raise TransportError('Downloaded hash differs from inventory; no file restored.')
            with operation_activity('Checking restored copy', report_non_tty=True,
                                    show_count='entries') as activity:
                activity.matches = 0
                refreshed = [new for new in find_remote_copies(targets, str(requested), False,
                                                             args.area, activity)
                             if new['remote_path'] == item['remote_path']]
            if refreshed != [item]:
                raise TransportError('Cloud version changed during download; no file restored.')
            if destination.is_symlink():
                raise ConfigError('The destination became a symlink.')
            if is_link:
                try:
                    link_target = Path(temporary).read_bytes().decode('utf-8')
                except UnicodeError as exc:
                    raise ConfigError('Symlink target cannot be represented in UTF-8.') from exc
                if not link_target or '\x00' in link_target:
                    raise ConfigError('Empty or invalid symlink target.')
                if args.replace:
                    if destination.exists() and not destination.is_file():
                        raise ConfigError('--replace can only replace a regular file.')
                    temporary_link = destination.parent / ('.macbackup-link-' + uuid.uuid4().hex)
                    try:
                        os.symlink(link_target, temporary_link)
                        os.replace(temporary_link, destination)
                    finally:
                        temporary_link.unlink(missing_ok=True)
                else:
                    try:
                        os.symlink(link_target, destination)
                    except FileExistsError as exc:
                        raise ConfigError('Destination created during download; nothing overwritten.') from exc
            elif args.replace:
                if destination.exists() and not destination.is_file():
                    raise ConfigError('--replace can only replace a regular file.')
                os.replace(temporary, destination)
            else:
                try:
                    os.link(temporary, destination, follow_symlinks=False)
                except FileExistsError as exc:
                    raise ConfigError('Destination created during download; nothing overwritten.') from exc
                os.unlink(temporary)
            data = {'operation': 'restore', 'ok': True, 'requested': str(requested),
                    'destination': str(destination), 'target': args.target,
                    'area': args.area, 'version': args.version, 'bytes': item['size'],
                    'hash_verified': bool(expected), 'kind': 'symlink' if is_link else 'file',
                    'link_target': link_target if is_link else None,
                    'note': ('Remote hash verified.' if expected else
                             'Content hash unavailable from backend; size and inventory checked.')}
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        label = 'Symlink recreated' if data['kind'] == 'symlink' else 'File restored'
        print(f"{ui(label, 'success')} in {destination} "
              f"({progress_quantity(data['bytes'], False)}). {data['note']}")
    return 0
