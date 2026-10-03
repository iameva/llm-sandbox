"""Runs only inside the new build VM, with no host credentials or project."""
from collections import deque
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import traceback

try:
    from .guest_verify import SYMBOL_FONT_RULE, TOOLS_RECORD
except ImportError:
    from guest_verify import SYMBOL_FONT_RULE, TOOLS_RECORD

AGENTS = {
    'codex': ('https://chatgpt.com/codex/install.sh', ['sh']),
    'claude': (None, None),  # Official npm distribution; avoids the stalled bootstrap.
    'pi': (None, None),  # Keep executables outside the shared ~/.pi state.
    'omp': ('https://omp.sh/install', ['sh']),
    'opencode': ('https://opencode.ai/install', ['bash']),
}


# The retired Containerfile's package set. The browser libraries are the
# real ldd closure of Playwright's Chromium (alsa-lib..pixman) and Firefox
# (gtk3..libXcursor) builds; `playwright install --with-deps` only knows
# Debian names. The WebKit set (gstreamer1..vulkan-loader) maps Playwright's
# own Ubuntu 24.04 WebKit list to Fedora names, less the three libraries
# WEBKIT_UBUNTU_DEBS supplies. binutils and zstd unpack those. Without the
# fonts, screenshots draw symbols as boxes.
PACKAGES = [
    'git', 'ripgrep', 'nodejs', 'npm', 'curl', 'tar', 'gzip', 'unzip', 'xz', 'findutils',
    'which', 'gcc', 'make', 'python3-pip', 'strace', 'zsh', 'neovim', 'vim-enhanced', 'tree',
    'task', 'golang', 'bubblewrap', 'caddy', 'sqlite', 'jq', 'iproute', 'openssl',
    'perl-Digest-SHA', 'ca-certificates', 'coreutils',
    'alsa-lib', 'at-spi2-atk', 'at-spi2-core', 'atk', 'avahi-libs', 'cairo', 'cups-libs',
    'dbus-libs', 'expat', 'fontconfig', 'freetype', 'fribidi', 'glib2', 'graphite2', 'harfbuzz',
    'libX11', 'libXcomposite', 'libXdamage', 'libXext', 'libXfixes', 'libXi', 'libXrandr',
    'libXrender', 'libdatrie', 'libdrm', 'libpng', 'libthai', 'libxcb', 'libxkbcommon',
    'mesa-libgbm', 'nspr', 'nss', 'nss-util', 'pango', 'pixman',
    'gtk3', 'cairo-gobject', 'gdk-pixbuf2', 'libXcursor',
    'gstreamer1', 'gstreamer1-plugins-base', 'gstreamer1-plugins-good',
    'gstreamer1-plugins-bad-free', 'gstreamer1-plugin-libav', 'gtk4', 'libatomic', 'enchant2',
    'libepoxy', 'libevent', 'flite', 'libglvnd-gles', 'harfbuzz-icu', 'hyphen', 'lcms2',
    'libmanette', 'opus', 'libsecret', 'libvpx', 'libwayland-client', 'libwayland-egl',
    'libwayland-server', 'libwebp', 'woff2', 'libxml2', 'libxslt', 'libavif', 'vulkan-loader',
    'binutils', 'zstd',
    'dejavu-sans-fonts', 'dejavu-sans-mono-fonts', 'dejavu-serif-fonts',
    'liberation-sans-fonts', 'liberation-serif-fonts', 'liberation-mono-fonts',
    'google-noto-sans-symbols-fonts', 'google-noto-sans-symbols-2-fonts',
    'google-noto-color-emoji-fonts',
]

# Exact on purpose: each Playwright release looks for one browser build
# number and fails with "executable doesn't exist" for any other. Move the
# version and a rebuild together.
PLAYWRIGHT_VERSION = '1.63.0'
PLAYWRIGHT_BROWSERS = ['firefox', 'chromium', 'webkit']
PLAYWRIGHT_BROWSERS_PATH = '/opt/ms-playwright'

