"""Internal macbackup ui module."""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import select
import shutil
import sys
import termios
import threading
import time
import tty

from .i18n import localized, message, print


def active_directories(stats: dict, home: Path, depth: int) -> list[str]:
    groups = set()
    prefix = str(home).lstrip('/') + '/'
    for item in stats.get('transferring', []):
        name = item.get('name') if isinstance(item, dict) else None
        if not isinstance(name, str):
            continue
        if name.startswith(prefix):
            parts = name[len(prefix):].split('/')
            folders = parts[:-1] or parts
            groups.add('~/' + '/'.join(folders[:depth]))
        elif name:
            parts = name.lstrip('/').split('/')
            folders = parts[:-1] or parts
            groups.add('/' + '/'.join(folders[:depth]))
    return sorted(groups)



def progress_quantity(value: object, binary: bool) -> str:
    if not isinstance(value, (int, float)) or value < 0:
        return '?'
    base = 1024 if binary else 1000
    units = ('B', 'KiB', 'MiB', 'GiB', 'TiB') if binary else ('B', 'kB', 'MB', 'GB', 'TB')
    number = float(value)
    unit = 0
    while number >= base and unit < len(units) - 1:
        number /= base
        unit += 1
    return f'{number:.1f} {units[unit]}' if unit else f'{int(number)} B'



def progress_duration(value: object) -> str:
    if not isinstance(value, (int, float)) or value < 0:
        return '?'
    seconds = int(value)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    parts = [f'{amount}{suffix}' for amount, suffix in
             ((days, 'd'), (hours, 'h'), (minutes, 'm'), (seconds, 's')) if amount]
    if not parts:
        parts.append('0s')
    return ''.join(parts)



def progress_bar(done: object, total: object, width: int = 28) -> str:
    if not isinstance(done, (int, float)) or not isinstance(total, (int, float)) or total <= 0:
        return '[' + '-' * width + ']  ?%'
    ratio = max(0.0, min(1.0, done / total))
    filled = min(width, int(ratio * width))
    return '[' + '#' * filled + '-' * (width - filled) + f'] {ratio * 100:5.1f}%'



def progress_lines(stats: dict | None, home: Path, depth: int, binary: bool,
                   legend: bool, label: str, width: int = 90) -> list[str]:
    stats = stats or {}
    bar_width = max(12, min(36, width - 55))
    lines = [f'macbackup  {label}  |  Elapsed {progress_duration(stats.get("elapsedTime"))}', '']
    byte_count = stats.get('bytes', 0)
    total_bytes = stats.get('totalBytes')
    transfers = stats.get('transfers', 0)
    total_transfers = stats.get('totalTransfers')
    lines.extend((
        f'Data       {progress_bar(byte_count, total_bytes, bar_width)}  '
        f'{progress_quantity(byte_count, binary)} / {progress_quantity(total_bytes, binary)}',
        f'File       {progress_bar(transfers, total_transfers, bar_width)}  '
        f'{transfers} / {total_transfers if isinstance(total_transfers, int) else "?"}',
        '',
        f'SPEED  {progress_quantity(stats.get("speed"), binary)}/s'
        f'     ETA {progress_duration(stats.get("eta"))}',
        f'Checks {stats.get("checks", 0)} / {stats.get("totalChecks", "?")}'
        f'     Listed {stats.get("listed", "?")}',
        f'Server-side moves {stats.get("serverSideMoves", 0)}'
        f' ({progress_quantity(stats.get("serverSideMoveBytes", 0), binary)})'
        f'     Renamed {stats.get("renames", 0)}',
        '',
    ))
    folders = active_directories(stats, home, depth)
    lines.append('Active folders: ' + (', '.join(folders) if folders else 'none'))
    lines.append('Active files:')
    active = stats.get('transferring', [])
    if not active:
        lines.append('  none')
    for item in active:
        if not isinstance(item, dict):
            continue
        name = item.get('name', '?')
        lines.append('  ' + str(name))
        lines.append('    ' + progress_bar(item.get('bytes'), item.get('size'), 12)
                     + '  ' + progress_quantity(item.get('bytes'), binary)
                     + ' / ' + progress_quantity(item.get('size'), binary)
                     + '  |  ' + progress_quantity(item.get('speed'), binary) + '/s')
    current_unit = 'binary (KiB, MiB, GiB)' if binary else 'decimal (kB, MB, GB)'
    next_unit = 'decimal' if binary else 'binary'
    lines.extend(('', f'Keys  [u] Switch to {next_unit} units  |  '
                   f'[?] {"Hide" if legend else "Show"} legend  |  '
                   '[Ctrl-C] Stop backup',
                  f'Current units: {current_unit}'))
    if legend:
        lines.extend((
            '', 'Legend (current phase only: plain, icons or private)',
            '  Data: bytes transferred / expected. The percentage is based on bytes.',
            '  Files: transfers completed / expected. The percentage is based on files.',
            '  SPEED: current overall rate; ETA: estimate may change.',
            '  Checks: files compared with existing copies; Listed: entries enumerated.',
            '  Server-side moves: files moved by the provider without downloading.',
            '  Renamed: files recognized under a new name.',
            '  Active folders and files: in-progress operations; several may run at once.',
        ))
    return lines



