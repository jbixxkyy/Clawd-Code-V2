"""Frontend-agnostic `/` command surface shared by the CLI and the web app.

This module is the single place that decides *what a slash command is and how it
runs*. Both :mod:`src.repl.core` and :mod:`src.web_app` call into it, so a
command added here is automatically available in both frontends.

Deliberately free of Rich, ``print`` and ``sys.exit`` — anything terminal-shaped
stays in the REPL. Results are plain data the caller renders however it likes.

Two kinds of command exist (see :mod:`src.command_system.types`):

* ``LocalCommand`` — no model call, returns text immediately.
* ``PromptCommand`` — expands to prompt text that the model then runs on.
  Every skill is one of these.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from src.command_system.builtins import get_builtin_commands
from src.command_system.engine import CommandEngine, CommandResult
from src.command_system.registry import CommandRegistry
from src.command_system.types import (
    Command,
    CommandContext,
    CommandType,
    LocalCommand,
    PromptCommand,
)
from src.skills.loader import find_skill, get_all_skills


# Commands that only make sense attached to a terminal process. The web app
# rejects these with an explanatory message instead of failing confusingly.
TERMINAL_ONLY_COMMANDS: frozenset[str] = frozenset(
    {
        "exit",
        "quit",
        "q",
        "multiline",
        "stream",
        "load",
        "render-last",
    }
)


@dataclass(frozen=True)
class CommandInfo:
    """One row of the command list shown to a user."""

    name: str
    description: str
    argument_hint: str = ""
    aliases: tuple[str, ...] = ()
    loaded_from: str = "builtin"
    kind: str = "local"  # "local" | "prompt" | "skill"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "argument_hint": self.argument_hint,
            "aliases": list(self.aliases),
            "loaded_from": self.loaded_from,
            "kind": self.kind,
        }


@dataclass
class SkillInvocation:
    """A slash command that resolved to a skill rather than a registry command."""

    skill_name: str
    args: str
    prompt: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


def parse_slash(text: str) -> Optional[tuple[str, str]]:
    """Split ``"/skills foo bar"`` into ``("skills", "foo bar")``.

    Returns ``None`` when ``text`` is not a slash command, or is the bare ``/``
    (which the CLI uses to open its command palette).
    """
    stripped = (text or "").strip()
    if not stripped.startswith("/"):
        return None
    body = stripped[1:]
    if not body:
        return None
    parts = body.split(maxsplit=1)
    name = parts[0].strip()
    if not name:
        return None
    args = parts[1].strip() if len(parts) > 1 else ""
    return name, args


def is_terminal_only(name: str) -> bool:
    """True when ``name`` is a terminal-only command."""
    return name.strip().lower().lstrip("/") in TERMINAL_ONLY_COMMANDS


def build_registry(project_root: str | Path | None = None) -> CommandRegistry:
    """Build a fresh registry of invocable non-skill commands.

    A fresh registry per call rather than the process-global one: the web app
    serves requests on multiple threads and two concurrent calls must not
    observe each other's partially-populated state.

    Skills are intentionally *not* registered here. The CLI runs them through
    the Skill tool, which injects a "Base directory for this skill:" line and
    honors ``disable-model-invocation``; the plain ``PromptCommand`` path drops
    both. :func:`build_command_list` layers skills on for display only.
    """
    registry = CommandRegistry()
    for command in get_builtin_commands():
        if command.name.lower() in TERMINAL_ONLY_COMMANDS:
            continue
        registry.register(command)
    _register_surface_commands(registry)
    return registry


def build_command_list(project_root: str | Path | None = None) -> list[CommandInfo]:
    """Every command a user can invoke, deduplicated by name.

    This is the same source the CLI's tab-completer and the browser's `/` menu
    both read from, so the two lists cannot drift apart.
    """
    registry = build_registry(project_root)
    seen: dict[str, CommandInfo] = {}

    for command in registry.list_commands(include_hidden=False):
        if command.name.lower() in TERMINAL_ONLY_COMMANDS:
            continue
        info = CommandInfo(
            name=command.name,
            description=command.description,
            argument_hint=command.argument_hint or "",
            aliases=tuple(command.aliases),
            loaded_from=getattr(command, "loaded_from", "builtin") or "builtin",
            kind=_kind_of(command),
        )
        seen[command.name.lower()] = info

    # Skills that did not make it through the registry (for example a legacy
    # Python skill) still deserve a row in the list.
    try:
        for skill in get_all_skills(project_root=project_root):
            key = skill.name.lower()
            if key in seen or key in TERMINAL_ONLY_COMMANDS:
                continue
            if not skill.user_invocable or skill.is_hidden:
                continue
            seen[key] = CommandInfo(
                name=skill.name,
                description=skill.description,
                argument_hint="",
                loaded_from=skill.loaded_from or "skills",
                kind="skill",
            )
    except Exception:
        pass

    return sorted(seen.values(), key=lambda info: info.name.lower())


def resolve_command(
    name: str, project_root: str | Path | None = None
) -> Optional[Command]:
    """Look up ``name`` (or its alias) in the command registry."""
    cleaned = (name or "").strip().lstrip("/").lower()
    if not cleaned or cleaned in TERMINAL_ONLY_COMMANDS:
        return None
    return build_registry(project_root).get(cleaned)


async def run_command(
    name: str,
    args: str,
    command_context: CommandContext,
    *,
    project_root: str | Path | None = None,
    tool_registry: Any = None,
    tool_context: Any = None,
    session: Any = None,
) -> CommandResult:
    """Run a slash command and return a :class:`CommandResult`.

    ``result_type`` tells the caller what happened:

    * ``"text"``   — a LocalCommand answered. Show ``result.text`` and stop.
    * ``"prompt"`` — a PromptCommand (or skill) expanded. Feed
      :func:`flatten_prompt_content` to the model as the next user message.
    * ``"skip"``   — handled silently.
    """
    cleaned = (name or "").strip().lstrip("/")
    if not cleaned:
        return CommandResult.error("", "Empty command")

    lowered = cleaned.lower()
    if lowered in TERMINAL_ONLY_COMMANDS:
        return CommandResult.error(
            cleaned,
            f"/{cleaned} is only available in the terminal.",
        )

    root = project_root or command_context.workspace_root

    # Commands implemented by the surface itself rather than the command
    # system: they need the tool registry or the session, which live on the
    # caller's side.
    surface_result = _run_surface_command(
        lowered,
        args,
        command_context,
        tool_registry=tool_registry,
        tool_context=tool_context,
        session=session,
    )
    if surface_result is not None:
        return surface_result

    # Registry first, then the Skill tool. This mirrors the CLI's precedence
    # (special commands -> command system -> skill slash, repl/core.py:667-881)
    # so both frontends resolve the same name to the same thing.
    command = build_registry(root).get(lowered)
    if command is not None:
        engine = CommandEngine(
            registry=build_registry(root),
            workspace_root=command_context.workspace_root,
            context=command_context,
        )
        command_input = f"/{cleaned}" + (f" {args}" if args else "")
        return await engine.execute(command_input)

    if tool_registry is not None and tool_context is not None:
        return _run_skill_tool_command(cleaned, args, tool_registry, tool_context)

    return CommandResult.error(cleaned, f"Unknown command: {cleaned}")


def run_skill_tool(
    name: str,
    args: str,
    tool_registry: Any,
    tool_context: Any,
) -> Any:
    """Dispatch the ``Skill`` tool directly.

    Covers markdown skills *and* legacy Python skills, and respects
    ``disable-model-invocation``. Returns the raw :class:`ToolResult`.
    """
    from src.tool_system.protocol import ToolCall

    return tool_registry.dispatch(
        ToolCall(name="Skill", input={"skill": name, "args": args}),
        tool_context,
    )


def flatten_prompt_content(content: list[dict[str, Any]]) -> str:
    """Turn ``CommandResult.prompt_content`` into a plain string for the model."""
    chunks: list[str] = []
    for item in content or []:
        if isinstance(item, dict):
            text = item.get("text")
            if isinstance(text, str):
                chunks.append(text)
        elif isinstance(item, str):
            chunks.append(item)
    return "\n\n".join(chunk for chunk in chunks if chunk.strip()).strip()


# --- internals -------------------------------------------------------------


def _kind_of(command: Command) -> str:
    if isinstance(command, PromptCommand):
        return "prompt"
    if isinstance(command, LocalCommand):
        return "local"
    return "local" if command.command_type == CommandType.LOCAL else "prompt"


def _register_surface_commands(registry: CommandRegistry) -> None:
    """Register commands the surface implements directly (not in builtins)."""
    registry.register(
        LocalCommand(
            name="tools",
            description="List available tools",
            argument_hint="",
            supports_non_interactive=True,
        )
    )


def _run_surface_command(
    name: str,
    args: str,
    command_context: CommandContext,
    *,
    tool_registry: Any = None,
    tool_context: Any = None,
    session: Any = None,
) -> Optional[CommandResult]:
    """Handle commands that need the caller's tool registry or session."""
    if name == "tools":
        if tool_registry is None:
            return CommandResult.error("tools", "Tool registry unavailable")
        names = sorted((spec.name for spec in tool_registry.list_specs()), key=str.lower)
        return CommandResult.success_text("tools", "\n".join(names))

    if name == "tool":
        return _run_raw_tool(name, args, tool_registry, tool_context)

    if name == "save":
        if session is None:
            return CommandResult.error("save", "No session to save")
        session.save()
        return CommandResult.success_text(
            "save", f"Session saved: {session.session_id}"
        )

    return None


