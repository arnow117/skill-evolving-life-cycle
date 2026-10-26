from __future__ import annotations

import importlib.util
import json
import os
import shlex
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

PROBE_PATH = Path(__file__).resolve().parents[2] / "native_context_probe.py"
MODULE_SPEC = importlib.util.spec_from_file_location("native_context_probe", PROBE_PATH)
assert MODULE_SPEC is not None
assert MODULE_SPEC.loader is not None
probe = importlib.util.module_from_spec(MODULE_SPEC)
sys.modules[MODULE_SPEC.name] = probe
MODULE_SPEC.loader.exec_module(probe)


class NativeContextProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.workspace = self.root / "fixture"
        self.original_directory = Path.cwd()

    def tearDown(self) -> None:
        os.chdir(self.original_directory)
        self.temporary_directory.cleanup()

    def prepare(self, host: str = "codex") -> dict[str, object]:
        return probe.prepare(self.workspace, host)

    def pre_tool_event(
        self,
        *,
        session_id: str = "session-1",
        tool_call_id: str = "tool-1",
        thread_id: str = "thread-1",
        turn_id: str = "turn-1",
        generation_id: str = "generation-1",
        message_id: str = "message-1",
        request_id: str = "request-1",
        command: str = "printf should-not-run",
    ) -> dict[str, object]:
        return {
            "hook_event_name": "PreToolUse",
            "cwd": str(self.workspace),
            "session_id": session_id,
            "tool_call_id": tool_call_id,
            "thread_id": thread_id,
            "turn_id": turn_id,
            "generation_id": generation_id,
            "conversation_message_id": message_id,
            "request_id": request_id,
            "tool_name": "Bash",
            "tool_input": {"command": command},
        }

    def run_with_environment(self, values: dict[str, str]) -> dict[str, object]:
        original_values = {name: os.environ.get(name) for name in probe.ALLOWED_ENVIRONMENT_NAMES}
        try:
            for name in probe.ALLOWED_ENVIRONMENT_NAMES:
                os.environ.pop(name, None)
            os.environ.update(values)
            os.chdir(self.workspace)
            return probe.run(self.workspace)
        finally:
            for name, value in original_values.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

    def prepare_updated_input_fixture(self) -> dict[str, object]:
        return probe.prepare(self.workspace, "workbuddy", transport="workbuddy-updated-input")

    def prepare_codex_updated_input_fixture(self) -> dict[str, object]:
        return probe.prepare(self.workspace, "codex", transport="codex-updated-input")

    def workbuddy_pre_tool_event(
        self,
        *,
        session_id: str = "workbuddy-session-1",
        generation_id: str = "generation-1",
        tool_use_id: str = "tool-use-1",
        call_id: str = "call-1",
        command: str | None = None,
    ) -> dict[str, object]:
        return {
            "hook_event_name": "PreToolUse",
            "cwd": str(self.workspace),
            "session_id": session_id,
            "generation_id": generation_id,
            "tool_use_id": tool_use_id,
            "call_id": call_id,
            "client": "workbuddy",
            "version": "5.7.6",
            "tool_name": "Bash",
            "tool_input": {
                "command": command or probe.run_command(self.workspace),
                "keep": {"permission": "original"},
            },
        }

    def receipt_token(self, received: dict[str, object]) -> str:
        hook_output = received["hook_output"]
        self.assertIsInstance(hook_output, dict)
        updated_input = hook_output["updatedInput"]
        self.assertIsInstance(updated_input, dict)
        command = updated_input["command"]
        self.assertIsInstance(command, str)
        command_arguments = shlex.split(command)
        self.assertEqual(command_arguments[-2], "--probe-receipt")
        return command_arguments[-1]

    def run_updated_input_receipt(
        self,
        receipt: str,
        *,
        session_id: str = "workbuddy-session-1",
        request_id: str | None = None,
        environment_values: dict[str, str] | None = None,
        runtime_arguments: tuple[str, ...] | None = None,
    ) -> dict[str, object]:
        original_values = {name: os.environ.get(name) for name in probe.ALLOWED_ENVIRONMENT_NAMES}
        try:
            for name in probe.ALLOWED_ENVIRONMENT_NAMES:
                os.environ.pop(name, None)
            os.environ["CODEBUDDY_SESSION_ID"] = session_id
            if request_id is not None:
                os.environ["CONVERSATION_REQUEST_ID"] = request_id
            if environment_values is not None:
                os.environ.update(environment_values)
            os.chdir(self.workspace)
            return probe.run(
                self.workspace,
                probe_receipt=receipt,
                runtime_arguments=runtime_arguments,
            )
        finally:
            for name, value in original_values.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

    def run_codex_updated_input_receipt(
        self,
        receipt: str | None,
        *,
        thread_id: str = "codex-session-1",
        runtime_arguments: tuple[str, ...] | None = None,
    ) -> dict[str, object]:
        original_values = {name: os.environ.get(name) for name in probe.ALLOWED_ENVIRONMENT_NAMES}
        try:
            for name in probe.ALLOWED_ENVIRONMENT_NAMES:
                os.environ.pop(name, None)
            os.environ["CODEX_THREAD_ID"] = thread_id
            os.chdir(self.workspace)
            return probe.run(
                self.workspace,
                probe_receipt=receipt,
                runtime_arguments=runtime_arguments,
            )
        finally:
            for name, value in original_values.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

    def test_prepare_creates_only_private_fixture_files_and_documented_hook_shapes(self) -> None:
        prepared = self.prepare("codex")
        configuration = json.loads((self.workspace / ".codex" / "hooks.json").read_text())
        instructions = (self.workspace / "RUNNING.txt").read_text(encoding="utf-8")

        self.assertEqual(prepared["evidence_label"], "candidate")
        self.assertIn(prepared["run_command"], instructions)
        self.assertEqual(
            set(configuration["hooks"]),
            {"SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop", "SessionEnd"},
        )
        self.assertEqual(configuration["hooks"]["PreToolUse"][0]["matcher"], "^Bash$")
        self.assertEqual(configuration["hooks"]["PostToolUse"][0]["matcher"], "^Bash$")
        for event_name in ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"):
            self.assertNotIn("matcher", configuration["hooks"][event_name][0])
        self.assertNotIn("allow", json.dumps(configuration).lower())
        self.assertNotIn("permission", json.dumps(configuration).lower())
        self.assertNotIn("updatedinput", json.dumps(configuration).lower())

        for private_file in self.workspace.rglob("*"):
            if private_file.is_file():
                self.assertEqual(stat.S_IMODE(private_file.stat().st_mode), 0o600)

        workbuddy_workspace = self.root / "workbuddy-fixture"
        probe.prepare(workbuddy_workspace, "workbuddy")
        workbuddy_configuration = json.loads(
            (workbuddy_workspace / ".codebuddy" / "settings.json").read_text()
        )
        self.assertEqual(
            workbuddy_configuration["hooks"]["PreToolUse"][0]["matcher"],
            "^(Bash|exec_command)$",
        )

    def test_prepare_refuses_existing_content_without_overwriting_it(self) -> None:
        self.workspace.mkdir()
        sentinel = self.workspace / "keep.txt"
        sentinel.write_text("keep", encoding="utf-8")

        with self.assertRaisesRegex(probe.ProbeError, "non-empty"):
            probe.prepare(self.workspace, "codex")

        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")
        self.assertFalse((self.workspace / ".native_context_probe").exists())

    def test_old_fixture_without_a_transport_uses_default_context(self) -> None:
        self.prepare()
        path = probe.fixture_path(self.workspace)
        fixture = json.loads(path.read_text(encoding="utf-8"))
        del fixture["transport"]
        probe.write_private_json(path, fixture, replace=True)

        inspection = probe.inspect(self.workspace)

        self.assertEqual(inspection["transport"], "context")
        self.assertEqual(inspection["receipts"], [])

    def test_receive_rejects_cwd_outside_the_fixture(self) -> None:
        self.prepare()
        outside = self.root / "outside"
        outside.mkdir()
        payload = self.pre_tool_event()
        payload["cwd"] = str(outside)

        with self.assertRaisesRegex(probe.ProbeError, "outside"):
            probe.receive_payload(self.workspace, payload)

        self.assertEqual(probe.inspect(self.workspace)["observations"], [])

    def test_receive_bounds_and_redacts_payload_bodies(self) -> None:
        self.prepare()
        secret_prompt = "prompt-secret-123"
        secret_response = "response-secret-456"
        payload = self.pre_tool_event(command="do-not-store-this-command")
        payload["prompt"] = secret_prompt
        payload["tool_response"] = secret_response
        payload["message"] = "message-secret-789"

        received = probe.receive_payload(self.workspace, payload)
        inspection = probe.inspect(self.workspace)
        serialized = json.dumps(inspection, sort_keys=True)

        self.assertEqual(received["status"], "recorded")
        self.assertIn("command_digest", inspection["observations"][0])
        self.assertNotIn("do-not-store-this-command", serialized)
        self.assertNotIn(secret_prompt, serialized)
        self.assertNotIn(secret_response, serialized)
        self.assertNotIn("message-secret-789", serialized)

        with self.assertRaisesRegex(probe.ProbeError, "64 KiB"):
            probe.receive_json_bytes(self.workspace, b"{" + b"x" * probe.MAX_INPUT_BYTES)

    def test_fixture_source_cannot_claim_native_proof_or_match_a_run(self) -> None:
        self.prepare()
        event = self.pre_tool_event()
        received = probe.receive_payload(self.workspace, event, source="fixture")
        run = self.run_with_environment(
            {
                "CODEX_SESSION_ID": "session-1",
                "CODEX_TOOL_CALL_ID": "tool-1",
                "CODEX_THREAD_ID": "thread-1",
            }
        )

        self.assertEqual(received["source"], "fixture_candidate")
        self.assertEqual(received["evidence_label"], "candidate")
        self.assertEqual(run["comparison"]["status"], "unknown")
        self.assertEqual(run["comparison"]["reason"], "no_unique_pretooluse_match")

    def test_actual_codex_names_keep_thread_distinct_from_native_turn(self) -> None:
        self.prepare()
        probe.receive_payload(self.workspace, self.pre_tool_event())
        run = self.run_with_environment(
            {
                "CODEX_SESSION_ID": "session-1",
                "CODEX_TOOL_CALL_ID": "tool-1",
                "CODEX_THREAD_ID": "thread-1",
                "CODEX_VERSION": "0.147",
                "UNRELATED_SECRET": "must-not-be-captured",
            }
        )

        self.assertEqual(run["comparison"]["status"], "matched")
        self.assertEqual(run["comparison"]["evidence_label"], "candidate")
        self.assertEqual(
            set(run["child_environment"]),
            {
                "CODEX_SESSION_ID",
                "CODEX_THREAD_ID",
                "CODEX_TOOL_CALL_ID",
                "CODEX_VERSION",
            },
        )
        self.assertNotIn("UNRELATED_SECRET", json.dumps(probe.inspect(self.workspace)))

    def test_prefixed_workbuddy_environment_matches_native_pretooluse_candidate(self) -> None:
        self.prepare("workbuddy")
        payload = self.workbuddy_pre_tool_event()
        payload["conversation_message_id"] = "message-1"
        probe.receive_payload(self.workspace, payload)

        run = self.run_with_environment(
            {
                "CODEBUDDY_SESSION_ID": "workbuddy-session-1",
                "CODEBUDDY_TOOL_CALL_ID": "tool-use-1",
                "CODEBUDDY_CONVERSATION_REQUEST_ID": "generation-1",
                "CODEBUDDY_CONVERSATION_MESSAGE_ID": "message-1",
                "CODEBUDDY_ROOT_REQUEST_ID": "root-request-1",
            }
        )

        self.assertEqual(run["comparison"]["status"], "matched")
        self.assertEqual(run["comparison"]["reason"], "unique_pretooluse_match")
        self.assertEqual(
            set(run["child_environment"]),
            {
                "CODEBUDDY_SESSION_ID",
                "CODEBUDDY_TOOL_CALL_ID",
                "CODEBUDDY_CONVERSATION_REQUEST_ID",
                "CODEBUDDY_CONVERSATION_MESSAGE_ID",
                "CODEBUDDY_ROOT_REQUEST_ID",
            },
        )

    def test_broker_environment_routes_the_same_native_session_and_tool(self) -> None:
        self.prepare("workbuddy")
        probe.receive_payload(self.workspace, self.workbuddy_pre_tool_event())

        run = self.run_with_environment(
            {
                "CODEBUDDY_SANDBOX_BROKER_SESSION_ID": "workbuddy-session-1",
                "CODEBUDDY_SANDBOX_BROKER_TOOL_CALL_ID": "tool-use-1",
            }
        )

        self.assertEqual(run["comparison"]["status"], "matched")
        self.assertEqual(run["comparison"]["reason"], "unique_pretooluse_match")

    def test_root_request_is_not_treated_as_a_generation(self) -> None:
        self.prepare("workbuddy")
        probe.receive_payload(self.workspace, self.workbuddy_pre_tool_event())

        run = self.run_with_environment(
            {
                "CODEBUDDY_SESSION_ID": "workbuddy-session-1",
                "CODEBUDDY_TOOL_CALL_ID": "tool-use-1",
                "CODEBUDDY_ROOT_REQUEST_ID": "different-root-request",
            }
        )
        child = probe.child_environment("workbuddy", run["child_environment"])

        self.assertEqual(run["comparison"]["status"], "matched")
        self.assertIsNone(child["generation_id"])
        self.assertEqual(child["root_request_id"], "different-root-request")

    def test_workbuddy_environment_alias_conflicts_remain_unknown(self) -> None:
        self.prepare("workbuddy")
        payload = self.workbuddy_pre_tool_event()
        payload["conversation_message_id"] = "message-1"
        probe.receive_payload(self.workspace, payload)

        base = {
            "CODEBUDDY_SESSION_ID": "workbuddy-session-1",
            "CODEBUDDY_TOOL_CALL_ID": "tool-use-1",
            "CODEBUDDY_CONVERSATION_REQUEST_ID": "generation-1",
            "CODEBUDDY_CONVERSATION_MESSAGE_ID": "message-1",
        }
        conflicting_aliases = {
            "session": {"CODEBUDDY_SANDBOX_BROKER_SESSION_ID": "other-session"},
            "native_tool": {"TOOL_CALL_ID": "other-tool"},
            "broker_tool": {
                "CODEBUDDY_SANDBOX_BROKER_TOOL_CALL_ID": "tool-use-1",
                "SANDBOX_BROKER_TOOL_CALL_ID": "other-tool",
            },
            "native_and_broker_tool": {
                "CODEBUDDY_SANDBOX_BROKER_TOOL_CALL_ID": "other-tool",
            },
            "request": {"CONVERSATION_REQUEST_ID": "other-generation"},
            "message": {"CONVERSATION_MESSAGE_ID": "other-message"},
        }

        for namespace, aliases in conflicting_aliases.items():
            with self.subTest(namespace=namespace):
                run = self.run_with_environment({**base, **aliases})

                self.assertEqual(run["comparison"]["status"], "unknown")
                self.assertEqual(
                    run["comparison"]["reason"],
                    "conflicting_child_environment_alias",
                )

    def test_replays_conflicts_and_missing_ids_do_not_silently_match(self) -> None:
        self.prepare()
        first = self.pre_tool_event()
        same = self.pre_tool_event()
        conflicting = self.pre_tool_event(command="changed-command")
        missing_ids = self.pre_tool_event()
        del missing_ids["tool_call_id"]

        self.assertEqual(probe.receive_payload(self.workspace, first)["status"], "recorded")
        self.assertEqual(probe.receive_payload(self.workspace, same)["status"], "deduplicated")
        self.assertEqual(probe.receive_payload(self.workspace, conflicting)["status"], "conflict")
        self.assertEqual(probe.receive_payload(self.workspace, missing_ids)["status"], "recorded")

        run = self.run_with_environment(
            {
                "CODEX_SESSION_ID": "session-1",
                "CODEX_TOOL_CALL_ID": "tool-1",
                "CODEX_THREAD_ID": "thread-1",
            }
        )
        self.assertEqual(run["comparison"]["status"], "conflict")
        self.assertEqual(run["comparison"]["reason"], "conflicting_pretooluse_payloads")
        statuses = [
            item["identity_status"] for item in probe.inspect(self.workspace)["observations"]
        ]
        self.assertIn("unknown_id_arrival", statuses)

    def test_same_namespace_thread_disagreement_and_missing_environment_remain_visible(
        self,
    ) -> None:
        self.prepare()
        probe.receive_payload(self.workspace, self.pre_tool_event())

        mismatch = self.run_with_environment(
            {
                "CODEX_SESSION_ID": "session-1",
                "CODEX_TOOL_CALL_ID": "tool-1",
                "CODEX_THREAD_ID": "other-thread",
            }
        )
        self.assertEqual(mismatch["comparison"]["status"], "conflict")
        self.assertIn("thread_id", mismatch["comparison"]["conflicting_fields"])

        missing = self.run_with_environment({"CODEX_SESSION_ID": "session-1"})
        self.assertEqual(missing["comparison"]["status"], "unknown")
        self.assertEqual(missing["comparison"]["reason"], "missing_child_identity")

    def test_run_never_executes_payload_command_and_inspect_is_json_serializable(self) -> None:
        self.prepare()
        marker = self.workspace / "payload-command-ran"
        payload = self.pre_tool_event(command=f"touch {marker}")
        probe.receive_payload(self.workspace, payload)

        self.run_with_environment(
            {
                "CODEX_SESSION_ID": "session-1",
                "CODEX_TOOL_CALL_ID": "tool-1",
                "CODEX_THREAD_ID": "thread-1",
            }
        )
        inspection = probe.inspect(self.workspace)

        self.assertFalse(marker.exists())
        json.dumps(inspection, sort_keys=True)
        self.assertEqual(inspection["evidence_label"], "candidate")

    def test_run_cli_stdout_does_not_expose_allowlisted_identity_values(self) -> None:
        self.prepare()
        session_id = "session-sentinel-private"
        tool_call_id = "tool-sentinel-private"
        turn_id = "turn-sentinel-private"
        probe.receive_payload(
            self.workspace,
            self.pre_tool_event(
                session_id=session_id,
                tool_call_id=tool_call_id,
                turn_id=turn_id,
            ),
        )
        environment = {
            **os.environ,
            "CODEX_SESSION_ID": session_id,
            "CODEX_TOOL_CALL_ID": tool_call_id,
            "CODEX_THREAD_ID": "thread-1",
        }

        completed = subprocess.run(
            [
                sys.executable,
                str(PROBE_PATH),
                "run",
                "--workspace",
                str(self.workspace),
            ],
            cwd=self.workspace,
            check=True,
            capture_output=True,
            env=environment,
            text=True,
        )
        public_response = json.loads(completed.stdout)
        private_report = json.loads(Path(public_response["report_path"]).read_text())

        self.assertEqual(public_response["status"], "matched")
        self.assertEqual(public_response["reason"], "unique_pretooluse_match")
        self.assertNotIn(session_id, completed.stdout)
        self.assertNotIn(tool_call_id, completed.stdout)
        self.assertNotIn(turn_id, completed.stdout)
        self.assertEqual(private_report["child_environment"]["CODEX_SESSION_ID"], session_id)

    def test_workbuddy_run_cli_hides_prefixed_identity_values(self) -> None:
        self.prepare("workbuddy")
        session_id = "workbuddy-session-sentinel-private"
        tool_call_id = "workbuddy-tool-sentinel-private"
        generation_id = "workbuddy-generation-sentinel-private"
        message_id = "workbuddy-message-sentinel-private"
        root_request_id = "workbuddy-root-sentinel-private"
        payload = self.workbuddy_pre_tool_event(
            session_id=session_id,
            tool_use_id=tool_call_id,
            generation_id=generation_id,
        )
        payload["conversation_message_id"] = message_id
        probe.receive_payload(self.workspace, payload)
        environment = {
            **os.environ,
            "CODEBUDDY_SESSION_ID": session_id,
            "CODEBUDDY_TOOL_CALL_ID": tool_call_id,
            "CODEBUDDY_CONVERSATION_REQUEST_ID": generation_id,
            "CODEBUDDY_CONVERSATION_MESSAGE_ID": message_id,
            "CODEBUDDY_ROOT_REQUEST_ID": root_request_id,
        }

        completed = subprocess.run(
            [
                sys.executable,
                str(PROBE_PATH),
                "run",
                "--workspace",
                str(self.workspace),
            ],
            cwd=self.workspace,
            check=True,
            capture_output=True,
            env=environment,
            text=True,
        )
        public_response = json.loads(completed.stdout)
        private_report = json.loads(Path(public_response["report_path"]).read_text())

        self.assertEqual(public_response["status"], "matched")
        for private_value in (
            session_id,
            tool_call_id,
            generation_id,
            message_id,
            root_request_id,
        ):
            self.assertNotIn(private_value, completed.stdout)
        self.assertEqual(
            private_report["child_environment"]["CODEBUDDY_ROOT_REQUEST_ID"], root_request_id
        )

    def test_concurrent_hook_receives_keep_every_arrival(self) -> None:
        self.prepare()
        original_load_observations = probe.load_observations

        def delayed_load(workspace: Path) -> list[dict[str, object]]:
            observations = original_load_observations(workspace)
            time.sleep(0.01)
            return observations

        def receive(index: int) -> dict[str, object]:
            return probe.receive_payload(
                self.workspace,
                self.pre_tool_event(tool_call_id=f"tool-{index}"),
            )

        with (
            mock.patch.object(
                probe,
                "load_observations",
                side_effect=delayed_load,
            ),
            ThreadPoolExecutor(max_workers=8) as executor,
        ):
            received = list(executor.map(receive, range(8)))

        self.assertEqual([item["status"] for item in received], ["recorded"] * 8)
        observations = probe.inspect(self.workspace)["observations"]
        self.assertEqual(len(observations), 8)
        self.assertEqual(
            {item["tool_call_id"] for item in observations}, {f"tool-{index}" for index in range(8)}
        )

    def test_updated_input_transport_rewrites_only_the_fixed_probe_command(self) -> None:
        self.prepare_updated_input_fixture()
        payload = self.workbuddy_pre_tool_event()
        original_input = payload["tool_input"]
        received = probe.receive_payload(self.workspace, payload)
        token = self.receipt_token(received)
        hook_output = received["hook_output"]

        self.assertEqual(received["transport_status"], "issued")
        self.assertEqual(set(hook_output), {"hookEventName", "updatedInput"})
        self.assertEqual(hook_output["hookEventName"], "PreToolUse")
        self.assertEqual(hook_output["updatedInput"]["keep"], original_input["keep"])
        self.assertEqual(
            shlex.split(hook_output["updatedInput"]["command"])[:-2],
            shlex.split(original_input["command"]),
        )
        self.assertNotIn("permissionDecision", hook_output)
        self.assertNotIn("decision", hook_output)
        self.assertNotIn("continue", hook_output)
        self.assertNotIn("updatedPermissions", hook_output)
        self.assertEqual(len(probe.inspect(self.workspace)["receipts"]), 1)

        run = self.run_updated_input_receipt(token)
        self.assertEqual(run["comparison"]["status"], "matched")
        self.assertEqual(run["comparison"]["reason"], "updated_input_receipt_consumed")

    def test_codex_updated_input_transports_an_exact_pre_receipt_to_the_fixed_run(self) -> None:
        self.prepare_codex_updated_input_fixture()
        original_command = probe.run_command(self.workspace)
        payload = self.pre_tool_event(
            session_id="codex-session-1",
            tool_call_id="codex-tool-1",
            turn_id="codex-turn-1",
            command=original_command,
        )
        payload["tool_input"]["keep"] = {"original": "preserved"}

        received = probe.receive_payload(self.workspace, payload)
        token = self.receipt_token(received)
        hook_output = received["hook_output"]

        self.assertEqual(received["transport_status"], "issued")
        self.assertEqual(hook_output["hookEventName"], "PreToolUse")
        self.assertEqual(hook_output["updatedInput"]["keep"], payload["tool_input"]["keep"])
        self.assertEqual(
            shlex.split(hook_output["updatedInput"]["command"])[:-2],
            shlex.split(original_command),
        )
        self.assertNotIn("permissionDecision", hook_output)
        self.assertNotIn("updatedPermissions", hook_output)
        receipt = probe.load_receipts(self.workspace)[0]
        self.assertEqual(
            receipt["original_command_digest"],
            probe.exact_command_digest(original_command),
        )
        self.assertEqual(
            receipt["updated_command_digest"],
            probe.exact_command_digest(hook_output["updatedInput"]["command"]),
        )
        self.assertNotEqual(receipt["original_command_digest"], probe.digest(original_command))
        self.assertNotEqual(
            receipt["updated_command_digest"],
            probe.digest(hook_output["updatedInput"]["command"]),
        )

        run = self.run_codex_updated_input_receipt(token)

        self.assertEqual(run["comparison"]["status"], "matched")
        self.assertEqual(run["comparison"]["reason"], "updated_input_receipt_consumed")

    def test_codex_updated_input_receive_cli_returns_only_the_hook_specific_schema(self) -> None:
        self.prepare_codex_updated_input_fixture()
        payload = self.pre_tool_event(
            session_id="codex-session-sentinel",
            tool_call_id="codex-tool-sentinel",
            turn_id="codex-turn-sentinel",
            command=probe.run_command(self.workspace),
        )
        completed = subprocess.run(
            [
                sys.executable,
                str(PROBE_PATH),
                "receive",
                "--workspace",
                str(self.workspace),
            ],
            cwd=self.workspace,
            input=json.dumps(payload),
            capture_output=True,
            check=True,
            text=True,
        )
        response = json.loads(completed.stdout)

        self.assertEqual(set(response), {"hookSpecificOutput"})
        hook_output = response["hookSpecificOutput"]
        self.assertEqual(set(hook_output), {"hookEventName", "updatedInput"})
        self.assertEqual(hook_output["hookEventName"], "PreToolUse")
        self.assertNotIn("permissionDecision", hook_output)
        self.assertNotIn("updatedPermissions", hook_output)
        for private_value in (
            payload["session_id"],
            payload["tool_call_id"],
            payload["turn_id"],
        ):
            self.assertNotIn(private_value, completed.stdout)

    def test_codex_updated_input_rejects_untrusted_non_bash_and_non_fixed_pre_events(self) -> None:
        self.prepare_codex_updated_input_fixture()
        fixed_command = probe.run_command(self.workspace)
        fixture_source = probe.receive_payload(
            self.workspace,
            self.pre_tool_event(command=fixed_command),
            source="fixture",
        )
        non_bash = self.pre_tool_event(command=fixed_command, tool_call_id="codex-tool-2")
        non_bash["tool_name"] = "Read"
        repeated_receipt = self.pre_tool_event(
            command=f"{fixed_command} --probe-receipt supplied-by-input",
            tool_call_id="codex-tool-3",
        )
        composed = self.pre_tool_event(
            command=f"printf harmless && {fixed_command}",
            tool_call_id="codex-tool-4",
        )

        responses = (
            fixture_source,
            probe.receive_payload(self.workspace, non_bash),
            probe.receive_payload(self.workspace, repeated_receipt),
            probe.receive_payload(self.workspace, composed),
        )

        self.assertEqual(fixture_source["transport_status"], "fixture_source")
        for response in responses:
            self.assertNotIn("hook_output", response)
        self.assertEqual(responses[1]["transport_status"], "unsupported_fixed_command")
        self.assertEqual(responses[2]["transport_status"], "unsupported_fixed_command")
        self.assertEqual(responses[3]["transport_status"], "unsupported_fixed_command")
        self.assertEqual(probe.inspect(self.workspace)["receipts"], [])

    def test_codex_updated_input_replay_conflict_and_distinct_tools_preserve_one_use(self) -> None:
        self.prepare_codex_updated_input_fixture()
        first_payload = self.pre_tool_event(
            session_id="codex-session-1",
            tool_call_id="codex-tool-1",
            turn_id="codex-turn-1",
            command=probe.run_command(self.workspace),
        )
        first = probe.receive_payload(self.workspace, first_payload)
        replay = probe.receive_payload(self.workspace, first_payload)
        first_receipt = probe.load_receipts(self.workspace)[0]
        first_digests = (
            first_receipt["original_command_digest"],
            first_receipt["updated_command_digest"],
        )
        second_tool = probe.receive_payload(
            self.workspace,
            self.pre_tool_event(
                session_id="codex-session-1",
                tool_call_id="codex-tool-2",
                turn_id="codex-turn-1",
                command=probe.run_command(self.workspace),
            ),
        )
        conflict_payload = dict(first_payload)
        conflict_input = dict(first_payload["tool_input"])
        conflict_input["description"] = "conflicting native callback"
        conflict_payload["tool_input"] = conflict_input
        conflict = probe.receive_payload(self.workspace, conflict_payload)
        first_token = self.receipt_token(first)
        second_token = self.receipt_token(second_tool)

        self.assertEqual(first_token, self.receipt_token(replay))
        self.assertEqual(
            (
                probe.load_receipts(self.workspace)[0]["original_command_digest"],
                probe.load_receipts(self.workspace)[0]["updated_command_digest"],
            ),
            first_digests,
        )
        self.assertNotEqual(first_token, second_token)
        self.assertEqual(replay["transport_status"], "reused")
        self.assertEqual(conflict["transport_status"], "conflict")
        self.assertEqual(
            self.run_codex_updated_input_receipt(first_token)["comparison"]["reason"],
            "probe_receipt_conflicted",
        )
        self.assertEqual(
            self.run_codex_updated_input_receipt(second_token)["comparison"]["status"],
            "matched",
        )

    def test_codex_updated_input_rejects_missing_mismatched_and_spent_tokens(self) -> None:
        self.prepare_codex_updated_input_fixture()
        received = probe.receive_payload(
            self.workspace,
            self.pre_tool_event(
                session_id="codex-session-1",
                tool_call_id="codex-tool-1",
                turn_id="codex-turn-1",
                command=probe.run_command(self.workspace),
            ),
        )
        token = self.receipt_token(received)

        missing = self.run_codex_updated_input_receipt(None)
        mismatched = self.run_codex_updated_input_receipt(token, thread_id="other-thread")
        consumed = self.run_codex_updated_input_receipt(token)
        replayed = self.run_codex_updated_input_receipt(token)
        expired_received = probe.receive_payload(
            self.workspace,
            self.pre_tool_event(
                session_id="codex-session-1",
                tool_call_id="codex-tool-2",
                turn_id="codex-turn-1",
                command=probe.run_command(self.workspace),
            ),
        )
        expired_token = self.receipt_token(expired_received)
        receipts = probe.load_receipts(self.workspace)
        receipts[-1]["expires_at"] = "2000-01-01T00:00:00Z"
        probe.write_private_json(probe.receipts_path(self.workspace), receipts, replace=True)
        expired = self.run_codex_updated_input_receipt(expired_token)

        self.assertEqual(missing["comparison"]["reason"], "missing_probe_receipt")
        self.assertEqual(mismatched["comparison"]["reason"], "probe_receipt_session_mismatch")
        self.assertEqual(consumed["comparison"]["status"], "matched")
        self.assertEqual(replayed["comparison"]["reason"], "probe_receipt_spent")
        self.assertEqual(expired["comparison"]["reason"], "probe_receipt_expired")

    def test_updated_input_accepts_only_a_same_fixture_cd_prefix(self) -> None:
        self.prepare_updated_input_fixture()
        fixed_command = probe.run_command(self.workspace)
        cd_command = f"cd {shlex.quote(str(self.workspace.resolve()))} && {fixed_command}"
        received = probe.receive_payload(
            self.workspace,
            self.workbuddy_pre_tool_event(command=cd_command),
        )
        token = self.receipt_token(received)
        updated_command = received["hook_output"]["updatedInput"]["command"]

        self.assertEqual(received["transport_status"], "issued")
        self.assertEqual(updated_command, f"{cd_command} --probe-receipt {token}")

    def test_updated_input_receipt_requires_the_native_session_and_generation_namespace(
        self,
    ) -> None:
        self.prepare_updated_input_fixture()
        received = probe.receive_payload(self.workspace, self.workbuddy_pre_tool_event())
        token = self.receipt_token(received)

        session_mismatch = self.run_updated_input_receipt(token, session_id="other-session")
        generation_mismatch = self.run_updated_input_receipt(token, request_id="other-generation")
        matched = self.run_updated_input_receipt(token, request_id="generation-1")

        self.assertEqual(session_mismatch["comparison"]["reason"], "probe_receipt_session_mismatch")
        self.assertEqual(
            generation_mismatch["comparison"]["reason"], "probe_receipt_generation_mismatch"
        )
        self.assertEqual(matched["comparison"]["status"], "matched")

    def test_updated_input_receipt_accepts_consistent_prefixed_context(self) -> None:
        self.prepare_updated_input_fixture()
        received = probe.receive_payload(self.workspace, self.workbuddy_pre_tool_event())
        token = self.receipt_token(received)

        run = self.run_updated_input_receipt(
            token,
            environment_values={
                "CODEBUDDY_SANDBOX_BROKER_SESSION_ID": "workbuddy-session-1",
                "CODEBUDDY_TOOL_CALL_ID": "tool-use-1",
                "CODEBUDDY_SANDBOX_BROKER_TOOL_CALL_ID": "tool-use-1",
                "CODEBUDDY_CONVERSATION_REQUEST_ID": "generation-1",
            },
        )

        self.assertEqual(run["comparison"]["status"], "matched")
        self.assertEqual(run["comparison"]["reason"], "updated_input_receipt_consumed")

    def test_updated_input_receipt_rejects_context_conflicts_without_consuming(self) -> None:
        self.prepare_updated_input_fixture()
        cases = (
            (
                "request_alias",
                {"CODEBUDDY_CONVERSATION_REQUEST_ID": "other-generation"},
                "probe_receipt_environment_conflict",
            ),
            (
                "broker_session",
                {"CODEBUDDY_SANDBOX_BROKER_SESSION_ID": "other-session"},
                "probe_receipt_environment_conflict",
            ),
            (
                "tool_alias",
                {
                    "CODEBUDDY_TOOL_CALL_ID": "tool-use-2",
                    "TOOL_CALL_ID": "other-tool",
                },
                "probe_receipt_environment_conflict",
            ),
            (
                "current_tool",
                {"CODEBUDDY_TOOL_CALL_ID": "other-tool"},
                "probe_receipt_tool_mismatch",
            ),
        )

        for index, (name, environment_values, reason) in enumerate(cases, start=1):
            with self.subTest(name=name):
                generation_id = f"generation-{index}"
                tool_use_id = f"tool-use-{index}"
                received = probe.receive_payload(
                    self.workspace,
                    self.workbuddy_pre_tool_event(
                        generation_id=generation_id,
                        tool_use_id=tool_use_id,
                        call_id=f"call-{index}",
                    ),
                )
                token = self.receipt_token(received)

                run = self.run_updated_input_receipt(
                    token,
                    request_id=generation_id,
                    environment_values=environment_values,
                )
                receipt = next(
                    item
                    for item in probe.load_receipts(self.workspace)
                    if item.get("receipt") == token
                )

                self.assertEqual(run["comparison"]["status"], "unknown")
                self.assertEqual(run["comparison"]["reason"], reason)
                self.assertIsNone(receipt["consumed_at"])

    def test_updated_input_receipt_rejects_prefixed_generation_mismatch_without_consuming(
        self,
    ) -> None:
        self.prepare_updated_input_fixture()
        received = probe.receive_payload(self.workspace, self.workbuddy_pre_tool_event())
        token = self.receipt_token(received)

        mismatch = self.run_updated_input_receipt(
            token,
            environment_values={"CODEBUDDY_CONVERSATION_REQUEST_ID": "other-generation"},
        )
        receipt = probe.load_receipts(self.workspace)[0]

        self.assertEqual(mismatch["comparison"]["status"], "unknown")
        self.assertEqual(mismatch["comparison"]["reason"], "probe_receipt_generation_mismatch")
        self.assertIsNone(receipt["consumed_at"])

        matched = self.run_updated_input_receipt(token, request_id="generation-1")

        self.assertEqual(matched["comparison"]["status"], "matched")
        self.assertIsNotNone(probe.load_receipts(self.workspace)[0]["consumed_at"])

    def test_updated_input_receive_cli_returns_only_hook_json(self) -> None:
        self.prepare_updated_input_fixture()
        payload = self.workbuddy_pre_tool_event(
            session_id="session-sentinel-private",
            generation_id="generation-sentinel-private",
            tool_use_id="tool-sentinel-private",
            call_id="call-sentinel-private",
        )
        completed = subprocess.run(
            [
                sys.executable,
                str(PROBE_PATH),
                "receive",
                "--workspace",
                str(self.workspace),
            ],
            cwd=self.workspace,
            input=json.dumps(payload),
            capture_output=True,
            check=True,
            text=True,
        )
        response = json.loads(completed.stdout)

        self.assertEqual(set(response), {"hookSpecificOutput"})
        hook_output = response["hookSpecificOutput"]
        self.assertEqual(set(hook_output), {"hookEventName", "updatedInput"})
        self.assertEqual(hook_output["hookEventName"], "PreToolUse")
        self.assertNotIn("permissionDecision", hook_output)
        self.assertNotIn("decision", hook_output)
        self.assertNotIn("continue", hook_output)
        self.assertNotIn("updatedPermissions", hook_output)
        for private_value in (
            payload["session_id"],
            payload["generation_id"],
            payload["tool_use_id"],
            payload["call_id"],
        ):
            self.assertNotIn(private_value, completed.stdout)

    def test_updated_input_run_cli_hides_native_identity_values(self) -> None:
        self.prepare_updated_input_fixture()
        payload = self.workbuddy_pre_tool_event(
            session_id="session-sentinel-private",
            generation_id="generation-sentinel-private",
            tool_use_id="tool-sentinel-private",
            call_id="call-sentinel-private",
        )
        received = probe.receive_payload(self.workspace, payload)
        self.receipt_token(received)
        command = received["hook_output"]["updatedInput"]["command"]
        self.assertIsInstance(command, str)
        environment = {
            **os.environ,
            "CODEBUDDY_SESSION_ID": payload["session_id"],
            "CONVERSATION_REQUEST_ID": payload["generation_id"],
        }

        completed = subprocess.run(
            shlex.split(command),
            cwd=self.workspace,
            env=environment,
            capture_output=True,
            check=True,
            text=True,
        )
        response = json.loads(completed.stdout)

        self.assertEqual(response["status"], "matched")
        for private_value in (
            payload["session_id"],
            payload["generation_id"],
            payload["tool_use_id"],
            payload["call_id"],
        ):
            self.assertNotIn(private_value, completed.stdout)

    def test_updated_input_replay_reuses_a_token_and_conflicting_callback_is_rejected(self) -> None:
        self.prepare_updated_input_fixture()
        payload = self.workbuddy_pre_tool_event()
        first = probe.receive_payload(self.workspace, payload)
        replay = probe.receive_payload(self.workspace, payload)
        conflicting = self.workbuddy_pre_tool_event(command="printf changed-command")
        conflict = probe.receive_payload(self.workspace, conflicting)

        self.assertEqual(self.receipt_token(first), self.receipt_token(replay))
        self.assertEqual(replay["transport_status"], "reused")
        self.assertEqual(conflict["status"], "conflict")
        self.assertEqual(conflict["transport_status"], "conflict")
        self.assertNotIn("hook_output", conflict)
        self.assertEqual(len(probe.inspect(self.workspace)["receipts"]), 1)

    def test_updated_input_conflict_invalidates_a_receipt_and_taints_its_identity(self) -> None:
        self.prepare_updated_input_fixture()
        payload = self.workbuddy_pre_tool_event()
        first = probe.receive_payload(self.workspace, payload)
        token = self.receipt_token(first)
        conflicting = self.workbuddy_pre_tool_event()
        conflicting_input = dict(conflicting["tool_input"])
        conflicting_input["description"] = "different callback payload"
        conflicting["tool_input"] = conflicting_input

        conflict = probe.receive_payload(self.workspace, conflicting)
        after_conflict = self.run_updated_input_receipt(token)
        replay = probe.receive_payload(self.workspace, payload)
        after_replay = self.run_updated_input_receipt(token)
        inspection = probe.inspect(self.workspace)

        self.assertEqual(conflict["status"], "conflict")
        self.assertEqual(conflict["transport_status"], "conflict")
        self.assertEqual(after_conflict["comparison"]["reason"], "probe_receipt_conflicted")
        self.assertEqual(replay["status"], "deduplicated")
        self.assertEqual(replay["transport_status"], "conflict")
        self.assertNotIn("hook_output", replay)
        self.assertEqual(after_replay["comparison"]["reason"], "probe_receipt_conflicted")
        self.assertEqual(len(inspection["receipts"]), 1)
        self.assertIsNotNone(inspection["receipts"][0]["invalidated_at"])

    def test_normal_post_tool_use_does_not_invalidate_a_consumed_pretool_receipt(self) -> None:
        self.prepare_updated_input_fixture()
        payload = self.workbuddy_pre_tool_event()
        first = probe.receive_payload(self.workspace, payload)
        token = self.receipt_token(first)
        consumed = self.run_updated_input_receipt(token)
        post_tool_use = dict(payload)
        post_tool_use["hook_event_name"] = "PostToolUse"
        post_tool_use["tool_response"] = {"exit_code": 0}

        post = probe.receive_payload(self.workspace, post_tool_use)
        replay = probe.receive_payload(self.workspace, post_tool_use)
        inspection = probe.inspect(self.workspace)

        self.assertEqual(consumed["comparison"]["status"], "matched")
        self.assertEqual(post["status"], "recorded")
        self.assertEqual(post["transport_status"], "not_pre_tool_use")
        self.assertEqual(replay["status"], "deduplicated")
        self.assertEqual(replay["transport_status"], "not_pre_tool_use")
        self.assertNotIn("hook_output", replay)
        self.assertIsNone(inspection["receipts"][0].get("invalidated_at"))

    def test_partial_conflicting_callback_taints_only_its_stable_identity(self) -> None:
        self.prepare_updated_input_fixture()
        payload = self.workbuddy_pre_tool_event()
        first = probe.receive_payload(self.workspace, payload)
        token = self.receipt_token(first)
        malformed = dict(payload)
        malformed.pop("generation_id")

        conflict = probe.receive_payload(self.workspace, malformed)
        after_conflict = self.run_updated_input_receipt(token)
        replay = probe.receive_payload(self.workspace, payload)
        after_replay = self.run_updated_input_receipt(token)
        unrelated = probe.receive_payload(
            self.workspace,
            self.workbuddy_pre_tool_event(
                session_id="unrelated-session",
                generation_id="unrelated-generation",
                tool_use_id="unrelated-tool",
                call_id="unrelated-call",
            ),
        )
        unrelated_token = self.receipt_token(unrelated)
        unrelated_run = self.run_updated_input_receipt(
            unrelated_token,
            session_id="unrelated-session",
            request_id="unrelated-generation",
        )
        inspection = probe.inspect(self.workspace)

        self.assertEqual(conflict["status"], "conflict")
        self.assertEqual(conflict["transport_status"], "missing_native_identity")
        self.assertEqual(after_conflict["comparison"]["reason"], "probe_receipt_conflicted")
        self.assertEqual(replay["transport_status"], "conflict")
        self.assertNotIn("hook_output", replay)
        self.assertEqual(after_replay["comparison"]["reason"], "probe_receipt_conflicted")
        self.assertEqual(unrelated["transport_status"], "issued")
        self.assertEqual(unrelated_run["comparison"]["status"], "matched")
        self.assertEqual(len(inspection["receipts"]), 2)
        self.assertIsNotNone(inspection["receipts"][0]["invalidated_at"])
        self.assertIsNone(inspection["receipts"][1].get("invalidated_at"))

    def test_tool_call_alias_conflict_taints_the_native_tool_use_receipt(self) -> None:
        self.prepare_updated_input_fixture()
        payload = self.workbuddy_pre_tool_event()
        first = probe.receive_payload(self.workspace, payload)
        token = self.receipt_token(first)
        conflicting = dict(payload)
        conflicting["tool_call_id"] = "different-alias"

        conflict = probe.receive_payload(self.workspace, conflicting)
        after_conflict = self.run_updated_input_receipt(token)
        replay = probe.receive_payload(self.workspace, payload)
        after_replay = self.run_updated_input_receipt(token)
        inspection = probe.inspect(self.workspace)

        self.assertEqual(conflict["status"], "recorded")
        self.assertEqual(conflict["transport_status"], "conflict")
        self.assertEqual(after_conflict["comparison"]["reason"], "probe_receipt_conflicted")
        self.assertEqual(replay["transport_status"], "conflict")
        self.assertNotIn("hook_output", replay)
        self.assertEqual(after_replay["comparison"]["reason"], "probe_receipt_conflicted")
        self.assertEqual(len(inspection["receipts"]), 1)
        self.assertIsNotNone(inspection["receipts"][0]["invalidated_at"])

    def test_partial_tool_call_alias_conflict_taints_the_native_tool_use_receipt(self) -> None:
        self.prepare_updated_input_fixture()
        payload = self.workbuddy_pre_tool_event()
        first = probe.receive_payload(self.workspace, payload)
        token = self.receipt_token(first)
        conflicting = dict(payload)
        conflicting.pop("generation_id")
        conflicting["tool_call_id"] = "different-alias"

        conflict = probe.receive_payload(self.workspace, conflicting)
        after_conflict = self.run_updated_input_receipt(token)
        replay = probe.receive_payload(self.workspace, payload)
        after_replay = self.run_updated_input_receipt(token)

        self.assertEqual(conflict["status"], "recorded")
        self.assertEqual(conflict["transport_status"], "missing_native_identity")
        self.assertEqual(after_conflict["comparison"]["reason"], "probe_receipt_conflicted")
        self.assertEqual(replay["transport_status"], "conflict")
        self.assertNotIn("hook_output", replay)
        self.assertEqual(after_replay["comparison"]["reason"], "probe_receipt_conflicted")

    def test_updated_input_rejects_non_fixed_commands_missing_native_ids_and_fixture_source(
        self,
    ) -> None:
        self.prepare_updated_input_fixture()
        fixed_command = probe.run_command(self.workspace)
        invalid_commands = (
            f"echo unrelated && {fixed_command}",
            f"cd {self.workspace} && {fixed_command} --unknown",
            f"cd {self.root.resolve()} && {fixed_command}",
            f"cd {self.workspace.resolve()} && {fixed_command} && true",
            f"cd $(pwd) && {fixed_command}",
            f"$(printf unexpected) {fixed_command}",
            f"/bin/sh outside.py run --workspace {self.workspace}",
            f"{fixed_command}\ntrue",
            f"{fixed_command}\rtrue",
            f"{fixed_command}\x00true",
        )
        for index, command in enumerate(invalid_commands):
            received = probe.receive_payload(
                self.workspace,
                self.workbuddy_pre_tool_event(
                    command=command,
                    tool_use_id=f"invalid-tool-{index}",
                    call_id=f"invalid-call-{index}",
                ),
            )
            self.assertEqual(received["transport_status"], "unsupported_fixed_command")
            self.assertNotIn("hook_output", received)

        missing_ids = self.workbuddy_pre_tool_event()
        del missing_ids["generation_id"]
        missing = probe.receive_payload(self.workspace, missing_ids)
        empty_ids = self.workbuddy_pre_tool_event(tool_use_id="", call_id="")
        empty = probe.receive_payload(self.workspace, empty_ids)
        fixture_source = probe.receive_payload(
            self.workspace,
            self.workbuddy_pre_tool_event(tool_use_id="fixture-tool", call_id="fixture-call"),
            source="fixture",
        )

        self.assertEqual(missing["transport_status"], "missing_native_identity")
        self.assertEqual(empty["transport_status"], "missing_native_identity")
        self.assertEqual(fixture_source["transport_status"], "fixture_source")
        self.assertNotIn("hook_output", missing)
        self.assertNotIn("hook_output", fixture_source)

    def test_updated_input_receipt_rejects_spent_expired_and_drifted_runtime_arguments(
        self,
    ) -> None:
        self.prepare_updated_input_fixture()
        first = probe.receive_payload(self.workspace, self.workbuddy_pre_tool_event())
        token = self.receipt_token(first)
        drifted = self.run_updated_input_receipt(
            token,
            runtime_arguments=(
                "run",
                "--workspace",
                str(self.workspace),
                "--probe-receipt",
                token,
                "--unexpected",
            ),
        )
        consumed = self.run_updated_input_receipt(token)
        spent = self.run_updated_input_receipt(token)

        self.assertEqual(drifted["comparison"]["status"], "unknown")
        self.assertEqual(drifted["comparison"]["reason"], "probe_receipt_arg_drift")
        self.assertEqual(consumed["comparison"]["status"], "matched")
        self.assertEqual(spent["comparison"]["reason"], "probe_receipt_spent")

        second = probe.receive_payload(
            self.workspace,
            self.workbuddy_pre_tool_event(tool_use_id="tool-use-2", call_id="call-2"),
        )
        receipts = probe.load_receipts(self.workspace)
        receipts[-1]["expires_at"] = "2000-01-01T00:00:00Z"
        probe.write_private_json(probe.receipts_path(self.workspace), receipts, replace=True)
        expired = self.run_updated_input_receipt(self.receipt_token(second))

        self.assertEqual(expired["comparison"]["status"], "unknown")
        self.assertEqual(expired["comparison"]["reason"], "probe_receipt_expired")


if __name__ == "__main__":
    unittest.main()