def styled_progress_lines(lines: list[str], stream) -> str:
    """Highlight dashboard landmarks without changing redirected output."""
    if not terminal_can_color(stream):
        return localized('\n'.join(lines))
    lines = [localized(line) for line in lines]
    labels = {'macbackup': '1;36', 'Data': '1;36',
              'Active files:': '1;36', 'File': '1;36',
              'SPEED': '1;32', 'Checks': '1;36',
              'Server-side moves': '1;36', 'Active folders:': '1;36'}
    rendered = []
    active_files = False
    secondary = False
    for line in lines:
        if line == 'Active files:':
            active_files = True
        elif line.startswith('Keys'):
            active_files = False
            secondary = True
        if secondary:
            rendered.append(terminal_style(line, '2;90', stream) if line else line)
            continue
        if active_files and line.startswith('  ') and not line.startswith('    '):
            rendered.append('  ' + terminal_style(line[2:], '2;90', stream))
            continue
        label = next((name for name in labels if line.startswith(name)), None)
        rendered.append(terminal_style(label, labels[label], stream) + line[len(label):]
                        if label else line)
    return '\n'.join(rendered)



class ProgressDisplay:
    def __init__(self, home: Path, depth: int, label: str):
        self.home, self.depth, self.label = home, depth, label
        self.binary = True
        self.legend = False
        self.stats: dict | None = None
        self.interactive = sys.stdin.isatty() and sys.stderr.isatty()
        self.terminal_settings = None

    def __enter__(self):
        if self.interactive:
            try:
                self.terminal_settings = termios.tcgetattr(sys.stdin.fileno())
                tty.setcbreak(sys.stdin.fileno())
                sys.stderr.write('\x1b[?1049h\x1b[?25l')
                self.draw()
            except (OSError, termios.error):
                self.interactive = False
                if self.terminal_settings is not None:
                    termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self.terminal_settings)
                    self.terminal_settings = None
        return self

    def __exit__(self, *_exc):
        if self.interactive:
            sys.stderr.write('\x1b[?25h\x1b[?1049l')
            sys.stderr.flush()
        if self.terminal_settings is not None:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self.terminal_settings)
        if self.interactive and self.stats is not None:
            print(styled_progress_lines(progress_lines(self.stats, self.home, self.depth,
                                                       self.binary, False, self.label), sys.stderr),
                  file=sys.stderr, flush=True)

    def draw(self):
        if not self.interactive:
            return
        width = shutil.get_terminal_size((90, 30)).columns
        lines = progress_lines(self.stats, self.home, self.depth,
                               self.binary, self.legend, self.label, width)
        sys.stderr.write('\x1b[H\x1b[2J' + styled_progress_lines(lines, sys.stderr) + '\n')
        sys.stderr.flush()

    def update(self, stats: dict):
        self.stats = stats
        if self.interactive:
            self.draw()
        else:
            print('\n'.join(progress_lines(stats, self.home, self.depth,
                                           self.binary, False, self.label)), file=sys.stderr, flush=True)

    def key(self, data: bytes):
        if b'u' in data.lower():
            self.binary = not self.binary
        if b'?' in data:
            self.legend = not self.legend
        self.draw()



def terminal_can_color(stream) -> bool:
    if not stream.isatty() or os.environ.get('PYTHON_COLORS') == '0':
        return False
    if os.environ.get('PYTHON_COLORS') == '1':
        return True
    if os.environ.get('NO_COLOR'):
        return False
    return bool(os.environ.get('FORCE_COLOR')) or os.environ.get('TERM') != 'dumb'



