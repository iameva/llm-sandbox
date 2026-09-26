# Pin the base image independently of the tools installed below.
# Tool installers intentionally fetch current releases on a fresh build.
# Refresh with:
#   skopeo inspect docker://registry.fedoraproject.org/fedora-minimal:43 | jq -r .Digest
FROM registry.fedoraproject.org/fedora-minimal:43@sha256:27ccd77437f9e11eb6024aa4a2be0c8b3bb6a4f9ed6f8e112581a3d06af175e9

# Create a non-root user
RUN useradd -m -u 1000 appuser

# Install ONLY what you want available inside the sandbox.
#
# The browser libraries below (alsa-lib through pixman for Chromium,
# gtk3 through libXcursor for Firefox) are the real ldd closure of
# Playwright's browser builds. They are not Playwright's own package
# names: those are Debian's, and `playwright install --with-deps` does
# nothing useful on Fedora.
#
# The fonts matter as much as the libraries. Without them a headless
# screenshot renders every symbol glyph as tofu, which reads as a
# rendering bug in the app under test.
RUN microdnf update -y && \
    microdnf install -y \
        zsh \
        ca-certificates \
        coreutils \
        findutils \
        python3 \
        python3-pip \
        curl \
        ripgrep \
        git \
        nvim \
        tree \
        task \
        gcc \
        golang \
	vim \
	bubblewrap \
	tar \
	nodejs \
	caddy \
	sqlite3 \
	jq \
	iproute \
	alsa-lib \
        at-spi2-atk \
        at-spi2-core \
        atk \
        avahi-libs \
        cairo \
        cups-libs \
        dbus-libs \
        expat \
        fontconfig \
        freetype \
        fribidi \
        glib2 \
        graphite2 \
        harfbuzz \
        libX11 \
        libXcomposite \
        libXdamage \
        libXext \
        libXfixes \
        libXi \
        libXrandr \
        libXrender \
        libdatrie \
        libdrm \
        libpng \
        libthai \
        libxcb \
        libxkbcommon \
        mesa-libgbm \
        nspr \
        nss \
        nss-util \
        pango \
        pixman \
        gtk3 \
        cairo-gobject \
        gdk-pixbuf2 \
        libXcursor \
        dejavu-sans-fonts \
        dejavu-sans-mono-fonts \
        dejavu-serif-fonts \
        liberation-sans-fonts \
        liberation-serif-fonts \
        liberation-mono-fonts \
        google-noto-sans-symbols-fonts \
        google-noto-sans-symbols-2-fonts \
        google-noto-color-emoji-fonts \
	openssl \
    && microdnf clean all

# Reaches the symbol fonts that Firefox's own glyph fallback misses.
COPY qemu/fontconfig-symbols.conf /etc/fonts/conf.d/99-symbol-fallback.conf

# Playwright, with its browsers baked in.
#
# The version is exact on purpose. Playwright looks for one browser build
# number, and only that one: 1.62.1 wants firefox-1538, chromium-1234 and
# ffmpeg-1011, which are what the layer below downloads. A caret range
# resolves to a newer Playwright at `npm install` time and then fails with
# "executable doesn't exist". Move both together or not at all.
#
# Roughly 950MB of browsers. Build with
#   --build-arg PLAYWRIGHT_BROWSERS=firefox
# for one of them, about 300MB. Adding webkit needs its own library
# closure first: `playwright install` ends by checking every browser it
# installed against ldd, so the build fails rather than shipping a
# browser that cannot start.
ARG PLAYWRIGHT_VERSION=1.62.1
ARG PLAYWRIGHT_BROWSERS="firefox chromium"

# Shared and read-only, so a project's own `npm install playwright` finds
# these browsers instead of downloading its own copy. A project pinned to
# a different Playwright cannot write here: point PLAYWRIGHT_BROWSERS_PATH
# at a directory under /workspace and install into that.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright

