"""Internal macbackup backup module."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import uuid

from ..i18n import print
from ..icons import FINDER_ICON, ICON_SIDECAR, icon_snapshot
from ..inventory import current_conflict_paths, remote_current_objects
from ..policy import ConfigError, Policy, Scan, absolute_path
from ..scan import scan_policy, selected_names
from ..state import display_safe_path, operation_lock, save_local_report
from ..transport import (
    TransferInterrupted, TransportError, load_targets, rclone_call,
    rclone_progress_call, remote_join, transfer_metrics, write_manifest,
)
from ..ui import MountPulse, issue_line, operation_activity, print_selection_summary, progress_quantity, ui


def transfer_plan(policy: Policy, scan: Scan, targets: dict, limit: int,
                  enforce_limit: bool = True,
                  show_inventory: bool = False) -> list[dict]:
    if scan.errors:
        raise ConfigError("The local selection has errors. Run macbackup check.")
    plans = []
    any_existing = False
    for target, areas in targets.items():
        first_plan = len(plans)
        current_objects = {}
        for area, root in areas.items():
            local = selected_names(scan, area)
            managed = set()
            policy_archives = []
            objects = remote_current_objects(
                remote_join(root, "current"),
                f'{target}/{area}/current' if show_inventory else None)
            current_objects[area] = objects
            existing = sorted(path for path, is_dir in objects.items() if not is_dir)
            any_existing = any_existing or bool(existing)
            if policy.gitignore is not None:
                policy.gitignore.preload([
                    absolute_path('/' + (name[:-len('.rclonelink')]
                                         if name.endswith('.rclonelink') else name))
                    for name in existing
                ])
            for name in existing:
                logical = name[:-len(".rclonelink")] if name.endswith(".rclonelink") else name
                decision = policy.decide(absolute_path("/" + logical))
                if decision.action == "error":
                    raise ConfigError(decision.reason)
                if decision.action == area:
                    managed.add(name)
                else:
                    policy_archives.append({
                        'path': '/' + logical, 'relative': name,
                        'remote_path': remote_join(root, 'current/' + name),
                        'reason': decision.reason, 'rule': decision.rule,
                        'current_area': area, 'new_action': decision.action,
                    })
            deletions = managed - local
            file_plan = {"target": target, "area": area, "root": root,
                         "files": sorted(local | managed), "selected": len(local),
                         "local": sorted(local), "existing": existing,
                         "deletions": sorted(deletions),
                         "archive_deleted": len(deletions),
                         "policy_archives": policy_archives,
                         "archive_policy": len(policy_archives)}
            plans.append(file_plan)
            icons = [entry for entry in scan.entries if entry.kind == 'finder-icon' and entry.decision.action == area]
            icon_local = {str(Path(entry.path).parent).lstrip('/') + ICON_SIDECAR for entry in icons}
            icon_root = remote_join(root, 'macos-icons')
            icon_existing = sorted(path for path, is_dir in
                                   remote_current_objects(
                                       remote_join(icon_root, 'current'),
                                       f'{target}/{area}/macos-icons/current' if show_inventory else None
                                   ).items() if not is_dir)
            any_existing = any_existing or bool(icon_existing)
            icon_managed = set()
            icon_policy_archives = []
            for name in icon_existing:
                if not name.endswith(ICON_SIDECAR):
                    raise TransportError("Unexpected path in the area reserved for icons.")
                parent = absolute_path('/' + name[:-len(ICON_SIDECAR)])
                decision = policy.decide(parent / FINDER_ICON)
                if decision.action == 'error':
                    raise ConfigError(decision.reason)
                if decision.action == area:
                    icon_managed.add(name)
                else:
                    icon_policy_archives.append({
                        'path': str(parent / FINDER_ICON),
                        'relative': name,
                        'remote_path': remote_join(icon_root, 'current/' + name),
                        'reason': decision.reason, 'rule': decision.rule,
                        'current_area': area, 'new_action': decision.action,
                    })
            icon_deleted = icon_managed - icon_local
            total_archives = (len(deletions) + len(policy_archives) +
                              len(icon_deleted) + len(icon_policy_archives))
            exceeded = total_archives > limit
            file_plan['archive_limit_exceeded'] = exceeded
            if exceeded and enforce_limit:
                raise ConfigError(f"{target}/{area}: {total_archives} archives across files and icons "
                                  f"exceed max_delete={limit}; no transfers started.")
            if icons or icon_existing:
                plans.append({'target': target, 'area': area, 'root': icon_root, 'kind': 'macos-icons',
                              'icons': icons, 'files': sorted(icon_local | icon_managed),
                              'local': sorted(icon_local), 'existing': icon_existing,
                              'selected': len(icon_local), 'deletions': sorted(icon_deleted),
                              'archive_deleted': len(icon_deleted),
                              'policy_archives': icon_policy_archives,
                              'archive_policy': len(icon_policy_archives),
                              'archive_limit_exceeded': exceeded})
        recoverable = []
        unsafe = []
        for path in current_conflict_paths(current_objects['plain'], current_objects['private']):
            archivers = [(plan, item) for plan in plans[first_plan:]
                         if plan.get('kind', 'files') == 'files'
                         for item in plan['policy_archives']
                         if item['relative'] == path and current_objects[plan['area']].get(path) is False]
            if not archivers:
                unsafe.append(path)
                continue
            for _, item in archivers:
                item['pre_archive'] = True
            recoverable.append({'target': target, 'path': '/' + path,
                                'archive_from': sorted({plan['area'] for plan, _ in archivers})})
        if unsafe:
            examples = ', '.join('/' + path for path in unsafe[:5])
            remaining = f' (and {len(unsafe) - 5} more)' if len(unsafe) > 5 else ''
            raise ConfigError(f'{target}/current: {len(unsafe)} conflicts without a safe repair: '
                              f'{examples}{remaining}. No transfer started.')
        if recoverable:
            plans[first_plan]['cross_area_conflicts'] = recoverable
    if not policy.roots and not any_existing:
        raise ConfigError("No source selected and no current copy to archive: rclone operation blocked.")
    return plans



def private_changes(plan: dict, state: Path, pulse: MountPulse | None = None) -> list[str]:
    local = set(plan["local"])
    existing = set(plan["existing"])
    to_check = local & existing
    changed = local - existing
    if to_check:
        with tempfile.TemporaryDirectory(prefix="cryptcheck-", dir=state) as directory:
            directory = Path(directory)
            manifest = write_manifest(directory, sorted(to_check))
            report = directory / "report"
            report.touch(mode=0o600)
            if pulse is not None:
                pulse.report_path = report
            rclone_call(
                "cryptcheck", plan.get('source', '/'), remote_join(plan["root"], "current"),
                "--files-from0", str(manifest), "--links", "--one-way",
                "--combined", str(report), "--log-level", "ERROR",
                allowed_codes=(0, 1),
            )
            checked = set()
            for line in report.read_text().splitlines():
                if len(line) < 3 or line[1] != " " or line[0] not in "=*+":
                    raise TransportError("Encrypted check incomplete or failed; private area not updated.")
                name = line[2:]
                if name not in to_check or name in checked:
                    raise TransportError("Inconsistent encrypted check; operation blocked.")
                checked.add(name)
                if line[0] != "=":
                    changed.add(name)
            if checked != to_check:
                raise TransportError("Not all private files were checked; operation blocked.")
            if pulse is not None:
                pulse.report_path = None
                pulse.completed = len(checked)
    return sorted(changed | set(plan["deletions"]))



def perform_sync(plan: dict, state: Path, run_id: str, limit: int, dry_run: bool,
                 home: Path | None = None, progress_depth: int = 2) -> dict | None:
    if plan.get('kind') == 'macos-icons' and 'source' not in plan:
        with tempfile.TemporaryDirectory(prefix='icons-', dir=state) as directory:
            source = Path(directory)
            for entry in plan['icons']:
                relative = str(Path(entry.path).parent).lstrip('/') + ICON_SIDECAR
                file = source / relative
                file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with file.open('x') as stream:
                    os.chmod(file, 0o600)
                    json.dump(icon_snapshot(entry), stream, sort_keys=True, ensure_ascii=False, indent=2)
            return perform_sync(plan | {'source': str(source)}, state, run_id, limit,
                                dry_run, home, progress_depth)
    if plan["area"] == "private":
        comparison = f'Comparing private {plan["target"]}/{plan["area"]}/{plan.get("kind", "files")}'
        total = len(set(plan['local']) & set(plan['existing']))
        if total:
            with operation_activity(comparison, total, report_non_tty=True) as pulse:
                names = private_changes(plan, state, pulse)
        else:
            names = private_changes(plan, state)
    else:
        names = plan["files"]
    if not names:
        return {'transfers': 0, 'bytes': 0, 'checks': 0, 'listed': 0,
                'renames': 0, 'serverSideMoves': 0, 'serverSideMoveBytes': 0} if not dry_run else None
    with tempfile.TemporaryDirectory(prefix="manifest-", dir=state) as directory:
        manifest = write_manifest(Path(directory), names)
        arguments = [
            "sync", plan.get('source', '/'), remote_join(plan["root"], "current"),
            "--files-from0", str(manifest), "--links", "--copy-links=false",
            "--backup-dir", remote_join(plan["root"], "history/" + run_id),
            "--delete-excluded=false", "--max-delete", str(max(limit, len(plan['deletions'])) if dry_run else limit),
            "--log-level", "ERROR" if dry_run else "NOTICE", "--retries", "1",
        ]
        arguments.append("--ignore-times" if plan["area"] == "private" else "--checksum")
        if dry_run:
            arguments.append("--dry-run")
            label = f'Preview {plan["target"]}/{plan["area"]}/{plan.get("kind", "files")}'
            report = Path(directory) / 'combined.txt'
            report.touch(mode=0o600)
            arguments.extend(('--combined', str(report)))
            with operation_activity(label, len(names), report_non_tty=True) as pulse:
                if pulse is not None:
                    pulse.count_verb = 'processed'
                    pulse.report_path = report
                    pulse.report_expected = set(names)
                rclone_call(*arguments)
            return None
        arguments.extend(("--stats", "5s", "--stats-file-name-length", "0",
                          "--stats-log-level", "NOTICE", "--use-json-log"))
        return rclone_progress_call(arguments, home or Path.home(), progress_depth,
                                   f"{plan['target']}/{plan['area']}/{plan.get('kind', 'files')}")



def archive_policy_copies(plan: dict, run_id: str, completed: list[dict],
                          activity: MountPulse | None = None,
                          on_complete=None) -> None:
    """Move policy-revoked current copies into the same area's run history."""
    for item in plan['policy_archives']:
        destination = remote_join(plan['root'], 'history/' + run_id + '/' + item['relative'])
        rclone_call('moveto', item['remote_path'], destination, '--retries', '1')
        completed.append({key: value for key, value in item.items() if key != 'relative'} |
                         {'history_path': destination})
        if activity is not None:
            activity.completed += 1
        if on_complete is not None:
            on_complete()