def terminal_style(value: str, code: str, stream) -> str:
    return f'\x1b[{code}m{value}\x1b[0m' if terminal_can_color(stream) else value



def ui(value: str, kind: str = 'info', stream=None) -> str:
    """Style only human-readable labels; structured output remains plain JSON."""
    # The shared WezTerm palette maps ANSI blue (34) to gray in both themes.
    remote_color = ('1;38;2;37;86;152' if os.environ.get('ZSH_THEME_MODE') == 'light'
                    else '1;38;2;139;181;236')
    colors = {'info': '1;36', 'success': '1;32', 'warning': '1;33',
              'error': '1;31', 'private': '1;35', 'muted': '2',
              'subtle': '2;90', 'remote': remote_color}
    return terminal_style(value, colors[kind], stream if stream is not None else sys.stdout)



def issue_line(label: str, message: str, kind: str) -> str:
    """Keep the warning prominent while muting the path it refers to."""
    path, separator, detail = message.partition(': ')
    if separator and path.startswith('/'):
        message = ui(path, 'muted') + separator + detail
    return (ui(label, kind) + ' ' + message).replace('\r', '\\r')



def print_selection_summary(counts: dict) -> None:
    """Use the same local selection summary for both check modes."""
    print()
    print(f"{ui('Plain:', 'info')} {counts['plain']}; "
          f"{ui('private:', 'private')} {counts['private']}; "
          f"{ui('skipped:', 'warning')} {counts['skipped']}; "
          f"{ui('selected size:', 'info')} "
          f"{progress_quantity(counts['selected_bytes'], False)}.")
    if counts['finder_icons']:
        print(f"Finder icons to preserve separately: {counts['finder_icons']}; "
              "resource fork bytes are not included in the estimate.")



class MountPulse:
    """An activity bar, or a file-count bar when the caller knows the total."""

    def __init__(self, label: str, total: int | None = None, show_count: bool | str = False):
        self.label = label
        self.started = time.monotonic()
        self.interactive = sys.stderr.isatty()
        self.total = total
        self.show_count = show_count
        self.completed = 0
        self.matches = None
        self.count_verb = None
        self.byte_fd = None
        self.byte_total = None
        self.report_path = None
        self.report_offset = 0
        self.report_expected = None
        self.report_seen = set()
        self.report_pending = b''
        if not self.interactive:
            print(f'{label}: in progress...', file=sys.stderr, flush=True)

    def refresh_report(self) -> None:
        report_path = self.report_path
        if report_path is not None:
            try:
                with report_path.open('rb') as stream:
                    stream.seek(self.report_offset)
                    new_data = stream.read()
                    self.report_offset = stream.tell()
                if self.report_expected is None:
                    self.completed += new_data.count(b'\n')
                else:
                    lines = (self.report_pending + new_data).split(b'\n')
                    self.report_pending = lines.pop()
                    for line in lines:
                        if len(line) >= 3 and line[1:2] == b' ':
                            name = line[2:].decode('utf-8', errors='replace')
                            if name in self.report_expected:
                                self.report_seen.add(name)
                    self.completed = len(self.report_seen)
            except FileNotFoundError:
                pass

    def count_detail(self) -> str:
        detail = ''
        if self.total is not None:
            verb = {'removed': 'Removed', 'archived': 'Archived',
                    'processed': 'Processed'}.get(self.count_verb, 'Checked')
            count = f'{localized(verb)} {self.completed}/{self.total}'
            detail += '  ' + terminal_style(count, '2;90', sys.stderr)
        elif self.show_count:
            if self.show_count == 'entries':
                count = f'{localized("Entries read")} {self.completed}'
            else:
                count = f'{localized("Paths checked")} {self.completed}'
            detail += '  ' + terminal_style(count, '2;90', sys.stderr)
        if self.matches is not None:
            matches = f'{localized("Matches")} {self.matches}'
            detail += '  ' + terminal_style(matches, '2;90', sys.stderr)
        if self.byte_fd is not None and self.byte_total is not None:
            received = os.fstat(self.byte_fd).st_size
            label = localized('Received')
            detail += ('  ' + terminal_style(label, '2;90', sys.stderr) + ' '
                       + progress_quantity(received, False) + ' / '
                       + progress_quantity(self.byte_total, False))
        return detail

    def draw(self, metadata_bytes: int | None = None) -> None:
        if not self.interactive:
            return
        self.refresh_report()
        width, block = 22, 5
        if self.byte_fd is not None and self.byte_total is not None:
            received = os.fstat(self.byte_fd).st_size
            filled = width * min(received, self.byte_total) // max(self.byte_total, 1)
            bar = '#' * filled + '.' * (width - filled)
        elif self.total is None:
            offset = int((time.monotonic() - self.started) * 5) % (width + block)
            bar = ''.join('#' if (index - offset) % (width + block) < block else '.'
                          for index in range(width))
        else:
            filled = width * min(self.completed, self.total) // max(self.total, 1)
            bar = '#' * filled + '.' * (width - filled)
        elapsed = progress_duration(time.monotonic() - self.started)
        detail = (f'  metadata {metadata_bytes / 1048576:.1f} MiB'
                  if metadata_bytes is not None else '')
        detail += self.count_detail()
        title = terminal_style(self.label, '1;36', sys.stderr)
        progress = terminal_style(f'[{bar}]', '34', sys.stderr)
        sys.stderr.write(localized(f'\r\x1b[2K{title} {progress} {elapsed}{detail}'))
        sys.stderr.flush()

    def done(self, detail: str) -> None:
        self.refresh_report()
        if self.interactive:
            sys.stderr.write('\r\x1b[2K')
        elapsed = progress_duration(time.monotonic() - self.started)
        color = '31' if detail == 'error' else '33' if detail == 'interrupted' else '32'
        status = terminal_style(localized(detail), color, sys.stderr)
        title = terminal_style(self.label, '1;36', sys.stderr)
        counts = self.count_detail() if detail in ('complete', 'interrupted', 'error') else ''
        print(f'{title}: {status} ({elapsed}).{counts}', file=sys.stderr, flush=True)



