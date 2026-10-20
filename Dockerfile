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

# Give the container its own config. Without this the agent would pick up
# the checkout's config/settings.toml (the package is installed editable, so
# that file is "repo-adjacent") — which carries the maintainer's paths and
# is not this container's business.
RUN printf '%s\n' \
    'active_provider = "local"' \
    '' \
    '[local]' \
    'base_url = "http://localhost:11434/v1"' \
    'model    = "qwen2.5-coder:7b"' \
    'api_key  = "ollama"' \
    '' \
    '[guardrails]' \
    'dry_run_default = true' \
    'shell_allowlist = ["ls","cat","head","tail","wc","du","df","grep","file","stat","mkdir","mv","cp","rsync","tar","zip","unzip"]' \
    'deny_globs = ["~/.ssh/**","~/.gnupg/**","~/.aws/**","**/.env"]' \
    'trusted_bin_dirs = ["/usr/bin","/bin","/usr/local/bin"]' \
    '' \
    '[paths]' \
    'writable_roots = ["/home/vtai/Downloads","/home/vtai/Documents/vt-data","/home/vtai/.vtaiagent/tmp"]' \
    'data_dir = "/home/vtai/.vtaiagent"' \
    > /home/vtai/.vtaiagent/settings.toml
ENV VT_CONFIG=/home/vtai/.vtaiagent/settings.toml

# The MCP server speaks stdio and has no HTTP port; a sane "is it alive"
# check is that the config loads and the CLI responds.
HEALTHCHECK --interval=60s --timeout=10s --start-period=15s --retries=3 \
    CMD ["uv", "run", "vtai", "info"]

# MCP server speaks stdio; register it with your agent via
# `docker run -i --rm vtai-agent`
CMD ["uv", "run", "vtai", "serve"]
