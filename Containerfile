FROM registry.fedoraproject.org/fedora-minimal:latest

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
    && microdnf clean all

# Install Open AI Codex
RUN npm install -g @openai/codex

# Create a non-root user
RUN useradd -m -u 1000 appuser
USER appuser

# Working directory inside the container
WORKDIR /workspace

# Get Rust
RUN curl https://sh.rustup.rs -sSf | bash -s -- -y

# Keep local tooling on PATH (cargo + installed binaries)
ENV PATH="/home/appuser/.cargo/bin:/home/appuser/.local/bin:/usr/local/bin:/usr/bin:/bin"

# install Claude Code
RUN curl -fsSL https://claude.ai/install.sh | bash

# Install Aider
RUN curl -LsSf https://aider.chat/install.sh | sh

USER root
# Install pi
RUN npm install -g @mariozechner/pi-coding-agent

USER appuser
# Install oh-my-pi
RUN curl -fsSL https://omp.sh/install | sh

CMD ["zsh"]
