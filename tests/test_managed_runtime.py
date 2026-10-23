"""Synthetic contract coverage for the managed runtime.

These tests exercise the installed runtime with native-shaped fixture payloads.
They deliberately do not assert desktop-host support.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shlex
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from contextlib import suppress
from pathlib import Path
from unittest import mock

from selc import managed_runtime, records
from selc.errors import SelcError
from selc.execution_lease import ProcessIdentity, assert_no_live_execution, process_group_is_live
from selc.install import install_suite
from selc.manifest import create_suite, seal_suite
from selc.native_adapter import _native_causation, receive_native_hook
from selc.records import close_task, delete_records, open_database, record_feedback

REPO_ROOT = Path(__file__).resolve().parents[1]


class ManagedRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.suite = self.root / "suite"
        create_suite("managed-reports", "1.0.0", self.suite, ["text-report"])
        manifest_path = self.suite / "suite.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["artifact_checks"] = {
            "managed-check": {
                "checker": {"name": "json-item-summary", "version": "1"},
                "rule_version": "summary-v1",
                "required_rule_ids": ["count", "sum"],
                "configuration": {"duplicate_ids_count_once": False},
            }
        }
        manifest["skills"][0]["artifact_check"] = "managed-check"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        seal_suite(self.suite)
        hooks_path = self.project / ".codex" / "hooks.json"
        hooks_path.parent.mkdir(parents=True)
        hooks_path.write_text(
            json.dumps({"hooks": {"Stop": [{"command": "echo preserved"}]}}),
            encoding="utf-8",
        )
        self.installation = install_suite(
            source=self.suite,
            project_root=self.project,
            harness="codex",
            ref=None,
        )
        self.input_path = self.root / "input.txt"
        self.input_path.write_text("first\nsecond\n", encoding="utf-8")
        self.transcript = self.project / "session.jsonl"
        self.transcript.write_bytes(b"")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_installed_stub_preserves_the_original_skill_and_unrelated_hooks(self) -> None:
        hooks_path = self.project / ".codex" / "hooks.json"
        hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
        self.assertEqual(hooks["hooks"]["Stop"][0], {"command": "echo preserved"})
        for event_name in (
            "UserPromptSubmit",
            "PreToolUse",
            "PostToolUse",
            "PermissionRequest",
            "Stop",
        ):
            entries = hooks["hooks"][event_name]
            managed_entries = [entry for entry in entries if "hooks" in entry]
            self.assertEqual(len(managed_entries), 1)
            command = managed_entries[0]["hooks"][0]["command"]
            self.assertIn("managed hook", command)
            self.assertNotIn("updatedInput", json.dumps(managed_entries[0]))
            self.assertNotIn("permission", json.dumps(managed_entries[0]).lower())

        entry = self.project / ".agents" / "skills" / "text-report" / "SKILL.md"
        original = self.suite / "skills" / "text-report" / "SKILL.md"
        self.assertTrue(entry.is_file())
        self.assertNotEqual(entry.read_bytes(), original.read_bytes())
        entry_text = entry.read_text(encoding="utf-8")
        self.assertIn("managed invoke", entry_text)
        self.assertNotIn("--receipt", entry_text)
        self.assertEqual(
            original.read_text(encoding="utf-8"),
            (
                self.project
                / ".selc"
                / "installs"
                / "managed-reports"
                / "current"
                / "skills"
                / "text-report"
                / "SKILL.md"
            ).read_text(encoding="utf-8"),
        )

    def test_workbuddy_registers_the_permission_denied_collector(self) -> None:
        install_suite(
            source=self.suite,
            project_root=self.project,
            harness="workbuddy",
            ref=None,
        )

        settings_path = self.project / ".codebuddy" / "settings.json"
        hooks = json.loads(settings_path.read_text(encoding="utf-8"))["hooks"]
        for event_name in ("PermissionRequest", "PermissionDenied"):
            managed_entries = [entry for entry in hooks[event_name] if "hooks" in entry]
            self.assertEqual(len(managed_entries), 1)
            self.assertIn("managed hook", managed_entries[0]["hooks"][0]["command"])

    def test_hook_update_preserves_an_unrelated_command_in_a_shared_hook_entry(self) -> None:
        hooks_path = self.project / ".codex" / "hooks.json"
        hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
        owned_command = hooks["hooks"]["Stop"][-1]["hooks"][0]["command"]
        hooks["hooks"]["Stop"][-1] = {
            "hooks": [{"command": "echo keep-me"}, {"command": owned_command}]
        }
        hooks_path.write_text(json.dumps(hooks), encoding="utf-8")

        install_suite(source=self.suite, project_root=self.project, harness="codex", ref=None)

        updated = json.loads(hooks_path.read_text(encoding="utf-8"))
        commands = [
            item["command"] for entry in updated["hooks"]["Stop"] for item in entry.get("hooks", [])
        ]
        self.assertIn("echo keep-me", commands)
        self.assertIn(owned_command, commands)

    def test_shell_parser_requires_the_current_installed_wrapper_and_project(self) -> None:
        entry = self.project / ".agents" / "skills" / "text-report" / "SKILL.md"
        command = next(
            line
            for line in entry.read_text(encoding="utf-8").splitlines()
            if "managed invoke" in line
        )
        parsed = managed_runtime.parse_installed_managed_command(
            command,
            "managed-reports",
            project_root=self.project,
        )
        self.assertIsNotNone(parsed)

        impostor = shlex.split(command)
        impostor[1] = str(self.root / "impostor" / "selc-suite.py")
        self.assertIsNone(
            managed_runtime.parse_installed_managed_command(
                shlex.join(impostor),
                "managed-reports",
                project_root=self.project,
            )
        )

    def test_installed_wrapper_completes_a_normal_shell_flow_without_receipts(self) -> None:
        """The generated wrapper selects each native receipt from child identity."""

        manifest_path = self.suite / "suite.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["managed"]["native_hosts"] = {"codex": ["0.163.0-alpha.10.2"]}
        manifest["managed"]["actions"]["text-report"] = {
            "skill": "text-report",
            "kind": "python-script-v1",
            "script": "bin/json-summary.py",
            "input": {"fields": {"input": {"type": "path"}}, "required": ["input"]},
        }
        (self.suite / "bin" / "json-summary.py").write_text(
            "import argparse\n"
            "import json\n"
            "from pathlib import Path\n"
            "parser = argparse.ArgumentParser()\n"
            "parser.add_argument('command')\n"
            "parser.add_argument('--request', required=True)\n"
            "parser.add_argument('--output', required=True)\n"
            "arguments = parser.parse_args()\n"
            "if arguments.command != 'managed-run':\n"
            "    raise SystemExit(2)\n"
            "request = json.loads(Path(arguments.request).read_text(encoding='utf-8'))\n"
            "items = json.loads(Path(request['input']).read_text(encoding='utf-8'))['items']\n"
            "artifact = {'count': len(items), 'sum': sum(item['value'] for item in items)}\n"
            "Path(arguments.output).write_text(json.dumps(artifact), encoding='utf-8')\n",
            encoding="utf-8",
        )
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        seal_suite(self.suite)
        self.installation = install_suite(
            source=self.suite,
            project_root=self.project,
            harness="codex",
            ref=None,
        )

        session_id = "session-automatic"
        turn_id = "turn-automatic"
        automatic_input = self.root / "automatic-items.json"
        automatic_input.write_text(
            json.dumps({"items": [{"id": "first", "value": 2}, {"id": "second", "value": 3}]}),
            encoding="utf-8",
        )
        prompt_text = "@text-report " + json.dumps({"input": str(automatic_input)})
        history = self.root / ".codex" / "sessions" / "2026" / f"{session_id}.jsonl"
        history.parent.mkdir(parents=True)
        initial_history = [
            {
                "type": "session_meta",
                "payload": {
                    "id": session_id,
                    "session_id": session_id,
                    "cwd": str(self.project.resolve()),
                    "cli_version": "0.163.0-alpha.10.2",
                    "source": "exec",
                    "history_mode": "paginated",
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "task_started",
                    "turn_id": turn_id,
                    "root_turn_id": turn_id,
                    "turn_attribution": {
                        "turn_id": turn_id,
                        "root_turn_id": turn_id,
                        "parent_turn_id": None,
                    },
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "developer",
                    "internal_chat_message_metadata_passthrough": {
                        "turn_id": turn_id,
                        "content_item_kinds": [
                            "host_skills.instructions",
                            "permissions.instructions",
                        ],
                    },
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "developer",
                    "internal_chat_message_metadata_passthrough": {
                        "turn_id": turn_id,
                        "content_item_kinds": ["multi_agent.usage_hint"],
                    },
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "developer",
                    "internal_chat_message_metadata_passthrough": {
                        "turn_id": turn_id,
                        "content_item_kinds": ["multi_agent.mode_instructions"],
                    },
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "installed collector instructions"},
                        {"type": "input_text", "text": "workspace context"},
                    ],
                    "internal_chat_message_metadata_passthrough": {
                        "turn_id": turn_id,
                        "content_item_kinds": [
                            "agents_md.instructions",
                            "environments.environment_context",
                        ],
                    },
                },
            },
            {"type": "world_state", "payload": {"full": True, "state": {}}},
            {
                "type": "turn_context",
                "payload": {
                    "turn_id": turn_id,
                    "root_turn_id": turn_id,
                    "cwd": str(self.project.resolve()),
                },
            },
            {
                "type": "event_msg",
                "payload": {"type": "thread_settings_applied", "thread_id": session_id},
            },
        ]
        metadata = b"".join(
            (json.dumps(record) + "\n").encode("utf-8") for record in initial_history
        )
        history.write_bytes(metadata)

        wrapper = (
            self.project
            / ".selc"
            / "installs"
            / "managed-reports"
            / "current"
            / "bin"
            / "selc-suite.py"
        )
        base_environment = {
            **os.environ,
            "CODEX_SESSION_ID": session_id,
            "CODEX_THREAD_ID": session_id,
            "CODEX_VERSION": "0.163.0-alpha.10.2",
        }
        base_environment.pop("CODEX_TOOL_CALL_ID", None)

        def receive(event: dict[str, object]) -> dict[str, object]:
            with (
                mock.patch("selc.managed_runtime.Path.home", return_value=self.root),
                mock.patch("selc.native_adapter.Path.home", return_value=self.root),
            ):
                return receive_native_hook(
                    project_root=self.project,
                    suite_id="managed-reports",
                    host="codex",
                    payload=event,
                    environment=base_environment,
                )

        def run(command: list[str], tool_use_id: str) -> dict[str, object]:
            completed = subprocess.run(
                command,
                env={**base_environment, "CODEX_TOOL_CALL_ID": tool_use_id},
                capture_output=True,
                check=False,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
            return json.loads(completed.stdout)

        prompt = {
            "hook_event_name": "UserPromptSubmit",
            "cwd": str(self.project),
            "session_id": session_id,
            "turn_id": turn_id,
            "transcript_path": str(history),
            "prefix_length": len(metadata),
            "prompt": prompt_text,
        }
        self.assertEqual(receive(prompt)["status"], "prompt_recorded")

        database = open_database(self.project)
        try:
            root_valid = database.execute("SELECT root_valid FROM managed_contexts").fetchone()[0]
        finally:
            database.close()
        self.assertEqual(root_valid, 0)

        appended_history = [
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": prompt_text}],
                    "internal_chat_message_metadata_passthrough": {
                        "turn_id": turn_id,
                        "content_item_kinds": ["user.text"],
                    },
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "thread_id": session_id,
                    "turn_id": turn_id,
                    "item": {"type": "UserMessage"},
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "thread_id": session_id,
                    "turn_id": turn_id,
                    "item": {"type": "Reasoning"},
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "reasoning",
                    "internal_chat_message_metadata_passthrough": {"turn_id": turn_id},
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "thread_id": session_id,
                    "turn_id": turn_id,
                    "item": {
                        "type": "AgentMessage",
                        "phase": "commentary",
                        "content": [{"type": "Text", "text": "Starting the managed work."}],
                    },
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "phase": "commentary",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": "Starting the managed work."}],
                    "internal_chat_message_metadata_passthrough": {
                        "turn_id": turn_id,
                        "content_item_kinds": ["unknown"],
                    },
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "internal_chat_message_metadata_passthrough": {"turn_id": turn_id},
                },
            },
            {
                "type": "token_usage_record",
                "payload": {
                    "session_id": session_id,
                    "thread_id": session_id,
                    "turn_id": turn_id,
                    "root_turn_id": turn_id,
                },
            },
        ]
        with history.open("ab") as stream:
            for record in appended_history:
                stream.write((json.dumps(record) + "\n").encode("utf-8"))

        invoke_command = [
            sys.executable,
            str(wrapper),
            "managed",
            "invoke",
            "--project-root",
            str(self.project),
            "--suite",
            "managed-reports",
            "--skill",
            "text-report",
            "--host",
            "codex",
            "--expected-version",
            str(self.installation["version"]),
            "--expected-composite",
            str(self.installation["digest"]),
        ]
        receive(
            {
                "hook_event_name": "PreToolUse",
                "cwd": str(self.project),
                "session_id": session_id,
                "turn_id": turn_id,
                "tool_call_id": "tool-automatic-invoke",
                "tool_name": "Bash",
                "tool_input": {"command": shlex.join(invoke_command)},
                "transcript_path": str(history),
                "prefix_length": history.stat().st_size,
            }
        )
        activated = run(invoke_command, "tool-automatic-invoke")["managed"]

        request_path = self.root / "automatic-request.json"
        request_path.write_text(json.dumps({"input": str(automatic_input)}), encoding="utf-8")
        action_command = [
            sys.executable,
            str(wrapper),
            "managed",
            "action",
            "--project-root",
            str(self.project),
            "--suite",
            "managed-reports",
            "--skill",
            "text-report",
            "--task-id",
            str(activated["task_id"]),
            "--call-id",
            str(activated["call_id"]),
            "--action",
            "text-report",
            "--request",
            str(request_path),
            "--host",
            "codex",
            "--expected-version",
            str(self.installation["version"]),
            "--expected-composite",
            str(self.installation["digest"]),
        ]
        receive(
            {
                "hook_event_name": "PreToolUse",
                "cwd": str(self.project),
                "session_id": session_id,
                "turn_id": turn_id,
                "tool_call_id": "tool-automatic-action",
                "tool_name": "Bash",
                "tool_input": {"command": shlex.join(action_command)},
                "transcript_path": str(history),
                "prefix_length": history.stat().st_size,
            }
        )
        action = run(action_command, "tool-automatic-action")["managed"]

        publish_command = [
            sys.executable,
            str(wrapper),
            "managed",
            "publish",
            "--project-root",
            str(self.project),
            "--suite",
            "managed-reports",
            "--skill",
            "text-report",
            "--task-id",
            str(activated["task_id"]),
            "--call-id",
            str(activated["call_id"]),
            "--artifact",
            str(action["artifact_path"]),
            "--target",
            "reports/automatic.md",
            "--host",
            "codex",
            "--expected-version",
            str(self.installation["version"]),
            "--expected-composite",
            str(self.installation["digest"]),
        ]
        receive(
            {
                "hook_event_name": "PreToolUse",
                "cwd": str(self.project),
                "session_id": session_id,
                "turn_id": turn_id,
                "tool_call_id": "tool-automatic-publish",
                "tool_name": "Bash",
                "tool_input": {"command": shlex.join(publish_command)},
                "transcript_path": str(history),
                "prefix_length": history.stat().st_size,
            }
        )
        published = run(publish_command, "tool-automatic-publish")["managed"]
        self.assertEqual(published["status"], "published")
        self.assertTrue((self.project / "reports" / "automatic.md").is_file())

        receive(
            {
                "hook_event_name": "PostToolUse",
                "cwd": str(self.project),
                "session_id": session_id,
                "turn_id": turn_id,
                "tool_call_id": "tool-automatic-publish",
                "tool_name": "Bash",
                "tool_input": {"command": shlex.join(publish_command)},
                "transcript_path": str(history),
                "prefix_length": history.stat().st_size,
            }
        )
        with history.open("ab") as stream:
            stream.write(
                (
                    json.dumps(
                        {
                            "type": "event_msg",
                            "payload": {
                                "type": "item_completed",
                                "thread_id": session_id,
                                "turn_id": turn_id,
                                "item": {
                                    "type": "CommandExecution",
                                    "id": "tool-automatic-publish",
                                    "command": ["/bin/zsh", "-lc", shlex.join(publish_command)],
                                    "cwd": str(self.project.resolve()),
                                    "status": "completed",
                                    "exit_code": 0,
                                },
                            },
                        }
                    )
                    + "\n"
                ).encode("utf-8")
            )
        stopped = receive(
            {
                "hook_event_name": "Stop",
                "cwd": str(self.project),
                "session_id": session_id,
                "turn_id": turn_id,
                "transcript_path": str(history),
                "prefix_length": history.stat().st_size,
            }
        )
        self.assertEqual(stopped["status"], "stop_recorded")
        health = managed_runtime.health(
            self.project,
            "managed-reports",
            str(activated["task_id"]),
        )
        database = open_database(self.project)
        try:
            task_status = database.execute(
                "SELECT status FROM tasks WHERE task_id = ?", (activated["task_id"],)
            ).fetchone()["status"]
            call_status = database.execute(
                "SELECT status FROM calls WHERE call_id = ?", (activated["call_id"],)
            ).fetchone()["status"]
        finally:
            database.close()
        self.assertEqual((health["first_result"], health["final_result"]), ("pass", "pass"))
        self.assertEqual(health["evidence"]["windows_closed"], 1)
        self.assertEqual(task_status, "closed")
        self.assertEqual(call_status, "closed")

    def test_codex_unmanaged_pre_promotes_prompt_anchor_without_issuing_a_receipt(self) -> None:
        """A discovery command can prove P1 without becoming a managed operation."""

        manifest_path = self.suite / "suite.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["managed"]["native_hosts"] = {"codex": ["0.163.0-alpha.10.2"]}
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        seal_suite(self.suite)
        self.installation = install_suite(
            source=self.suite,
            project_root=self.project,
            harness="codex",
            ref=None,
        )

        session_id = "session-unmanaged-anchor"
        turn_id = "turn-unmanaged-anchor"
        prompt_text = "@text-report " + json.dumps({"input": str(self.input_path)})
        history = self.root / ".codex" / "sessions" / "2026" / "unmanaged-anchor.jsonl"
        history.parent.mkdir(parents=True)
        initial_history = [
            {
                "type": "session_meta",
                "payload": {
                    "id": session_id,
                    "session_id": session_id,
                    "cwd": str(self.project.resolve()),
                    "cli_version": "0.163.0-alpha.10.2",
                    "source": "exec",
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "task_started",
                    "turn_id": turn_id,
                    "root_turn_id": turn_id,
                    "turn_attribution": {
                        "turn_id": turn_id,
                        "root_turn_id": turn_id,
                        "parent_turn_id": None,
                    },
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "developer",
                    "internal_chat_message_metadata_passthrough": {
                        "turn_id": turn_id,
                        "content_item_kinds": ["host_skills.instructions"],
                    },
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "injected context"}],
                    "internal_chat_message_metadata_passthrough": {
                        "turn_id": turn_id,
                        "content_item_kinds": ["agents_md.instructions"],
                    },
                },
            },
            {"type": "world_state", "payload": {"full": True, "state": {}}},
            {
                "type": "turn_context",
                "payload": {
                    "turn_id": turn_id,
                    "root_turn_id": turn_id,
                    "cwd": str(self.project.resolve()),
                },
            },
            {
                "type": "event_msg",
                "payload": {"type": "thread_settings_applied", "thread_id": session_id},
            },
        ]
        history.write_bytes(
            b"".join((json.dumps(record) + "\n").encode("utf-8") for record in initial_history)
        )

        base_environment = {
            **os.environ,
            "CODEX_SESSION_ID": session_id,
            "CODEX_THREAD_ID": session_id,
            "CODEX_VERSION": "0.163.0-alpha.10.2",
        }

        def receive(payload: dict[str, object], tool_use_id: str = "") -> dict[str, object]:
            environment = dict(base_environment)
            if tool_use_id:
                environment["CODEX_TOOL_CALL_ID"] = tool_use_id
            with (
                mock.patch("selc.managed_runtime.Path.home", return_value=self.root),
                mock.patch("selc.native_adapter.Path.home", return_value=self.root),
            ):
                return receive_native_hook(
                    project_root=self.project,
                    suite_id="managed-reports",
                    host="codex",
                    payload=payload,
                    environment=environment,
                )

        def tool_callback(
            event_name: str,
            *,
            tool_use_id: str,
            command: str,
        ) -> dict[str, object]:
            return {
                "hook_event_name": event_name,
                "cwd": str(self.project),
                "session_id": session_id,
                "turn_id": turn_id,
                "tool_call_id": tool_use_id,
                "tool_name": "Bash",
                "tool_input": {"command": command},
                "transcript_path": str(history),
                "prefix_length": history.stat().st_size,
            }

        prompt = {
            "hook_event_name": "UserPromptSubmit",
            "cwd": str(self.project),
            "session_id": session_id,
            "turn_id": turn_id,
            "prompt": prompt_text,
            "transcript_path": str(history),
            "prefix_length": history.stat().st_size,
        }
        self.assertEqual(receive(prompt)["status"], "prompt_recorded")
        with history.open("ab") as stream:
            stream.write(
                (
                    json.dumps(
                        {
                            "type": "response_item",
                            "payload": {
                                "type": "message",
                                "role": "user",
                                "content": [{"type": "input_text", "text": prompt_text}],
                                "internal_chat_message_metadata_passthrough": {
                                    "turn_id": turn_id,
                                    "content_item_kinds": ["user.text"],
                                },
                            },
                        }
                    )
                    + "\n"
                ).encode("utf-8")
            )

        discovery_command = "pwd && sed -n '1p' .agents/skills/text-report/SKILL.md"
        discovery_tool = "tool-unmanaged-discovery"
        discovery_pre = receive(
            tool_callback(
                "PreToolUse",
                tool_use_id=discovery_tool,
                command=discovery_command,
            ),
            discovery_tool,
        )
        self.assertEqual(discovery_pre["status"], "recorded_unusable")
        self.assertEqual(
            receive(
                tool_callback(
                    "PostToolUse",
                    tool_use_id=discovery_tool,
                    command=discovery_command,
                ),
                discovery_tool,
            )["status"],
            "post_recorded",
        )
        with history.open("ab") as stream:
            stream.write(
                (
                    json.dumps(
                        {
                            "type": "event_msg",
                            "payload": {
                                "type": "item_completed",
                                "thread_id": session_id,
                                "turn_id": turn_id,
                                "item": {
                                    "type": "CommandExecution",
                                    "id": discovery_tool,
                                    "command": ["/bin/zsh", "-lc", discovery_command],
                                    "cwd": str(self.project.resolve()),
                                    "status": "completed",
                                    "exit_code": 0,
                                },
                            },
                        }
                    )
                    + "\n"
                ).encode("utf-8")
            )

        database = open_database(self.project)
        try:
            context = database.execute(
                "SELECT root_valid, input_skill, unknown_change FROM managed_contexts"
            ).fetchone()
            counts = tuple(
                database.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("managed_receipts", "tasks", "calls")
            )
        finally:
            database.close()
        self.assertEqual(tuple(context), (1, "text-report", 1))
        self.assertEqual(counts, (0, 0, 0))

        wrapper = (
            self.project
            / ".selc"
            / "installs"
            / "managed-reports"
            / "current"
            / "bin"
            / "selc-suite.py"
        )
        invoke_command = [
            sys.executable,
            str(wrapper),
            "managed",
            "invoke",
            "--project-root",
            str(self.project),
            "--suite",
            "managed-reports",
            "--skill",
            "text-report",
            "--host",
            "codex",
            "--expected-version",
            str(self.installation["version"]),
            "--expected-composite",
            str(self.installation["digest"]),
        ]
        wrong_skill_command = list(invoke_command)
        wrong_skill_command[wrong_skill_command.index("--skill") + 1] = "wrong-skill"
        wrong_tool = "tool-wrong-skill"
        with self.assertRaises(SelcError) as captured:
            receive(
                tool_callback(
                    "PreToolUse",
                    tool_use_id=wrong_tool,
                    command=shlex.join(wrong_skill_command),
                ),
                wrong_tool,
            )
        self.assertEqual(captured.exception.code, "NATIVE_REQUEST_UNAVAILABLE")

        invoke_tool = "tool-managed-invoke"
        managed_pre = receive(
            tool_callback(
                "PreToolUse",
                tool_use_id=invoke_tool,
                command=shlex.join(invoke_command),
            ),
            invoke_tool,
        )
        self.assertEqual(managed_pre["status"], "recorded")
        completed = subprocess.run(
            invoke_command,
            env={**base_environment, "CODEX_TOOL_CALL_ID": invoke_tool},
            capture_output=True,
            check=False,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        activated = json.loads(completed.stdout)["managed"]
        self.assertTrue(activated["task_id"])
        self.assertTrue(activated["call_id"])

    def test_workbuddy_wrapper_flow_closes_post_before_result_and_links_correction(self) -> None:
        """Exercise the installed WorkBuddy path with bounded native-shaped history."""

        manifest_path = self.suite / "suite.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["managed"]["native_hosts"] = {"workbuddy": ["5.8.3"]}
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        seal_suite(self.suite)
        self.installation = install_suite(
            source=self.suite,
            project_root=self.project,
            harness="workbuddy",
            ref=None,
        )

        session_id = "workbuddy-wrapper-session"
        project_cwd = str(self.project.resolve())
        compressed_cwd = project_cwd.lstrip("/").replace("/", "-")
        history = self.root / ".codebuddy" / "projects" / compressed_cwd / f"{session_id}.jsonl"
        history.parent.mkdir(parents=True)
        metadata = [
            {"type": "session-meta", "sessionId": session_id},
            {"type": "session-meta", "sessionId": session_id, "cwd": project_cwd},
        ]
        history.write_bytes(
            b"".join((json.dumps(record) + "\n").encode("utf-8") for record in metadata)
        )

        wrapper = (
            self.project
            / ".selc"
            / "installs"
            / "managed-reports"
            / "current"
            / "bin"
            / "selc-suite.py"
        )
        base_environment = {
            **os.environ,
            "CODEBUDDY_SESSION_ID": session_id,
            "CODEBUDDY_CONVERSATION_MESSAGE_ID": "workbuddy-message",
        }

        def receive(payload: dict[str, object]) -> dict[str, object]:
            with mock.patch("selc.managed_runtime.Path.home", return_value=self.root):
                return receive_native_hook(
                    project_root=self.project,
                    suite_id="managed-reports",
                    host="workbuddy",
                    payload=payload,
                    environment=base_environment,
                )

        def callback(
            event_name: str,
            *,
            request_id: str,
            generation_id: str,
            message_id: str,
            tool_id: str | None = None,
            command: list[str] | None = None,
            prompt: str | None = None,
        ) -> dict[str, object]:
            payload: dict[str, object] = {
                "hook_event_name": event_name,
                "client": "WorkBuddy",
                "version": "5.8.3",
                "cwd": project_cwd,
                "session_id": session_id,
                "generation_id": generation_id,
                "conversation_request_id": request_id,
                "conversation_message_id": message_id,
                "transcript_path": str(history),
                "prefix_length": history.stat().st_size,
            }
            if prompt is not None:
                payload["prompt"] = prompt
            if tool_id is not None:
                payload["tool_call_id"] = tool_id
            if command is not None:
                payload["tool_name"] = "Bash"
                payload["tool_input"] = {"command": shlex.join(command)}
            return payload

        def wrapper_command(
            operation: str,
            arguments: list[str],
        ) -> list[str]:
            return [
                sys.executable,
                str(wrapper),
                "managed",
                operation,
                "--project-root",
                str(self.project),
                "--suite",
                "managed-reports",
                *arguments,
                "--host",
                "workbuddy",
                "--expected-version",
                str(self.installation["version"]),
                "--expected-composite",
                str(self.installation["digest"]),
            ]

        def run(command: list[str], *, request_id: str, tool_id: str) -> dict[str, object]:
            completed = subprocess.run(
                command,
                env={
                    **base_environment,
                    "CODEBUDDY_CONVERSATION_REQUEST_ID": request_id,
                    "CODEBUDDY_TOOL_CALL_ID": tool_id,
                },
                capture_output=True,
                check=False,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
            return json.loads(completed.stdout)["managed"]

        def append(record: dict[str, object]) -> None:
            with history.open("ab") as stream:
                stream.write((json.dumps(record) + "\n").encode("utf-8"))

        def append_result(
            *,
            request_id: str,
            tool_id: str,
            command: list[str],
            parent_id: str,
        ) -> str:
            call_id = f"native-{tool_id}"
            append(
                {
                    "id": call_id,
                    "type": "function_call",
                    "callId": tool_id,
                    "parentId": parent_id,
                    "sessionId": session_id,
                    "cwd": project_cwd,
                    "providerData": {"conversationRequestId": request_id},
                    "arguments": json.dumps(
                        {"command": shlex.join(command), "description": "managed publish"}
                    ),
                }
            )
            result_id = f"native-result-{tool_id}"
            append(
                {
                    "id": result_id,
                    "type": "function_call_result",
                    "callId": tool_id,
                    "parentId": call_id,
                    "sessionId": session_id,
                    "cwd": project_cwd,
                    "providerData": {"conversationRequestId": request_id},
                    "output": {"exit_code": 0},
                }
            )
            return result_id

        def run_turn(
            *,
            request_id: str,
            generation_id: str,
            user_id: str,
            parent_id: str | None,
            prompt: str,
            target: str,
        ) -> tuple[dict[str, object], str]:
            self.assertEqual(
                receive(
                    callback(
                        "UserPromptSubmit",
                        request_id=request_id,
                        generation_id=generation_id,
                        message_id=user_id,
                        prompt=prompt,
                    )
                )["status"],
                "prompt_recorded",
            )
            append(
                {
                    "id": user_id,
                    "type": "message",
                    "role": "user",
                    "parentId": parent_id,
                    "sessionId": session_id,
                    "cwd": project_cwd,
                    "providerData": {
                        "startsNewUserRequest": True,
                        "conversationRequestId": request_id,
                    },
                }
            )

            invoke_tool = f"{request_id}-invoke"
            invoke = wrapper_command("invoke", ["--skill", "text-report"])
            self.assertEqual(
                receive(
                    callback(
                        "PreToolUse",
                        request_id=request_id,
                        generation_id=generation_id,
                        message_id=user_id,
                        tool_id=invoke_tool,
                        command=invoke,
                    )
                )["status"],
                "recorded",
            )
            activated = run(invoke, request_id=request_id, tool_id=invoke_tool)

            request_path = self.root / f"{request_id}-action.json"
            request_path.write_text(json.dumps({"input": str(self.input_path)}), encoding="utf-8")
            action_tool = f"{request_id}-action"
            action_command = wrapper_command(
                "action",
                [
                    "--skill",
                    "text-report",
                    "--task-id",
                    str(activated["task_id"]),
                    "--call-id",
                    str(activated["call_id"]),
                    "--action",
                    "text-report",
                    "--request",
                    str(request_path),
                ],
            )
            self.assertEqual(
                receive(
                    callback(
                        "PreToolUse",
                        request_id=request_id,
                        generation_id=generation_id,
                        message_id=user_id,
                        tool_id=action_tool,
                        command=action_command,
                    )
                )["status"],
                "recorded",
            )
            action = run(action_command, request_id=request_id, tool_id=action_tool)

            publish_tool = f"{request_id}-publish"
            publish_command = wrapper_command(
                "publish",
                [
                    "--skill",
                    "text-report",
                    "--task-id",
                    str(activated["task_id"]),
                    "--call-id",
                    str(activated["call_id"]),
                    "--artifact",
                    str(action["artifact_path"]),
                    "--target",
                    target,
                ],
            )
            self.assertEqual(
                receive(
                    callback(
                        "PreToolUse",
                        request_id=request_id,
                        generation_id=generation_id,
                        message_id=user_id,
                        tool_id=publish_tool,
                        command=publish_command,
                    )
                )["status"],
                "recorded",
            )
            published = run(publish_command, request_id=request_id, tool_id=publish_tool)
            self.assertEqual(published["status"], "published")

            post = receive(
                callback(
                    "PostToolUse",
                    request_id=request_id,
                    generation_id=generation_id,
                    message_id=user_id,
                    tool_id=publish_tool,
                    command=publish_command,
                )
            )
            self.assertEqual(post["status"], "post_recorded")
            result_id = append_result(
                request_id=request_id,
                tool_id=publish_tool,
                command=publish_command,
                parent_id=user_id,
            )
            stopped = receive(
                callback(
                    "Stop",
                    request_id=request_id,
                    generation_id=generation_id,
                    message_id=user_id,
                )
            )
            self.assertEqual(stopped["status"], "stop_recorded")
            return activated, result_id

        first, first_result_id = run_turn(
            request_id="workbuddy-request-one",
            generation_id="workbuddy-generation-one",
            user_id="workbuddy-user-one",
            parent_id=None,
            prompt="Create the requested report from the available input.",
            target="reports/workbuddy-one.md",
        )
        correction, _ = run_turn(
            request_id="workbuddy-request-two",
            generation_id="workbuddy-generation-two",
            user_id="workbuddy-user-two",
            parent_id=first_result_id,
            prompt="纠正上一份报告",
            target="reports/workbuddy-two.md",
        )
        self.assertEqual(correction["task_id"], first["task_id"])
        self.assertNotEqual(correction["call_id"], first["call_id"])

        database = open_database(self.project)
        try:
            windows_closed = database.execute(
                "SELECT COUNT(*) FROM managed_windows WHERE status = 'closed'"
            ).fetchone()[0]
            windows = [
                dict(row)
                for row in database.execute(
                    "SELECT request_id, tool_use_id, status FROM managed_windows ORDER BY request_id"
                )
            ]
            statuses = {
                row["status"]
                for row in database.execute(
                    "SELECT status FROM calls WHERE task_id = ?", (first["task_id"],)
                )
            }
        finally:
            database.close()
        self.assertEqual(windows_closed, 2, windows)
        self.assertEqual(statuses, {"closed"})

    def test_synthetic_journey_records_automatically_and_requires_post_closure(self) -> None:
        activated = self._activate()
        action_receipt = self._observe("text-report", "tool-action")
        action = managed_runtime.run_action(
            project_root=self.project,
            suite_id="managed-reports",
            skill="text-report",
            task_id=activated["task_id"],
            call_id=activated["call_id"],
            action_id="text-report",
            receipt=action_receipt,
            request={"input": str(self.input_path)},
        )
        publish_receipt = self._observe("publish", "tool-publish")
        published = managed_runtime.publish(
            project_root=self.project,
            suite_id="managed-reports",
            task_id=activated["task_id"],
            call_id=activated["call_id"],
            receipt=publish_receipt,
            artifact_path=Path(action["artifact_path"]),
            target="reports/out.md",
        )
        self.assertEqual(published["status"], "published")
        before_post = managed_runtime.health(self.project, "managed-reports", activated["task_id"])
        self.assertEqual(before_post["first_result"], "unknown")

        self._observe("publish", "tool-publish", phase="PostToolUse")
        after_post = managed_runtime.health(self.project, "managed-reports", activated["task_id"])
        self.assertEqual(after_post["final_result"], "unknown")
        self.assertEqual(after_post["first_result"], "unknown")

        database = open_database(self.project)
        try:
            status_before_stop = database.execute(
                "SELECT status FROM calls WHERE call_id = ?", (activated["call_id"],)
            ).fetchone()["status"]
        finally:
            database.close()
        self.assertEqual(status_before_stop, "active")

        stopped = self._stop("tool-publish")
        self.assertEqual(stopped["status"], "stop_recorded")

        database = open_database(self.project)
        try:
            self.assertEqual(database.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 1)
            self.assertEqual(database.execute("SELECT COUNT(*) FROM calls").fetchone()[0], 1)
            self.assertEqual(
                database.execute(
                    "SELECT status FROM calls WHERE call_id = ?", (activated["call_id"],)
                ).fetchone()["status"],
                "closed",
            )
            self.assertEqual(database.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0], 1)
            self.assertEqual(database.execute("SELECT COUNT(*) FROM checker_runs").fetchone()[0], 1)
            native = database.execute("SELECT payload_json FROM managed_native_events").fetchall()
        finally:
            database.close()
        self.assertTrue(native)
        self.assertNotIn(str(self.input_path), "".join(row["payload_json"] for row in native))

    def test_codex_window_rejects_an_isolated_matching_output(self) -> None:
        isolated = (
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call_output",
                        "call_id": "tool-isolated",
                    },
                }
            )
            + "\n"
        ).encode("utf-8")
        self.transcript.write_bytes(isolated)
        event = {
            "host": "codex",
            "transcript_path": str(self.transcript),
            "prefix": {"length": len(isolated)},
        }
        self.assertFalse(
            managed_runtime._history_contains_publication_result(event, "tool-isolated")
        )

    def test_codex_window_stays_open_without_a_verified_hook_to_history_mapping(self) -> None:
        records_for_other_turn = [
            {
                "type": "session_meta",
                "payload": {"session_id": "session-codex", "cwd": str(self.project.resolve())},
            },
            {
                "type": "turn_context",
                "ordinal": 1,
                "payload": {"turn_id": "other-turn", "cwd": str(self.project.resolve())},
            },
            {
                "type": "response_item",
                "ordinal": 2,
                "payload": {
                    "type": "custom_tool_call",
                    "name": "exec",
                    "call_id": "shared-call",
                },
            },
            {
                "type": "response_item",
                "ordinal": 3,
                "payload": {"type": "custom_tool_call_output", "call_id": "shared-call"},
            },
        ]
        raw = b"".join(
            (json.dumps(record) + "\n").encode("utf-8") for record in records_for_other_turn
        )
        self.transcript.write_bytes(raw)
        event = {
            "host": "codex",
            "session_id": "session-codex",
            "generation_id": "expected-turn",
            "cwd": str(self.project.resolve()),
            "transcript_path": str(self.transcript),
            "prefix": {"length": len(raw)},
        }
        self.assertFalse(managed_runtime._history_contains_publication_result(event, "shared-call"))

    def test_codex_window_accepts_the_matching_command_execution_item(self) -> None:
        """A current 0.163 CommandExecution closes only its matching window."""

        command = "python selc-suite.py managed publish"
        event = {
            "host": "codex",
            "session_id": "codex-session",
            "generation_id": "codex-turn",
            "tool_command_digest": hashlib.sha256(command.encode("utf-8")).hexdigest(),
            "operation_digest": "declared-publish-operation",
            "cwd": str(self.project.resolve()),
        }
        history = [
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "thread_id": "codex-session",
                    "turn_id": "codex-turn",
                    "item": {
                        "type": "CommandExecution",
                        "id": "tool-codex",
                        "command": ["/bin/zsh", "-lc", command],
                        "cwd": str(self.project.resolve()),
                        "status": "completed",
                        "exit_code": 0,
                    },
                },
            }
        ]

        self.assertTrue(
            managed_runtime._codex_history_contains_publication_result(
                history,
                event,
                "tool-codex",
            )
        )

    def test_codex_window_accepts_a_matching_local_file_uri_cwd(self) -> None:
        """A local file URI names the same canonical CommandExecution workspace."""

        command = "python selc-suite.py managed publish"
        event = {
            "host": "codex",
            "session_id": "codex-session",
            "generation_id": "codex-turn",
            "tool_command_digest": hashlib.sha256(command.encode("utf-8")).hexdigest(),
            "operation_digest": "declared-publish-operation",
            "cwd": str(self.project.resolve()),
        }
        history = [
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "thread_id": "codex-session",
                    "turn_id": "codex-turn",
                    "item": {
                        "type": "CommandExecution",
                        "id": "tool-codex",
                        "command": ["/bin/zsh", "-lc", command],
                        "cwd": self.project.resolve().as_uri(),
                        "status": "completed",
                        "exit_code": 0,
                    },
                },
            }
        ]

        self.assertTrue(
            managed_runtime._codex_history_contains_publication_result(
                history,
                event,
                "tool-codex",
            )
        )

    def test_codex_window_rejects_duplicate_or_mismatched_command_execution_items(self) -> None:
        """A matching tool ID cannot hide a conflicting native completion."""

        command = "python selc-suite.py managed publish"
        event = {
            "host": "codex",
            "session_id": "codex-session",
            "generation_id": "codex-turn",
            "tool_command_digest": hashlib.sha256(command.encode("utf-8")).hexdigest(),
            "operation_digest": "declared-publish-operation",
            "cwd": str(self.project.resolve()),
        }

        def completed_item(**updates: object) -> dict[str, object]:
            item = {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "thread_id": "codex-session",
                    "turn_id": "codex-turn",
                    "item": {
                        "type": "CommandExecution",
                        "id": "tool-codex",
                        "command": ["/bin/zsh", "-lc", command],
                        "cwd": str(self.project.resolve()),
                        "status": "completed",
                        "exit_code": 0,
                    },
                },
            }
            payload = item["payload"]
            assert isinstance(payload, dict)
            item_body = payload["item"]
            assert isinstance(item_body, dict)
            for key, value in updates.items():
                if key in {"thread_id", "turn_id"}:
                    payload[key] = value
                else:
                    item_body[key] = value
            return item

        valid = completed_item()
        self.assertFalse(
            managed_runtime._codex_history_contains_publication_result(
                [valid, valid], event, "tool-codex"
            )
        )
        wrong_cwd = self.root / "other-project"
        wrong_cwd.mkdir()
        file_uri = self.project.resolve().as_uri()
        for invalid in (
            completed_item(turn_id="other-turn"),
            completed_item(cwd=str(self.root)),
            completed_item(cwd="file://example.test" + str(self.project.resolve())),
            completed_item(cwd="file:relative-project"),
            completed_item(cwd=file_uri + "?query=1"),
            completed_item(cwd=file_uri + "#fragment"),
            completed_item(cwd=file_uri + "\t"),
            completed_item(cwd=file_uri + "\r"),
            completed_item(cwd=file_uri + "\n"),
            completed_item(cwd="file:///tmp/%ZZ"),
            completed_item(cwd="file:///tmp/%00"),
            completed_item(cwd="file:///tmp/%FF"),
            completed_item(cwd=file_uri + "\ud800"),
            completed_item(cwd=wrong_cwd.resolve().as_uri()),
            completed_item(cwd="https://example.test/project"),
            completed_item(command=["/bin/zsh", "-lc", "printf wrong"]),
            completed_item(exit_code=1),
            completed_item(exit_code=False),
            completed_item(exit_code=0.0),
        ):
            self.assertFalse(
                managed_runtime._codex_history_contains_publication_result(
                    [invalid], event, "tool-codex"
                )
            )
        outer_exec = {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "exec",
                "call_id": "tool-codex",
                "input": "await tools.exec_command(...) ",
            },
        }
        self.assertFalse(
            managed_runtime._codex_history_contains_publication_result(
                [outer_exec], event, "tool-codex"
            )
        )

    def test_codex_window_ignores_matching_items_before_the_pre_prefix(self) -> None:
        """A result from a previous native window cannot close the current one."""

        command = "python selc-suite.py managed publish"
        completed = {
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "thread_id": "codex-session",
                "turn_id": "codex-turn",
                "item": {
                    "type": "CommandExecution",
                    "id": "tool-codex",
                    "command": ["/bin/zsh", "-lc", command],
                    "cwd": str(self.project.resolve()),
                    "status": "completed",
                    "exit_code": 0,
                },
            },
        }
        old_prefix = (json.dumps(completed) + "\n").encode("utf-8")
        self.transcript.write_bytes(old_prefix)
        event = {
            "host": "codex",
            "session_id": "codex-session",
            "generation_id": "codex-turn",
            "tool_command_digest": hashlib.sha256(command.encode("utf-8")).hexdigest(),
            "operation_digest": "declared-publish-operation",
            "cwd": str(self.project.resolve()),
            "transcript_path": str(self.transcript),
            "prefix": {"length": len(old_prefix)},
            "pre_prefix_length": len(old_prefix),
        }

        self.assertFalse(managed_runtime._history_contains_publication_result(event, "tool-codex"))
        with self.transcript.open("ab") as stream:
            stream.write((json.dumps(completed) + "\n").encode("utf-8"))
        event["prefix"] = {"length": self.transcript.stat().st_size}
        self.assertTrue(managed_runtime._history_contains_publication_result(event, "tool-codex"))

    def test_publication_window_rejects_a_mutated_or_truncated_cutoff(self) -> None:
        """A frozen Stop cutoff cannot be replaced before later reconciliation."""

        command = "python selc-suite.py managed publish"
        history_path = self.root / "codex-window.jsonl"
        pre_raw = b'{"type":"session_meta"}\n'

        def completion(turn_id: str) -> bytes:
            return (
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "item_completed",
                            "thread_id": "codex-session",
                            "turn_id": turn_id,
                            "item": {
                                "type": "CommandExecution",
                                "id": "tool-codex",
                                "command": ["/bin/zsh", "-lc", command],
                                "cwd": str(self.project.resolve()),
                                "status": "completed",
                                "exit_code": 0,
                            },
                        },
                    }
                )
                + "\n"
            ).encode("utf-8")

        wrong_turn = completion("other-turn")
        matching_turn = completion("codex-turn")
        self.assertEqual(len(wrong_turn), len(matching_turn))
        history_path.write_bytes(pre_raw + wrong_turn)
        stat = history_path.stat()
        pre_event = {
            "payload_json": json.dumps(
                {
                    "tool_command_digest": hashlib.sha256(command.encode("utf-8")).hexdigest(),
                    "operation_digest": "declared-publish-operation",
                    "prefix": {
                        "sha256": hashlib.sha256(pre_raw).hexdigest(),
                        "length": len(pre_raw),
                        "path_hash": hashlib.sha256(
                            str(history_path.resolve()).encode("utf-8")
                        ).hexdigest(),
                        "device": stat.st_dev,
                        "inode": stat.st_ino,
                    },
                }
            )
        }
        window = {
            "tool_use_id": "tool-codex",
            "pre_prefix_hash": hashlib.sha256(pre_raw).hexdigest(),
            "pre_prefix_length": len(pre_raw),
        }

        def stop_event(cutoff: bytes) -> dict[str, object]:
            return {
                "host": "codex",
                "session_id": "codex-session",
                "generation_id": "codex-turn",
                "cwd": str(self.project.resolve()),
                "transcript_path": str(history_path),
                "prefix": {
                    "sha256": hashlib.sha256(cutoff).hexdigest(),
                    "length": len(cutoff),
                    "path_hash": hashlib.sha256(
                        str(history_path.resolve()).encode("utf-8")
                    ).hexdigest(),
                    "device": stat.st_dev,
                    "inode": stat.st_ino,
                },
            }

        wrong_cutoff = pre_raw + wrong_turn
        captured_stop = stop_event(wrong_cutoff)
        self.assertEqual(
            managed_runtime._publication_window_status(pre_event, window, captured_stop),
            "pending",
        )

        history_path.write_bytes(pre_raw + matching_turn)
        self.assertEqual(
            managed_runtime._publication_window_status(pre_event, window, captured_stop),
            "invalid",
        )

        history_path.write_bytes(pre_raw + matching_turn[:-1])
        self.assertEqual(
            managed_runtime._publication_window_status(pre_event, window, captured_stop),
            "invalid",
        )

        history_path.write_bytes(wrong_cutoff + matching_turn)
        self.assertEqual(
            managed_runtime._publication_window_status(pre_event, window, captured_stop),
            "pending",
        )

        matching_cutoff = pre_raw + matching_turn
        history_path.write_bytes(matching_cutoff)
        self.assertEqual(
            managed_runtime._publication_window_status(
                pre_event,
                window,
                stop_event(matching_cutoff),
            ),
            "closed",
        )

    def test_codex_root_prefix_requires_the_exact_user_text_not_injected_context(self) -> None:
        """Injected Codex context is not a substitute for the original prompt."""

        prompt = "Generate the required report."
        history = [
            {
                "type": "session_meta",
                "payload": {
                    "id": "codex-session",
                    "session_id": "codex-session",
                    "cwd": str(self.project.resolve()),
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "injected context"}],
                    "internal_chat_message_metadata_passthrough": {
                        "turn_id": "codex-turn",
                        "content_item_kinds": ["agents_md.instructions"],
                    },
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": prompt}],
                    "internal_chat_message_metadata_passthrough": {
                        "turn_id": "codex-turn",
                        "content_item_kinds": ["user.text"],
                    },
                },
            },
        ]
        digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        self.assertTrue(
            managed_runtime._codex_root_prefix_is_verified(
                history,
                session_id="codex-session",
                cwd=self.project.resolve(),
                generation_id="codex-turn",
                raw_prompt_digest=digest,
            )
        )
        changed = json.loads(json.dumps(history))
        changed[2]["payload"]["content"][0]["text"] = prompt + " "
        self.assertFalse(
            managed_runtime._codex_root_prefix_is_verified(
                changed,
                session_id="codex-session",
                cwd=self.project.resolve(),
                generation_id="codex-turn",
                raw_prompt_digest=digest,
            )
        )

    def test_codex_root_prefix_validates_initialization_record_attribution(self) -> None:
        """Observed initialization records must belong to the initial turn and workspace."""

        prompt = "Generate the required report."
        history = [
            {
                "type": "session_meta",
                "payload": {
                    "id": "codex-session",
                    "session_id": "codex-session",
                    "cwd": str(self.project.resolve()),
                },
            },
            {
                "type": "world_state",
                "payload": {"full": True, "state": {}},
            },
            {
                "type": "turn_context",
                "payload": {
                    "turn_id": "codex-turn",
                    "root_turn_id": "codex-turn",
                    "cwd": str(self.project.resolve()),
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "task_started",
                    "turn_id": "codex-turn",
                    "root_turn_id": "codex-turn",
                    "turn_attribution": {
                        "turn_id": "codex-turn",
                        "root_turn_id": "codex-turn",
                        "parent_turn_id": None,
                    },
                },
            },
            {
                "type": "event_msg",
                "payload": {"type": "thread_settings_applied", "thread_id": "codex-session"},
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": prompt}],
                    "internal_chat_message_metadata_passthrough": {
                        "turn_id": "codex-turn",
                        "content_item_kinds": ["user.text"],
                    },
                },
            },
        ]
        digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        self.assertTrue(
            managed_runtime._codex_root_prefix_is_verified(
                history,
                session_id="codex-session",
                cwd=self.project.resolve(),
                generation_id="codex-turn",
                raw_prompt_digest=digest,
            )
        )

        for path, value in (
            ((2, "payload", "turn_id"), "other-turn"),
            ((2, "payload", "cwd"), str(self.root)),
            ((3, "payload", "turn_id"), "other-turn"),
            ((3, "payload", "turn_attribution", "parent_turn_id"), "other-turn"),
            ((4, "payload", "thread_id"), "other-session"),
        ):
            conflicting = json.loads(json.dumps(history))
            target: object = conflicting
            for key in path[:-1]:
                assert isinstance(target, (list, dict))
                target = target[key]
            assert isinstance(target, dict)
            target[path[-1]] = value
            self.assertFalse(
                managed_runtime._codex_root_prefix_is_verified(
                    conflicting,
                    session_id="codex-session",
                    cwd=self.project.resolve(),
                    generation_id="codex-turn",
                    raw_prompt_digest=digest,
                )
            )
        unknown_kind = json.loads(json.dumps(history))
        unknown_kind[1]["payload"]["internal_chat_message_metadata_passthrough"] = {
            "turn_id": "codex-turn",
            "content_item_kinds": ["unrecognized.context"],
        }
        self.assertFalse(
            managed_runtime._codex_root_prefix_is_verified(
                unknown_kind,
                session_id="codex-session",
                cwd=self.project.resolve(),
                generation_id="codex-turn",
                raw_prompt_digest=digest,
            )
        )

    def test_codex_two_phase_anchor_rejects_business_history_and_changed_prompt_bytes(self) -> None:
        """P0 is initialization only, while P1 binds the original UTF-8 bytes."""

        prompt = "Create the report."
        anchor = [
            {
                "type": "session_meta",
                "payload": {
                    "id": "codex-session",
                    "session_id": "codex-session",
                    "cwd": str(self.project.resolve()),
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "task_started",
                    "turn_id": "codex-turn",
                    "root_turn_id": "codex-turn",
                    "turn_attribution": {
                        "turn_id": "codex-turn",
                        "root_turn_id": "codex-turn",
                        "parent_turn_id": None,
                    },
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "developer",
                    "internal_chat_message_metadata_passthrough": {
                        "turn_id": "codex-turn",
                        "content_item_kinds": ["host_skills.instructions"],
                    },
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "internal_chat_message_metadata_passthrough": {
                        "turn_id": "codex-turn",
                        "content_item_kinds": ["agents_md.instructions"],
                    },
                },
            },
            {"type": "world_state", "payload": {"full": True, "state": {}}},
            {
                "type": "turn_context",
                "payload": {
                    "turn_id": "codex-turn",
                    "root_turn_id": "codex-turn",
                    "cwd": str(self.project.resolve()),
                },
            },
            {
                "type": "event_msg",
                "payload": {"type": "thread_settings_applied", "thread_id": "codex-session"},
            },
        ]
        self.assertTrue(
            managed_runtime._codex_initialization_prefix_is_verified(
                anchor,
                session_id="codex-session",
                cwd=self.project.resolve(),
                generation_id="codex-turn",
            )
        )

        with_business_history = [
            *anchor,
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "earlier request"}],
                    "internal_chat_message_metadata_passthrough": {
                        "turn_id": "codex-turn",
                        "content_item_kinds": ["user.text"],
                    },
                },
            },
        ]
        self.assertFalse(
            managed_runtime._codex_initialization_prefix_is_verified(
                with_business_history,
                session_id="codex-session",
                cwd=self.project.resolve(),
                generation_id="codex-turn",
            )
        )

        changed_prompt = [
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": prompt + " "}],
                    "internal_chat_message_metadata_passthrough": {
                        "turn_id": "codex-turn",
                        "content_item_kinds": ["user.text"],
                    },
                },
            }
        ]
        self.assertFalse(
            managed_runtime._codex_appended_prompt_is_verified(
                changed_prompt,
                session_id="codex-session",
                cwd=self.project.resolve(),
                generation_id="codex-turn",
                raw_prompt_digest=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            )
        )

        two_user_messages = [
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": prompt}],
                    "internal_chat_message_metadata_passthrough": {
                        "turn_id": "codex-turn",
                        "content_item_kinds": ["user.text"],
                    },
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "second request"}],
                    "internal_chat_message_metadata_passthrough": {
                        "turn_id": "codex-turn",
                        "content_item_kinds": ["user.text"],
                    },
                },
            },
        ]
        self.assertFalse(
            managed_runtime._codex_appended_prompt_is_verified(
                two_user_messages,
                session_id="codex-session",
                cwd=self.project.resolve(),
                generation_id="codex-turn",
                raw_prompt_digest=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            )
        )

        verified_append = [
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": prompt}],
                    "internal_chat_message_metadata_passthrough": {
                        "turn_id": "codex-turn",
                        "content_item_kinds": ["user.text"],
                    },
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "thread_id": "codex-session",
                    "turn_id": "codex-turn",
                    "item": {"type": "UserMessage"},
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "thread_id": "codex-session",
                    "turn_id": "codex-turn",
                    "item": {"type": "Reasoning"},
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "reasoning",
                    "internal_chat_message_metadata_passthrough": {"turn_id": "codex-turn"},
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "thread_id": "codex-session",
                    "turn_id": "codex-turn",
                    "item": {
                        "type": "AgentMessage",
                        "phase": "commentary",
                        "content": [{"type": "Text", "text": "Current turn commentary."}],
                    },
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "phase": "commentary",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": "Current turn commentary."}],
                    "internal_chat_message_metadata_passthrough": {
                        "turn_id": "codex-turn",
                        "content_item_kinds": ["unknown"],
                    },
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "internal_chat_message_metadata_passthrough": {"turn_id": "codex-turn"},
                },
            },
            {
                "type": "token_usage_record",
                "payload": {
                    "session_id": "codex-session",
                    "thread_id": "codex-session",
                    "turn_id": "codex-turn",
                    "root_turn_id": "codex-turn",
                },
            },
        ]
        self.assertTrue(
            managed_runtime._codex_appended_prompt_is_verified(
                verified_append,
                session_id="codex-session",
                cwd=self.project.resolve(),
                generation_id="codex-turn",
                raw_prompt_digest=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            )
        )

        missing_commentary_content = json.loads(json.dumps(verified_append))
        missing_commentary_content[4]["payload"]["item"].pop("content")
        self.assertFalse(
            managed_runtime._codex_appended_prompt_is_verified(
                missing_commentary_content,
                session_id="codex-session",
                cwd=self.project.resolve(),
                generation_id="codex-turn",
                raw_prompt_digest=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            )
        )

        missing_commentary_thread = json.loads(json.dumps(verified_append))
        missing_commentary_thread[4]["payload"].pop("thread_id")
        self.assertFalse(
            managed_runtime._codex_appended_prompt_is_verified(
                missing_commentary_thread,
                session_id="codex-session",
                cwd=self.project.resolve(),
                generation_id="codex-turn",
                raw_prompt_digest=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            )
        )

        malformed_assistant_commentary = json.loads(json.dumps(verified_append))
        malformed_assistant_commentary[5]["payload"]["content"][0]["type"] = "Text"
        self.assertFalse(
            managed_runtime._codex_appended_prompt_is_verified(
                malformed_assistant_commentary,
                session_id="codex-session",
                cwd=self.project.resolve(),
                generation_id="codex-turn",
                raw_prompt_digest=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            )
        )

        for path, value in (
            ((1, "payload", "thread_id"), "foreign-session"),
            ((2, "payload", "cwd"), str(self.root)),
            (
                (
                    3,
                    "payload",
                    "internal_chat_message_metadata_passthrough",
                    "turn_id",
                ),
                "foreign-turn",
            ),
            (
                (
                    5,
                    "payload",
                    "internal_chat_message_metadata_passthrough",
                    "turn_id",
                ),
                "foreign-turn",
            ),
        ):
            conflicting = json.loads(json.dumps(verified_append))
            target: object = conflicting
            for key in path[:-1]:
                assert isinstance(target, (list, dict))
                target = target[key]
            assert isinstance(target, dict)
            target[path[-1]] = value
            self.assertFalse(
                managed_runtime._codex_appended_prompt_is_verified(
                    conflicting,
                    session_id="codex-session",
                    cwd=self.project.resolve(),
                    generation_id="codex-turn",
                    raw_prompt_digest=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                )
            )

    def test_feedback_revisions_recompute_the_managed_assessment_owner(self) -> None:
        activated = self._activate()
        action = self._run_text_action(activated)
        receipt = self._observe("publish", "tool-feedback-publish")
        published = managed_runtime.publish(
            project_root=self.project,
            suite_id="managed-reports",
            task_id=str(activated["task_id"]),
            call_id=str(activated["call_id"]),
            receipt=receipt,
            artifact_path=Path(action["artifact_path"]),
            target="reports/feedback.md",
        )
        self._observe("publish", "tool-feedback-publish", phase="PostToolUse")

        database = open_database(self.project)
        try:
            record_feedback(
                database,
                feedback_id="feedback-thread",
                task_id=str(activated["task_id"]),
                delivery_id=str(published["delivery_id"]),
                revision=1,
                scope="final",
                result="fail",
                body="feedback-v1",
            )
        finally:
            database.close()
        after_failure = managed_runtime.health(
            self.project, "managed-reports", str(activated["task_id"])
        )
        self.assertEqual(after_failure["final_result"], "fail")

        database = open_database(self.project)
        try:
            record_feedback(
                database,
                feedback_id="feedback-thread",
                task_id=str(activated["task_id"]),
                delivery_id=str(published["delivery_id"]),
                revision=2,
                scope="final",
                result="pass",
                body="feedback-v2",
            )
        finally:
            database.close()
        after_revision = managed_runtime.health(
            self.project, "managed-reports", str(activated["task_id"])
        )
        self.assertEqual(after_revision["final_result"], "unknown")

    def test_recovery_fences_a_crashed_prepared_publication(self) -> None:
        activated = self._activate()
        action = self._run_text_action(activated)
        publish_receipt = self._observe("publish", "tool-crash-publish")
        with (
            mock.patch(
                "selc.managed_runtime._supply_publication", side_effect=RuntimeError("crash")
            ),
            self.assertRaisesRegex(RuntimeError, "crash"),
        ):
            managed_runtime.publish(
                project_root=self.project,
                suite_id="managed-reports",
                task_id=str(activated["task_id"]),
                call_id=str(activated["call_id"]),
                receipt=publish_receipt,
                artifact_path=Path(action["artifact_path"]),
                target="reports/crash.md",
            )

        next_action_receipt = self._observe("text-report", "tool-fenced-action")
        with self.assertRaises(SelcError) as captured:
            managed_runtime.run_action(
                project_root=self.project,
                suite_id="managed-reports",
                skill="text-report",
                task_id=str(activated["task_id"]),
                call_id=str(activated["call_id"]),
                action_id="text-report",
                receipt=next_action_receipt,
                request={"input": str(self.input_path)},
            )
        self.assertEqual(captured.exception.code, "MANAGED_ACTION_FENCED")
        self.assertEqual(managed_runtime.recover(self.project, "managed-reports"), {"recovered": 1})

        recovered_action = managed_runtime.run_action(
            project_root=self.project,
            suite_id="managed-reports",
            skill="text-report",
            task_id=str(activated["task_id"]),
            call_id=str(activated["call_id"]),
            action_id="text-report",
            receipt=next_action_receipt,
            request={"input": str(self.input_path)},
        )
        self.assertTrue(Path(str(recovered_action["artifact_path"])).is_file())

    def test_recovery_refuses_to_run_while_an_executor_still_holds_the_fence(self) -> None:
        lock_path = self.project / ".selc" / "installs" / "managed-reports" / ".lock"
        holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "from pathlib import Path\n"
                    "from selc.util import exclusive_lock\n"
                    "import sys\n"
                    "with exclusive_lock(Path(sys.argv[1])):\n"
                    "    print('locked', flush=True)\n"
                    "    sys.stdin.readline()\n"
                ),
                str(lock_path),
            ],
            env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")},
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual(holder.stdout.readline().strip(), "locked")
            with self.assertRaises(SelcError) as captured:
                managed_runtime.recover(self.project, "managed-reports")
            self.assertEqual(captured.exception.code, "LOCKED")
        finally:
            if holder.stdin is not None:
                holder.stdin.write("release\n")
                holder.stdin.close()
            return_code = holder.wait(timeout=5)
            stderr = holder.stderr.read() if holder.stderr is not None else ""
            if holder.stdout is not None:
                holder.stdout.close()
            if holder.stderr is not None:
                holder.stderr.close()
            self.assertEqual(return_code, 0, stderr)

    def test_killed_runtime_keeps_the_execution_fence_until_its_child_exits(self) -> None:
        """A released child must outlive a killed caller without losing its fence."""

        staging = self.root / "crashed-runtime-staging"
        staging.mkdir()
        started = self.root / "runner-started"
        finished = self.root / "runner-finished"
        parent_program = "\n".join(
            (
                "import sys",
                "from pathlib import Path",
                "from selc import records",
                "from selc.managed_runtime import _record_execution_started, _run_managed_subprocess",
                "project = Path(sys.argv[1])",
                "staging = Path(sys.argv[2])",
                "started = Path(sys.argv[3])",
                "finished = Path(sys.argv[4])",
                "database = records.open_database(project)",
                "def started_callback(identity, heartbeat_path):",
                "    _record_execution_started(database, 'crashed-runtime', 'managed-reports', identity, heartbeat_path)",
                "    started.write_text('recorded', encoding='utf-8')",
                "try:",
                "    _run_managed_subprocess(",
                "        [sys.executable, '-c', \"import sys, time; from pathlib import Path; time.sleep(0.8); Path(sys.argv[1]).write_text('child finished')\", str(finished)],",
                "        staging=staging,",
                "        on_execution_started=started_callback,",
                "        on_execution_finished=None,",
                "    )",
                "finally:",
                "    database.close()",
            )
        )
        parent = subprocess.Popen(
            [
                sys.executable,
                "-c",
                parent_program,
                str(self.project),
                str(staging),
                str(started),
                str(finished),
            ],
            env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")},
        )
        try:
            deadline = time.monotonic() + 3
            while not started.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(started.is_file())
            parent.kill()
            parent.wait(timeout=3)

            database = open_database(self.project)
            try:
                with self.assertRaises(SelcError) as captured:
                    assert_no_live_execution(database, "managed-reports")
            finally:
                database.close()
            self.assertIn(
                captured.exception.code,
                {"MANAGED_EXECUTION_ACTIVE", "MANAGED_EXECUTION_UNCERTAIN"},
            )
            with self.assertRaises(SelcError) as captured:
                managed_runtime.recover(self.project, "managed-reports")
            self.assertIn(
                captured.exception.code,
                {"MANAGED_EXECUTION_ACTIVE", "MANAGED_EXECUTION_UNCERTAIN"},
            )

            deadline = time.monotonic() + 3
            while not finished.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(finished.is_file())
            time.sleep(0.15)

            database = open_database(self.project)
            try:
                assert_no_live_execution(database, "managed-reports")
                state = database.execute(
                    "SELECT state FROM managed_executions WHERE action_run_id = 'crashed-runtime'"
                ).fetchone()[0]
            finally:
                database.close()
            self.assertEqual(state, "terminated")
            self.assertEqual(
                managed_runtime.recover(self.project, "managed-reports"), {"recovered": 0}
            )
        finally:
            if parent.poll() is None:
                parent.kill()
                parent.wait(timeout=3)

    def test_group_fence_survives_a_leader_exit_while_a_background_child_runs(self) -> None:
        """A dead leader cannot make an inherited process group safe to reuse."""

        ready = self.root / "background-ready"
        finished = self.root / "background-finished"
        program = "\n".join(
            (
                "import os",
                "import subprocess",
                "import sys",
                "from pathlib import Path",
                "ready = Path(sys.argv[1])",
                "finished = Path(sys.argv[2])",
                'child = "import sys, time; from pathlib import Path; '
                "time.sleep(0.5); Path(sys.argv[1]).write_text('finished')\"",
                "subprocess.Popen([sys.executable, '-c', child, str(finished)])",
                "ready.write_text(str(os.getpgrp()), encoding='utf-8')",
            )
        )
        leader = subprocess.Popen(
            [sys.executable, "-c", program, str(ready), str(finished)],
            start_new_session=True,
        )
        process_group = leader.pid
        try:
            deadline = time.monotonic() + 3
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready.is_file())
            self.assertEqual(int(ready.read_text(encoding="utf-8")), process_group)
            self.assertEqual(leader.wait(timeout=3), 0)

            database = open_database(self.project)
            try:
                managed_runtime._record_execution_started(
                    database,
                    "background-descendant",
                    "managed-reports",
                    ProcessIdentity(leader.pid, process_group, "background-token"),
                    self.root / "missing-heartbeat",
                )
                with self.assertRaises(SelcError) as captured:
                    assert_no_live_execution(database, "managed-reports")
                self.assertEqual(captured.exception.code, "MANAGED_EXECUTION_UNCERTAIN")
            finally:
                database.close()

            deadline = time.monotonic() + 3
            while not finished.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(finished.is_file())
            deadline = time.monotonic() + 3
            while process_group_is_live(process_group) and time.monotonic() < deadline:
                time.sleep(0.01)

            database = open_database(self.project)
            try:
                assert_no_live_execution(database, "managed-reports")
                state = database.execute(
                    "SELECT state FROM managed_executions WHERE action_run_id = 'background-descendant'"
                ).fetchone()[0]
            finally:
                database.close()
            self.assertEqual(state, "terminated")
        finally:
            if process_group_is_live(process_group):
                os.killpg(process_group, 9)

    def test_group_termination_kills_a_term_ignoring_descendant(self) -> None:
        ready = self.root / "term-ignoring-ready"
        child_pid_path = self.root / "term-ignoring-child-pid"
        child_heartbeat = self.root / "term-ignoring-child-heartbeat"
        program = "\n".join(
            (
                "import os",
                "import subprocess",
                "import sys",
                "import time",
                "from pathlib import Path",
                "child = '\\n'.join(('import signal, sys, time', 'from pathlib import Path', "
                "'signal.signal(signal.SIGTERM, signal.SIG_IGN)', "
                "'heartbeat = Path(sys.argv[1])', 'while True:', "
                "'    heartbeat.write_text(str(time.monotonic()), encoding=\"utf-8\")', "
                "'    time.sleep(0.05)'))",
                "process = subprocess.Popen([sys.executable, '-c', child, sys.argv[3]])",
                "Path(sys.argv[2]).write_text(str(process.pid), encoding='utf-8')",
                "Path(sys.argv[1]).write_text(str(os.getpgrp()), encoding='utf-8')",
                "time.sleep(30)",
            )
        )
        runner = subprocess.Popen(
            [
                sys.executable,
                "-c",
                program,
                str(ready),
                str(child_pid_path),
                str(child_heartbeat),
            ],
            start_new_session=True,
        )
        identity = ProcessIdentity(runner.pid, runner.pid, "term-ignoring-token")
        try:
            deadline = time.monotonic() + 3
            while (
                not ready.exists() or not child_pid_path.exists() or not child_heartbeat.exists()
            ) and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready.is_file())
            self.assertTrue(child_pid_path.is_file())
            self.assertTrue(child_heartbeat.is_file())
            started = time.monotonic()
            termination_error: SelcError | None = None
            try:
                managed_runtime._terminate_managed_process_group(identity)
            except SelcError as error:
                termination_error = error
            self.assertLess(time.monotonic() - started, 3)
            if runner.poll() is None:
                runner.wait(timeout=3)
            first_heartbeat = child_heartbeat.read_text(encoding="utf-8")
            time.sleep(0.2)
            self.assertEqual(child_heartbeat.read_text(encoding="utf-8"), first_heartbeat)
            if termination_error is not None:
                self.assertEqual(
                    termination_error.code,
                    "EXECUTION_IDENTITY_UNAVAILABLE",
                )
        finally:
            if runner.poll() is None:
                with suppress(ProcessLookupError, PermissionError):
                    os.killpg(runner.pid, 9)
            runner.wait(timeout=3)

    def test_group_termination_keeps_the_fence_when_liveness_is_unavailable(self) -> None:
        runner = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        identity = ProcessIdentity(runner.pid, runner.pid, "identity-unavailable-token")
        try:
            with (
                mock.patch(
                    "selc.managed_runtime.process_group_is_live",
                    side_effect=SelcError(
                        "EXECUTION_IDENTITY_UNAVAILABLE",
                        "injected process-group inspection failure",
                    ),
                ),
                self.assertRaises(SelcError) as captured,
            ):
                managed_runtime._terminate_managed_process_group(identity)
            self.assertEqual(captured.exception.code, "EXECUTION_IDENTITY_UNAVAILABLE")
        finally:
            if runner.poll() is None:
                runner.kill()
            runner.wait(timeout=3)

    def test_stop_and_full_scope_delete_keep_a_live_execution_occupied(self) -> None:
        activated = self._activate()
        action = self._run_text_action(activated)
        receipt = self._observe("publish", "tool-live-delete-publish")
        managed_runtime.publish(
            project_root=self.project,
            suite_id="managed-reports",
            task_id=str(activated["task_id"]),
            call_id=str(activated["call_id"]),
            receipt=receipt,
            artifact_path=Path(str(action["artifact_path"])),
            target="reports/live-delete.md",
        )
        self._observe("publish", "tool-live-delete-publish", phase="PostToolUse")

        heartbeat = self.root / "live-delete-heartbeat"
        execution_token = "live-delete-token"
        heartbeat.write_text(f"{execution_token} running {time.time()}", encoding="utf-8")
        runner = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        try:
            database = open_database(self.project)
            try:
                database.execute(
                    "DELETE FROM managed_executions WHERE action_run_id = ?",
                    (str(action["action_run_id"]),),
                )
                database.commit()
                managed_runtime._record_execution_started(
                    database,
                    str(action["action_run_id"]),
                    "managed-reports",
                    ProcessIdentity(runner.pid, runner.pid, execution_token),
                    heartbeat,
                )
            finally:
                database.close()

            stopped = self._stop("tool-live-delete-publish")
            self.assertEqual(stopped["status"], "stop_recorded")

            database = open_database(self.project)
            try:
                call_status = database.execute(
                    "SELECT status FROM calls WHERE call_id = ?", (activated["call_id"],)
                ).fetchone()["status"]
                task_status = database.execute(
                    "SELECT status FROM tasks WHERE task_id = ?", (activated["task_id"],)
                ).fetchone()["status"]
                with self.assertRaises(SelcError) as captured:
                    delete_records(database, suite_id=None, before=None, apply=True)
                with self.assertRaises(SelcError) as held_captured:
                    delete_records(
                        database,
                        suite_id=None,
                        before="9999-12-31T23:59:59Z",
                        apply=True,
                        _locks_held=True,
                    )
                remaining_tasks = database.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
                remaining_executions = database.execute(
                    "SELECT COUNT(*) FROM managed_executions"
                ).fetchone()[0]
            finally:
                database.close()

            cli_delete = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "scripts" / "selc.py"),
                    "delete",
                    "--project-root",
                    str(self.project),
                    "--apply",
                ],
                cwd=REPO_ROOT,
                capture_output=True,
                check=False,
                text=True,
            )
            self.assertEqual(cli_delete.returncode, 2, cli_delete.stderr)
            cli_error = json.loads(cli_delete.stdout)["error"]["code"]
            database = open_database(self.project)
            try:
                remaining_tasks = database.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
                remaining_executions = database.execute(
                    "SELECT COUNT(*) FROM managed_executions"
                ).fetchone()[0]
            finally:
                database.close()

            self.assertEqual(call_status, "active")
            self.assertEqual(task_status, "active")
            self.assertIn(
                captured.exception.code,
                {"MANAGED_EXECUTION_ACTIVE", "MANAGED_EXECUTION_UNCERTAIN", "ACTIVE_LEASE"},
            )
            self.assertIn(
                held_captured.exception.code,
                {"MANAGED_EXECUTION_ACTIVE", "MANAGED_EXECUTION_UNCERTAIN", "ACTIVE_LEASE"},
            )
            self.assertIn(
                cli_error,
                {"MANAGED_EXECUTION_ACTIVE", "MANAGED_EXECUTION_UNCERTAIN", "ACTIVE_LEASE"},
            )
            self.assertEqual(remaining_tasks, 1)
            self.assertEqual(remaining_executions, 1)

            os.killpg(runner.pid, 9)
            runner.wait(timeout=3)
            recovered = managed_runtime.recover(self.project, "managed-reports")
            self.assertEqual(recovered["recovered"], 0)

            database = open_database(self.project)
            try:
                state = database.execute(
                    "SELECT state FROM managed_executions WHERE action_run_id = ?",
                    (str(action["action_run_id"]),),
                ).fetchone()["state"]
                recovered_call_status = database.execute(
                    "SELECT status FROM calls WHERE call_id = ?", (activated["call_id"],)
                ).fetchone()["status"]
                recovered_task_status = database.execute(
                    "SELECT status FROM tasks WHERE task_id = ?", (activated["task_id"],)
                ).fetchone()["status"]
            finally:
                database.close()
            self.assertEqual(state, "terminated")
            self.assertEqual(recovered_call_status, "closed")
            self.assertEqual(recovered_task_status, "closed")
        finally:
            if runner.poll() is None:
                os.killpg(runner.pid, 9)
            runner.wait(timeout=3)

    def test_stop_fences_an_unpublished_finished_action_before_closing(self) -> None:
        activated = self._activate()
        action = self._run_text_action(activated)

        stopped = self._stop("tool-action")
        self.assertEqual(stopped["status"], "stop_recorded")
        self.assertEqual(managed_runtime.recover(self.project, "managed-reports"), {"recovered": 0})

        database = open_database(self.project)
        try:
            action_status = database.execute(
                "SELECT status FROM managed_actions WHERE action_run_id = ?",
                (action["action_run_id"],),
            ).fetchone()["status"]
            call_status = database.execute(
                "SELECT status FROM calls WHERE call_id = ?", (activated["call_id"],)
            ).fetchone()["status"]
            task_status = database.execute(
                "SELECT status FROM tasks WHERE task_id = ?", (activated["task_id"],)
            ).fetchone()["status"]
        finally:
            database.close()
        self.assertEqual(action_status, "fenced")
        self.assertTrue(Path(str(action["artifact_path"])).is_file())
        self.assertEqual(call_status, "closed")
        self.assertEqual(task_status, "closed")

        publish_receipt = self._observe("publish", "tool-stopped-unpublished-publish")
        with self.assertRaises(SelcError) as captured:
            managed_runtime.publish(
                project_root=self.project,
                suite_id="managed-reports",
                task_id=str(activated["task_id"]),
                call_id=str(activated["call_id"]),
                receipt=publish_receipt,
                artifact_path=Path(str(action["artifact_path"])),
                target="reports/stopped-unpublished.md",
            )
        self.assertEqual(captured.exception.code, "MANAGED_CONTEXT_CLOSED")

        correction_payload = self._payload("activate", "tool-stopped-correction-activate")
        correction_payload.update(
            {
                "generation_id": "generation-stopped-correction",
                "request_id": "request-stopped-correction",
                "root_request_id": "root-a",
                "causation": "business_correction",
            }
        )
        correction_receipt = managed_runtime.observe_request_boundary(
            project_root=self.project,
            suite_id="managed-reports",
            payload=correction_payload,
            fixture=True,
        )["receipt"]
        correction = managed_runtime.activate(
            project_root=self.project,
            suite_id="managed-reports",
            skill="text-report",
            receipt=str(correction_receipt),
        )
        self.assertEqual(correction["task_id"], activated["task_id"])

        correction_action_payload = self._payload("text-report", "tool-stopped-correction-action")
        correction_action_payload.update(
            {
                "generation_id": "generation-stopped-correction",
                "request_id": "request-stopped-correction",
                "root_request_id": "root-a",
                "causation": "business_correction",
            }
        )
        correction_action_receipt = managed_runtime.observe_request_boundary(
            project_root=self.project,
            suite_id="managed-reports",
            payload=correction_action_payload,
            fixture=True,
        )["receipt"]
        corrected_action = managed_runtime.run_action(
            project_root=self.project,
            suite_id="managed-reports",
            skill="text-report",
            task_id=str(correction["task_id"]),
            call_id=str(correction["call_id"]),
            action_id="text-report",
            receipt=str(correction_action_receipt),
            request={"input": str(self.input_path)},
        )
        self.assertTrue(Path(str(corrected_action["artifact_path"])).is_file())

    def test_recover_fences_a_stopped_unpublished_action_after_execution_ends(self) -> None:
        """A stored Stop closes only after recovery proves the group quiescent."""

        activated = self._activate()
        action = self._run_text_action(activated)
        heartbeat = self.root / "stopped-unpublished-heartbeat"
        execution_token = "stopped-unpublished-token"
        heartbeat.write_text(f"{execution_token} running {time.time()}", encoding="utf-8")
        runner = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        try:
            database = open_database(self.project)
            try:
                database.execute(
                    "DELETE FROM managed_executions WHERE action_run_id = ?",
                    (str(action["action_run_id"]),),
                )
                database.commit()
                managed_runtime._record_execution_started(
                    database,
                    str(action["action_run_id"]),
                    "managed-reports",
                    ProcessIdentity(runner.pid, runner.pid, execution_token),
                    heartbeat,
                )
            finally:
                database.close()

            stopped = self._stop("tool-action")
            self.assertEqual(stopped["status"], "stop_recorded")
            database = open_database(self.project)
            try:
                self.assertEqual(
                    database.execute(
                        "SELECT status FROM calls WHERE call_id = ?", (activated["call_id"],)
                    ).fetchone()["status"],
                    "active",
                )
            finally:
                database.close()

            os.killpg(runner.pid, 9)
            runner.wait(timeout=3)
            self.assertEqual(
                managed_runtime.recover(self.project, "managed-reports"), {"recovered": 0}
            )

            database = open_database(self.project)
            try:
                execution_state = database.execute(
                    "SELECT state FROM managed_executions WHERE action_run_id = ?",
                    (str(action["action_run_id"]),),
                ).fetchone()["state"]
                action_status = database.execute(
                    "SELECT status FROM managed_actions WHERE action_run_id = ?",
                    (str(action["action_run_id"]),),
                ).fetchone()["status"]
                call_status = database.execute(
                    "SELECT status FROM calls WHERE call_id = ?", (activated["call_id"],)
                ).fetchone()["status"]
                task_status = database.execute(
                    "SELECT status FROM tasks WHERE task_id = ?", (activated["task_id"],)
                ).fetchone()["status"]
            finally:
                database.close()
            self.assertEqual(execution_state, "terminated")
            self.assertEqual(action_status, "fenced")
            self.assertEqual(call_status, "closed")
            self.assertEqual(task_status, "closed")
        finally:
            if runner.poll() is None:
                os.killpg(runner.pid, 9)
            runner.wait(timeout=3)

    def test_deletion_suppresses_late_native_replay(self) -> None:
        activated = self._activate()
        action = self._run_text_action(activated)
        receipt = self._observe("publish", "tool-delete-publish")
        managed_runtime.publish(
            project_root=self.project,
            suite_id="managed-reports",
            task_id=str(activated["task_id"]),
            call_id=str(activated["call_id"]),
            receipt=receipt,
            artifact_path=Path(action["artifact_path"]),
            target="reports/delete.md",
        )
        self._observe("publish", "tool-delete-publish", phase="PostToolUse")
        self._stop("tool-delete-publish")
        database = open_database(self.project)
        try:
            close_task(database, str(activated["task_id"]))
            deleted = delete_records(
                database,
                suite_id="managed-reports",
                before=None,
                apply=True,
            )
        finally:
            database.close()
        self.assertGreater(deleted["deleted"]["managed_native_events"], 0)
        self.assertFalse(Path(str(action["artifact_path"])).exists())
        database = open_database(self.project)
        try:
            self.assertEqual(
                database.execute("SELECT COUNT(*) FROM managed_publication_intents").fetchone()[0],
                0,
            )
            self.assertEqual(
                database.execute("SELECT COUNT(*) FROM managed_executions").fetchone()[0],
                0,
            )
        finally:
            database.close()

        replay = managed_runtime.observe_request_boundary(
            project_root=self.project,
            suite_id="managed-reports",
            payload=self._payload("activate", "tool-activate"),
            fixture=True,
        )
        self.assertEqual(replay["status"], "suppressed")
        unseen_late = self._payload("activate", "tool-late-unseen")
        unseen_late["request_id"] = "generation-a"
        suppressed_late = managed_runtime.observe_request_boundary(
            project_root=self.project,
            suite_id="managed-reports",
            payload=unseen_late,
            fixture=True,
        )
        self.assertEqual(suppressed_late["status"], "suppressed")

    def test_time_filtered_deletion_closes_the_full_managed_context(self) -> None:
        activated = self._activate()
        action = self._run_text_action(activated)
        receipt = self._observe("publish", "tool-time-delete-publish")
        managed_runtime.publish(
            project_root=self.project,
            suite_id="managed-reports",
            task_id=str(activated["task_id"]),
            call_id=str(activated["call_id"]),
            receipt=receipt,
            artifact_path=Path(str(action["artifact_path"])),
            target="reports/time-delete.md",
        )
        self._stop("tool-time-delete-publish")
        late_event = self._payload("activate", "tool-time-delete-late")
        managed_runtime.observe_request_boundary(
            project_root=self.project,
            suite_id="managed-reports",
            payload=late_event,
            fixture=True,
        )

        database = open_database(self.project)
        try:
            close_task(database, str(activated["task_id"]))
            database.execute(
                "UPDATE tasks SET created_at = ? WHERE task_id = ?",
                ("2020-01-01T00:00:00Z", activated["task_id"]),
            )
            database.commit()
            deleted = delete_records(
                database,
                suite_id="managed-reports",
                before="2021-01-01T00:00:00Z",
                apply=True,
            )
        finally:
            database.close()
        self.assertGreater(deleted["deleted"]["managed_native_events"], 1)
        self.assertFalse(Path(str(action["artifact_path"])).parent.exists())

        unseen_late = self._payload("activate", "tool-time-delete-unseen")
        suppressed = managed_runtime.observe_request_boundary(
            project_root=self.project,
            suite_id="managed-reports",
            payload=unseen_late,
            fixture=True,
        )
        self.assertEqual(suppressed["status"], "suppressed")

    def test_delete_removes_failed_action_directory_without_an_artifact_path(self) -> None:
        activated = self._activate()
        receipt = self._observe("text-report", "tool-delete-failed-action")
        with (
            mock.patch(
                "selc.managed_runtime._execute_declared_action",
                side_effect=SelcError("MANAGED_ACTION_FAILED", "injected failure"),
            ),
            self.assertRaises(SelcError),
        ):
            managed_runtime.run_action(
                project_root=self.project,
                suite_id="managed-reports",
                skill="text-report",
                task_id=str(activated["task_id"]),
                call_id=str(activated["call_id"]),
                action_id="text-report",
                receipt=receipt,
                request={"input": str(self.input_path)},
            )
        database = open_database(self.project)
        try:
            action = database.execute(
                "SELECT action_run_id, artifact_path FROM managed_actions"
            ).fetchone()
            self.assertIsNone(action["artifact_path"])
            private_directory = (
                self.project
                / ".selc"
                / "managed-staging"
                / "managed-reports"
                / str(action["action_run_id"])
            )
            self.assertTrue(private_directory.is_dir())
            database.execute("UPDATE calls SET status = 'closed'")
            database.execute("UPDATE tasks SET status = 'closed'")
            database.commit()
            deleted = delete_records(
                database,
                suite_id="managed-reports",
                before=None,
                apply=True,
            )
        finally:
            database.close()
        self.assertEqual(deleted["deleted"]["local_tasks"], 1)
        self.assertFalse(private_directory.exists())

    def test_action_receipt_cannot_cross_business_tasks(self) -> None:
        first = self._activate()
        second_payload = self._payload("activate", "tool-task-b")
        second_payload["request_id"] = "request-task-b"
        second_receipt = managed_runtime.observe_request_boundary(
            project_root=self.project,
            suite_id="managed-reports",
            payload=second_payload,
            fixture=True,
        )["receipt"]
        second = managed_runtime.activate(
            project_root=self.project,
            suite_id="managed-reports",
            skill="text-report",
            receipt=str(second_receipt),
        )
        self.assertNotEqual(first["task_id"], second["task_id"])
        action_payload = self._payload("text-report", "tool-task-b-action")
        action_payload["request_id"] = "request-task-b"
        action_receipt = managed_runtime.observe_request_boundary(
            project_root=self.project,
            suite_id="managed-reports",
            payload=action_payload,
            fixture=True,
        )["receipt"]
        with self.assertRaises(SelcError) as captured:
            managed_runtime.run_action(
                project_root=self.project,
                suite_id="managed-reports",
                skill="text-report",
                task_id=str(first["task_id"]),
                call_id=str(first["call_id"]),
                action_id="text-report",
                receipt=str(action_receipt),
                request={"input": str(self.input_path)},
            )
        self.assertEqual(captured.exception.code, "RECEIPT_SUBJECT_MISMATCH")

    def test_action_receipt_cannot_cross_calls_in_one_business_task(self) -> None:
        first = self._activate()
        second_payload = self._payload("activate", "tool-second-activate")
        second_payload.update(
            {
                "generation_id": "generation-correction",
                "request_id": "request-correction",
                "root_request_id": "root-a",
                "causation": "business_correction",
            }
        )
        second_receipt = managed_runtime.observe_request_boundary(
            project_root=self.project,
            suite_id="managed-reports",
            payload=second_payload,
            fixture=True,
        )["receipt"]
        second = managed_runtime.activate(
            project_root=self.project,
            suite_id="managed-reports",
            skill="text-report",
            receipt=str(second_receipt),
        )
        self.assertEqual(second["task_id"], first["task_id"])

        action_payload = self._payload("text-report", "tool-second-action")
        action_payload.update(
            {
                "generation_id": "generation-correction",
                "request_id": "request-correction",
                "root_request_id": "root-a",
                "causation": "business_correction",
            }
        )
        action_receipt = managed_runtime.observe_request_boundary(
            project_root=self.project,
            suite_id="managed-reports",
            payload=action_payload,
            fixture=True,
        )["receipt"]
        with self.assertRaises(SelcError) as captured:
            managed_runtime.run_action(
                project_root=self.project,
                suite_id="managed-reports",
                skill="text-report",
                task_id=str(first["task_id"]),
                call_id=str(first["call_id"]),
                action_id="text-report",
                receipt=str(action_receipt),
                request={"input": str(self.input_path)},
            )
        self.assertEqual(captured.exception.code, "RECEIPT_SUBJECT_MISMATCH")

    def test_native_replay_keeps_the_original_prefix_after_history_growth(self) -> None:
        payload = self._payload("activate", "tool-growth")
        first = managed_runtime.observe_request_boundary(
            project_root=self.project,
            suite_id="managed-reports",
            payload=payload,
            fixture=True,
        )
        self.assertEqual(first["status"], "recorded")
        self.transcript.write_bytes(b'{"type":"later-native-record"}\n')
        payload["prefix_length"] = self.transcript.stat().st_size
        replay = managed_runtime.observe_request_boundary(
            project_root=self.project,
            suite_id="managed-reports",
            payload=payload,
            fixture=True,
        )
        self.assertEqual(replay["status"], "replayed")

    def test_native_metadata_root_requires_session_and_no_unresolved_history_base(self) -> None:
        valid = json.dumps({"type": "session-meta", "sessionId": "wb-session"}).encode()
        self.assertTrue(
            managed_runtime._metadata_only_prefix(
                valid,
                host="workbuddy",
                session_id="wb-session",
                cwd=self.project.resolve(),
            )
        )
        missing_session = json.dumps({"type": "session-meta"}).encode()
        self.assertFalse(
            managed_runtime._metadata_only_prefix(
                missing_session,
                host="workbuddy",
                session_id="wb-session",
                cwd=self.project.resolve(),
            )
        )
        unresolved = json.dumps(
            {
                "type": "session-meta",
                "sessionId": "wb-session",
                "metadata": {"history_base": "unmapped"},
            }
        ).encode()
        self.assertFalse(
            managed_runtime._metadata_only_prefix(
                unresolved,
                host="workbuddy",
                session_id="wb-session",
                cwd=self.project.resolve(),
            )
        )

    def test_workbuddy_window_requires_matching_call_result_and_parent_chain(self) -> None:
        command = "python selc-suite.py managed publish"
        event = {
            "session_id": "wb-session",
            "cwd": str(self.project.resolve()),
            "request_id": "wb-request",
            "tool_command_digest": hashlib.sha256(command.encode()).hexdigest(),
        }
        user = {
            "id": "user-message",
            "type": "message",
            "role": "user",
            "sessionId": "wb-session",
            "cwd": str(self.project.resolve()),
            "providerData": {
                "startsNewUserRequest": True,
                "conversationRequestId": "wb-request",
            },
        }
        call = {
            "id": "function-call",
            "type": "function_call",
            "callId": "wb-tool",
            "parentId": "user-message",
            "sessionId": "wb-session",
            "cwd": str(self.project.resolve()),
            "providerData": {"conversationRequestId": "wb-request"},
            "arguments": json.dumps({"command": command, "description": "publish"}),
        }
        result = {
            "id": "function-result",
            "type": "function_call_result",
            "callId": "wb-tool",
            "parentId": "function-call",
            "sessionId": "wb-session",
            "cwd": str(self.project.resolve()),
            "providerData": {"conversationRequestId": "wb-request"},
            "output": {"exit_code": 0},
        }
        self.assertTrue(
            managed_runtime._workbuddy_history_contains_publication_result(
                [user, call, result], event, "wb-tool"
            )
        )
        orphan = dict(result)
        orphan["parentId"] = "missing-call"
        self.assertFalse(
            managed_runtime._workbuddy_history_contains_publication_result(
                [user, call, orphan], event, "wb-tool"
            )
        )

    def test_workbuddy_window_rejects_cross_request_nodes_before_current_user_boundary(
        self,
    ) -> None:
        """A matching call ID cannot skip a newer request boundary or bridge."""

        command = "python selc-suite.py managed publish"
        event = {
            "session_id": "wb-session",
            "cwd": str(self.project.resolve()),
            "request_id": "request-one",
            "tool_command_digest": hashlib.sha256(command.encode()).hexdigest(),
        }
        original_user = {
            "id": "user-one",
            "type": "message",
            "role": "user",
            "sessionId": "wb-session",
            "cwd": str(self.project.resolve()),
            "providerData": {
                "startsNewUserRequest": True,
                "conversationRequestId": "request-one",
            },
        }
        newer_user = {
            "id": "user-two",
            "type": "message",
            "role": "user",
            "parentId": "user-one",
            "sessionId": "wb-session",
            "cwd": str(self.project.resolve()),
            "providerData": {
                "startsNewUserRequest": True,
                "conversationRequestId": "request-two",
            },
        }
        matching_call = {
            "id": "call-one",
            "type": "function_call",
            "callId": "wb-tool",
            "parentId": "user-two",
            "sessionId": "wb-session",
            "cwd": str(self.project.resolve()),
            "providerData": {"conversationRequestId": "request-one"},
            "arguments": json.dumps({"command": command, "description": "publish"}),
        }
        result = {
            "id": "result-one",
            "type": "function_call_result",
            "callId": "wb-tool",
            "parentId": "call-one",
            "sessionId": "wb-session",
            "cwd": str(self.project.resolve()),
            "providerData": {"conversationRequestId": "request-one"},
            "output": {"exit_code": 0},
        }
        self.assertFalse(
            managed_runtime._workbuddy_history_contains_publication_result(
                [original_user, newer_user, matching_call, result], event, "wb-tool"
            )
        )

        assistant_bridge = {
            "id": "assistant-two",
            "type": "message",
            "role": "assistant",
            "parentId": "user-one",
            "sessionId": "wb-session",
            "cwd": str(self.project.resolve()),
            "providerData": {"conversationRequestId": "request-two"},
        }
        bridged_call = dict(matching_call)
        bridged_call["parentId"] = "assistant-two"
        self.assertFalse(
            managed_runtime._workbuddy_history_contains_publication_result(
                [original_user, assistant_bridge, bridged_call, result], event, "wb-tool"
            )
        )

    def test_workbuddy_stop_uses_the_pre_tool_digest_as_its_cutoff_binding(self) -> None:
        """Stop has no WorkBuddy tool input, so its matching Pre owns the binding."""

        command = "python selc-suite.py managed publish --fixed"
        user = {
            "id": "user-message",
            "type": "message",
            "role": "user",
            "sessionId": "wb-session",
            "cwd": str(self.project.resolve()),
            "providerData": {
                "startsNewUserRequest": True,
                "conversationRequestId": "wb-request",
            },
        }
        history_path = self.root / "workbuddy-stop.jsonl"
        initial = (json.dumps(user) + "\n").encode("utf-8")
        history_path.write_bytes(initial)
        history_stat = history_path.stat()
        pre_event = {
            "payload_json": json.dumps(
                {
                    "tool_command_digest": hashlib.sha256(command.encode("utf-8")).hexdigest(),
                    "prefix": {
                        "sha256": hashlib.sha256(initial).hexdigest(),
                        "length": len(initial),
                        "path_hash": hashlib.sha256(
                            str(history_path.resolve()).encode("utf-8")
                        ).hexdigest(),
                        "device": history_stat.st_dev,
                        "inode": history_stat.st_ino,
                    },
                }
            )
        }
        call = {
            "id": "function-call",
            "type": "function_call",
            "callId": "wb-tool",
            "parentId": "user-message",
            "sessionId": "wb-session",
            "cwd": str(self.project.resolve()),
            "providerData": {"conversationRequestId": "wb-request"},
            "arguments": json.dumps({"command": command, "description": "publish"}),
        }
        result = {
            "id": "function-result",
            "type": "function_call_result",
            "callId": "wb-tool",
            "parentId": "function-call",
            "sessionId": "wb-session",
            "cwd": str(self.project.resolve()),
            "providerData": {"conversationRequestId": "wb-request"},
            "output": {"exit_code": 0},
        }
        with history_path.open("ab") as stream:
            stream.write((json.dumps(call) + "\n" + json.dumps(result) + "\n").encode("utf-8"))
        cutoff = history_path.read_bytes()
        window = {
            "tool_use_id": "wb-tool",
            "pre_prefix_hash": hashlib.sha256(initial).hexdigest(),
            "pre_prefix_length": len(initial),
        }
        stop_event = {
            "host": "workbuddy",
            "session_id": "wb-session",
            "cwd": str(self.project.resolve()),
            "request_id": "wb-request",
            "transcript_path": str(history_path),
            "prefix": {
                "sha256": hashlib.sha256(cutoff).hexdigest(),
                "length": len(cutoff),
                "path_hash": hashlib.sha256(
                    str(history_path.resolve()).encode("utf-8")
                ).hexdigest(),
                "device": history_stat.st_dev,
                "inode": history_stat.st_ino,
            },
        }
        self.assertEqual(
            managed_runtime._publication_window_status(pre_event, window, stop_event),
            "closed",
        )

    def test_workbuddy_correction_candidate_links_only_through_native_message_ancestors(
        self,
    ) -> None:
        """A callback without parent-request fields can reuse one proven task."""

        original = self._activate()
        original_user = {
            "id": "original-user",
            "type": "message",
            "role": "user",
            "sessionId": "wb-session",
            "cwd": str(self.project.resolve()),
            "providerData": {
                "startsNewUserRequest": True,
                "conversationRequestId": "wb-original-request",
            },
        }
        correction_user = {
            "id": "correction-user",
            "type": "message",
            "role": "user",
            "parentId": "original-user",
            "sessionId": "wb-session",
            "cwd": str(self.project.resolve()),
            "providerData": {
                "startsNewUserRequest": True,
                "conversationRequestId": "wb-correction-request",
            },
        }
        history_path = self.root / "workbuddy-correction.jsonl"
        history_path.write_text(
            json.dumps(original_user) + "\n" + json.dumps(correction_user) + "\n",
            encoding="utf-8",
        )
        event = {
            "host": "workbuddy",
            "suite_id": "managed-reports",
            "session_id": "wb-session",
            "cwd": str(self.project.resolve()),
            "request_id": "wb-correction-request",
            "transcript_path": str(history_path),
            "prefix": {"length": history_path.stat().st_size},
        }
        database = open_database(self.project)
        try:
            database.execute(
                """INSERT INTO managed_contexts(
                context_key, suite_id, host, session_id, generation_id, request_id,
                root_request_id, request_digest, input_skill, root_event_id, root_valid,
                task_id, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, '', '', '', ?, 0, ?, 'active', ?)""",
                (
                    "workbuddy-original-context",
                    "managed-reports",
                    "workbuddy",
                    "wb-session",
                    "wb-original-generation",
                    "wb-original-request",
                    "event-original",
                    original["task_id"],
                    records.utc_now(),
                ),
            )
            database.execute(
                """INSERT INTO managed_contexts(
                context_key, suite_id, host, session_id, generation_id, request_id,
                root_request_id, request_digest, input_skill, root_event_id, root_valid,
                status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, '', '', '', ?, 0, 'active', ?)""",
                (
                    "workbuddy-correction-context",
                    "managed-reports",
                    "workbuddy",
                    "wb-session",
                    "wb-correction-generation",
                    "wb-correction-request",
                    "event-correction",
                    records.utc_now(),
                ),
            )
            self.assertTrue(
                managed_runtime._link_history_followup_context(
                    database,
                    "workbuddy-correction-context",
                    event,
                )
            )
            linked = database.execute(
                "SELECT task_id FROM managed_contexts WHERE context_key = ?",
                ("workbuddy-correction-context",),
            ).fetchone()[0]
        finally:
            database.close()
        self.assertEqual(linked, original["task_id"])

    def test_stop_closes_every_call_for_its_native_request(self) -> None:
        first = self._activate()
        second_receipt = self._observe("activate", "tool-activate-second")
        second = managed_runtime.activate(
            project_root=self.project,
            suite_id="managed-reports",
            skill="text-report",
            receipt=second_receipt,
        )
        self.assertEqual(first["task_id"], second["task_id"])
        self._stop("tool-activate-second")

        database = open_database(self.project)
        try:
            statuses = {
                row["status"]
                for row in database.execute(
                    "SELECT status FROM calls WHERE task_id = ?", (first["task_id"],)
                )
            }
        finally:
            database.close()
        self.assertEqual(statuses, {"closed"})

    def test_stop_does_not_close_an_independent_active_correction_call(self) -> None:
        first = self._activate()
        correction = self._payload("activate", "tool-stop-correction")
        correction.update(
            {
                "generation_id": "generation-stop-correction",
                "request_id": "request-stop-correction",
                "root_request_id": "root-a",
                "causation": "business_correction",
            }
        )
        correction_receipt = managed_runtime.observe_request_boundary(
            project_root=self.project,
            suite_id="managed-reports",
            payload=correction,
            fixture=True,
        )["receipt"]
        correction_call = managed_runtime.activate(
            project_root=self.project,
            suite_id="managed-reports",
            skill="text-report",
            receipt=str(correction_receipt),
        )
        self.assertEqual(correction_call["task_id"], first["task_id"])

        self._stop("tool-activate")

        database = open_database(self.project)
        try:
            first_status = database.execute(
                "SELECT status FROM calls WHERE call_id = ?", (first["call_id"],)
            ).fetchone()["status"]
            correction_status = database.execute(
                "SELECT status FROM calls WHERE call_id = ?", (correction_call["call_id"],)
            ).fetchone()["status"]
            task_status = database.execute(
                "SELECT status FROM tasks WHERE task_id = ?", (first["task_id"],)
            ).fetchone()["status"]
        finally:
            database.close()
        self.assertEqual(first_status, "closed")
        self.assertEqual(correction_status, "active")
        self.assertEqual(task_status, "active")

    def test_conflicting_native_replay_invalidates_the_original_receipt(self) -> None:
        receipt = self._observe("activate", "tool-conflict")
        payload = self._payload("activate", "tool-conflict")
        payload["tool_input"] = {
            "command": json.dumps(
                {"action": "publish", "suite": "managed-reports", "skill": "text-report"},
                sort_keys=True,
            )
        }
        conflict = managed_runtime.observe_request_boundary(
            project_root=self.project,
            suite_id="managed-reports",
            payload=payload,
            fixture=True,
        )
        self.assertEqual(conflict["status"], "conflict")
        with self.assertRaises(SelcError) as captured:
            managed_runtime.activate(
                project_root=self.project,
                suite_id="managed-reports",
                skill="text-report",
                receipt=receipt,
            )
        self.assertEqual(captured.exception.code, "RECEIPT_INVALID")

    def test_receipt_is_one_use_and_cannot_select_a_new_task_for_correction(self) -> None:
        activated = self._activate()
        with self.assertRaises(SelcError) as captured:
            managed_runtime.activate(
                project_root=self.project,
                suite_id="managed-reports",
                skill="text-report",
                receipt=self._last_receipt,
            )
        self.assertEqual(captured.exception.code, "RECEIPT_ALREADY_CONSUMED")

        correction = self._payload("activate", "tool-correction")
        correction["causation"] = "business_correction"
        correction["request_id"] = "correction-request"
        correction["request"] = {"skill": "text-report", "input": {"input": str(self.input_path)}}
        receipt = managed_runtime.observe_request_boundary(
            project_root=self.project,
            suite_id="managed-reports",
            payload=correction,
            fixture=True,
        )["receipt"]
        corrected = managed_runtime.activate(
            project_root=self.project,
            suite_id="managed-reports",
            skill="text-report",
            receipt=receipt,
        )
        self.assertEqual(corrected["task_id"], activated["task_id"])
        self.assertNotEqual(corrected["call_id"], activated["call_id"])

    def test_parent_request_links_a_correction_without_a_root_identifier(self) -> None:
        original = self._activate()
        correction = self._payload("activate", "tool-parent-correction")
        correction.update(
            {
                "generation_id": "generation-correction",
                "request_id": "request-correction",
                "parent_request_id": "generation-a",
                "causation": "business_correction",
            }
        )
        correction.pop("root_request_id")
        receipt = managed_runtime.observe_request_boundary(
            project_root=self.project,
            suite_id="managed-reports",
            payload=correction,
            fixture=True,
        )["receipt"]
        corrected = managed_runtime.activate(
            project_root=self.project,
            suite_id="managed-reports",
            skill="text-report",
            receipt=str(receipt),
        )
        self.assertEqual(corrected["task_id"], original["task_id"])
        health = managed_runtime.health(self.project, "managed-reports", str(original["task_id"]))
        self.assertEqual(health["first_result"], "fail")

    def test_native_adapter_only_classifies_explicit_correction_prompt_forms(self) -> None:
        self.assertEqual(
            _native_causation({"hook_event_name": "UserPromptSubmit", "prompt": "纠正上一份报告"}),
            "correction_candidate",
        )
        self.assertEqual(
            _native_causation(
                {"hook_event_name": "UserPromptSubmit", "prompt": "请继续处理这个请求"}
            ),
            "unknown",
        )
        self.assertEqual(
            _native_causation(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "is_internal_continuation": True,
                    "prompt": "纠正上一份报告",
                }
            ),
            "automatic_retry",
        )
        self.assertEqual(
            _native_causation(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "准备输入：请判断应该采用哪个税率和业务口径",
                }
            ),
            "unknown",
        )

    def test_caller_claimed_native_payload_never_starts_a_managed_task(self) -> None:
        payload = self._payload("activate", "forged-tool")
        payload["source"] = "caller-claims-native"
        with self.assertRaises(SelcError) as captured:
            managed_runtime.observe_request_boundary(
                project_root=self.project,
                suite_id="managed-reports",
                payload=payload,
            )
        self.assertEqual(captured.exception.code, "NATIVE_PRODUCER_UNTRUSTED")
        database = open_database(self.project)
        try:
            self.assertEqual(database.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)
            self.assertEqual(
                database.execute("SELECT COUNT(*) FROM managed_receipts").fetchone()[0], 0
            )
        finally:
            database.close()

    def test_action_receipt_rejects_a_different_declared_input(self) -> None:
        activated = self._activate()
        receipt = self._observe("text-report", "tool-bound-input")
        other_input = self.root / "other-input.txt"
        other_input.write_text("different", encoding="utf-8")
        with self.assertRaises(SelcError) as captured:
            managed_runtime.run_action(
                project_root=self.project,
                suite_id="managed-reports",
                skill="text-report",
                task_id=activated["task_id"],
                call_id=activated["call_id"],
                action_id="text-report",
                receipt=receipt,
                request={"input": str(other_input)},
            )
        self.assertEqual(captured.exception.code, "RECEIPT_ARGUMENT_MISMATCH")

    def test_raw_native_event_survives_a_receipt_projection_failure_and_replay(self) -> None:
        payload = self._payload("activate", "tool-raw-first")
        with (
            mock.patch("selc.managed_runtime._issue_receipt", side_effect=RuntimeError("receipt")),
            self.assertRaises(RuntimeError),
        ):
            managed_runtime.observe_request_boundary(
                project_root=self.project,
                suite_id="managed-reports",
                payload=payload,
                fixture=True,
            )
        database = open_database(self.project)
        try:
            self.assertEqual(
                database.execute("SELECT COUNT(*) FROM managed_native_events").fetchone()[0], 1
            )
            self.assertEqual(
                database.execute("SELECT COUNT(*) FROM managed_receipts").fetchone()[0], 0
            )
        finally:
            database.close()
        replayed = managed_runtime.observe_request_boundary(
            project_root=self.project,
            suite_id="managed-reports",
            payload=payload,
            fixture=True,
        )
        self.assertEqual(replayed["status"], "recorded")
        self.assertTrue(replayed["receipt"])

    def test_unverified_native_version_pre_without_a_prompt_anchor_is_unusable(self) -> None:
        payload = self._payload("activate", "tool-no-root")
        payload["source"] = "native-collector"
        payload["prefix_length"] = 1
        observed = managed_runtime.observe_request_boundary(
            project_root=self.project,
            suite_id="managed-reports",
            payload=payload,
            trusted_producer=True,
        )
        self.assertEqual(observed["status"], "recorded_unusable")
        self.assertNotIn("receipt", observed)
        database = open_database(self.project)
        try:
            self.assertEqual(
                database.execute("SELECT COUNT(*) FROM managed_contexts").fetchone()[0], 0
            )
            self.assertEqual(database.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)
        finally:
            database.close()

    def test_declared_native_version_still_requires_a_persisted_prompt_proof(self) -> None:
        manifest_path = self.suite / "suite.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["managed"]["native_hosts"] = {"codex": ["0.163.0-alpha.10.2"]}
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        seal_suite(self.suite)
        self.installation = install_suite(
            source=self.suite,
            project_root=self.project,
            harness="codex",
            ref=None,
        )

        payload = self._payload("activate", "tool-declared-no-root")
        payload["source"] = "native-collector"
        payload["prefix_length"] = 1
        observed = managed_runtime.observe_request_boundary(
            project_root=self.project,
            suite_id="managed-reports",
            payload=payload,
            trusted_producer=True,
        )
        self.assertEqual(observed["status"], "recorded_unusable")
        self.assertNotIn("receipt", observed)
        database = open_database(self.project)
        try:
            self.assertEqual(
                database.execute("SELECT COUNT(*) FROM managed_contexts").fetchone()[0], 0
            )
            self.assertEqual(database.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)
        finally:
            database.close()

    def test_codex_pre_without_a_prompt_anchor_is_recorded_but_unusable(self) -> None:
        manifest_path = self.suite / "suite.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["managed"]["native_hosts"] = {"codex": ["0.163.0-alpha.10.2"]}
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        seal_suite(self.suite)
        install_suite(source=self.suite, project_root=self.project, harness="codex", ref=None)

        host_history = self.root / ".codex" / "sessions" / "2026" / "10" / "session.jsonl"
        host_history.parent.mkdir(parents=True)
        prompt_text = "Create the requested report."
        metadata = b"".join(
            (json.dumps(record) + "\n").encode("utf-8")
            for record in (
                {
                    "type": "session_meta",
                    "payload": {
                        "id": "session-a",
                        "session_id": "session-a",
                        "cwd": str(self.project.resolve()),
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": prompt_text}],
                        "internal_chat_message_metadata_passthrough": {
                            "turn_id": "generation-a",
                            "content_item_kinds": ["user.text"],
                        },
                    },
                },
            )
        )
        host_history.write_bytes(metadata)
        payload = self._payload("activate", "tool-host-history")
        payload["source"] = "caller-claims-native"
        payload["transcript_path"] = str(host_history)
        payload["prefix_length"] = len(metadata)
        payload["request"]["raw_prompt_digest"] = hashlib.sha256(
            prompt_text.encode("utf-8")
        ).hexdigest()
        with mock.patch("selc.managed_runtime.Path.home", return_value=self.root):
            observed = managed_runtime.observe_request_boundary(
                project_root=self.project,
                suite_id="managed-reports",
                payload=payload,
                trusted_producer=True,
            )
        self.assertEqual(observed["status"], "recorded_unusable")
        database = open_database(self.project)
        try:
            event = database.execute("SELECT payload_json FROM managed_native_events").fetchone()
            self.assertEqual(
                database.execute("SELECT COUNT(*) FROM managed_contexts").fetchone()[0], 0
            )
            self.assertEqual(database.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)
        finally:
            database.close()
        self.assertIn('"root_proof_status":"verified"', event["payload_json"])
        self.assertNotIn(str(host_history), event["payload_json"])

    def test_workbuddy_environment_keeps_request_message_and_root_separate(self) -> None:
        environment = {
            "CODEBUDDY_SESSION_ID": "session-1",
            "CODEBUDDY_SANDBOX_BROKER_SESSION_ID": "session-1",
            "CODEBUDDY_TOOL_CALL_ID": "tool-1",
            "CODEBUDDY_SANDBOX_BROKER_TOOL_CALL_ID": "tool-1",
            "CODEBUDDY_CONVERSATION_REQUEST_ID": "request-1",
            "CODEBUDDY_CONVERSATION_MESSAGE_ID": "message-1",
            "CODEBUDDY_ROOT_REQUEST_ID": "root-1",
        }
        resolved = managed_runtime.native_context_from_environment("workbuddy", environment)
        self.assertEqual(resolved["request_id"], "request-1")
        self.assertEqual(resolved["message_id"], "message-1")
        self.assertEqual(resolved["root_request_id"], "root-1")

        environment["CODEBUDDY_SANDBOX_BROKER_TOOL_CALL_ID"] = "tool-2"
        with self.assertRaises(SelcError) as captured:
            managed_runtime.native_context_from_environment("workbuddy", environment)
        self.assertEqual(captured.exception.code, "NATIVE_CONTEXT_CONFLICT")

    def test_codex_environment_requires_direct_session_thread_and_version_identity(self) -> None:
        environment = {
            "CODEX_SESSION_ID": "codex-session",
            "CODEX_THREAD_ID": "codex-session",
            "CODEX_TOOL_CALL_ID": "codex-tool",
            "CODEX_VERSION": "0.163.0-alpha.10.2",
        }
        context = managed_runtime.native_context_from_environment("codex", environment)
        self.assertEqual(context["session_id"], "codex-session")
        self.assertEqual(context["tool_use_id"], "codex-tool")
        self.assertEqual(context["client_version"], "0.163.0-alpha.10.2")

        environment["CODEX_THREAD_ID"] = "different-thread"
        with self.assertRaises(SelcError) as captured:
            managed_runtime.native_context_from_environment("codex", environment)
        self.assertEqual(captured.exception.code, "NATIVE_CONTEXT_CONFLICT")

        with self.assertRaises(SelcError) as captured:
            managed_runtime.native_context_from_environment(
                "codex",
                {"CODEX_THREAD_ID": "codex-session"},
            )
        self.assertEqual(captured.exception.code, "NATIVE_CONTEXT_UNAVAILABLE")

    def test_workbuddy_hook_uses_payload_identity_without_hook_environment(self) -> None:
        payload = {
            "hook_event_name": "UserPromptSubmit",
            "client": "WorkBuddy",
            "version": "5.8.3",
            "cwd": str(self.project),
            "session_id": "wb-session",
            "generation_id": "wb-generation",
            "conversation_request_id": "wb-request",
            "conversation_message_id": "wb-message",
            "root_request_id": "wb-root",
            "prompt": '{"objective":"summarize"}',
            "transcript_path": str(self.transcript),
        }
        result = receive_native_hook(
            project_root=self.project,
            suite_id="managed-reports",
            host="workbuddy",
            payload=payload,
            environment={},
        )
        self.assertEqual(result["status"], "prompt_recorded")
        database = open_database(self.project)
        try:
            context = database.execute(
                "SELECT session_id, generation_id, request_id, root_request_id FROM managed_contexts"
            ).fetchone()
            event = database.execute("SELECT payload_json FROM managed_native_events").fetchone()
        finally:
            database.close()
        self.assertEqual(
            tuple(context),
            ("wb-session", "wb-generation", "wb-request", "wb-root"),
        )
        self.assertIn('"message_id":"wb-message"', event["payload_json"])

    def test_native_hook_rejects_a_codex_pre_without_a_persisted_prompt_anchor(self) -> None:
        manifest_path = self.suite / "suite.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["managed"]["native_hosts"] = {"codex": ["0.163.0-alpha.10.2"]}
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        seal_suite(self.suite)
        install_suite(source=self.suite, project_root=self.project, harness="codex", ref=None)

        entry = self.project / ".agents" / "skills" / "text-report" / "SKILL.md"
        command = next(
            line
            for line in entry.read_text(encoding="utf-8").splitlines()
            if "managed invoke" in line
        )
        session_id = "session-hook"
        history = self.root / ".codex" / "sessions" / "2026" / "hook.jsonl"
        history.parent.mkdir(parents=True)
        metadata = {
            "type": "session_meta",
            "payload": {
                "id": session_id,
                "session_id": session_id,
                "cwd": str(self.project.resolve()),
                "cli_version": "0.163.0-alpha.10.2",
                "source": "exec",
            },
        }
        history.write_text(json.dumps(metadata) + "\n", encoding="utf-8")
        payload = {
            "hook_event_name": "PreToolUse",
            "cwd": str(self.project),
            "turn_id": "turn-hook",
            "session_id": session_id,
            "tool_use_id": "tool-hook",
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "managed_request": {"skill": "text-report", "input": {"input": "fixture"}},
            "transcript_path": str(history),
            "prefix_length": history.stat().st_size,
        }
        environment = {
            "CODEX_SESSION_ID": session_id,
            "CODEX_THREAD_ID": session_id,
            "CODEX_TOOL_CALL_ID": "tool-hook",
            "CODEX_VERSION": "0.163.0-alpha.10.2",
        }
        with (
            self.assertRaises(SelcError) as captured,
            mock.patch("selc.managed_runtime.Path.home", return_value=self.root),
            mock.patch("selc.native_adapter.Path.home", return_value=self.root),
        ):
            receive_native_hook(
                project_root=self.project,
                suite_id="managed-reports",
                host="codex",
                payload=payload,
                environment=environment,
            )
        self.assertEqual(captured.exception.code, "NATIVE_REQUEST_UNAVAILABLE")
        database = open_database(self.project)
        try:
            recorded = database.execute(
                "SELECT phase, payload_json FROM managed_native_events"
            ).fetchone()
            context_count = database.execute("SELECT COUNT(*) FROM managed_contexts").fetchone()[0]
            task_count = database.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        finally:
            database.close()
        self.assertEqual(recorded["phase"], "PreToolUse")
        self.assertNotIn("fixture", recorded["payload_json"])
        self.assertEqual((context_count, task_count), (0, 0))

    def test_codex_hook_rejects_metadata_and_environment_identity_conflicts(self) -> None:
        session_id = "session-conflict"
        history = self.root / ".codex" / "sessions" / "2026" / "conflict.jsonl"
        history.parent.mkdir(parents=True)
        metadata = {
            "type": "session_meta",
            "payload": {
                "id": "other-session",
                "session_id": "other-session",
                "cwd": str(self.project.resolve()),
                "cli_version": "0.163.0-alpha.10.2",
                "source": "exec",
            },
        }
        history.write_text(json.dumps(metadata) + "\n", encoding="utf-8")
        payload = {
            "hook_event_name": "PreToolUse",
            "cwd": str(self.project),
            "turn_id": "turn-conflict",
            "session_id": session_id,
            "tool_use_id": "tool-conflict",
            "tool_name": "Bash",
            "tool_input": {"command": "printf conflict"},
            "transcript_path": str(history),
            "prefix_length": history.stat().st_size,
        }
        environment = {
            "CODEX_SESSION_ID": session_id,
            "CODEX_THREAD_ID": session_id,
            "CODEX_TOOL_CALL_ID": "tool-conflict",
            "CODEX_VERSION": "0.163.0-alpha.10.2",
        }
        with (
            self.assertRaises(SelcError) as captured,
            mock.patch("selc.native_adapter.Path.home", return_value=self.root),
        ):
            receive_native_hook(
                project_root=self.project,
                suite_id="managed-reports",
                host="codex",
                payload=payload,
                environment=environment,
            )
        self.assertEqual(captured.exception.code, "NATIVE_CONTEXT_CONFLICT")

        metadata["payload"]["id"] = session_id
        metadata["payload"]["session_id"] = session_id
        history.write_text(json.dumps(metadata) + "\n", encoding="utf-8")
        payload["prefix_length"] = history.stat().st_size
        environment["CODEX_VERSION"] = "0.147.0"
        with (
            self.assertRaises(SelcError) as captured,
            mock.patch("selc.native_adapter.Path.home", return_value=self.root),
        ):
            receive_native_hook(
                project_root=self.project,
                suite_id="managed-reports",
                host="codex",
                payload=payload,
                environment=environment,
            )
        self.assertEqual(captured.exception.code, "NATIVE_CONTEXT_CONFLICT")

    def test_installed_stub_rejects_a_pre_without_a_two_phase_root_proof(self) -> None:
        manifest_path = self.suite / "suite.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["managed"]["native_hosts"] = {"codex": ["0.163.0-alpha.10.2"]}
        custom_path = self.suite / "skills" / "custom-report"
        (self.suite / "skills" / "text-report").rename(custom_path)
        manifest["skills"][0]["path"] = "skills/custom-report"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        seal_suite(self.suite)
        install_suite(source=self.suite, project_root=self.project, harness="codex", ref=None)

        session_id = "session-native"
        generation_id = "generation-native"
        tool_id = "tool-native"
        request_id = tool_id
        host_history = self.root / ".codex" / "sessions" / "2026" / "native.jsonl"
        host_history.parent.mkdir(parents=True)
        metadata = {
            "type": "session_meta",
            "payload": {
                "id": session_id,
                "session_id": session_id,
                "cwd": str(self.project.resolve()),
                "cli_version": "0.163.0-alpha.10.2",
                "source": "exec",
            },
        }
        host_history.write_text(json.dumps(metadata) + "\n", encoding="utf-8")
        prompt = {
            "hook_event_name": "UserPromptSubmit",
            "client": "codex-desktop",
            "version": "0.163.0-alpha.10.2",
            "cwd": str(self.project),
            "session_id": session_id,
            "generation_id": generation_id,
            "request_id": request_id,
            "root_request_id": request_id,
            "prompt": json.dumps({"input": {"input": str(self.input_path)}}),
            "transcript_path": str(host_history),
            "prefix_length": host_history.stat().st_size,
        }
        with (
            mock.patch("selc.managed_runtime.Path.home", return_value=self.root),
            mock.patch("selc.native_adapter.Path.home", return_value=self.root),
        ):
            observed_prompt = receive_native_hook(
                project_root=self.project,
                suite_id="managed-reports",
                host="codex",
                payload=prompt,
                environment={},
            )
        self.assertEqual(observed_prompt["status"], "prompt_recorded")

        entry = self.project / ".agents" / "skills" / "text-report" / "SKILL.md"
        command = next(
            line
            for line in entry.read_text(encoding="utf-8").splitlines()
            if "managed invoke" in line
        )
        pre = {
            "hook_event_name": "PreToolUse",
            "client": "codex-desktop",
            "version": "0.163.0-alpha.10.2",
            "cwd": str(self.project),
            "session_id": session_id,
            "generation_id": generation_id,
            "request_id": request_id,
            "tool_use_id": tool_id,
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "transcript_path": str(host_history),
            "prefix_length": host_history.stat().st_size,
        }
        with (
            self.assertRaises(SelcError) as captured,
            mock.patch("selc.managed_runtime.Path.home", return_value=self.root),
            mock.patch("selc.native_adapter.Path.home", return_value=self.root),
        ):
            receive_native_hook(
                project_root=self.project,
                suite_id="managed-reports",
                host="codex",
                payload=pre,
                environment={},
            )
        self.assertEqual(captured.exception.code, "NATIVE_REQUEST_UNAVAILABLE")

    def test_native_prompt_boundary_supplies_root_proof_to_a_later_tool_receipt(self) -> None:
        manifest_path = self.suite / "suite.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["managed"]["native_hosts"] = {"codex": ["0.163.0-alpha.10.2"]}
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        seal_suite(self.suite)
        install_suite(source=self.suite, project_root=self.project, harness="codex", ref=None)

        environment = {
            "CODEX_SESSION_ID": "session-prompt",
            "CODEX_THREAD_ID": "session-prompt",
            "CODEX_TOOL_CALL_ID": "tool-prompt",
            "CODEX_VERSION": "0.163.0-alpha.10.2",
        }
        host_history = self.root / ".codex" / "sessions" / "2026" / "10" / "prompt.jsonl"
        host_history.parent.mkdir(parents=True)
        prompt_text = "Please create the requested report from the available input."
        metadata = b"".join(
            (json.dumps(record) + "\n").encode("utf-8")
            for record in (
                {
                    "type": "session_meta",
                    "payload": {
                        "id": "session-prompt",
                        "session_id": "session-prompt",
                        "cwd": str(self.project.resolve()),
                        "cli_version": "0.163.0-alpha.10.2",
                        "source": "exec",
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "task_started",
                        "turn_id": "generation-prompt",
                        "root_turn_id": "generation-prompt",
                        "turn_attribution": {
                            "turn_id": "generation-prompt",
                            "root_turn_id": "generation-prompt",
                            "parent_turn_id": None,
                        },
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "developer",
                        "internal_chat_message_metadata_passthrough": {
                            "turn_id": "generation-prompt",
                            "content_item_kinds": ["host_skills.instructions"],
                        },
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "injected"}],
                        "internal_chat_message_metadata_passthrough": {
                            "turn_id": "generation-prompt",
                            "content_item_kinds": ["agents_md.instructions"],
                        },
                    },
                },
                {"type": "world_state", "payload": {"full": True, "state": {}}},
                {
                    "type": "turn_context",
                    "payload": {
                        "turn_id": "generation-prompt",
                        "root_turn_id": "generation-prompt",
                        "cwd": str(self.project.resolve()),
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "thread_settings_applied",
                        "thread_id": "session-prompt",
                    },
                },
            )
        )
        host_history.write_bytes(metadata)
        prompt = {
            "hook_event_name": "UserPromptSubmit",
            "client": "codex-desktop",
            "version": "0.163.0-alpha.10.2",
            "cwd": str(self.project),
            "generation_id": "generation-prompt",
            "session_id": "session-prompt",
            "request_id": "request-prompt",
            "root_request_id": "request-prompt",
            "prompt": prompt_text,
            "transcript_path": str(host_history),
            "prefix_length": len(metadata),
        }
        with (
            mock.patch("selc.managed_runtime.Path.home", return_value=self.root),
            mock.patch("selc.native_adapter.Path.home", return_value=self.root),
        ):
            recorded_prompt = receive_native_hook(
                project_root=self.project,
                suite_id="managed-reports",
                host="codex",
                payload=prompt,
                environment=environment,
            )
        self.assertEqual(recorded_prompt["status"], "prompt_recorded")
        with host_history.open("ab") as stream:
            stream.write(
                (
                    json.dumps(
                        {
                            "type": "response_item",
                            "payload": {
                                "type": "message",
                                "role": "user",
                                "content": [{"type": "input_text", "text": prompt_text}],
                                "internal_chat_message_metadata_passthrough": {
                                    "turn_id": "generation-prompt",
                                    "content_item_kinds": ["user.text"],
                                },
                            },
                        }
                    )
                    + "\n"
                ).encode("utf-8")
            )

        command = json.dumps(
            {"action": "activate", "suite": "managed-reports", "skill": "text-report"},
            sort_keys=True,
        )
        pre_tool = {
            "hook_event_name": "PreToolUse",
            "client": "codex-desktop",
            "version": "0.163.0-alpha.10.2",
            "cwd": str(self.project),
            "generation_id": "generation-prompt",
            "session_id": "session-prompt",
            "request_id": "request-prompt",
            "tool_use_id": "tool-pre",
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "transcript_path": str(host_history),
            "prefix_length": host_history.stat().st_size,
        }
        environment["CODEX_TOOL_CALL_ID"] = "tool-pre"
        with (
            mock.patch("selc.managed_runtime.Path.home", return_value=self.root),
            mock.patch("selc.native_adapter.Path.home", return_value=self.root),
        ):
            recorded_pre = receive_native_hook(
                project_root=self.project,
                suite_id="managed-reports",
                host="codex",
                payload=pre_tool,
                environment=environment,
            )
            activated = managed_runtime.activate(
                project_root=self.project,
                suite_id="managed-reports",
                skill="text-report",
                receipt=str(recorded_pre["receipt"]),
            )
        self.assertEqual(activated["native_support"], "pending")

        with (
            mock.patch("selc.managed_runtime.Path.home", return_value=self.root),
            mock.patch("selc.native_adapter.Path.home", return_value=self.root),
        ):
            replayed_prompt = receive_native_hook(
                project_root=self.project,
                suite_id="managed-reports",
                host="codex",
                payload=prompt,
                environment=environment,
            )
        self.assertEqual(replayed_prompt["status"], "prompt_recorded")

        database = open_database(self.project)
        try:
            replayed_context = database.execute(
                "SELECT root_valid, request_digest, input_skill, unknown_change "
                "FROM managed_contexts"
            ).fetchone()
        finally:
            database.close()
        self.assertEqual(
            tuple(replayed_context),
            (1, "", "text-report", 0),
        )

        conflicting_prompt = dict(prompt)
        conflicting_prompt["prompt"] = '@text-report {"input":"changed"}'
        with (
            mock.patch("selc.managed_runtime.Path.home", return_value=self.root),
            mock.patch("selc.native_adapter.Path.home", return_value=self.root),
        ):
            conflict = receive_native_hook(
                project_root=self.project,
                suite_id="managed-reports",
                host="codex",
                payload=conflicting_prompt,
                environment=environment,
            )
        self.assertEqual(conflict["status"], "conflict")

        database = open_database(self.project)
        try:
            raw = database.execute("SELECT payload_json FROM managed_native_events").fetchall()
            context = database.execute(
                "SELECT root_valid, input_skill, unknown_change FROM managed_contexts"
            ).fetchone()
        finally:
            database.close()
        serialized = "".join(row["payload_json"] for row in raw)
        self.assertNotIn("fixture", serialized)
        self.assertEqual(
            (context["root_valid"], context["input_skill"], context["unknown_change"]),
            (1, "text-report", 1),
        )

    def test_native_tool_without_a_prompt_boundary_fails_closed(self) -> None:
        command = json.dumps(
            {"action": "activate", "suite": "managed-reports", "skill": "text-report"},
            sort_keys=True,
        )
        payload = {
            "hook_event_name": "PreToolUse",
            "client": "codex-desktop",
            "version": "0.163.0-alpha.10.2",
            "cwd": str(self.project),
            "generation_id": "generation-missing-prompt",
            "session_id": "session-missing-prompt",
            "request_id": "request-missing-prompt",
            "tool_use_id": "tool-missing-prompt",
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "transcript_path": str(
                self.root / ".codex" / "sessions" / "2026" / "missing-prompt.jsonl"
            ),
            "prefix_length": 0,
        }
        history = Path(str(payload["transcript_path"]))
        history.parent.mkdir(parents=True)
        metadata = {
            "type": "session_meta",
            "payload": {
                "id": "session-missing-prompt",
                "session_id": "session-missing-prompt",
                "cwd": str(self.project.resolve()),
                "cli_version": "0.163.0-alpha.10.2",
                "source": "exec",
            },
        }
        history.write_text(json.dumps(metadata) + "\n", encoding="utf-8")
        payload["prefix_length"] = history.stat().st_size
        environment = {
            "CODEX_SESSION_ID": "session-missing-prompt",
            "CODEX_THREAD_ID": "session-missing-prompt",
            "CODEX_TOOL_CALL_ID": "tool-missing-prompt",
            "CODEX_VERSION": "0.163.0-alpha.10.2",
        }
        with (
            self.assertRaises(SelcError) as captured,
            mock.patch("selc.managed_runtime.Path.home", return_value=self.root),
            mock.patch("selc.native_adapter.Path.home", return_value=self.root),
        ):
            receive_native_hook(
                project_root=self.project,
                suite_id="managed-reports",
                host="codex",
                payload=payload,
                environment=environment,
            )
        self.assertEqual(captured.exception.code, "NATIVE_REQUEST_UNAVAILABLE")

    def test_separate_requests_in_one_generation_do_not_share_a_context(self) -> None:
        first = self._payload("activate", "tool-request-one")
        first["request_id"] = "request-one"
        second = self._payload("activate", "tool-request-two")
        second["request_id"] = "request-two"
        managed_runtime.observe_request_boundary(
            project_root=self.project,
            suite_id="managed-reports",
            payload=first,
            fixture=True,
        )
        managed_runtime.observe_request_boundary(
            project_root=self.project,
            suite_id="managed-reports",
            payload=second,
            fixture=True,
        )
        database = open_database(self.project)
        try:
            contexts = database.execute("SELECT request_id FROM managed_contexts").fetchall()
        finally:
            database.close()
        self.assertEqual({row["request_id"] for row in contexts}, {"request-one", "request-two"})

    def test_reservation_remains_a_gap_when_publish_target_conflicts(self) -> None:
        activated = self._activate()
        action = self._run_text_action(activated)
        target = self.project / "reports" / "exists.md"
        target.parent.mkdir(parents=True)
        target.write_text("external", encoding="utf-8")
        receipt = self._observe("publish", "tool-conflict-target")
        with self.assertRaises(SelcError) as captured:
            managed_runtime.publish(
                project_root=self.project,
                suite_id="managed-reports",
                task_id=activated["task_id"],
                call_id=activated["call_id"],
                receipt=receipt,
                artifact_path=Path(action["artifact_path"]),
                target="reports/exists.md",
            )
        self.assertEqual(captured.exception.code, "PUBLICATION_TARGET_CONFLICT")
        database = open_database(self.project)
        try:
            reservation = database.execute(
                "SELECT ordinal, status FROM managed_publications"
            ).fetchone()
        finally:
            database.close()
        self.assertEqual((reservation["ordinal"], reservation["status"]), (1, "target_conflict"))

    def test_existing_owned_target_is_replaced_by_darwin_exchange(self) -> None:
        activated = self._activate()
        first = self._run_text_action(activated)
        first_receipt = self._observe("publish", "tool-first-publish")
        managed_runtime.publish(
            project_root=self.project,
            suite_id="managed-reports",
            task_id=activated["task_id"],
            call_id=activated["call_id"],
            receipt=first_receipt,
            artifact_path=Path(first["artifact_path"]),
            target="reports/correctable.md",
        )
        second = self._run_text_action(activated, tool_use_id="tool-second-action")
        second_receipt = self._observe("publish", "tool-second-publish")
        target = self.project / "reports" / "correctable.md"
        original = target.read_bytes()
        replacement = managed_runtime.publish(
            project_root=self.project,
            suite_id="managed-reports",
            task_id=activated["task_id"],
            call_id=activated["call_id"],
            receipt=second_receipt,
            artifact_path=Path(second["artifact_path"]),
            target="reports/correctable.md",
        )
        self.assertEqual(replacement["status"], "published")
        self.assertEqual(target.read_bytes(), original)
        target.write_text("externally changed", encoding="utf-8")
        third = self._run_text_action(activated, tool_use_id="tool-third-action")
        third_receipt = self._observe("publish", "tool-third-publish")
        with self.assertRaises(SelcError) as captured:
            managed_runtime.publish(
                project_root=self.project,
                suite_id="managed-reports",
                task_id=activated["task_id"],
                call_id=activated["call_id"],
                receipt=third_receipt,
                artifact_path=Path(third["artifact_path"]),
                target="reports/correctable.md",
            )
        self.assertEqual(captured.exception.code, "PUBLICATION_TARGET_CONFLICT")

    def test_same_bytes_user_replacement_is_not_an_owned_exchange_target(self) -> None:
        target = self.project / "reports" / "same-bytes.md"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"managed bytes")
        original = target.stat()
        staged = self.root / "same-bytes-staged"
        staged.write_bytes(b"replacement bytes")
        intent = managed_runtime._legacy_publication_intent(
            staged,
            target,
            hashlib.sha256(b"managed bytes").hexdigest(),
        )
        replacement = self.root / "user-replacement"
        replacement.write_bytes(b"managed bytes")
        os.replace(replacement, target)
        self.assertNotEqual(target.stat().st_ino, original.st_ino)

        with self.assertRaises(SelcError) as captured:
            managed_runtime._supply_publication(staged, target, intent=intent)

        self.assertEqual(captured.exception.code, "PUBLICATION_TARGET_CONFLICT")
        self.assertEqual(target.read_bytes(), b"managed bytes")
        self.assertTrue(staged.is_file())

    def test_owned_target_prefers_the_latest_publication_ordinal_over_equal_timestamps(
        self,
    ) -> None:
        target = self.project / "reports" / "ordinal.md"
        target.parent.mkdir(parents=True)
        database = open_database(self.project)
        try:
            timestamp = records.utc_now()
            expected_digest = ""
            for ordinal in range(1, 4):
                staged = self.root / f"ordinal-staged-{ordinal}"
                staged.write_bytes(f"version {ordinal}".encode("utf-8"))
                if ordinal == 3:
                    os.link(staged, target)
                    expected_digest = hashlib.sha256(staged.read_bytes()).hexdigest()
                staged_stat = staged.stat()
                database.execute(
                    """INSERT INTO managed_publications(
                    delivery_id, suite_id, task_id, call_id, receipt_digest, action_run_id,
                    ordinal, artifact_digest, staging_path, target_path, status, detail, reserved_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'recorded', '{}', ?)""",
                    (
                        f"ordinal-delivery-{ordinal}",
                        "managed-reports",
                        "ordinal-task",
                        "ordinal-call",
                        f"ordinal-receipt-{ordinal}",
                        f"ordinal-action-{ordinal}",
                        ordinal,
                        hashlib.sha256(staged.read_bytes()).hexdigest(),
                        str(staged),
                        str(target),
                        timestamp,
                    ),
                )
                database.execute(
                    """INSERT INTO managed_publication_intents(
                    delivery_id, suite_id, task_id, target_path, staged_path, staged_digest,
                    staged_device, staged_inode, staging_parent_device, staging_parent_inode,
                    state, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'recorded', ?, ?)""",
                    (
                        f"ordinal-delivery-{ordinal}",
                        "managed-reports",
                        "ordinal-task",
                        str(target),
                        str(staged),
                        hashlib.sha256(staged.read_bytes()).hexdigest(),
                        staged_stat.st_dev,
                        staged_stat.st_ino,
                        staged.parent.stat().st_dev,
                        staged.parent.stat().st_ino,
                        timestamp,
                        timestamp,
                    ),
                )
            self.assertEqual(
                managed_runtime._owned_target_digest(database, "ordinal-task", target),
                expected_digest,
            )
        finally:
            database.close()

    def test_parent_symlink_replacement_cannot_redirect_publication(self) -> None:
        reports = self.project / "reports"
        reports.mkdir()
        target = reports / "redirected.md"
        staged = self.root / "redirected-staged"
        staged.write_bytes(b"managed bytes")
        intent = managed_runtime._legacy_publication_intent(staged, target, None)
        original_reports = self.project / "reports-original"
        outside = self.root / "outside"
        outside.mkdir()
        reports.rename(original_reports)
        reports.symlink_to(outside, target_is_directory=True)

        with self.assertRaises(SelcError) as captured:
            managed_runtime._supply_publication(staged, target, intent=intent)

        self.assertEqual(captured.exception.code, "PUBLICATION_TARGET_CONFLICT")
        self.assertFalse((outside / "redirected.md").exists())
        self.assertTrue(staged.is_file())

    def test_exchange_rejects_a_target_path_replaced_after_directory_open(self) -> None:
        """A descriptor-safe exchange is still uncertain after path retargeting."""

        reports = self.project / "reports"
        reports.mkdir()
        target = reports / "late-redirected.md"
        target.write_bytes(b"old managed bytes")
        staged = self.root / "late-redirected-staged"
        staged.write_bytes(b"replacement bytes")
        intent = managed_runtime._legacy_publication_intent(
            staged,
            target,
            hashlib.sha256(b"old managed bytes").hexdigest(),
        )
        original_reports = self.project / "reports-original"
        outside = self.root / "outside"
        outside.mkdir()
        original_exchange = managed_runtime._rename_exchange_at

        def exchange_then_replace_parent(
            staged_directory: int,
            staged_name: str,
            target_directory: int,
            target_name: str,
        ) -> None:
            original_exchange(staged_directory, staged_name, target_directory, target_name)
            reports.rename(original_reports)
            reports.symlink_to(outside, target_is_directory=True)

        with (
            mock.patch(
                "selc.managed_runtime._rename_exchange_at",
                side_effect=exchange_then_replace_parent,
            ),
            self.assertRaises(SelcError) as captured,
        ):
            managed_runtime._supply_publication(staged, target, intent=intent)

        self.assertEqual(captured.exception.code, "PUBLICATION_EXCHANGE_UNCERTAIN")
        self.assertEqual(
            (original_reports / "late-redirected.md").read_bytes(), b"replacement bytes"
        )
        self.assertFalse((outside / "late-redirected.md").exists())

    def test_recovery_preserves_unknown_bytes_displaced_by_an_interrupted_exchange(self) -> None:
        target = self.project / "reports" / "interrupted.md"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"old managed bytes")
        staged = self.root / "interrupted-staged"
        staged.write_bytes(b"replacement bytes")
        delivery_id = "interrupted-delivery"
        database = open_database(self.project)
        try:
            with records.projection_transaction(database):
                intent = managed_runtime._record_publication_intent(
                    database,
                    delivery_id=delivery_id,
                    suite_id="managed-reports",
                    task_id="interrupted-task",
                    staged_path=staged,
                    target_path=target,
                    previous_owned_digest=hashlib.sha256(b"old managed bytes").hexdigest(),
                )

            original_exchange = managed_runtime._rename_exchange_at

            def replace_target_then_exchange(
                staged_directory: int,
                staged_name: str,
                target_directory: int,
                target_name: str,
            ) -> None:
                os.unlink(target_name, dir_fd=target_directory)
                descriptor = os.open(
                    target_name,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                    dir_fd=target_directory,
                )
                try:
                    os.write(descriptor, b"user bytes")
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                original_exchange(staged_directory, staged_name, target_directory, target_name)

            with (
                mock.patch(
                    "selc.managed_runtime._rename_exchange_at",
                    side_effect=replace_target_then_exchange,
                ),
                mock.patch(
                    "selc.managed_runtime._fsync_file_descriptor",
                    side_effect=RuntimeError("crash after exchange"),
                ),
                self.assertRaisesRegex(RuntimeError, "crash after exchange"),
            ):
                managed_runtime._supply_publication(staged, target, intent=intent)

            preserved = managed_runtime._recover_interrupted_publication_staging(
                database, delivery_id
            )
        finally:
            database.close()

        self.assertEqual(target.read_bytes(), b"replacement bytes")
        self.assertIsNotNone(preserved)
        self.assertEqual(Path(str(preserved)).read_bytes(), b"user bytes")
        self.assertFalse(staged.exists())

    def test_hardlink_failure_leaves_no_streamed_visible_target(self) -> None:
        staged = self.root / "staged"
        target = self.project / "reports" / "atomic.md"
        staged.write_bytes(b"complete bytes")
        with (
            mock.patch("selc.managed_runtime.os.link", side_effect=OSError("cross-device")),
            self.assertRaises(SelcError) as captured,
        ):
            managed_runtime._supply_publication(
                staged,
                target,
                previous_owned_digest=None,
            )
        self.assertEqual(captured.exception.code, "PUBLICATION_ATOMIC_UNAVAILABLE")
        self.assertFalse(target.exists())
        self.assertTrue(staged.exists())

    def test_target_created_after_precheck_is_preserved(self) -> None:
        staged = self.root / "late-staged"
        target = self.project / "reports" / "late-user-file.md"
        staged.write_bytes(b"managed bytes")

        def user_creates_target(*_arguments: object, **_keywords: object) -> None:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"user bytes")
            raise FileExistsError(target)

        with (
            mock.patch("selc.managed_runtime.os.link", side_effect=user_creates_target),
            self.assertRaises(SelcError) as captured,
        ):
            managed_runtime._supply_publication(
                staged,
                target,
                previous_owned_digest=None,
            )
        self.assertEqual(captured.exception.code, "PUBLICATION_TARGET_CONFLICT")
        self.assertEqual(target.read_bytes(), b"user bytes")
        self.assertTrue(staged.exists())

    def test_fixed_python_action_uses_the_declared_json_checker(self) -> None:
        suite = self.root / "json-suite"
        project = self.root / "json-project"
        create_suite("json-suite", "1.0.0", suite, ["json-summary"])
        script = suite / "bin" / "json-action.py"
        script.write_text(
            "import argparse\n"
            "import json\n"
            "from pathlib import Path\n"
            "parser = argparse.ArgumentParser()\n"
            "parser.add_argument('managed-run')\n"
            "parser.add_argument('--request')\n"
            "parser.add_argument('--output')\n"
            "args = parser.parse_args()\n"
            "request = json.loads(Path(args.request).read_text())\n"
            "items = json.loads(Path(request['input']).read_text())['items']\n"
            "Path(args.output).write_text(json.dumps({'count': len(items), 'sum': sum(item['value'] for item in items)}))\n",
            encoding="utf-8",
        )
        manifest_path = suite / "suite.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["managed"] = {
            "protocol": 1,
            "actions": {
                "json-summary": {
                    "skill": "json-summary",
                    "kind": "python-script-v1",
                    "script": "bin/json-action.py",
                    "input": {"fields": {"input": {"type": "path"}}, "required": ["input"]},
                },
                "instruction-artifact": {
                    "skill": "json-summary",
                    "kind": "instruction-staging-v1",
                    "input": {"fields": {}, "required": []},
                },
            },
        }
        manifest["artifact_checks"] = {
            "json-summary": {
                "checker": {"name": "json-item-summary", "version": "1"},
                "rule_version": "summary-v1",
                "required_rule_ids": ["count", "sum"],
                "configuration": {"duplicate_ids_count_once": False},
            }
        }
        manifest["skills"][0]["artifact_check"] = "json-summary"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        seal_suite(suite)
        install_suite(source=suite, project_root=project, harness="codex", ref=None)

        input_path = self.root / "items.json"
        input_path.write_text(
            json.dumps({"items": [{"id": "a", "value": 2}, {"id": "b", "value": 3}]}),
            encoding="utf-8",
        )
        transcript = project / "native.jsonl"
        transcript.write_bytes(b"")

        def observe(action: str, tool_id: str) -> str:
            payload = {
                "host": "codex",
                "surface": "fixture",
                "client_version": "fixture",
                "cwd": str(project),
                "session_id": "session-json",
                "generation_id": "generation-json",
                "root_request_id": "root-json",
                "tool_use_id": tool_id,
                "phase": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {
                    "command": json.dumps(
                        {"action": action, "suite": "json-suite", "skill": "json-summary"},
                        sort_keys=True,
                    )
                },
                "request": {"skill": "json-summary", "input": {"input": str(input_path)}},
                "transcript_path": str(transcript),
                "prefix_length": 0,
            }
            result = managed_runtime.observe_request_boundary(
                project_root=project,
                suite_id="json-suite",
                payload=payload,
                fixture=True,
            )
            return str(result["receipt"])

        activation = managed_runtime.activate(
            project_root=project,
            suite_id="json-suite",
            skill="json-summary",
            receipt=observe("activate", "tool-json-activate"),
        )
        action = managed_runtime.run_action(
            project_root=project,
            suite_id="json-suite",
            skill="json-summary",
            task_id=str(activation["task_id"]),
            call_id=str(activation["call_id"]),
            action_id="json-summary",
            receipt=observe("json-summary", "tool-json-action"),
            request={"input": str(input_path)},
        )
        published = managed_runtime.publish(
            project_root=project,
            suite_id="json-suite",
            task_id=str(activation["task_id"]),
            call_id=str(activation["call_id"]),
            receipt=observe("publish", "tool-json-publish"),
            artifact_path=Path(str(action["artifact_path"])),
            target="reports/summary.json",
        )
        self.assertEqual(published["status"], "published")
        database = open_database(project)
        try:
            record_feedback(
                database,
                feedback_id="first-delivery-failure",
                task_id=str(activation["task_id"]),
                delivery_id=str(published["delivery_id"]),
                revision=1,
                scope="first",
                result="fail",
                body="known first delivery failure",
            )
        finally:
            database.close()
        first_failure = managed_runtime.health(project, "json-suite", str(activation["task_id"]))
        self.assertEqual(
            (first_failure["first_result"], first_failure["final_result"]), ("fail", "fail")
        )

        later_action = managed_runtime.run_action(
            project_root=project,
            suite_id="json-suite",
            skill="json-summary",
            task_id=str(activation["task_id"]),
            call_id=str(activation["call_id"]),
            action_id="json-summary",
            receipt=observe("json-summary", "tool-json-later-action"),
            request={"input": str(input_path)},
        )
        with (
            mock.patch(
                "selc.managed_runtime._supply_publication", side_effect=RuntimeError("crash")
            ),
            self.assertRaisesRegex(RuntimeError, "crash"),
        ):
            managed_runtime.publish(
                project_root=project,
                suite_id="json-suite",
                task_id=str(activation["task_id"]),
                call_id=str(activation["call_id"]),
                receipt=observe("publish", "tool-json-crashed-publish"),
                artifact_path=Path(str(later_action["artifact_path"])),
                target="reports/crashed.json",
            )
        self.assertEqual(managed_runtime.recover(project, "json-suite"), {"recovered": 1})
        after_gap = managed_runtime.health(project, "json-suite", str(activation["task_id"]))
        self.assertEqual(
            (after_gap["first_result"], after_gap["final_result"]), ("fail", "unknown")
        )

        staged = managed_runtime.stage_instruction_artifact(
            project_root=project,
            suite_id="json-suite",
            task_id=str(activation["task_id"]),
            call_id=str(activation["call_id"]),
            action_id="instruction-artifact",
            artifact_bytes=b'{"count":0,"sum":0}',
            checker_input_bytes=b'{"items":[]}',
        )
        self.assertTrue(Path(str(staged["artifact_path"])).is_file())
        staged_publication = managed_runtime.publish(
            project_root=project,
            suite_id="json-suite",
            task_id=str(activation["task_id"]),
            call_id=str(activation["call_id"]),
            receipt=observe("publish", "tool-instruction-publish"),
            artifact_path=Path(str(staged["artifact_path"])),
            target="reports/instruction.json",
        )
        self.assertEqual(staged_publication["status"], "published")
        repaired = managed_runtime.health(project, "json-suite", str(activation["task_id"]))
        self.assertEqual((repaired["first_result"], repaired["final_result"]), ("fail", "pass"))
        database = open_database(project)
        try:
            self.assertEqual(
                database.execute("SELECT checker_result FROM deliveries").fetchone()[0], "pass"
            )
        finally:
            database.close()

    def test_protocol_two_writer_cannot_mutate_managed_protocol_three_tables(self) -> None:
        database = open_database(self.project)
        database.close()
        legacy = sqlite3.connect(self.project / ".selc" / "records.sqlite3")
        legacy.create_function("selc_writer_protocol", 0, lambda: 2)
        with self.assertRaises(sqlite3.DatabaseError):
            legacy.execute(
                "INSERT INTO managed_native_events("
                "event_id, stable_identity, payload_digest, payload_json, host, suite_id, "
                "session_id, generation_id, tool_use_id, phase, received_at, status) "
                "VALUES ('legacy', 'legacy', 'x', '{}', 'codex', 'managed-reports', "
                "'s', 'g', 't', 'PreToolUse', '2026-01-01T00:00:00Z', 'recorded')"
            )
        legacy.close()

    def test_real_protocol_two_database_migrates_feedback_before_protocol_three_guards(
        self,
    ) -> None:
        legacy_source = self.root / "legacy-source"
        archive = subprocess.run(
            ["git", "archive", "d17d7bd", "src/selc"],
            cwd=REPO_ROOT,
            capture_output=True,
            check=True,
        ).stdout
        with tarfile.open(fileobj=io.BytesIO(archive)) as extracted:
            extracted.extractall(legacy_source)
        old_project = self.root / "old-project"
        initializer = """
