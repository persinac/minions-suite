# ============================================================
# Stage 1: Build Python dependencies (only)
# ============================================================
FROM python:3.14-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Only pyproject + lockfile — builder output is just the venv,
# so it stays cached until dependencies change.
COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen --no-dev --python-preference only-system

# ============================================================
# Stage 2: Runtime image
# ============================================================
FROM python:3.14-slim AS runtime

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        ripgrep \
        curl \
        gnupg \
        unzip \
    && rm -rf /var/lib/apt/lists/*

# GitHub CLI
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# AWS CLI v2
RUN curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscli.zip \
    && unzip -q /tmp/awscli.zip -d /tmp \
    && /tmp/aws/install \
    && rm -rf /tmp/awscli.zip /tmp/aws

# CircleCI CLI
RUN curl -fsSL https://raw.githubusercontent.com/CircleCI-Public/circleci-cli/main/install.sh | bash

# Doppler CLI (via apt repo)
RUN curl -sLf --retry 3 --tlsv1.2 --proto "=https" \
        'https://packages.doppler.com/public/cli/gpg.DE2A7741A397C129.key' \
        | gpg --dearmor -o /usr/share/keyrings/doppler-archive-keyring.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/doppler-archive-keyring.gpg] https://packages.doppler.com/public/cli/deb/debian any-version main" \
        | tee /etc/apt/sources.list.d/doppler-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends doppler \
    && rm -rf /var/lib/apt/lists/*

# uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Non-root user
RUN groupadd --gid 1000 minions \
    && useradd --uid 1000 --gid minions --create-home minions \
    && mkdir -p /app/logs/agents \
    && chown -R minions:minions /app

WORKDIR /app

# --- Cached boundary: everything above here is stable ---

# Python venv from builder (only rebuilds when deps change)
COPY --from=builder --chown=minions:minions /app/.venv /app/.venv

# Application source (changes frequently — copied last)
COPY --chown=minions:minions pyproject.toml uv.lock* /app/
COPY --chown=minions:minions minions/ /app/minions/
COPY --chown=minions:minions prompts/ /app/prompts/
COPY --chown=minions:minions projects.yaml /app/

USER minions

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    CONTAINER_ENV=true \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8321

EXPOSE 8321

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:${MCP_PORT}/sse')" || exit 1

CMD ["python", "-m", "minions", "--server"]
