from __future__ import annotations

import contextlib
import io
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from urllib.parse import urlencode

from src.agent import Session
from src.tool_system.agent_loop import AgentLoopCancelled, ToolEvent
from src.tool_system.context import ToolContext
from src.web_app import _AppState, _HTML, _Job, _make_handler, _run_job, _source_signature


class WebAppTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name).resolve()
        self.state = _AppState(
            workspace=self.workspace,
            provider_name="nvidia",
            provider=SimpleNamespace(model="test-model"),
            session=Session.create("nvidia", "test-model"),
            registry=object(),
            tool_context=ToolContext(workspace_root=self.workspace),
        )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self.state))
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=2)
        self.temp.cleanup()

    def test_api_is_accessible_without_a_token(self) -> None:
        request = urllib.request.Request(f"{self.url}/api/state")
        with urllib.request.urlopen(request, timeout=2) as response:
            payload = json.load(response)
        self.assertEqual(payload["workspace"], str(self.workspace))

    def test_state_includes_workspace_and_model_without_authentication(self) -> None:
        request = urllib.request.Request(
            f"{self.url}/api/state",
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            payload = json.load(response)
        self.assertEqual(payload["workspace"], str(self.workspace))
        self.assertEqual(payload["provider"], "nvidia")
        self.assertEqual(payload["model"], "test-model")

    def test_directory_browser_lists_subdirectories(self) -> None:
        child = self.workspace / "project"
        child.mkdir()
        request = urllib.request.Request(
            f"{self.url}/api/directories?{urlencode({'path': str(self.workspace)})}",
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            payload = json.load(response)
        self.assertEqual(payload["path"], str(self.workspace))
        self.assertEqual(payload["directories"], [{"name": "project", "path": str(child)}])

    def test_directory_search_stays_within_selected_folder_and_ranks_exact_names(self) -> None:
        search_root = self.workspace / "selected-folder"
        exact_match = search_root / "nested" / "PS4"
        partial_match = search_root / "PS4-tools"
        outside_match = self.workspace / "outside" / "PS4"
        exact_match.mkdir(parents=True)
        partial_match.mkdir(parents=True)
        outside_match.mkdir(parents=True)
        request = urllib.request.Request(
            f"{self.url}/api/directories/search?{urlencode({'path': str(search_root), 'query': 'ps4'})}",
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            payload = json.load(response)
        self.assertEqual(
            [directory["path"] for directory in payload["directories"]],
            [str(exact_match), str(partial_match)],
        )
        self.assertEqual(payload["directories"][0]["name"], "PS4")
        self.assertEqual(payload["directories"][0]["parent"], str(exact_match.parent))
        self.assertEqual(
            payload["directories"][0]["drive"],
            exact_match.anchor.rstrip("\\/"),
        )
        self.assertFalse(payload["truncated"])
        self.assertEqual(payload["skipped"], 0)

    def test_folder_picker_exposes_location_and_optional_scoped_search(self) -> None:
        self.assertIn('id="folderPath"', _HTML)
        self.assertIn('id="goFolderPath"', _HTML)
        self.assertIn('id="folderSearch"', _HTML)
        self.assertIn("/api/directories/search?", _HTML)
        self.assertIn("Search within this folder", _HTML)
        self.assertIn("path:currentDirectory,query:queryText", _HTML)
        self.assertIn('addEventListener("input",()=>{selectedFolderPath=null;', _HTML)

    def test_request_switches_workspace_context_without_authentication(self) -> None:
        selected = self.workspace / "selected"
        selected.mkdir()
        old_session_id = self.state.session.session_id
        request = urllib.request.Request(
            f"{self.url}/api/workspace",
            data=json.dumps({"path": str(selected)}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            payload = json.load(response)
        self.assertEqual(payload["workspace"], str(selected))
        self.assertEqual(self.state.workspace, selected)
        self.assertEqual(self.state.tool_context.workspace_root, selected)
        self.assertEqual(self.state.session.conversation.messages, [])
        self.assertNotEqual(self.state.session.session_id, old_session_id)
        self.assertEqual(self.state.session.workspace, str(selected))

    def test_workspace_switch_rejects_non_directories(self) -> None:
        request = urllib.request.Request(
            f"{self.url}/api/workspace",
            data=json.dumps({"path": str(self.workspace / "missing")}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=2)
        self.assertEqual(raised.exception.code, 400)
        self.assertEqual(self.state.workspace, self.workspace)

    def test_workspace_switch_does_not_change_a_running_jobs_context(self) -> None:
        selected = self.workspace / "selected"
        selected.mkdir()
        job_context = ToolContext(workspace_root=self.workspace)
        job = _Job(job_id="busy", session_id=self.state.session.session_id,
                   session=self.state.session, tool_context=job_context)
        self.state.jobs[job.job_id] = job
        request = urllib.request.Request(
            f"{self.url}/api/workspace",
            data=json.dumps({"path": str(selected)}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            payload = json.load(response)
        self.assertEqual(payload["workspace"], str(selected))
        self.assertIs(job.tool_context, job_context)
        self.assertEqual(job.tool_context.workspace_root, self.workspace)
        self.assertEqual(self.state.workspace, selected)

    def test_new_session_clears_conversation_and_rebuilds_workspace_context(self) -> None:
        self.state.session.conversation.add_user_message("Previous conversation")
        old_session_id = self.state.session.session_id
        request = urllib.request.Request(
            f"{self.url}/api/session/new",
            data=json.dumps({"start": True}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            payload = json.load(response)
        self.assertTrue(payload["ok"])
        self.assertEqual(self.state.session.conversation.messages, [])
        self.assertNotEqual(self.state.session.session_id, old_session_id)
        self.assertEqual(self.state.session.workspace, str(self.workspace))
        self.assertEqual(self.state.tool_context.workspace_root, self.workspace)

    def test_new_session_can_start_while_another_chat_is_running(self) -> None:
        old_session_id = self.state.session.session_id
        job = _Job(job_id="busy", session_id=old_session_id, session=self.state.session,
                   tool_context=ToolContext(workspace_root=self.workspace))
        self.state.jobs[job.job_id] = job
        request = urllib.request.Request(
            f"{self.url}/api/session/new",
            data=json.dumps({"start": True}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            payload = json.load(response)
        self.assertTrue(payload["ok"])
        self.assertNotEqual(payload["session_id"], old_session_id)
        self.assertEqual(self.state.session.session_id, payload["session_id"])
        self.assertEqual(self.state.session.workspace, str(self.workspace))
        self.assertIs(self.state.jobs[job.job_id], job)

    def test_saved_chats_are_listed_and_can_be_loaded(self) -> None:
        saved = Session.create("nvidia", "test-model")
        saved.session_id = "saved_chat_20261011"
        saved.conversation.add_user_message("Explain this project")
        saved.conversation.add_assistant_message("It is a Python project.")
        saved_workspace = self.workspace / "saved-project"
        saved_workspace.mkdir()
        saved.workspace = str(saved_workspace)
        with patch("pathlib.Path.home", return_value=self.workspace):
            saved.save()
            request = urllib.request.Request(
                f"{self.url}/api/sessions",
            )
            with urllib.request.urlopen(request, timeout=2) as response:
                listing = json.load(response)
            self.assertEqual(listing["sessions"][0]["session_id"], saved.session_id)
            self.assertEqual(listing["sessions"][0]["title"], "Explain this project")

            request = urllib.request.Request(
                f"{self.url}/api/session/load",
                data=json.dumps({"session_id": saved.session_id}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=2) as response:
                loaded = json.load(response)

        self.assertEqual(loaded["session_id"], saved.session_id)
        self.assertEqual(loaded["workspace"], str(saved_workspace))
        self.assertEqual(
            loaded["messages"],
            [
                {"role": "user", "text": "Explain this project"},
                {"role": "assistant", "text": "It is a Python project."},
            ],
        )
        self.assertEqual(self.state.session.session_id, saved.session_id)
        self.assertEqual(self.state.workspace, saved_workspace)
        self.assertEqual(self.state.tool_context.workspace_root, saved_workspace)

    def test_loading_legacy_session_uses_web_app_launch_folder(self) -> None:
        saved = Session.create("nvidia", "test-model")
        saved.session_id = "legacy_session_20261011"
        saved.conversation.add_user_message("Legacy conversation")
        saved.workspace = None
        with patch("pathlib.Path.home", return_value=self.workspace):
            saved.save()
            request = urllib.request.Request(
                f"{self.url}/api/session/load",
                data=json.dumps({"session_id": saved.session_id}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=2) as response:
                payload = json.load(response)
        self.assertEqual(payload["workspace"], str(self.workspace))
        self.assertEqual(self.state.session.workspace, str(self.workspace))
        self.assertEqual(self.state.tool_context.workspace_root, self.workspace)

    def test_session_load_rejects_invalid_session_id(self) -> None:
        request = urllib.request.Request(
            f"{self.url}/api/session/load",
            data=json.dumps({"session_id": "../outside"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=2)
        self.assertEqual(raised.exception.code, 400)

    def test_running_chats_are_isolated_and_can_run_concurrently(self) -> None:
        second = Session.create("nvidia", "test-model")
        second.session_id = "other_session_20261011"
        other_workspace = self.workspace / "other-project"
        other_workspace.mkdir()
        second.workspace = str(other_workspace)
        self.state.sessions[second.session_id] = second
        started = threading.Event()
        release = threading.Event()
        starts_lock = threading.Lock()
        started_count = 0
        contexts = []

        def run_loop(**kwargs):
            nonlocal started_count
            contexts.append(kwargs["tool_context"])
            with starts_lock:
                started_count += 1
                if started_count == 2:
                    started.set()
            self.assertTrue(release.wait(timeout=3))
            kwargs["on_text_chunk"]("Isolated reply")
            return SimpleNamespace(response_text="Isolated reply")

        def start_chat(session_id: str) -> dict:
            request = urllib.request.Request(
                f"{self.url}/api/chat",
                data=json.dumps({"session_id": session_id, "message": f"Message for {session_id}"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=2) as response:
                return json.load(response)

        with patch("pathlib.Path.home", return_value=self.workspace), patch(
            "src.web_app.run_agent_loop", side_effect=run_loop
        ):
            first_job = start_chat(self.state.session.session_id)
            second_job = start_chat(second.session_id)
            self.assertTrue(started.wait(timeout=2))
            self.assertEqual(len(self.state.jobs), 2)
            self.assertEqual(
                {job.session_id for job in self.state.jobs.values()},
                {self.state.session.session_id, second.session_id},
            )
            duplicate = urllib.request.Request(
                f"{self.url}/api/chat",
                data=json.dumps({
                    "session_id": self.state.session.session_id,
                    "message": "A concurrent turn in the same chat",
                }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(duplicate, timeout=2)
            self.assertEqual(raised.exception.code, 409)
            request = urllib.request.Request(
                f"{self.url}/api/sessions",
            )
            with urllib.request.urlopen(request, timeout=2) as response:
                listed = {entry["session_id"]: entry for entry in json.load(response)["sessions"]}
            self.assertTrue(listed[self.state.session.session_id]["running"])
            self.assertTrue(listed[second.session_id]["running"])
            request = urllib.request.Request(
                f"{self.url}/api/session/load",
                data=json.dumps({"session_id": self.state.session.session_id}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=2) as response:
                self.assertEqual(json.load(response)["session_id"], self.state.session.session_id)
            release.set()
            for _ in range(100):
                if all(job.status == "completed" for job in self.state.jobs.values()):
                    break
                threading.Event().wait(0.02)

        self.assertEqual(self.state.jobs[first_job["job_id"]].status, "completed")
        self.assertEqual(self.state.jobs[second_job["job_id"]].status, "completed")
        first_session = self.state.jobs[first_job["job_id"]].session
        self.assertEqual(first_session.conversation.messages[0].content,
                         f"Message for {first_session.session_id}")
        self.assertEqual(second.conversation.messages[0].content, f"Message for {second.session_id}")
        self.assertEqual(len(contexts), 2)
        self.assertIsNot(contexts[0], contexts[1])
        self.assertEqual({context.workspace_root for context in contexts},
                         {self.workspace, other_workspace})

    def test_saved_chat_can_be_deleted_but_running_chat_cannot(self) -> None:
        saved = Session.create("nvidia", "test-model")
        saved.session_id = "delete_me_20261011"
        saved.conversation.add_user_message("Delete this chat")
        busy = Session.create("nvidia", "test-model")
        busy.session_id = "busy_chat_20261011"
        busy.conversation.add_user_message("Still running")
        with patch("pathlib.Path.home", return_value=self.workspace):
            saved.save()
            busy.save()
            busy_job = _Job(
                job_id="busy-delete",
                session_id=busy.session_id,
                session=busy,
                tool_context=ToolContext(workspace_root=self.workspace),
            )
            self.state.jobs[busy_job.job_id] = busy_job
            request = urllib.request.Request(
                f"{self.url}/api/session/delete",
                data=json.dumps({"session_id": busy.session_id}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(request, timeout=2)
            self.assertEqual(raised.exception.code, 409)
            self.assertTrue((self.workspace / ".clawd" / "sessions" / f"{busy.session_id}.json").exists())

            self.state.session = saved
            self.state.sessions[saved.session_id] = saved
            request = urllib.request.Request(
                f"{self.url}/api/session/delete",
                data=json.dumps({"session_id": saved.session_id}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=2) as response:
                result = json.load(response)
            self.assertTrue(result["ok"])
            self.assertNotEqual(result["session_id"], saved.session_id)
            self.assertEqual(self.state.session.session_id, result["session_id"])
            self.assertEqual(self.state.session.conversation.messages, [])
            self.assertFalse((self.workspace / ".clawd" / "sessions" / f"{saved.session_id}.json").exists())

    def test_saved_chat_listing_skips_a_corrupt_session_file(self) -> None:
        saved = Session.create("nvidia", "test-model")
        saved.session_id = "healthy_chat_20261011"
        saved.conversation.add_user_message("Still here")
        with patch("pathlib.Path.home", return_value=self.workspace):
            saved.save()
            sessions_dir = self.workspace / ".clawd" / "sessions"
            (sessions_dir / "truncated_chat_20261011.json").write_text("", encoding="utf-8")
            (sessions_dir / "garbage_chat_20261011.json").write_text("{not json", encoding="utf-8")

            request = urllib.request.Request(f"{self.url}/api/sessions")
            with urllib.request.urlopen(request, timeout=2) as response:
                listing = json.load(response)

        self.assertEqual(
            [entry["session_id"] for entry in listing["sessions"]],
            [saved.session_id],
        )

    def test_session_save_is_atomic(self) -> None:
        session = Session.create("nvidia", "test-model")
        session.conversation.add_user_message("Persist me")
        with patch("pathlib.Path.home", return_value=self.workspace):
            session.save()
            sessions_dir = self.workspace / ".clawd" / "sessions"
            self.assertTrue((sessions_dir / f"{session.session_id}.json").exists())
            self.assertEqual(list(sessions_dir.glob("*.tmp")), [])
            reloaded = Session.load(session.session_id)
        self.assertIsNotNone(reloaded)
        self.assertEqual(reloaded.conversation.messages[0].content, "Persist me")

    def test_root_serves_browser_ui_without_exposing_state(self) -> None:
        with urllib.request.urlopen(f"{self.url}/", timeout=2) as response:
            body = response.read().decode("utf-8")
        self.assertNotIn("Connect to Clawd", body)
        self.assertIn("id=\"sidebar\"", body)
        self.assertIn("id=\"welcome\"", body)
        self.assertIn("id=\"newSession\"", body)
        self.assertIn("id=\"sessionsList\"", body)
        self.assertIn("className=\"markdown-body\"", body)
        self.assertIn("function renderMarkdown", body)
        self.assertIn("function renderTimelineItem", body)
        self.assertIn("Used", body)
        self.assertIn("function isTableSeparator", body)
        self.assertIn("activity-pulse", body)
        self.assertIn('activity==="typing"?"Typing":"Thinking"', body)
        self.assertNotIn("access token", body.lower())

    def test_chat_uses_existing_agent_loop_and_context(self) -> None:
        job = _Job(
            job_id="job",
            session_id=self.state.session.session_id,
            session=self.state.session,
            tool_context=ToolContext(workspace_root=self.workspace),
        )

        def run_loop(**kwargs):
            self.assertIs(kwargs["provider"], self.state.provider)
            self.assertIs(kwargs["tool_registry"], self.state.registry)
            self.assertIs(kwargs["tool_context"], job.tool_context)
            self.assertTrue(kwargs["stream"])
            kwargs["on_activity"]("thinking")
            self.assertEqual(job.activity, "thinking")
            kwargs["on_text_chunk"]("Hello from ")
            self.assertEqual(job.activity, "typing")
            kwargs["on_event"](
                ToolEvent(
                    kind="tool_use",
                    tool_name="Read",
                    tool_input={"file_path": "src/main.py"},
                    tool_use_id="read-1",
                )
            )
            self.assertEqual(job.activity, "tool")
            kwargs["on_event"](
                ToolEvent(
                    kind="tool_result",
                    tool_name="Read",
                    tool_output={"type": "text", "file": {"filePath": "src/main.py", "content": "print('hi')"}},
                    tool_use_id="read-1",
                )
            )
            self.assertEqual(job.activity, "tool")
            kwargs["on_activity"]("thinking")
            self.assertEqual(job.activity, "thinking")
            kwargs["on_text_chunk"]("the shared agent loop")
            self.assertEqual(job.activity, "typing")
            return SimpleNamespace(response_text="Hello from the shared agent loop")

        with patch("src.web_app.run_agent_loop", side_effect=run_loop):
            _run_job(self.state, job, "Hi")

        self.assertEqual(job.status, "completed")
        self.assertEqual(job.text, "Hello from the shared agent loop")
        self.assertEqual(job.activity, "")
        self.assertEqual(job.events[0]["kind"], "tool_use")
        self.assertEqual(job.events[0]["tool_name"], "Read")
        self.assertEqual(job.events[0]["tool_use_id"], "read-1")
        self.assertIn("src/main.py", job.events[0]["input"])
        self.assertEqual(job.events[1]["kind"], "tool_result")
        self.assertIn("print('hi')", job.events[1]["output"])
        self.assertEqual(
            [item["kind"] for item in job.timeline],
            ["text", "tool", "tool", "text"],
        )
        self.assertEqual(job.timeline[1]["event"]["tool_name"], "Read")
        self.assertEqual(job.timeline[2]["event"]["output"], job.events[1]["output"])
        self.assertEqual(self.state.session.conversation.messages[0].content, "Hi")

    def test_finished_turn_keeps_tool_activity_after_reload(self) -> None:
        session = self.state.session
        session.workspace = str(self.workspace)

        def run_loop(**kwargs):
            kwargs["on_event"](
                ToolEvent(
                    kind="tool_use",
                    tool_name="Read",
                    tool_input={"file_path": "src/main.py"},
                    tool_use_id="read-1",
                )
            )
            kwargs["on_event"](
                ToolEvent(
                    kind="tool_result",
                    tool_name="Read",
                    tool_output={"content": "print('hi')"},
                    tool_use_id="read-1",
                )
            )
            kwargs["on_event"](
                ToolEvent(
                    kind="tool_use",
                    tool_name="Edit",
                    tool_input={"file_path": "src/main.py"},
                    tool_use_id="edit-1",
                )
            )
            kwargs["on_event"](
                ToolEvent(
                    kind="tool_error",
                    tool_name="Edit",
                    tool_input={"file_path": "src/main.py"},
                    tool_use_id="edit-1",
                    is_error=True,
                    error="boom",
                )
            )
            kwargs["on_text_chunk"]("All done.")
            return SimpleNamespace(response_text="All done.")

        with patch("pathlib.Path.home", return_value=self.workspace), patch(
            "src.web_app.run_agent_loop", side_effect=run_loop
        ):
            job = _Job(
                job_id="job-tools",
                session_id=session.session_id,
                session=session,
                tool_context=ToolContext(workspace_root=self.workspace),
            )
            _run_job(self.state, job, "Read main.py")
            self.assertEqual(job.status, "completed")

            loaded = Session.load(session.session_id)
            self.assertIsNotNone(loaded)

        from src.web_app import _session_transcript

        transcript = _session_transcript(loaded)
        self.assertEqual(transcript[0], {"role": "user", "text": "Read main.py"})
        tool_events = [item["event"] for item in transcript if item.get("kind") == "tool"]
        self.assertEqual(
            [(event["kind"], event["tool_name"], event["tool_use_id"]) for event in tool_events],
            [
                ("tool_use", "Read", "read-1"),
                ("tool_result", "Read", "read-1"),
                ("tool_use", "Edit", "edit-1"),
                ("tool_error", "Edit", "edit-1"),
            ],
        )
        self.assertFalse(tool_events[1]["error"])
        self.assertTrue(tool_events[3]["error"])
        self.assertIn("print('hi')", tool_events[1]["output"])
        # The assistant reply still comes through as a text bubble.
        self.assertEqual(transcript[-1]["role"], "assistant")
        self.assertEqual(transcript[-1]["text"], "All done.")
        # One "Used N …" group per turn: replay keeps the turn's tools together.
        self.assertEqual(transcript[1]["kind"], "tool")

    def test_legacy_sessions_without_turn_timelines_still_transcribe(self) -> None:
        from src.web_app import _session_transcript

        session = self.state.session
        session.conversation.add_user_message("Old question")
        session.conversation.add_assistant_message("Old answer")
        self.assertEqual(
            _session_transcript(session),
            [
                {"role": "user", "text": "Old question"},
                {"role": "assistant", "text": "Old answer"},
            ],
        )

    def test_job_endpoint_includes_current_activity(self) -> None:
        timeline = [{"kind": "text", "text": "Working through the request."}]
        job = _Job(
            job_id="active",
            session_id=self.state.session.session_id,
            activity="typing",
            timeline=timeline,
        )
        self.state.jobs[job.job_id] = job
        request = urllib.request.Request(
            f"{self.url}/api/jobs/{job.job_id}",
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            payload = json.load(response)
        self.assertEqual(payload["activity"], "typing")
        self.assertEqual(payload["timeline"], timeline)

    def test_lock_refuses_a_second_holder_and_frees_on_release(self) -> None:
        from src.web_app import _acquire_instance_lock

        port = 61234
        first = _acquire_instance_lock(port)
        self.assertIsNotNone(first)
        try:
            second = _acquire_instance_lock(port)
            if second is not None:
                second.close()
                self.skipTest("This platform allows one process to double-lock a file.")
            self.assertIsNone(second)
        finally:
            close = getattr(first, "close", None)
            if close is not None:
                close()
        third = _acquire_instance_lock(port)
        self.assertIsNotNone(third)
        close = getattr(third, "close", None)
        if close is not None:
            close()

    def test_web_app_refuses_a_port_already_held(self) -> None:
        import io
        from src.web_app import _acquire_instance_lock, run_web_app

        port = 61235
        held = _acquire_instance_lock(port)
        if held is None:
            self.skipTest("This platform allows one process to double-lock a file.")
        try:
            with contextlib.redirect_stdout(io.StringIO()) as captured:
                code = run_web_app(self.workspace, port)
            self.assertEqual(code, 1)
            self.assertIn("already running on port", captured.getvalue())
        finally:
            close = getattr(held, "close", None)
            if close is not None:
                close()

    def test_web_app_still_starts_when_the_lock_cannot_be_created(self) -> None:
        import io
        from unittest.mock import patch as _patch
        from src.web_app import run_web_app

        with _patch("src.web_app._acquire_instance_lock", return_value=object()), \
                _patch("src.web_app._create_state", side_effect=ValueError("nope")), \
                contextlib.redirect_stdout(io.StringIO()) as captured:
            code = run_web_app(self.workspace, 61236)
        # A missing lock must never be what stops the server from booting.
        self.assertEqual(code, 1)
        self.assertIn("nope", captured.getvalue())

    def _post_json(self, path: str, payload: dict) -> dict:
        request = urllib.request.Request(
            f"{self.url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            return json.load(response)

    def _wait_until(self, condition, timeout: float = 3.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if condition():
                return True
            threading.Event().wait(0.01)
        return condition()

    def test_stop_endpoint_cancels_a_running_turn(self) -> None:
        started = threading.Event()

        def run_loop(**kwargs):
            kwargs["on_text_chunk"]("Partial ")
            started.set()
            deadline = time.time() + 3
            while not kwargs["should_stop"]() and time.time() < deadline:
                threading.Event().wait(0.01)
            raise AgentLoopCancelled("Partial ")

        with patch("pathlib.Path.home", return_value=self.workspace), patch(
            "src.web_app.run_agent_loop", side_effect=run_loop
        ):
            started_job = self._post_json(
                "/api/chat",
                {"session_id": self.state.session.session_id, "message": "Long running task"},
            )
            self.assertTrue(started.wait(timeout=2))
            job_id = started_job["job_id"]

            cancelled = self._post_json(f"/api/jobs/{job_id}/cancel", {})
            self.assertTrue(cancelled["ok"])

            self.assertTrue(self._wait_until(lambda: self.state.jobs[job_id].status == "stopped"))
            job = self.state.jobs[job_id]
            self.assertEqual(job.activity, "")
            self.assertEqual(job.text, "Partial ")
            self.assertTrue(job.cancel_event.is_set())
            self.assertTrue(
                (self.workspace / ".clawd" / "sessions" / f"{job.session_id}.json").exists()
            )

    def test_stop_endpoint_rejects_an_unknown_job(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self._post_json("/api/jobs/missing-job/cancel", {})
        self.assertEqual(raised.exception.code, 404)

    def test_stop_during_a_browser_interaction_unblocks_the_worker(self) -> None:
        started = threading.Event()

        def run_loop(**kwargs):
            started.set()
            with self.assertRaises(AgentLoopCancelled):
                kwargs["tool_context"].permission_handler("Bash", "Run a long command?", None)
            raise AgentLoopCancelled("Stopped during the permission prompt")

        with patch("pathlib.Path.home", return_value=self.workspace), patch(
            "src.web_app.run_agent_loop", side_effect=run_loop
        ):
            started_job = self._post_json(
                "/api/chat",
                {"session_id": self.state.session.session_id, "message": "Run something"},
            )
            self.assertTrue(started.wait(timeout=2))
            job_id = started_job["job_id"]
            self.assertTrue(
                self._wait_until(lambda: self.state.jobs[job_id].interaction is not None)
            )

            cancelled = self._post_json(f"/api/jobs/{job_id}/cancel", {})
            self.assertTrue(cancelled["ok"])

            self.assertTrue(self._wait_until(lambda: self.state.jobs[job_id].status == "stopped"))
            self.assertIsNone(self.state.jobs[job_id].interaction)

    def test_stopped_turn_frees_the_chat_for_the_next_message(self) -> None:
        session = self.state.session
        job = _Job(
            job_id="stopped-job",
            session_id=session.session_id,
            session=session,
            tool_context=ToolContext(workspace_root=self.workspace),
        )
        job.cancel_event.set()
        job.status = "stopped"
        self.state.jobs[job.job_id] = job

        def run_loop(**kwargs):
            kwargs["on_text_chunk"]("Follow-up reply")
            return SimpleNamespace(response_text="Follow-up reply")

        with patch("pathlib.Path.home", return_value=self.workspace), patch(
            "src.web_app.run_agent_loop", side_effect=run_loop
        ):
            started_job = self._post_json(
                "/api/chat",
                {"session_id": session.session_id, "message": "Follow-up message"},
            )
            self.assertIn("job_id", started_job)
            job_id = started_job["job_id"]
            self.assertTrue(self._wait_until(lambda: self.state.jobs[job_id].status == "completed"))

    def test_browser_ui_offers_a_stop_control_for_running_turns(self) -> None:
        with urllib.request.urlopen(f"{self.url}/", timeout=2) as response:
            body = response.read().decode("utf-8")
        self.assertIn('id="stop"', body)
        self.assertIn("/cancel", body)
        self.assertIn('j.status==="stopped"', body)
        self.assertIn('notice.textContent="Stopped."', body)

    def test_browser_ui_model_label_opens_settings_and_drops_redundant_chrome(self) -> None:
        with urllib.request.urlopen(f"{self.url}/", timeout=2) as response:
            body = response.read().decode("utf-8")
        # The composer model label is a button that opens the compact model picker.
        self.assertIn('class="composer-model" id="composerModel" type="button"', body)
        self.assertIn('document.querySelector("#composerModel").onclick=()=>openModelPicker()', body)
        self.assertIn('id="modelPicker"', body)
        self.assertIn("function openModelPicker", body)
        self.assertIn("function chooseModel", body)
        # The full settings dialog stays available behind the gear button only.
        self.assertIn('document.querySelector("#openSettings").onclick=openSettings', body)
        # Removed: topbar model text and assistant avatar squares.
        self.assertNotIn("top-model", body)
        self.assertNotIn('id="model"', body)
        self.assertNotIn("message-avatar", body)
        # The prompt box grows with content instead of showing a resize grabber.
        self.assertIn("#prompt{display:block;width:100%;min-height:44px;max-height:180px;resize:none", body)

    def test_dev_version_endpoint_reports_source_version_and_active_jobs(self) -> None:
        self.state.dev_reload_version = "source-version"
        job = _Job(job_id="active", session_id=self.state.session.session_id)
        self.state.jobs[job.job_id] = job
        with urllib.request.urlopen(f"{self.url}/api/dev-version", timeout=2) as response:
            payload = json.load(response)
        self.assertTrue(payload["enabled"])
        self.assertEqual(payload["version"], "source-version")
        self.assertTrue(payload["busy"])

        job.status = "completed"
        with urllib.request.urlopen(f"{self.url}/api/dev-version", timeout=2) as response:
            payload = json.load(response)
        self.assertFalse(payload["busy"])

    def test_source_signature_changes_when_source_content_changes(self) -> None:
        source_root = self.workspace / "src"
        source_root.mkdir()
        source_file = source_root / "web_app.py"
        source_file.write_text("VERSION = 1\n", encoding="utf-8")
        original = _source_signature(source_root)
        source_file.write_text("VERSION = 2\n", encoding="utf-8")
        self.assertNotEqual(_source_signature(source_root), original)

    def test_requests_do_not_write_to_stdout(self) -> None:
        # The browser UI polls constantly, so request logs would flood the
        # terminal. The handler must stay silent for every request.
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            with urllib.request.urlopen(f"{self.url}/api/state", timeout=2) as response:
                payload = json.load(response)
        self.assertEqual(payload["workspace"], str(self.workspace))
        self.assertEqual(captured.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
