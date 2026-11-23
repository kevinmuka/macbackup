"""Internal macbackup forget operations."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import time
import uuid

from ..i18n import print
from ..inventory import find_remote_copies
from ..policy import ConfigError, Policy, absolute_path
from ..state import operation_lock
from ..transport import TransportError, load_targets, rclone_call, remote_join
from ..ui import operation_activity, progress_quantity, ui


def write_private_json(path: Path, data: dict) -> None:
    with path.open('x') as stream:
        os.chmod(path, 0o600)
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())



def check_permanent_google(targets: dict) -> None:
    remotes = {item['name']: item['type'] for item in json.loads(rclone_call('listremotes', '--json'))}
    plain_remote = targets['google']['plain'].split(':', 1)[0]
    if remotes.get(plain_remote) != 'drive':
        raise ConfigError('Permanent removal requires a real Google Drive remote.')



def forget_directory_present(root: str) -> bool:
    parent, name = root.rsplit('/', 1)
    listing = json.loads(rclone_call('lsjson', parent, '--dirs-only',
                                     '--original', missing_ok=True))
    matches = [item for item in listing if item.get('Name') == name]
    if len(matches) > 1:
        raise TransportError('Duplicate cloud directories; forget blocked.')
    return bool(matches)



def forget_cleanup_roots(targets: dict, requested: str, area_filter: str) -> list[str]:
    """Find the exact requested directory in current and each history version."""
    roots = set()
    for areas in targets.values():
        for area, area_root in areas.items():
            if area_filter != 'all' and area != area_filter:
                continue
            for kind in ('files', 'macos-icons'):
                prefix = area_root if kind == 'files' else remote_join(area_root, 'macos-icons')
                current = remote_join(remote_join(prefix, 'current'), requested.lstrip('/'))
                if forget_directory_present(current):
                    roots.add(current)
                history = remote_join(prefix, 'history')
                versions = json.loads(rclone_call('lsjson', history, '--dirs-only',
                                                  '--original', missing_ok=True))
                seen = set()
                for version in versions:
                    name = version.get('Name')
                    if not isinstance(name, str) or not name or '/' in name or name in ('.', '..'):
                        raise TransportError('History version has an invalid path.')
                    if name in seen:
                        raise TransportError('Duplicate history versions in the cloud.')
                    seen.add(name)
                    root = remote_join(remote_join(history, name), requested.lstrip('/'))
                    if forget_directory_present(root):
                        roots.add(root)
    return sorted(roots)



def forget_remove_empty_roots(roots: list[str], flag: str) -> list[str]:
    """Remove only empty directories, then confirm that each requested root vanished."""
    completed = []
    for root in roots:
        if forget_directory_present(root):
            rclone_call('rmdirs', root, flag, '--retries', '1')
        if forget_directory_present(root):
            raise TransportError('Directory still present after cleanup: ' + root)
        completed.append(root)
    return completed



def forget_operation(args, config: Path, home: Path) -> int:
    state = home / '.local/state/macbackup'
    with operation_lock(state):
        plans = state / 'forget-plans'
        plans.mkdir(mode=0o700, exist_ok=True)
        if plans.is_symlink():
            raise ConfigError('The plan directory cannot be a symlink.')
        os.chmod(plans, 0o700)
        if args.plan:
            if args.path or args.area or args.target or args.mode or not args.apply:
                raise ConfigError('To apply a plan, use only --plan ID --apply.')
            if not re.fullmatch(r'F[0-9a-f]{12}', args.plan):
                raise ConfigError('Invalid plan ID.')
            file = plans / (args.plan + '.json')
            if file.is_symlink() or not file.is_file():
                raise ConfigError('Plan missing or already used.')
            data = json.loads(file.read_text())
            if data.get('schema_version') != 1 or data.get('plan_id') != args.plan:
                raise ConfigError('Invalid plan format.')
            if data.get('config_dir') != str(config):
                raise ConfigError('Use the same configuration directory as the plan.')
            if time.time() > data.get('expires_at', 0):
                raise ConfigError('Plan expired; create a new one.')
            if data.get('mode') == 'permanent' and data.get('target') != 'google':
                raise ConfigError('Automatic permanent removal is available only for Google.')
            targets, _ = load_targets(config, data['target'], require_enabled=False)
            if data['mode'] == 'permanent':
                check_permanent_google(targets)
            if targets != data.get('roots'):
                raise ConfigError('Targets changed; create a new plan.')
            with operation_activity('Checking forget plan', report_non_tty=True,
                                    show_count='entries') as activity:
                activity.matches = 0
                current = find_remote_copies(targets, data['requested'], data['subtree'],
                                           data['area'], activity)
                cleanup_roots = (forget_cleanup_roots(targets, data['requested'], data['area'])
                                 if data['subtree'] else [])
            if current != data.get('items'):
                raise ConfigError('Cloud copies changed; create a new plan.')
            if cleanup_roots != data.get('cleanup_roots', cleanup_roots):
                raise ConfigError('Plan directories changed; create a new plan.')
            applying = plans / (args.plan + '.applying')
            os.replace(file, applying)
            completed = []
            error = None
            flag = '--drive-use-trash=false' if data['mode'] == 'permanent' else '--drive-use-trash=true'
            with operation_activity('Removing copies', len(current)) as activity:
                if activity is not None:
                    activity.count_verb = 'removed'
                for item in current:
                    try:
                        rclone_call('deletefile', item['remote_path'], flag, '--retries', '1')
                        completed.append(item['remote_path'])
                        if activity is not None:
                            activity.completed = len(completed)
                    except (TransportError, KeyboardInterrupt) as exc:
                        error = ("Interrupted by user; some copies may already have been removed."
                                 if isinstance(exc, KeyboardInterrupt) else str(exc))
                        if activity is not None:
                            activity.outcome = ('interrupted' if isinstance(exc, KeyboardInterrupt)
                                                else 'error')
                        break
            try:
                with operation_activity('Checking remaining copies', report_non_tty=True,
                                        show_count='entries') as activity:
                    activity.matches = 0
                    remaining = find_remote_copies(targets, data['requested'], data['subtree'],
                                                 data['area'], activity)
            except (ConfigError, TransportError) as exc:
                remaining = None
                error = str(exc) if error is None else error + '; final check: ' + str(exc)
            cleaned_roots = []
            if error is None and remaining == [] and cleanup_roots:
                try:
                    cleaned_roots = forget_remove_empty_roots(cleanup_roots, flag)
                except (ConfigError, TransportError) as exc:
                    error = 'Cleaning empty directories: ' + str(exc)
            result = {
                'operation': 'forget-apply', 'plan_id': args.plan,
                'ok': error is None and remaining == [], 'mode': data['mode'],
                'attempted': len(completed) + (1 if error and len(completed) < len(current) else 0),
                'removed_from_active_backup': completed,
                'empty_directory_roots_removed': cleaned_roots,
                'remaining': remaining, 'error': error,
                'note': ('Provider trash; this is not permanent deletion.'
                         if data['mode'] == 'trash' else
                         'Permanent deletion requested; native revisions and existing trash not inspected.'),
            }
            write_private_json(plans / (args.plan + '.result.json'), result)
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print(f"{ui('Plan', 'success' if result['ok'] else 'error')} {args.plan}: "
                      f"{len(completed)}/{len(current)} files removed from backup folders.")
                if cleanup_roots:
                    print(f"Empty directories cleaned: {len(cleaned_roots)}/{len(cleanup_roots)} selected roots.")
                if error:
                    print(ui('ERROR:', 'error') + ' ' + error)
                print(ui(result['note'], 'subtle'))
                if remaining:
                    print(f'Items still present: {len(remaining)}. Create a new plan to retry.')
            return 0 if result['ok'] else 1
        if args.apply or not (args.path and args.area and args.target and args.mode):
            raise ConfigError('Creating a plan requires a path, --area, --target and --mode.')
        requested = absolute_path(args.path)
        if requested == Path('/'):
            raise ConfigError('Root / is not allowed in forget.')
        subtree = args.path.endswith('/')
        if requested.is_dir() and not subtree:
            raise ConfigError('For a directory, add / to the end of the path.')
        if args.mode == 'permanent' and args.target != 'google':
            raise ConfigError('Automatic permanent removal is available only for Google.')
        targets, _ = load_targets(config, args.target, require_enabled=False)
        if args.mode == 'permanent':
            check_permanent_google(targets)
        with operation_activity('Searching copies for forget', report_non_tty=True,
                                show_count='entries') as activity:
            activity.matches = 0
            items = find_remote_copies(targets, str(requested), subtree, args.area, activity)
            cleanup_roots = forget_cleanup_roots(targets, str(requested), args.area) if subtree else []
        if len(items) > 10000:
            raise ConfigError('More than 10000 files; narrow the path and create multiple plans.')
        result = {
            'operation': 'forget-plan', 'plan_id': None, 'requested': str(requested),
            'subtree': subtree, 'area': args.area, 'target': args.target,
            'mode': args.mode, 'items': items, 'count': len(items),
            'cleanup_roots': cleanup_roots,
            'known_bytes': sum(item['size'] for item in items),
            'selected_locally': None,
            'note': ('Files will move to provider trash; empty directories '
                     'included in the plan will be removed.' if args.mode == 'trash' else
                     'Files and empty directories in the plan will be deleted '
                     'permanently through the provider; native revisions and prior trash '
                     'are not included.'),
        }
        if items or cleanup_roots:
            # The selector is advisory here. Forget must still find old copies
            # after exclusions or routing changes.
            try:
                policy = Policy(config, home)
                result['selected_locally'] = policy.decide(requested, directory=subtree).action in ('plain', 'private')
            except ConfigError:
                pass
            plan_id = 'F' + uuid.uuid4().hex[:12]
            data = {
                'schema_version': 1, 'plan_id': plan_id, 'config_dir': str(config),
                'requested': str(requested), 'subtree': subtree,
                'target': args.target, 'area': args.area, 'mode': args.mode,
                'roots': targets, 'items': items,
                'cleanup_roots': cleanup_roots,
                'expires_at': time.time() + 3600,
            }
            write_private_json(plans / (plan_id + '.json'), data)
            result['plan_id'] = plan_id
            result['expires_at'] = data['expires_at']
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"{ui('Matches:', 'info')} {len(items)} file, "
                  f"{progress_quantity(result['known_bytes'], False)} noti.")
            for item in items:
                print(f"{ui(item['target'] + '/' + item['area'] + '/' + item['location'], 'remote')}: "
                      f"{ui(item['remote_path'], 'remote')}")
            if cleanup_roots:
                print(f"Directories to clean if empty: {len(cleanup_roots)} roots.")
                for root in cleanup_roots:
                    print(f"Directory: {ui(root, 'remote')}")
            if result['selected_locally']:
                print(ui('WARNING:', 'warning') + ' the path is still selected and may be uploaded again by the next run.')
            print(ui(result['note'], 'subtle'))
            if result['plan_id']:
                print(f"{ui('To apply within one hour:', 'info')} macbackup forget --plan {result['plan_id']} --apply")
        return 0
