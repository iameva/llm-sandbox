#!/usr/bin/env python3
"""Manage versioned QEMU agent images without root or changing running VMs."""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import shlex
import signal
import subprocess
import sys
import tempfile

try:
    from .runtime_support import disk_cache, private_directory, validate_base_image
except ImportError:
    from runtime_support import disk_cache, private_directory, validate_base_image

AGENTS = {'claude', 'codex', 'pi', 'omp', 'opencode'}
REQUIRED_CHECKS = {'uid_1000', 'codex_sqlite_wal', 'one_default_route',
                   'unlisted_connect_denied', 'direct_public_tcp_blocked',
                   'host_alias_tcp_blocked', 'writable:/workspace',
                   *(name+'_version' for name in AGENTS)}
DEFAULT_CONFIG = Path.home()/'.config/llm-sandbox/qemu.json'


def load_config(path):
    if not path.exists():
        return {'version': 1}
    config = json.loads(path.read_text())
    if not isinstance(config, dict) or config.get('version') != 1:
        raise ValueError('unsupported QEMU image configuration; expected version 1')
    for key in ('source_disk', 'image_store', 'active_image', 'previous_image'):
        if key in config and (not isinstance(config[key], str) or not Path(config[key]).is_absolute()):
            raise ValueError(f'{key} must be an absolute path')
    return config


