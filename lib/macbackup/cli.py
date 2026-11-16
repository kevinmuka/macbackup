"""Internal macbackup cli module."""

from __future__ import annotations

import argparse
import builtins
from dataclasses import asdict
import json
from pathlib import Path
import sys

from .commands.backup import transport_operation
from .commands.forget import forget_operation
from .commands.mount import mount_operation
from .commands.restore import restore_operation
from .commands.verify import verify_operation
from .commands.versions import versions_operation
from .i18n import localized, message, print
from .icons import restore_icon
from .policy import ConfigError, Policy, absolute_path, file_kind, init_config
from .scan import scan_policy, split_layout
from .state import operation_lock
from .transport import TransportError
from .ui import issue_line, operation_activity, print_selection_summary, progress_quantity, terminal_style, ui


VERSION = "0.16.0"



class LocalizedArgumentParser(argparse.ArgumentParser):
    def _print_message(self, message, file=None):
        super()._print_message(localized(message) if message else message, file)



def localize_parser(parser: argparse.ArgumentParser) -> None:
    parser.description = localized(parser.description) if parser.description else None
    for action in parser._actions:
        if isinstance(action.help, str):
            action.help = localized(action.help)
        if isinstance(action, argparse._SubParsersAction):
            for choice in action._choices_actions:
                choice.help = localized(choice.help)
            for child in action.choices.values():
                localize_parser(child)



def make_parser() -> argparse.ArgumentParser:
    parser = LocalizedArgumentParser(
        prog="macbackup",
        description="macbackup: protected file selection, preview and backup using rclone.",
        epilog='In path lists, directories need a trailing /; files and symlinks do not.'
    )
    parser.add_argument("--version", action="version", version=f"macbackup {VERSION}")
    parser.add_argument("--config-dir", metavar="PATH", help="absolute directory containing path lists and settings.toml")
    commands = parser.add_subparsers(dest="command", required=True,
                                     parser_class=LocalizedArgumentParser)
    commands.add_parser("init", help="create four lists without active inclusions")
    check = commands.add_parser("check", help="check path lists and local sources")
    check.add_argument("--json", action="store_true", help="structured result")
    check.add_argument("--remote", action="store_true", help="also check remotes without writing to the cloud")
    check.add_argument("--target", choices=("google", "proton", "all"), help="remote check target; requires --remote (default: google)")
    explain = commands.add_parser("explain", help="explain classification of one path")
    explain.add_argument("path", metavar="PATH")
    explain.add_argument("--json", action="store_true", help="print classification as JSON")
    layout = commands.add_parser('layout', help='show private subtrees inside a plain directory')
    layout.add_argument('path', nargs='?', metavar='PATH', help='plain folder; default: current directory')
    layout.add_argument('--json', action='store_true', help='print result as JSON')
    preview = commands.add_parser("preview", help="local preview or remote comparison without cloud changes")
    mode = preview.add_mutually_exclusive_group(required=True)
    mode.add_argument("--local", action="store_true", help="show local selection without cloud access")
    mode.add_argument("--target", choices=("google", "proton", "all"), help="read the cloud and show planned archives")
    preview.add_argument("--simulate", action="store_true",
                         help="also run cryptcheck and an rclone transfer dry run")
    preview.add_argument("--json", action="store_true", help="print plan as JSON")
    run = commands.add_parser("run", help="run backup; requires enabled targets and valid sources")
    run.add_argument("--target", choices=("google", "proton", "all"), required=True,
                     help="backup target; apply archives within max_delete")
    run.add_argument('--progress-depth', type=int, default=2, metavar='N',
                     help='active folder depth relative to home (1-5; default 2)')
    run.add_argument("--json", action="store_true", help="print final summary as JSON; progress stays on stderr")
    mount = commands.add_parser('mount', help='mount cloud copies read-only for browsing')
    mount.add_argument('--target', choices=('google', 'proton', 'all'), required=True,
                       help='service to mount; must be enabled in settings.toml')
    mount.add_argument('--area', choices=('plain', 'private', 'all'), default='all',
                       help='areas to show (default: all)')
    mount.add_argument('--mountpoint', metavar='ABS_PATH',
                       help='base folder; default: ~/Desktop')
    status = commands.add_parser("status", help="show the saved result of the latest run or verification")
    status.add_argument("--json", action="store_true", help="compatibility: status already prints JSON")
    status.add_argument("--verify", action="store_true", help="show the latest verification report")
    icon = commands.add_parser('restore-icon', help='restore one Finder icon from a local metadata file')
    icon.add_argument('--file', required=True, metavar='PATH', help='icon.json snapshot downloaded from backup')
    icon.add_argument('--folder', required=True, metavar='PATH', help='existing local folder on which to apply the icon')
    icon.add_argument('--replace', action='store_true', help='allows replacing an existing icon')
    icon.add_argument('--json', action='store_true', help='print result as JSON')
    forget = commands.add_parser('forget', help='plan or apply selective removal from current and history')
    forget.add_argument('path', nargs='?', metavar='ABS_PATH', help='original path; trailing / for a directory')
    forget.add_argument('--area', choices=('plain', 'private', 'all'), help='area from which to remove current and history copies')
    forget.add_argument('--target', choices=('google', 'proton', 'all'), help='target from which to remove copies')
    forget.add_argument('--mode', choices=('trash', 'permanent'), help='trash or permanent removal; permanent only on Google')
    forget.add_argument('--plan', metavar='ID', help='plan to apply')
    forget.add_argument('--apply', action='store_true', help='apply the checked plan')
    forget.add_argument('--json', action='store_true', help='print plan or result as JSON')
    versions = commands.add_parser('versions', help='list current and history for a file or directory')
    versions.add_argument('path', metavar='ABS_PATH')
    versions.add_argument('--target', choices=('google', 'proton', 'all'), required=True,
                          help='target in which to search versions')
    versions.add_argument('--area', choices=('plain', 'private', 'all'), default='all',
                          help='area to search (default: all)')
    versions.add_argument('--files', action='store_true', help='for a directory, list only files in history')
    versions.add_argument('--json', action='store_true', help='print versions as JSON')
    restore = commands.add_parser('restore', help='download one file version to an explicit path')
    restore.add_argument('path', metavar='ABS_PATH')
    restore.add_argument('--target', choices=('google', 'proton'), required=True,
                         help='target from which to restore the file')
    restore.add_argument('--area', choices=('plain', 'private'), required=True,
                         help='area containing the version')
    restore.add_argument('--version', required=True, metavar='VERSION',
                         help='current or history ID returned by versions')
    restore.add_argument('--to', required=True, metavar='ABS_PATH',
                         help='absolute local destination path')
    restore.add_argument('--replace', action='store_true', help='replace an existing local file')
    restore.add_argument('--links', action='store_true', help='recreate a .rclonelink as a symlink')
    restore.add_argument('--json', action='store_true', help='print result as JSON')
    verify = commands.add_parser('verify', help='compare current local sources with cloud current')
    verify.add_argument('--target', choices=('google', 'proton', 'all'), required=True,
                        help='target to compare')
    verify.add_argument('--area', choices=('plain', 'private', 'all'), default='all',
                        help='area to compare (default: all)')
    verify.add_argument('--download', action='store_true', help='read remote data for comparison')
    verify.add_argument('--size-only', action='store_true',
                        help='compare paths and sizes without reading contents')
    verify.add_argument('--json', action='store_true', help='print comparison as JSON')
    localize_parser(parser)
    return parser



