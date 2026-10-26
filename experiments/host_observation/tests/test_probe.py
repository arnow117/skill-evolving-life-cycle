from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock


PROBE_PATH = Path(__file__).resolve().parents[1] / "probe.py"
MODULE_SPEC = importlib.util.spec_from_file_location("host_observation_probe", PROBE_PATH)
assert MODULE_SPEC is not None
assert MODULE_SPEC.loader is not None
probe = importlib.util.module_from_spec(MODULE_SPEC)
sys.modules[MODULE_SPEC.name] = probe
MODULE_SPEC.loader.exec_module(probe)


class ProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.workspace = self.root / "fixture"
        self.original_directory = Path.cwd()

    def tearDown(self) -> None:
        os.chdir(self.original_directory)
        self.temporary_directory.cleanup()

    def prepare(self, host: str = "codex") -> probe.Fixture:
        probe.prepare(self.workspace, host)
        return probe.read_fixture(self.workspace)

    def native_event(
        self,
        fixture: probe.Fixture,
        *,
        command: str | None = None,
        cwd: Path | None = None,
        tool_use_id: str = "native-call-1",
    ) -> dict[str, object]:
        return {
            "cwd": str(cwd or fixture.workspace),
            "hook_event_name": "PreToolUse",
            "session_id": "session-1",
            "tool_name": "Bash",
            "tool_use_id": tool_use_id,
            "tool_input": {"command": command or probe.command_text(fixture.execute_argv)},
        }

    def receive_native(self, fixture: probe.Fixture, **overrides: object) -> dict[str, object]:
        payload = self.native_event(fixture, **overrides)
        return probe.receive_event(self.workspace, fixture.host, "native", payload)

    def run_execute(self, fixture: probe.Fixture) -> dict[str, object]:
        os.chdir(fixture.workspace)
        return probe.execute(self.workspace, fixture.nonce)

    def database(self) -> sqlite3.Connection:
        database = probe.open_database(self.workspace)
        self.addCleanup(database.close)
        return database

    def test_prepare_creates_isolated_skill_and_host_configurations(self) -> None:
        fixture = self.prepare("codex")
        codex_config = self.workspace / ".codex" / "hooks.json"
        skill = self.workspace / ".claude" / "skills" / probe.SKILL_NAME / "SKILL.md"
        codex_link = self.workspace / ".agents" / "skills" / probe.SKILL_NAME

        self.assertTrue(codex_config.is_file())
        self.assertTrue(skill.is_file())
        self.assertTrue(codex_link.is_symlink())
        self.assertEqual(os.readlink(codex_link), f"../../.claude/skills/{probe.SKILL_NAME}")
        self.assertIn(probe.command_text(fixture.execute_argv), skill.read_text(encoding="utf-8"))
        configuration = json.loads(codex_config.read_text(encoding="utf-8"))
        self.assertEqual(configuration["hooks"]["PreToolUse"][0]["matcher"], "^Bash$")

        workbuddy_workspace = self.root / "workbuddy-fixture"
        probe.prepare(workbuddy_workspace, "workbuddy")
        workbuddy = probe.read_fixture(workbuddy_workspace)
        workbuddy_config = workbuddy_workspace / ".codebuddy" / "settings.json"
        workbuddy_link = workbuddy_workspace / ".codebuddy" / "skills" / probe.SKILL_NAME
        self.assertTrue(workbuddy_config.is_file())
        self.assertFalse((workbuddy_workspace / ".agents").exists())
        self.assertTrue(workbuddy_link.is_symlink())
        self.assertEqual(
            os.readlink(workbuddy_link),
            f"../../.claude/skills/{probe.SKILL_NAME}",
        )
        self.assertEqual(workbuddy.host, "workbuddy")
        workbuddy_configuration = json.loads(workbuddy_config.read_text(encoding="utf-8"))
        self.assertEqual(
            workbuddy_configuration["hooks"]["PreToolUse"][0]["matcher"],
            "^(Bash|exec_command)$",
        )

    def test_inspect_reports_the_current_disabled_or_missing_configuration(self) -> None:
        self.prepare("codex")
        configuration_path = self.workspace / ".codex" / "hooks.json"
        configuration_path.write_text('{"hooks": {}}\n', encoding="utf-8")

        observed = probe.inspect(self.workspace)
        self.assertEqual(observed["config"], {"hooks": {}})

        configuration_path.unlink()
        with self.assertRaisesRegex(probe.ProbeError, "does not exist"):
            probe.inspect(self.workspace)

        configuration_path.write_text("not JSON\n", encoding="utf-8")
        with self.assertRaisesRegex(probe.ProbeError, "not JSON"):
            probe.inspect(self.workspace)

    def test_native_event_persists_raw_payload_and_receipt(self) -> None:
        fixture = self.prepare()
        received = self.receive_native(fixture)
        self.assertEqual(received["receipt_status"], "ready")

        event = self.database().execute("SELECT * FROM events").fetchone()
        self.assertEqual(event["source_type"], "native")
        self.assertEqual(event["host"], "codex")
        self.assertEqual(event["native_tool_use_id"], "native-call-1")
        self.assertEqual(event["receipt_status"], "recorded")
        self.assertEqual(json.loads(event["raw_payload_json"])["hook_event_name"], "PreToolUse")
        self.assertEqual(self.database().execute("SELECT COUNT(*) FROM receipts").fetchone()[0], 1)

    def test_synthetic_event_is_labeled_and_cannot_authorize_execution(self) -> None:
        fixture = self.prepare()
        payload = self.native_event(fixture)
        payload["host"] = "codex"
        received = probe.receive_event(self.workspace, "codex", "synthetic", payload)
        self.assertEqual(received["receipt_status"], "synthetic_event")

        with self.assertRaisesRegex(probe.ProbeError, "no unconsumed"):
            self.run_execute(fixture)
        event = self.database().execute(
            "SELECT source_type, receipt_reason FROM events"
        ).fetchone()
        self.assertEqual(tuple(event), ("synthetic", "synthetic_event"))
        self.assertFalse((self.workspace / probe.RESULT_FILE).exists())

    def test_execute_commits_start_before_writing_business_artifact(self) -> None:
        fixture = self.prepare()
        self.receive_native(fixture)
        original_write_result = probe.write_result

        def assert_started(workspace: Path) -> tuple[Path, str]:
            database = probe.open_database(workspace)
            try:
                started = database.execute(
                    "SELECT status FROM invocations WHERE status = 'started'"
                ).fetchone()
            finally:
                database.close()
            self.assertIsNotNone(started)
            return original_write_result(workspace)

        with mock.patch.object(probe, "write_result", side_effect=assert_started):
            result = self.run_execute(fixture)

        result_text = (self.workspace / probe.RESULT_FILE).read_text(encoding="utf-8")
        self.assertEqual(result_text, "host-observation-probe-v1\n")
        self.assertIn("artifact_digest", result)
        invocation = self.database().execute(
            "SELECT status, artifact_digest FROM invocations"
        ).fetchone()
        self.assertEqual(invocation["status"], "finished")
        self.assertIsNotNone(invocation["artifact_digest"])

    def test_duplicate_native_receipt_is_only_consumed_once(self) -> None:
        fixture = self.prepare()
        self.receive_native(fixture)
        duplicate = self.receive_native(fixture)
        self.assertEqual(duplicate["receipt_status"], "duplicate_native_id")

        self.run_execute(fixture)
        with self.assertRaisesRegex(probe.ProbeError, "no unconsumed"):
            self.run_execute(fixture)
        receipt = self.database().execute("SELECT consumed_at FROM receipts").fetchone()
        self.assertIsNotNone(receipt["consumed_at"])
        invocation_count = self.database().execute("SELECT COUNT(*) FROM invocations").fetchone()[0]
        self.assertEqual(invocation_count, 1)

    def test_stale_wrong_nonce_wrong_cwd_and_no_hook_stop_without_artifact(self) -> None:
        stale_fixture = self.prepare()
        self.receive_native(stale_fixture)
        old_time = (datetime.now(UTC) - timedelta(seconds=probe.RECEIPT_MAX_AGE_SECONDS + 1)).replace(
            microsecond=0
        )
        stale_database = self.database()
        stale_database.execute("UPDATE receipts SET received_at = ?", (old_time.isoformat(),))
        stale_database.commit()
        with self.assertRaisesRegex(probe.ProbeError, "too old"):
            self.run_execute(stale_fixture)
        self.assertFalse((self.workspace / probe.RESULT_FILE).exists())

        wrong_nonce_workspace = self.root / "wrong-nonce"
        probe.prepare(wrong_nonce_workspace, "codex")
        wrong_nonce_fixture = probe.read_fixture(wrong_nonce_workspace)
        os.chdir(wrong_nonce_workspace)
        with self.assertRaisesRegex(probe.ProbeError, "nonce"):
            probe.execute(wrong_nonce_workspace, "wrong-nonce")
        self.assertFalse((wrong_nonce_workspace / probe.RESULT_FILE).exists())

        wrong_cwd_workspace = self.root / "wrong-cwd"
        probe.prepare(wrong_cwd_workspace, "codex")
        wrong_cwd_fixture = probe.read_fixture(wrong_cwd_workspace)
        nested_directory = wrong_cwd_workspace / "nested"
        nested_directory.mkdir()
        probe.receive_event(
            wrong_cwd_workspace,
            "codex",
            "native",
            self.native_event(wrong_cwd_fixture, cwd=nested_directory),
        )
        os.chdir(wrong_cwd_workspace)
        with self.assertRaisesRegex(probe.ProbeError, "no unconsumed"):
            probe.execute(wrong_cwd_workspace, wrong_cwd_fixture.nonce)
        self.assertFalse((wrong_cwd_workspace / probe.RESULT_FILE).exists())

        no_hook_workspace = self.root / "no-hook"
        probe.prepare(no_hook_workspace, "codex")
        no_hook_fixture = probe.read_fixture(no_hook_workspace)
        os.chdir(no_hook_workspace)
        with self.assertRaisesRegex(probe.ProbeError, "no unconsumed"):
            probe.execute(no_hook_workspace, no_hook_fixture.nonce)
        self.assertFalse((no_hook_workspace / probe.RESULT_FILE).exists())

    def test_event_outside_fixture_is_rejected(self) -> None:
        fixture = self.prepare()
        outside = self.root / "outside"
        outside.mkdir()
        with self.assertRaisesRegex(probe.ProbeError, "outside"):
            self.receive_native(fixture, cwd=outside)
        event_count = self.database().execute("SELECT COUNT(*) FROM events").fetchone()[0]
        self.assertEqual(event_count, 0)

    def test_mismatched_event_host_is_rejected(self) -> None:
        fixture = self.prepare()
        payload = self.native_event(fixture)
        payload["host"] = "workbuddy"
        with self.assertRaisesRegex(probe.ProbeError, "not codex"):
            probe.receive_event(self.workspace, "codex", "native", payload)
        event_count = self.database().execute("SELECT COUNT(*) FROM events").fetchone()[0]
        self.assertEqual(event_count, 0)

    def test_scan_event_does_not_create_receipt_or_invocation(self) -> None:
        fixture = self.prepare()
        self.receive_native(fixture, command="ls -la", tool_use_id="scan-call")
        receipt_count = self.database().execute("SELECT COUNT(*) FROM receipts").fetchone()[0]
        invocation_count = self.database().execute("SELECT COUNT(*) FROM invocations").fetchone()[0]
        self.assertEqual(receipt_count, 0)
        self.assertEqual(invocation_count, 0)
        with self.assertRaisesRegex(probe.ProbeError, "no unconsumed"):
            self.run_execute(fixture)
        self.assertFalse((self.workspace / probe.RESULT_FILE).exists())

    def test_nonce_mentioned_outside_the_exact_command_is_not_a_receipt(self) -> None:
        fixture = self.prepare()
        self.receive_native(
            fixture,
            command=f"echo prompt mentions {fixture.nonce}",
            tool_use_id="nonce-in-prompt",
        )
        event = self.database().execute("SELECT receipt_reason FROM events").fetchone()
        self.assertEqual(event["receipt_reason"], "command_does_not_match_fixture")
        receipt_count = self.database().execute("SELECT COUNT(*) FROM receipts").fetchone()[0]
        self.assertEqual(receipt_count, 0)
        with self.assertRaisesRegex(probe.ProbeError, "no unconsumed"):
            self.run_execute(fixture)

    def test_missing_stable_native_id_is_recorded_without_a_receipt(self) -> None:
        fixture = self.prepare()
        payload = self.native_event(fixture)
        del payload["tool_use_id"]
        received = probe.receive_event(self.workspace, "codex", "native", payload)
        self.assertEqual(received["receipt_status"], "missing_stable_native_id")
        event = self.database().execute("SELECT receipt_reason FROM events").fetchone()
        self.assertEqual(event["receipt_reason"], "missing_stable_native_id")
        self.assertEqual(self.database().execute("SELECT COUNT(*) FROM receipts").fetchone()[0], 0)

    def test_database_failure_stops_before_business_artifact(self) -> None:
        fixture = self.prepare()
        os.chdir(fixture.workspace)
        with mock.patch.object(
            probe,
            "open_database",
            side_effect=sqlite3.OperationalError("readonly"),
        ):
            with self.assertRaises(sqlite3.OperationalError):
                probe.execute(self.workspace, fixture.nonce)
        self.assertFalse((self.workspace / probe.RESULT_FILE).exists())

    def test_finish_record_failure_removes_the_business_artifact(self) -> None:
        fixture = self.prepare()
        self.receive_native(fixture)
        os.chdir(fixture.workspace)
        with mock.patch.object(
            probe,
            "finish_invocation",
            side_effect=sqlite3.OperationalError("readonly"),
        ):
            with self.assertRaisesRegex(probe.ProbeError, "readonly"):
                probe.execute(self.workspace, fixture.nonce)
        self.assertFalse((self.workspace / probe.RESULT_FILE).exists())


if __name__ == "__main__":
    unittest.main()
