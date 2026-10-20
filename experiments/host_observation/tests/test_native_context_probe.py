from __future__ import annotations

import importlib.util
import json
import os
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
        statuses = [item["identity_status"] for item in probe.inspect(self.workspace)["observations"]]
        self.assertIn("unknown_id_arrival", statuses)

    def test_same_namespace_thread_disagreement_and_missing_environment_remain_visible(self) -> None:
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

        with mock.patch.object(probe, "load_observations", side_effect=delayed_load):
            with ThreadPoolExecutor(max_workers=8) as executor:
                received = list(executor.map(receive, range(8)))

        self.assertEqual([item["status"] for item in received], ["recorded"] * 8)
        observations = probe.inspect(self.workspace)["observations"]
        self.assertEqual(len(observations), 8)
        self.assertEqual({item["tool_call_id"] for item in observations}, {f"tool-{index}" for index in range(8)})


if __name__ == "__main__":
    unittest.main()