def mount_input_wait(input_fd: int | None, timeout: float) -> None:
    """Wait for the next mount check while discarding typed characters."""
    if input_fd is None:
        time.sleep(timeout)
    elif select.select([input_fd], [], [], timeout)[0]:
        os.read(input_fd, 4096)



@contextmanager
def operation_activity(label: str, total: int | None = None,
                       report_non_tty: bool = False, show_count: bool = False):
    """Show a TTY progress bar and discard input during a long phase."""
    if not sys.stderr.isatty():
        if not report_non_tty:
            yield None
            return
        pulse = MountPulse(label, total, show_count)
        outcome = 'complete'
        try:
            yield pulse
        except KeyboardInterrupt:
            outcome = 'interrupted'
            raise
        except BaseException:
            outcome = 'error'
            raise
        finally:
            pulse.done(getattr(pulse, 'outcome', outcome))
        return
    with discard_mount_input() as input_fd:
        pulse = MountPulse(label, total, show_count)
        stopped = threading.Event()

        def animate() -> None:
            while not stopped.is_set():
                pulse.draw()
                if input_fd is None:
                    stopped.wait(0.2)
                else:
                    mount_input_wait(input_fd, 0.2)

        worker = threading.Thread(target=animate, name='macbackup-progress', daemon=True)
        worker.start()
        outcome = 'complete'
        try:
            yield pulse
        except KeyboardInterrupt:
            outcome = 'interrupted'
            raise
        except BaseException:
            outcome = 'error'
            raise
        finally:
            stopped.set()
            worker.join()
            pulse.done(getattr(pulse, 'outcome', outcome))



@contextmanager
def discard_mount_input():
    """Hide and discard terminal input while a foreground mount is active."""
    if not sys.stdin.isatty():
        yield None
        return
    fd = sys.stdin.fileno()
    original = termios.tcgetattr(fd)
    active = termios.tcgetattr(fd)
    active[3] &= ~(termios.ECHO | termios.ICANON)
    active[0] &= ~termios.IXON
    active[6][termios.VMIN] = 1
    active[6][termios.VTIME] = 0
    disabled = os.fpathconf(fd, 'PC_VDISABLE')
    for name in ('VQUIT', 'VSUSP', 'VDSUSP'):
        index = getattr(termios, name, None)
        if index is not None:
            active[6][index] = bytes((disabled,))
    try:
        termios.tcsetattr(fd, termios.TCSANOW, active)
        termios.tcflush(fd, termios.TCIFLUSH)
        yield fd
    finally:
        termios.tcsetattr(fd, termios.TCSANOW, original)
        termios.tcflush(fd, termios.TCIFLUSH)
