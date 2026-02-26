FROM python:3.14-slim

# Inject uv from the official image
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# System deps: ripgrep (search_code tool) + gh CLI (GitHub support)
RUN apt-get update && apt-get install -y --no-install-recommends \
        ripgrep \
        curl \
        gpg \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] \
        https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies (layer-cached separately from source)
COPY pyproject.toml uv.lock* ./
RUN uv sync --no-dev

# Application source
COPY minions/ ./minions/
COPY prompts/ ./prompts/
COPY projects.yaml ./

EXPOSE 8321

CMD ["uv", "run", "python", "-m", "minions", "--server"]
