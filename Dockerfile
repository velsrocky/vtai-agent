# VT-AIAgent Dockerfile
FROM python:3.14-slim

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY --from=ghcr.io/astral-sh/uv:latest /uvx /usr/local/bin/uvx

# Copy dependency specs
COPY pyproject.toml uv.lock README.md ./

# Install dependencies without building the project (source not copied yet)
RUN uv sync --no-dev --no-install-project

# Copy application
COPY . .

# Now install/build the project itself (deps are cached)
RUN uv sync --no-dev

# Run as an unprivileged user; HOME is where vtai writes its data dir and
# where ~ expands inside config/settings.toml.
RUN useradd --create-home --shell /bin/bash vtai \
    && mkdir -p /home/vtai/.vtaiagent/tmp /home/vtai/Downloads /home/vtai/Documents/vt-data \
    && mkdir -p /usr/lib/cargo/bin \
    && chown -R vtai:vtai /home/vtai /app /usr/lib/cargo
USER vtai
ENV HOME=/home/vtai

# The MCP server speaks stdio and has no HTTP port; a sane "is it alive"
# check is that the config loads and the CLI responds.
HEALTHCHECK --interval=60s --timeout=10s --start-period=15s --retries=3 \
    CMD ["uv", "run", "vtai", "info"]

# MCP server speaks stdio; register it with your agent via
# `docker run -i --rm vtai-agent`
CMD ["uv", "run", "vtai", "serve"]