RUN npm install -g "@playwright/test@${PLAYWRIGHT_VERSION}" && \
    playwright install ${PLAYWRIGHT_BROWSERS} && \
    chmod -R a+rX /opt/ms-playwright

# Proves the browsers run here, rather than assuming they do.
COPY --chmod=0755 qemu/browser-smoke.mjs /usr/local/bin/browser-smoke.mjs

RUN microdnf install -y shasum

USER appuser

# Working directory inside the container
WORKDIR /workspace

# Agent installers intentionally fetch current releases. Use a build
# without cache to refresh them. Rust defaults to the current stable release.
# Download scripts before executing so a failed curl cannot look like success.
ARG RUST_TOOLCHAIN=stable

# Install Open AI Codex.
#
# Must run before CODEX_HOME is set below: the installer reads that
# variable and would put the runtime in the directory we are about to
# reserve for credentials.
RUN curl -fsSL https://chatgpt.com/codex/install.sh -o /tmp/install-codex.sh && \
    CODEX_NON_INTERACTIVE=1 sh /tmp/install-codex.sh && \
    rm /tmp/install-codex.sh

# Get Rust
RUN curl -fsSL https://sh.rustup.rs -o /tmp/install-rust.sh && \
    bash /tmp/install-rust.sh -y --default-toolchain "${RUST_TOOLCHAIN}" && \
    rm /tmp/install-rust.sh

# Keep local tooling on PATH (cargo + installed binaries)
ENV PATH="/home/appuser/.cargo/bin:/home/appuser/.local/bin:/usr/local/bin:/usr/bin:/bin"

# Move codex's config and credentials out of its install directory.
#
# ~/.codex is two things at once: CODEX_HOME, where auth.json and
# config.toml live, and the install root, where the installer puts a
# 320MB runtime under packages/standalone. ~/.local/bin/codex is only a
# symlink into that runtime. So mounting the host's credentials over
# ~/.codex — which is what sandbox-run.sh used to do — hid the runtime,
# left the symlink dangling, and the container failed to start with
#   error finding executable "codex" in PATH
#
# Splitting the two roles fixes it at the source. Codex finds its
# package relative to its own executable, not through CODEX_HOME, so the
# runtime stays where the installer put it and only the config directory
# moves. sandbox-run.sh mounts credentials here instead.
#
# Verified with `codex doctor` on 2026-09-07: package, bundled ripgrep
# and resources all still resolve, and state follows CODEX_HOME.
#
# One deliberate side effect: doctor's "update action" drops from
# "standalone installer" to "manual or unknown", because codex detects a
# self-updatable install by checking whether its executable sits under
# CODEX_HOME. Self-update is unwanted here anyway — the base image is
# pinned by digest, and an update would write into the mounted host
# credential directory while the symlink kept pointing at the image copy.
# Rebuild to upgrade.
ENV CODEX_HOME=/home/appuser/.config/codex

# Add rust analyzer
RUN rustup component add rust-src

# install Claude Code
RUN curl -fsSL https://claude.ai/install.sh -o /tmp/install-claude.sh && \
    bash /tmp/install-claude.sh && \
    rm /tmp/install-claude.sh

# Install pi
RUN curl -fsSL https://pi.dev/install.sh -o /tmp/install-pi.sh && \
    sh /tmp/install-pi.sh && \
    rm /tmp/install-pi.sh

# Install oh-my-pi
RUN curl -fsSL https://omp.sh/install -o /tmp/install-omp.sh && \
    sh /tmp/install-omp.sh && \
    rm /tmp/install-omp.sh

# Install OpenCode. Keep its executable available to both the launcher and shells.
RUN curl -fsSL https://opencode.ai/install -o /tmp/install-opencode.sh && \
    bash /tmp/install-opencode.sh --no-modify-path && \
    rm /tmp/install-opencode.sh
ENV PATH="/home/appuser/.opencode/bin:${PATH}"

# Catch missing executables during a build, before installing host launchers.
RUN codex --version && claude --version && \
    pi --version && omp --version && opencode --version

CMD ["zsh"]
