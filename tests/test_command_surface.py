from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.command_surface import (
    TERMINAL_ONLY_COMMANDS,
    build_command_list,
    build_registry,
    flatten_prompt_content,
    is_terminal_only,
    parse_slash,
    resolve_command,
    run_command,
)
from src.command_system.engine import create_command_context
from src.skills.create import create_skill
from src.skills.loader import clear_skill_registry
from src.tool_system.defaults import build_default_registry


class TestParseSlash(unittest.TestCase):
    def test_splits_name_and_args(self) -> None:
        self.assertEqual(parse_slash("/skills foo bar"), ("skills", "foo bar"))

    def test_no_args(self) -> None:
        self.assertEqual(parse_slash("/skills"), ("skills", ""))

    def test_bare_slash_is_not_a_command(self) -> None:
        # The CLI uses a bare "/" to open its command palette.
        self.assertIsNone(parse_slash("/"))

    def test_plain_message_is_not_a_command(self) -> None:
        self.assertIsNone(parse_slash("fix the bug"))
        self.assertIsNone(parse_slash(""))

    def test_strips_outer_whitespace(self) -> None:
        self.assertEqual(parse_slash("  /help  "), ("help", ""))

    def test_slash_inside_message_is_not_a_command(self) -> None:
        self.assertIsNone(parse_slash("divide 10 / 2"))


class TestTerminalOnly(unittest.TestCase):
    def test_known_terminal_commands(self) -> None:
        for name in ("exit", "quit", "q", "multiline", "stream", "load", "render-last"):
            self.assertTrue(is_terminal_only(name), name)
            self.assertTrue(is_terminal_only(f"/{name}"), name)

    def test_normal_commands_are_not_terminal_only(self) -> None:
        for name in ("help", "clear", "skills", "cost", "context", "compact", "init"):
            self.assertFalse(is_terminal_only(name), name)

    def test_registry_excludes_terminal_commands(self) -> None:
        registry = build_registry(Path.cwd())
        for name in TERMINAL_ONLY_COMMANDS:
            self.assertIsNone(registry.get(name), f"{name} leaked into the registry")


