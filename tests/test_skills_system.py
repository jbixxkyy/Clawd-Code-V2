from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.skills.create import create_skill
from src.skills.frontmatter import parse_frontmatter
from src.skills.loader import (
    clear_skill_registry,
    find_skill,
    get_all_skills,
)
from src.tool_system.context import ToolContext
from src.tool_system.tools import SkillTool


class SkillSystemTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()

    def tearDown(self) -> None:
        clear_skill_registry()
        self.tmp.cleanup()


class TestSkillCreate(SkillSystemTests):
    def test_create_writes_skill_md(self) -> None:
        skills_dir = self.root / "skills"
        p = create_skill(
            directory=skills_dir,
            name="demo",
            description="demo skill",
            when_to_use="use it when testing",
            allowed_tools=["Read", "Grep"],
            arguments=["foo", "bar"],
            body="Hello $foo $1",
        )
        self.assertTrue(p.exists())
        parsed = parse_frontmatter(p.read_text(encoding="utf-8"))
        self.assertEqual(parsed.frontmatter["description"], "demo skill")
        self.assertEqual(parsed.frontmatter["when_to_use"], "use it when testing")

    def test_parse_frontmatter_supports_inline_lists(self) -> None:
        parsed = parse_frontmatter(
            "---\n"
            "arguments: [name]\n"
            "allowed-tools: [Read, Grep]\n"
            "---\n"
            "Hello $name\n"
        )
        self.assertEqual(parsed.frontmatter["arguments"], ["name"])
        self.assertEqual(parsed.frontmatter["allowed-tools"], ["Read", "Grep"])


class TestSkillRegister(SkillSystemTests):
    def test_register_loads_skill_from_dir(self) -> None:
        skills_dir = self.root / "skills"
        create_skill(
            directory=skills_dir,
            name="hello",
            description="say hello",
            body="Hello",
        )
        with patch.dict(os.environ, {"CLAWD_SKILLS_DIR": str(skills_dir)}):
            skills = get_all_skills(project_root=self.root)
            by_name = {s.name: s for s in skills}
            self.assertIn("hello", by_name)
            self.assertEqual(by_name["hello"].description, "say hello")
            self.assertEqual(by_name["hello"].loaded_from, "user")


class TestSkillUse(SkillSystemTests):
    def test_use_substitutes_arguments(self) -> None:
        skills_dir = self.root / "skills"
        create_skill(
            directory=skills_dir,
            name="hello",
            description="say hello",
            arguments=["name"],
            body="Hello $name ($0) / $ARGUMENTS",
        )
        ctx = ToolContext(workspace_root=self.root)
        with patch.dict(os.environ, {"CLAWD_SKILLS_DIR": str(skills_dir)}):
            out = SkillTool().run({"skill": "hello", "args": 'bob "the builder"'}, ctx).output
            self.assertTrue(out["success"])
            self.assertIn("Hello bob (bob)", out["prompt"])
            self.assertIn('bob "the builder"', out["prompt"])


class TestSkillConcurrency(SkillSystemTests):
    """The web app loads skills from request threads; the CLI never did.

    Before the loader lock, `get_all_skills` cleared a module-global registry
    and rebuilt it, so two concurrent callers could observe each other's
    half-finished load. These tests pin that down.
    """

    def test_find_skill_isolates_concurrent_project_roots(self) -> None:
        import concurrent.futures

        # One skill per project, each with a body unique to its project.
        projects: dict[str, Path] = {}
        for tag in ("alpha", "beta", "gamma", "delta"):
            project = self.root / tag
            create_skill(
                directory=project / ".clawd" / "skills",
                name="shared-name",
                description=f"skill from {tag}",
                body=f"body-{tag}",
            )
            projects[tag] = project

        def lookup(tag: str) -> str:
            # Call repeatedly so a racy implementation has many chances to
            # return another thread's skill.
            bodies = set()
            for _ in range(12):
                skill = find_skill("shared-name", project_root=projects[tag])
                self.assertIsNotNone(skill, f"{tag} lost its skill")
                bodies.add(skill.markdown_content)
            self.assertEqual(bodies, {f"body-{tag}"}, f"{tag} saw a mixed result")
            return tag

        tags = [tag for tag in projects for _ in range(6)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            done = list(pool.map(lookup, tags))
        self.assertEqual(sorted(done), sorted(tags))

    def test_get_all_skills_returns_a_stable_snapshot(self) -> None:
        import concurrent.futures

        skills_dir = self.root / "skills"
        for name in ("one", "two", "three"):
            create_skill(
                directory=skills_dir,
                name=name,
                description=f"{name} skill",
                body=f"{name} body",
            )

        def read_all() -> int:
            for _ in range(15):
                snapshot = list(get_all_skills(project_root=self.root))
                names = {s.name for s in snapshot}
                # Every snapshot must be complete, never a partial rebuild.
                # Only assert the three we created: the real ~/.clawd/skills
                # and ~/.claude/skills dirs on this machine may contribute more.
                self.assertTrue(
                    {"one", "two", "three"} <= names,
                    f"partial skill list: {sorted(names)}",
                )
            return len(names)

        with patch.dict(os.environ, {"CLAWD_SKILLS_DIR": str(skills_dir)}):
            with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
                counts = list(pool.map(lambda _: read_all(), range(24)))
        # A racy loader returns partial lists, so the count would dip below 3.
        self.assertTrue(all(c >= 3 for c in counts), counts)

    def test_skill_tool_runs_concurrently_without_cross_talk(self) -> None:
        import concurrent.futures

        projects: dict[str, Path] = {}
        for tag in ("north", "south", "east", "west"):
            project = self.root / tag
            create_skill(
                directory=project / ".clawd" / "skills",
                name="report",
                description=f"{tag} report",
                arguments=["who"],
                body=f"{tag} report for $who",
            )
            projects[tag] = project

        def run(tag: str) -> str:
            ctx = ToolContext(workspace_root=projects[tag])
            for who in ("alice", "bob", "carol"):
                out = SkillTool().run({"skill": "report", "args": who}, ctx).output
                self.assertTrue(out["success"], out)
                self.assertIn(f"{tag} report for {who}", out["prompt"])
                self.assertNotIn(
                    [other for other in projects if other != tag][0],
                    out["prompt"],
                )
            return tag

        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            done = list(pool.map(run, [t for t in projects for _ in range(6)]))
        self.assertEqual(sorted(done), sorted([t for t in projects for _ in range(6)]))
