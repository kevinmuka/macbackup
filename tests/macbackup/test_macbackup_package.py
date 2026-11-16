"""Exercise both the dotfiles launcher and the standalone executable archive."""

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from zipfile import ZipFile

from . import test_macbackup_transport as transport_tests


REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / 'bin/macbackup'
BUILDER = REPO / 'scripts/build_macbackup.py'
RELEASE_BUILDER = REPO / 'scripts/build_macbackup_release.py'


def build_archive(destination: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(BUILDER), '--output', str(destination)],
                          cwd=destination.parent, capture_output=True, text=True)


def archive_environment(environment: dict) -> dict:
    env = {key: value for key, value in environment.items()
           if key not in ('PYTHONHOME', 'PYTHONPATH')}
    env['PATH'] = str(Path(sys.executable).parent) + os.pathsep + env.get('PATH', '')
    return env


class PackageTests(unittest.TestCase):
    def test_only_the_command_is_in_bin(self):
        self.assertTrue((REPO / 'lib/macbackup/locales/it.py').is_file())
        self.assertEqual({path.name for path in SCRIPT.parent.iterdir()}, {'macbackup'})
        self.assertTrue(os.access(SCRIPT, os.X_OK))

    def test_symlinked_command_loads_both_languages(self):
        with tempfile.TemporaryDirectory() as directory:
            link = Path(directory) / 'macbackup'
            link.symlink_to(SCRIPT)
            for language, expected in (('en', 'protected file selection'),
                                       ('it', 'selezione protetta')):
                with self.subTest(language=language):
                    result = subprocess.run([sys.executable, str(link), '--help'], cwd=directory,
                                            env={**os.environ, 'MACBACKUP_LANG': language},
                                            capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn(expected, result.stdout)

    def test_archive_runs_outside_checkout_in_both_languages(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory).resolve()
            executable = work / 'macbackup'
            built = build_archive(executable)
            self.assertEqual(built.returncode, 0, built.stderr)
            self.assertTrue(os.access(executable, os.X_OK))
            with ZipFile(executable) as archive:
                members = {entry.filename for entry in archive.infolist() if not entry.is_dir()}
                self.assertTrue({'__main__.py', 'lib/__init__.py', 'lib/macbackup/cli.py',
                                 'lib/macbackup/locales/it.py'}.issubset(members))
                self.assertTrue(all(
                    entry in {'__main__.py', 'lib/__init__.py'}
                    or (entry.startswith('lib/macbackup/') and entry.endswith('.py')
                        and '__pycache__' not in entry.split('/'))
                    for entry in members
                ), members)
            env = archive_environment(os.environ) | {'HOME': str(work)}
            for language in ('en', 'it'):
                with self.subTest(language=language):
                    env['MACBACKUP_LANG'] = language
                    for arguments in (('--help',), ('run', '--help'), ('--version',),
                                      ('explain', 'relative-path')):
                        actual = subprocess.run([str(executable), *arguments], cwd=work,
                                                env=env, capture_output=True, text=True)
                        expected = subprocess.run([sys.executable, str(SCRIPT), *arguments],
                                                  cwd=work, env=env, capture_output=True, text=True)
                        self.assertEqual(actual.returncode,
                                         2 if arguments[0] == 'explain' else 0, actual.stderr)
                        self.assertEqual((actual.stdout, actual.stderr),
                                         (expected.stdout, expected.stderr))

    def test_build_preserves_an_existing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            existing = Path(directory) / 'macbackup'
            existing.write_text('existing file')
            built = build_archive(existing)
            self.assertNotEqual(built.returncode, 0)
            self.assertIn('Output already exists:', built.stderr)
            self.assertEqual(existing.read_text(), 'existing file')

    def test_release_formula_and_downloads_agree(self):
        with tempfile.TemporaryDirectory() as directory:
            release = Path(directory) / 'release'
            command = [sys.executable, str(RELEASE_BUILDER), '--output-dir', str(release)]
            built = subprocess.run(command, cwd=directory, capture_output=True, text=True)
            self.assertEqual(built.returncode, 0, built.stderr)
            executable = release / 'macbackup'
            version = subprocess.check_output(
                [sys.executable, str(executable), '--version'], cwd=directory, text=True
            ).strip().removeprefix('macbackup ')
            archive = release / f'macbackup-{version}.tar.gz'
            with tarfile.open(archive) as bundle:
                self.assertEqual(bundle.getnames(), ['macbackup'])
                self.assertTrue(bundle.getmember('macbackup').mode & 0o111)
                self.assertEqual(bundle.extractfile('macbackup').read(), executable.read_bytes())

            hashes = {}
            for artifact in (executable, archive):
                hashes[artifact.name] = hashlib.sha256(artifact.read_bytes()).hexdigest()
            sums = (release / 'SHA256SUMS').read_text().splitlines()
            self.assertEqual(sums, [f'{digest}  {name}' for name, digest in hashes.items()])
            formula = (release / 'Formula/macbackup.rb').read_text()
            self.assertIn(f'https://github.com/kevinmuka/macbackup/releases/download/macbackup-v{version}/{archive.name}"', formula)
            self.assertIn(f'sha256 "{hashes[archive.name]}"', formula)

            rebuilt = subprocess.run(command, cwd=directory, capture_output=True, text=True)
            self.assertNotEqual(rebuilt.returncode, 0)
            self.assertIn('Output already exists:', rebuilt.stderr)
            self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(), hashes[archive.name])


@unittest.skipUnless(shutil.which('rclone'), 'rclone is needed for the local round trip')
class ArchiveRoundTripTests(unittest.TestCase):
    setUp = transport_tests.TransportTests.setUp
    rules = transport_tests.TransportTests.rules
    settings = transport_tests.TransportTests.settings

    def test_archive_backs_up_and_restores_both_areas(self):
        executable = self.base / 'macbackup'
        built = build_archive(executable)
        self.assertEqual(built.returncode, 0, built.stderr)
        self.rules(private=True)
        env = archive_environment(self.environment) | {'MACBACKUP_LANG': 'en'}

        def run(*args):
            result = subprocess.run([str(executable), *args], cwd=self.base,
                                    env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        run('run', '--target', 'google', '--json')
        for area, original in (('plain', self.normal), ('private', self.secret)):
            with self.subTest(area=area):
                destination = self.base / f'restored-{area}'
                run('restore', str(original), '--target', 'google', '--area', area,
                    '--version', 'current', '--to', str(destination))
                self.assertEqual(destination.read_bytes(), original.read_bytes())
