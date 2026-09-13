#!/usr/bin/env python3
"""Interactive rootless QEMU sandbox. No downloads or host changes.

The explicitly selected workspace is writable by the guest. Without --vm-dir, guest disk changes disappear on exit. With --vm-dir,
a private disk copy retains the guest home and credentials.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time

try:
    from .proxy_process import Processes
    from .runtime_support import (bounded_integer, disk_cache, private_directory,
                                  write_size, read_report, finish_artifacts, prune_artifacts, validate_base_image)
except ImportError:
    from proxy_process import Processes
    from runtime_support import (bounded_integer, disk_cache, private_directory,
                                 write_size, read_report, finish_artifacts, prune_artifacts, validate_base_image)


USER_DATA = """#!/bin/sh
exec </dev/null
set -eu
trap 'echo "Guest setup failed"; /sbin/poweroff' EXIT
mkdir -p /workspace /mnt/seed
mount -o ro LABEL=cidata /mnt/seed
modprobe virtiofs
mount -t virtiofs workspace /workspace
python3 - <<'GUEST'
import os, subprocess
interface, = [x for x in os.listdir('/sys/class/net') if x != 'lo']
for command in [
    ['ip', 'link', 'set', interface, 'up'],
    ['ip', 'addr', 'replace', '10.0.2.15/24', 'dev', interface],
    ['ip', 'route', 'replace', 'default', 'via', '10.0.2.2'],
]:
    subprocess.run(command, check=True)
