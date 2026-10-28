"""Synthetic contract coverage for the managed runtime.

These tests exercise the installed runtime with native-shaped fixture payloads.
They deliberately do not assert desktop-host support.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from contextlib import suppress
from pathlib import Path
from unittest import mock

from selc import managed_runtime, records
from selc.errors import SelcError
from selc.execution_lease import ProcessIdentity, assert_no_live_execution, process_group_is_live
from selc.install import current_installation, install_suite
from selc.manifest import create_suite, seal_suite
from selc.native_adapter import (
    _managed_prompt_request,
    _native_causation,
    _workbuddy_skill_request,
    receive_native_hook,
)
from selc.records import close_task, delete_records, open_database, record_feedback

REPO_ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_FIVE_RUNTIME_ARCHIVE = REPO_ROOT / "tests" / "fixtures" / "c20-protocol5-runtime.zip"
PROTOCOL_FIVE_RUNTIME_METADATA = REPO_ROOT / "tests" / "fixtures" / "c20-protocol5-runtime.json"


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
            mode="observed",
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
        for event_name in ("PreToolUse", "PostToolUse"):
            managed_entries = [entry for entry in hooks[event_name] if "hooks" in entry]
            self.assertEqual(len(managed_entries), 1)
            self.assertEqual(managed_entries[0]["matcher"], "^(Bash|exec_command|shell|Skill)$")
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

        same_project_prefix = f"cd {self.project.resolve()} && {command}"
        prefixed = managed_runtime.parse_installed_managed_command(
            same_project_prefix,
            "managed-reports",
            project_root=self.project,
        )
        self.assertIsNotNone(prefixed)
        self.assertEqual(prefixed, parsed)

        project_alias = self.root / "project-alias"
        project_alias.symlink_to(self.project, target_is_directory=True)
        canonical_alias_prefix = f"cd {project_alias} && {command}"
        self.assertEqual(
            managed_runtime.parse_installed_managed_command(
                canonical_alias_prefix,
                "managed-reports",
                project_root=self.project,
            ),
            parsed,
        )

        other_project = self.root / "other-project"
        other_project.mkdir()
        current_install = self.project / ".selc" / "installs" / "managed-reports" / "current"
        logical_escape = current_install / "../../../../.."
        self.assertEqual(logical_escape.resolve(), self.project.resolve())
        invalid_prefixes = (
            f"cd relative-project && {command}",
            f"cd {other_project} && {command}",
            f"cd {logical_escape} && {command}",
            f"cd $PROJECT_ROOT && {command}",
            f"cd $(pwd) && {command}",
            f"cd {self.project.resolve()}\x00 && {command}",
            f"cd {self.project.resolve()} '&&' {command}",
            f"cd {self.project.resolve()} && {command} && echo extra",
            f"cd {self.project.resolve()} && {command} | cat",
            f"cd {self.project.resolve()} && {command} > output.json",
        )
        self.assertIsNone(
            managed_runtime.parse_installed_managed_command(
                same_project_prefix,
                "managed-reports",
            )
        )
        for candidate in invalid_prefixes:
            self.assertIsNone(
                managed_runtime.parse_installed_managed_command(
                    candidate,
                    "managed-reports",
                    project_root=self.project,
                ),
                candidate,
            )

        impostor = shlex.split(command)
        impostor[1] = str(self.root / "impostor" / "selc-suite.py")
        self.assertIsNone(
            managed_runtime.parse_installed_managed_command(
                shlex.join(impostor),
                "managed-reports",
                project_root=self.project,
            )
        )

    def test_codex_discovery_read_requires_native_output_and_frozen_entry_identity(self) -> None:
        """Only one installed routing entry may be read before managed activation."""

        entry = self.project / ".agents" / "skills" / "text-report" / "SKILL.md"
        command = shlex.join(("/bin/cat", str(entry)))
        binding = managed_runtime._discovery_read_binding(
            self.project,
            "managed-reports",
            current_installation(self.project, "managed-reports"),
            command,
        )
        self.assertIsNotNone(binding)
        assert binding is not None

        event = {
            "session_id": "discovery-session",
            "generation_id": "discovery-turn",
            "cwd": str(self.project.resolve()),
            "tool_command_digest": hashlib.sha256(command.encode("utf-8")).hexdigest(),
        }
        completion = {
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "thread_id": "discovery-session",
                "turn_id": "discovery-turn",
                "item": {
                    "type": "CommandExecution",
                    "id": "discovery-tool",
                    "command": ["/bin/zsh", "-lc", command],
                    "cwd": str(self.project.resolve()),
                    "status": "completed",
                    "exit_code": 0,
                    "aggregated_output": entry.read_text(encoding="utf-8"),
                },
            },
        }
        self.assertEqual(
            managed_runtime._codex_discovery_read_status(
                [completion], event, "discovery-tool", binding
            ),
            "closed",
        )
        original_stat = os.stat
        reader_values = list(original_stat("/bin/cat"))
        reader_values[1] += 1
        replacement_reader_stat = os.stat_result(reader_values)

        def replaced_reader_stat(
            path: str | os.PathLike[str],
            *arguments: object,
            **keyword_arguments: object,
        ) -> os.stat_result:
            if os.fspath(path) == "/bin/cat":
                return replacement_reader_stat
            return original_stat(path, *arguments, **keyword_arguments)

        with mock.patch("selc.managed_runtime.os.stat", side_effect=replaced_reader_stat):
            self.assertEqual(
                managed_runtime._codex_discovery_read_status(
                    [completion], event, "discovery-tool", binding
                ),
                "invalid",
            )
        self.assertIsNone(
            managed_runtime._discovery_read_binding(
                self.project,
                "managed-reports",
                current_installation(self.project, "managed-reports"),
                f"{command} && printf injected",
            )
        )

        output_mismatch = json.loads(json.dumps(completion))
        output_mismatch["payload"]["item"]["aggregated_output"] = "wrong entry\n"
        self.assertEqual(
            managed_runtime._codex_discovery_read_status(
                [output_mismatch], event, "discovery-tool", binding
            ),
            "invalid",
        )
        self.assertEqual(
            managed_runtime._codex_discovery_read_status([], event, "discovery-tool", binding),
            "pending",
        )

        original_bytes = entry.read_bytes()
        try:
            entry.write_bytes(b"replaced entry\n")
            self.assertEqual(
                managed_runtime._codex_discovery_read_status(
                    [completion], event, "discovery-tool", binding
                ),
                "invalid",
            )
        finally:
            entry.write_bytes(original_bytes)

    def test_codex_verified_discovery_read_keeps_the_prompt_context_usable(self) -> None:
        """A native completion makes the one installed routing read non-business."""

        session_id = "verified-discovery-session"
        turn_id = "verified-discovery-turn"
        prompt_text = "@text-report " + json.dumps({"input": str(self.input_path)})
        history = self.root / ".codex" / "sessions" / "2026" / "verified-discovery.jsonl"
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

        entry = self.project / ".agents" / "skills" / "text-report" / "SKILL.md"
        command = shlex.join(("/bin/cat", str(entry)))
        tool_use_id = "verified-discovery-tool"

        def tool_callback(event_name: str) -> dict[str, object]:
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

        self.assertEqual(
            receive(tool_callback("PreToolUse"), tool_use_id)["status"], "discovery_pending"
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
                                    "id": tool_use_id,
                                    "command": ["/bin/zsh", "-c", command],
                                    "cwd": str(self.project.resolve()),
                                    "status": "completed",
                                    "exit_code": 0,
                                    "aggregated_output": entry.read_text(encoding="utf-8"),
                                },
                            },
                        }
                    )
                    + "\n"
                ).encode("utf-8")
            )
        self.assertEqual(
            receive(tool_callback("PostToolUse"), tool_use_id)["status"], "discovery_verified"
        )

        database = open_database(self.project)
        try:
            context = database.execute(
                "SELECT root_valid, unknown_change FROM managed_contexts"
            ).fetchone()
            counts = tuple(
                database.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("managed_receipts", "tasks", "calls")
            )
        finally:
            database.close()
        self.assertEqual(tuple(context), (1, 0))
        self.assertEqual(counts, (0, 0, 0))

        database = open_database(self.project)
        try:
            row = database.execute(
                "SELECT event_id, stable_identity, payload_json FROM managed_native_events "
                "WHERE tool_use_id = ? AND phase = 'PreToolUse'",
                (tool_use_id,),
            ).fetchone()
            assert row is not None
            legacy_payload = json.loads(row["payload_json"])
            command_digest = hashlib.sha256(command.encode("utf-8")).hexdigest()
            legacy_payload.update(
                {
                    "action": "unmanaged",
                    "skill": "",
                    "command_digest": managed_runtime._digest(
                        {"action": "unmanaged", "command_digest": command_digest}
                    ),
                    "operation_digest": "",
                    "discovery": None,
                }
            )
            legacy_digest = managed_runtime._digest(
                {
                    "identity": row["stable_identity"],
                    "command": {"action": "unmanaged", "command_digest": command_digest},
                    "raw_request": "prompt-context",
                    "source": "native-collector",
                }
            )
            database.execute(
                "UPDATE managed_native_events SET payload_digest = ?, payload_json = ? WHERE event_id = ?",
                (legacy_digest, json.dumps(legacy_payload, sort_keys=True), row["event_id"]),
            )
            database.execute("UPDATE managed_contexts SET unknown_change = 1")
            database.commit()
        finally:
            database.close()

        self.assertEqual(receive(tool_callback("PreToolUse"), tool_use_id)["status"], "replayed")
        database = open_database(self.project)
        try:
            replayed = database.execute(
                "SELECT payload_json FROM managed_native_events WHERE tool_use_id = ? "
                "AND phase = 'PreToolUse'",
                (tool_use_id,),
            ).fetchone()
            conflicts = database.execute(
                "SELECT COUNT(*) FROM managed_native_conflicts"
            ).fetchone()[0]
            unknown_change = database.execute(
                "SELECT unknown_change FROM managed_contexts"
            ).fetchone()[0]
        finally:
            database.close()
        assert replayed is not None
        self.assertEqual(json.loads(replayed["payload_json"])["action"], "unmanaged")
        self.assertEqual(conflicts, 0)
        self.assertEqual(unknown_change, 1)

    def test_codex_native_packet_suffix_uses_observed_completion_serializers(self) -> None:
        """Only redacted same-turn completion records may follow the frozen P1."""

        session_id = "suffix-session"
        turn_id = "suffix-turn"
        command_completion = {
            "type": "event_msg",
            "timestamp": "1970-01-01T00:00:00Z",
            "ordinal": 1,
            "payload": {
                "type": "item_completed",
                "thread_id": session_id,
                "turn_id": turn_id,
                "started_at_ms": 0,
                "completed_at_ms": 1,
                "item": {
                    "type": "CommandExecution",
                    "id": "suffix-command",
                    "command": ["/bin/zsh", "-lc", "/bin/true"],
                    "cwd": self.project.resolve().as_uri(),
                    "status": "completed",
                    "exit_code": 0,
                    "aggregated_output": "",
                    "duration": 1,
                    "parsed_cmd": "read",
                    "process_id": 1,
                    "source": "shell",
                },
            },
        }
        reasoning = {
            "type": "event_msg",
            "timestamp": "1970-01-01T00:00:00Z",
            "ordinal": 2,
            "payload": {
                "type": "item_completed",
                "thread_id": session_id,
                "turn_id": turn_id,
                "started_at_ms": 0,
                "completed_at_ms": 1,
                "item": {"type": "Reasoning"},
            },
        }
        custom_tool_output = {
            "type": "response_item",
            "timestamp": "1970-01-01T00:00:00Z",
            "ordinal": 3,
            "metadata": {},
            "payload": {
                "type": "custom_tool_call_output",
                "id": "suffix-output",
                "call_id": "different-tool-call-id",
                "output": [
                    {"type": "input_text", "text": "<redacted>"},
                    {"type": "input_text", "text": "<redacted>"},
                ],
                "internal_chat_message_metadata_passthrough": {
                    "turn_id": turn_id,
                    "create_time": 0.0,
                },
            },
        }
        token_count = {
            "type": "event_msg",
            "timestamp": "1970-01-01T00:00:00Z",
            "ordinal": 4,
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {},
                    "last_token_usage": {},
                    "model_context_window": 0,
                },
                "rate_limits": {},
            },
        }
        verified_suffix = [command_completion, reasoning, custom_tool_output, token_count]
        self.assertTrue(
            managed_runtime._codex_native_packet_suffix_is_verified(
                verified_suffix,
                session_id=session_id,
                cwd=self.project,
                generation_id=turn_id,
            )
        )
        self.assertFalse(
            managed_runtime._codex_native_packet_suffix_is_verified(
                [token_count],
                session_id=session_id,
                cwd=self.project,
                generation_id=turn_id,
            )
        )

        after_reasoning_user = list(verified_suffix)
        after_reasoning_user.insert(
            2,
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "thread_id": session_id,
                    "turn_id": turn_id,
                    "item": {"type": "UserMessage"},
                },
            },
        )
        self.assertFalse(
            managed_runtime._codex_native_packet_suffix_is_verified(
                after_reasoning_user,
                session_id=session_id,
                cwd=self.project,
                generation_id=turn_id,
            )
        )

        wrong_turn = json.loads(json.dumps(command_completion))
        wrong_turn["payload"]["turn_id"] = "another-turn"
        self.assertFalse(
            managed_runtime._codex_native_packet_suffix_is_verified(
                [wrong_turn],
                session_id=session_id,
                cwd=self.project,
                generation_id=turn_id,
            )
        )

        forked_suffix = json.loads(json.dumps(command_completion))
        forked_suffix["history_base"] = "unresolved"
        self.assertFalse(
            managed_runtime._codex_native_packet_suffix_is_verified(
                [forked_suffix],
                session_id=session_id,
                cwd=self.project,
                generation_id=turn_id,
            )
        )

    def test_native_request_packet_carries_one_verified_dto_to_an_installed_action(self) -> None:
        """The trusted Pre callback supplies a short-lived DTO without a host path."""

        manifest_path = self.suite / "suite.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["managed"] = {
            "protocol": 1,
            "native_hosts": {"codex": ["0.163.0-alpha.11"]},
            "actions": {
                "text-report": {
                    "skill": "text-report",
                    "kind": "python-script-v1",
                    "script": "bin/native-packet-report.py",
                    "input": {"fields": {"input": {"type": "path"}}, "required": ["input"]},
                }
            },
        }
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        (self.suite / "bin" / "native-packet-report.py").write_text(
            "import argparse\n"
            "import json\n"
            "from pathlib import Path\n"
            "parser = argparse.ArgumentParser()\n"
            "parser.add_argument('command')\n"
            "parser.add_argument('--request', required=True)\n"
            "parser.add_argument('--output', required=True)\n"
            "arguments = parser.parse_args()\n"
            "request = json.loads(Path(arguments.request).read_text(encoding='utf-8'))\n"
            "items = Path(request['input']).read_text(encoding='utf-8').splitlines()\n"
            "Path(arguments.output).write_text('\\n'.join(items), encoding='utf-8')\n",
            encoding="utf-8",
        )
        seal_suite(self.suite)
        self.installation = install_suite(
            source=self.suite,
            project_root=self.project,
            harness="codex",
            ref=None,
            mode="observed",
        )

        session_id = "packet-session"
        turn_id = "packet-turn"
        prompt_text = "@text-report " + json.dumps({"input": str(self.input_path)})
        native_version = "0.163.0-alpha.11"
        history = self.root / ".codex" / "sessions" / "2026" / "packet.jsonl"
        history.parent.mkdir(parents=True)
        initial_history = [
            {
                "type": "session_meta",
                "payload": {
                    "id": session_id,
                    "session_id": session_id,
                    "cwd": str(self.project.resolve()),
                    "cli_version": native_version,
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
        environment = {
            **os.environ,
            "CODEX_SESSION_ID": session_id,
            "CODEX_THREAD_ID": session_id,
            "CODEX_VERSION": native_version,
        }

        def receive(payload: dict[str, object], tool_use_id: str = "") -> dict[str, object]:
            callback_environment = dict(environment)
            if tool_use_id:
                callback_environment["CODEX_TOOL_CALL_ID"] = tool_use_id
            with (
                mock.patch("selc.managed_runtime.Path.home", return_value=self.root),
                mock.patch("selc.native_adapter.Path.home", return_value=self.root),
            ):
                return receive_native_hook(
                    project_root=self.project,
                    suite_id="managed-reports",
                    host="codex",
                    payload=payload,
                    environment=callback_environment,
                )

        def callback(
            command: str,
            tool_use_id: str,
            event_name: str = "PreToolUse",
            *,
            history_path: Path | None = None,
            generation_id: str | None = None,
            request_id: str | None = None,
        ) -> dict[str, object]:
            callback_history = history if history_path is None else history_path
            callback_generation_id = turn_id if generation_id is None else generation_id
            payload: dict[str, object] = {
                "hook_event_name": event_name,
                "cwd": str(self.project),
                "session_id": session_id,
                "turn_id": callback_generation_id,
                "tool_call_id": tool_use_id,
                "tool_name": "Bash",
                "tool_input": {"command": command},
                "transcript_path": str(callback_history),
                "prefix_length": callback_history.stat().st_size,
            }
            if request_id is not None:
                payload["request_id"] = request_id
            return payload

        def append_command_completion(
            command: str,
            tool_use_id: str,
            output: str = "",
            *,
            history_path: Path | None = None,
            generation_id: str | None = None,
        ) -> None:
            completion_history = history if history_path is None else history_path
            completion_generation_id = turn_id if generation_id is None else generation_id
            completion = {
                "type": "event_msg",
                "timestamp": "1970-01-01T00:00:00Z",
                "ordinal": 1,
                "payload": {
                    "type": "item_completed",
                    "thread_id": session_id,
                    "turn_id": completion_generation_id,
                    "started_at_ms": 0,
                    "completed_at_ms": 1,
                    "item": {
                        "type": "CommandExecution",
                        "id": tool_use_id,
                        "command": ["/bin/zsh", "-c", command],
                        "cwd": self.project.resolve().as_uri(),
                        "status": "completed",
                        "exit_code": 0,
                        "aggregated_output": output,
                        "duration": 1,
                        "parsed_cmd": "read",
                        "process_id": 1,
                        "source": "shell",
                    },
                },
            }
            reasoning = {
                "type": "event_msg",
                "timestamp": "1970-01-01T00:00:00Z",
                "ordinal": 2,
                "payload": {
                    "type": "item_completed",
                    "thread_id": session_id,
                    "turn_id": completion_generation_id,
                    "started_at_ms": 0,
                    "completed_at_ms": 1,
                    "item": {"type": "Reasoning"},
                },
            }
            custom_tool_output = {
                "type": "response_item",
                "timestamp": "1970-01-01T00:00:00Z",
                "ordinal": 3,
                "metadata": {},
                "payload": {
                    "type": "custom_tool_call_output",
                    "id": f"output-{tool_use_id}",
                    "call_id": f"call-{tool_use_id}",
                    "output": [
                        {"type": "input_text", "text": "<redacted>"},
                        {"type": "input_text", "text": "<redacted>"},
                    ],
                    "internal_chat_message_metadata_passthrough": {
                        "turn_id": completion_generation_id,
                        "create_time": 0.0,
                    },
                },
            }
            token_count = {
                "type": "event_msg",
                "timestamp": "1970-01-01T00:00:00Z",
                "ordinal": 4,
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {},
                        "last_token_usage": {},
                        "model_context_window": 0,
                    },
                    "rate_limits": {},
                },
            }
            with completion_history.open("ab") as stream:
                for record in (completion, reasoning, custom_tool_output, token_count):
                    stream.write((json.dumps(record) + "\n").encode("utf-8"))

        def write_initial_history(history_path: Path, generation_id: str) -> None:
            records = json.loads(json.dumps(initial_history))
            task_started = records[1]["payload"]
            task_started["turn_id"] = generation_id
            task_started["root_turn_id"] = generation_id
            task_started["turn_attribution"]["turn_id"] = generation_id
            task_started["turn_attribution"]["root_turn_id"] = generation_id
            records[2]["payload"]["internal_chat_message_metadata_passthrough"]["turn_id"] = (
                generation_id
            )
            records[3]["payload"]["internal_chat_message_metadata_passthrough"]["turn_id"] = (
                generation_id
            )
            turn_context = records[5]["payload"]
            turn_context["turn_id"] = generation_id
            turn_context["root_turn_id"] = generation_id
            history_path.write_bytes(
                b"".join((json.dumps(record) + "\n").encode("utf-8") for record in records)
            )

        def append_prompt_record(
            history_path: Path,
            *,
            generation_id: str,
            text: str,
        ) -> None:
            prompt_record = {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": text}],
                    "internal_chat_message_metadata_passthrough": {
                        "turn_id": generation_id,
                        "content_item_kinds": ["user.text"],
                    },
                },
            }
            with history_path.open("ab") as stream:
                stream.write((json.dumps(prompt_record) + "\n").encode("utf-8"))

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

        discovery_entry = self.project / ".agents" / "skills" / "text-report" / "SKILL.md"
        discovery_command = shlex.join(("/bin/cat", str(discovery_entry)))
        discovery_tool = "packet-discovery"
        self.assertEqual(
            receive(callback(discovery_command, discovery_tool), discovery_tool)["status"],
            "discovery_pending",
        )
        append_command_completion(
            discovery_command,
            discovery_tool,
            discovery_entry.read_text(encoding="utf-8"),
        )
        self.assertEqual(
            receive(
                callback(discovery_command, discovery_tool, "PostToolUse"),
                discovery_tool,
            )["status"],
            "discovery_verified",
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
        invoke = [
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
        invoke_tool = "packet-invoke"
        self.assertEqual(
            receive(callback(shlex.join(invoke), invoke_tool), invoke_tool)["status"], "recorded"
        )
        invoked = subprocess.run(
            invoke,
            env={**environment, "CODEX_TOOL_CALL_ID": invoke_tool},
            capture_output=True,
            check=False,
            text=True,
        )
        self.assertEqual(invoked.returncode, 0, invoked.stderr or invoked.stdout)
        subject = json.loads(invoked.stdout)["managed"]
        invoke_command = shlex.join(invoke)
        append_command_completion(invoke_command, invoke_tool)
        self.assertEqual(
            receive(callback(invoke_command, invoke_tool, "PostToolUse"), invoke_tool)["status"],
            "post_recorded",
        )

        action = [
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
            str(subject["task_id"]),
            "--call-id",
            str(subject["call_id"]),
            "--action",
            "text-report",
            "--request-source",
            "native",
            "--host",
            "codex",
            "--expected-version",
            str(self.installation["version"]),
            "--expected-composite",
            str(self.installation["digest"]),
        ]
        action_tool = "packet-action"
        self.assertEqual(
            receive(callback(shlex.join(action), action_tool), action_tool)["status"], "recorded"
        )

        database = open_database(self.project)
        try:
            event = database.execute(
                "SELECT payload_json FROM managed_native_events WHERE tool_use_id = ?",
                (action_tool,),
            ).fetchone()
            receipt = database.execute(
                "SELECT receipt_digest FROM managed_receipts WHERE tool_use_id = ?",
                (action_tool,),
            ).fetchone()
        finally:
            database.close()
        assert event is not None
        assert receipt is not None
        event_payload = json.loads(event["payload_json"])
        self.assertIn("native_request_packet", event_payload)
        self.assertEqual(set(event_payload["native_request_packet"]), {"version", "digest"})
        self.assertNotIn(str(history), json.dumps(event_payload))
        packet = managed_runtime._native_request_packet_path(
            self.project,
            "managed-reports",
            receipt["receipt_digest"],
        )
        self.assertTrue(packet.is_file())
        packet_bytes = packet.read_bytes()
        interrupted_temporary = packet.parent / f".{packet.name}.{'a' * 24}.tmp"
        os.link(packet, interrupted_temporary)
        with self.assertRaises(SelcError):
            managed_runtime._read_native_request_packet_file(packet)
        database = open_database(self.project)
        try:
            managed_runtime._cleanup_expired_native_request_packets(
                database,
                self.project,
                "managed-reports",
                self.installation,
            )
        finally:
            database.close()
        self.assertFalse(interrupted_temporary.exists())
        self.assertEqual(managed_runtime._read_native_request_packet_file(packet), packet_bytes)

        expired_garbage: list[Path] = []
        for index in range(129):
            digest = hashlib.sha256(f"expired-packet-{index}".encode("utf-8")).hexdigest()
            garbage = packet.parent / f"{digest}.json"
            garbage.write_bytes(b"{}")
            expired_garbage.append(garbage)
        database = open_database(self.project)
        try:
            managed_runtime._cleanup_expired_native_request_packets(
                database,
                self.project,
                "managed-reports",
                self.installation,
            )
        finally:
            database.close()
        self.assertTrue(any(garbage.exists() for garbage in expired_garbage))
        for _ in range(4):
            database = open_database(self.project)
            try:
                managed_runtime._cleanup_expired_native_request_packets(
                    database,
                    self.project,
                    "managed-reports",
                    self.installation,
                )
            finally:
                database.close()
        self.assertTrue(all(not garbage.exists() for garbage in expired_garbage))
        self.assertEqual(
            receive(callback(shlex.join(action), action_tool), action_tool)["status"],
            "replayed",
        )
        self.assertEqual(packet.read_bytes(), packet_bytes)

        wrong_subject = list(action)
        wrong_subject[wrong_subject.index("--task-id") + 1] = "managed-task-wrong-subject"
        wrong_subject_result = subprocess.run(
            wrong_subject,
            env={**environment, "CODEX_TOOL_CALL_ID": action_tool},
            capture_output=True,
            check=False,
            text=True,
        )
        self.assertNotEqual(wrong_subject_result.returncode, 0)

        completed = subprocess.run(
            action,
            env={**environment, "CODEX_TOOL_CALL_ID": action_tool},
            capture_output=True,
            check=False,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        self.assertFalse(packet.exists())

        orphan_tool = "packet-action-orphan"
        self.assertEqual(
            receive(callback(shlex.join(action), orphan_tool), orphan_tool)["status"], "recorded"
        )
        database = open_database(self.project)
        try:
            orphan_receipt = database.execute(
                "SELECT * FROM managed_receipts WHERE tool_use_id = ?",
                (orphan_tool,),
            ).fetchone()
            assert orphan_receipt is not None
            orphan_packet = managed_runtime._native_request_packet_path(
                self.project,
                "managed-reports",
                orphan_receipt["receipt_digest"],
            )
            orphan_event = database.execute(
                "SELECT event_id, payload_json FROM managed_native_events WHERE tool_use_id = ?",
                (orphan_tool,),
            ).fetchone()
            assert orphan_event is not None
            orphan_payload = json.loads(orphan_event["payload_json"])
            orphan_payload.pop("native_request_packet")
            database.execute(
                "UPDATE managed_native_events SET payload_json = ? WHERE event_id = ?",
                (json.dumps(orphan_payload, sort_keys=True), orphan_event["event_id"]),
            )
            database.commit()
            self.assertFalse(
                managed_runtime._native_request_packet_artifact_matches_receipt(
                    database,
                    orphan_packet.read_bytes(),
                    orphan_receipt,
                    self.installation,
                )
            )
            restored_payload = json.loads(
                database.execute(
                    "SELECT payload_json FROM managed_native_events WHERE event_id = ?",
                    (orphan_event["event_id"],),
                ).fetchone()["payload_json"]
            )
        finally:
            database.close()
        self.assertNotIn("native_request_packet", restored_payload)
        orphaned = subprocess.run(
            action,
            env={**environment, "CODEX_TOOL_CALL_ID": orphan_tool},
            capture_output=True,
            check=False,
            text=True,
        )
        self.assertNotEqual(orphaned.returncode, 0)

        non_packet_blockers: list[Path] = []
        for index in range(64):
            blocker = packet.parent / f"unrelated-packet-entry-{index}"
            blocker.mkdir()
            non_packet_blockers.append(blocker)

        recovery_tool = "packet-action-direct-recovery"
        self.assertEqual(
            receive(callback(shlex.join(action), recovery_tool), recovery_tool)["status"],
            "recorded",
        )
        database = open_database(self.project)
        try:
            recovery_receipt = database.execute(
                "SELECT * FROM managed_receipts WHERE tool_use_id = ?",
                (recovery_tool,),
            ).fetchone()
            assert recovery_receipt is not None
            recovery_packet = managed_runtime._native_request_packet_path(
                self.project,
                "managed-reports",
                recovery_receipt["receipt_digest"],
            )
            recovery_temporary = recovery_packet.with_name(f".{recovery_packet.name}.tmp")
            os.link(recovery_packet, recovery_temporary)
            managed_runtime._recover_current_native_request_packet_artifact(
                database,
                project_root=self.project,
                receipt=recovery_receipt,
                installation=self.installation,
            )
        finally:
            database.close()
        self.assertTrue(recovery_packet.is_file())
        self.assertFalse(recovery_temporary.exists())

        ready_stop_tool = "packet-action-ready-stop"
        self.assertEqual(
            receive(callback(shlex.join(action), ready_stop_tool), ready_stop_tool)["status"],
            "recorded",
        )
        database = open_database(self.project)
        try:
            ready_stop_receipt = database.execute(
                "SELECT receipt_digest FROM managed_receipts WHERE tool_use_id = ?",
                (ready_stop_tool,),
            ).fetchone()
        finally:
            database.close()
        assert ready_stop_receipt is not None
        ready_stop_packet = managed_runtime._native_request_packet_path(
            self.project,
            "managed-reports",
            ready_stop_receipt["receipt_digest"],
        )
        self.assertTrue(ready_stop_packet.is_file())
        ready_stop_temporary = ready_stop_packet.with_name(f".{ready_stop_packet.name}.tmp")
        os.link(ready_stop_packet, ready_stop_temporary)

        replay = receive(callback(shlex.join(action), action_tool), action_tool)
        self.assertEqual(replay["status"], "replayed")
        repeated = subprocess.run(
            action,
            env={**environment, "CODEX_TOOL_CALL_ID": action_tool},
            capture_output=True,
            check=False,
            text=True,
        )
        self.assertNotEqual(repeated.returncode, 0)

        tampered_tool = "packet-action-tampered"
        self.assertEqual(
            receive(callback(shlex.join(action), tampered_tool), tampered_tool)["status"],
            "recorded",
        )
        database = open_database(self.project)
        try:
            tampered_receipt = database.execute(
                "SELECT receipt_digest FROM managed_receipts WHERE tool_use_id = ?",
                (tampered_tool,),
            ).fetchone()
        finally:
            database.close()
        assert tampered_receipt is not None
        tampered_packet = managed_runtime._native_request_packet_path(
            self.project,
            "managed-reports",
            tampered_receipt["receipt_digest"],
        )
        tampered_packet.write_bytes(b"{}")
        tampered = subprocess.run(
            action,
            env={**environment, "CODEX_TOOL_CALL_ID": tampered_tool},
            capture_output=True,
            check=False,
            text=True,
        )
        self.assertNotEqual(tampered.returncode, 0)
        self.assertTrue(tampered_packet.exists())
        tampered_packet.unlink()
        missing = subprocess.run(
            action,
            env={**environment, "CODEX_TOOL_CALL_ID": tampered_tool},
            capture_output=True,
            check=False,
            text=True,
        )
        self.assertNotEqual(missing.returncode, 0)

        database = open_database(self.project)
        try:
            existing_live_packets = managed_runtime._live_native_request_packet_receipts(
                database,
                suite_id="managed-reports",
                limit=managed_runtime.MAX_LIVE_NATIVE_REQUEST_PACKETS + 1,
            )
        finally:
            database.close()
        self.assertLess(
            len(existing_live_packets),
            managed_runtime.MAX_LIVE_NATIVE_REQUEST_PACKETS,
        )
        for index in range(
            managed_runtime.MAX_LIVE_NATIVE_REQUEST_PACKETS - len(existing_live_packets)
        ):
            cap_tool = f"packet-action-capacity-{index}"
            with self.subTest(cap_tool=cap_tool):
                self.assertEqual(
                    receive(callback(shlex.join(action), cap_tool), cap_tool)["status"], "recorded"
                )
        database = open_database(self.project)
        try:
            live_packets = managed_runtime._live_native_request_packet_receipts(
                database,
                suite_id="managed-reports",
                limit=65,
            )
        finally:
            database.close()
        self.assertEqual(len(live_packets), 64)
        with self.assertRaises(SelcError) as captured:
            receive(
                callback(shlex.join(action), "packet-action-capacity-overflow"),
                "packet-action-capacity-overflow",
            )
        self.assertEqual(captured.exception.code, "NATIVE_REQUEST_UNAVAILABLE")

        original_history = history.read_bytes()
        appended_user = {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "later business request"}],
                "internal_chat_message_metadata_passthrough": {
                    "turn_id": turn_id,
                    "content_item_kinds": ["user.text"],
                },
            },
        }
        with history.open("ab") as stream:
            stream.write((json.dumps(appended_user) + "\n").encode("utf-8"))
        with self.assertRaises(SelcError) as captured:
            receive(
                callback(shlex.join(action), "packet-action-new-user"), "packet-action-new-user"
            )
        self.assertEqual(captured.exception.code, "ROOT_PROOF_UNAVAILABLE")

        history.write_bytes(original_history)
        replacement_history = history.with_name("packet-replacement.jsonl")
        replacement_history.write_bytes(original_history)
        original_history_link = history.with_name("packet-original-history.jsonl")
        os.link(history, original_history_link)
        os.replace(replacement_history, history)
        with self.assertRaises(SelcError) as captured:
            receive(
                callback(shlex.join(action), "packet-action-replaced-history"),
                "packet-action-replaced-history",
            )
        self.assertEqual(captured.exception.code, "ROOT_PROOF_UNAVAILABLE")
        os.replace(original_history_link, history)

        stop_garbage = packet.parent / f"{hashlib.sha256(b'stop-garbage').hexdigest()}.json"
        stop_garbage.write_bytes(b"{}")
        self.assertEqual(
            receive(
                {
                    "hook_event_name": "Stop",
                    "cwd": str(self.project),
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "transcript_path": str(history),
                    "prefix_length": history.stat().st_size,
                }
            )["status"],
            "stop_recorded",
        )
        self.assertFalse(stop_garbage.exists())
        self.assertFalse(ready_stop_packet.exists())
        self.assertFalse(ready_stop_temporary.exists())
        self.assertFalse(recovery_packet.exists())
        self.assertTrue(all(blocker.is_dir() for blocker in non_packet_blockers))
        database = open_database(self.project)
        try:
            remaining_packets = managed_runtime._live_native_request_packet_receipts(
                database,
                suite_id="managed-reports",
                limit=managed_runtime.MAX_LIVE_NATIVE_REQUEST_PACKETS + 1,
                include_expired=True,
            )
        finally:
            database.close()
        self.assertEqual(remaining_packets, [])

        next_turn_id = "packet-turn-after-stop"
        next_history = history.with_name("packet-after-stop.jsonl")
        write_initial_history(next_history, next_turn_id)
        next_prompt = {
            "hook_event_name": "UserPromptSubmit",
            "cwd": str(self.project),
            "session_id": session_id,
            "turn_id": next_turn_id,
            "prompt": prompt_text,
            "transcript_path": str(next_history),
            "prefix_length": next_history.stat().st_size,
        }
        self.assertEqual(receive(next_prompt)["status"], "prompt_recorded")
        append_prompt_record(next_history, generation_id=next_turn_id, text=prompt_text)

        next_discovery_tool = "packet-after-stop-discovery"
        self.assertEqual(
            receive(
                callback(
                    discovery_command,
                    next_discovery_tool,
                    history_path=next_history,
                    generation_id=next_turn_id,
                ),
                next_discovery_tool,
            )["status"],
            "discovery_pending",
        )
        append_command_completion(
            discovery_command,
            next_discovery_tool,
            discovery_entry.read_text(encoding="utf-8"),
            history_path=next_history,
            generation_id=next_turn_id,
        )
        self.assertEqual(
            receive(
                callback(
                    discovery_command,
                    next_discovery_tool,
                    "PostToolUse",
                    history_path=next_history,
                    generation_id=next_turn_id,
                ),
                next_discovery_tool,
            )["status"],
            "discovery_verified",
        )

        next_invoke_tool = "packet-after-stop-invoke"
        invoke_command = shlex.join(invoke)
        self.assertEqual(
            receive(
                callback(
                    invoke_command,
                    next_invoke_tool,
                    history_path=next_history,
                    generation_id=next_turn_id,
                ),
                next_invoke_tool,
            )["status"],
            "recorded",
        )
        next_invoked = subprocess.run(
            invoke,
            env={**environment, "CODEX_TOOL_CALL_ID": next_invoke_tool},
            capture_output=True,
            check=False,
            text=True,
        )
        self.assertEqual(next_invoked.returncode, 0, next_invoked.stderr or next_invoked.stdout)
        next_subject = json.loads(next_invoked.stdout)["managed"]
        append_command_completion(
            invoke_command,
            next_invoke_tool,
            history_path=next_history,
            generation_id=next_turn_id,
        )
        self.assertEqual(
            receive(
                callback(
                    invoke_command,
                    next_invoke_tool,
                    "PostToolUse",
                    history_path=next_history,
                    generation_id=next_turn_id,
                ),
                next_invoke_tool,
            )["status"],
            "post_recorded",
        )

        next_action = list(action)
        next_action[next_action.index("--task-id") + 1] = str(next_subject["task_id"])
        next_action[next_action.index("--call-id") + 1] = str(next_subject["call_id"])
        next_action_tool = "packet-after-stop-action"
        self.assertEqual(
            receive(
                callback(
                    shlex.join(next_action),
                    next_action_tool,
                    history_path=next_history,
                    generation_id=next_turn_id,
                ),
                next_action_tool,
            )["status"],
            "recorded",
        )

        other_request_tool = "packet-after-stop-other-request"
        self.assertEqual(
            receive(
                callback(
                    shlex.join(next_action),
                    other_request_tool,
                    history_path=next_history,
                    generation_id=next_turn_id,
                ),
                other_request_tool,
            )["status"],
            "recorded",
        )
        database = open_database(self.project)
        try:
            other_receipt = database.execute(
                "SELECT * FROM managed_receipts WHERE tool_use_id = ?",
                (other_request_tool,),
            ).fetchone()
            assert other_receipt is not None
            other_packet = managed_runtime._native_request_packet_path(
                self.project,
                "managed-reports",
                other_receipt["receipt_digest"],
            )
            other_packet_body = json.loads(other_packet.read_bytes())
            other_packet_body["request_id"] = "other-request"
            other_packet_bytes = managed_runtime.canonical_json(other_packet_body).encode("utf-8")
            other_packet.write_bytes(other_packet_bytes)
            other_event = database.execute(
                "SELECT event_id, payload_json FROM managed_native_events WHERE event_id = ?",
                (other_receipt["native_event_id"],),
            ).fetchone()
            assert other_event is not None
            other_event_payload = json.loads(other_event["payload_json"])
            other_event_payload["native_request_packet"]["digest"] = hashlib.sha256(
                other_packet_bytes
            ).hexdigest()
            database.execute(
                "UPDATE managed_receipts SET request_id = ? WHERE receipt_digest = ?",
                ("other-request", other_receipt["receipt_digest"]),
            )
            database.execute(
                "UPDATE managed_native_events SET request_id = ?, payload_json = ? WHERE event_id = ?",
                (
                    "other-request",
                    managed_runtime.canonical_json(other_event_payload),
                    other_event["event_id"],
                ),
            )
            database.commit()
        finally:
            database.close()

        database = open_database(self.project)
        try:
            next_receipt = database.execute(
                "SELECT * FROM managed_receipts WHERE tool_use_id = ?",
                (next_action_tool,),
            ).fetchone()
            assert next_receipt is not None
            database.execute(
                "UPDATE managed_receipts SET expires_at = ? WHERE receipt_digest = ?",
                ("1970-01-01T00:00:00Z", next_receipt["receipt_digest"]),
            )
            database.commit()
        finally:
            database.close()
        next_packet = managed_runtime._native_request_packet_path(
            self.project,
            "managed-reports",
            next_receipt["receipt_digest"],
        )
        next_temporary = managed_runtime._native_request_packet_temporary_path(next_packet)
        os.link(next_packet, next_temporary)

        self.assertEqual(
            receive(
                {
                    "hook_event_name": "Stop",
                    "cwd": str(self.project),
                    "session_id": session_id,
                    "turn_id": next_turn_id,
                    "transcript_path": str(next_history),
                    "prefix_length": next_history.stat().st_size,
                }
            )["status"],
            "stop_recorded",
        )
        self.assertFalse(next_packet.exists())
        self.assertFalse(next_temporary.exists())
        self.assertTrue(other_packet.is_file())
        database = open_database(self.project)
        try:
            next_status = database.execute(
                "SELECT status FROM managed_receipts WHERE receipt_digest = ?",
                (next_receipt["receipt_digest"],),
            ).fetchone()["status"]
        finally:
            database.close()
        self.assertEqual(next_status, "expired")

    def test_codex_selectorless_dto_reuses_a_failed_task_for_a_structured_correction(self) -> None:
        self._assert_codex_dto_correction_chain(
            first_selectorless=True,
            correction_selectorless=True,
        )

    def test_codex_explicit_goal_may_be_corrected_automatically(self) -> None:
        self._assert_codex_dto_correction_chain(
            first_selectorless=False,
            correction_selectorless=True,
        )

    def test_codex_automatic_goal_may_be_corrected_explicitly(self) -> None:
        self._assert_codex_dto_correction_chain(
            first_selectorless=True,
            correction_selectorless=False,
        )

    def test_codex_pathless_automatic_goal_stages_an_instruction_artifact(self) -> None:
        self._assert_codex_dto_correction_chain(
            first_selectorless=True,
            correction_selectorless=True,
            instruction_staging=True,
            pathless_business_dto=True,
        )

    def _assert_codex_dto_correction_chain(
        self,
        *,
        first_selectorless: bool,
        correction_selectorless: bool,
        instruction_staging: bool = False,
        pathless_business_dto: bool = False,
    ) -> None:
        """Exercise selector-independent goal binding through an installed Codex wrapper."""

        manifest_path = self.suite / "suite.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["managed"]["native_hosts"] = {"codex": ["0.163.0-alpha.11"]}
        if instruction_staging:
            manifest["managed"]["actions"]["text-report"] = {
                "skill": "text-report",
                "kind": "instruction-staging-v1",
                "input": {"fields": {}, "required": []},
            }
        else:
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
                "expected_sum = sum(item['value'] for item in items)\n"
                f"marker = Path({str(self.root / 'allow-correction-pass')!r})\n"
                "artifact = {'count': len(items), 'sum': expected_sum if marker.is_file() else expected_sum - 1}\n"
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
        account_home = managed_runtime._trusted_posix_account_home()
        self.assertIsNotNone(account_home)
        old_filesystem = (
            "<filesystem><cwd>project</cwd><path>"
            f"{account_home}/.codex/tmp/arg0-macos/codex-arg0OLD123"
            "</path></entry></filesystem>"
        )
        new_filesystem = old_filesystem.replace("codex-arg0OLD123", "codex-arg0NEW456")
        automatic_input = self.root / "automatic-items.json"
        automatic_input_body = json.dumps(
            {"items": [{"id": "first", "value": 2}, {"id": "second", "value": 3}]}
        )
        automatic_input.write_text(automatic_input_body, encoding="utf-8")
        business_dto: dict[str, object]
        if pathless_business_dto:
            business_dto = {"objective": "summarize the current inventory"}
        else:
            business_dto = {"input": str(automatic_input)}
        first_business_dto = json.dumps(business_dto)
        prompt_text = (
            first_business_dto if first_selectorless else "@text-report " + first_business_dto
        )
        if pathless_business_dto:
            self.assertNotIn(str(automatic_input), prompt_text)
            self.assertNotIn("text-report", prompt_text)
        history = self.root / ".codex" / "sessions" / "2026" / f"{session_id}.jsonl"
        history.parent.mkdir(parents=True)
        initial_history = [
            {
                "type": "session_meta",
                "payload": {
                    "id": session_id,
                    "session_id": session_id,
                    "cwd": str(self.project.resolve()),
                    "cli_version": "0.163.0-alpha.11",
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
            {
                "type": "world_state",
                "payload": {
                    "full": True,
                    "state": {"environments": {"filesystem": old_filesystem}},
                },
            },
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
            "CODEX_VERSION": "0.163.0-alpha.11",
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

        def run_failure(command: list[str], tool_use_id: str) -> subprocess.CompletedProcess[str]:
            completed = subprocess.run(
                command,
                env={**base_environment, "CODEX_TOOL_CALL_ID": tool_use_id},
                capture_output=True,
                check=False,
                text=True,
            )
            self.assertNotEqual(completed.returncode, 0, completed.stderr or completed.stdout)
            return completed

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

        instruction_artifact = self.root / "automatic-instruction-artifact.json"
        if instruction_staging:
            checker_input = automatic_input
            items = json.loads(checker_input.read_text(encoding="utf-8"))["items"]
            expected_sum = sum(item["value"] for item in items)
            instruction_artifact.write_text(
                json.dumps({"count": len(items), "sum": expected_sum - 1}),
                encoding="utf-8",
            )
            action_command = [
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
                "text-report",
                "--artifact-bytes",
                str(instruction_artifact),
                "--checker-input",
                str(checker_input),
                "--host",
                "codex",
                "--expected-version",
                str(self.installation["version"]),
                "--expected-composite",
                str(self.installation["digest"]),
            ]
        else:
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
                                    "command": ["/bin/zsh", "-c", shlex.join(publish_command)],
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
        self.assertEqual((health["first_result"], health["final_result"]), ("fail", "fail"))
        self.assertEqual(health["evidence"]["windows_closed"], 1)
        self.assertEqual(task_status, "closed")
        self.assertEqual(call_status, "closed")

        (self.root / "allow-correction-pass").write_text("pass", encoding="utf-8")
        correction_turn = "turn-automatic-correction"
        correction_business_dto = json.dumps(
            {
                **business_dto,
                "selc": {
                    "intent": "correct_delivery",
                    "target": "reports/automatic.md",
                    "preserve_goal": True,
                },
            }
        )
        correction_prompt = (
            correction_business_dto
            if correction_selectorless
            else "@text-report " + correction_business_dto
        )
        correction_p0 = [
            {
                "type": "event_msg",
                "payload": {
                    "type": "task_started",
                    "turn_id": correction_turn,
                    "root_turn_id": correction_turn,
                    "turn_attribution": {
                        "turn_id": correction_turn,
                        "root_turn_id": correction_turn,
                        "parent_turn_id": None,
                    },
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": new_filesystem}],
                    "internal_chat_message_metadata_passthrough": {
                        "turn_id": correction_turn,
                        "content_item_kinds": ["environments.environment_context"],
                    },
                },
            },
            {
                "type": "world_state",
                "payload": {
                    "full": False,
                    "state": {"environments": {"filesystem": new_filesystem}},
                },
            },
            {
                "type": "turn_context",
                "payload": {
                    "turn_id": correction_turn,
                    "root_turn_id": correction_turn,
                    "cwd": str(self.project.resolve()),
                },
            },
        ]
        with history.open("ab") as stream:
            for record in correction_p0:
                stream.write((json.dumps(record) + "\n").encode("utf-8"))
        self.assertEqual(
            receive(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "cwd": str(self.project),
                    "session_id": session_id,
                    "turn_id": correction_turn,
                    "transcript_path": str(history),
                    "prefix_length": history.stat().st_size,
                    "prompt": correction_prompt,
                }
            )["status"],
            "prompt_recorded",
        )
        with history.open("ab") as stream:
            stream.write(
                (
                    json.dumps(
                        {
                            "type": "response_item",
                            "payload": {
                                "type": "message",
                                "role": "user",
                                "content": [{"type": "input_text", "text": correction_prompt}],
                                "internal_chat_message_metadata_passthrough": {
                                    "turn_id": correction_turn,
                                    "content_item_kinds": ["user.text"],
                                },
                            },
                        }
                    )
                    + "\n"
                ).encode("utf-8")
            )

        correction_invoke_tool = "tool-automatic-correction-invoke"
        receive(
            {
                "hook_event_name": "PreToolUse",
                "cwd": str(self.project),
                "session_id": session_id,
                "turn_id": correction_turn,
                "tool_call_id": correction_invoke_tool,
                "tool_name": "Bash",
                "tool_input": {"command": shlex.join(invoke_command)},
                "transcript_path": str(history),
                "prefix_length": history.stat().st_size,
            }
        )
        correction = run(invoke_command, correction_invoke_tool)["managed"]
        self.assertEqual(correction["task_id"], activated["task_id"])
        self.assertNotEqual(correction["call_id"], activated["call_id"])

        correction_action_command = list(action_command)
        correction_action_command[correction_action_command.index("--call-id") + 1] = str(
            correction["call_id"]
        )
        receive(
            {
                "hook_event_name": "PreToolUse",
                "cwd": str(self.project),
                "session_id": session_id,
                "turn_id": correction_turn,
                "tool_call_id": "tool-automatic-correction-action",
                "tool_name": "Bash",
                "tool_input": {"command": shlex.join(correction_action_command)},
                "transcript_path": str(history),
                "prefix_length": history.stat().st_size,
            }
        )
        if instruction_staging:
            instruction_artifact.write_text(
                json.dumps({"count": 2, "sum": 5}),
                encoding="utf-8",
            )
            correction_action = run(
                correction_action_command,
                "tool-automatic-correction-action",
            )["managed"]
        else:
            automatic_input.write_text(
                json.dumps(
                    {"items": [{"id": "first", "value": 102}, {"id": "second", "value": 3}]}
                ),
                encoding="utf-8",
            )
            rejected_input = run_failure(
                correction_action_command,
                "tool-automatic-correction-action",
            )
            self.assertIn("CORRECTION_BINDING_STALE", rejected_input.stdout + rejected_input.stderr)
            self.assertEqual(
                managed_runtime.health(self.project, "managed-reports", str(activated["task_id"]))[
                    "final_result"
                ],
                "fail",
            )
            automatic_input.write_text(automatic_input_body, encoding="utf-8")
            correction_action = run(
                correction_action_command,
                "tool-automatic-correction-action",
            )["managed"]

        correction_publish_command = list(publish_command)
        correction_publish_command[correction_publish_command.index("--call-id") + 1] = str(
            correction["call_id"]
        )
        correction_publish_command[correction_publish_command.index("--artifact") + 1] = str(
            correction_action["artifact_path"]
        )
        wrong_target_command = list(correction_publish_command)
        wrong_target_command[wrong_target_command.index("--target") + 1] = (
            "reports/different-goal.json"
        )
        wrong_target_tool = "tool-automatic-correction-wrong-publish"
        receive(
            {
                "hook_event_name": "PreToolUse",
                "cwd": str(self.project),
                "session_id": session_id,
                "turn_id": correction_turn,
                "tool_call_id": wrong_target_tool,
                "tool_name": "Bash",
                "tool_input": {"command": shlex.join(wrong_target_command)},
                "transcript_path": str(history),
                "prefix_length": history.stat().st_size,
            }
        )
        rejected_target = run_failure(wrong_target_command, wrong_target_tool)
        self.assertIn("CORRECTION_TARGET_MISMATCH", rejected_target.stdout + rejected_target.stderr)
        self.assertFalse((self.project / "reports" / "different-goal.json").exists())
        self.assertEqual(
            managed_runtime.health(self.project, "managed-reports", str(activated["task_id"]))[
                "final_result"
            ],
            "fail",
        )
        correction_publish_tool = "tool-automatic-correction-publish"
        receive(
            {
                "hook_event_name": "PreToolUse",
                "cwd": str(self.project),
                "session_id": session_id,
                "turn_id": correction_turn,
                "tool_call_id": correction_publish_tool,
                "tool_name": "Bash",
                "tool_input": {"command": shlex.join(correction_publish_command)},
                "transcript_path": str(history),
                "prefix_length": history.stat().st_size,
            }
        )
        correction_published = run(correction_publish_command, correction_publish_tool)["managed"]
        self.assertEqual(correction_published["status"], "published")
        receive(
            {
                "hook_event_name": "PostToolUse",
                "cwd": str(self.project),
                "session_id": session_id,
                "turn_id": correction_turn,
                "tool_call_id": correction_publish_tool,
                "tool_name": "Bash",
                "tool_input": {"command": shlex.join(correction_publish_command)},
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
                                "turn_id": correction_turn,
                                "item": {
                                    "type": "CommandExecution",
                                    "id": correction_publish_tool,
                                    "command": [
                                        "/bin/zsh",
                                        "-lc",
                                        shlex.join(correction_publish_command),
                                    ],
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
        self.assertEqual(
            receive(
                {
                    "hook_event_name": "Stop",
                    "cwd": str(self.project),
                    "session_id": session_id,
                    "turn_id": correction_turn,
                    "transcript_path": str(history),
                    "prefix_length": history.stat().st_size,
                }
            )["status"],
            "stop_recorded",
        )

        correction_health = managed_runtime.health(
            self.project,
            "managed-reports",
            str(activated["task_id"]),
        )
        database = open_database(self.project)
        try:
            context = database.execute(
                "SELECT root_valid FROM managed_contexts WHERE generation_id = ?",
                (correction_turn,),
            ).fetchone()
            anchor = database.execute("SELECT rule_version FROM managed_request_anchors").fetchone()
            binding = database.execute(
                "SELECT status, anchor_rule_version FROM managed_correction_bindings"
            ).fetchone()
            task_count = database.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            call_count = database.execute(
                "SELECT COUNT(*) FROM calls WHERE task_id = ?",
                (activated["task_id"],),
            ).fetchone()[0]
            trigger_kinds = [
                row["trigger_kind"]
                for row in database.execute(
                    "SELECT trigger_kind FROM calls WHERE task_id = ? ORDER BY rowid",
                    (activated["task_id"],),
                )
            ]
            checker_results = [
                row["result"]
                for row in database.execute("SELECT result FROM checker_runs ORDER BY created_at")
            ]
        finally:
            database.close()
        self.assertEqual(
            (correction_health["first_result"], correction_health["final_result"]),
            ("fail", "pass"),
            (correction_published, checker_results),
        )
        self.assertEqual(context["root_valid"], 0)
        self.assertEqual(binding["status"], "closed")
        self.assertEqual(anchor["rule_version"], "current-request-v3-codex-alpha11-arg0")
        self.assertEqual(
            binding["anchor_rule_version"],
            "current-request-v3-codex-alpha11-arg0",
        )
        self.assertEqual((task_count, call_count), (1, 2))
        self.assertEqual(
            trigger_kinds,
            [
                "automatic" if first_selectorless else "explicit",
                "automatic" if correction_selectorless else "explicit",
            ],
        )

    def test_codex_alpha11_current_turn_proves_only_a_strict_arg0_delta(self) -> None:
        """An alpha.11 resumed turn may change only its native arg0 directory."""

        session_id = "alpha11-session"
        turn_id = "alpha11-turn"
        current_home = Path("/Users/current-account")
        home_patch = mock.patch(
            "selc.managed_runtime._trusted_posix_account_home",
            return_value=current_home,
        )
        home_patch.start()
        self.addCleanup(home_patch.stop)
        old_filesystem = (
            "<filesystem><cwd>/project</cwd><path>"
            f"{current_home}/.codex/tmp/arg0-macos/codex-arg0OLD123"
            "</path></entry></filesystem>"
        )
        earlier_filesystem = old_filesystem.replace("<cwd>/project", "<cwd>/previous-project")
        middle_filesystem = old_filesystem.replace("codex-arg0OLD123", "codex-arg0MID456")
        new_filesystem = old_filesystem.replace("codex-arg0OLD123", "codex-arg0NEW456")

        def delta(filesystem: str) -> dict[str, object]:
            return {
                "type": "world_state",
                "payload": {
                    "full": False,
                    "state": {"environments": {"filesystem": filesystem}},
                },
            }

        history = [
            {
                "type": "session_meta",
                "payload": {
                    "id": session_id,
                    "session_id": session_id,
                    "cwd": str(self.project.resolve()),
                    "cli_version": "0.163.0-alpha.11",
                },
            },
            {
                "type": "world_state",
                "payload": {
                    "full": True,
                    "state": {"environments": {"filesystem": earlier_filesystem}},
                },
            },
            {
                "type": "world_state",
                "payload": {
                    "full": True,
                    "state": {"environments": {"filesystem": old_filesystem}},
                },
            },
            delta(middle_filesystem),
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
                    "role": "user",
                    "content": [{"type": "input_text", "text": new_filesystem}],
                    "internal_chat_message_metadata_passthrough": {
                        "turn_id": turn_id,
                        "content_item_kinds": ["environments.environment_context"],
                    },
                },
            },
            delta(new_filesystem),
            {
                "type": "turn_context",
                "payload": {
                    "turn_id": turn_id,
                    "root_turn_id": turn_id,
                    "cwd": str(self.project.resolve()),
                },
            },
        ]

        def rule_version(candidate: list[dict[str, object]]) -> str | None:
            return managed_runtime._codex_current_turn_anchor_rule_version(
                candidate,
                session_id=session_id,
                cwd=self.project.resolve(),
                generation_id=turn_id,
                prompt_version="0.163.0-alpha.11",
                pre_version="0.163.0-alpha.11",
            )

        self.assertEqual(
            rule_version(history),
            "current-request-v3-codex-alpha11-arg0",
        )

        no_full_baseline = json.loads(json.dumps(history))
        no_full_baseline[1] = delta(earlier_filesystem)
        no_full_baseline[2] = delta(old_filesystem)
        self.assertIsNone(rule_version(no_full_baseline))

        extra_delta_field = json.loads(json.dumps(history))
        extra_delta_field[3]["payload"]["unexpected"] = True
        self.assertIsNone(rule_version(extra_delta_field))

        second_path = json.loads(json.dumps(history))
        second_filesystem = new_filesystem.replace(
            "</path>",
            f"</path><path>{current_home}/.codex/tmp/arg0-macos/codex-arg0SECOND789</path>",
        )
        second_path[5]["payload"]["content"][0]["text"] = second_filesystem
        second_path[6]["payload"]["state"]["environments"]["filesystem"] = second_filesystem
        self.assertIsNone(rule_version(second_path))

        changed_non_path_bytes = json.loads(json.dumps(history))
        changed_non_path_bytes[3]["payload"]["state"]["environments"]["filesystem"] = (
            middle_filesystem.replace("<cwd>/project", "<cwd>/different-project")
        )
        self.assertIsNone(rule_version(changed_non_path_bytes))

        wrong_environment = json.loads(json.dumps(history))
        wrong_environment[5]["payload"]["content"][0]["text"] = "not the filesystem"
        self.assertIsNone(rule_version(wrong_environment))

        wrong_user = json.loads(json.dumps(history))
        wrong_user[2]["payload"]["state"]["environments"]["filesystem"] = old_filesystem.replace(
            str(current_home), "/Users/another-user"
        )
        self.assertIsNone(rule_version(wrong_user))

        other_home = Path("/Users/other-current-account")
        other_history = json.loads(json.dumps(history).replace(str(current_home), str(other_home)))
        with mock.patch(
            "selc.managed_runtime._trusted_posix_account_home",
            return_value=other_home,
        ):
            self.assertEqual(
                rule_version(other_history),
                "current-request-v3-codex-alpha11-arg0",
            )
            self.assertIsNone(rule_version(history))

        untrusted_old = old_filesystem.replace(
            f"<path>{current_home}",
            f"<path>/untrusted{current_home}",
        )
        untrusted_new = new_filesystem.replace(
            f"<path>{current_home}",
            f"<path>/untrusted{current_home}",
        )
        self.assertFalse(
            managed_runtime._codex_alpha11_arg0_transition_is_verified(
                untrusted_old,
                untrusted_new,
            )
        )

        nested_old = old_filesystem.replace("</path>", "/nested</path>")
        nested_new = new_filesystem.replace("</path>", "/nested</path>")
        self.assertFalse(
            managed_runtime._codex_alpha11_arg0_transition_is_verified(
                nested_old,
                nested_new,
            )
        )
        with mock.patch(
            "selc.managed_runtime._trusted_posix_account_home",
            return_value=None,
        ):
            self.assertFalse(
                managed_runtime._codex_alpha11_arg0_transition_is_verified(
                    old_filesystem,
                    new_filesystem,
                )
            )

        conflicting_version = json.loads(json.dumps(history))
        conflicting_version[0]["payload"]["cli_version"] = "0.163.0-alpha.10.2"
        self.assertIsNone(rule_version(conflicting_version))
        self.assertIsNone(
            managed_runtime._codex_current_turn_anchor_rule_version(
                history,
                session_id=session_id,
                cwd=self.project.resolve(),
                generation_id=turn_id,
                prompt_version="0.163.0-alpha.10.2",
                pre_version="0.163.0-alpha.11",
            )
        )
        self.assertIsNone(
            managed_runtime._codex_current_turn_anchor_rule_version(
                history,
                session_id=session_id,
                cwd=self.project.resolve(),
                generation_id=turn_id,
                prompt_version="0.163.0-alpha.11",
                pre_version="0.163.0-alpha.10.2",
            )
        )
        self.assertFalse(
            managed_runtime._current_request_anchor_rule_is_supported(
                "codex",
                "current-request-v999-unknown",
            )
        )

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

    def test_workbuddy_wrapper_reuses_failed_delivery_for_current_json_correction(self) -> None:
        """Allow a new correction request to use the other supported release."""

        self._assert_workbuddy_wrapper_correction_chain(
            first_client_version="5.9.2",
            correction_client_version="5.8.3",
            first_rejected_bash_version="5.8.3",
            correction_rejected_bash_version="5.9.2",
        )

    def test_workbuddy_592_wrapper_reuses_failed_delivery_for_current_json_correction(
        self,
    ) -> None:
        """Exercise the installed WorkBuddy failure and correction path at 5.9.2."""

        self._assert_workbuddy_wrapper_correction_chain(
            first_client_version="5.9.2",
            correction_client_version="5.9.2",
            first_rejected_bash_version=None,
            correction_rejected_bash_version=None,
        )

    def test_workbuddy_592_same_project_cd_prefixed_action_completes_the_wrapper_chain(
        self,
    ) -> None:
        """The finite shell prefix still binds a full native command digest."""

        self._assert_workbuddy_wrapper_correction_chain(
            first_client_version="5.9.2",
            correction_client_version="5.9.2",
            first_rejected_bash_version=None,
            correction_rejected_bash_version=None,
            same_project_cd_action_prefix=True,
        )

    def test_workbuddy_592_selectorless_dto_reuses_failed_delivery_automatically(self) -> None:
        """A verified native Skill choice may activate a selectorless JSON DTO."""

        self._assert_workbuddy_wrapper_correction_chain(
            first_client_version="5.9.2",
            correction_client_version="5.9.2",
            first_rejected_bash_version=None,
            correction_rejected_bash_version=None,
            selectorless=True,
        )

    def test_workbuddy_583_selectorless_dto_reuses_failed_delivery_automatically(self) -> None:
        """The original supported WorkBuddy release keeps the automatic chain."""

        self._assert_workbuddy_wrapper_correction_chain(
            first_client_version="5.8.3",
            correction_client_version="5.8.3",
            first_rejected_bash_version=None,
            correction_rejected_bash_version=None,
            selectorless=True,
        )

    def test_workbuddy_automatic_version_and_actual_skill_drifts_issue_no_business_receipt(
        self,
    ) -> None:
        self._assert_workbuddy_wrapper_correction_chain(
            first_client_version="5.9.2",
            correction_client_version="5.9.2",
            first_rejected_bash_version="5.8.3",
            correction_rejected_bash_version=None,
            selectorless=True,
        )

    def test_workbuddy_automatic_scope_drift_issues_no_business_receipt(self) -> None:
        self._assert_workbuddy_wrapper_correction_chain(
            first_client_version="5.9.2",
            correction_client_version="5.9.2",
            first_rejected_bash_version=None,
            correction_rejected_bash_version=None,
            selectorless=True,
            first_scope_drift=True,
        )

    def test_workbuddy_automatic_dto_drift_stops_before_business_receipt(self) -> None:
        self._assert_workbuddy_wrapper_correction_chain(
            first_client_version="5.9.2",
            correction_client_version="5.9.2",
            first_rejected_bash_version=None,
            correction_rejected_bash_version=None,
            selectorless=True,
            first_skill_args_drift=True,
        )

    def test_workbuddy_automatic_goal_may_be_corrected_with_an_explicit_selector(self) -> None:
        self._assert_workbuddy_wrapper_correction_chain(
            first_client_version="5.9.2",
            correction_client_version="5.9.2",
            first_rejected_bash_version=None,
            correction_rejected_bash_version=None,
            selectorless=True,
            correction_selectorless=False,
        )

    def test_workbuddy_explicit_goal_may_be_corrected_with_automatic_selection(self) -> None:
        self._assert_workbuddy_wrapper_correction_chain(
            first_client_version="5.9.2",
            correction_client_version="5.9.2",
            first_rejected_bash_version=None,
            correction_rejected_bash_version=None,
            selectorless=False,
            correction_selectorless=True,
        )

    def test_workbuddy_automatic_source_without_goal_cannot_use_explicit_fallback(self) -> None:
        self._assert_workbuddy_wrapper_correction_chain(
            first_client_version="5.9.2",
            correction_client_version="5.9.2",
            first_rejected_bash_version=None,
            correction_rejected_bash_version=None,
            selectorless=True,
            correction_selectorless=False,
            source_goal_missing_before_correction=True,
        )

    def test_workbuddy_explicit_source_without_goal_cannot_use_automatic_fallback(self) -> None:
        self._assert_workbuddy_wrapper_correction_chain(
            first_client_version="5.9.2",
            correction_client_version="5.9.2",
            first_rejected_bash_version=None,
            correction_rejected_bash_version=None,
            selectorless=False,
            correction_selectorless=True,
            source_goal_missing_before_correction=True,
        )

    def test_workbuddy_legacy_explicit_source_without_goal_keeps_its_safe_fallback(self) -> None:
        self._assert_workbuddy_wrapper_correction_chain(
            first_client_version="5.9.2",
            correction_client_version="5.9.2",
            first_rejected_bash_version=None,
            correction_rejected_bash_version=None,
            selectorless=False,
            correction_selectorless=False,
            source_goal_missing_before_correction=True,
            allow_legacy_explicit_fallback=True,
        )

    def test_automatic_first_activation_cannot_change_the_selected_skill(self) -> None:
        """A stale first-selection snapshot cannot replace the committed skill."""

        database = open_database(self.project)
        business_digest = hashlib.sha256(b'{"objective":"summarize"}').hexdigest()
        try:
            database.execute(
                """INSERT INTO managed_contexts(
                context_key, suite_id, host, session_id, generation_id, request_id,
                request_digest, input_skill, business_dto_digest, business_dto_rule_version,
                root_event_id, root_valid, status, created_at)
                VALUES ('automatic-first-selection', 'managed-reports', 'workbuddy',
                'selection-session', 'selection-generation', 'selection-request',
                'selection-request-digest', '', ?, ?, 'selection-root-event', 1,
                'active', ?)""",
                (
                    business_digest,
                    managed_runtime.BUSINESS_DTO_RULE_VERSION,
                    records.utc_now(),
                ),
            )
            database.commit()
        finally:
            database.close()

        first_connection = open_database(self.project)
        rival_connection = open_database(self.project)
        try:
            first_snapshot = first_connection.execute(
                "SELECT * FROM managed_contexts WHERE context_key = 'automatic-first-selection'"
            ).fetchone()
            rival_snapshot = rival_connection.execute(
                "SELECT * FROM managed_contexts WHERE context_key = 'automatic-first-selection'"
            ).fetchone()
            self.assertIsNone(first_snapshot["selected_skill"])
            self.assertIsNone(rival_snapshot["selected_skill"])

            self.assertEqual(
                managed_runtime._confirm_context_selection(
                    first_connection,
                    first_snapshot,
                    "text-report",
                ),
                "automatic",
            )
            first_connection.commit()

            with self.assertRaisesRegex(SelcError, "another activation reserved"):
                managed_runtime._confirm_context_selection(
                    rival_connection,
                    rival_snapshot,
                    "other-report",
                )
            rival_connection.rollback()
            selected_skill = first_connection.execute(
                """SELECT selected_skill FROM managed_contexts
                WHERE context_key = 'automatic-first-selection'"""
            ).fetchone()[0]
            business_counts = tuple(
                first_connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("tasks", "calls", "managed_receipts")
            )
        finally:
            rival_connection.close()
            first_connection.close()

        self.assertEqual(selected_skill, "text-report")
        self.assertEqual(business_counts, (0, 0, 0))

    def test_workbuddy_pathless_automatic_goal_stages_an_instruction_artifact(self) -> None:
        """A pathless business goal may select, stage, and repair one delivery."""

        self._assert_workbuddy_wrapper_correction_chain(
            first_client_version="5.9.2",
            correction_client_version="5.9.2",
            first_rejected_bash_version=None,
            correction_rejected_bash_version=None,
            selectorless=True,
            instruction_staging=True,
            pathless_business_dto=True,
        )

    def _assert_workbuddy_wrapper_correction_chain(
        self,
        *,
        first_client_version: str,
        correction_client_version: str,
        first_rejected_bash_version: str | None,
        correction_rejected_bash_version: str | None,
        selectorless: bool = False,
        correction_selectorless: bool | None = None,
        instruction_staging: bool = False,
        pathless_business_dto: bool = False,
        source_goal_missing_before_correction: bool = False,
        allow_legacy_explicit_fallback: bool = False,
        first_scope_drift: bool = False,
        first_skill_args_drift: bool = False,
        same_project_cd_action_prefix: bool = False,
    ) -> None:
        """Exercise the installed WorkBuddy failure and correction path end to end."""

        if correction_selectorless is None:
            correction_selectorless = selectorless
        if pathless_business_dto and not selectorless:
            raise AssertionError("a pathless business DTO requires automatic selection")

        manifest_path = self.suite / "suite.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["managed"]["native_hosts"] = {"workbuddy": ["5.8.3", "5.9.2"]}
        if instruction_staging:
            manifest["managed"]["actions"]["text-report"] = {
                "skill": "text-report",
                "kind": "instruction-staging-v1",
                "input": {"fields": {}, "required": []},
            }
        else:
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
                "expected_sum = sum(item['value'] for item in items)\n"
                f"marker = Path({str(self.root / 'workbuddy-correction-pass')!r})\n"
                "artifact = {'count': len(items), 'sum': expected_sum if marker.is_file() else expected_sum - 1}\n"
                "Path(arguments.output).write_text(json.dumps(artifact), encoding='utf-8')\n",
                encoding="utf-8",
            )
        if selectorless:
            other_skill = self.suite / "skills" / "other-report"
            other_skill.mkdir()
            (other_skill / "SKILL.md").write_text(
                "---\n"
                "name: other-report\n"
                "description: Synthetic alternative for selection rejection coverage.\n"
                "---\n"
                "# Other report\n"
                "\n"
                "This fixture exists only to prove the selected skill cannot change.\n",
                encoding="utf-8",
            )
            manifest["skills"].append({"name": "other-report"})
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
            {
                "id": "workbuddy-initial-session-meta",
                "type": "session-meta",
                "sessionId": session_id,
                "cwd": project_cwd,
            },
        ]
        history.write_bytes(
            b"".join((json.dumps(record) + "\n").encode("utf-8") for record in metadata)
        )
        automatic_input = self.root / "workbuddy-items.json"
        automatic_input.write_text(
            json.dumps({"items": [{"id": "first", "value": 2}, {"id": "second", "value": 3}]}),
            encoding="utf-8",
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
            command: list[str] | str | None = None,
            skill_input: dict[str, object] | None = None,
            prompt: str | None = None,
            client_version: str = "5.9.2",
        ) -> dict[str, object]:
            payload: dict[str, object] = {
                "hook_event_name": event_name,
                "client": "WorkBuddy",
                "version": client_version,
                "cwd": project_cwd,
                "session_id": session_id,
                "generation_id": generation_id,
                "conversation_request_id": request_id,
                "transcript_path": str(history),
                "prefix_length": history.stat().st_size,
            }
            if prompt is not None:
                payload["prompt"] = prompt
            if tool_id is not None:
                payload["tool_call_id"] = tool_id
            if skill_input is not None:
                payload["tool_name"] = "Skill"
                payload["tool_input"] = skill_input
            elif command is not None:
                payload["tool_name"] = "Bash"
                payload["tool_input"] = {
                    "command": command if isinstance(command, str) else shlex.join(command)
                }
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
                    "CODEBUDDY_CONVERSATION_MESSAGE_ID": "workbuddy-wrapper-message",
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

        def native_user_text(raw_prompt: str) -> str:
            return (
                '<system-reminder data-role="user-context">\n'
                "synthetic host context\n"
                "</system-reminder>\n"
                '<system-reminder data-role="additional-data">\n'
                "synthetic host metadata\n"
                "</system-reminder>\n"
                f"<user_query>{raw_prompt}</user_query>"
            )

        def append_result(
            *,
            request_id: str,
            tool_id: str,
            command: list[str] | str,
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
                        {
                            "command": command if isinstance(command, str) else shlex.join(command),
                            "description": "managed publish",
                        }
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

        def business_fact_counts() -> tuple[int, int, int]:
            database = open_database(self.project)
            try:
                return tuple(
                    database.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in ("tasks", "calls", "managed_receipts")
                )
            finally:
                database.close()

        prefixed_action_commands: dict[str, str] = {}

        def run_turn(
            *,
            request_id: str,
            generation_id: str,
            user_id: str,
            parent_id: str | None,
            prompt: str,
            input_path: Path,
            target: str,
            use_skill_pre: bool,
            include_title: bool,
            include_assistant_text: bool,
            client_version: str,
            rejected_bash_version: str | None,
            expected_correction_rejection: bool = False,
            scope_drift: bool = False,
            skill_args_drift: bool = False,
        ) -> tuple[dict[str, object], str] | None:
            self.assertEqual(
                receive(
                    callback(
                        "UserPromptSubmit",
                        request_id=request_id,
                        generation_id=generation_id,
                        message_id=user_id,
                        prompt=prompt,
                        client_version=client_version,
                    )
                )["status"],
                "prompt_recorded",
            )
            user_record = {
                "id": user_id,
                "type": "message",
                "role": "user",
                "sessionId": session_id,
                "cwd": project_cwd,
                "providerData": {
                    "startsNewUserRequest": True,
                    "conversationRequestId": request_id,
                },
                "content": [{"type": "input_text", "text": native_user_text(prompt)}],
            }
            if parent_id is not None:
                user_record["parentId"] = parent_id
            append(user_record)

            if use_skill_pre:
                skill_tool = f"{request_id}-skill"
                skill_args = prompt.removeprefix("@//text-report/")
                model_message_id = f"native-{skill_tool}-model"
                skill_snapshot = {
                    "messageId": user_id,
                    "trackedFileBackups": {},
                }
                append(
                    {
                        "type": "file-history-snapshot",
                        "messageId": user_id,
                        "snapshot": skill_snapshot,
                        "cwd": project_cwd,
                        "timestamp": 1_769_000_000,
                        "isSnapshotUpdate": True,
                        "_meta": {
                            "baggage": "synthetic-baggage",
                            "traceparent": "synthetic-traceparent",
                        },
                    }
                )
                if include_title:
                    append(
                        {
                            "id": f"native-{skill_tool}-title",
                            "type": "ai-title",
                            "aiTitle": "Synthetic managed summary",
                            "timestamp": 1_769_000_000,
                            "sessionId": session_id,
                            "cwd": project_cwd,
                            "_meta": {
                                "baggage": "synthetic-baggage",
                                "traceparent": "synthetic-traceparent",
                            },
                        }
                    )
                reasoning_id = f"native-{skill_tool}-reasoning"
                append(
                    {
                        "id": reasoning_id,
                        "type": "reasoning",
                        "parentId": user_id,
                        "sessionId": session_id,
                        "cwd": project_cwd,
                        "providerData": {
                            "conversationRequestId": request_id,
                            "messageId": model_message_id,
                            "model": "workbuddy-model",
                            "stepSeq": 1,
                        },
                    }
                )
                skill_parent_id = reasoning_id
                if include_assistant_text:
                    skill_parent_id = f"native-{skill_tool}-assistant"
                    append(
                        {
                            "id": skill_parent_id,
                            "type": "message",
                            "role": "assistant",
                            "status": "completed",
                            "parentId": reasoning_id,
                            "sessionId": session_id,
                            "cwd": project_cwd,
                            "timestamp": 1_769_000_000,
                            "_meta": {
                                "baggage": "synthetic-baggage",
                                "traceparent": "synthetic-traceparent",
                            },
                            "providerData": {
                                "agent": "synthetic-agent",
                                "conversationRequestId": request_id,
                                "messageId": model_message_id,
                                "model": "workbuddy-model",
                                "requestModelId": "synthetic-request-model",
                                "requestModelName": "Synthetic WorkBuddy model",
                                "stepSeq": 1,
                                "traceId": "synthetic-trace",
                            },
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "Synthetic completed assistant output.",
                                    "providerData": {"annotations": []},
                                }
                            ],
                        }
                    )
                append(
                    {
                        "id": f"native-{skill_tool}",
                        "type": "function_call",
                        "name": "Skill",
                        "callId": skill_tool,
                        "parentId": skill_parent_id,
                        "sessionId": session_id,
                        "cwd": project_cwd,
                        "providerData": {
                            "conversationRequestId": request_id,
                            "messageId": model_message_id,
                            "model": "workbuddy-model",
                            "stepSeq": 1,
                        },
                        "arguments": json.dumps(
                            {"skill": "text-report", "args": skill_args},
                            separators=(",", ":"),
                        ),
                    }
                )
                skill_pre = callback(
                    "PreToolUse",
                    request_id=request_id,
                    generation_id=generation_id,
                    message_id=user_id,
                    tool_id=skill_tool,
                    skill_input={"skill": "text-report", "args": skill_args},
                    client_version=client_version,
                )
                if scope_drift:
                    before_scope_drift = business_fact_counts()
                    scope_drift_payload = dict(skill_pre)
                    scope_drift_payload["session_id"] = "foreign-workbuddy-session"
                    scope_drift_result = receive(scope_drift_payload)
                    self.assertIn(scope_drift_result["status"], {"ignored", "technical_unusable"})
                    self.assertEqual(business_fact_counts(), before_scope_drift)
                if skill_args_drift:
                    before_args_drift = business_fact_counts()
                    args_drift_payload = callback(
                        "PreToolUse",
                        request_id=request_id,
                        generation_id=generation_id,
                        message_id=user_id,
                        tool_id=skill_tool,
                        skill_input={
                            "skill": "text-report",
                            "args": json.dumps({"objective": "different"}),
                        },
                        client_version=client_version,
                    )
                    self.assertEqual(receive(args_drift_payload)["status"], "technical_unusable")
                    self.assertEqual(business_fact_counts(), before_args_drift)
                    return None
                self.assertEqual(receive(skill_pre)["status"], "technical_recorded")
                self.assertEqual(receive(skill_pre)["status"], "replayed")

            if selectorless:
                wrong_invoke = wrapper_command("invoke", ["--skill", "other-report"])
                before_wrong_skill = business_fact_counts()
                with self.assertRaises(SelcError) as captured:
                    receive(
                        callback(
                            "PreToolUse",
                            request_id=request_id,
                            generation_id=generation_id,
                            message_id=user_id,
                            tool_id=f"{request_id}-wrong-invoke",
                            command=wrong_invoke,
                            client_version=client_version,
                        )
                    )
                self.assertEqual(captured.exception.code, "NATIVE_REQUEST_UNAVAILABLE")
                self.assertEqual(business_fact_counts(), before_wrong_skill)

            invoke_tool = f"{request_id}-invoke"
            invoke = wrapper_command("invoke", ["--skill", "text-report"])
            if rejected_bash_version is not None:
                before_wrong_version = business_fact_counts()
                with self.assertRaises(SelcError) as captured:
                    receive(
                        callback(
                            "PreToolUse",
                            request_id=request_id,
                            generation_id=generation_id,
                            message_id=user_id,
                            tool_id=f"{request_id}-mixed-version-invoke",
                            command=invoke,
                            client_version=rejected_bash_version,
                        )
                    )
                self.assertEqual(captured.exception.code, "NATIVE_REQUEST_UNAVAILABLE")
                self.assertEqual(business_fact_counts(), before_wrong_version)
            if expected_correction_rejection:
                with self.assertRaises(SelcError) as captured:
                    receive(
                        callback(
                            "PreToolUse",
                            request_id=request_id,
                            generation_id=generation_id,
                            message_id=user_id,
                            tool_id=invoke_tool,
                            command=invoke,
                            client_version=client_version,
                        )
                    )
                self.assertEqual(captured.exception.code, "NATIVE_REQUEST_UNAVAILABLE")
                return None
            self.assertEqual(
                receive(
                    callback(
                        "PreToolUse",
                        request_id=request_id,
                        generation_id=generation_id,
                        message_id=user_id,
                        tool_id=invoke_tool,
                        command=invoke,
                        client_version=client_version,
                    )
                )["status"],
                "recorded",
            )
            activated = run(invoke, request_id=request_id, tool_id=invoke_tool)

            action_tool = f"{request_id}-action"
            if instruction_staging:
                checker_input_path = self.root / f"{request_id}-checker-input.json"
                checker_input_path.write_bytes(input_path.read_bytes())
                artifact_source_path = self.root / f"{request_id}-instruction-artifact.json"
                items = json.loads(checker_input_path.read_text(encoding="utf-8"))["items"]
                expected_sum = sum(item["value"] for item in items)
                artifact_sum = (
                    expected_sum
                    if (self.root / "workbuddy-correction-pass").is_file()
                    else expected_sum - 1
                )
                artifact_source_path.write_text(
                    json.dumps({"count": len(items), "sum": artifact_sum}),
                    encoding="utf-8",
                )
                action_command = wrapper_command(
                    "stage",
                    [
                        "--skill",
                        "text-report",
                        "--task-id",
                        str(activated["task_id"]),
                        "--call-id",
                        str(activated["call_id"]),
                        "--action",
                        "text-report",
                        "--artifact-bytes",
                        str(artifact_source_path),
                        "--checker-input",
                        str(checker_input_path),
                    ],
                )
            else:
                request_path = self.root / f"{request_id}-action.json"
                request_path.write_text(json.dumps({"input": str(input_path)}), encoding="utf-8")
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
            native_action_command: list[str] | str = action_command
            if same_project_cd_action_prefix:
                native_action_command = (
                    f"cd {self.project.resolve()} && {shlex.join(action_command)}"
                )
                prefixed_action_commands[request_id] = native_action_command
            self.assertEqual(
                receive(
                    callback(
                        "PreToolUse",
                        request_id=request_id,
                        generation_id=generation_id,
                        message_id=user_id,
                        tool_id=action_tool,
                        command=native_action_command,
                        client_version=client_version,
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
                        client_version=client_version,
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
                    client_version=client_version,
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
                    client_version=client_version,
                )
            )
            self.assertEqual(stopped["status"], "stop_recorded")
            return activated, result_id

        business_dto: dict[str, object]
        if pathless_business_dto:
            business_dto = {"objective": "summarize the current inventory"}
        else:
            business_dto = {"input": str(automatic_input)}
        first_prompt = (
            json.dumps(business_dto)
            if selectorless
            else "@//text-report/" + json.dumps(business_dto)
        )
        if pathless_business_dto:
            self.assertNotIn(str(automatic_input), first_prompt)
            self.assertNotIn("text-report", first_prompt)

        first_result = run_turn(
            request_id="workbuddy-request-one",
            generation_id="workbuddy-generation-one",
            user_id="workbuddy-user-one",
            parent_id=None,
            prompt=first_prompt,
            input_path=automatic_input,
            target="reports/workbuddy-one.md",
            use_skill_pre=True,
            include_title=True,
            include_assistant_text=False,
            client_version=first_client_version,
            rejected_bash_version=first_rejected_bash_version,
            scope_drift=first_scope_drift,
            skill_args_drift=first_skill_args_drift,
        )
        if first_skill_args_drift:
            self.assertIsNone(first_result)
            return
        assert first_result is not None
        first, first_result_id = first_result
        (self.root / "workbuddy-correction-pass").write_text("pass", encoding="utf-8")
        if source_goal_missing_before_correction:
            database = open_database(self.project)
            try:
                database.execute(
                    """UPDATE managed_contexts
                    SET business_dto_digest = NULL, business_dto_rule_version = NULL
                    WHERE request_id = 'workbuddy-request-one'"""
                )
                database.commit()
            finally:
                database.close()
        correction_dto = {
            **business_dto,
            "selc": {
                "intent": "correct_delivery",
                "target": "reports/workbuddy-one.md",
                "preserve_goal": True,
            },
        }
        correction_result = run_turn(
            request_id="workbuddy-request-correction",
            generation_id="workbuddy-generation-correction",
            user_id="workbuddy-user-correction",
            parent_id=first_result_id,
            prompt=("" if correction_selectorless else "@//text-report/")
            + json.dumps(correction_dto),
            input_path=automatic_input,
            target="reports/workbuddy-one.md",
            use_skill_pre=True,
            include_title=False,
            include_assistant_text=True,
            client_version=correction_client_version,
            rejected_bash_version=correction_rejected_bash_version,
            expected_correction_rejection=(
                source_goal_missing_before_correction and not allow_legacy_explicit_fallback
            ),
        )
        if source_goal_missing_before_correction and not allow_legacy_explicit_fallback:
            self.assertIsNone(correction_result)
            database = open_database(self.project)
            try:
                call_count = database.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
                correction_context = database.execute(
                    """SELECT root_valid, unknown_change FROM managed_contexts
                    WHERE request_id = 'workbuddy-request-correction'"""
                ).fetchone()
                correction_receipts = database.execute(
                    """SELECT COUNT(*) FROM managed_receipts
                    WHERE request_id = 'workbuddy-request-correction'"""
                ).fetchone()[0]
            finally:
                database.close()
            self.assertEqual(call_count, 1)
            self.assertEqual(tuple(correction_context), (0, 1))
            self.assertEqual(correction_receipts, 0)
            return
        self.assertIsNotNone(correction_result)
        correction, _ = correction_result
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
                (row["status"], row["trigger_kind"])
                for row in database.execute(
                    "SELECT status, trigger_kind FROM calls WHERE task_id = ?", (first["task_id"],)
                )
            }
            correction_context = database.execute(
                "SELECT context_key, root_valid FROM managed_contexts WHERE request_id = ?",
                ("workbuddy-request-correction",),
            ).fetchone()
            binding = database.execute(
                """SELECT status, anchor_event_id, anchor_rule_version
                FROM managed_correction_bindings WHERE task_id = ?""",
                (first["task_id"],),
            ).fetchone()
            anchor = database.execute(
                """SELECT pre_event_id, rule_version FROM managed_request_anchors
                WHERE context_key = ?""",
                (correction_context["context_key"],),
            ).fetchone()
            technical_receipts = database.execute(
                """SELECT COUNT(*) FROM managed_native_events AS events
                JOIN managed_receipts AS receipts ON receipts.native_event_id = events.event_id
                WHERE events.tool_use_id IN (?, ?)""",
                (
                    "workbuddy-request-one-skill",
                    "workbuddy-request-correction-skill",
                ),
            ).fetchone()[0]
            mixed_version_receipts = database.execute(
                """SELECT COUNT(*) FROM managed_native_events AS events
                JOIN managed_receipts AS receipts ON receipts.native_event_id = events.event_id
                WHERE events.tool_use_id IN (?, ?)""",
                (
                    "workbuddy-request-one-mixed-version-invoke",
                    "workbuddy-request-correction-mixed-version-invoke",
                ),
            ).fetchone()[0]
            call_count = database.execute(
                "SELECT COUNT(*) FROM calls WHERE task_id = ?", (first["task_id"],)
            ).fetchone()[0]
            first_action_payload = None
            if same_project_cd_action_prefix:
                first_action_event = database.execute(
                    "SELECT payload_json FROM managed_native_events WHERE tool_use_id = ?",
                    ("workbuddy-request-one-action",),
                ).fetchone()
                self.assertIsNotNone(first_action_event)
                first_action_payload = first_action_event["payload_json"]
        finally:
            database.close()
        self.assertEqual(windows_closed, 2, windows)
        first_trigger = "automatic" if selectorless else "explicit"
        correction_trigger = "automatic" if correction_selectorless else "explicit"
        self.assertEqual(statuses, {("closed", first_trigger), ("closed", correction_trigger)})
        self.assertEqual(correction_context["root_valid"], 0)
        self.assertEqual(binding["status"], "closed")
        expected_anchor_rule = (
            "current-request-v4-workbuddy-auto-skill-pre"
            if correction_selectorless
            else "current-request-v3-workbuddy-skill-pre"
        )
        self.assertEqual(anchor["rule_version"], expected_anchor_rule)
        self.assertEqual(binding["anchor_event_id"], anchor["pre_event_id"])
        self.assertEqual(binding["anchor_rule_version"], anchor["rule_version"])
        self.assertEqual(technical_receipts, 0)
        self.assertEqual(mixed_version_receipts, 0)
        self.assertEqual(call_count, 2)
        if same_project_cd_action_prefix:
            self.assertIsNotNone(first_action_payload)
            action_payload = json.loads(first_action_payload)
            expected_digest = hashlib.sha256(
                prefixed_action_commands["workbuddy-request-one"].encode("utf-8")
            ).hexdigest()
            self.assertEqual(action_payload["tool_command_digest"], expected_digest)
        health = managed_runtime.health(self.project, "managed-reports", str(first["task_id"]))
        self.assertEqual((health["first_result"], health["final_result"]), ("fail", "pass"))

    def test_workbuddy_skill_pre_requires_exact_pending_current_request(self) -> None:
        """The WorkBuddy technical anchor accepts only its finite pending-call shape."""

        cwd = self.project.resolve()
        session_id = "workbuddy-skill-session"
        request_id = "workbuddy-skill-request"
        message_id = "workbuddy-skill-user"
        tool_id = "workbuddy-skill-call"
        prompt = '@//text-report/{"input":"resources/input.json","selc":'
        prompt += '{"intent":"correct_delivery","target":"reports/item.json","preserve_goal":true}}'
        request = _managed_prompt_request(
            "workbuddy",
            {"version": "5.8.3", "prompt": prompt},
            declared_skills=frozenset({"text-report"}),
        )
        args = json.dumps(
            {
                "input": "resources/input.json",
                "selc": {
                    "intent": "correct_delivery",
                    "target": "reports/item.json",
                    "preserve_goal": True,
                },
            },
            separators=(",", ":"),
        )
        prompt_payload = {
            "surface": "WorkBuddy",
            "client_version": "5.8.3",
            "skill": "text-report",
            "request_digest": managed_runtime._native_request_digest(
                {"request": request}, "text-report"
            ),
            "raw_request_digest": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "correction": request["correction"],
        }
        event = {
            "host": "workbuddy",
            "surface": "WorkBuddy",
            "client_version": "5.8.3",
            "technical_skill_event": True,
            "technical_skill_pre": True,
            "technical_skill_valid": True,
            "skill": "text-report",
            "request_digest": prompt_payload["request_digest"],
            "correction": prompt_payload["correction"],
            "technical_skill_args_digest": hashlib.sha256(args.encode("utf-8")).hexdigest(),
            "tool_use_id": tool_id,
            "session_id": session_id,
            "request_id": request_id,
            "cwd": str(cwd),
        }
        p0 = [
            {"type": "session-meta", "sessionId": session_id},
            {
                "id": "workbuddy-skill-prior-result",
                "type": "function_call_result",
                "callId": "workbuddy-skill-prior-call",
                "sessionId": session_id,
                "cwd": str(cwd),
            },
        ]
        native_user_text = (
            '<system-reminder data-role="user-context">\n'
            "synthetic prior context\n"
            "</system-reminder>\n"
            '<system-reminder data-role="additional-data">\n'
            "synthetic additional data\n"
            f"</system-reminder>\n<user_query>{prompt}</user_query>"
        )
        appended = [
            {
                "id": message_id,
                "type": "message",
                "role": "user",
                "parentId": "workbuddy-skill-prior-result",
                "sessionId": session_id,
                "cwd": str(cwd),
                "providerData": {
                    "startsNewUserRequest": True,
                    "conversationRequestId": request_id,
                },
                "content": [{"type": "input_text", "text": native_user_text}],
            },
            {
                "type": "file-history-snapshot",
                "messageId": message_id,
                "cwd": str(cwd),
                "timestamp": 1_769_000_000,
                "isSnapshotUpdate": True,
                "_meta": {
                    "baggage": "synthetic-baggage",
                    "traceparent": "synthetic-traceparent",
                },
                "snapshot": {"messageId": message_id, "trackedFileBackups": {}},
            },
            {
                "id": "workbuddy-skill-reasoning",
                "type": "reasoning",
                "parentId": message_id,
                "sessionId": session_id,
                "cwd": str(cwd),
                "providerData": {
                    "conversationRequestId": request_id,
                    "messageId": "workbuddy-skill-model",
                    "model": "workbuddy-model",
                    "stepSeq": 1,
                },
            },
            {
                "id": "workbuddy-skill-native-call",
                "type": "function_call",
                "name": "Skill",
                "callId": tool_id,
                "parentId": "workbuddy-skill-reasoning",
                "sessionId": session_id,
                "cwd": str(cwd),
                "providerData": {
                    "conversationRequestId": request_id,
                    "messageId": "workbuddy-skill-model",
                    "model": "workbuddy-model",
                    "stepSeq": 1,
                },
                "arguments": json.dumps(
                    {"skill": "text-report", "args": args}, separators=(",", ":")
                ),
            },
        ]

        def verified(
            candidate_event: dict[str, object] | None = None,
            candidate_appended: list[dict[str, object]] | None = None,
            candidate_p0: list[dict[str, object]] | None = None,
            candidate_prompt_payload: dict[str, object] | None = None,
        ) -> bool:
            return managed_runtime._workbuddy_skill_pre_request_is_verified(
                p0 if candidate_p0 is None else candidate_p0,
                appended if candidate_appended is None else candidate_appended,
                session_id=session_id,
                cwd=cwd,
                request_id=request_id,
                raw_prompt_digest=prompt_payload["raw_request_digest"],
                prompt_payload=(
                    prompt_payload if candidate_prompt_payload is None else candidate_prompt_payload
                ),
                event=event if candidate_event is None else candidate_event,
            )

        self.assertTrue(verified())
        prompt_592 = {**prompt_payload, "client_version": "5.9.2"}
        event_592 = {**event, "client_version": "5.9.2"}
        self.assertTrue(
            verified(
                candidate_event=event_592,
                candidate_prompt_payload=prompt_592,
            )
        )
        self.assertFalse(
            verified(
                candidate_event=event_592,
                candidate_prompt_payload=prompt_payload,
            )
        )
        self.assertFalse(
            verified(
                candidate_event=event,
                candidate_prompt_payload=prompt_592,
            )
        )
        for invalid_version in ("5.8.2", "5.9.1", "5.9.3", "", None, 592):
            self.assertFalse(verified(candidate_event={**event, "client_version": invalid_version}))
            self.assertFalse(
                verified(
                    candidate_prompt_payload={
                        **prompt_payload,
                        "client_version": invalid_version,
                    }
                )
            )
        self.assertFalse(verified(candidate_event={**event, "surface": "Other"}))
        self.assertFalse(verified(candidate_prompt_payload={**prompt_payload, "surface": "Other"}))
        self.assertFalse(verified(candidate_appended=appended[:3]))

        pathless_prompt = json.dumps({"objective": "summarize current inventory"})
        pathless_request = _managed_prompt_request(
            "workbuddy",
            {"version": "5.8.3", "prompt": pathless_prompt},
            declared_skills=frozenset({"text-report"}),
        )
        pathless_args = pathless_prompt
        pathless_business_digest = managed_runtime._business_dto_goal_digest(
            pathless_request["input"]
        )
        self.assertIsNotNone(pathless_business_digest)
        pathless_prompt_payload = {
            "surface": "WorkBuddy",
            "client_version": "5.8.3",
            "skill": "",
            "request_digest": managed_runtime._native_request_digest(
                {
                    "request": {
                        "skill": "text-report",
                        "input": pathless_request["input"],
                    }
                },
                "text-report",
            ),
            "raw_request_digest": hashlib.sha256(pathless_prompt.encode("utf-8")).hexdigest(),
            "business_dto_digest": pathless_business_digest,
            "business_dto_rule_version": "business-dto-v1",
            "correction": None,
        }
        pathless_event = {
            **event,
            "request_digest": pathless_prompt_payload["request_digest"],
            "correction": None,
            "technical_skill_args_digest": hashlib.sha256(
                pathless_args.encode("utf-8")
            ).hexdigest(),
        }
        pathless_appended = json.loads(json.dumps(appended))
        pathless_appended[0]["content"][0]["text"] = native_user_text.replace(
            prompt, pathless_prompt
        )
        pathless_appended[-1]["arguments"] = json.dumps(
            {"skill": "text-report", "args": pathless_args}, separators=(",", ":")
        )
        self.assertTrue(
            managed_runtime._workbuddy_skill_pre_request_is_verified(
                p0,
                pathless_appended,
                session_id=session_id,
                cwd=cwd,
                request_id=request_id,
                raw_prompt_digest=pathless_prompt_payload["raw_request_digest"],
                prompt_payload=pathless_prompt_payload,
                event=pathless_event,
            )
        )

        assistant_record = {
            "id": "workbuddy-skill-assistant",
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "parentId": "workbuddy-skill-reasoning",
            "sessionId": session_id,
            "cwd": str(cwd),
            "timestamp": 1_769_000_000,
            "_meta": {
                "baggage": "synthetic-baggage",
                "traceparent": "synthetic-traceparent",
            },
            "providerData": {
                "agent": "synthetic-agent",
                "conversationRequestId": request_id,
                "messageId": "workbuddy-skill-model",
                "model": "workbuddy-model",
                "requestModelId": "synthetic-request-model",
                "requestModelName": "Synthetic WorkBuddy model",
                "stepSeq": 1,
                "traceId": "synthetic-trace",
            },
            "content": [
                {
                    "type": "output_text",
                    "text": "Synthetic completed assistant output.",
                    "providerData": {"annotations": []},
                }
            ],
        }
        assistant_appended = json.loads(json.dumps(appended))
        assistant_appended.insert(3, assistant_record)
        assistant_appended[4]["parentId"] = assistant_record["id"]
        self.assertTrue(verified(candidate_appended=assistant_appended))

        title_and_assistant = json.loads(json.dumps(assistant_appended))
        title_and_assistant.insert(
            2,
            {
                "id": "workbuddy-skill-title-and-assistant",
                "type": "ai-title",
                "aiTitle": "Synthetic title",
                "timestamp": 1_769_000_000,
                "sessionId": session_id,
                "cwd": str(cwd),
                "_meta": {
                    "baggage": "synthetic-baggage",
                    "traceparent": "synthetic-traceparent",
                },
            },
        )
        self.assertTrue(verified(candidate_appended=title_and_assistant))

        assistant_with_extra_field = json.loads(json.dumps(assistant_appended))
        assistant_with_extra_field[3]["message"] = "unobserved"
        self.assertFalse(verified(candidate_appended=assistant_with_extra_field))
        assistant_with_tool_result = json.loads(json.dumps(assistant_appended))
        assistant_with_tool_result[3]["providerData"]["toolResult"] = {"status": "completed"}
        self.assertFalse(verified(candidate_appended=assistant_with_tool_result))
        for provider_field in (
            "agent",
            "conversationRequestId",
            "messageId",
            "model",
            "requestModelId",
            "requestModelName",
            "stepSeq",
            "traceId",
        ):
            assistant_with_missing_provider_field = json.loads(json.dumps(assistant_appended))
            assistant_with_missing_provider_field[3]["providerData"].pop(provider_field)
            self.assertFalse(
                verified(candidate_appended=assistant_with_missing_provider_field),
                provider_field,
            )
        for provider_field in (
            "agent",
            "conversationRequestId",
            "messageId",
            "model",
            "requestModelId",
            "requestModelName",
            "traceId",
        ):
            assistant_with_invalid_provider_type = json.loads(json.dumps(assistant_appended))
            assistant_with_invalid_provider_type[3]["providerData"][provider_field] = 1
            self.assertFalse(
                verified(candidate_appended=assistant_with_invalid_provider_type),
                provider_field,
            )
        assistant_with_empty_agent = json.loads(json.dumps(assistant_appended))
        assistant_with_empty_agent[3]["providerData"]["agent"] = ""
        self.assertFalse(verified(candidate_appended=assistant_with_empty_agent))
        assistant_with_boolean_step = json.loads(json.dumps(assistant_appended))
        assistant_with_boolean_step[3]["providerData"]["stepSeq"] = True
        self.assertFalse(verified(candidate_appended=assistant_with_boolean_step))
        assistant_with_wrong_parent = json.loads(json.dumps(assistant_appended))
        assistant_with_wrong_parent[3]["parentId"] = "wrong-reasoning"
        self.assertFalse(verified(candidate_appended=assistant_with_wrong_parent))
        assistant_with_wrong_scope = json.loads(json.dumps(assistant_appended))
        assistant_with_wrong_scope[3]["cwd"] = str(self.root / "other-project")
        self.assertFalse(verified(candidate_appended=assistant_with_wrong_scope))
        assistant_with_tool_content = json.loads(json.dumps(assistant_appended))
        assistant_with_tool_content[3]["content"][0]["type"] = "tool_use"
        self.assertFalse(verified(candidate_appended=assistant_with_tool_content))
        assistant_with_multiple_content = json.loads(json.dumps(assistant_appended))
        assistant_with_multiple_content[3]["content"].append(
            {
                "type": "output_text",
                "text": "Another output.",
                "providerData": {"annotations": []},
            }
        )
        self.assertFalse(verified(candidate_appended=assistant_with_multiple_content))
        assistant_with_annotations = json.loads(json.dumps(assistant_appended))
        assistant_with_annotations[3]["content"][0]["providerData"]["annotations"] = ["x"]
        self.assertFalse(verified(candidate_appended=assistant_with_annotations))
        assistant_with_other_model = json.loads(json.dumps(assistant_appended))
        assistant_with_other_model[3]["providerData"]["model"] = "other-model"
        self.assertFalse(verified(candidate_appended=assistant_with_other_model))
        skill_with_other_step = json.loads(json.dumps(assistant_appended))
        skill_with_other_step[4]["providerData"]["stepSeq"] = 2
        self.assertFalse(verified(candidate_appended=skill_with_other_step))
        duplicate_assistant = json.loads(json.dumps(assistant_appended))
        duplicate_assistant.insert(4, json.loads(json.dumps(assistant_record)))
        duplicate_assistant[4]["id"] = "workbuddy-skill-second-assistant"
        self.assertFalse(verified(candidate_appended=duplicate_assistant))
        assistant_position_tool_result = json.loads(json.dumps(assistant_appended))
        assistant_position_tool_result[3] = {
            "id": "workbuddy-skill-unexpected-result",
            "type": "function_call_result",
            "callId": "other-tool",
            "parentId": "workbuddy-skill-reasoning",
            "sessionId": session_id,
            "cwd": str(cwd),
        }
        self.assertFalse(verified(candidate_appended=assistant_position_tool_result))
        assistant_position_user = json.loads(json.dumps(assistant_appended))
        assistant_position_user[3]["role"] = "user"
        self.assertFalse(verified(candidate_appended=assistant_position_user))

        initial_p0 = [
            {
                "id": "workbuddy-skill-initial-meta",
                "type": "session-meta",
                "sessionId": session_id,
                "cwd": str(cwd),
            }
        ]
        initial_appended = json.loads(json.dumps(appended))
        initial_appended[0].pop("parentId")
        initial_appended.insert(
            2,
            {
                "id": "workbuddy-skill-initial-title",
                "type": "ai-title",
                "aiTitle": "Synthetic title",
                "timestamp": 1_769_000_000,
                "sessionId": session_id,
                "cwd": str(cwd),
                "_meta": {
                    "baggage": "synthetic-baggage",
                    "traceparent": "synthetic-traceparent",
                },
            },
        )
        self.assertTrue(verified(candidate_appended=initial_appended, candidate_p0=initial_p0))
        explicit_null_initial_parent = json.loads(json.dumps(initial_appended))
        explicit_null_initial_parent[0]["parentId"] = None
        self.assertFalse(
            verified(candidate_appended=explicit_null_initial_parent, candidate_p0=initial_p0)
        )
        wrong_initial_parent = json.loads(json.dumps(initial_appended))
        wrong_initial_parent[0]["parentId"] = "workbuddy-skill-initial-meta"
        self.assertFalse(verified(candidate_appended=wrong_initial_parent, candidate_p0=initial_p0))
        for mutate in (
            lambda value: value.replace("user-context", "wrong-role", 1),
            lambda value: value.replace("\n", "\r\n", 1),
            lambda value: value.replace(
                "synthetic prior context",
                "synthetic prior context<user_query>nested</user_query>",
            ),
            lambda value: value.replace(
                "synthetic additional data",
                'synthetic additional data<system-reminder data-role="nested">x</system-reminder>',
            ),
            lambda value: value + "trailing",
            lambda value: value.replace(prompt, prompt + " altered", 1),
        ):
            malformed_envelope = json.loads(json.dumps(appended))
            original_text = malformed_envelope[0]["content"][0]["text"]
            malformed_envelope[0]["content"][0]["text"] = mutate(original_text)
            self.assertFalse(verified(candidate_appended=malformed_envelope))
        title_with_extra_key = json.loads(json.dumps(initial_appended))
        title_with_extra_key[2]["parentId"] = message_id
        self.assertFalse(verified(candidate_appended=title_with_extra_key, candidate_p0=initial_p0))
        title_with_boolean_timestamp = json.loads(json.dumps(initial_appended))
        title_with_boolean_timestamp[2]["timestamp"] = True
        self.assertFalse(
            verified(candidate_appended=title_with_boolean_timestamp, candidate_p0=initial_p0)
        )
        title_with_wrong_scope = json.loads(json.dumps(initial_appended))
        title_with_wrong_scope[2]["cwd"] = str(self.root / "other-project")
        self.assertFalse(
            verified(candidate_appended=title_with_wrong_scope, candidate_p0=initial_p0)
        )
        p0_identifier_collision = json.loads(json.dumps(p0))
        p0_identifier_collision[0]["id"] = message_id
        self.assertFalse(verified(candidate_p0=p0_identifier_collision))
        duplicate_p0_identifier = json.loads(json.dumps(p0))
        duplicate_p0_identifier[0]["id"] = "workbuddy-skill-prior-result"
        self.assertFalse(verified(candidate_p0=duplicate_p0_identifier))
        reused_call_identifier = json.loads(json.dumps(p0))
        reused_call_identifier[1]["callId"] = tool_id
        self.assertFalse(verified(candidate_p0=reused_call_identifier))
        shared_historic_call_identifier = json.loads(json.dumps(p0))
        shared_historic_call_identifier.insert(
            1,
            {
                "id": "workbuddy-skill-prior-call",
                "type": "function_call",
                "callId": "workbuddy-skill-prior-call",
                "sessionId": session_id,
                "cwd": str(cwd),
            },
        )
        self.assertTrue(verified(candidate_p0=shared_historic_call_identifier))
        for changed_event in (
            {**event, "client_version": "5.8.2"},
            {**event, "tool_use_id": "other-call"},
            {**event, "request_id": "other-request"},
            {**event, "session_id": "other-session"},
            {**event, "cwd": str(self.root / "other-project")},
            {**event, "technical_skill_valid": False},
            {**event, "technical_skill_args_digest": "0" * 64},
        ):
            self.assertFalse(verified(candidate_event=changed_event))

        wrong_parent = json.loads(json.dumps(appended))
        wrong_parent[2]["parentId"] = "wrong-parent"
        self.assertFalse(verified(candidate_appended=wrong_parent))
        missing_user_parent = json.loads(json.dumps(appended))
        missing_user_parent[0].pop("parentId")
        self.assertFalse(verified(candidate_appended=missing_user_parent))
        wrong_snapshot = json.loads(json.dumps(appended))
        wrong_snapshot[1]["snapshot"]["trackedFileBackups"] = ["unexpected"]
        self.assertFalse(verified(candidate_appended=wrong_snapshot))
        missing_snapshot_timestamp = json.loads(json.dumps(appended))
        missing_snapshot_timestamp[1].pop("timestamp")
        self.assertFalse(verified(candidate_appended=missing_snapshot_timestamp))
        unknown_snapshot_field = json.loads(json.dumps(appended))
        unknown_snapshot_field[1]["sessionId"] = session_id
        self.assertFalse(verified(candidate_appended=unknown_snapshot_field))
        wrong_snapshot_metadata = json.loads(json.dumps(appended))
        wrong_snapshot_metadata[1]["_meta"]["baggage"] = False
        self.assertFalse(verified(candidate_appended=wrong_snapshot_metadata))
        wrong_callback_identity = {**event, "message_id": "different-native-user"}
        self.assertFalse(verified(candidate_event=wrong_callback_identity))
        for field in ("id", "sessionId", "cwd"):
            missing_identity = json.loads(json.dumps(appended))
            missing_identity[3].pop(field)
            self.assertFalse(verified(candidate_appended=missing_identity))
        for field in ("sessionId", "cwd"):
            missing_scope = json.loads(json.dumps(appended))
            missing_scope[2].pop(field)
            self.assertFalse(verified(candidate_appended=missing_scope))
        duplicate_reasoning_identity = json.loads(json.dumps(appended))
        duplicate_reasoning_identity[2]["id"] = message_id
        self.assertFalse(verified(candidate_appended=duplicate_reasoning_identity))
        completed_call = json.loads(json.dumps(appended))
        completed_call[3]["status"] = "completed"
        self.assertFalse(verified(candidate_appended=completed_call))
        unknown_call_status = json.loads(json.dumps(appended))
        unknown_call_status[3]["status"] = "pending"
        self.assertFalse(verified(candidate_appended=unknown_call_status))
        different_model = json.loads(json.dumps(appended))
        different_model[3]["providerData"]["model"] = "other-workbuddy-model"
        self.assertFalse(verified(candidate_appended=different_model))
        wrong_args = json.loads(json.dumps(appended))
        wrong_args[3]["arguments"] = json.dumps(
            {"skill": "text-report", "args": '{"input":"resources/other.json"}'},
            separators=(",", ":"),
        )
        self.assertFalse(verified(candidate_appended=wrong_args))
        wrong_name = json.loads(json.dumps(appended))
        wrong_name[3]["arguments"] = json.dumps(
            {"skill": "other-skill", "args": args}, separators=(",", ":")
        )
        self.assertFalse(verified(candidate_appended=wrong_name))
        completed = json.loads(json.dumps(appended))
        completed.append(
            {
                "type": "function_call_result",
                "callId": tool_id,
                "parentId": "workbuddy-skill-native-call",
                "sessionId": session_id,
                "cwd": str(cwd),
            }
        )
        self.assertFalse(verified(candidate_appended=completed))
        wrong_scope = json.loads(json.dumps(p0))
        wrong_scope[1]["cwd"] = str(self.root / "other-project")
        self.assertFalse(verified(candidate_p0=wrong_scope))

    def test_workbuddy_skill_input_accepts_only_one_declared_json_object(self) -> None:
        """Skill hook input cannot fall back to a command or a permissive serializer."""

        args = '{"input":"resources/input.json"}'
        request, skill, args_digest, valid = _workbuddy_skill_request(
            {"skill": "text-report", "args": args},
            declared_skills=frozenset({"text-report"}),
        )
        self.assertEqual(
            request, {"skill": "text-report", "input": {"input": "resources/input.json"}}
        )
        self.assertEqual(skill, "text-report")
        self.assertEqual(args_digest, hashlib.sha256(args.encode("utf-8")).hexdigest())
        self.assertTrue(valid)

        invalid_inputs = (
            {"skill": "text-report", "args": args, "command": "ignored"},
            {"skill": "text-report", "args": args + " trailing"},
            {"skill": "text-report", "args": "[]"},
            {"skill": "other-skill", "args": args},
            {"skill": "text-report", "args": None},
            {"command": "managed invoke"},
        )
        for tool_input in invalid_inputs:
            _, _, _, is_valid = _workbuddy_skill_request(
                tool_input,
                declared_skills=frozenset({"text-report"}),
            )
            self.assertFalse(is_valid)

    def test_legacy_nontechnical_workbuddy_event_replays_without_a_digest_conflict(self) -> None:
        """A pre-Skill native row retains its original digest formula on replay."""

        session_id = "legacy-workbuddy-session"
        history = self.root / ".codebuddy" / "legacy-workbuddy.jsonl"
        history.parent.mkdir(parents=True)
        history.write_text(
            json.dumps({"type": "session-meta", "sessionId": session_id}) + "\n",
            encoding="utf-8",
        )
        prompt = '@//text-report/{"input":"resources/input.json"}'
        payload = {
            "hook_event_name": "UserPromptSubmit",
            "client": "WorkBuddy",
            "version": "5.8.3",
            "cwd": str(self.project.resolve()),
            "session_id": session_id,
            "generation_id": "legacy-workbuddy-generation",
            "conversation_request_id": "legacy-workbuddy-request",
            "prompt": prompt,
            "transcript_path": str(history),
            "prefix_length": history.stat().st_size,
        }
        environment = {"CODEBUDDY_SESSION_ID": session_id}
        first = receive_native_hook(
            project_root=self.project,
            suite_id="managed-reports",
            host="workbuddy",
            payload=payload,
            environment=environment,
        )
        self.assertEqual(first["status"], "prompt_recorded")

        database = open_database(self.project)
        try:
            row = database.execute("SELECT * FROM managed_native_events").fetchone()
            legacy_payload = json.loads(row["payload_json"])
            for key in (
                "technical_skill_event",
                "technical_skill_pre",
                "technical_skill_valid",
                "technical_skill_args_digest",
            ):
                legacy_payload.pop(key)
            legacy_digest = managed_runtime._digest(
                {
                    "identity": row["stable_identity"],
                    "command": None,
                    "raw_request": legacy_payload["raw_request_digest"],
                    "source": "native-collector",
                }
            )
            database.execute(
                """UPDATE managed_native_events
                SET payload_digest = ?, payload_json = ? WHERE event_id = ?""",
                (legacy_digest, json.dumps(legacy_payload, sort_keys=True), row["event_id"]),
            )
            database.commit()
        finally:
            database.close()

        replay = receive_native_hook(
            project_root=self.project,
            suite_id="managed-reports",
            host="workbuddy",
            payload=payload,
            environment=environment,
        )
        self.assertEqual(replay["status"], "replayed")
        database = open_database(self.project)
        try:
            self.assertEqual(
                database.execute("SELECT COUNT(*) FROM managed_native_events").fetchone()[0], 1
            )
            self.assertEqual(
                database.execute("SELECT COUNT(*) FROM managed_native_conflicts").fetchone()[0], 0
            )
        finally:
            database.close()

    def test_workbuddy_skill_pre_cannot_replace_invalid_or_terminated_request_facts(self) -> None:
        """A later Skill callback cannot repair a frozen failed or terminal boundary."""

        manifest_path = self.suite / "suite.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["managed"]["native_hosts"] = {"workbuddy": ["5.8.3"]}
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        seal_suite(self.suite)
        installation = install_suite(
            source=self.suite,
            project_root=self.project,
            harness="workbuddy",
            ref=None,
        )

        session_id = "workbuddy-skill-fence-session"
        project_cwd = str(self.project.resolve())
        compressed_cwd = project_cwd.lstrip("/").replace("/", "-")
        history = self.root / ".codebuddy" / "projects" / compressed_cwd / f"{session_id}.jsonl"
        history.parent.mkdir(parents=True)
        history.write_text(
            "\n".join(
                (
                    json.dumps({"type": "session-meta", "sessionId": session_id}),
                    json.dumps(
                        {
                            "type": "session-meta",
                            "sessionId": session_id,
                            "cwd": project_cwd,
                        }
                    ),
                )
            )
            + "\n",
            encoding="utf-8",
        )

        base_environment = {**os.environ, "CODEBUDDY_SESSION_ID": session_id}

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
            message_id: str,
            tool_id: str | None = None,
            tool_input: dict[str, object] | None = None,
            tool_name: str = "Skill",
            prompt: str | None = None,
        ) -> dict[str, object]:
            payload: dict[str, object] = {
                "hook_event_name": event_name,
                "client": "WorkBuddy",
                "version": "5.8.3",
                "cwd": project_cwd,
                "session_id": session_id,
                "generation_id": f"generation-{request_id}",
                "conversation_request_id": request_id,
                "conversation_message_id": message_id,
                "transcript_path": str(history),
                "prefix_length": history.stat().st_size,
            }
            if prompt is not None:
                payload["prompt"] = prompt
            if tool_id is not None:
                payload["tool_call_id"] = tool_id
            if tool_input is not None:
                payload["tool_name"] = tool_name
                payload["tool_input"] = tool_input
            return payload

        def append(record: dict[str, object]) -> None:
            with history.open("ab") as stream:
                stream.write((json.dumps(record) + "\n").encode("utf-8"))

        def begin_pending_skill(
            *,
            request_id: str,
            message_id: str,
            tool_id: str,
            args: str,
            parent_id: str | None = None,
        ) -> str:
            prompt = "@//text-report/" + args
            self.assertEqual(
                receive(
                    callback(
                        "UserPromptSubmit",
                        request_id=request_id,
                        message_id=message_id,
                        prompt=prompt,
                    )
                )["status"],
                "prompt_recorded",
            )
            append(
                {
                    "id": message_id,
                    "type": "message",
                    "role": "user",
                    "parentId": parent_id,
                    "sessionId": session_id,
                    "cwd": project_cwd,
                    "providerData": {
                        "startsNewUserRequest": True,
                        "conversationRequestId": request_id,
                    },
                    "content": [{"type": "input_text", "text": prompt}],
                }
            )
            append(
                {
                    "type": "file-history-snapshot",
                    "messageId": message_id,
                    "cwd": project_cwd,
                    "snapshot": {"messageId": message_id, "trackedFileBackups": {}},
                }
            )
            reasoning_id = f"{tool_id}-reasoning"
            append(
                {
                    "id": reasoning_id,
                    "type": "reasoning",
                    "parentId": message_id,
                    "sessionId": session_id,
                    "cwd": project_cwd,
                    "providerData": {
                        "conversationRequestId": request_id,
                        "messageId": f"{tool_id}-model",
                        "model": "workbuddy-model",
                    },
                }
            )
            append(
                {
                    "id": f"{tool_id}-call",
                    "type": "function_call",
                    "name": "Skill",
                    "callId": tool_id,
                    "parentId": reasoning_id,
                    "sessionId": session_id,
                    "cwd": project_cwd,
                    "providerData": {
                        "conversationRequestId": request_id,
                        "messageId": f"{tool_id}-model",
                        "model": "workbuddy-model",
                    },
                    "arguments": json.dumps(
                        {"skill": "text-report", "args": args}, separators=(",", ":")
                    ),
                }
            )
            return prompt

        invalid_args = '{"input":"resources/input.json"} trailing'
        begin_pending_skill(
            request_id="invalid-request",
            message_id="invalid-user",
            tool_id="invalid-skill",
            args=invalid_args,
        )
        self.assertEqual(
            receive(
                callback(
                    "PreToolUse",
                    request_id="invalid-request",
                    message_id="invalid-user",
                    tool_id="invalid-skill",
                    tool_input={"skill": "text-report", "args": invalid_args},
                )
            )["status"],
            "technical_unusable",
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
        blocked_invoke = [
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
            "workbuddy",
            "--expected-version",
            str(installation["version"]),
            "--expected-composite",
            str(installation["digest"]),
        ]
        with self.assertRaises(SelcError) as blocked:
            receive(
                callback(
                    "PreToolUse",
                    request_id="invalid-request",
                    message_id="invalid-user",
                    tool_id="invalid-bash-invoke",
                    tool_name="Bash",
                    tool_input={"command": shlex.join(blocked_invoke)},
                )
            )
        self.assertEqual(blocked.exception.code, "NATIVE_REQUEST_UNAVAILABLE")
        database = open_database(self.project)
        try:
            invalid_context = database.execute(
                """SELECT root_valid, unknown_change FROM managed_contexts
                WHERE request_id = 'invalid-request'"""
            ).fetchone()
            invalid_receipts = database.execute(
                """SELECT COUNT(*) FROM managed_receipts
                WHERE request_id = 'invalid-request'"""
            ).fetchone()[0]
        finally:
            database.close()
        self.assertIsNotNone(invalid_context)
        self.assertEqual(invalid_context["root_valid"], 1)
        self.assertEqual(invalid_context["unknown_change"], 1)
        self.assertEqual(invalid_receipts, 0)
        valid_args = '{"input":"resources/input.json"}'
        append(
            {
                "id": "invalid-later-call",
                "type": "function_call",
                "name": "Skill",
                "callId": "invalid-later-skill",
                "parentId": "invalid-skill-reasoning",
                "sessionId": session_id,
                "cwd": project_cwd,
                "providerData": {
                    "conversationRequestId": "invalid-request",
                    "messageId": "invalid-user",
                },
                "arguments": json.dumps(
                    {"skill": "text-report", "args": valid_args}, separators=(",", ":")
                ),
            }
        )
        self.assertEqual(
            receive(
                callback(
                    "PreToolUse",
                    request_id="invalid-request",
                    message_id="invalid-user",
                    tool_id="invalid-later-skill",
                    tool_input={"skill": "text-report", "args": valid_args},
                )
            )["status"],
            "technical_unusable",
        )

        begin_pending_skill(
            request_id="post-first-request",
            message_id="post-first-user",
            tool_id="post-first-skill",
            args=valid_args,
            parent_id="invalid-later-call",
        )
        post_first = callback(
            "PostToolUse",
            request_id="post-first-request",
            message_id="post-first-user",
            tool_id="post-first-different-tool",
            tool_input={"skill": "text-report", "args": valid_args},
        )
        self.assertEqual(receive(post_first)["status"], "post_recorded")
        self.assertEqual(
            receive(
                callback(
                    "PreToolUse",
                    request_id="post-first-request",
                    message_id="post-first-user",
                    tool_id="post-first-skill",
                    tool_input={"skill": "text-report", "args": valid_args},
                )
            )["status"],
            "technical_unusable",
        )

        begin_pending_skill(
            request_id="non-skill-pre-first-request",
            message_id="non-skill-pre-first-user",
            tool_id="non-skill-pre-first-skill",
            args=valid_args,
            parent_id="post-first-skill-call",
        )
        self.assertEqual(
            receive(
                callback(
                    "PreToolUse",
                    request_id="non-skill-pre-first-request",
                    message_id="non-skill-pre-first-user",
                    tool_id="non-skill-pre-first-bash",
                    tool_name="Bash",
                    tool_input={"command": "echo untrusted"},
                )
            )["status"],
            "recorded_unusable",
        )
        self.assertEqual(
            receive(
                callback(
                    "PreToolUse",
                    request_id="non-skill-pre-first-request",
                    message_id="non-skill-pre-first-user",
                    tool_id="non-skill-pre-first-skill",
                    tool_input={"skill": "text-report", "args": valid_args},
                )
            )["status"],
            "technical_unusable",
        )

        begin_pending_skill(
            request_id="stop-first-request",
            message_id="stop-first-user",
            tool_id="stop-first-skill",
            args=valid_args,
            parent_id="non-skill-pre-first-skill-call",
        )
        self.assertEqual(
            receive(
                callback(
                    "Stop",
                    request_id="stop-first-request",
                    message_id="stop-first-user",
                )
            )["status"],
            "stop_recorded",
        )
        self.assertEqual(
            receive(
                callback(
                    "PreToolUse",
                    request_id="stop-first-request",
                    message_id="stop-first-user",
                    tool_id="stop-first-skill",
                    tool_input={"skill": "text-report", "args": valid_args},
                )
            )["status"],
            "technical_unusable",
        )

        database = open_database(self.project)
        try:
            anchors = database.execute("SELECT COUNT(*) FROM managed_request_anchors").fetchone()[0]
            receipts = database.execute("SELECT COUNT(*) FROM managed_receipts").fetchone()[0]
            tasks = database.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            calls = database.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
        finally:
            database.close()
        self.assertEqual((anchors, receipts, tasks, calls), (0, 0, 0, 0))

    def test_run_action_executes_a_frozen_path_snapshot(self) -> None:
        """Mutating a source after validation cannot change the child process input."""

        activated = self._activate()
        action_receipt = self._observe("text-report", "tool-frozen-action")
        original_input = self.input_path.read_text(encoding="utf-8")
        mutated_input = "changed\nsource\n"
        verify_frozen_input = managed_runtime._assert_correction_request_input
        mutation_count = 0

        def mutate_source_after_frozen_check(*args: object, **kwargs: object) -> None:
            nonlocal mutation_count
            verify_frozen_input(*args, **kwargs)
            mutation_count += 1
            self.input_path.write_text(mutated_input, encoding="utf-8")

        with mock.patch(
            "selc.managed_runtime._assert_correction_request_input",
            side_effect=mutate_source_after_frozen_check,
        ):
            action = managed_runtime.run_action(
                project_root=self.project,
                suite_id="managed-reports",
                skill="text-report",
                task_id=str(activated["task_id"]),
                call_id=str(activated["call_id"]),
                action_id="text-report",
                receipt=action_receipt,
                request={"input": str(self.input_path)},
            )

        self.assertEqual(mutation_count, 1)
        self.assertEqual(self.input_path.read_text(encoding="utf-8"), mutated_input)
        self.assertEqual(original_input, "first\nsecond\n")
        self.assertEqual(
            Path(str(action["artifact_path"])).read_text(encoding="utf-8"),
            "# Text report\n\n- first\n- second\n",
        )

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
            completed_item(command=["/bin/zsh", "-l", command]),
            completed_item(command=["/bin/zsh", "-ic", command]),
            completed_item(command=["/bin/zsh", {"flag": "-c"}, command]),
            completed_item(command=["/bin/zsh", ["-c"], command]),
            completed_item(command=["/bin/zsh", "-c", command, "unexpected"]),
            completed_item(command=["/bin/zsh", "-c", command + " --altered"]),
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
        """A `-c` completion closes only the current native publication window."""

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
                    "command": ["/bin/zsh", "-c", command],
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

    def test_publication_assessment_reduction_cannot_clear_a_sticky_unknown_context(self) -> None:
        """Publication reconciliation never turns an existing unknown into pass."""

        activated = self._activate()
        database = open_database(self.project)
        try:
            database.execute(
                "UPDATE managed_contexts SET unknown_change = 1 WHERE task_id = ?",
                (activated["task_id"],),
            )
            managed_runtime.reduce_managed_assessment(database, str(activated["task_id"]))
            unknown_change = database.execute(
                "SELECT unknown_change FROM managed_contexts WHERE task_id = ?",
                (activated["task_id"],),
            ).fetchone()[0]
        finally:
            database.close()

        self.assertEqual(unknown_change, 1)

    def test_publication_window_rejects_a_mutated_or_truncated_cutoff(self) -> None:
        """A frozen Stop cutoff accepts only its exact `-c` completion."""

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
                                "command": ["/bin/zsh", "-c", command],
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
        history.insert(
            4,
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "developer",
                    "internal_chat_message_metadata_passthrough": {
                        "turn_id": "codex-turn",
                        "content_item_kinds": ["generic.developer_instructions"],
                    },
                },
            },
        )
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
                        "content_item_kinds": [
                            "host_skills.instructions",
                            "generic.developer_instructions",
                        ],
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

    def test_natural_correction_does_not_reuse_a_business_task(self) -> None:
        self._activate()
        second_payload = self._payload("activate", "tool-second-activate")
        second_payload.update(
            {
                "generation_id": "generation-correction",
                "request_id": "request-correction",
                "root_request_id": "root-a",
                "causation": "business_correction",
            }
        )
        observed = managed_runtime.observe_request_boundary(
            project_root=self.project,
            suite_id="managed-reports",
            payload=second_payload,
            fixture=True,
        )
        self.assertEqual(observed["status"], "recorded_unusable")
        self.assertNotIn("receipt", observed)
        database = open_database(self.project)
        try:
            task_count = database.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            context = database.execute(
                "SELECT task_id, intervention, unknown_change FROM managed_contexts "
                "WHERE request_id = 'request-correction'"
            ).fetchone()
        finally:
            database.close()
        self.assertEqual(task_count, 1)
        self.assertEqual(tuple(context), (None, 0, 1))

    def test_audit_context_is_paused_without_reusing_a_business_task(self) -> None:
        self._activate()
        audit = self._payload("activate", "tool-audit")
        audit.update(
            {
                "generation_id": "generation-audit",
                "request_id": "request-audit",
                "causation": "audit",
            }
        )
        observed = managed_runtime.observe_request_boundary(
            project_root=self.project,
            suite_id="managed-reports",
            payload=audit,
            fixture=True,
        )
        self.assertEqual(observed["status"], "recorded_unusable")
        self.assertNotIn("receipt", observed)

        database = open_database(self.project)
        try:
            task_count = database.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            context = database.execute(
                "SELECT task_id, intervention, unknown_change FROM managed_contexts "
                "WHERE request_id = 'request-audit'"
            ).fetchone()
        finally:
            database.close()
        self.assertEqual(task_count, 1)
        self.assertEqual(tuple(context), (None, 0, 1))

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
        prefixed_command = f"cd {self.project.resolve()} && {command}"
        prefixed_event = dict(event)
        prefixed_event["tool_command_digest"] = hashlib.sha256(
            prefixed_command.encode()
        ).hexdigest()
        self.assertFalse(
            managed_runtime._workbuddy_history_contains_publication_result(
                [user, call, result], prefixed_event, "wb-tool"
            )
        )
        prefixed_call = dict(call)
        prefixed_call["arguments"] = json.dumps(
            {"command": prefixed_command, "description": "publish"}
        )
        self.assertTrue(
            managed_runtime._workbuddy_history_contains_publication_result(
                [user, prefixed_call, result], prefixed_event, "wb-tool"
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

    def test_stop_closes_the_original_task_when_natural_correction_is_paused(self) -> None:
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
        observed = managed_runtime.observe_request_boundary(
            project_root=self.project,
            suite_id="managed-reports",
            payload=correction,
            fixture=True,
        )
        self.assertEqual(observed["status"], "recorded_unusable")
        self.assertNotIn("receipt", observed)

        self._stop("tool-activate")

        database = open_database(self.project)
        try:
            first_status = database.execute(
                "SELECT status FROM calls WHERE call_id = ?", (first["call_id"],)
            ).fetchone()["status"]
            task_status = database.execute(
                "SELECT status FROM tasks WHERE task_id = ?", (first["task_id"],)
            ).fetchone()["status"]
            task_count = database.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        finally:
            database.close()
        self.assertEqual(first_status, "closed")
        self.assertEqual(task_status, "closed")
        self.assertEqual(task_count, 1)

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

    def test_receipt_is_one_use_and_natural_correction_stays_unbound(self) -> None:
        self._activate()
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
        observed = managed_runtime.observe_request_boundary(
            project_root=self.project,
            suite_id="managed-reports",
            payload=correction,
            fixture=True,
        )
        self.assertEqual(observed["status"], "recorded_unusable")
        self.assertNotIn("receipt", observed)

        database = open_database(self.project)
        try:
            task_count = database.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        finally:
            database.close()
        self.assertEqual(task_count, 1)

    def test_parent_request_does_not_bind_a_natural_correction(self) -> None:
        self._activate()
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
        observed = managed_runtime.observe_request_boundary(
            project_root=self.project,
            suite_id="managed-reports",
            payload=correction,
            fixture=True,
        )
        self.assertEqual(observed["status"], "recorded_unusable")
        self.assertNotIn("receipt", observed)

        database = open_database(self.project)
        try:
            task_count = database.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        finally:
            database.close()
        self.assertEqual(task_count, 1)

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

    def test_workbuddy_skill_chip_accepts_only_its_exact_declared_json_transport(self) -> None:
        prompt = (
            '@//item-summary/{"input":".selc/installs/item-summary-fixture/current/'
            'resources/items.json","mode":"controlled-fail"}'
        )
        expected_digest = "9b00af373f37613b4b88d986d280d44f9425f9dcd053f7ebd54228ba1cf89722"
        self.assertEqual(hashlib.sha256(prompt.encode("utf-8")).hexdigest(), expected_digest)
        expected_request = {
            "skill": "item-summary",
            "input": {
                "input": ".selc/installs/item-summary-fixture/current/resources/items.json",
                "mode": "controlled-fail",
            },
            "raw_prompt_digest": expected_digest,
        }
        for version in ("5.8.3", "5.9.2"):
            request = _managed_prompt_request(
                "workbuddy",
                {"version": version, "prompt": prompt},
                declared_skills=frozenset({"item-summary"}),
            )
            self.assertEqual(request, expected_request)

        correction_prompt = (
            '@//item-summary/{"input":"resources/items.json","selc":'
            '{"intent":"correct_delivery","target":"reports/item.json","preserve_goal":true}}'
        )
        correction = _managed_prompt_request(
            "workbuddy",
            {"version": "5.8.3", "prompt": correction_prompt},
            declared_skills=frozenset({"item-summary"}),
        )
        self.assertEqual(correction["input"], {"input": "resources/items.json"})
        self.assertEqual(
            correction["correction"],
            {
                "intent": "correct_delivery",
                "target": "reports/item.json",
                "preserve_goal": True,
                "valid": True,
            },
        )

        ordinary_codex = _managed_prompt_request(
            "codex",
            {
                "version": "0.163.0-alpha.10.2",
                "prompt": '@item-summary {"input":"resources/items.json"}',
            },
            declared_skills=frozenset({"item-summary"}),
        )
        self.assertEqual(ordinary_codex["input"], {"input": "resources/items.json"})
        self.assertNotIn("correction", ordinary_codex)

        for host, payload in (
            (
                "codex",
                {
                    "version": "0.163.0-alpha.10.2",
                    "prompt": "@item-summary ",
                },
            ),
            (
                "workbuddy",
                {
                    "version": "5.8.3",
                    "prompt": "@//item-summary/",
                },
            ),
        ):
            for invalid_control in (None, False, [], "correct_delivery"):
                input_value = {
                    "input": "resources/items.json",
                    "selc": invalid_control,
                }
                parsed = _managed_prompt_request(
                    host,
                    {
                        **payload,
                        "prompt": str(payload["prompt"])
                        + json.dumps(input_value, separators=(",", ":")),
                    },
                    declared_skills=frozenset({"item-summary"}),
                )
                self.assertEqual(parsed["input"], {"input": "resources/items.json"})
                self.assertEqual(parsed["correction"], {"valid": False})

        invalid_prompts = (
            '@//unknown/{"input":"resources/items.json"}',
            '@//item-summary {"input":"resources/items.json"}',
            '@//item-summary/["not-an-object"]',
            '@//item-summary/{"input":"resources/items.json"} trailing',
            '@//item-summary/%7B"input":"resources/items.json"%7D',
        )
        for invalid in invalid_prompts:
            self.assertEqual(
                _managed_prompt_request(
                    "workbuddy",
                    {"version": "5.8.3", "prompt": invalid},
                    declared_skills=frozenset({"item-summary"}),
                )["input"],
                {"classification": "unknown"},
            )
        invalid_workbuddy_versions: tuple[object, ...] = (
            "5.8.2",
            "5.9.1",
            "5.9.3",
            "",
            None,
            592,
        )
        invalid_host_versions = [("codex", "0.163.0-alpha.10.2")]
        invalid_host_versions.extend(
            ("workbuddy", version) for version in invalid_workbuddy_versions
        )
        for host, version in invalid_host_versions:
            self.assertEqual(
                _managed_prompt_request(
                    host,
                    {"version": version, "prompt": prompt},
                    declared_skills=frozenset({"item-summary"}),
                )["input"],
                {"classification": "unknown"},
            )

    def test_invalid_structured_correction_never_issues_a_receipt(self) -> None:
        for index, invalid_control in enumerate((None, False, [], "correct_delivery")):
            request_id = f"invalid-correction-request-{index}"
            generation_id = f"invalid-correction-generation-{index}"
            request = _managed_prompt_request(
                "codex",
                {
                    "version": "0.163.0-alpha.10.2",
                    "prompt": "@text-report "
                    + json.dumps(
                        {"input": str(self.input_path), "selc": invalid_control},
                        separators=(",", ":"),
                    ),
                },
                declared_skills=frozenset({"text-report"}),
            )
            prompt = self._payload(
                "activate",
                f"invalid-correction-prompt-{index}",
                phase="UserPromptSubmit",
            )
            prompt.update(
                {
                    "generation_id": generation_id,
                    "request_id": request_id,
                    "request": request,
                }
            )
            prompt_observed = managed_runtime.observe_request_boundary(
                project_root=self.project,
                suite_id="managed-reports",
                payload=prompt,
                fixture=True,
            )
            self.assertEqual(prompt_observed["status"], "prompt_recorded")

            pre = self._payload("activate", f"invalid-correction-pre-{index}")
            pre.update(
                {
                    "generation_id": generation_id,
                    "request_id": request_id,
                    "request": request,
                }
            )
            observed = managed_runtime.observe_request_boundary(
                project_root=self.project,
                suite_id="managed-reports",
                payload=pre,
                fixture=True,
            )
            self.assertEqual(observed["status"], "recorded_unusable")
            self.assertNotIn("receipt", observed)

        database = open_database(self.project)
        try:
            self.assertEqual(database.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)
            self.assertEqual(
                database.execute("SELECT COUNT(*) FROM managed_receipts").fetchone()[0], 0
            )
        finally:
            database.close()

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

    def test_pending_native_projection_recovers_prompt_and_stop_on_replay(self) -> None:
        """A committed raw callback retries only its missing projection."""

        prompt = self._payload("activate", "", phase="UserPromptSubmit")
        with (
            mock.patch(
                "selc.managed_runtime._record_prompt_context",
                side_effect=RuntimeError("prompt projection"),
            ),
            self.assertRaises(RuntimeError),
        ):
            managed_runtime.observe_request_boundary(
                project_root=self.project,
                suite_id="managed-reports",
                payload=prompt,
                fixture=True,
            )
        database = open_database(self.project)
        try:
            self.assertEqual(
                database.execute("SELECT COUNT(*) FROM managed_native_events").fetchone()[0], 1
            )
            self.assertEqual(
                database.execute("SELECT COUNT(*) FROM managed_contexts").fetchone()[0], 0
            )
            self.assertEqual(
                database.execute("SELECT projection_status FROM managed_native_events").fetchone()[
                    0
                ],
                "pending",
            )
        finally:
            database.close()
        recovered_prompt = managed_runtime.observe_request_boundary(
            project_root=self.project,
            suite_id="managed-reports",
            payload=prompt,
            fixture=True,
        )
        self.assertEqual(recovered_prompt["status"], "prompt_recorded")

        stop = self._stop("tool-pending-stop")
        self.assertEqual(stop["status"], "stop_recorded")
        database = open_database(self.project)
        try:
            self.assertEqual(
                database.execute("SELECT COUNT(*) FROM managed_native_events").fetchone()[0], 2
            )
        finally:
            database.close()

    def test_pending_stop_projection_recovers_without_reclassifying_the_callback(self) -> None:
        """A Stop replay completes its durable closure after a projection fault."""

        self._observe("activate", "tool-pending-stop")
        stop = self._payload("publish", "tool-pending-stop", phase="Stop")
        stop.pop("tool_name")
        stop.pop("tool_input")
        stop["request"] = {"source": "user-prompt"}
        with (
            mock.patch(
                "selc.managed_runtime._close_pending_windows_on_stop",
                side_effect=RuntimeError("stop projection"),
            ),
            self.assertRaises(RuntimeError),
        ):
            managed_runtime.observe_request_boundary(
                project_root=self.project,
                suite_id="managed-reports",
                payload=stop,
                fixture=True,
            )
        database = open_database(self.project)
        try:
            self.assertEqual(
                database.execute(
                    "SELECT projection_status FROM managed_native_events WHERE phase = 'Stop'"
                ).fetchone()[0],
                "pending",
            )
        finally:
            database.close()

        recovered_stop = managed_runtime.observe_request_boundary(
            project_root=self.project,
            suite_id="managed-reports",
            payload=stop,
            fixture=True,
        )
        self.assertEqual(recovered_stop["status"], "stop_recorded")

        replayed_stop = managed_runtime.observe_request_boundary(
            project_root=self.project,
            suite_id="managed-reports",
            payload=stop,
            fixture=True,
        )
        self.assertEqual(replayed_stop["status"], "replayed")

    def test_post_before_pre_and_stopped_receipt_cannot_start_business_work(self) -> None:
        """Late callbacks cannot issue or consume a receipt after native termination."""

        self._observe("activate", "tool-post-before-pre", phase="PostToolUse")
        late_pre = managed_runtime.observe_request_boundary(
            project_root=self.project,
            suite_id="managed-reports",
            payload=self._payload("activate", "tool-post-before-pre"),
            fixture=True,
        )
        self.assertEqual(late_pre["status"], "recorded_unusable")
        self.assertNotIn("receipt", late_pre)

        stopped_pre = self._payload("activate", "tool-stopped-receipt")
        stopped_pre["generation_id"] = "generation-stopped-receipt"
        stopped_pre["request_id"] = "request-stopped-receipt"
        stopped_observed = managed_runtime.observe_request_boundary(
            project_root=self.project,
            suite_id="managed-reports",
            payload=stopped_pre,
            fixture=True,
        )
        receipt = str(stopped_observed["receipt"])
        stopped_event = dict(stopped_pre)
        stopped_event["phase"] = "Stop"
        stopped_event.pop("tool_name")
        stopped_event.pop("tool_input")
        stopped_event["request"] = {"source": "user-prompt"}
        self.assertEqual(
            managed_runtime.observe_request_boundary(
                project_root=self.project,
                suite_id="managed-reports",
                payload=stopped_event,
                fixture=True,
            )["status"],
            "stop_recorded",
        )
        with self.assertRaises(SelcError) as captured:
            managed_runtime.activate(
                project_root=self.project,
                suite_id="managed-reports",
                skill="text-report",
                receipt=receipt,
            )
        self.assertEqual(captured.exception.code, "RECEIPT_INVALID")

        database = open_database(self.project)
        try:
            self.assertEqual(database.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)
        finally:
            database.close()

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

    def test_workbuddy_skill_chip_hook_uses_payload_identity_without_hook_environment(self) -> None:
        prompt = '@//text-report/{"objective":"summarize"}'
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
            "prompt": prompt,
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
        stored = json.loads(event["payload_json"])
        self.assertEqual(
            stored["raw_request_digest"],
            hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        )

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
        self.assertEqual(replayed_prompt["status"], "replayed")

        database = open_database(self.project)
        try:
            replayed_context = database.execute(
                "SELECT root_valid, request_digest, input_skill, unknown_change "
                "FROM managed_contexts"
            ).fetchone()
            trigger_kind = database.execute("SELECT trigger_kind FROM calls").fetchone()[0]
        finally:
            database.close()
        self.assertEqual(tuple(replayed_context), (1, "", "", 0))
        self.assertEqual(trigger_kind, "unknown")

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
            (1, "", 1),
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
        install_suite(
            source=suite,
            project_root=project,
            harness="codex",
            ref=None,
            mode="observed",
        )

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

    def test_business_dto_goal_digest_strips_only_top_level_selc(self) -> None:
        business = {
            "objective": "summarize the current inventory",
            "nested": {"selc": "ordinary business data"},
        }
        corrected = {
            **business,
            "selc": {
                "intent": "correct_delivery",
                "target": "reports/inventory.md",
                "preserve_goal": True,
            },
        }

        original_digest = managed_runtime._business_dto_goal_digest(business)
        corrected_digest = managed_runtime._business_dto_goal_digest(corrected)

        self.assertIsNotNone(original_digest)
        self.assertEqual(original_digest, corrected_digest)
        self.assertIsNone(managed_runtime._business_dto_goal_digest({"classification": "unknown"}))
        self.assertIsNone(managed_runtime._business_dto_goal_digest({"selc": None}))

        request = {"skill": "", "input": business}
        self.assertEqual(
            managed_runtime._business_dto_goal_digest_from_request(request), original_digest
        )
        valid_request = {
            **request,
            "correction": {
                "intent": "correct_delivery",
                "target": "reports/inventory.md",
                "preserve_goal": True,
                "valid": True,
            },
        }
        self.assertEqual(
            managed_runtime._business_dto_goal_digest_from_request(valid_request), original_digest
        )
        invalid_controls: list[object] = [
            None,
            False,
            "correct_delivery",
            [],
            {"valid": True},
            {"classification": "unknown"},
        ]
        for invalid_control in invalid_controls:
            with self.subTest(invalid_control=invalid_control):
                self.assertIsNone(
                    managed_runtime._business_dto_goal_digest_from_request(
                        {**request, "correction": invalid_control}
                    )
                )

    def test_automatic_selection_rejects_a_later_different_actual_skill(self) -> None:
        database = open_database(self.project)
        try:
            context = {
                "input_skill": "",
                "business_dto_digest": managed_runtime._business_dto_goal_digest(
                    {"objective": "summarize current inventory"}
                ),
                "business_dto_rule_version": "business-dto-v1",
                "selected_skill": "text-report",
            }
            event = {"skill": "other-report", "host": "workbuddy", "action": "activate"}
            self.assertFalse(
                managed_runtime._context_accepts_receipt_skill(database, context, event)
            )
        finally:
            database.close()

    def _create_protocol_five_database(self, name: str) -> Path:
        """Create a records database with the frozen c20 protocol-5 writer."""

        metadata = json.loads(PROTOCOL_FIVE_RUNTIME_METADATA.read_text(encoding="utf-8"))
        self.assertEqual(metadata["writer_protocol_version"], 5)
        self.assertEqual(
            hashlib.sha256(PROTOCOL_FIVE_RUNTIME_ARCHIVE.read_bytes()).hexdigest(),
            metadata["archive_sha256"],
        )
        with zipfile.ZipFile(PROTOCOL_FIVE_RUNTIME_ARCHIVE) as archive:
            archived_names = sorted(archive.namelist())
        self.assertEqual(archived_names, sorted(metadata["files"]))
        self.assertEqual(
            metadata["files"]["selc/records.py"],
            "e4ea95c6a1bdb3eee2b341403dbb913d2bd8b9fd1621c6c7550dd3c71d5de860",
        )

        project = self.root / name
        initializer = '''
import hashlib
import json
import sys
from pathlib import Path

from selc import managed_runtime
from selc.records import WRITER_PROTOCOL_VERSION, open_database

assert WRITER_PROTOCOL_VERSION == 5
project = Path(sys.argv[1])
project.mkdir(parents=True, exist_ok=True)
database = open_database(project)
database.execute(
    """INSERT INTO tasks(
    task_id, suite_id, suite_version, harness, initiator, evidence, is_test, status,
    created_at, owner_origin, assessment_owner)
    VALUES ('task-old', 'managed-reports', '1.0.0', 'workbuddy', 'unknown', 'native', 0,
    'closed', '2026-10-25T00:00:00Z', 'origin-old', 'legacy')"""
)
database.execute(
    """INSERT INTO calls(
    call_id, task_id, skill, executor, actual_harness, evidence, expected_version,
    expected_hash, trigger_kind, status, created_at, assessment_owner)
    VALUES ('call-old', 'task-old', 'text-report', 'workbuddy', 'workbuddy', 'native',
    '1.0.0', 'install-old', 'explicit', 'closed', '2026-10-25T00:00:00Z', 'legacy')"""
)
history = project / "legacy-history.jsonl"
history.write_bytes(b"{}" + bytes([10]))


def store_old_prompt(event_id, request_id, status, skill):
    raw_prompt_digest = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
    payload = {
        "host": "workbuddy",
        "surface": "WorkBuddy",
        "client_version": "5.8.3",
        "cwd": str(project),
        "session_id": "session-old",
        "generation_id": "generation-old",
        "request_id": request_id,
        "phase": "UserPromptSubmit",
        "request": {
            **({"skill": skill} if skill else {}),
            "input": {"objective": "legacy " + request_id},
            "raw_prompt_digest": raw_prompt_digest,
        },
        "transcript_path": str(history),
        "prefix_length": history.stat().st_size,
    }
    event = managed_runtime._normalize_native_payload(
        project,
        "managed-reports",
        payload,
        trusted_producer=False,
        fixture=True,
    )
    event["event_id"] = event_id
    stored = managed_runtime._store_native_event(database, event)
    assert stored["status"] == "recorded"
    database.execute(
        """UPDATE managed_native_events
        SET status = ?, projection_status = 'applied' WHERE event_id = ?""",
        (status, event_id),
    )


store_old_prompt("event-applied", "request-old", "applied", "text-report")
store_old_prompt("event-invalid", "request-invalid", "invalid", "text-report")
store_old_prompt("event-terminated", "request-terminated", "terminated", "text-report")
store_old_prompt("event-backfilled", "request-backfilled", "recorded", "")

database.execute(
    """INSERT INTO managed_contexts(
    context_key, suite_id, host, session_id, generation_id, request_id, root_request_id,
    request_digest, input_skill, root_event_id, root_valid, task_id, status, created_at)
    VALUES ('context-old', 'managed-reports', 'workbuddy', 'session-old', 'generation-old',
    'request-old', '', 'request-old-digest', 'text-report', 'event-applied', 1,
    'task-old', 'closed', '2026-10-25T00:00:00Z')"""
)
database.execute(
    """INSERT INTO managed_contexts(
    context_key, suite_id, host, session_id, generation_id, request_id, root_request_id,
    request_digest, input_skill, root_event_id, root_valid, status, created_at)
    VALUES ('context-backfilled', 'managed-reports', 'workbuddy', 'session-old', 'generation-old',
    'request-backfilled', '', 'request-backfilled-digest', 'text-report',
    'event-backfilled', 1, 'active', '2026-10-25T00:00:00Z')"""
)
database.commit()
database.close()
'''
        completed = subprocess.run(
            [sys.executable, "-c", initializer, str(project)],
            env={**os.environ, "PYTHONPATH": str(PROTOCOL_FIVE_RUNTIME_ARCHIVE)},
            capture_output=True,
            check=False,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        return project

    def test_protocol_five_database_migrates_without_reclassifying_old_facts(self) -> None:
        old_project = self._create_protocol_five_database("protocol-five-project")
        old_path = old_project / ".selc" / "records.sqlite3"
        stale_writer = sqlite3.connect(old_path)
        stale_writer.create_function("selc_writer_protocol", 0, lambda: 5)
        legacy_events = (
            sqlite3.connect(old_path)
            .execute(
                """SELECT event_id, payload_digest, payload_json, status, projection_status
            FROM managed_native_events ORDER BY event_id"""
            )
            .fetchall()
        )

        database = open_database(old_project)
        try:
            self.assertEqual(
                database.execute("SELECT value FROM meta WHERE key = 'writer_protocol'").fetchone()[
                    0
                ],
                "6",
            )
            migrated_events = [
                tuple(row)
                for row in database.execute(
                    """SELECT event_id, payload_digest, payload_json, status, projection_status
                    FROM managed_native_events ORDER BY event_id"""
                ).fetchall()
            ]
            context = database.execute(
                """SELECT business_dto_digest, business_dto_rule_version, selected_skill
                FROM managed_contexts WHERE context_key = 'context-old'"""
            ).fetchone()
            call_trigger = database.execute(
                "SELECT trigger_kind FROM calls WHERE call_id = 'call-old'"
            ).fetchone()[0]
            receipt_count = database.execute("SELECT COUNT(*) FROM managed_receipts").fetchone()[0]
            backfilled_context = database.execute(
                "SELECT * FROM managed_contexts WHERE context_key = 'context-backfilled'"
            ).fetchone()
            self.assertIsNotNone(backfilled_context)
            self.assertEqual(
                managed_runtime._confirm_context_selection(
                    database, backfilled_context, "text-report"
                ),
                "unknown",
            )
            database.commit()
        finally:
            database.close()
        self.assertEqual(migrated_events, legacy_events)
        self.assertEqual(tuple(context), (None, None, None))
        self.assertEqual(call_trigger, "explicit")
        self.assertEqual(receipt_count, 0)

        reopen_old_writer = """
