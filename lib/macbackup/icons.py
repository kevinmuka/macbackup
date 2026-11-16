"""Internal macbackup icons module."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import stat
import sys
import uuid

from .i18n import print
from .policy import ConfigError, Entry


FINDER_ICON = "Icon\r"



ICON_SIDECAR = "/icon.json"



class MacXattrs:
    """Use fd-based native APIs: Python's os xattr APIs are absent on macOS."""
    def __init__(self):
        if sys.platform != "darwin":
            raise ConfigError("Finder icons require macOS.")
        import ctypes
        self.c = ctypes
        self.lib = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
        self.lib.fgetxattr.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p,
                                      ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int]
        self.lib.fgetxattr.restype = ctypes.c_ssize_t
        self.lib.fsetxattr.argtypes = self.lib.fgetxattr.argtypes
        self.lib.fsetxattr.restype = ctypes.c_int

    def get(self, fd: int, name: str) -> bytes | None:
        size = self.lib.fgetxattr(fd, name.encode(), None, 0, 0, 0)
        if size < 0:
            error = self.c.get_errno()
            if error == 93:  # ENOATTR on Darwin
                return None
            raise OSError(error, "Could not read metadata " + name)
        if size > 64 * 1024 * 1024:
            raise ConfigError("Icon metadata exceeds 64 MiB; operation blocked.")
        buffer = self.c.create_string_buffer(size)
        if self.lib.fgetxattr(fd, name.encode(), buffer, size, 0, 0) != size:
            raise OSError("Icon metadata changed while being read.")
        return buffer.raw

    def set(self, fd: int, name: str, value: bytes) -> None:
        if self.lib.fsetxattr(fd, name.encode(), value, len(value), 0, 0):
            raise OSError(self.c.get_errno(), "Could not restore metadata " + name)



def icon_snapshot(entry: Entry) -> dict:
    api = MacXattrs()
    path = Path(entry.path)
    parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fd = os.open(FINDER_ICON, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or (before.st_dev, before.st_ino) != (entry.device, entry.inode):
                raise ConfigError("The icon was replaced after the scan.")
            if before.st_size > 64 * 1024 * 1024:
                raise ConfigError("Icon file exceeds 64 MiB; operation blocked.")
            with os.fdopen(os.dup(fd), "rb") as stream:
                data = stream.read(64 * 1024 * 1024 + 1)
            if len(data) > 64 * 1024 * 1024:
                raise ConfigError("Icon file grew beyond 64 MiB while being read.")
            attrs = {name: api.get(fd, name) for name in ('com.apple.ResourceFork', 'com.apple.FinderInfo')}
            finder = api.get(parent, 'com.apple.FinderInfo')
            after = os.fstat(fd)
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ConfigError("The icon changed while being read.")
        finally:
            os.close(fd)
    finally:
        os.close(parent)
    if any(value is not None and len(value) != 32 for value in (finder, attrs['com.apple.FinderInfo'])):
        raise ConfigError("FinderInfo has an unexpected size; operation blocked.")
    encode = lambda value: None if value is None else base64.b64encode(value).decode('ascii')
    return {'schema_version': 1, 'kind': 'macos-finder-icon', 'source': entry.path,
            'data_base64': encode(data), 'file_xattrs': {k: encode(v) for k, v in attrs.items()},
            'folder_finder_info': encode(finder)}



def restore_icon(file: Path, folder: Path, replace: bool) -> dict:
    if file.stat().st_size > 180 * 1024 * 1024:
        raise ConfigError("Icon restore file is too large.")
    snapshot = json.loads(file.read_text())
    if (not isinstance(snapshot, dict) or type(snapshot.get('schema_version')) is not int or snapshot.get('schema_version') != 1
            or snapshot.get('kind') != 'macos-finder-icon'):
        raise ConfigError("Unrecognized icon restore format.")
    try:
        decode = lambda value: base64.b64decode(value, validate=True)
        data = decode(snapshot['data_base64'])
        attrs = snapshot['file_xattrs']
        if not isinstance(attrs, dict) or set(attrs) != {'com.apple.ResourceFork', 'com.apple.FinderInfo'}:
            raise ValueError("Attributi inattesi")
        attrs = {k: None if v is None else decode(v) for k, v in attrs.items()}
        finder = snapshot['folder_finder_info']
        finder = None if finder is None else decode(finder)
        if any(value is not None and len(value) != 32 for value in (finder, attrs['com.apple.FinderInfo'])):
            raise ValueError("Invalid FinderInfo")
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigError("Invalid icon restore data.") from exc
    api = MacXattrs()
    directory = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = '.macbackup-icon-' + uuid.uuid4().hex
    try:
        current = api.get(directory, 'com.apple.FinderInfo') or bytes(32)
        if len(current) != 32:
            raise ConfigError("Invalid FinderInfo on destination folder.")
        flags = int.from_bytes(current[8:10], 'big') & ~0x0400
        flags |= int.from_bytes((finder or bytes(32))[8:10], 'big') & 0x0400
        updated = current[:8] + flags.to_bytes(2, 'big') + current[10:]
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        try:
            with os.fdopen(os.dup(fd), 'wb') as stream:
                stream.write(data)
            for name, value in attrs.items():
                if value is not None:
                    api.set(fd, name, value)
        finally:
            os.close(fd)
        if replace:
            os.rename(temporary, FINDER_ICON, src_dir_fd=directory, dst_dir_fd=directory)
        else:
            try:
                os.link(temporary, FINDER_ICON, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
            except FileExistsError as exc:
                raise ConfigError("Icon already present; use --replace to replace only the icon.") from exc
            os.unlink(temporary, dir_fd=directory)
        api.set(directory, 'com.apple.FinderInfo', updated)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
        os.close(directory)
    return {'ok': True, 'folder': str(folder), 'note': 'Icon restored; no cloud access.'}