# Fedora gets Playwright's Ubuntu 24.04 WebKit build. It links ICU 74 and
# libjpeg.so.8; Fedora 44 ships ICU 77 and libjpeg.so.62, and ICU versions
# every symbol, so no symlink bridges them. The Ubuntu copies go in WebKit's
# own sys/lib, which its launcher already searches, and stay out of the
# system library path. They need only libc and libstdc++.
#
# Playwright also refuses to launch WebKit unless `ldconfig -p` lists
# libx264.so, which Fedora does not package. That check is the only reason
# for libx264: video recording uses libvpx, and WebKit reaches x264 only
# through a GStreamer plugin that is not installed. It goes in its own
# directory on the system path.
#
# Hashes come from the noble and noble-updates package indexes, fetched
# over HTTPS on 2026-09-26. A Playwright bump that moves off the Ubuntu
# 24.04 build needs new pins.
UBUNTU_POOL = 'https://archive.ubuntu.com/ubuntu/pool/'
WEBKIT_UBUNTU_DEBS = {
    'main/i/icu/libicu74_74.2-1ubuntu3.1_amd64.deb':
        'c9a70989678660eed9a1e904c74fa043da8bec8e2036856fc16e31ced79b04f8',
    'main/libj/libjpeg-turbo/libjpeg-turbo8_2.1.5-2ubuntu2_amd64.deb':
        'f68b5b23bc8a1688fb787d2aed7e2cdf895a73022f6a5025e183162dac4500b2',
}
WEBKIT_UBUNTU_LIBRARIES = ['libicudata.so.74', 'libicui18n.so.74', 'libicuuc.so.74', 'libjpeg.so.8']
X264_DEB = ('universe/x/x264/libx264-164_0.164.3108+git31e19f9-1_amd64.deb',
            '7a410be6797c32357a2b7f0cc68ee14ae0e3b652bb17d16cdc941e4d830c7842')
X264_LIBRARY = 'libx264.so.164'
X264_DIRECTORY = '/usr/local/lib/playwright-x264'
X264_LD_CONF = '/etc/ld.so.conf.d/playwright-x264.conf'

# The build disk is resized before boot; cloud-init grows the root
# filesystem. Fail early rather than halfway through the toolchain.
MINIMUM_ROOT_BYTES = 25 * 1024**3


def claude_install_command(home, proxy):
    return npm_install_command(home, proxy, '@anthropic-ai/claude-code')


def npm_install_command(home, proxy, package):
    return [
        'npm', 'install', '--global', '--prefix', str(Path(home)/'.local'),
        '--registry=https://registry.npmjs.org',
        '--proxy='+proxy, '--https-proxy='+proxy,
        '--fetch-timeout=60000', '--fetch-retries=2',
        '--fetch-retry-mintimeout=1000', '--fetch-retry-maxtimeout=10000',
        '--include=optional', '--no-audit', '--no-fund', '--loglevel=http',
        package,
    ]


def pi_install_command(home, proxy):
    return [*npm_install_command(home, proxy, '@earendil-works/pi-coding-agent'),
            '--ignore-scripts']


def omp_asset(release):
    assets = [asset for asset in release.get('assets', []) if asset.get('name') == 'omp-linux-x64']
    if len(assets) != 1:
        raise RuntimeError('OMP release does not have exactly one Linux x64 binary')
    asset = assets[0]
    if not asset.get('browser_download_url', '').startswith(
            'https://github.com/can1357/oh-my-pi/releases/download/'):
        raise RuntimeError('unexpected OMP release URL')
    if type(asset.get('size')) is not int or asset['size'] <= 0:
        raise RuntimeError('OMP release has invalid binary size')
    return asset