def archive_deleted_paths(plan: dict) -> list[str]:
    """Return original Mac paths, not rclone link or Finder icon sidecar names."""
    if plan.get('kind') == 'macos-icons':
        return [str(Path('/' + name[:-len(ICON_SIDECAR)]) / FINDER_ICON)
                for name in plan['deletions']]
    return ['/' + (name[:-len('.rclonelink')] if name.endswith('.rclonelink') else name)
            for name in plan['deletions']]



def completed_deleted_archive_paths(plan: dict, run_id: str) -> list[str] | None:
    """Confirm interrupted sync moves from this run's history, or return unknown."""
    if not plan['deletions']:
        return []
    try:
        label = f"Checking history {plan['target']}/{plan['area']}/{plan.get('kind', 'files')}"
        with operation_activity(label, report_non_tty=True):
            history = remote_current_objects(remote_join(plan['root'], 'history/' + run_id))
    except (ConfigError, TransportError, OSError, ValueError, KeyboardInterrupt):
        return None
    completed = [name for name in plan['deletions'] if history.get(name) is False]
    return archive_deleted_paths(plan | {'deletions': completed})



def transport_operation(args, policy: Policy) -> int:
    if getattr(args, 'progress_depth', 2) not in range(1, 6):
        raise ConfigError('--progress-depth must be an integer from 1 to 5.')
    targets, limit = load_targets(policy.config_dir, args.target)
    with operation_lock(policy.state_dir):
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:12]
        results = []
        if args.command == 'run':
            data = {'operation': 'run', 'run_id': run_id, 'ok': False,
                    'complete': False, 'interrupted': False, 'active_phase': 'local-scan',
                    'started_at': datetime.now(timezone.utc).isoformat(),
                    'results': results, 'note': 'Results separated by target and area.'}
            save_local_report(policy.state_dir, 'last-run.json', data)
        try:
            with operation_activity('Local scan', show_count=True) as pulse:
                scan = scan_policy(policy, pulse)
            if args.command == 'run':
                data['active_phase'] = 'planning'
                save_local_report(policy.state_dir, 'last-run.json', data)
            if args.command == 'check':
                plans = transfer_plan(policy, scan, targets, limit,
                                      enforce_limit=False,
                                      show_inventory=True)
            else:
                plans = transfer_plan(policy, scan, targets, limit,
                                      enforce_limit=args.command == 'run',
                                      show_inventory=True)
        except (KeyboardInterrupt, ConfigError, TransportError, OSError) as exc:
            if args.command == 'run':
                data.update(interrupted=isinstance(exc, KeyboardInterrupt),
                            error=("Interrupted by user." if isinstance(exc, KeyboardInterrupt)
                                   else str(exc)))
                save_local_report(policy.state_dir, 'last-run.json', data)
            raise
        conflicts = [conflict for plan in plans for conflict in plan.get('cross_area_conflicts', [])]
        if args.command == "check":
            warnings = scan.warnings + [
                f"{item['target']}/current: {item['path']}: repairable conflict; "
                f"the copy {', '.join(item['archive_from'])} must be archived before transfers. "
                "Use preview to check max_delete."
                for item in conflicts]
            result = {"scope": "remote-read-only", "ok": True,
                      "note": "Remote access and types checked; inventories read without changing the cloud.",
                      "summary": scan.summary(), "warnings": warnings, "conflicts": conflicts}
            if getattr(args, "json", False):
                print(json.dumps(result, ensure_ascii=False))
            else:
                print(ui('Remote check complete.', 'success') + ' ' + result['note'])
                for warning in warnings:
                    print(issue_line('WARNING:', warning, 'warning'))
                print_selection_summary(result['summary'])
            return 0
        for plan in plans:
            result = {key: plan[key] for key in ("target", "area", "selected", "archive_deleted", "archive_policy")}
            result['archive_limit_exceeded'] = plan['archive_limit_exceeded']
            result['kind'] = plan.get('kind', 'files')
            result['policy_archives'] = [
                {key: value for key, value in item.items() if key != 'relative'}
                for item in plan['policy_archives']
            ]
            result['archive_deleted_paths'] = archive_deleted_paths(plan)
            if args.command == 'run':
                result['archive_deleted_completed_paths'] = ([] if not plan['deletions'] else None)
                result['policy_archived'] = []
                result['ok'] = False
            results.append(result)
        if args.command == 'run':
            data['conflicts'] = conflicts
            save_local_report(policy.state_dir, 'last-run.json', data)
        prearchive_failed = False
        if args.command == 'run':
            # A reclassification must leave the old area's current before the new
            # area's sync can publish the same logical path.
            for plan, result in zip(plans, results):
                transitions = [item for item in plan['policy_archives']
                               if item['new_action'] in ('plain', 'private') or item.get('pre_archive')]
                if not transitions:
                    continue
                data['active_phase'] = f"{plan['target']}/{plan['area']}/{result['kind']}:archive-transition"
                result['in_progress'] = True
                save_local_report(policy.state_dir, 'last-run.json', data)
                try:
                    with operation_activity(
                            f'Archiving before transfer {plan["target"]}/{plan["area"]}',
                            len(transitions)) as activity:
                        if activity is not None:
                            activity.count_verb = 'archived'
                        archive_policy_copies(
                            plan | {'policy_archives': transitions}, run_id,
                            result['policy_archived'], activity,
                            lambda: save_local_report(policy.state_dir, 'last-run.json', data))
                except (KeyboardInterrupt, ConfigError, TransportError, OSError) as exc:
                    result['error'] = ("Interrupted by user during archiving."
                                       if isinstance(exc, KeyboardInterrupt) else str(exc))
                    result['policy_archive_completion_uncertain'] = True
                    data['interrupted'] = isinstance(exc, KeyboardInterrupt)
                    prearchive_failed = True
                finally:
                    result.pop('in_progress', None)
                    save_local_report(policy.state_dir, 'last-run.json', data)
                if prearchive_failed:
                    break
        attempted = 0
        for plan, result in zip(plans, results):
            if prearchive_failed:
                break
            attempted += 1
            if args.command == 'run':
                result['in_progress'] = True
                data['active_phase'] = f"{plan['target']}/{plan['area']}/{result['kind']}"
                save_local_report(policy.state_dir, 'last-run.json', data)
            if args.command == 'run':
                print(f"{ui('Starting', 'info', sys.stderr)} {plan['target']}/{plan['area']}/{result['kind']}: "
                      f"{plan['selected']} items selected.", file=sys.stderr, flush=True)
            if args.command == 'preview' and not args.simulate:
                result['ok'] = True
                continue
            try:
                stats = perform_sync(plan, policy.state_dir, run_id, limit,
                                     args.command == "preview", policy.home,
                                     getattr(args, 'progress_depth', 2))
                if args.command == 'run':
                    result.update(transfer_metrics(stats, True))
                    result['archive_deleted_completed_paths'] = result['archive_deleted_paths'][:]
                    remaining_archives = [item for item in plan['policy_archives']
                                          if item['new_action'] not in ('plain', 'private')
                                          and not item.get('pre_archive')]
                    if remaining_archives:
                        with operation_activity(
                                f'Archiving due to policy {plan["target"]}/{plan["area"]}',
                                len(remaining_archives)) as activity:
                            if activity is not None:
                                activity.count_verb = 'archived'
                            archive_policy_copies(
                                plan | {'policy_archives': remaining_archives}, run_id,
                                result['policy_archived'], activity,
                                lambda: save_local_report(policy.state_dir, 'last-run.json', data))
                result["ok"] = True
            except TransferInterrupted as exc:
                result.update(transfer_metrics(exc.stats, False))
                if args.command == 'run':
                    result['archive_deleted_completed_paths'] = completed_deleted_archive_paths(plan, run_id)
                result.update(ok=False, error="Interrupted by user; files already uploaded remain in the cloud.")
                if args.command == 'run':
                    data['interrupted'] = True
                    result.pop('in_progress', None)
                    save_local_report(policy.state_dir, 'last-run.json', data)
                break
            except KeyboardInterrupt:
                if args.command == 'run':
                    result.update(transfer_metrics(None, False))
                    if result['archive_deleted_completed_paths'] is None:
                        result['archive_deleted_completed_paths'] = completed_deleted_archive_paths(plan, run_id)
                result.update(ok=False, error="Interrupted by user; files already uploaded remain in the cloud.")
                if args.command == 'run':
                    data['interrupted'] = True
                    result.pop('in_progress', None)
                    save_local_report(policy.state_dir, 'last-run.json', data)
                break
            except (ConfigError, TransportError, OSError) as exc:
                if args.command == 'run' and 'transferred_files' not in result:
                    result.update(transfer_metrics(getattr(exc, 'stats', None), False))
                    result['archive_deleted_completed_paths'] = completed_deleted_archive_paths(plan, run_id)
                result.update(ok=False, error=str(exc))
            if args.command == 'run':
                result.pop('in_progress', None)
                save_local_report(policy.state_dir, 'last-run.json', data)
        preview_note = ("Preview; no cloud changes." if args.simulate else
                        "Plan based on local selection and current inventories; "
                        "content differences not checked. No cloud changes.") if args.command == 'preview' else None
        if args.command == "run":
            complete = not data['interrupted'] and not prearchive_failed and attempted == len(plans)
            for result in results:
                if not result['ok'] and 'error' not in result:
                    result['error'] = 'Not started: an earlier phase did not complete.'
            data.update(ok=all(item['ok'] for item in results),
                        complete=complete,
                        active_phase=None if complete else data['active_phase'],
                        finished_at=datetime.now(timezone.utc).isoformat())
            save_local_report(policy.state_dir, 'last-run.json', data)
        else:
            data = {"operation": args.command, "run_id": run_id,
                    "ok": all(item["ok"] for item in results),
                    "results": results, "note": preview_note, "conflicts": conflicts}
        if getattr(args, "json", False):
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            print(ui(data["note"], 'subtle'))
            for item in conflicts:
                print(f"{ui('WARNING:', 'warning')} {ui(item['target'] + '/current', 'remote')}: "
                      f"{ui(display_safe_path(item['path']), 'muted')} in plain and private; "
                      f"the copy {', '.join(item['archive_from'])} must be archived before transfers.")
            for result in results:
                outcome = ui('OK', 'success') if result['ok'] else ui('ERROR', 'error')
                absence_label = 'to archive because absent from local selection'
                print(f"{ui(result['target'] + '/' + result['area'] + '/' + result['kind'], 'remote')}: {outcome}; "
                      f"selected {result['selected']}; {absence_label} {result['archive_deleted']}; "
                      f"to archive due to policy {result['archive_policy']}.")
                absent_paths = result['archive_deleted_paths']
                for path in absent_paths if args.command == 'preview' else absent_paths[:20]:
                    print(f"  Absent from local selection: {ui(display_safe_path(path), 'muted')}")
                if args.command == 'run' and len(absent_paths) > 20:
                    print(ui(f"  Other {len(absent_paths) - 20} paths in macbackup status.", 'subtle'))
                for item in result['policy_archives']:
                    print(f"  Policy: {item['path']} → {item['new_action']}; {item['reason']}" +
                          (f" Rule: {item['rule']}" if item['rule'] else ''))
                if result['archive_limit_exceeded']:
                    print(f"  {ui('WARNING:', 'warning')} exceeds max_delete={limit}; a run would stop before transfers.")
                if args.command == 'run':
                    archived = result['archive_deleted_completed_paths']
                    if archived is None:
                        print(ui('Archives for local selection absence: completion could not be verified.', 'warning'))
                    else:
                        print(f'Archived for absence from local selection: '
                              f'{len(archived)}/{result["archive_deleted"]}.')
                    if result['archive_policy']:
                        print(f"Archived due to policy: {len(result['policy_archived'])}/{result['archive_policy']}.")
                    if result.get('transferred_files') is None:
                        print('Transfers: count unavailable.')
                    else:
                        label = ' (latest report, incomplete)' if not result['counts_complete'] else ''
                        print(f"Transferred {result['transferred_files']} file, "
                              f"{progress_quantity(result['transferred_bytes'], False)}; "
                              f"checked {result['checked_files']}{label}.")
                        if result.get('server_side_moves') is not None:
                            print(f"Server-side moves: {result['server_side_moves']} file, "
                                  f"{progress_quantity(result['server_side_move_bytes'], False)}{label}.")
                if not result["ok"]:
                    print(f"{ui('ERROR:', 'error')} {result['error']}")
        return 0 if data["ok"] else 1