def atomic_json(path, value):
    fd, temporary = tempfile.mkstemp(prefix='.'+path.name+'-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as output:
            json.dump(value, output, indent=2)
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextlib.contextmanager
def config_lock(path):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path.with_suffix('.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('another image operation is running for this config') from exc
        yield


def run_owned(command, timeout):
    """Give the child launcher time to stop its own VM and helpers on failure."""
    child = subprocess.Popen(command, start_new_session=True)
    try:
        status = child.wait(timeout=timeout)
        if status:
            raise RuntimeError(f'{Path(command[1]).name if len(command) > 1 else command[0]} exited with status {status}')
    finally:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()


def image_tool(*arguments):
    executable = shutil.which('qemu-img')
    if not executable:
        raise ValueError('qemu-img must already be installed')
    subprocess.run([executable, *map(str, arguments)], check=True, timeout=600)


def digest(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def verify_published(image):
    image = image.expanduser().resolve(strict=True)
    validate_base_image(image)
    if image.name != 'agents.qcow2':
        raise ValueError('select a published agents.qcow2; use adopt for a recovered image')
    manifest = json.loads(image.with_name('manifest.json').read_text())
    if not isinstance(manifest, dict):
        raise ValueError('image manifest must be an object')
    versions = manifest.get('versions', {})
    if (manifest.get('ok') is not True or not isinstance(versions, dict) or set(versions) != AGENTS
            or any(not isinstance(value, str) or not value.strip() for value in versions.values())):
        raise ValueError('manifest must record a successful build and all five agent versions')
    if manifest.get('sha256') != digest(image):
        raise ValueError('image does not match its manifest SHA-256 digest')
    image_tool('check', '-f', 'qcow2', image)
    return image, manifest


def probe(image, store):
    """Boot a disposable candidate with no project, credentials or shared state."""
    directory = Path(tempfile.mkdtemp(prefix='probe-', dir=store))
    workspace = directory/'workspace'
    workspace.mkdir(mode=0o700)
    allow = directory/'allow.txt'
    allow.write_text('example.com\n')
    print(f'Candidate boot-check artifacts: {directory}', flush=True)
    run_owned([sys.executable, str(Path(__file__).with_name('sandbox.py')),
               '--disk', str(image), '--workspace', str(workspace), '--allow-file', str(allow),
               '--cache-dir', str(directory/'runs'), '--keep-artifacts', '--verify-agents'], timeout=1800)
    reports = list((directory/'runs').glob('run-*/verify.json'))
    if len(reports) != 1:
        raise RuntimeError('candidate boot check did not produce exactly one report')
    report = json.loads(reports[0].read_text())
    if not isinstance(report, dict):
        raise RuntimeError('candidate boot report must be an object')
    checks, versions = report.get('checks', {}), report.get('versions', {})
    if (not isinstance(checks, dict) or not REQUIRED_CHECKS.issubset(checks) or not isinstance(versions, dict)
            or not all(value is True for value in checks.values()) or set(versions) != AGENTS):
        raise RuntimeError('candidate boot checks did not all pass')
    if any(not isinstance(value, str) or not value.strip() for value in versions.values()):
        raise RuntimeError('candidate did not report all five agent versions')
    return versions


def select_image(config_path, config, candidate, store, tested_versions=None):
    image, manifest = verify_published(candidate)
    versions = probe(image, store) if tested_versions is None else tested_versions
    if versions != manifest['versions']:
        raise RuntimeError('booted agent versions differ from the published manifest')
    # Recheck after boot; the candidate was opened only through an overlay.
    if digest(image) != manifest['sha256']:
        raise RuntimeError('candidate changed during verification')
    previous = config.get('active_image')
    updated = dict(config, active_image=str(image))
    if previous and previous != str(image):
        updated['previous_image'] = previous
    atomic_json(config_path, updated)
    print(f'Active image: {image}\nFuture launches use this image; running VMs keep their existing base.', flush=True)
    return updated


def builder_path():
    installed = Path(__file__).with_name('build_image.py')
    if installed.is_file():
        return installed
    source = Path(__file__).resolve().parents[1]/'prototypes/qemu/build_image.py'
    if not source.is_file():
        raise ValueError('image builder is missing; rerun install.sh from the repository')
    return source


def build_candidate(config, store):
    source = Path(config.get('source_disk', '')).expanduser()
    if not config.get('source_disk') or not source.is_file():
        raise ValueError('configure --source-disk with the verified Fedora 44 cloud image first')
    # The machine-readable result is private and outside every guest export.
    with tempfile.TemporaryDirectory(prefix='qemu-image-result-') as temporary:
        result = Path(temporary)/'result.json'
        run_owned([sys.executable, str(builder_path()), '--disk', str(source),
                   '--parent', str(store), '--allow-downloads', '--result-file', str(result)], timeout=7800)
        return Path(json.loads(result.read_text())['image'])


def adopt(source, store):
    source = source.expanduser().resolve(strict=True)
    if not source.is_file():
        raise ValueError('adoption source must be a qcow2 file')
    directory = Path(tempfile.mkdtemp(prefix='adopted-', dir=store))
    candidate = directory/'candidate.qcow2'
    print(f'Copying recovered image into {directory}; original remains unchanged.', flush=True)
    image_tool('convert', '-f', 'qcow2', '-O', 'qcow2', source, candidate)
    image_tool('check', '-f', 'qcow2', candidate)
    candidate.chmod(0o400)
    versions = probe(candidate, store)
    manifest = {'ok': True, 'versions': versions, 'sha256': digest(candidate),
                'origin': 'adopted', 'source_disk': str(source)}
    published = directory/'agents.qcow2'
    candidate.rename(published)
    atomic_json(directory/'manifest.json', manifest)
    return published, versions


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path,
                        default=Path(os.environ.get('SANDBOX_QEMU_CONFIG', str(DEFAULT_CONFIG))))
    commands = parser.add_subparsers(dest='action', required=True)
    configure = commands.add_parser('configure', help='record the source cloud image and optional image store')
    configure.add_argument('--source-disk', type=Path, required=True)
    configure.add_argument('--store', type=Path)
    commands.add_parser('status', help='show image configuration without changing files')
    commands.add_parser('path', help='print the active image path for launchers')
    commands.add_parser('list', help='list published images and current selections')
    for name in ('build', 'update'):
        command = commands.add_parser(name, help='build a candidate'+(' and activate it after checks' if name == 'update' else ' without selecting it'))
        command.add_argument('--allow-downloads', action='store_true', help='allow public HTTPS downloads in the isolated build VM')
        command.add_argument('--check', action='store_true', help='read-only builder prerequisite check')
    for name in ('activate', 'adopt'):
        commands.add_parser(name).add_argument('image', type=Path)
    commands.add_parser('rollback', help='verify and select the previous image')
    args = parser.parse_args()
    config_path = args.config.expanduser().resolve()
    try:
        if os.geteuid() == 0:
            raise ValueError('run as your normal user, without sudo')
        config = load_config(config_path)
        if args.action == 'status':
            print(json.dumps({'config_path': str(config_path), **config}, indent=2))
            return 0
        if args.action == 'path':
            if not config.get('active_image'):
                raise ValueError('no active image; use the image manager or set SANDBOX_QEMU_DISK')
            image = Path(config['active_image']).resolve(strict=True)
            validate_base_image(image)
            print(image)
            return 0
        if args.action == 'list':
            store = Path(config.get('image_store', str(Path.home()/'.local/share/llm-sandbox/images')))
            images = sorted(str(path) for path in store.glob('*/agents.qcow2') if path.with_name('manifest.json').is_file())
            print(json.dumps({'images': images, 'active': config.get('active_image'), 'previous': config.get('previous_image')}, indent=2))
            return 0
        if args.action in ('build', 'update') and args.check:
            if not config.get('source_disk'):
                raise ValueError('configure the source disk first')
            store = Path(config.get('image_store', str(Path.home()/'.local/share/llm-sandbox/images')))
            parent = store
            while not parent.exists():
                parent = parent.parent
            disk_cache(store)
            run_owned([sys.executable, str(builder_path()), '--disk', config['source_disk'], '--parent', str(parent), '--check'], timeout=30)
            return 0
        if args.action in ('build', 'update') and not args.allow_downloads:
            raise ValueError('build/update requires --allow-downloads; use --check for read-only preflight')
        with config_lock(config_path):
            config = load_config(config_path)
            if args.action == 'configure':
                source = args.source_disk.expanduser().resolve(strict=True)
                if not source.is_file():
                    raise ValueError('source disk must be a verified Fedora 44 cloud qcow2 file')
                store = disk_cache(args.store or Path(config.get('image_store', str(Path.home()/'.local/share/llm-sandbox/images'))))
                updated = dict(config, source_disk=str(source), image_store=str(store))
                atomic_json(config_path, updated)
                print(f'Configured {config_path}. Source and active images were not modified.')
                return 0
            store = disk_cache(Path(config.get('image_store', str(Path.home()/'.local/share/llm-sandbox/images'))))
            private_directory(store)
            config['image_store'] = str(store)
            tested_versions = None
            if args.action in ('build', 'update'):
                candidate = build_candidate(config, store)
                if args.action == 'build':
                    verify_published(candidate)
                    command = shlex.join([sys.executable, __file__, '--config', str(config_path), 'activate', str(candidate)])
                    print(f'Candidate ready: {candidate}\nActivate it with: {command}')
                    return 0
            elif args.action == 'adopt':
                candidate, tested_versions = adopt(args.image, store)
            elif args.action == 'activate':
                candidate = args.image
            else:
                if not config.get('previous_image'):
                    raise ValueError('no previous image is recorded')
                candidate = Path(config['previous_image'])
            select_image(config_path, config, candidate, store, tested_versions)
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, KeyboardInterrupt) as exc:
        print(f'Image operation stopped: {exc}. Use status to inspect the current selection.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    def interrupt(signum, frame):
        raise KeyboardInterrupt(f'signal {signum}')
    signal.signal(signal.SIGTERM, interrupt)
    raise SystemExit(main())