def install_omp(as_user, home):
    stage('Resolving OMP release')
    curl = ['curl', '-q', '--fail', '--show-error', '--location',
            '--proto', '=https', '--proto-redir', '=https', '--connect-timeout', '20']
    response = as_user([*curl, '--silent', '--max-time', '60',
                       'https://api.github.com/repos/can1357/oh-my-pi/releases/latest'],
                      capture_output=True, text=True, timeout=70)
    release = json.loads(response.stdout)
    asset = omp_asset(release)
    destination = Path(home)/'.local/bin/omp'
    partial = destination.with_name('omp.partial')
    as_user(['mkdir', '-p', str(destination.parent)], timeout=10)
    stage(f'Downloading OMP {release.get("tag_name")} ({asset["size"]} bytes; five-minute limit)')
    as_user([*curl, '--no-progress-meter', '--max-time', '300',
             '--speed-limit', '1024', '--speed-time', '30',
             '--write-out', 'OMP download: %{size_download} bytes in %{time_total} seconds\n',
             asset['browser_download_url'], '--output', str(partial)], timeout=310)
    if partial.stat().st_size != asset['size']:
        raise RuntimeError('OMP download size differs from release metadata')
    digest = asset.get('digest')
    if digest:
        with partial.open('rb') as stream:
            actual = 'sha256:'+hashlib.file_digest(stream, 'sha256').hexdigest()
        if actual != digest:
            raise RuntimeError('OMP release digest mismatch')
    as_user(['chmod', '755', str(partial)], timeout=10)
    as_user(['mv', str(partial), str(destination)], timeout=10)
    print('OMP download complete; starting separate version check', flush=True)


def install_playwright(env, proxy):
    stage(f'Installing Playwright {PLAYWRIGHT_VERSION}')
    subprocess.run(['npm', 'install', '--global', '--registry=https://registry.npmjs.org',
                    '--proxy='+proxy, '--https-proxy='+proxy, '--no-audit', '--no-fund',
                    f'@playwright/test@{PLAYWRIGHT_VERSION}'],
                   env=env, stdin=subprocess.DEVNULL, check=True, timeout=600)
    stage('Downloading Playwright browsers')
    # Ends with an ldd check of each browser, but a missing library only
    # prints a warning; the browser smoke test in guest_verify.py is the gate.
    prefix = subprocess.run(['npm', 'prefix', '--global'], env=env, capture_output=True,
                            text=True, check=True, timeout=60).stdout.strip()
    subprocess.run([f'{prefix}/bin/playwright', 'install', *PLAYWRIGHT_BROWSERS],
                   env={**env, 'PLAYWRIGHT_BROWSERS_PATH': PLAYWRIGHT_BROWSERS_PATH},
                   stdin=subprocess.DEVNULL, check=True, timeout=1800)
    install_webkit_ubuntu_libraries(env)
    subprocess.run(['chmod', '-R', 'a+rX', PLAYWRIGHT_BROWSERS_PATH], check=True, timeout=120)


def fetch_deb(pool_path, digest, env, directory):
    deb = Path(directory)/Path(pool_path).name
    subprocess.run(['curl', '-q', '--fail', '--silent', '--show-error', '--location',
                    '--proto', '=https', '--proto-redir', '=https', '--connect-timeout', '20',
                    '--max-time', '300', '--retry', '2', UBUNTU_POOL+pool_path, '--output', str(deb)],
                   env=env, stdin=subprocess.DEVNULL, check=True, timeout=700)
    with deb.open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != digest:
            raise RuntimeError(f'{deb.name} does not match its pinned digest')
    return deb


def deb_libraries(deb, destination):
    """Unpack a .deb's data archive; return its x86_64 library directory."""
    members = subprocess.run(['ar', 't', str(deb)], capture_output=True, text=True,
                             check=True, timeout=60).stdout.split()
    data = [member for member in members if member.startswith('data.tar')]
    if len(data) != 1:
        raise RuntimeError(f'{Path(deb).name} does not have exactly one data archive')
    destination = Path(destination)
    (destination/'root').mkdir(parents=True)
    subprocess.run(['ar', 'x', str(Path(deb).resolve()), data[0]], cwd=destination,
                   check=True, timeout=60)
    # tar recognises the compression from the file's first bytes.
    subprocess.run(['tar', '-xf', data[0], '-C', 'root'], cwd=destination, check=True,
                   timeout=120)
    return destination/'root/usr/lib/x86_64-linux-gnu'