def _run_raw_tool(
    name: str,
    args: str,
    tool_registry: Any,
    tool_context: Any,
) -> CommandResult:
    """``/tool <name> [<json-input>]`` — dispatch a tool by hand."""
    import json

    if tool_registry is None or tool_context is None:
        return CommandResult.error("tool", "Tool registry unavailable")

    parts = args.split(maxsplit=1)
    if not parts:
        return CommandResult.error("tool", "Usage: /tool <name> <json-input>")

    tool_name = parts[0]
    payload: dict[str, Any] = {}
    if len(parts) == 2:
        try:
            parsed = json.loads(parts[1])
        except json.JSONDecodeError as exc:
            return CommandResult.error("tool", f"Invalid JSON input: {exc}")
        if not isinstance(parsed, dict):
            return CommandResult.error("tool", "JSON input must be an object")
        payload = parsed

    from src.tool_system.protocol import ToolCall

    try:
        result = tool_registry.dispatch(ToolCall(name=tool_name, input=payload), tool_context)
    except Exception as exc:
        return CommandResult.error("tool", f"Tool error: {exc}")

    return CommandResult.success_text(
        "tool",
        json.dumps(result.output, indent=2, ensure_ascii=False, default=str),
    )


def _run_skill_tool_command(
    name: str,
    args: str,
    tool_registry: Any,
    tool_context: Any,
) -> CommandResult:
    """Run a skill through the Skill tool and translate the result."""
    try:
        result = run_skill_tool(name, args, tool_registry, tool_context)
    except Exception as exc:
        return CommandResult.error(name, f"Skill error: {exc}")

    payload = result.output if isinstance(result.output, dict) else {}
    if result.is_error or not payload.get("success"):
        error = payload.get("error")
        message = error if isinstance(error, str) else f"Unknown skill: {name}"
        return CommandResult.error(name, message)

    # The Skill tool returns the expanded prompt; the caller sends it on.
    prompt = payload.get("prompt")
    if isinstance(prompt, str) and prompt.strip():
        return CommandResult.success_prompt(
            name,
            [{"type": "text", "text": prompt}],
            should_query=True,
        )

    return CommandResult.success_text(name, f"Skill {name} finished.")


def find_skill_or_none(
    name: str, project_root: str | Path | None = None
) -> Optional[Any]:
    """Convenience wrapper for callers that need the raw skill object."""
    return find_skill(name, project_root=project_root)