def main(argv: list[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
    try:
        home = Path.home()
        config = absolute_path(args.config_dir) if args.config_dir else home / ".config/macbackup"
        if args.command == 'restore-icon':
            with operation_lock(home / '.local/state/macbackup'):
                data = restore_icon(absolute_path(args.file), absolute_path(args.folder), args.replace)
            print(json.dumps(data, ensure_ascii=False) if args.json else ui(data['note'], 'success'))
            return 0
        if args.command == 'forget':
            return forget_operation(args, config, home)
        if args.command == 'versions':
            return versions_operation(args, config, home)
        if args.command == 'restore':
            return restore_operation(args, config, home)
        if args.command == 'verify':
            return verify_operation(args, config, home)
        if args.command == 'mount':
            return mount_operation(args, config, home)
        if args.command == "init":
            files = init_config(config, home)
            print(ui("Configuration initialized.", 'success') +
                  " No paths included and no cloud access.")
            print("\n".join(files))
            return 0
        if args.command == "status":
            file = home / (".local/state/macbackup/last-verify.json" if args.verify
                           else ".local/state/macbackup/last-run.json")
            if file.is_symlink():
                raise ConfigError("The status file cannot be a symlink.")
            if not file.exists():
                message = ('No verification recorded.' if args.verify else
                           'No run recorded. No backup certified.')
                data = {"ok": None, "note": localized(message)}
            else:
                data = json.loads(file.read_text())
            # A saved report is a historical record: keep its original language.
            builtins.print(json.dumps(data, ensure_ascii=False, indent=2))
            return 1 if data.get("ok") is False else 0
        policy = Policy(config, home)
        if args.command == 'layout':
            with operation_activity('Analyzing layout', show_count=True) as pulse:
                data = split_layout(policy, absolute_path(args.path) if args.path else Path.cwd(), pulse)
            if args.json:
                print(json.dumps(data, ensure_ascii=False, indent=2))
            else:
                if not data['ok']:
                    for error in data['errors']:
                        print(ui('ERROR:', 'error', sys.stderr) + ' ' +
                              error.replace('\r', '\\r'), file=sys.stderr)
                else:
                    print(f"{ui(data['path'], 'subtle')}: {data['plain_items']} plain items, "
                          f"{data['private_items']} private.")
                    for item in data['private_subtrees']:
                        print(f"{ui('PRIVATE', 'private')} {ui(item['path'], 'subtle')} "
                              f"({item['private_items']} items, "
                              f"{progress_quantity(item['selected_bytes'], False)})")
                    if not data['private_subtrees']:
                        print('No private subtrees found inside it.')
                    print(ui(data['note'], 'subtle'))
            return 0 if data['ok'] else 1
        if args.command == "check" and args.target and not args.remote:
            raise ConfigError("check --target also requires --remote.")
        if args.command == 'preview' and args.local and args.simulate:
            raise ConfigError('preview --simulate requires --target.')
        if args.command == "run" or (args.command == "preview" and not args.local) or (args.command == "check" and args.remote):
            if args.command == "check" and args.target is None:
                args.target = "google"
            return transport_operation(args, policy)
        if args.command == "explain":
            path = absolute_path(args.path)
            try:
                info = path.lstat()
                kind = file_kind(info)
                exists = True
            except FileNotFoundError:
                kind, exists = "missing", False
            decision = policy.decide(path, directory=kind == "directory")
            data = {
                "scope": "single-path", "path": str(path), "kind": kind,
                "exists": exists, **asdict(decision),
                "note": "Classification of this path only; use check to inspect the full selection.",
            }
            if args.json:
                print(json.dumps(data, ensure_ascii=False, indent=2))
            else:
                tone = ('private' if decision.action == 'private' else
                        'error' if decision.action == 'error' else
                        'success' if decision.action == 'plain' else 'warning')
                print(f"{ui(decision.action.upper() + ':', tone)} {path}\n{decision.reason}")
                if decision.rule:
                    print(f"Rule: {decision.rule}")
                if decision.protected_pattern:
                    print(f"Protected pattern: {decision.protected_pattern}; exact inclusion: {decision.explicit}")
                if kind == "symlink":
                    print("Symlink: the target is not traversed.")
                if not exists:
                    print("The path does not currently exist; this is only a classification.")
                print(ui(data["note"], 'subtle'))
            return 1 if decision.action == "error" else 0
        with operation_activity('Local scan', show_count=True) as pulse:
            scan = scan_policy(policy, pulse)
        if not policy.roots:
            scan.warnings.append("No paths selected: the inclusion lists are empty.")
        data = {
            "scope": "local-only", "ok": not scan.errors,
            "config_dir": str(config), "summary": scan.summary(),
            "errors": scan.errors, "warnings": scan.warnings,
        }
        if args.command == "preview":
            # Fail closed: never present a partial scan as an executable plan.
            data["entries"] = [] if scan.errors else [entry.public_dict() for entry in scan.entries]
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            print(ui("Local check complete.", 'success') if data["ok"]
                  else ui("Local check FAILED.", 'error'))
            print(ui("No cloud access; no files transferred or deleted.", 'muted'))
            for error in scan.errors:
                print(issue_line('ERROR:', error, 'error'))
            for warning in scan.warnings:
                print(issue_line('WARNING:', warning, 'warning'))
            if args.command == "preview" and not scan.errors:
                for entry in scan.entries:
                    action = entry.decision.action.upper().ljust(7)
                    tone = 'private' if action.strip() == 'PRIVATE' else 'success' if action.strip() == 'PLAIN' else 'muted'
                    print((f"{ui(action, tone)} {ui(str(entry.path), 'muted')} "
                           f"[{entry.kind}] — {entry.decision.reason}").replace('\r', '\\r'))
            print_selection_summary(data["summary"])
        return 0 if data["ok"] else 1
    except KeyboardInterrupt:
        if not getattr(args, 'json', False):
            print('Interrupted by user.', file=sys.stderr)
        return 130
    except (ConfigError, OSError, TransportError, json.JSONDecodeError) as exc:
        if getattr(args, "json", False):
            print(json.dumps({"scope": "operation-error", "ok": False, "errors": [str(exc)]}, ensure_ascii=False))
        else:
            print(f'{terminal_style("ERROR:", "1;31", sys.stderr)} {exc}', file=sys.stderr)
        return 1 if isinstance(exc, TransportError) else 2
