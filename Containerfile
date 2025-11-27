FROM registry.fedoraproject.org/fedora-minimal:latest

# 1. Install ONLY what you want available inside the sandbox
RUN microdnf update -y && \
    microdnf install -y \
        bash \
        coreutils \
        findutils \
        ca-certificates \
        python3 \
        python3-pip \
	curl \
	ripgrep \
	git \
	nvim \
    && microdnf clean all

# 2. Create a non-root user
RUN useradd -m -u 1000 appuser
USER appuser

# 3. Working directory inside the container
WORKDIR /workspace

# 4. Get Rust
RUN curl https://sh.rustup.rs -sSf | bash -s -- -y

# 6. Get Claude Code
RUN curl -fsSL https://claude.ai/install.sh | bash

# 5. Copy Codex
COPY codex /home/appuser/.local/bin/codex

# 7. Restrict PATH a bit
ENV PATH="/home/appuser/.local/bin:/usr/local/bin:/usr/bin:/bin"

CMD ["bash"]

