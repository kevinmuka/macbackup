"""Internal macbackup verify module."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile

from ..i18n import print
from ..icons import FINDER_ICON, ICON_SIDECAR, icon_snapshot
from ..inventory import mount_inventory
from ..policy import ConfigError, Policy
from ..scan import scan_policy, selected_names
from ..state import display_safe_path, operation_lock, save_local_report
from ..transport import TransportError, load_targets, rclone_call, remote_join, write_manifest
from ..ui import MountPulse, operation_activity, ui


def verify_report(source: str, destination: str, names: list[str],
                  state: Path, private: bool, download: bool,
                  size_only: bool = False, pulse: MountPulse | None = None) -> dict:
    if not names:
        return {'selected': 0, 'matched': 0, 'different': [], 'missing': [], 'errors': []}
    if size_only:
        options = ('--recursive', '--files-only', '--links', '--fast-list',
                   '--no-modtime', '--no-mimetype')
        if pulse is not None:
            original_label = pulse.label
            listing = mount_inventory(destination,
                                      original_label.removeprefix('Verifying ') + '/current',
                                      options=options, activity=pulse)
            pulse.label = 'Comparing sizes ' + original_label.removeprefix('Verifying ')
            pulse.total = len(names)
            pulse.completed = 0
            pulse.show_count = False
        else:
            listing = json.loads(rclone_call('lsjson', destination, *options, missing_ok=True))
        remote = {}
        for item in listing:
            name, size = item.get('Path'), item.get('Size')
            if not isinstance(name, str) or not name or name.startswith('/') or any(
                    part in ('', '.', '..') for part in name.split('/')):
                raise TransportError('Invalid remote inventory during verification.')
            if name in remote or type(size) is not int or size < 0:
                raise TransportError('Duplicate remote inventory entries or missing sizes.')
            remote[name] = size
        missing, different, errors = [], [], []
        for name in names:
            if name not in remote:
                missing.append(name)
                if pulse is not None:
                    pulse.completed += 1
                continue
            try:
                link_path = Path('/') / name[:-len('.rclonelink')]
                if source == '/' and name.endswith('.rclonelink') and link_path.is_symlink():
                    local_size = len(os.fsencode(os.readlink(link_path)))
                else:
                    local_size = (Path(source) / name).stat().st_size
            except OSError:
                errors.append(name)
                if pulse is not None:
                    pulse.completed += 1
                continue
            if local_size != remote[name]:
                different.append(name)
            if pulse is not None:
                pulse.completed += 1
        return {'selected': len(names), 'matched': len(names) - len(missing) - len(different) - len(errors),
                'different': different, 'missing': missing, 'errors': errors}
    with tempfile.TemporaryDirectory(prefix='verify-', dir=state) as directory:
        directory = Path(directory)
        manifest = write_manifest(directory, names)
        report = directory / 'combined.txt'
        report.touch(mode=0o600)
        if pulse is not None:
            pulse.report_path = report
        command = 'check' if download or size_only or not private else 'cryptcheck'
        options = [command, source, destination, '--files-from0', str(manifest),
                   '--links', '--one-way', '--combined', str(report), '--log-level', 'ERROR']
        if download:
            options.append('--download')
        if size_only:
            options.append('--size-only')
        rclone_call(*options, allowed_codes=(0, 1))
        found = {}
        expected = set(names)
        for line in report.read_text().splitlines():
            if len(line) < 3 or line[1] != ' ' or line[0] not in '=+*!':
                raise TransportError('Incomplete or unexpected verification report.')
            name = line[2:]
            if name not in expected or name in found:
                raise TransportError('Inconsistent verification report.')
            found[name] = line[0]
        if set(found) != expected:
            raise TransportError('Not all selected files were verified.')
        if pulse is not None:
            pulse.report_path = None
            pulse.completed = len(found)
        return {
            'selected': len(names),
            'matched': sum(value == '=' for value in found.values()),
            'different': sorted(name for name, value in found.items() if value == '*'),
            'missing': sorted(name for name, value in found.items() if value == '+'),
            'errors': sorted(name for name, value in found.items() if value == '!'),
        }



def verify_display_path(name: str, kind: str) -> str:
    if kind == 'macos-icons' and name.endswith(ICON_SIDECAR):
        original = str(Path('/' + name[:-len(ICON_SIDECAR)]) / FINDER_ICON)
    else:
        original = '/' + (name[:-len('.rclonelink')] if name.endswith('.rclonelink') else name)
    return display_safe_path(original)



def verify_operation(args, config: Path, home: Path) -> int:
    if args.size_only and args.download:
        raise ConfigError('--size-only and --download cannot be used together.')
    targets, _ = load_targets(config, args.target, require_enabled=False)
    policy = Policy(config, home)
    with operation_lock(policy.state_dir):
        results = []
        data = {'operation': 'verify', 'ok': False, 'complete': False, 'interrupted': False,
                'target': args.target, 'area': args.area, 'download': args.download,
                'size_only': args.size_only, 'scope': 'selected-local-versus-remote-current',
                'started_at': datetime.now(timezone.utc).isoformat(), 'results': results,
                'note': 'Compared with current local sources; a difference does not prove corruption. '
                        'History and previously excluded copies are not covered.'}
        active_phase = 'local-scan'
        data['active_phase'] = active_phase
        save_local_report(policy.state_dir, 'last-verify.json', data)
        try:
            with operation_activity('Local scan', show_count=True) as pulse:
                scan = scan_policy(policy, pulse)
            if scan.errors:
                raise ConfigError('The local selection has errors: run macbackup check.')
            for target, areas in targets.items():
                for area, root in areas.items():
                    if args.area != 'all' and args.area != area:
                        continue
                    local = sorted(selected_names(scan, area))
                    method = ('size-only' if args.size_only else 'download' if args.download else
                              'cryptcheck' if area == 'private' else 'check')
                    active_phase = f'{target}/{area}/files'
                    data['active_phase'] = active_phase
                    save_local_report(policy.state_dir, 'last-verify.json', data)
                    try:
                        with operation_activity(f'Verifying {target}/{area}/files', len(local)) as pulse:
                            checked = verify_report('/', remote_join(root, 'current'), local,
                                                    policy.state_dir, area == 'private', args.download,
                                                    args.size_only, pulse)
                        results.append({'target': target, 'area': area, 'kind': 'files',
                                        'method': method, **checked})
                    except (ConfigError, TransportError, OSError) as exc:
                        results.append({'target': target, 'area': area, 'kind': 'files',
                                        'selected': len(local), 'error': str(exc)})
                    save_local_report(policy.state_dir, 'last-verify.json', data)
                    icons = [entry for entry in scan.entries
                             if entry.kind == 'finder-icon' and entry.decision.action == area]
                    if icons:
                        active_phase = f'{target}/{area}/macos-icons'
                        data['active_phase'] = active_phase
                        save_local_report(policy.state_dir, 'last-verify.json', data)
                        try:
                            with tempfile.TemporaryDirectory(prefix='verify-icons-', dir=policy.state_dir) as directory:
                                source = Path(directory)
                                names = []
                                for entry in icons:
                                    relative = str(Path(entry.path).parent).lstrip('/') + ICON_SIDECAR
                                    path = source / relative
                                    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                                    with path.open('x') as stream:
                                        os.chmod(path, 0o600)
                                        json.dump(icon_snapshot(entry), stream, sort_keys=True,
                                                  ensure_ascii=False, indent=2)
                                    names.append(relative)
                                with operation_activity(f'Verifying {target}/{area}/macos-icons', len(names)) as pulse:
                                    checked = verify_report(str(source),
                                        remote_join(remote_join(root, 'macos-icons'), 'current'),
                                        sorted(names), policy.state_dir, area == 'private', args.download,
                                        args.size_only, pulse)
                                results.append({'target': target, 'area': area, 'kind': 'macos-icons',
                                                'method': method, **checked})
                        except (ConfigError, TransportError, OSError) as exc:
                            results.append({'target': target, 'area': area, 'kind': 'macos-icons',
                                            'selected': len(icons), 'error': str(exc)})
                        save_local_report(policy.state_dir, 'last-verify.json', data)
        except KeyboardInterrupt:
            data.update(interrupted=True, active_phase=active_phase,
                        note='Verification interrupted; only completed phases are reported.')
            save_local_report(policy.state_dir, 'last-verify.json', data)
            raise
        except (ConfigError, TransportError, OSError) as exc:
            data.update(error=str(exc), active_phase=active_phase)
            save_local_report(policy.state_dir, 'last-verify.json', data)
            raise
        ok = all(not item.get('error') and not item.get('different')
                 and not item.get('missing') and not item.get('errors') for item in results)
        data.update(ok=ok, complete=True, active_phase=None,
                    finished_at=datetime.now(timezone.utc).isoformat())
        save_local_report(policy.state_dir, 'last-verify.json', data)
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            if args.size_only:
                print(ui('Quick verification: paths and sizes only; contents not compared.', 'warning'))
            for item in results:
                print(f"{ui(item['target'] + '/' + item['area'] + '/' + item['kind'], 'info')}: "
                      f"{item.get('matched', 0)}/{item['selected']} matching; "
                      f"different {len(item.get('different', []))}; "
                      f"missing {len(item.get('missing', []))}; "
                      f"errors {len(item.get('errors', []))}.")
                for key, label in (('different', 'Different'), ('missing', 'Missing'),
                                   ('errors', 'Read error')):
                    paths = item.get(key, [])
                    for name in paths[:20]:
                        print(f"  {label}: {ui(verify_display_path(name, item['kind']), 'muted')}")
                    if len(paths) > 20:
                        print(ui(f"  Other {len(paths) - 20} paths in the saved report: macbackup status --verify", 'subtle'))
                if item.get('error'):
                    print(ui('ERROR:', 'error') + ' ' + item['error'])
            print(ui(data['note'], 'subtle'))
            print(ui('Full report: macbackup status --verify', 'subtle'))
        return 0 if ok else 1
