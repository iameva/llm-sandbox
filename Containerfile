# Pinned by digest so a build is reproducible and a compromised or
# retagged upstream cannot silently change the base.
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
    && microdnf clean all

# Reaches the symbol fonts that Firefox's own glyph fallback misses.
COPY fontconfig-symbols.conf /etc/fonts/conf.d/99-symbol-fallback.conf

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
COPY --chmod=0755 browser-smoke.mjs /usr/local/bin/browser-smoke.mjs

USER appuser

# Working directory inside the container
WORKDIR /workspace

# Version pins. Override at build time with --build-arg.
#
# The three agent installers below (codex, claude, aider) are fetched
# unpinned over curl-to-shell, because their installers' version
# arguments could not be verified from inside the sandbox. Each is a
# build-time trust of a third party. Pin them once the correct flag is
# confirmed; that is the remaining supply-chain gap in this image.
ARG RUST_TOOLCHAIN=stable
ARG PI_VERSION=latest

# Install Open AI Codex
RUN curl -fsSL https://chatgpt.com/codex/install.sh | CODEX_NON_INTERACTIVE=1 sh

# Get Rust
RUN curl https://sh.rustup.rs -sSf | bash -s -- -y --default-toolchain "${RUST_TOOLCHAIN}"

# Keep local tooling on PATH (cargo + installed binaries)
ENV PATH="/home/appuser/.cargo/bin:/home/appuser/.local/bin:/usr/local/bin:/usr/bin:/bin"

# Add rust analyzer
RUN rustup component add rust-src

# install Claude Code
RUN curl -fsSL https://claude.ai/install.sh | bash

# Install Aider
RUN curl -LsSf https://aider.chat/install.sh | sh

# Install pi
RUN curl -fsSL https://pi.dev/install.sh | sh

# Install oh-my-pi
RUN curl -fsSL https://omp.sh/install | sh

CMD ["zsh"]
