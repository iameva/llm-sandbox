#!/usr/bin/env python3
"""Run two QEMU guests against a disposable virtiofs share.
Offline by default; --network-smoke enables restricted local test networking.
No downloads, host configuration changes, or real project mounts.
Only --dns-observation uses sudo, for a bounded packet capture.
Requires the already verified Fedora disk, QEMU, virtiofsd, and an ISO maker.
Each guest uses snapshot mode. Logs and artifacts are retained for review.
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time


def wait_until(condition, processes, seconds):
    deadline = time.monotonic() + seconds
    while not condition():
        if any(p.poll() is not None for p in processes):
            raise RuntimeError('a guest or daemon exited early; inspect logs')
        if time.monotonic() > deadline:
            raise TimeoutError('guest synchronization timed out; inspect logs')
        time.sleep(0.2)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--proxy-process-lifecycle', action='store_true', help='kill/restart real proxy processes; opts in to example.com HTTPS')
    ap.add_argument('--dns-observation', action='store_true', help='35-second sudo capture of tagged DNS probes; enables network-full')
    ap.add_argument('--network-full', action='store_true', help='batch protocol, production-proxy denial, IPv6 and active-stream probes')
    ap.add_argument('--public-probes', action='store_true', help='opt in to public TCP/DNS controls and example.com HTTPS (requires --network-full)')
    ap.add_argument('--network-lifecycle', action='store_true', help='also test proxy-listener failure and stopping one VM')
    ap.add_argument('--network-smoke', action='store_true', help='test restricted user networking with local proxy fixtures')
    ap.add_argument('--disk', type=Path, required=True)
    ap.add_argument('--virtiofsd', default=shutil.which('virtiofsd') or '/usr/libexec/virtiofsd')
    args = ap.parse_args()
    if args.proxy_process_lifecycle:
        args.network_full = True
    if args.dns_observation:
        args.network_full = True
    if args.public_probes and not args.network_full:
        ap.error('--public-probes requires --network-full')
    if args.network_full:
        args.network_lifecycle = True
    if args.network_lifecycle:
        args.network_smoke = True
    if os.geteuid() == 0:
        ap.error('run without sudo')
    qemu = shutil.which('qemu-system-x86_64')
    maker = next((shutil.which(x) for x in ('genisoimage', 'xorrisofs', 'mkisofs') if shutil.which(x)), None)
    if not qemu or not maker or not os.access(args.virtiofsd, os.X_OK):
        ap.error('QEMU, virtiofsd, and an ISO maker must be installed')
    if not os.access('/dev/kvm', os.R_OK | os.W_OK):
        ap.error('KVM access required for this concurrent test')
    disk = args.disk.resolve(strict=True)
    if not disk.is_file() or ',' in str(disk):
        ap.error('use the verified qcow2 disk, with no comma in its path')
    if args.dns_observation:
        from dns_observation import Observation
        try:
            Observation.preflight()
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            ap.error(f'DNS observation unavailable: {exc}')
    base = Path(tempfile.mkdtemp(prefix='qemu-boundary-'))
    share = base/'share'
    share.mkdir(mode=0o700)
    token = secrets.token_hex(32)
    outside = base/'outside.txt'
    outside.write_text(token)
    (share/'absolute-link').symlink_to(outside)
    (share/'relative-link').symlink_to('../outside.txt')
    assert (share/'absolute-link').read_text() == token
    assert (share/'relative-link').read_text() == token
    processes, logs, guests = [], [], []
    receiver = None
    receiver_thread = None
    stop = threading.Event()
    received = []
    result = 2
    observation = Observation(base) if args.dns_observation else None
    production_processes = None
    if args.proxy_process_lifecycle:
        from process_lifecycle import Processes
        production_processes = Processes(base)
    network = None
    if args.network_smoke:
        from network_support import Controls
        network = Controls(base, lifecycle=args.network_lifecycle, broad=args.network_full, public=args.public_probes)

    def spawn(argv, log_path):
        log = log_path.open('w')
        logs.append(log)
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log,
                                stderr=subprocess.STDOUT, start_new_session=True)
        processes.append(proc)
        return proc

    try:
        print(f'Artifacts: {base}; starting two VMs (4 GiB total RAM); network_smoke={args.network_smoke}', flush=True)
        for key in ('a', 'b'):
            seed_dir = base/f'seed-{key}'
            seed_dir.mkdir()
            shutil.copyfile(Path(__file__).with_name('guest_network.py' if network else 'guest_boundary.py'), seed_dir/'check.py')
            if args.network_full:
                shutil.copyfile(Path(__file__).with_name('guest_network_broad.py'), seed_dir/'guest_network_broad.py')
            config = network.configure(key) if network else {'outside_token': token}
            if production_processes:
                production_processes.start(key)
                network.process_ports[key] = production_processes.ports[key]
                config['process_lifecycle'] = True
                shutil.copyfile(Path(__file__).with_name('guest_process_lifecycle.py'), seed_dir/'guest_process_lifecycle.py')
            if observation:
                config['dns_tag'] = observation.tag
                shutil.copyfile(Path(__file__).with_name('dns_observation.py'), seed_dir/'dns_observation.py')
            (seed_dir/'config.json').write_text(json.dumps(config))
            (seed_dir/'meta-data').write_text(f'instance-id: boundary-{key}\nlocal-hostname: boundary-{key}\n')
            (seed_dir/'network-config').write_text('version: 2\nethernets: {}\n')
            (seed_dir/'user-data').write_text('''#!/bin/sh
exec >/dev/ttyS0 2>&1
set -eu
trap 'sync; /sbin/poweroff' EXIT
mkdir -p /workspace /mnt/seed
mount -o ro LABEL=cidata /mnt/seed
modprobe virtiofs
mount -t virtiofs workspace /workspace
python3 /mnt/seed/check.py '''+key+'\n')
            seed = base/f'{key}.iso'
            subprocess.run([maker, '-quiet', '-output', str(seed), '-volid', 'cidata',
                            '-joliet', '-rock', 'user-data', 'meta-data', 'network-config',
                            'check.py', 'config.json', *(['guest_process_lifecycle.py'] if production_processes else []), *(['dns_observation.py'] if observation else []), *(['guest_network_broad.py'] if args.network_full else [])], cwd=seed_dir, check=True, timeout=60)
            fs = base/f'{key}.vhost'
            daemon = spawn([args.virtiofsd, '--socket-path', str(fs), '--shared-dir', str(share),
                            '--sandbox=namespace', '--cache=never',
                            f'--uid-map=:1000:{os.getuid()}:1:', f'--gid-map=:1000:{os.getgid()}:1:'], base/f'{key}.virtiofsd.log')
            wait_until(fs.exists, [daemon], 15)
            vm = spawn([qemu, '-machine', 'q35,accel=kvm', '-m', '2048', '-smp', '2',
                        '-object', 'memory-backend-memfd,id=mem,size=2048M,share=on',
                        '-numa', 'node,memdev=mem', '-nodefaults', '-no-user-config',
                        *(network.argv(key) if network else ['-nic', 'none']), '-display', 'none', '-monitor', 'none', '-serial', 'stdio',
                        '-no-reboot', '-snapshot', '-drive', f'file={disk},format=qcow2,if=virtio',
                        '-chardev', f'socket,id=fs,path={fs}', '-device', 'vhost-user-fs-pci,chardev=fs,tag=workspace',
                        '-device', 'virtio-scsi-pci,id=seedbus',
                        '-drive', f'file={seed},format=raw,if=none,id=seed,readonly=on',
                        '-device', 'scsi-cd,drive=seed,bus=seedbus.0'], base/f'{key}.console.log')
            guests.append(vm)
        wait_until(lambda: all((share/f'{k}.ready').exists() for k in ('a', 'b')), processes, 300)
        if observation:
            print('Observing tagged DNS probes for 35 seconds; host controls bracket guest sends.', flush=True)
            observation.start()
            (share/'dns-start').write_text('ready')
            wait_until(lambda: all((share/f'{k}.dns-sent').exists() for k in ('a', 'b')), processes, 15)
            observation.finish()
        if network:
            print('Both guest TCP listener controls passed; starting proxy and direct-access probes.', flush=True)
            (share/'late.ready').write_text('ready')
            if production_processes:
                wait_until(lambda: all((share/f'{k}.process-ready').exists() for k in ('a', 'b')), processes, 90)
                print('Both production HTTPS streams work; killing proxy A.', flush=True)
                production_processes.kill('a')
                (share/'process-a-killed').write_text('ready')
                wait_until(lambda: all((share/f'{k}.process-stopped').exists() for k in ('a', 'b')), processes, 60)
                production_processes.start('a')
                (share/'process-a-restarted').write_text('ready')
                wait_until(lambda: all((share/f'{k}.process-done').exists() for k in ('a', 'b')), processes, 90)
                (share/'process-complete').write_text('ready')
                print('Production proxy restart probes complete; continuing fixture checks.', flush=True)
            remaining = guests
            if args.network_lifecycle:
                wait_until(lambda: all((share/f'{k}.attempted').exists() for k in ('a', 'b')), processes, 180)
                network.stop_proxy('a')
                print('A proxy listener stopped; both guests remain alive.', flush=True)
                (share/'proxy-a-stopped').write_text('ready')
                wait_until(lambda: all((share/f'{k}.proxy-stop-checked').exists() for k in ('a', 'b')), processes, 180)
                os.killpg(guests[0].pid, signal.SIGTERM)
                try:
                    guests[0].wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(guests[0].pid, signal.SIGKILL)
                    guests[0].wait(timeout=10)
                print('VM A stopped; checking VM B again.', flush=True)
                (share/'vm-a-stopped').write_text('ready')
                remaining = [guests[1]]
            for vm in remaining:
                if vm.wait(timeout=240):
                    raise RuntimeError('QEMU failed; inspect console logs')
            checks, reports = network.evaluate(share)
            if production_processes:
                reports['production_processes'] = {
                    k: json.loads((share/f'{k}.process.json').read_text()) for k in ('a', 'b')}
                for key, report in reports['production_processes'].items():
                    checks.update({f'{key}_process_{name}': value for name, value in report.items()})
            if observation:
                reports['dns_observation'] = observation.result
                checks['guest_dns_absent_from_host_capture'] = observation.result['guest_dns_absent_from_host_capture']
            (base/'result.json').write_text(json.dumps({'checks': checks, 'reports': reports}, indent=2))
            print(json.dumps(checks, indent=2))
            notes = {k: r['notes'] for k, r in reports.get('broad', {}).items() if r['notes']}
            if notes:
                print('Coverage notes: '+json.dumps(notes, indent=2))
            print('Targeted probes only: no claim of complete egress certification. Only --dns-observation checks host packet visibility for tagged IPv4 UDP DNS probes.')
            if not all(checks.values()):
                return 1
            if any('INCONCLUSIVE' in message for report in notes.values() for message in report.values()):
                return 2
            return 0
        print('Both guests have working local sockets. Adding the host socket after boot.', flush=True)
        receiver = socket.socket(socket.AF_UNIX)
        receiver.bind(str(share/'late.sock'))
        (share/'late.sock').chmod(0o600)
        receiver.listen(8)
        receiver.settimeout(0.2)
        def serve():
            while not stop.is_set():
                try:
                    client, _ = receiver.accept()
                except socket.timeout:
                    continue
                except OSError:
                    return
                with client:
                    client.settimeout(3)
                    try:
                        data = client.recv(100)
                        received.append(data.decode())
                        client.sendall(data)
                    except OSError:
                        pass
        receiver_thread = threading.Thread(target=serve, daemon=True)
        receiver_thread.start()
        with socket.socket(socket.AF_UNIX) as control:
            control.settimeout(3)
            control.connect(str(share/'late.sock'))
            control.sendall(b'host-control')
            if control.recv(100) != b'host-control':
                raise RuntimeError('host socket positive control failed')
        (share/'late.ready').write_text('ready')
        for vm in guests:
            if vm.wait(timeout=240):
                raise RuntimeError('QEMU failed; inspect console logs')
        reports = {f'{k}.{who}': json.loads((share/f'{k}.{who}.json').read_text())
                   for k in ('a', 'b') for who in ('user', 'root')}
        checks = {
            'both_guests_offline': all(reports[f'{k}.user']['interfaces'] == ['lo'] for k in ('a', 'b')),
            'late_host_socket_blocked': all(not r['late_host_socket']['connected'] for r in reports.values()),
            'cross_guest_socket_blocked': all(not r['neighbour_socket']['connected'] for r in reports.values()),
            'no_outside_canary_read': all(not v['read'] for r in reports.values() for v in r['escapes'].values()),
            'outside_canary_unchanged': outside.read_text() == token,
            'host_receiver_only_control': received == ['host-control'],
            'guest_receivers_only_controls': all(reports[f'{k}.user']['received'] == ['self-'+k] for k in ('a', 'b')),
        }
        (base/'result.json').write_text(json.dumps({'checks': checks, 'reports': reports}, indent=2))
        print(json.dumps(checks, indent=2))
        result = 0 if all(checks.values()) else 1
    except (Exception, KeyboardInterrupt) as exc:
        print(f'INCONCLUSIVE: {type(exc).__name__}: {exc}', flush=True)
    finally:
        # Stop only owned process groups, guests before daemons, also after
        # timeout. Never sweep processes or containers by a global name.
        for proc in reversed(processes):
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
        if production_processes:
            production_processes.close()
        if observation:
            observation.close()
        if network:
            network.close()
        stop.set()
        if receiver:
            receiver.close()
        if receiver_thread:
            receiver_thread.join(timeout=4)
        for log in logs:
            log.close()
        print(f'Processes stopped; logs and reports retained at {base}')
    return result


if __name__ == '__main__':
    def interrupt(signum, frame):
        raise KeyboardInterrupt(f'signal {signum}')
    signal.signal(signal.SIGTERM, interrupt)
    raise SystemExit(main())
