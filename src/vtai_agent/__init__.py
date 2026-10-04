"""VT-AIAgent: system-automation agent exposed as an MCP server.

Drivable from opencode, Claude Code, and pi. Cloud (Anthropic/OpenAI) and
local (llama.cpp/Ollama via OpenAI-compatible endpoint) LLMs are both supported.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("vtai-agent")
except PackageNotFoundError:  # running from a raw source checkout
    __version__ = "0.0.0+unknown"