import sys
from pathlib import Path
from selc.errors import SelcError
from selc.records import open_database

try:
    open_database(Path(sys.argv[1]))
except SelcError as error:
    print(error.code)
    raise SystemExit(7)
raise SystemExit(0)
"""
        completed = subprocess.run(
            [sys.executable, "-c", reopen_old_writer, str(old_project)],
            env={**os.environ, "PYTHONPATH": str(PROTOCOL_FIVE_RUNTIME_ARCHIVE)},
            capture_output=True,
            check=False,
            text=True,
        )
        self.assertEqual(completed.returncode, 7, completed.stdout + completed.stderr)
        self.assertEqual(completed.stdout.strip(), "UNSUPPORTED_DATABASE_PROTOCOL")

        insert = """INSERT INTO managed_native_events(
        event_id, stable_identity, payload_digest, payload_json, host, suite_id,
        session_id, generation_id, request_id, tool_use_id, phase, received_at,
        status, projection_status)
        VALUES ('event-stale', 'stable-stale', 'stale-digest', '{}', 'workbuddy',
        'suite-old', 'session-old', 'generation-old', 'request-old', 'tool-stale',
        'PreToolUse', '2026-10-25T00:00:00Z', 'recorded', 'pending')"""
        with self.assertRaisesRegex(sqlite3.DatabaseError, "SELC_WRITER_PROTOCOL_REQUIRED"):
            stale_writer.execute(insert)
        stale_writer.rollback()
        with self.assertRaisesRegex(sqlite3.DatabaseError, "SELC_WRITER_PROTOCOL_REQUIRED"):
            stale_writer.execute(
                "UPDATE managed_native_events SET status = 'recorded' WHERE event_id = 'event-invalid'"
            )
        stale_writer.close()

    def test_protocol_five_replays_keep_legacy_native_facts_unclassified(self) -> None:
        """Current callbacks replay frozen protocol-5 rows without adding goal evidence."""

        old_project = self._create_protocol_five_database("protocol-five-replay")
        database = open_database(old_project)
        database.close()
        install_suite(
            source=self.suite,
            project_root=old_project,
            harness="workbuddy",
            ref=None,
            mode="observed",
        )
        history = old_project / "legacy-history.jsonl"

        def legacy_callback(request_id: str) -> dict[str, object]:
            raw_prompt_digest = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
            return {
                "host": "workbuddy",
                "surface": "WorkBuddy",
                "client_version": "5.8.3",
                "cwd": str(old_project),
                "session_id": "session-old",
                "generation_id": "generation-old",
                "request_id": request_id,
                "phase": "UserPromptSubmit",
                "request": {
                    "skill": "text-report",
                    "input": {"objective": "legacy " + request_id},
                    "raw_prompt_digest": raw_prompt_digest,
                },
                "transcript_path": str(history),
                "prefix_length": history.stat().st_size,
            }

        database = open_database(old_project)
        try:
            before_events = [
                tuple(row)
                for row in database.execute(
                    """SELECT event_id, payload_digest, payload_json, status, projection_status
                    FROM managed_native_events
                    WHERE event_id IN ('event-applied', 'event-invalid', 'event-terminated')
                    ORDER BY event_id"""
                ).fetchall()
            ]
            before_context = database.execute(
                """SELECT business_dto_digest, business_dto_rule_version, selected_skill
                FROM managed_contexts WHERE context_key = 'context-old'"""
            ).fetchone()
            before_counts = tuple(
                database.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("managed_request_anchors", "managed_receipts", "calls")
            )
        finally:
            database.close()

        for request_id in ("request-old", "request-invalid", "request-terminated"):
            replayed = managed_runtime.observe_request_boundary(
                project_root=old_project,
                suite_id="managed-reports",
                payload=legacy_callback(request_id),
                fixture=True,
            )
            self.assertEqual(replayed, {"status": "replayed"})

        history.write_bytes(history.read_bytes() + b'{"late":true}' + bytes([10]))
        self.assertEqual(
            managed_runtime.observe_request_boundary(
                project_root=old_project,
                suite_id="managed-reports",
                payload=legacy_callback("request-old"),
                fixture=True,
            ),
            {"status": "replayed"},
        )

        database = open_database(old_project)
        try:
            after_events = [
                tuple(row)
                for row in database.execute(
                    """SELECT event_id, payload_digest, payload_json, status, projection_status
                    FROM managed_native_events
                    WHERE event_id IN ('event-applied', 'event-invalid', 'event-terminated')
                    ORDER BY event_id"""
                ).fetchall()
            ]
            after_context = database.execute(
                """SELECT business_dto_digest, business_dto_rule_version, selected_skill
                FROM managed_contexts WHERE context_key = 'context-old'"""
            ).fetchone()
            after_counts = tuple(
                database.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("managed_request_anchors", "managed_receipts", "calls")
            )
        finally:
            database.close()

        self.assertEqual(after_events, before_events)
        self.assertEqual(tuple(after_context), (None, None, None))
        self.assertEqual(tuple(after_context), tuple(before_context))
        self.assertEqual(after_counts, before_counts)

    def test_failed_protocol_six_migration_rolls_back_to_protocol_five_guards(self) -> None:
        old_project = self._create_protocol_five_database("protocol-five-rollback")
        old_path = old_project / ".selc" / "records.sqlite3"
        before = (
            sqlite3.connect(old_path)
            .execute(
                "SELECT event_id, payload_digest, payload_json, status FROM managed_native_events ORDER BY event_id"
            )
            .fetchall()
        )
        with (
            mock.patch(
                "selc.records._create_managed_runtime_tables", side_effect=RuntimeError("stop")
            ),
            self.assertRaisesRegex(RuntimeError, "stop"),
        ):
            open_database(old_project)
        legacy = sqlite3.connect(old_path)
        try:
            protocol = legacy.execute(
                "SELECT value FROM meta WHERE key = 'writer_protocol'"
            ).fetchone()[0]
            triggers = legacy.execute(
                """SELECT sql FROM sqlite_master
                WHERE type = 'trigger' AND name GLOB 'writer_protocol_managed_*'"""
            ).fetchall()
            after = legacy.execute(
                """SELECT event_id, payload_digest, payload_json, status
                FROM managed_native_events ORDER BY event_id"""
            ).fetchall()
        finally:
            legacy.close()
        self.assertEqual(protocol, "5")
        self.assertTrue(triggers)
        self.assertTrue(all("!= 5" in row[0] for row in triggers))
        self.assertEqual(after, before)

        reopened = open_database(old_project)
        try:
            self.assertEqual(
                reopened.execute("SELECT value FROM meta WHERE key = 'writer_protocol'").fetchone()[
                    0
                ],
                "6",
            )
        finally:
            reopened.close()

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