def install_webkit_ubuntu_libraries(env, browsers_path=PLAYWRIGHT_BROWSERS_PATH,
                                    x264_directory=X264_DIRECTORY, ld_conf=X264_LD_CONF,
                                    ldconfig=True):
    stage('Installing Ubuntu libraries for WebKit')
    targets = sorted(Path(browsers_path).glob('webkit-*/minibrowser-*/sys/lib'))
    if len(targets) != 2:
        raise RuntimeError(f'expected the gtk and wpe WebKit sys/lib directories, found {targets}')
    work = Path(browsers_path)/'.ubuntu-debs'
    try:
        work.mkdir()
        found = {}
        for pool_path, digest in WEBKIT_UBUNTU_DEBS.items():
            libraries = deb_libraries(fetch_deb(pool_path, digest, env, work), work/Path(pool_path).stem)
            found.update({name: libraries/name for name in WEBKIT_UBUNTU_LIBRARIES
                          if (libraries/name).exists()})
        if sorted(found) != sorted(WEBKIT_UBUNTU_LIBRARIES):
            raise RuntimeError(f'Ubuntu packages lack {set(WEBKIT_UBUNTU_LIBRARIES) - set(found)}')
        for target in targets:
            for name, source in found.items():
                # Copy the real file under its soname; the launcher needs nothing else.
                shutil.copyfile(source, target/name)
        libraries = deb_libraries(fetch_deb(*X264_DEB, env, work), work/'x264')
        Path(x264_directory).mkdir(parents=True, exist_ok=True)
        shutil.copyfile(libraries/X264_LIBRARY, Path(x264_directory)/X264_LIBRARY)
        Path(ld_conf).write_text(x264_directory+'\n')
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if ldconfig:
        subprocess.run(['ldconfig'], check=True, timeout=120)
        listed = subprocess.run(['ldconfig', '-p'], capture_output=True, text=True,
                                check=True, timeout=60).stdout
        if X264_LIBRARY not in listed:
            raise RuntimeError('ldconfig does not list libx264; Playwright will refuse WebKit')


