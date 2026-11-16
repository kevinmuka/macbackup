#!/usr/bin/env python3
"""Prepare the macbackup downloads and the matching Homebrew formula."""

import argparse
import hashlib
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

from build_macbackup import REPO, build


FORMULA = """\
require "shellwords"

class Macbackup < Formula
  desc "Back up selected Mac files with rclone"
  homepage "https://dotfiles.kevinmuka.dev/macbackup"
  url "https://github.com/kevinmuka/macbackup/releases/download/macbackup-v@VERSION@/macbackup-@VERSION@.tar.gz"
  version "@VERSION@"
  sha256 "@SHA256@"

  depends_on :macos
  depends_on "python@3.14"
  depends_on "rclone"

  def install
    libexec.install "macbackup"
    python = Formula["python@3.14"].opt_bin/"python3.14"
    rclone_bin = Formula["rclone"].opt_bin
    (bin/"macbackup").write_env_script python, [Shellwords.escape((libexec/"macbackup").to_s)],
                                      PATH: "#{rclone_bin}:$PATH"
  end

  test do
    assert_equal "macbackup #{version}", shell_output("#{bin}/macbackup --version").strip
    assert_match "protected file selection", shell_output("MACBACKUP_LANG=en #{bin}/macbackup --help")
    assert_match "selezione protetta", shell_output("MACBACKUP_LANG=it #{bin}/macbackup --help")
  end
end
"""


def prepare_release(directory: Path | None) -> Path:
    with tempfile.TemporaryDirectory(prefix='macbackup-release-') as work:
        staging = Path(work)
        executable = staging / 'macbackup'
        build(executable)
        result = subprocess.check_output([sys.executable, str(executable), '--version'], text=True)
        match = re.fullmatch(r'macbackup (\d+\.\d+\.\d+)\n', result)
        if not match:
            raise ValueError('Could not determine the version from the built command.')
        version = match[1]
        archive = staging / f'macbackup-{version}.tar.gz'
        with tarfile.open(archive, 'w:gz') as package, executable.open('rb') as stream:
            member = tarfile.TarInfo('macbackup')
            member.size = executable.stat().st_size
            member.mode = 0o755
            package.addfile(member, stream)

        hashes = {file.name: hashlib.sha256(file.read_bytes()).hexdigest()
                  for file in (executable, archive)}
        (staging / 'SHA256SUMS').write_text(
            ''.join(f'{digest}  {name}\n' for name, digest in hashes.items()), encoding='utf-8')
        formula = staging / 'Formula/macbackup.rb'
        formula.parent.mkdir()
        formula.write_text(FORMULA.replace('@VERSION@', version)
                           .replace('@SHA256@', hashes[archive.name]), encoding='utf-8')

        output = directory or REPO / 'dist' / f'macbackup-{version}'
        output.mkdir(parents=True)
        shutil.copytree(staging, output, dirs_exist_ok=True)
        return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, metavar='DIRECTORY',
                        help='new output directory (default: dist/macbackup-VERSION)')
    args = parser.parse_args()
    output = args.output_dir.expanduser().absolute() if args.output_dir else None
    try:
        release = prepare_release(output)
    except FileExistsError as exc:
        parser.exit(1, f'Output already exists: {exc.filename}\n')
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f'Could not prepare the macbackup release: {exc}\n')
    print(release)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
