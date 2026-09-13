#!/usr/bin/env python3
"""Disposable, offline QEMU/virtiofs feasibility prototype. Never run with sudo.
Requires an existing trusted, BIOS-bootable Linux guest disk with a serial
console, login credentials, Python 3 and virtiofs support. No downloads.
The guest disk is opened in snapshot mode; only a fresh temporary share is
exported. Exit 0 means the narrow filesystem/socket checks passed, not that
the eventual sandbox is secure. Exit 1 means failed checks; 2 means setup error.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--disk', required=True, type=Path)
    parser.add_argument('--seed', type=Path, help='NoCloud seed ISO for automatic guest checks')
    parser.add_argument('--format', choices=['qcow2', 'raw'], required=True)
    parser.add_argument('--virtiofsd', default=shutil.which('virtiofsd') or '/usr/libexec/virtiofsd')
    parser.add_argument('--accel', choices=['kvm', 'tcg'], default='kvm')
    args = parser.parse_args()
    if os.geteuid() == 0:
        parser.error('run as the ordinary host user, without sudo')
    qemu = shutil.which('qemu-system-x86_64')
    missing = []
    if not qemu:
        missing.append('qemu-system-x86_64 (Fedora package: qemu-system-x86-core)')
    if not os.access(args.virtiofsd, os.X_OK):
        missing.append(f'virtiofsd at {args.virtiofsd} (Fedora package: virtiofsd; or use --virtiofsd PATH)')
    if missing:
        parser.error('Missing host runtime: ' + '; '.join(missing) +
                     '. No VM or temporary share was created.')
    disk = args.disk.resolve(strict=True)
    if not disk.is_file() or ',' in str(disk):
        parser.error('disk must be a regular file with no comma in its path')
    seed = args.seed.resolve(strict=True) if args.seed else None
    if seed and (not seed.is_file() or ',' in str(seed)):
        parser.error('seed must be a regular ISO file with no comma in its path')
    if args.accel == 'kvm' and not os.access('/dev/kvm', os.R_OK | os.W_OK):
        parser.error('/dev/kvm is not accessible; use --accel tcg for a slow functional test')
    directory = Path(tempfile.mkdtemp(prefix='qemu-fs-'))
    directory.chmod(0o700)
    shared = directory / 'share'
    shared.mkdir(mode=0o700)
    (shared / 'host.txt').write_text('host control\n')
    shutil.copyfile(Path(__file__).with_name('guest_check.py'), shared / 'guest_check.py')
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(shared / 'host.sock'))
    (shared / 'host.sock').chmod(0o600)
    listener.listen(4)
    listener.settimeout(0.2)
    stop = threading.Event()
    received = []

    def receive():
        while not stop.is_set():
            try:
                client, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with client:
                client.settimeout(2)
                try:
                    data = client.recv(100)
                    received.append(data)
                    client.sendall(data)
                except OSError:
                    pass

    thread = threading.Thread(target=receive, daemon=True)
    thread.start()
    processes = []
    result = 2
    try:
        # Prove the host service is alive before interpreting guest failure.
        with socket.socket(socket.AF_UNIX) as control:
            control.settimeout(3)
            control.connect(str(shared / 'host.sock'))
            control.sendall(b'host-control\n')
            assert control.recv(100) == b'host-control\n'
        print('Artifacts:', directory, flush=True)
        fs_socket = directory / 'vhost.sock'
        with (directory / 'virtiofsd.log').open('w') as log:
            daemon = subprocess.Popen([
                args.virtiofsd, '--socket-path', str(fs_socket), '--shared-dir', str(shared),
                '--sandbox=namespace', '--cache=never',
                f'--uid-map=:1000:{os.getuid()}:1:', f'--gid-map=:1000:{os.getgid()}:1:',
            ], stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            processes.append(daemon)
            deadline = time.monotonic() + 10
            while not fs_socket.exists():
                if daemon.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError('virtiofsd did not become ready; inspect virtiofsd.log')
                time.sleep(0.1)
            print('In the guest, log in as root and run:\n'
                  '  mkdir -p /workspace\n'
                  '  mount -t virtiofs workspace /workspace\n'
                  '  setpriv --reuid=1000 --regid=1000 --clear-groups python3 /workspace/guest_check.py\n'
                  '  poweroff\n'
                  'QEMU serial console follows. Ctrl-A X exits QEMU.', flush=True)
            argv = [qemu, '-machine', f'q35,accel={args.accel}', '-m', '2048', '-smp', '2',
                    '-object', 'memory-backend-memfd,id=mem,size=2048M,share=on',
                    '-numa', 'node,memdev=mem', '-nodefaults', '-no-user-config',
                    '-nic', 'none', '-display', 'none', '-serial', 'mon:stdio',
                    '-no-reboot', '-snapshot',
                    '-drive', f'file={disk},format={args.format},if=virtio',
                    '-chardev', f'socket,id=fs,path={fs_socket}',
                    '-device', 'vhost-user-fs-pci,chardev=fs,tag=workspace']
            if seed:
                argv += ['-device', 'virtio-scsi-pci,id=seedbus',
                         '-drive', f'file={seed},format=raw,if=none,id=seed,readonly=on',
                         '-device', 'scsi-cd,drive=seed,bus=seedbus.0']
                print('Seed attached: checks and poweroff are automatic; no login needed.', flush=True)
            # Child stays on this terminal for the serial console. -snapshot
            # prevents commits to the supplied base disk; no real host share.
            vm = subprocess.Popen(argv)
            processes.append(vm)
            rc = vm.wait(timeout=600 if seed else None)
            if rc:
                raise RuntimeError(f'QEMU exited with status {rc}')
        report = json.loads((shared / 'guest-result.json').read_text())
        output = shared / 'guest.txt'
        checks = {
            'guest_file_content': output.read_text() == 'guest control\n',
            'host_file_ownership': (output.stat().st_uid, output.stat().st_gid) == (os.getuid(), os.getgid()),
            'no_external_guest_interface': report['interfaces'] == ['lo'],
            'guest_socket_connect_blocked': report['host_socket']['connected'] is False,
            'host_receiver_saw_no_guest_data': b'guest-probe\n' not in received,
        }
        print(json.dumps(checks, indent=2))
        (directory / 'host-result.json').write_text(json.dumps(checks, indent=2)+'\n')
        result = 0 if all(checks.values()) else 1
    except (Exception, KeyboardInterrupt) as exc:
        print(f'INCONCLUSIVE: {type(exc).__name__}: {exc}', flush=True)
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        stop.set()
        listener.close()
        thread.join(timeout=3)
        print(f'Processes stopped. Temporary artifacts retained at {directory}')
        print('Remove that directory after reviewing the results; no host configuration was changed.')
    return result


if __name__ == '__main__':
    def interrupted(signum, frame):
        raise KeyboardInterrupt(f'signal {signum}')
    signal.signal(signal.SIGTERM, interrupted)
    raise SystemExit(main())
