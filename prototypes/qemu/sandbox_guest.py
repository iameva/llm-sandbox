"""Start an ordinary-user interactive shell inside the disposable VM."""
import json
import os
from pathlib import Path
import pwd

user = pwd.getpwuid(1000)
config = json.loads(Path('/mnt/seed/session.json').read_text())
os.setgroups([])
os.setgid(user.pw_gid)
os.setuid(user.pw_uid)
os.chdir('/workspace')
environment = {
    'HOME': user.pw_dir,
    'USER': user.pw_name,
    'LOGNAME': user.pw_name,
    'PATH': f'{user.pw_dir}/.local/bin:{user.pw_dir}/.opencode/bin:'
            f'{user.pw_dir}/.bun/bin:/usr/local/bin:/usr/bin:/bin',
    'CODEX_HOME': f'{user.pw_dir}/.config/codex',
    'CLAUDE_CONFIG_DIR': f'{user.pw_dir}/.claude',
    'TERM': 'xterm-256color',
    'http_proxy': 'http://10.0.2.100:3128',
    'https_proxy': 'http://10.0.2.100:3128',
    'HTTP_PROXY': 'http://10.0.2.100:3128',
    'HTTPS_PROXY': 'http://10.0.2.100:3128',
    'NO_PROXY': 'localhost,127.0.0.1,::1',
    'no_proxy': 'localhost,127.0.0.1,::1',
    'PS1': '[qemu sandbox] \\w \\$ ',
}
print('\nQEMU sandbox ready. /workspace is your writable host directory.\n'
      'Network access requires the allowlisted HTTPS proxy. Exit powers off the VM.\n',
      flush=True)
agent = config['agent']
if agent == 'shell':
    os.execve('/bin/bash', ['bash', '--noprofile', '--norc', '-i'], environment)
os.execvpe(agent, [agent], environment)
