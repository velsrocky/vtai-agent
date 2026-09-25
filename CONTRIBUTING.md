# Contributing

## Setup

```bash
# Clone & install
git clone https://github.com/your-org/vtai-agent.git
cd vtai-agent
uv sync --dev

# Run tests
uv run pytest tests/
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
