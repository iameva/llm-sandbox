"""Runs only inside the new build VM, with no host credentials or project."""
import json
import os
from pathlib import Path
import pwd
import subprocess
import traceback

AGENTS = {
    'codex': ('https://chatgpt.com/codex/install.sh', ['sh']),
    'claude': ('https://claude.ai/install.sh', ['bash']),
    'pi': ('https://pi.dev/install.sh', ['sh']),
    'omp': ('https://omp.sh/install', ['sh']),
    'opencode': ('https://opencode.ai/install', ['bash']),
}


def stage(name):
    print(name, flush=True)
    payload = json.dumps({'stage': name})
    subprocess.run([
        'setpriv', '--reuid=1000', '--regid=1000', '--clear-groups', 'python3', '-c',
        'import pathlib,sys; p=pathlib.Path("/workspace/build-status.pending"); '
        'p.write_text(sys.stdin.read()); p.rename("/workspace/build-status.json")',
    ], input=payload, text=True, check=True, timeout=10)


def main():
    proxy = 'http://10.0.2.100:3128'
    env = {**os.environ, 'TERM': 'dumb', 'NO_COLOR': '1', 'http_proxy': proxy, 'https_proxy': proxy,
           'HTTP_PROXY': proxy, 'HTTPS_PROXY': proxy}
    user = pwd.getpwuid(1000)
    # Fixed HTTPS repositories avoid a metalink selecting an HTTP mirror,
    # which the CONNECT-only proxy intentionally does not support.
    repos = Path('/tmp/sandbox-build-repos')
    repos.mkdir()
    repos.joinpath('fedora.repo').write_text(
        '[build-base]\nname=Fedora 44 base\n'
        'baseurl=https://dl.fedoraproject.org/pub/fedora/linux/releases/44/Everything/x86_64/os/\n'
        'enabled=1\ngpgcheck=1\n'
        'gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-fedora-44-x86_64\n'
        '[build-updates]\nname=Fedora 44 updates\n'
        'baseurl=https://dl.fedoraproject.org/pub/fedora/linux/updates/44/Everything/x86_64/\n'
        'enabled=1\ngpgcheck=1\n'
        'gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-fedora-44-x86_64\n')
    stage('Installing Fedora dependencies')
    subprocess.run(['dnf', '-y', '--setopt=reposdir='+str(repos),
                    '--setopt=proxy='+proxy, 'install',
                    'git', 'ripgrep', 'nodejs', 'npm', 'curl', 'tar', 'gzip',
                    'unzip', 'xz', 'findutils', 'which', 'gcc', 'make', 'python3-pip'],
                   env=env, check=True, timeout=1800)
    user_env = {**env, 'HOME': user.pw_dir, 'USER': user.pw_name,
                'LOGNAME': user.pw_name, 'CODEX_NON_INTERACTIVE': '1',
                'PATH': f'{user.pw_dir}/.local/bin:{user.pw_dir}/.opencode/bin:'
                        f'{user.pw_dir}/.bun/bin:/usr/local/bin:/usr/bin:/bin'}
    # Keep build-time credentials/config absent, including installer state
    # inherited from neither the host nor the launcher.
    user_env.pop('CODEX_HOME', None)
    def as_user(command, **kwargs):
        return subprocess.run(['setpriv', '--reuid=1000', '--regid='+str(user.pw_gid),
                               '--clear-groups', *command],
                              cwd=user.pw_dir, env=user_env, check=True, **kwargs)
    versions = {}
    for agent, (url, interpreter) in AGENTS.items():
        stage(f'Downloading {agent} installer')
        script = f'{user.pw_dir}/install-{agent}.sh'
        as_user(['curl', '-q', '-fsSL', '--proto', '=https', '--proto-redir', '=https',
                 '--connect-timeout', '30', '--max-time', '180', '--retry', '2', '--retry-max-time', '180', url, '-o', script], timeout=400)
        arguments = ['--no-modify-path'] if agent == 'opencode' else []
        stage(f'Installing {agent}')
        as_user([*interpreter, script, *arguments], stdin=subprocess.DEVNULL, timeout=1200)
        Path(script).unlink()
        stage(f'Checking {agent} version')
        version = as_user([agent, '--version'], capture_output=True, text=True, timeout=60)
        versions[agent] = version.stdout.strip()
        print(f'{agent}: {versions[agent]}', flush=True)
    stage('All five agents installed')
    Path('/etc/sandbox-agents.json').write_text(json.dumps(versions, indent=2))
    return versions


if __name__ == '__main__':
    report = {}
    try:
        report = {'ok': True, 'versions': main()}
    except Exception as exc:
        traceback.print_exc()
        if isinstance(exc, subprocess.CalledProcessError):
            print('Command output:', exc.stdout or '', exc.stderr or '', flush=True)
        report = {'ok': False, 'error': f'{type(exc).__name__}: {exc}'}
    finally:
        # The host share maps only UID 1000.
        os.setgroups([])
        os.setgid(1000)
        os.setuid(1000)
        pending = Path('/workspace/build-result.pending')
        pending.write_text(json.dumps(report, indent=2))
        pending.rename('/workspace/build-result.json')
