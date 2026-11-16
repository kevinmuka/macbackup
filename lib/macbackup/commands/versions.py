"""Internal macbackup versions operations."""

from __future__ import annotations

import json
from pathlib import Path

from ..i18n import print
from ..icons import ICON_SIDECAR
from ..inventory import mount_inventory
from ..policy import ConfigError, absolute_path
from ..state import operation_lock
from ..transport import TransportError, load_targets, rclone_call, remote_join
from ..ui import MountPulse, operation_activity, progress_quantity, ui


def versions_inventory(targets: dict, requested: str, subtree: bool, area_filter: str,
                       activity: MountPulse | None = None) -> list[dict]:
    """Read only the requested current subtree and matching subtrees in each history run."""
    wanted = requested.lstrip('/')
    found = []
    for target, areas in targets.items():
        for area, root in areas.items():
            if area_filter != 'all' and area != area_filter:
                continue
            for kind in (('files', 'macos-icons') if subtree else ('files',)):
                prefix = root if kind == 'files' else remote_join(root, 'macos-icons')
                history_root = remote_join(prefix, 'history')
                runs = (mount_inventory(history_root, f'{target}/{area}/{kind}/history',
                                        options=('--dirs-only',), activity=activity)
                        if activity is not None else
                        json.loads(rclone_call('lsjson', history_root, '--dirs-only',
                                               missing_ok=True)))
                if not isinstance(runs, list):
                    raise TransportError('Invalid history run list.')
                versions = [None]
                for run in runs:
                    name = run.get('Name') if isinstance(run, dict) else None
                    if not isinstance(name, str) or not name or '/' in name or name in ('.', '..'):
                        raise TransportError('Invalid history run name.')
                    versions.append(name)
                for version in versions:
                    location = 'history' if version else 'current'
                    inventory_root = remote_join(prefix, location)
                    base = remote_join(inventory_root, version) if version else inventory_root
                    scope = wanted if subtree else wanted.rpartition('/')[0]
                    search_root = remote_join(base, scope) if scope else base
                    options = ['--files-only', '--links']
                    if subtree:
                        options.append('--recursive')
                    listing = (mount_inventory(
                        search_root, f'{target}/{area}/{kind}/{version or "current"}',
                        options=tuple(options), activity=activity)
                        if activity is not None else
                        json.loads(rclone_call('lsjson', search_root, *options,
                                               missing_ok=True)))
                    if not isinstance(listing, list):
                        raise TransportError('Invalid version inventory.')
                    seen = set()
                    for entry in listing:
                        path = entry.get('Path') if isinstance(entry, dict) else None
                        if not isinstance(path, str) or not path or path.startswith('/') or any(
                            part in ('', '.', '..') for part in path.split('/')
                        ):
                            raise TransportError('Cloud inventory contains an invalid path.')
                        logical = scope + '/' + path if scope else path
                        absolute_path('/' + logical)
                        if logical in seen:
                            raise TransportError('Duplicate cloud paths; versions blocked.')
                        seen.add(logical)
                        if kind == 'macos-icons':
                            if not logical.endswith(ICON_SIDECAR):
                                raise TransportError('Unexpected item in the icon area.')
                            original = logical[:-len(ICON_SIDECAR)]
                        else:
                            original = logical[:-len('.rclonelink')] if logical.endswith('.rclonelink') else logical
                        if not (original == wanted or subtree and original.startswith(wanted + '/')):
                            continue
                        size = entry.get('Size')
                        if type(size) is not int or size < 0:
                            raise TransportError('Invalid cloud size; versions blocked.')
                        relative = version + '/' + logical if version else logical
                        found.append({
                            'target': target, 'area': area, 'kind': kind,
                            'location': location, 'version': version,
                            'remote_path': remote_join(inventory_root, relative),
                            'relative': relative, 'size': size,
                            'modtime': entry.get('ModTime'),
                        })
                        if activity is not None:
                            activity.matches += 1
    found.sort(key=lambda item: (item['target'], item['area'], item['remote_path']))
    return found



def versions_operation(args, config: Path, home: Path) -> int:
    requested = absolute_path(args.path)
    directory = args.path.endswith('/') or (requested.is_dir() and not requested.is_symlink())
    if directory and requested.is_file():
        raise ConfigError('The path given as a directory is a file.')
    if not directory and args.files:
        raise ConfigError('--files is only valid for a directory.')
    targets, _ = load_targets(config, args.target, require_enabled=False)
    with operation_lock(home / '.local/state/macbackup'):
        with operation_activity('Searching versions', report_non_tty=True,
                                show_count='entries') as activity:
            activity.matches = 0
            items = versions_inventory(targets, str(requested), directory, args.area, activity)
    if directory:
        groups = {}
        for item in items:
            version = item['version'] or 'current'
            key = (item['target'], item['area'], version)
            group = groups.setdefault(key, {'target': item['target'], 'area': item['area'],
                                            'version': version, 'location': item['location'],
                                            'count': 0, 'bytes': 0, 'items': []})
            logical = item['relative'].split('/', 1)[1] if item['location'] == 'history' else item['relative']
            if item['kind'] == 'macos-icons':
                logical = logical[:-len(ICON_SIDECAR)]
            elif logical.endswith('.rclonelink'):
                logical = logical[:-len('.rclonelink')]
            group['count'] += 1
            group['bytes'] += item['size']
            group['items'].append({'path': '/' + logical, 'kind': item['kind'],
                                   'size': item['size'], 'modtime': item['modtime'],
                                   'remote_path': item['remote_path']})
        versions = sorted(groups.values(), key=lambda group: (
            group['target'], group['area'], group['version'] != 'current', group['version']))
        for group in versions:
            group['items'].sort(key=lambda item: (item['path'], item['kind']))
        data = {
            'operation': 'versions', 'requested': str(requested), 'kind': 'directory',
            'target': args.target, 'area': args.area, 'versions': versions,
            'note': 'current is the latest copy; each history contains only items moved in that run, not a full directory snapshot.',
        }
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            print(f"{ui('Directory versions', 'info')} {requested}: {len(versions)} groups; "
                  f"{len(items)} total items.")
            for group in versions:
                print(f"{ui(group['target'] + '/' + group['area'] + ' ' + group['version'], 'remote')}: "
                      f"{group['count']} items, {progress_quantity(group['bytes'], False)}")
                if args.files and group['location'] == 'history':
                    for item in group['items']:
                        print(f"  {ui(item['path'], 'subtle')} "
                              f"[{item['kind']}; {progress_quantity(item['size'], False)}]")
            print(ui(data['note'], 'subtle'))
        return 0
    items = [item for item in items if item['kind'] == 'files']
    data = {
        'operation': 'versions', 'requested': str(requested),
        'target': args.target, 'area': args.area,
        'versions': [
            {'target': item['target'], 'area': item['area'],
             'version': item['version'] or 'current',
             'location': item['location'], 'size': item['size'],
             'modtime': item['modtime'], 'remote_path': item['remote_path']}
            for item in items
        ],
        'note': 'Modification dates are not archive dates; each history contains only files moved in that run.',
    }
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(f"{ui('Versions found:', 'info')} {len(items)} for {requested}.")
        for item in data['versions']:
            print(f"{ui(item['target'] + '/' + item['area'] + ' ' + item['version'], 'remote')} "
                  f"{progress_quantity(item['size'], False)} {item['modtime'] or 'data ignota'}")
        print(ui(data['note'], 'subtle'))
    return 0
