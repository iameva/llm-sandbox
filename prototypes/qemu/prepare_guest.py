#!/usr/bin/env python3
"""Prepare a verified Fedora 44 cloud disk and offline test seed, without sudo.
Downloads about a gigabyte into a new private directory. Never installs host
packages, mounts disks, changes firewall/SELinux, or starts a VM. Prints the
separate boot command. Existing files and your GPG keyring are not modified.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

IMAGE = 'Fedora-Cloud-Base-Generic-44-1.7.x86_64.qcow2'
CHECKSUM = 'Fedora-Cloud-44-1.7-x86_64-CHECKSUM'
BASE = 'https://download.fedoraproject.org/pub/fedora/linux/releases/44/Cloud/x86_64/images/'
FINGERPRINT = '36F612DCF27F7D1A48A835E4DBFCF71C6D9F90A6'
# Fedora Cloud download page, x86_64 non-UKI image, checked 2026-09-12.
DIGEST = '28680fe5b371a5a82ebf43a31926e086a168e59949d03969c5093e7071f90b7f'
USER_DATA = '''#!/bin/sh
# Runs INSIDE the disposable guest only. No networking or package installs.
exec >/dev/ttyS0 2>&1
set -eu
trap 'echo "Guest test finished; powering off"; sync; /sbin/poweroff' EXIT
command -v python3
command -v setpriv
modprobe virtiofs
mkdir -p /workspace
mount -t virtiofs workspace /workspace
setpriv --reuid=1000 --regid=1000 --clear-groups python3 /workspace/guest_check.py
'''


def execute(argv, *, timeout=120, **kwargs):
    return subprocess.run(argv, check=True, timeout=timeout, **kwargs)


def download(url, path):
    # HTTPS also enforced across Fedora mirror redirects. Never read curlrc.
    execute(['curl', '-q', '--fail', '--location', '--proto', '=https',
             '--proto-redir', '=https', '--connect-timeout', '20', '--max-time',
             '1800', '--retry', '2', '--output', str(path), url], timeout=5500)


def signed_digest(text):
    matches = re.findall(r'^SHA256 \('+re.escape(IMAGE)+r'\) = ([0-9a-f]{64})$', text, re.M)
    if len(matches) != 1 or matches[0] != DIGEST:
        raise ValueError('signed checksum does not match the pinned Fedora image digest')
    return matches[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--parent', type=Path, default=Path.home(),
                        help='existing directory for a new private output directory (default: home)')
    parser.add_argument('--check', action='store_true', help='check prerequisites only; no downloads or writes')
    args = parser.parse_args()
    if os.geteuid() == 0:
        parser.error('run as your ordinary user, without sudo')
    maker = next((shutil.which(x) for x in ('genisoimage', 'xorrisofs', 'mkisofs') if shutil.which(x)), None)
    missing = [x for x in ('curl', 'gpgv') if not shutil.which(x)]
    if not maker:
        missing.append('genisoimage, xorrisofs, or mkisofs (one ISO creation tool)')
    if missing:
        print('Missing host tools: '+', '.join(missing)+'. Nothing installed or downloaded.', file=sys.stderr)
        return 2
    parent = args.parent.resolve(strict=True)
    if not parent.is_dir() or ',' in str(parent):
        parser.error('parent must be an existing directory without a comma in its path')
    if shutil.disk_usage(parent).free < 3 * 1024**3:
        parser.error('at least 3 GiB free space is required')
    if args.check:
        print('Preparation prerequisites available. No files created.')
        return 0
    output = Path(tempfile.mkdtemp(prefix='qemu-guest-', dir=parent))
    output.chmod(0o700)
    print(f'Writing only inside {output}', flush=True)
    try:
        keyring = output / 'fedora.gpg'
        checksum = output / CHECKSUM
        download('https://fedoraproject.org/fedora.gpg', keyring)
        download(BASE+CHECKSUM, checksum)
        home = output / 'verification'
        home.mkdir(mode=0o700)
        verified = output / 'verified-checksums.txt'
        status = execute(['gpgv', '--homedir', str(home), '--keyring', str(keyring),
                          '--status-fd', '1', '--output', str(verified), str(checksum)],
                         capture_output=True, text=True).stdout
        valid = [line.split() for line in status.splitlines() if line.startswith('[GNUPG:] VALIDSIG ')]
        if not any(line[2] == FINGERPRINT or line[-1] == FINGERPRINT for line in valid):
            raise ValueError('checksum not signed by the pinned Fedora 44 signing key')
        expected = signed_digest(verified.read_text())
        partial = output / (IMAGE+'.partial')
        download(BASE+IMAGE, partial)
        with partial.open('rb') as stream:
            actual = hashlib.file_digest(stream, 'sha256').hexdigest()
        if actual != expected:
            raise ValueError('image checksum mismatch; image will not be marked ready')
        disk = output / IMAGE
        partial.rename(disk)
        disk.chmod(0o400)
        seed_source = output / 'seed-source'
        seed_source.mkdir(mode=0o700)
        (seed_source / 'user-data').write_text(USER_DATA)
        (seed_source / 'meta-data').write_text('instance-id: qemu-fs-prototype\nlocal-hostname: qemu-fs-prototype\n')
        (seed_source / 'network-config').write_text('version: 2\nethernets: {}\n')
        seed = output / 'seed.iso'
        execute([maker, '-quiet', '-output', str(seed), '-volid', 'cidata', '-joliet', '-rock',
                 'user-data', 'meta-data', 'network-config'], cwd=seed_source)
        seed.chmod(0o400)
        launch = [sys.executable, str(Path(__file__).with_name('run.py').resolve()),
                  '--disk', str(disk), '--format', 'qcow2', '--seed', str(seed)]
        (output / 'manifest.json').write_text(json.dumps({
            'image_url': BASE+IMAGE, 'sha256': expected, 'signing_key': FINGERPRINT,
            'launch': launch, 'guest_network': 'none',
        }, indent=2)+'\n')
        print('Verified image and seed ready. No VM has been started.\nRun separately:\n'+shlex.join(launch))
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        print(f'Preparation failed: {type(exc).__name__}: {exc}\nPartial artifacts retained at {output}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
