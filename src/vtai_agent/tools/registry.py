"""Tool registry.

Tools are declared once with a typed input model and a run() method. The same
instances are exposed to the MCP server (for opencode/claude/pi) and, later, to
the pydantic-ai agent loop — so behavior and guardrails stay identical no
matter who is driving.
"""

from abc import ABC, abstractmethod
import os
import shutil
from typing import Any, ClassVar, Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


def resolve_cli_argv(cli_name: str) -> list[str] | None:
    """Full argv prefix for spawning a coding CLI on this platform, or None
    when it is not installed.

    A bare CLI name is not safe to spawn: the server process' PATH can
    differ from the user's shell (WinError 2 in practice), and on Windows
    npm-installed CLIs are .cmd/.bat shims that CreateProcess cannot exec
    directly. Spawn the absolute path `shutil.which` resolved instead,
    routed through cmd.exe when it is a shim.
    """
    found = shutil.which(cli_name)
    if found is None:
        return None
    if os.name == "nt" and found.lower().endswith((".cmd", ".bat")):
        return ["cmd.exe", "/c", found]
    return [found]


class ToolResult(BaseModel):
    ok: bool
    summary: str
    detail: dict[str, Any] = {}
    dry_run: bool = False


class ToolError(Exception):
    pass


class Tool(ABC, Generic[T]):
    name: ClassVar[str]
    description: ClassVar[str]
    InputModel: ClassVar[type[BaseModel]]

    @abstractmethod
    async def run(self, inp: T, *, run_id: int | None = None) -> ToolResult:
        ...


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> Tool:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool: {tool.name}")
        self._tools[tool.name] = tool
        return tool

    def get(self, name: str) -> Tool:
        if name not in self._tools:
            raise KeyError(f"unknown tool: {name}")
        return self._tools[name]

    def all(self) -> dict[str, Tool]:
        return dict(self._tools)

    def clear(self) -> None:
        self._tools.clear()

    async def call(self, name: str, args: dict[str, Any], *, run_id: int | None = None) -> ToolResult:
        tool = self.get(name)
        inp = tool.InputModel(**args)
        return await tool.run(inp, run_id=run_id)


registry = ToolRegistry()