class TestCommandList(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()

    def tearDown(self) -> None:
        clear_skill_registry()
        self.tmp.cleanup()

    def test_lists_core_commands(self) -> None:
        names = {info.name for info in build_command_list(self.root)}
        for expected in ("help", "clear", "skills", "cost", "context", "compact", "init", "tools"):
            self.assertIn(expected, names)

    def test_lists_are_deduplicated(self) -> None:
        infos = build_command_list(self.root)
        lowered = [info.name.lower() for info in infos]
        self.assertEqual(len(lowered), len(set(lowered)))

    def test_includes_workspace_skills(self) -> None:
        create_skill(
            directory=self.root / ".clawd" / "skills",
            name="ship-it",
            description="tag a release",
            body="Release steps",
        )
        infos = build_command_list(self.root)
        match = next((i for i in infos if i.name == "ship-it"), None)
        self.assertIsNotNone(match, "workspace skill missing from command list")
        assert match is not None
        self.assertEqual(match.kind, "skill")
        self.assertEqual(match.description, "tag a release")
        self.assertEqual(match.loaded_from, "project")

    def test_hidden_skills_are_omitted(self) -> None:
        create_skill(
            directory=self.root / ".clawd" / "skills",
            name="secret",
            description="not for models",
            body="hidden",
        )
        # user-invocable: false makes it hidden
        skill_md = self.root / ".clawd" / "skills" / "secret" / "SKILL.md"
        skill_md.write_text(
            "---\nuser-invocable: false\ndescription: not for models\n---\nhidden\n",
            encoding="utf-8",
        )
        names = {i.name for i in build_command_list(self.root)}
        self.assertNotIn("secret", names)

    def test_broken_skills_dir_does_not_break_the_list(self) -> None:
        bad = self.root / ".clawd" / "skills" / "broken"
        bad.mkdir(parents=True)
        (bad / "SKILL.md").write_bytes(b"\xff\xfe\x00 not utf-8")
        names = {i.name for i in build_command_list(self.root)}
        self.assertIn("help", names)

    def test_cli_and_web_read_the_same_list(self) -> None:
        """The browser menu and the CLI completer must not drift."""
        from src.repl.core import ClawdREPL  # noqa: F401  (import guard only)

        surface = {f"/{info.name}" for info in build_command_list(self.root)}
        # Everything the surface offers must be invocable; terminal-only
        # commands are the CLI's business, not the surface's.
        self.assertTrue({"/help", "/skills", "/clear"} <= surface)


class TestResolveCommand(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()

    def tearDown(self) -> None:
        clear_skill_registry()
        self.tmp.cleanup()

    def test_resolves_builtin_by_name_and_alias(self) -> None:
        self.assertIsNotNone(resolve_command("help", self.root))
        self.assertIsNotNone(resolve_command("?", self.root))
        self.assertIsNotNone(resolve_command("/clear", self.root))

    def test_terminal_only_resolves_to_none(self) -> None:
        self.assertIsNone(resolve_command("exit", self.root))

    def test_unknown_resolves_to_none(self) -> None:
        self.assertIsNone(resolve_command("definitely-not-a-command", self.root))


class TestRunCommand(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.skills_dir = self.root / ".clawd" / "skills"

    def tearDown(self) -> None:
        clear_skill_registry()
        self.tmp.cleanup()

    def _ctx(self):
        return create_command_context(workspace_root=self.root)

    def _run(self, name, args="", **kwargs):
        return asyncio.run(run_command(name, args, self._ctx(), **kwargs))

    def test_local_command_returns_text(self) -> None:
        create_skill(
            directory=self.skills_dir,
            name="greeter",
            description="greets",
            body="Say hello",
        )
        result = self._run("skills", "", project_root=self.root)
        self.assertTrue(result.success)
        self.assertEqual(result.result_type, "text")
        self.assertIn("greeter", result.text)

    def test_terminal_only_command_is_rejected(self) -> None:
        result = self._run("exit", "", project_root=self.root)
        self.assertFalse(result.success)
        self.assertIn("terminal", result.error or "")

    def test_unknown_command_reports_unknown(self) -> None:
        result = self._run("nope-not-real", "", project_root=self.root)
        self.assertFalse(result.success)
        self.assertIn("Unknown command", result.error or "")

    def test_skill_slash_returns_prompt_for_the_model(self) -> None:
        create_skill(
            directory=self.skills_dir,
            name="release",
            description="tag a release",
            arguments=["version"],
            body="Releasing version $version now.",
        )
        from src.tool_system.defaults import build_default_registry
        from src.tool_system.context import ToolContext

        registry = build_default_registry()
        tool_context = ToolContext(workspace_root=self.root)
        with patch.dict(os.environ, {"CLAWD_SKILLS_DIR": str(self.skills_dir)}):
            result = self._run(
                "release",
                "v1.2.3",
                project_root=self.root,
                tool_registry=registry,
                tool_context=tool_context,
            )
        self.assertTrue(result.success, result.error)
        self.assertEqual(result.result_type, "prompt")
        text = flatten_prompt_content(result.prompt_content)
        self.assertIn("Releasing version v1.2.3 now.", text)
        # The Skill tool injects the skill's base directory.
        self.assertIn("Base directory for this skill", text)

    def test_skill_with_model_invocation_disabled_is_rejected(self) -> None:
        skill_md = self.skills_dir / "locked" / "SKILL.md"
        skill_md.parent.mkdir(parents=True)
        skill_md.write_text(
            "---\ndescription: locked down\ndisable-model-invocation: true\n---\nnope\n",
            encoding="utf-8",
        )
        from src.tool_system.context import ToolContext

        result = self._run(
            "locked",
            "",
            project_root=self.root,
            tool_registry=build_default_registry(),
            tool_context=ToolContext(workspace_root=self.root),
        )
        self.assertFalse(result.success)
        self.assertIn("disable-model-invocation", result.error or "")

    def test_tools_command_lists_tools(self) -> None:
        result = self._run(
            "tools", "", project_root=self.root, tool_registry=build_default_registry()
        )
        self.assertTrue(result.success)
        self.assertIn("Bash", result.text)
        self.assertIn("Skill", result.text)

    def test_save_command_saves_the_session(self) -> None:
        from src.agent import Session

        session = Session.create("glm", "test-model")
        result = self._run("save", "", project_root=self.root, session=session)
        self.assertTrue(result.success)
        self.assertIn(session.session_id, result.text)


class TestFlattenPromptContent(unittest.TestCase):
    def test_joins_text_blocks(self) -> None:
        content = [
            {"type": "text", "text": "first"},
            {"type": "text", "text": "second"},
        ]
        self.assertEqual(flatten_prompt_content(content), "first\n\nsecond")

    def test_skips_empty_and_non_text(self) -> None:
        content = [{"type": "image", "data": "x"}, {"type": "text", "text": "  "}, "raw"]
        self.assertEqual(flatten_prompt_content(content), "raw")

    def test_handles_empty(self) -> None:
        self.assertEqual(flatten_prompt_content([]), "")
        self.assertEqual(flatten_prompt_content(None), "")


if __name__ == "__main__":
    unittest.main()
