#!/usr/bin/env bash
# Register the VT-AIAgent MCP server with the coding-agent CLIs.
# opencode and Claude Code support MCP natively; pi does not (extension-only),
# so pi drives the agent via the `vtai` CLI instead.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Resolve uv to an absolute path. A bare "uv" in the registered command breaks
# when the client spawns the server from a daemon with a minimal PATH
# (launchd/systemd give PATH=/usr/bin:/bin:...), and the MCP server then
# fails to start with "NotFound: ChildProcess.spawn (uv ...)".
UV_BIN="$(command -v uv || true)"
if [ -z "$UV_BIN" ]; then
  echo "error: uv not found on PATH — install it: https://docs.astral.sh/uv/" >&2
  exit 1
fi
CMD=("$UV_BIN" run --directory "$PROJECT_DIR" vtai serve)

echo "Registering vtai-agent MCP server (project: $PROJECT_DIR)"

# --- opencode: global config, merged with existing providers ---
# opencode writes the global MCP registry to opencode.json (a hand-written
# opencode.jsonc in the same directory is merged on top of it).
OPENCODE_CONFIG="${OPENCODE_CONFIG:-$HOME/.config/opencode/opencode.json}"
if command -v opencode >/dev/null 2>&1; then
  echo "  opencode -> $OPENCODE_CONFIG"
  # --global writes the user-level config; without it the server lands in the
  # current directory's opencode.json and is only visible in this project.
  opencode mcp add --global vtai-agent -- "${CMD[@]}" 2>/dev/null \
    || echo "  (manual edit needed: add mcp.servers.vtai-agent to $OPENCODE_CONFIG)"
fi

# --- Claude Code: user-scoped ---
if command -v claude >/dev/null 2>&1; then
  echo "  claude  -> user scope"
  claude mcp add vtai-agent -s user -- "${CMD[@]}" || true
fi

# --- pi: no native MCP; verify the CLI is reachable instead ---
if command -v pi >/dev/null 2>&1; then
  echo "  pi      -> no native MCP (extension-only); CLI fallback:"
  echo "            pi \"run: \$($UV_BIN run --directory $PROJECT_DIR vtai run system_info)\""
fi

echo
echo "Verify from ${PROJECT_DIR}: $UV_BIN run vtai info"