GUEST
systemctl stop serial-getty@ttyS0.service
cp /mnt/seed/sandbox-session.service /etc/systemd/system/
systemctl daemon-reload
systemctl start --no-block sandbox-session.service
trap - EXIT
"""

UNIT = """[Unit]
Description=Disposable sandbox console
After=cloud-final.service
Conflicts=serial-getty@ttyS0.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 /mnt/seed/session.py
ExecStopPost=/usr/sbin/poweroff
StandardInput=tty
StandardOutput=tty
StandardError=tty
TTYPath=/dev/ttyS0
TTYReset=yes
TTYVHangup=yes
"""


def qemu_command(qemu, disk, seed, fs, port, memory_mib=2048, cpus=2):
    relay = shlex.join([sys.executable, str(Path(__file__).with_name('network_relay.py').resolve()),
                        '--tcp', '127.0.0.1', str(port)])
    if ',' in relay:
        raise ValueError('relay path must not contain commas')
    return [
        qemu, '-machine', 'q35,accel=kvm', '-cpu', 'host', '-m', str(memory_mib), '-smp', str(cpus),
        '-object', f'memory-backend-memfd,id=mem,size={memory_mib}M,share=on',
        '-numa', 'node,memdev=mem', '-nodefaults', '-no-user-config',
        '-netdev', 'user,id=n,net=10.0.2.0/24,restrict=on,ipv6=off,'
                   f'guestfwd=tcp:10.0.2.100:3128-cmd:{relay}',
        '-device', 'virtio-net-pci,netdev=n',
        '-display', 'none', '-monitor', 'none',
        '-chardev', 'stdio,id=console,signal=off', '-serial', 'chardev:console',
        '-no-reboot', '-drive', f'file={disk},format=qcow2,if=virtio',
        '-chardev', f'socket,id=fs,path={fs}',
        '-device', 'vhost-user-fs-pci,chardev=fs,tag=workspace',
        '-device', 'virtio-scsi-pci,id=seedbus',
        '-drive', f'file={seed},format=raw,if=none,id=seed,readonly=on',
        '-device', 'scsi-cd,drive=seed,bus=seedbus.0',
    ]


def launch_settings(mounts, assets, environment, command, verify=False):
    result = {'mounts': [], 'assets': [], 'environment': {}, 'command': command, 'verify': verify}
    targets = set()
    for entry in mounts:
        source, separator, target = entry.partition(':')
        destination = Path(target)
        if (not separator or not destination.is_relative_to('/home/fedora')
                or destination == Path('/home/fedora') or '..' in destination.parts):
            raise ValueError('state mount target must be beneath /home/fedora')
        if target in targets:
            raise ValueError('duplicate state mount target')
        targets.add(target)
        path = Path(source).resolve()
        if ':' in str(path) or ',' in str(path):
            raise ValueError('state source path contains an unsupported separator')
        if path.exists() and not path.is_dir():
            raise ValueError('QEMU state mounts must be directories')
        result['mounts'].append({'source': str(path), 'target': target})
    for index, entry in enumerate(assets):
        source, separator, target = entry.partition(':')
        if not separator or not target.startswith('/opt/sandbox/') or '..' in Path(target).parts:
            raise ValueError('asset target must be beneath /opt/sandbox')
        path = Path(source).resolve(strict=True)
        if not path.is_file():
            raise ValueError('backend asset is not a file')
        result['assets'].append({'source': str(path), 'target': target, 'seed': f'asset-{index}'})
    for entry in environment:
        key, separator, value = entry.partition('=')
        if not key.isidentifier():
            raise ValueError('invalid environment variable name')
        result['environment'][key] = value if separator else os.environ.get(key, '')
    return result


def check_helpers(vm, helpers, shutdown_grace=2):
    stopped = []
    for name, child in helpers:
        status = child.poll()
        if status is not None:
            stopped.append(f'{name} (exit {status})')
    if not stopped:
        return
    # QEMU can disconnect virtiofsd immediately before its own process exits.
    # Reap QEMU if it is finishing; otherwise a lost helper remains fatal.
    # No guest-supplied completion marker can extend this bounded grace.
    try:
        vm.wait(timeout=shutdown_grace)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError('required helper exited while QEMU remained running: '
                           + ', '.join(stopped) + '; stopping this VM') from exc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--disk', type=Path, required=True, help='previously verified Fedora qcow2')
    parser.add_argument('--workspace', type=Path, required=True, help='existing directory; guest writes persist here')
    parser.add_argument('--allow-file', type=Path, required=True, help='explicit HTTPS hostname allowlist')
    parser.add_argument('--virtiofsd', default=shutil.which('virtiofsd') or '/usr/libexec/virtiofsd')
    parser.add_argument('--check', action='store_true', help='validate inputs and tools without writing or starting anything')
    parser.add_argument('--agent', choices=['shell', 'claude', 'codex', 'pi', 'omp', 'opencode'], default='shell')
    parser.add_argument('--vm-dir', type=Path,
                        help='dedicated VM storage directory; guest home and logins persist inside its disk')
    parser.add_argument('--mount', action='append', default=[], help='explicit live state directory SOURCE:GUEST_TARGET')
    parser.add_argument('--asset', action='append', default=[], help='backend file SOURCE:GUEST_TARGET')
    parser.add_argument('--env', action='append', default=[], help='guest NAME=value, or inherited NAME')
    parser.add_argument('--batch', action='store_true', help='noninteractive command; console goes to the run log')
    parser.add_argument('--verify', action='store_true', help='check this launch configuration inside the guest')
    parser.add_argument('--verify-agents', action='store_true', help='also check all five installed agent versions')
    parser.add_argument('--command', nargs=argparse.REMAINDER, help='exact guest command and arguments')
    parser.add_argument('--memory-mib', type=bounded_integer(512, 1048576), default=2048)
    parser.add_argument('--cpus', type=bounded_integer(1, 1024), default=2)
    parser.add_argument('--boot-timeout', type=bounded_integer(1, 604800), default=600)
    parser.add_argument('--batch-timeout', type=bounded_integer(0, 604800), default=0,
                        help='seconds after guest readiness; 0 means no command deadline')
    parser.add_argument('--idle-timeout', type=bounded_integer(0, 604800), default=0,
                        help='proxy tunnel inactivity seconds; 0 keeps idle tunnels open')
    parser.add_argument('--cache-dir', type=Path,
                        default=Path(os.environ.get('XDG_CACHE_HOME', str(Path.home()/'.cache')))/'llm-sandbox/qemu')
    parser.add_argument('--keep-artifacts', action='store_true', help='retain bounded diagnostics, never the disposable disk')
    args = parser.parse_args()
    args.verify = args.verify or args.verify_agents
    if os.geteuid() == 0:
        parser.error('run as your normal user, without sudo')
    try:
        cache = disk_cache(args.cache_dir)
        settings = launch_settings(args.mount, args.asset, args.env, args.command, args.verify)
        settings['verify_agents'] = args.verify_agents
        disk = args.disk.resolve(strict=True)
        workspace = args.workspace.resolve(strict=True)
        allow = args.allow_file.resolve(strict=True)
        if not disk.is_file() or not workspace.is_dir() or not allow.is_file():
            raise ValueError('disk and allowlist must be files; workspace must be a directory')
        validate_base_image(disk)
        if disk.is_relative_to(workspace):
            raise ValueError('the base image must be outside the shared workspace')
        if workspace == Path('/') or workspace == Path.home().resolve():
            raise ValueError('select a project directory, not your home or filesystem root')
        vm_dir = args.vm_dir.resolve() if args.vm_dir else None
        if vm_dir and (vm_dir == workspace or vm_dir.is_relative_to(workspace)):
            raise ValueError('VM storage must be outside the shared workspace')
        if vm_dir and (',' in str(vm_dir) or not shutil.which('qemu-img')):
            raise ValueError('persistent VM storage requires qemu-img and a path without commas')
        for mount in settings['mounts']:
            state_source = Path(mount['source'])
            if disk.is_relative_to(state_source) or (vm_dir and vm_dir.is_relative_to(state_source)):
                raise ValueError('state export must not contain VM storage')
        if not allow.read_text().strip():
            raise ValueError('allowlist is empty')
        if any(',' in str(path) for path in (disk, workspace, Path(__file__).resolve())):
            raise ValueError('paths must not contain commas')
        qemu = shutil.which('qemu-system-x86_64')
        maker = next((shutil.which(x) for x in ('genisoimage', 'xorrisofs', 'mkisofs') if shutil.which(x)), None)
        if not qemu or not maker or not shutil.which('qemu-img') or not os.access(args.virtiofsd, os.X_OK):
            raise ValueError('QEMU, qemu-img, virtiofsd and an ISO maker must already be installed')
        if not os.access('/dev/kvm', os.R_OK | os.W_OK):
            raise ValueError('KVM access required')
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    exports_paths = [workspace, *[Path(m['source']) for m in settings['mounts']]]
    if any(cache.is_relative_to(path) for path in exports_paths):
        parser.error('QEMU cache must be outside all guest exports')
    if args.check:
        print(f'Prerequisites available. Writable workspace: {workspace}. No files created.')
        return 0
    if not sys.stdin.isatty() and not args.verify and not args.batch:
        parser.error('an interactive terminal is required')
    base = None
    try:
        private_directory(cache)
        prune_artifacts(cache)
        base = Path(tempfile.mkdtemp(prefix='run-', dir=cache))
        sockets = Path(tempfile.mkdtemp(prefix='qemu-sockets-', dir='/tmp'))
    except (OSError, ValueError) as exc:
        if base is not None:
            base.rmdir()
        parser.error(f'cannot prepare QEMU run storage: {exc}')
    # Keep control files outside the guest export, even for a workspace such
    # as /tmp that could otherwise expose all runs.
    if any(sockets.is_relative_to(path) for path in exports_paths):
        base.rmdir()
        sockets.rmdir()
        parser.error('workspace contains the runtime directory; choose a narrower workspace')
    children = []
    helpers = []
    logs = []
    proxies = None
    vm_lock = None
    success = False
    previous_winch = None
    resized = True
    def on_resize(signum, frame):
        nonlocal resized
        resized = True
    print(f'Writable workspace: {workspace}\nPrivate run artifacts: {base}', flush=True)
    try:
        for mount in settings['mounts']:
            Path(mount['source']).mkdir(mode=0o700, parents=True, exist_ok=True)
        if vm_dir:
            vm_dir.mkdir(mode=0o700, parents=False, exist_ok=True)
            vm_lock = (vm_dir/'lock').open('a')
            try:
                fcntl.flock(vm_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError('this VM directory is already in use')
            private_disk = vm_dir/'disk.qcow2'
            manifest = vm_dir/'vm.json'
            if not private_disk.exists():
                # Exclusive lock and a separate partial name prevent an
                # interrupted conversion from becoming a usable VM disk.
                partial = vm_dir/'creating.qcow2'
                if partial.exists() or manifest.exists():
                    raise RuntimeError('incomplete VM creation; inspect VM directory or choose a new one')
                subprocess.run([shutil.which('qemu-img'), 'convert', '-f', 'qcow2',
                                '-O', 'qcow2', str(disk), str(partial)], check=True, timeout=600)
                manifest.write_text(json.dumps({'base_disk': str(disk)}))
                partial.rename(private_disk)
            elif not manifest.exists() or json.loads(manifest.read_text()).get('base_disk') != str(disk):
                raise RuntimeError('VM directory does not match the selected base image')
            disk = private_disk
        else:
            overlay = base/'overlay.qcow2'
            subprocess.run([shutil.which('qemu-img'), 'create', '-f', 'qcow2',
                            '-F', 'qcow2', '-b', str(disk), str(overlay)],
                           check=True, timeout=30, stdout=subprocess.DEVNULL)
            disk = overlay
        proxies = Processes(base, allow_file=allow, idle_timeout=args.idle_timeout)
        proxies.start('sandbox')
        helpers.append(('HTTPS proxy', proxies.children['sandbox']))
        source = base/'seed'
        source.mkdir(mode=0o700)
        files = {
            'user-data': USER_DATA,
            'meta-data': f'instance-id: {base.name}\nlocal-hostname: sandbox\n',
            'network-config': 'version: 2\nethernets: {}\n',
            'sandbox-session.service': UNIT,
            'session.json': json.dumps({'agent': args.agent, **settings}),
        }
        for name, content in files.items():
            (source/name).write_text(content)
        shutil.copyfile(Path(__file__).with_name('sandbox_guest.py'), source/'session.py')
        shutil.copyfile(Path(__file__).with_name('guest_verify.py'), source/'guest_verify.py')
        for asset in settings['assets']:
            shutil.copyfile(asset['source'], source/asset['seed'])
        seed = base/'seed.iso'
        subprocess.run([maker, '-quiet', '-output', str(seed), '-volid', 'cidata',
                        '-joliet', '-rock', *files, 'session.py', 'guest_verify.py',
                        *[a['seed'] for a in settings['assets']]], cwd=source, check=True, timeout=60)
        fs = sockets/'vhost.sock'
        log = (base/'virtiofsd.log').open('w')
        logs.append(log)
        daemon = subprocess.Popen([
            args.virtiofsd, '--socket-path', str(fs), '--shared-dir', str(workspace),
            '--sandbox=namespace', '--cache=never',
            f'--uid-map=:1000:{os.getuid()}:1:', f'--gid-map=:1000:{os.getgid()}:1:',
        ], stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        children.append(daemon)
        helpers.append(('workspace virtiofsd', daemon))
        deadline = time.monotonic()+15
        while not fs.exists():
            if daemon.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError('virtiofsd failed to start; inspect its log')
            time.sleep(.1)
        command = qemu_command(qemu, disk, seed, fs, proxies.ports['sandbox'], args.memory_mib, args.cpus)
        report_dir = base/'report'
        report_dir.mkdir(mode=0o700)
        exports = [(Path(m['source']), f'state{i}') for i, m in enumerate(settings['mounts'])]
        exports.append((report_dir, 'report'))
        for path, tag in exports:
            socket_path = sockets/f'{tag}.sock'
            state_log = (base/f'{tag}.virtiofsd.log').open('w')
            logs.append(state_log)
            helper = subprocess.Popen([
                args.virtiofsd, '--socket-path', str(socket_path), '--shared-dir', str(path),
                '--sandbox=namespace', '--cache=never',
                f'--uid-map=:1000:{os.getuid()}:1:', f'--gid-map=:1000:{os.getgid()}:1:',
            ], stdin=subprocess.DEVNULL, stdout=state_log, stderr=state_log, start_new_session=True)
            children.append(helper)
            helpers.append((f'{tag} virtiofsd', helper))
            deadline = time.monotonic()+15
            while not socket_path.exists():
                if helper.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError('virtiofsd failed to start: '+tag)
                time.sleep(.1)
            command += ['-chardev', f'socket,id={tag},path={socket_path}',
                        '-device', f'vhost-user-fs-pci,chardev={tag},tag={tag}']
        if not (args.verify or args.batch):
            command[command.index('stdio,id=console,signal=off')] += f',logfile={base}/console.log'
        if args.verify or args.batch:
            command[command.index('stdio,id=console,signal=off')] = f'file,id=console,path={base}/console.log'

        (base/'launch.json').write_text(json.dumps({'workspace': str(workspace), 'argv': command}, indent=2))
        if sys.stdin.isatty() and not (args.batch or args.verify):
            previous_winch = signal.signal(signal.SIGWINCH, on_resize)
            write_size(report_dir, os.get_terminal_size(sys.stdin.fileno()))
        vm = subprocess.Popen(command)
        children.append(vm)
        boot_deadline = time.monotonic()+args.boot_timeout
        command_timeout = args.batch_timeout or (900 if args.verify else 0)
        batch_deadline = None
        while vm.poll() is None:
            if resized and previous_winch is not None:
                resized = False
                write_size(report_dir, os.get_terminal_size(sys.stdin.fileno()))
            if (report_dir/'ready').exists() and batch_deadline is None:
                batch_deadline = time.monotonic()+command_timeout if command_timeout else float('inf')
            if (args.batch or args.verify) and batch_deadline is not None and time.monotonic() > batch_deadline:
                raise TimeoutError(f'guest command exceeded {command_timeout} seconds')
            if not (report_dir/'ready').exists() and time.monotonic() > boot_deadline:
                raise TimeoutError(f'guest did not start within {args.boot_timeout} seconds; inspect console.log')
            check_helpers(vm, helpers)
            time.sleep(.2)
        if vm.returncode:
            return vm.returncode
        try:
            exit_report = read_report(report_dir/'exit.json')
        except (OSError, ValueError) as exc:
            raise RuntimeError('guest stopped without a valid exit report; guest setup may have failed; '
                               'inspect console.log and virtiofsd logs') from exc
        if exit_report.get('error'):
            print('Guest setup failed: '+exit_report['error'], file=sys.stderr)
        if type(exit_report.get('returncode')) is not int:
            raise RuntimeError('guest exit report has no integer return code; inspect console.log')
        success = exit_report['returncode'] == 0
        if args.verify and (report_dir/'verify.json').exists():
            print(json.dumps(read_report(report_dir/'verify.json'), indent=2))
        return exit_report['returncode'] if exit_report['returncode'] >= 0 else 128-exit_report['returncode']
    except (Exception, KeyboardInterrupt) as exc:
        print(f'\nSandbox stopped: {exc}', file=sys.stderr)
        return 2
    finally:
        if previous_winch is not None:
            signal.signal(signal.SIGWINCH, previous_winch)
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
        if proxies:
            proxies.close()
        for log in logs:
            log.close()
        if vm_lock:
            vm_lock.close()
        shutil.rmtree(sockets)
        keep = args.keep_artifacts or not success
        finish_artifacts(base, keep)
        prune_artifacts(cache)
        persistence = 'Guest disk changes persist in '+str(vm_dir) if vm_dir else 'Guest disk changes are discarded'
        artifacts = f'Diagnostics retained at {base}' if keep else 'Temporary run artifacts removed'
        print(f'\n{artifacts}. Workspace edits persist. {persistence}.')


def entrypoint():
    def interrupt(signum, frame):
        raise KeyboardInterrupt(f'signal {signum}')
    signal.signal(signal.SIGTERM, interrupt)
    return main()


if __name__ == '__main__':
    raise SystemExit(entrypoint())