from pathlib import Path
from selc.records import open_database

database = open_database(Path(__import__('sys').argv[1]))
database.execute("INSERT INTO tasks(task_id, suite_id, suite_version, harness, initiator, evidence, is_test, status, created_at, owner_origin) VALUES ('task', 'suite', '1', 'codex', 'user', 'old', 0, 'closed', '2026-01-01T00:00:00Z', 'origin')")
database.execute("INSERT INTO deliveries(delivery_id, task_id, artifact_hash, rule_version, declared_result, effective_result, complete_fixture, sequence, created_at) VALUES ('delivery', 'task', 'hash', 'rule', 'pass', 'pass', 1, 1, '2026-01-01T00:00:00Z')")
database.execute("INSERT INTO feedback(feedback_id, task_id, delivery_id, revision, scope, result, created_at) VALUES ('feedback', 'task', 'delivery', 1, 'final', 'fail', '2026-01-01T00:00:00Z')")
database.commit()
database.close()
"""
        completed = subprocess.run(
            [sys.executable, "-c", initializer, str(old_project)],
            env={**os.environ, "PYTHONPATH": str(legacy_source / "src")},
            capture_output=True,
            check=False,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

        database = open_database(old_project)
        try:
            self.assertEqual(
                database.execute("SELECT value FROM meta WHERE key = 'writer_protocol'").fetchone()[
                    0
                ],
                "3",
            )
            self.assertEqual(
                database.execute(
                    "SELECT result FROM feedback_revisions WHERE feedback_id = 'feedback'"
                ).fetchone()[0],
                "fail",
            )
        finally:
            database.close()

    def test_failed_protocol_three_migration_rolls_back_to_protocol_two_guards(self) -> None:
        legacy_source = self.root / "legacy-source"
        archive = subprocess.run(
            ["git", "archive", "d17d7bd", "src/selc"],
            cwd=REPO_ROOT,
            capture_output=True,
            check=True,
        ).stdout
        with tarfile.open(fileobj=io.BytesIO(archive)) as extracted:
            extracted.extractall(legacy_source)
        old_project = self.root / "old-project"
        initializer = "from pathlib import Path; from selc.records import open_database; open_database(Path(__import__('sys').argv[1])).close()"
        completed = subprocess.run(
            [sys.executable, "-c", initializer, str(old_project)],
            env={**os.environ, "PYTHONPATH": str(legacy_source / "src")},
            capture_output=True,
            check=False,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        with (
            mock.patch(
                "selc.records._create_managed_runtime_tables", side_effect=RuntimeError("stop")
            ),
            self.assertRaises(RuntimeError),
        ):
            open_database(old_project)
        legacy = sqlite3.connect(old_project / ".selc" / "records.sqlite3")
        try:
            legacy.create_function("selc_writer_protocol", 0, lambda: 2)
            protocol = legacy.execute(
                "SELECT value FROM meta WHERE key = 'writer_protocol'"
            ).fetchone()[0]
            trigger = legacy.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = 'writer_protocol_feedback_insert'"
            ).fetchone()[0]
        finally:
            legacy.close()
        self.assertEqual(protocol, "2")
        self.assertIn("!= 2", trigger)

    def test_installed_wrapper_rejects_manual_receipts_for_normal_operations(self) -> None:
        request_path = self.root / "request.json"
        request_path.write_text(json.dumps({"input": str(self.input_path)}), encoding="utf-8")
        wrapper = (
            self.project
            / ".selc"
            / "installs"
            / "managed-reports"
            / "current"
            / "bin"
            / "selc-suite.py"
        )
        completed = subprocess.run(
            [
                sys.executable,
                str(wrapper),
                "managed",
                "action",
                "--project-root",
                str(self.project),
                "--suite",
                "managed-reports",
                "--skill",
                "text-report",
                "--task-id",
                "manual-task",
                "--call-id",
                "manual-call",
                "--action",
                "text-report",
                "--receipt",
                "opaque-token",
                "--request",
                str(request_path),
            ],
            capture_output=True,
            check=False,
            text=True,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn('"code": "INVALID_ARGUMENT"', completed.stdout)

    def test_installed_wrapper_stages_instruction_bytes_from_a_native_receipt(self) -> None:
        manifest_path = self.suite / "suite.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["managed"]["native_hosts"] = {"codex": ["0.163.0-alpha.10.2"]}
        manifest["managed"]["actions"]["instruction-report"] = {
            "skill": "text-report",
            "kind": "instruction-staging-v1",
            "input": {"fields": {}, "required": []},
        }
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        seal_suite(self.suite)
        self.installation = install_suite(
            source=self.suite,
            project_root=self.project,
            harness="codex",
            ref=None,
        )
        activated = self._activate()
        artifact_bytes = self.root / "instruction-artifact.txt"
        checker_input = self.root / "instruction-checker.json"
        artifact_bytes.write_bytes(b"instruction result\n")
        checker_input.write_bytes(b"{}")
        wrapper = (
            self.project
            / ".selc"
            / "installs"
            / "managed-reports"
            / "current"
            / "bin"
            / "selc-suite.py"
        )
        command = [
            sys.executable,
            str(wrapper),
            "managed",
            "stage",
            "--project-root",
            str(self.project),
            "--suite",
            "managed-reports",
            "--skill",
            "text-report",
            "--task-id",
            str(activated["task_id"]),
            "--call-id",
            str(activated["call_id"]),
            "--action",
            "instruction-report",
            "--artifact-bytes",
            str(artifact_bytes),
            "--checker-input",
            str(checker_input),
            "--host",
            "codex",
            "--expected-version",
            str(self.installation["version"]),
            "--expected-composite",
            str(self.installation["digest"]),
        ]
        payload = self._payload("stage", "tool-instruction-stage")
        payload["tool_input"] = {"command": shlex.join(command)}
        observed = managed_runtime.observe_request_boundary(
            project_root=self.project,
            suite_id="managed-reports",
            payload=payload,
            fixture=True,
        )
        self.assertEqual(observed["status"], "recorded")
        completed = subprocess.run(
            command,
            env={
                **os.environ,
                "CODEX_SESSION_ID": "session-a",
                "CODEX_THREAD_ID": "session-a",
                "CODEX_TOOL_CALL_ID": "tool-instruction-stage",
                "CODEX_VERSION": "0.163.0-alpha.10.2",
            },
            capture_output=True,
            check=False,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        staged = json.loads(completed.stdout)["managed"]
        self.assertEqual(
            Path(str(staged["artifact_path"])).read_bytes(), artifact_bytes.read_bytes()
        )

    def test_installed_wrapper_rejects_a_stale_expected_identity(self) -> None:
        wrapper = (
            self.project
            / ".selc"
            / "installs"
            / "managed-reports"
            / "current"
            / "bin"
            / "selc-suite.py"
        )
        completed = subprocess.run(
            [
                sys.executable,
                str(wrapper),
                "managed",
                "invoke",
                "--project-root",
                str(self.project),
                "--suite",
                "managed-reports",
                "--skill",
                "text-report",
                "--host",
                "codex",
                "--expected-version",
                "0.0.0-stale",
                "--expected-composite",
                str(self.installation["digest"]),
            ],
            env={
                **os.environ,
                "CODEX_SESSION_ID": "session-stale",
                "CODEX_THREAD_ID": "session-stale",
                "CODEX_TOOL_CALL_ID": "tool-stale",
                "CODEX_VERSION": "0.163.0-alpha.10.2",
            },
            capture_output=True,
            check=False,
            text=True,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn('"code": "INSTALLATION_IDENTITY_DRIFT"', completed.stdout)

    def _wrapper_json(self, *arguments: str) -> dict[str, object]:
        wrapper = (
            self.project
            / ".selc"
            / "installs"
            / "managed-reports"
            / "current"
            / "bin"
            / "selc-suite.py"
        )
        completed = subprocess.run(
            [sys.executable, str(wrapper), *arguments],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        return json.loads(completed.stdout)

    def _activate(self) -> dict[str, object]:
        self._last_receipt = self._observe("activate", "tool-activate")
        return managed_runtime.activate(
            project_root=self.project,
            suite_id="managed-reports",
            skill="text-report",
            receipt=self._last_receipt,
        )

    def _run_text_action(
        self, activated: dict[str, object], *, tool_use_id: str = "tool-action"
    ) -> dict[str, object]:
        receipt = self._observe("text-report", tool_use_id)
        return managed_runtime.run_action(
            project_root=self.project,
            suite_id="managed-reports",
            skill="text-report",
            task_id=str(activated["task_id"]),
            call_id=str(activated["call_id"]),
            action_id="text-report",
            receipt=receipt,
            request={"input": str(self.input_path)},
        )

    def _observe(self, action: str, tool_use_id: str, *, phase: str = "PreToolUse") -> str:
        observed = managed_runtime.observe_request_boundary(
            project_root=self.project,
            suite_id="managed-reports",
            payload=self._payload(action, tool_use_id, phase=phase),
            fixture=True,
        )
        return str(observed.get("receipt", ""))

    def _stop(self, tool_use_id: str) -> dict[str, object]:
        payload = self._payload("publish", tool_use_id, phase="Stop")
        payload.pop("tool_name")
        payload.pop("tool_input")
        payload["request"] = {"source": "user-prompt"}
        return managed_runtime.observe_request_boundary(
            project_root=self.project,
            suite_id="managed-reports",
            payload=payload,
            fixture=True,
        )

    def _payload(
        self, action: str, tool_use_id: str, *, phase: str = "PreToolUse"
    ) -> dict[str, object]:
        command = json.dumps(
            {"action": action, "suite": "managed-reports", "skill": "text-report"},
            sort_keys=True,
        )
        return {
            "source": "synthetic",
            "host": "codex",
            "surface": "codex-desktop",
            "client_version": "0.163.0-alpha.10.2",
            "cwd": str(self.project),
            "session_id": "session-a",
            "generation_id": "generation-a",
            "root_request_id": "root-a",
            "tool_use_id": tool_use_id,
            "phase": phase,
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "action": action,
            "request": {"skill": "text-report", "input": {"input": str(self.input_path)}},
            "transcript_path": str(self.transcript),
            "prefix_length": 0,
        }


if __name__ == "__main__":
    unittest.main()
