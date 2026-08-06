# Pinned by digest so a build is reproducible and a compromised or
# retagged upstream cannot silently change the base.
# Refresh with:
#   skopeo inspect docker://registry.fedoraproject.org/fedora-minimal:43 | jq -r .Digest
FROM registry.fedoraproject.org/fedora-minimal:43@sha256:ff75598624f2c7e7bfdf977e0ebd509fa25d4da7ee9a95b8b5470df6db6240e1

# Create a non-root user
RUN useradd -m -u 1000 appuser

# Install ONLY what you want available inside the sandbox
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
    && microdnf clean all

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

USER root
# Install pi
RUN npm install -g "@mariozechner/pi-coding-agent@${PI_VERSION}"

USER appuser
# Install oh-my-pi
RUN curl -fsSL https://omp.sh/install | sh

CMD ["zsh"]
