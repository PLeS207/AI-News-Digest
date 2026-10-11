"""Per-role tool registries enforced at dispatch time, plus the shared agent error types.

The model loop itself runs in OpenClaw (see researcher/openclaw_loop.py and coordinator/service.py)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable



class ToolPermissionError(PermissionError):
    """Raised when an agent requests a tool outside its role's allowlist."""


class ToolInputError(ValueError):
    """Raised by a tool handler for invalid arguments; returned to the model as an error result."""


class AgentLimitError(RuntimeError):
    """Raised when the agent exceeds its Gateway-call budget without finishing."""


class AgentLLMError(RuntimeError):
    """Raised when the model stops for a reason the loop cannot continue from."""


ToolHandler = Callable[[dict[str, Any], Any], Awaitable[Any]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler
    terminal: bool = False  # a successful call ends the loop (result is the agent output)

    def definition(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}


class ToolRegistry:
    """The only tools a role can see or execute. Dispatch re-checks the allowlist."""

    def __init__(self, role: str, tools: list[ToolSpec]):
        self.role = role
        self._tools = {t.name: t for t in tools}

    @property
    def names(self) -> list[str]:
        return list(self._tools)

    def definitions(self) -> list[dict[str, Any]]:
        return [t.definition() for t in self._tools.values()]

    def get(self, name: str) -> ToolSpec:
        if name not in self._tools:
            raise ToolPermissionError(f"Tool '{name}' is not allowed for role '{self.role}'")
        return self._tools[name]

    async def dispatch(self, name: str, args: dict[str, Any], ctx: Any) -> Any:
        spec = self.get(name)
        if not isinstance(args, dict):
            raise ToolInputError("tool input must be a JSON object")
        return await spec.handler(args, ctx)


@dataclass
class AgentRunResult:
    text: str
    terminal_tool: str | None = None
    terminal_output: Any = None
    iterations: int = 0
    tool_calls: int = 0
    usage: dict[str, int] = field(default_factory=dict)


@dataclass
class AgentSpec:
    """A role's tools and budgets. Its instructions live in the agent's OpenClaw workspace."""

    role: str
    registry: ToolRegistry
    max_iterations: int = 4  # Gateway calls per task
    max_tool_calls: int = 8
