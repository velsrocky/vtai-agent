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

# MCP server speaks stdio; register it with your agent via `docker run -i --rm vtai-agent`
CMD ["uv", "run", "vtai", "serve"]
