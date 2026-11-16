#!/usr/bin/env python3
"""Build the executable macbackup archive for distribution."""

import argparse
import os
from pathlib import Path
import shutil
import sys
import tempfile
import zipapp


REPO = Path(__file__).resolve().parents[1]
ENTRY_POINT = """\
import sys

if sys.version_info < (3, 11):
    raise SystemExit("macbackup requires Python 3.11 or later.")

from lib.macbackup.cli import main

raise SystemExit(main())
"""


def build(output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.macbackup-build-', dir=output.parent) as work:
        source = Path(work) / 'source'
        library = source / 'lib'
        library.mkdir(parents=True)
        shutil.copyfile(REPO / 'lib/__init__.py', library / '__init__.py')
        package = REPO / 'lib/macbackup'
        for module in sorted(package.rglob('*.py')):
            if not module.is_file() or module.is_symlink() or '__pycache__' in module.parts:
                continue
            destination = library / 'macbackup' / module.relative_to(package)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(module, destination)
        (source / '__main__.py').write_text(ENTRY_POINT, encoding='utf-8')
        archive = Path(work) / 'macbackup'
        zipapp.create_archive(source, archive, interpreter='/usr/bin/env python3', compressed=True)
        # Publish the complete archive without overwriting an existing file or symlink.
        os.link(archive, output)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=REPO / 'dist/macbackup',
                        metavar='FILE', help='new output file (default: dist/macbackup)')
    args = parser.parse_args()
    if sys.version_info < (3, 11):
        parser.exit(1, 'The macbackup build requires Python 3.11 or later.\n')
    output = args.output.expanduser().absolute()
    try:
        build(output)
    except FileExistsError:
        parser.exit(1, f'Output already exists: {output}\n')
    except OSError as exc:
        parser.exit(1, f'Could not build macbackup: {exc}\n')
    print(output)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
