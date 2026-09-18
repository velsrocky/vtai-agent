#!/usr/bin/env bash
# Register the VT-AIAgent MCP server with the coding-agent CLIs.
# opencode and Claude Code support MCP natively; pi does not (extension-only),
# so pi drives the agent via the `vtai` CLI instead.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CMD=(uv run --directory "$PROJECT_DIR" vtai serve)

echo "Registering vtai-agent MCP server (project: $PROJECT_DIR)"

# --- opencode: global config, merged with existing providers ---
OPENCODE_CONFIG="${OPENCODE_CONFIG:-$HOME/.config/opencode/opencode.jsonc}"
if command -v opencode >/dev/null 2>&1; then
  echo "  opencode -> $OPENCODE_CONFIG"
  opencode mcp add vtai-agent -- "${CMD[@]}" 2>/dev/null \
    || echo "  (manual edit needed: add mcp.vtai-agent to $OPENCODE_CONFIG)"
fi

# --- Claude Code: user-scoped ---
if command -v claude >/dev/null 2>&1; then
  echo "  claude  -> user scope"
  claude mcp add vtai-agent -s user -- "${CMD[@]}" || true
fi

# --- pi: no native MCP; verify the CLI is reachable instead ---
if command -v pi >/dev/null 2>&1; then
  echo "  pi      -> no native MCP (extension-only); CLI fallback:"
  echo "            pi \"run: \$(uv run --directory $PROJECT_DIR vtai run system_info)\""
fi

echo
echo "Verify from ${PROJECT_DIR}: uv run vtai info"