def install_rust(as_user, home):
    stage('Downloading rustup')
    script = f'{home}/install-rust.sh'
    as_user(['curl', '-q', '-fsSL', '--proto', '=https', '--proto-redir', '=https',
             '--connect-timeout', '30', '--max-time', '180', '--retry', '2',
             'https://sh.rustup.rs', '-o', script], timeout=400)
    stage('Installing Rust stable')
    # The guest session puts ~/.cargo/bin on PATH; no shell profile edits.
    as_user(['sh', script, '-y', '--no-modify-path', '--default-toolchain', 'stable'],
            stdin=subprocess.DEVNULL, timeout=1800)
    Path(script).unlink()
    as_user(['rustup', 'component', 'add', 'rust-src'], stdin=subprocess.DEVNULL, timeout=600)


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
    # protocol=https keeps the metalink from selecting an HTTP mirror, which
    # the CONNECT-only proxy does not support. A metalink, not one fixed
    # baseurl, lets dnf move to the next mirror when one serves a repomd.xml
    # that names missing files (seen on dl.fedoraproject.org 2026-10-02).
    # skip_if_unavailable=0 fails the build at the broken repo instead of
    # going on without it and failing later on hundreds of missing deps.
    repos = Path('/tmp/sandbox-build-repos')
    repos.mkdir()
    repos.joinpath('fedora.repo').write_text(
        '[build-base]\nname=Fedora 44 base\n'
        'metalink=https://mirrors.fedoraproject.org/metalink?repo=fedora-44&arch=x86_64&protocol=https\n'
        'enabled=1\ngpgcheck=1\nskip_if_unavailable=0\n'
        'gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-fedora-44-x86_64\n'
        '[build-updates]\nname=Fedora 44 updates\n'
        'metalink=https://mirrors.fedoraproject.org/metalink?repo=updates-released-f44&arch=x86_64&protocol=https\n'
        'enabled=1\ngpgcheck=1\nskip_if_unavailable=0\n'
        'gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-fedora-44-x86_64\n')
    root = os.statvfs('/')
    if root.f_blocks * root.f_frsize < MINIMUM_ROOT_BYTES:
        raise RuntimeError('root filesystem did not grow to the resized disk; inspect console.log for growpart')
    stage('Installing Fedora dependencies')
    subprocess.run(['dnf', '-y', '--setopt=reposdir='+str(repos),
                    '--setopt=proxy='+proxy, 'install', *PACKAGES],
                   env=env, check=True, timeout=3600)
    # Firefox's glyph fallback never reaches the Noto symbol fonts without this.
    shutil.copyfile('/mnt/seed/fontconfig-symbols.conf', SYMBOL_FONT_RULE)
    shutil.copyfile('/mnt/seed/browser-smoke.mjs', '/usr/local/bin/browser-smoke.mjs')
    os.chmod('/usr/local/bin/browser-smoke.mjs', 0o755)
    install_playwright(env, proxy)
    user_env = {**env, 'HOME': user.pw_dir, 'USER': user.pw_name,
                'LOGNAME': user.pw_name, 'CODEX_NON_INTERACTIVE': '1',
                'PLAYWRIGHT_BROWSERS_PATH': PLAYWRIGHT_BROWSERS_PATH,
                'PATH': f'{user.pw_dir}/.cargo/bin:{user.pw_dir}/.local/bin:{user.pw_dir}/.opencode/bin:'
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
        if agent == 'omp':
            install_omp(as_user, user.pw_dir)
        elif agent == 'claude':
            stage('Installing claude from npm')
            as_user(claude_install_command(user.pw_dir, proxy),
                    stdin=subprocess.DEVNULL, timeout=600)
        elif agent == 'pi':
            stage('Installing pi from npm outside shared state')
            as_user(pi_install_command(user.pw_dir, proxy),
                    stdin=subprocess.DEVNULL, timeout=600)
        else:
            stage(f'Downloading {agent} installer')
            script = f'{user.pw_dir}/install-{agent}.sh'
            as_user(['curl', '-q', '-fsSL', '--proto', '=https', '--proto-redir', '=https',
                     '--connect-timeout', '30', '--max-time', '180', '--retry', '2', '--retry-max-time', '180', url, '-o', script], timeout=400)
            arguments = ['--no-modify-path'] if agent == 'opencode' else []
            stage(f'Installing {agent}')
            as_user([*interpreter, script, *arguments], stdin=subprocess.DEVNULL, timeout=1200)
            Path(script).unlink()
        stage(f'Checking {agent} version')
        try:
            version = as_user([agent, '--version'], capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            if agent == 'omp':
                stage('Diagnosing OMP startup timeout')
                print(Path('/proc/cpuinfo').read_text().split('\n\n')[0], flush=True)
                trace = Path(user.pw_dir)/'omp-startup.strace'
                try:
                    as_user(['timeout', '--kill-after=5', '15', 'strace', '-f', '-tt',
                             '-o', str(trace), 'omp', '--version'],
                            stdin=subprocess.DEVNULL, timeout=25)
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as diagnostic:
                    print(f'Diagnostic ended: {diagnostic}', flush=True)
                if trace.exists():
                    with trace.open(errors='replace') as stream:
                        print('OMP startup trace (last 80 lines):\n'+''.join(deque(stream, maxlen=80)), flush=True)
            raise
        versions[agent] = version.stdout.strip()
        print(f'{agent}: {versions[agent]}', flush=True)
    stage('All five agents installed')
    Path('/etc/sandbox-agents.json').write_text(json.dumps(versions, indent=2))
    install_rust(as_user, user.pw_dir)
    stage('Checking tool versions')
    checks, tools = tool_checks(as_user)
    failed = sorted(name for name, passed in checks.items() if not passed)
    if failed:
        raise RuntimeError('tools missing after installation: '+', '.join(failed))
    for name, version in tools.items():
        print(f'{name}: {version}', flush=True)
    TOOLS_RECORD.write_text(json.dumps(tools, indent=2))
    return versions, tools


def tool_checks(as_user):
    """Run the boot check's own tool probes as the session user."""
    result = as_user(['python3', '-c', 'import json, os, sys; sys.path.insert(0, "/mnt/seed"); '
                      'from guest_verify import tool_versions; '
                      'print(json.dumps(tool_versions(dict(os.environ))))'],
                     stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=600)
    # Failed probes print diagnostics first; the report is the last line.
    *diagnostics, report = result.stdout.splitlines()
    print('\n'.join(diagnostics), flush=True)
    return json.loads(report)


if __name__ == '__main__':
    report = {}
    try:
        versions, tools = main()
        report = {'ok': True, 'versions': versions, 'tools': tools}
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
