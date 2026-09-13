"""Guest console entry point; mounts only the host-selected exports."""
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess

user = pwd.getpwuid(1000)
config = json.loads(Path('/mnt/seed/session.json').read_text())
mounts = config.get('mounts', [])
for index, item in enumerate([*mounts, {'target': '/mnt/report'}]):
    target = Path(f'/mnt/state{index}') if index < len(mounts) else Path('/mnt/report')
    target.mkdir(parents=True, exist_ok=True)
    tag = f'state{index}' if index < len(mounts) else 'report'
    subprocess.run(['mount', '-t', 'virtiofs', tag, str(target)], check=True)
for asset in config.get('assets', []):
    target = Path(asset['target'])
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(Path('/mnt/seed')/asset['seed'], target)
# Keep SQLite's WAL and shared-memory files on the guest disk. State exports
# under /home/fedora remain shared, but cannot cover this directory.
sqlite_home = Path('/var/lib/llm-sandbox/codex-sqlite')
sqlite_home.mkdir(mode=0o700, parents=True, exist_ok=True)
sqlite_home.chmod(0o700)
os.chown(sqlite_home, user.pw_uid, user.pw_gid)
os.setgroups([])
os.setgid(user.pw_gid)
os.setuid(user.pw_uid)
for index, item in enumerate(mounts):
    target = Path(item['target'])
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        if target.is_symlink():
            target.unlink()
        else:
            # Preserve baked-in state inside this private VM disk.
            backup = target.with_name(target.name+'.image-state')
            if backup.exists():
                raise RuntimeError('state backup already exists: '+str(backup))
            target.rename(backup)
    target.symlink_to(f'/mnt/state{index}')
for relative in ('.config/codex', '.claude'):
    (Path(user.pw_dir)/relative).mkdir(mode=0o700, parents=True, exist_ok=True)
os.chdir('/workspace')
environment = {
    **config.get('environment', {}),
    'HOME': user.pw_dir,
    'USER': user.pw_name,
    'LOGNAME': user.pw_name,
    'PATH': f'{user.pw_dir}/.local/bin:{user.pw_dir}/.opencode/bin:'
            f'{user.pw_dir}/.bun/bin:/usr/local/bin:/usr/bin:/bin',
    'CODEX_HOME': f'{user.pw_dir}/.config/codex',
    'CODEX_SQLITE_HOME': str(sqlite_home),
    'TERM': 'xterm-256color',
    'http_proxy': 'http://10.0.2.100:3128',
    'https_proxy': 'http://10.0.2.100:3128',
    'HTTP_PROXY': 'http://10.0.2.100:3128',
    'HTTPS_PROXY': 'http://10.0.2.100:3128',
    'NO_PROXY': 'localhost,127.0.0.1,::1',
    'no_proxy': 'localhost,127.0.0.1,::1',
    'PS1': '[qemu sandbox] \\w \\$ ',
}
environment.setdefault('CLAUDE_CONFIG_DIR', f'{user.pw_dir}/.claude')
Path('/mnt/report/ready').write_text('ready')
print('\nQEMU sandbox ready. /workspace is your writable host directory.\n'
      'Network access requires the allowlisted HTTPS proxy. Exit powers off the VM.\n',
      flush=True)
returncode = 2
try:
    if config.get('verify'):
        from guest_verify import verify
        returncode = verify(mounts, sqlite_home)
    else:
        command = config.get('command')
        if not command:
            command = ['bash', '--noprofile', '--norc', '-i'] if config['agent'] == 'shell' else [config['agent']]
        returncode = subprocess.run(command, env=environment).returncode
finally:
    pending = Path('/mnt/report/exit.pending')
    pending.write_text(json.dumps({'returncode': returncode}))
    pending.rename('/mnt/report/exit.json')
raise SystemExit(0)
