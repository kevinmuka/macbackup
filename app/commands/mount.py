"""Internal macbackup mount module."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import time
import uuid

from ..i18n import print
from ..inventory import mount_current_conflicts
from ..policy import ConfigError, Policy, absolute_path, inside
from ..state import operation_lock
from ..transport import TransportError, load_targets, remote_join, transport_env
from ..ui import MountPulse, discard_mount_input, mount_input_wait, terminal_style


def mount_merged_env(roots: dict[str, str]) -> tuple[str, dict[str, str]]:
    suffix = uuid.uuid4().hex[:12]
    names = {part: f'macbackup_mount_{suffix}_{part}'
             for part in ('current', 'history', 'icons', 'view')}
    prefix = lambda part: 'RCLONE_CONFIG_' + names[part].upper() + '_'
    env = {
        prefix('current') + 'TYPE': 'union',
        prefix('current') + 'UPSTREAMS': ' '.join(
            json.dumps(remote_join(roots[area], 'current') + ':ro')
            for area in ('private', 'plain')),
        prefix('current') + 'SEARCH_POLICY': 'epall',
        prefix('history') + 'TYPE': 'combine',
        prefix('history') + 'UPSTREAMS': ' '.join(
            json.dumps(f'{area}={remote_join(roots[area], "history")}')
            for area in ('plain', 'private')),
        prefix('icons') + 'TYPE': 'combine',
        prefix('icons') + 'UPSTREAMS': ' '.join(
            json.dumps(f'{area}={remote_join(roots[area], "macos-icons")}')
            for area in ('plain', 'private')),
        prefix('view') + 'TYPE': 'combine',
        prefix('view') + 'UPSTREAMS': ' '.join(
            f'{part}={names[part]}:' for part in ('current', 'history', 'icons')),
    }
    return names['view'] + ':', env



def mount_operation(args, config: Path, home: Path) -> int:
    with discard_mount_input() as input_fd:
        return _mount_operation(args, config, home, input_fd)



def _mount_operation(args, config: Path, home: Path, input_fd: int | None) -> int:
    if sys.platform != 'darwin':
        raise ConfigError('macbackup mounting requires macOS.')
    executable = shutil.which('rclone')
    if executable is None:
        raise ConfigError('rclone is not available in PATH.')
    state = home / '.local/state/macbackup'
    processes = []
    created_points = []
    mounted_points = set()
    interrupted = False
    try:
        with operation_lock(state):
            targets, _ = load_targets(config, args.target)
            policy = Policy(config, home)
            base = absolute_path(args.mountpoint) if args.mountpoint else home / 'Desktop'
            if args.area == 'all':
                mounts = []
                for name, roots in targets.items():
                    mount_current_conflicts(roots, name, input_fd)
                    view, env = mount_merged_env(roots)
                    mounts.append((name, 'all', view, base / name, env))
            else:
                mounts = [(name, args.area, roots[args.area], base / f'{name}-{args.area}', {})
                          for name, roots in targets.items()]
            for _, _, _, point, _ in mounts:
                if point not in policy.mount_roots and any(
                        inside(point, rule.path) or inside(rule.path, point) for rule in policy.roots):
                    raise ConfigError(f'{point}: the mount overlaps a backup source.')
                ancestors_below_home = []
                for ancestor in (point, *point.parents):
                    if ancestor == home:
                        break
                    ancestors_below_home.append(ancestor)
                if any(ancestor.is_symlink() for ancestor in ancestors_below_home):
                    raise ConfigError(f'{point}: the mount path crosses a symlink.')
                if os.path.ismount(point):
                    raise ConfigError(f'{point}: already mounted.')
                if point.exists() and (not point.is_dir() or any(point.iterdir())):
                    raise ConfigError(f'{point}: an empty directory is required.')
            for name, area, remote, point, extra_env in mounts:
                created = not point.exists()
                if created:
                    point.mkdir(parents=True, mode=0o700)
                if created:
                    info = point.stat()
                    created_points.append((point, info.st_dev, info.st_ino))
                log = state / f'mount-{name}-{area}.log'
                fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, 'w') as output:
                    process = subprocess.Popen(
                        [executable, '--ask-password=false', 'nfsmount', remote, str(point), '--read-only',
                         '--vfs-cache-mode', 'off', '--addr', '127.0.0.1:0'],
                        stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                        start_new_session=True, env=transport_env() | extra_env)
                processes.append((process, point, log))
                deadline = time.monotonic() + 30
                pulse = MountPulse(f'Starting {name}/{area}')
                try:
                    while not os.path.ismount(point):
                        code = process.poll()
                        if code is not None:
                            detail = log.read_text(errors='replace')[-1200:]
                            if code == 0 and 'Unmounted rclone mount' in detail:
                                pulse.done('unmounted')
                                print(f'{terminal_style("Volume unmounted:", "1;32", sys.stdout)} '
                                      f'{terminal_style(str(point), "36", sys.stdout)}', flush=True)
                                return 0
                        if code is not None or time.monotonic() >= deadline:
                            pulse.done('error')
                            detail = log.read_text(errors='replace')[-1200:]
                            raise TransportError(f'Mount {name}/{area} failed: {detail or "no response from rclone."}')
                        pulse.draw()
                        mount_input_wait(input_fd, 0.2)
                except KeyboardInterrupt:
                    pulse.done('interrupted')
                    raise
                pulse.done('complete')
                mounted_points.add(point)
                print(f'{terminal_style(f"Mounted {name}/{area} read-only at:", "1;32", sys.stdout)} '
                      f'{terminal_style(str(point), "36", sys.stdout)}', flush=True)
        # The mounted read-only volume stays open, but no longer holds the operation lock.
        print(terminal_style('Keep this terminal open; press Ctrl-C or eject the volume in Finder (⌘E).',
                             '2', sys.stdout), flush=True)
        while True:
            for process, point, log in processes:
                code = process.poll()
                if code is not None:
                    if code == 0:
                        deadline = time.monotonic() + 3
                        while os.path.ismount(point) and time.monotonic() < deadline:
                            mount_input_wait(input_fd, 0.1)
                        if not os.path.ismount(point):
                            print(f'{terminal_style("Volume unmounted:", "1;32", sys.stdout)} '
                                  f'{terminal_style(str(point), "36", sys.stdout)}', flush=True)
                            return 0
                    detail = log.read_text(errors='replace')[-1200:]
                    raise TransportError(f'Mount exited with code {code}: {point}. {detail}')
            mount_input_wait(input_fd, 1)
    except KeyboardInterrupt:
        interrupted = True
        print('\nUnmounting...', flush=True)
    finally:
        for process, _, _ in reversed(processes):
            if process.poll() is None:
                process.terminate()
        for process, point, _ in reversed(processes):
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                print(f'{terminal_style("WARNING:", "1;33", sys.stderr)} '
                      f'{point} did not unmount; check with mount.', file=sys.stderr)
        for point, device, inode in reversed(created_points):
            try:
                info = point.lstat()
                if (not os.path.ismount(point) and stat.S_ISDIR(info.st_mode)
                        and (info.st_dev, info.st_ino) == (device, inode)):
                    point.rmdir()
            except (FileNotFoundError, OSError):
                # An active mount, changed directory or nonempty folder is left intact.
                pass
        if interrupted:
            for _, point, _ in processes:
                if point in mounted_points and not os.path.ismount(point):
                    print(f'{terminal_style("Volume unmounted:", "1;32", sys.stdout)} '
                          f'{terminal_style(str(point), "36", sys.stdout)}', flush=True)
    return 0
