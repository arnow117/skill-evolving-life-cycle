"""Integration contracts for declared, installed artifact checkers."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from selc import records
from selc.checker_integration import run_installed_check
from selc.checkers import SubjectIdentity
from selc.errors import SelcError
from selc.install import install_suite
from selc.manifest import create_suite, seal_suite


class CheckerIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(prefix="selc-checker-integration-")
        self.root = Path(self.temporary_directory.name)
        self.suite = self.root / "summary-suite"
        self.project = self.root / "project"
        create_suite("summary-suite", "1.0.0", self.suite, ["item-summary"])

        manifest_path = self.suite / "suite.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["artifact_checks"] = {
            "item-summary": {
                "checker": {"name": "json-item-summary", "version": "1"},
                "rule_version": "summary-v1",
                "required_rule_ids": ["count", "sum"],
                "configuration": {"duplicate_ids_count_once": False},
            }
        }
        manifest["skills"][0]["artifact_check"] = "item-summary"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        seal_suite(self.suite)
        install_suite(source=self.suite, project_root=self.project, harness="codex", ref=None)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def cli(self, *arguments: str, expected: int = 0) -> dict[str, object]:
        completed = subprocess.run(
            [sys.executable, str(REPOSITORY_ROOT / "scripts" / "selc.py"), *arguments],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            check=False,
            text=True,
        )
        self.assertEqual(completed.returncode, expected, completed.stdout + completed.stderr)
        return json.loads(completed.stdout)

    def check(self, input_bytes: bytes, artifact_bytes: bytes) -> dict[str, object]:
        input_path = self.root / "input.json"
        artifact_path = self.root / "artifact.json"
        input_path.write_bytes(input_bytes)
        artifact_path.write_bytes(artifact_bytes)
        return self.cli(
            "check",
            "--project-root",
            str(self.project),
            "--suite",
            "summary-suite",
            "--check-id",
            "item-summary",
            "--input",
            str(input_path),
            "--artifact",
            str(artifact_path),
        )

    def test_installed_declaration_cli_runs_builtin_without_business_records(self) -> None:
        result = self.check(
            b'{"items":[{"id":"a","value":2},{"id":"b","value":3}]}',
            b'{"count":2,"sum":5}',
        )
        check = result["check"]
        self.assertEqual(check["status"], "pass")
        self.assertEqual(
            check["checker"]["digest"],
            hashlib.sha256(
                (
                    self.project
                    / ".selc"
                    / "installs"
                    / "summary-suite"
                    / "current"
                    / "runtime"
                    / "selc"
                    / "checkers.py"
                ).read_bytes()
            ).hexdigest(),
        )
        database = records.open_database(self.project)
        try:
            self.assertEqual(database.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)
            self.assertEqual(database.execute("SELECT COUNT(*) FROM calls").fetchone()[0], 0)
        finally:
            database.close()

        failed = self.check(
            b'{"items":[{"id":"a","value":2},{"id":"b","value":3}]}',
            b'{"count":1,"sum":5}',
        )
        self.assertEqual(failed["check"]["status"], "fail")

        unknown = self.check(b"not json", b'{"count":2,"sum":5}')
        self.assertEqual(unknown["check"]["status"], "unknown")

    def test_external_registry_is_explicit_and_pinned(self) -> None:
        script = self.root / "custom_checker.py"
        script.write_text(
            "import json\n"
            "request = json.load(__import__('sys').stdin)\n"
            "print(json.dumps({'protocol': request['protocol'], 'checker': request['checker'], "
            "'results': [{'rule_id': 'custom', 'status': 'pass', 'reason': 'custom-rule'}]}))\n",
            encoding="utf-8",
        )
        digest = hashlib.sha256(script.read_bytes()).hexdigest()
        registered = self.cli(
            "checker",
            "register-script",
            "--project-root",
            str(self.project),
            "--name",
            "custom-summary",
            "--version",
            "1",
            "--script",
            str(script),
            "--digest",
            digest,
            "--rule",
            "custom",
        )
        self.assertEqual(registered["checker"]["digest"], digest)

        manifest_path = self.suite / "suite.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["artifact_checks"]["custom"] = {
            "checker": {"name": "custom-summary", "version": "1", "digest": digest},
            "rule_version": "custom-v1",
            "required_rule_ids": ["custom"],
            "configuration": {},
        }
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        seal_suite(self.suite)
        install_suite(source=self.suite, project_root=self.project, harness="codex", ref=None)

        checked = self.check_external("custom")
        self.assertEqual(checked["check"]["status"], "pass")

        locked_database = records.open_database(self.project)
        try:
            locked_database.execute("BEGIN EXCLUSIVE")
            input_path = self.root / "locked-input.json"
            artifact_path = self.root / "locked-artifact.json"
            input_path.write_bytes(b"{}")
            artifact_path.write_bytes(b"{}")
            execution = run_installed_check(
                project_root=self.project,
                suite_id="summary-suite",
                check_id="custom",
                input_path=input_path,
                artifact_path=artifact_path,
                subject=SubjectIdentity("task", "call", "delivery"),
                run_id="locked-database-check",
            )
            self.assertEqual(execution.result.status, "pass")
        finally:
            locked_database.rollback()
            locked_database.close()

        script.write_text("raise SystemExit(3)\n", encoding="utf-8")
        drifted = self.check_external("custom")
        self.assertEqual(drifted["check"]["status"], "unknown")
        self.assertEqual(drifted["check"]["reason"], "checker-digest-mismatch")

    def test_manifest_pin_mismatch_and_invalid_declaration_never_create_a_pass(self) -> None:
        manifest_path = self.suite / "suite.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["artifact_checks"]["item-summary"]["checker"]["digest"] = "f" * 64
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        seal_suite(self.suite)
        install_suite(source=self.suite, project_root=self.project, harness="codex", ref=None)

        pinned = self.check(
            b'{"items":[{"id":"a","value":2}]}', b'{"count":1,"sum":2}'
        )
        self.assertEqual(pinned["check"]["status"], "unknown")
        self.assertEqual(pinned["check"]["reason"], "checker-digest-mismatch")

        manifest["artifact_checks"]["item-summary"]["required_rule_ids"] = ["count", "count"]
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(SelcError) as invalid:
            seal_suite(self.suite)
        self.assertEqual(invalid.exception.code, "INVALID_MANIFEST")

    def test_late_input_or_artifact_mutation_changes_a_slow_external_result_to_unknown(self) -> None:
        script = self.root / "slow_checker.py"
        started_path = self.root / "checker-started"
        release_path = self.root / "checker-release"
        script.write_text(
            "import json\n"
            "import sys\n"
            "import time\n"
            "from pathlib import Path\n"
            "request = json.load(sys.stdin)\n"
            "Path(request['configuration']['started_path']).write_text('started')\n"
            "while not Path(request['configuration']['release_path']).exists():\n"
            "    time.sleep(0.01)\n"
            "print(json.dumps({'protocol': request['protocol'], 'checker': request['checker'], "
            "'results': [{'rule_id': 'slow', 'status': 'pass', 'reason': 'slow-pass'}]}))\n",
            encoding="utf-8",
        )
        digest = hashlib.sha256(script.read_bytes()).hexdigest()
        self.cli(
            "checker",
            "register-script",
            "--project-root",
            str(self.project),
            "--name",
            "slow-summary",
            "--version",
            "1",
            "--script",
            str(script),
            "--digest",
            digest,
            "--rule",
            "slow",
        )
        manifest_path = self.suite / "suite.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["artifact_checks"]["slow"] = {
            "checker": {"name": "slow-summary", "version": "1", "digest": digest},
            "rule_version": "slow-v1",
            "required_rule_ids": ["slow"],
            "configuration": {
                "started_path": str(started_path),
                "release_path": str(release_path),
            },
        }
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        seal_suite(self.suite)
        install_suite(source=self.suite, project_root=self.project, harness="codex", ref=None)

        for source_name, expected_reason in (
            ("input", "input-source-changed"),
            ("artifact", "artifact-source-changed"),
        ):
            with self.subTest(source_name=source_name):
                input_path = self.root / f"mutable-{source_name}-input.json"
                artifact_path = self.root / f"mutable-{source_name}-artifact.json"
                input_path.write_bytes(b"{}")
                artifact_path.write_bytes(b"{}")
                started_path.unlink(missing_ok=True)
                release_path.unlink(missing_ok=True)

                command = [
                    sys.executable,
                    str(REPOSITORY_ROOT / "scripts" / "selc.py"),
                    "check",
                    "--project-root",
                    str(self.project),
                    "--suite",
                    "summary-suite",
                    "--check-id",
                    "slow",
                    "--input",
                    str(input_path),
                    "--artifact",
                    str(artifact_path),
                ]
                process = subprocess.Popen(
                    command,
                    cwd=REPOSITORY_ROOT,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                deadline = time.monotonic() + 2
                while not started_path.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(started_path.exists())
                changed_bytes = b'{"changed":true}'
                changed_path = input_path if source_name == "input" else artifact_path
                changed_path.write_bytes(changed_bytes)
                release_path.write_text("release", encoding="utf-8")
                stdout, stderr = process.communicate(timeout=2)
                self.assertEqual(process.returncode, 0, stdout + stderr)
                result = json.loads(stdout)

                self.assertEqual(result["check"]["status"], "unknown")
                self.assertEqual(result["check"]["reason"], expected_reason)
                self.assertEqual(changed_path.read_bytes(), changed_bytes)

    def check_external(self, check_id: str) -> dict[str, object]:
        input_path = self.root / "external-input.json"
        artifact_path = self.root / "external-artifact.json"
        input_path.write_bytes(b"{}")
        artifact_path.write_bytes(b"{}")
        return self.cli(
            "check",
            "--project-root",
            str(self.project),
            "--suite",
            "summary-suite",
            "--check-id",
            check_id,
            "--input",
            str(input_path),
            "--artifact",
            str(artifact_path),
        )

    def test_recorded_metadata_is_bound_to_delivery_and_is_deleted_with_it(self) -> None:
        input_bytes = b'{"items":[{"id":"a","value":2}]}'
        artifact_bytes = b'{"count":1,"sum":2}'
        input_path = self.root / "record-input.json"
        artifact_path = self.root / "record-artifact.json"
        input_path.write_bytes(input_bytes)
        artifact_path.write_bytes(artifact_bytes)
        execution = run_installed_check(
            project_root=self.project,
            suite_id="summary-suite",
            check_id="item-summary",
            input_path=input_path,
            artifact_path=artifact_path,
            subject=SubjectIdentity("task", "call", "delivery"),
            run_id="record-run",
        )
        database = records.open_database(self.project)
        try:
            records.start_task(
                database,
                task_id="task",
                suite_id="summary-suite",
                suite_version="1.0.0",
                harness="codex",
                initiator="fixture",
                evidence="fixture",
                is_test=True,
            )
            records.start_call(
                database,
                call_id="call",
                task_id="task",
                skill="item-summary",
                executor="fixture",
                evidence="fixture",
                expected_version="1.0.0",
                expected_hash=execution.installation_digest,
                trigger_kind="explicit",
            )
            records.record_checked_delivery(database, execution)
            run = database.execute(
                "SELECT checker_digest, composite_digest, installation_digest FROM checker_runs "
                "WHERE delivery_id = 'delivery'"
            ).fetchone()
            self.assertEqual(run["checker_digest"], execution.result.checker.digest)
            self.assertNotEqual(run["composite_digest"], run["installation_digest"])
            self.assertEqual(
                database.execute(
                    "SELECT COUNT(*) FROM checker_rule_results WHERE delivery_id = 'delivery'"
                ).fetchone()[0],
                2,
            )
            snapshot_path = self.root / "checker-free-snapshot.json"
            records.export_snapshot(database, snapshot_path)
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            self.assertNotIn("checker_runs", snapshot)
            self.assertNotIn("checker_rule_results", snapshot)
            self.assertNotIn(artifact_bytes.decode("utf-8"), snapshot_path.read_text(encoding="utf-8"))
            records.complete_call(database, "call", "pass")
            records.close_task(database, "task")
            records.delete_records(database, suite_id="summary-suite", before=None, apply=True)
            self.assertEqual(
                database.execute("SELECT COUNT(*) FROM checker_runs").fetchone()[0],
                0,
            )
        finally:
            database.close()


if __name__ == "__main__":
    unittest.main()
