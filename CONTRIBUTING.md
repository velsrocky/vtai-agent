# Contributing

## Setup

Requires **uv >= 0.13.1** (the lockfile is `revision = 5`; older uv cannot
read it). The floor is declared in `pyproject.toml` as
`[tool.uv] required-version`, so uv reports a clear error instead of a
lockfile-parse failure, and `setup-uv` reads it to pick the CI uv version.

```bash
# Clone & install
git clone https://github.com/your-org/vtai-agent.git
cd vtai-agent
uv sync --dev

# Run tests
uv run pytest tests/
```

### CLI on your PATH (optional)

The MCP server is registered per machine; to get the `vtai` CLI available
outside the repo, install it as an editable tool so it tracks the checkout:

```bash
uv tool install --editable /path/to/vtai-agent
# -> installs `vtai` into `~/.local/bin` (check with `uv tool dir --bin`)
```

## Development Guidelines

### Code Style
- Use `uv run ruff check src/ tests/` and `uv run ruff format`
- Follow existing patterns in `src/vtai_agent/tools/`

### Adding New Tools

1. Create `src/vtai_agent/tools/your_tool.py` (typed `InputModel` + `run()`;
   take a `Guardrails` and call `check_readable`/`check_writable`/
   `ensure_not_killed` before touching disk)
2. Export it from `src/vtai_agent/tools/__init__.py`
3. Register it in `mcp_server.register_tools()` and add the `@server.tool()`
   wrapper so MCP clients see it
4. Add tests in `tests/test_your_tool.py` (use the `sandbox` fixture in
   `tests/conftest.py`)

### Changing Shell Commands

`run_shell` is fail-closed: a new binary needs all three of — an entry in
`guardrails.shell_allowlist`, a mode (`none`/`read`/`write`) in guardrails.py
or `shell_extra_modes`, and a resolve path under `trusted_bin_dirs`.

### Testing
```bash
uv run pytest -xvs
```

## Code of Conduct

- Be respectful
- Keep discussions focused on technical merits
- Follow security best practices
