"""Disk placement, bounded diagnostics and terminal size exchange."""
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import time


def bounded_integer(minimum, maximum):
    def parse(value):
        number = int(value)
        if not minimum <= number <= maximum:
            raise ValueError(f'must be between {minimum} and {maximum}')
        return number
    return parse


def disk_cache(path):
    path = path.expanduser().resolve()
    ancestor = path
    while not ancestor.exists():
        ancestor = ancestor.parent
    mounts = []
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        before, after = line.split(' - ', 1)
        mount = Path(re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), before.split()[4]))
        if ancestor.is_relative_to(mount):
            mounts.append((len(mount.parts), after.split()[0]))
    if not mounts or max(mounts)[1] in ('tmpfs', 'ramfs'):
        raise ValueError('QEMU cache must be on a disk-backed filesystem, not tmpfs or ramfs')
    if ',' in str(path):
        raise ValueError('QEMU cache path must not contain commas')
    return path


def private_directory(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError(f'expected a private directory owned by this user: {path}')


def write_size(report, size):
    """Atomic replacement avoids following a guest-created destination symlink."""
    columns, rows = size
    if not 1 <= rows <= 10000 or not 1 <= columns <= 10000:
        return
    fd, name = tempfile.mkstemp(prefix='.terminal-', dir=report)
    try:
        with os.fdopen(fd, 'w') as output:
            json.dump({'rows': rows, 'columns': columns}, output)
        os.replace(name, report/'terminal.json')
    finally:
        Path(name).unlink(missing_ok=True)


def read_report(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError('guest report must be a regular file')
        return json.loads(source.read(65536))


def remove_tree(path):
    if path.is_symlink():
        path.unlink()
        return
    if not path.exists():
        return
    # Helpers and the VM have stopped. Restore owner access to directories
    # a guest may have made read-only before deleting this disposable export.
    path.chmod(0o700)
    for root, directories, files in os.walk(path):
        for name in directories:
            directory = Path(root)/name
            if not directory.is_symlink():
                directory.chmod(0o700)
    shutil.rmtree(path)


def finish_artifacts(base, keep):
    # Overlays may contain credentials. Retain only bounded diagnostics, even
    # on failure. Never retain a writable disk as an accidental resume image.
    (base/'overlay.qcow2').unlink(missing_ok=True)
    (base/'seed.iso').unlink(missing_ok=True)
    remove_tree(base/'seed')
    if not keep:
        remove_tree(base)
        return
    # Guest-writable reports can contain arbitrary files or links; extract
    # only small regular reports and remove the export before retention.
    reports = {}
    for name in ('exit.json', 'verify.json'):
        try:
            reports[name] = read_report(base/'report'/name)
        except (OSError, ValueError):
            pass
    remove_tree(base/'report')
    for name, value in reports.items():
        (base/name).write_text(json.dumps(value, indent=2))
    for path in base.iterdir():
        if path.is_file() and path.stat().st_size > 1024*1024:
            with path.open('rb+') as output:
                output.seek(-1024*1024, os.SEEK_END)
                tail = output.read()
                output.seek(0)
                output.write(tail)
                output.truncate()
    (base/'finished').write_text(str(time.time()))


def prune_artifacts(cache, keep=5, max_age=7*86400):
    # Only completed runs are eligible, and locks protect concurrent pruning.
    with (cache/'.prune.lock').open('a') as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        completed = []
        for path in cache.glob('run-*'):
            if path.is_symlink() or not path.is_dir():
                continue
            marker = path/'finished'
            if marker.is_file():
                completed.append((marker.stat().st_mtime, path))
        for index, (modified, path) in enumerate(sorted(completed, reverse=True)):
            if index >= keep or time.time()-modified > max_age:
                remove_tree(path)
