#!/usr/bin/env python3
"""Build a new agent image without sudo, host installs, project or credentials.

Explicit --allow-downloads enables public HTTPS through the proxy only in
this build VM. The verified input disk is never opened writable.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time

from sandbox import USER_DATA, qemu_command
from process_lifecycle import Processes
from build_support import BuildMonitor, read_report


def build_script():
    script = USER_DATA.split('systemctl stop serial-getty')[0]
    # Keep cloud-init's managed output descriptors. Opening the getty's
    # terminal and then stopping that getty can revoke our terminal access.
    # Installers receive no input and their output is piped to a file below.
    script = script.replace('exec >/dev/ttyS0 2>&1', 'exec </dev/null')
    script += (
        'echo "Starting agent provisioning"\n'
        'python3 -u /mnt/seed/provision_agents.py 2>&1 | '
        'setpriv --reuid=1000 --regid=1000 --clear-groups '
        'tee /workspace/provision.log\n'
        'sync\n/sbin/poweroff\n'
    )
    return script


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--disk', required=True, type=Path)
    parser.add_argument('--parent', type=Path, default=Path.home())
    parser.add_argument('--allow-downloads', action='store_true')
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--virtiofsd', default=shutil.which('virtiofsd') or '/usr/libexec/virtiofsd')
    args = parser.parse_args()
    if os.geteuid() == 0:
        parser.error('run without sudo')
    qemu, image_tool = shutil.which('qemu-system-x86_64'), shutil.which('qemu-img')
    maker = next((shutil.which(x) for x in ('genisoimage', 'xorrisofs', 'mkisofs') if shutil.which(x)), None)
    try:
        disk, parent = args.disk.resolve(strict=True), args.parent.resolve(strict=True)
        if not disk.is_file() or not parent.is_dir() or any(',' in str(p) for p in (disk, parent)):
            raise ValueError('use an existing disk and parent directory without commas')
        missing = []
        if not qemu:
            missing.append('qemu-system-x86_64 (Fedora package: qemu-system-x86-core)')
        if not image_tool:
            missing.append('qemu-img (Fedora package: qemu-img)')
        if not maker:
            missing.append('an ISO maker: genisoimage, xorrisofs, or mkisofs')
        if not os.access(args.virtiofsd, os.X_OK):
            missing.append(f'virtiofsd at {args.virtiofsd} (or use --virtiofsd PATH)')
        if missing:
            raise ValueError('Missing host tools: '+ '; '.join(missing)+'. Nothing created or started.')
        if not os.access('/dev/kvm', os.R_OK | os.W_OK):
            raise ValueError('KVM access required')
        if shutil.disk_usage(parent).free < 12 * 1024**3:
            raise ValueError('at least 12 GiB free space required')
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    if args.check:
        print('Build prerequisites available. No files created.')
        return 0
    if not args.allow_downloads:
        parser.error('building requires --allow-downloads (public HTTPS; no credentials or project)')
    base = Path(tempfile.mkdtemp(prefix='qemu-agents-', dir=parent))
    share = Path(tempfile.mkdtemp(prefix='qemu-build-report-'))
    source = base/'seed'
    source.mkdir(mode=0o700)
    children, logs = [], []
    proxy = None
    print(f'Image and logs: {base}. Temporary report share: {share}. Public HTTPS downloads enabled for this build.', flush=True)
    try:
        # A full conversion avoids a long-term dependency on the input disk.
        image = base/'building.qcow2'
        subprocess.run([image_tool, 'convert', '-f', 'qcow2', '-O', 'qcow2',
                        str(disk), str(image)], check=True, timeout=600)
        proxy = Processes(base, mode='log')
        proxy.start('build')
        script = build_script()
        files = {'user-data': script, 'meta-data': f'instance-id: {base.name}\n',
                 'network-config': 'version: 2\nethernets: {}\n'}
        for name, value in files.items():
            (source/name).write_text(value)
        shutil.copyfile(Path(__file__).with_name('provision_agents.py'), source/'provision_agents.py')
        seed = base/'seed.iso'
        subprocess.run([maker, '-quiet', '-output', str(seed), '-volid', 'cidata',
                        '-joliet', '-rock', *files, 'provision_agents.py'], cwd=source,
                       check=True, timeout=60)
        fs = base/'vhost.sock'
        log = (base/'virtiofsd.log').open('w')
        logs.append(log)
        daemon = subprocess.Popen([args.virtiofsd, '--socket-path', str(fs), '--shared-dir', str(share),
                                   '--sandbox=namespace', '--cache=never',
                                   f'--uid-map=:1000:{os.getuid()}:1:', f'--gid-map=:1000:{os.getgid()}:1:'],
                                  stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        children.append(daemon)
        deadline = time.monotonic()+15
        while not fs.exists():
            if daemon.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError('virtiofsd did not start')
            time.sleep(.1)
        command = qemu_command(qemu, image, seed, fs, proxy.ports['build'])
        command.remove('-snapshot')  # Only the newly created private copy is writable.
        command[command.index('stdio,id=console,signal=off')] = f'file,id=console,path={base}/console.log'
        qlog = (base/'qemu.log').open('w')
        logs.append(qlog)
        vm = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=qlog, stderr=qlog)
        children.append(vm)
        print(f'Progress log: {base}/console.log', flush=True)
        print(f'Installer log: {share}/provision.log', flush=True)
        monitor = BuildMonitor(base, share)
        while vm.poll() is None:
            monitor.poll()
            if daemon.poll() is not None or proxy.children['build'].poll() is not None:
                raise RuntimeError('build helper exited')
            time.sleep(.5)
        report = read_report(share/'build-result.json')
        if report is None:
            raise RuntimeError('VM exited without a build report; inspect provision.log and console.log')
        (base/'build-result.json').write_text(json.dumps(report, indent=2))
        if vm.returncode or not report.get('ok'):
            raise RuntimeError(f'build failed: {report}')
        subprocess.run([image_tool, 'check', '-f', 'qcow2', str(image)], check=True, timeout=120)
        # Hash and prepare the manifest before publishing the final name.
        with image.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        pending_manifest = base/'manifest.pending'
        pending_manifest.write_text(json.dumps({'sha256': digest, 'base_disk': str(disk),
                                                **report}, indent=2))
        image.chmod(0o400)
        image.rename(base/'agents.qcow2')
        image = base/'agents.qcow2'
        try:
            pending_manifest.rename(base/'manifest.json')
        except BaseException:
            image.rename(base/'building.qcow2')
            raise
        print(f'Agent image ready: {image}\nVersions: {report["versions"]}', flush=True)
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        print(f'Build incomplete: {exc}\nInspect {base}; no image is marked ready.', flush=True)
        return 2
    finally:
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
        if proxy:
            proxy.close()
        for name in ('provision.log', 'build-result.json', 'build-status.json'):
            if (share/name).is_file():
                try:
                    shutil.copyfile(share/name, base/name)
                except OSError as exc:
                    print(f'Could not copy {name}: {exc}; original remains at {share}', flush=True)
        for log in logs:
            log.close()


if __name__ == '__main__':
    def interrupt(signum, frame):
        raise KeyboardInterrupt(f'signal {signum}')
    signal.signal(signal.SIGTERM, interrupt)
    raise SystemExit(main())
