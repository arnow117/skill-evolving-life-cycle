"""Behavior coverage for the standalone local statistics report."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from selc import records, stats_report
from selc.errors import SelcError
from selc.manifest import create_suite, package_suite
from selc.stats_report import generate_stats_report, read_project


class StatsReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _project(self, name: str, *, malicious_suite: str = "suite-a") -> Path:
        project = self.root / name
        database = records.open_database(project)
        try:
            records.start_task(
                database,
                task_id="task-1",
                suite_id=malicious_suite,
                suite_version="1.0",
                harness="codex",
                initiator="manual",
                evidence="private evidence prompt log credential must not reach the report",
                is_test=False,
            )
            records.start_task(
                database,
                task_id="task-test",
                suite_id="suite-test",
                suite_version="1.0",
                harness="workbuddy",
                initiator="fixture",
                evidence="test-only private evidence",
                is_test=True,
            )
            records.start_call(
                database,
                call_id="call-1",
                task_id="task-1",
                skill="summary",
                executor="codex",
                evidence="private call evidence",
                expected_version="1.0",
                expected_hash="fixture-hash",
                trigger_kind="explicit",
            )
            database.execute(
                "UPDATE tasks SET first_result = 'pass', current_result = 'fail' WHERE task_id = 'task-1'"
            )
            database.execute(
                "UPDATE tasks SET first_result = 'unknown', current_result = 'unknown' WHERE task_id = 'task-test'"
            )
            database.execute(
                "UPDATE calls SET first_result = 'pass', result = 'fail' WHERE call_id = 'call-1'"
            )
            database.commit()
        finally:
            database.close()
        return project

    def test_reads_current_metrics_without_writing_source_or_embedding_sensitive_fields(
        self,
    ) -> None:
        project = self._project("alpha")
        database_path = project / ".selc" / "records.sqlite3"
        before = database_path.read_bytes()
        before_directory = sorted(path.name for path in (project / ".selc").iterdir())
        before_schema = self._schema_snapshot(database_path)
        output = self.root / "report.html"

        result = generate_stats_report([project], output, include_test=False)

        self.assertEqual(result["partial"], False)
        self.assertEqual(result["projects"][0]["status"], "ready")
        metrics = result["projects"][0]["projections"]["without_test"]["tasks"]
        self.assertEqual(metrics["one_shot"], {"N": 1, "S": 1, "K": 1, "U": 0})
        self.assertEqual(metrics["final"], {"N": 1, "S": 0, "K": 1, "U": 0})
        self.assertEqual(database_path.read_bytes(), before)
        self.assertEqual(self._schema_snapshot(database_path), before_schema)
        self.assertEqual(
            sorted(path.name for path in (project / ".selc").iterdir()), before_directory
        )

        report = output.read_text(encoding="utf-8")
        for forbidden in ("private evidence", "prompt", "log", "credential"):
            self.assertNotIn(forbidden, report)
        self.assertNotIn(str(project.resolve()), report)
        self.assertNotIn("task-1", report)
        self.assertIn("Content-Security-Policy", report)
        self.assertIn("suite-a", report)

    def test_embeds_python_reference_counts_for_filtering_without_sample_identifiers(self) -> None:
        project = self._project("alpha")
        output = self.root / "report.html"

        generate_stats_report([project], output, include_test=False)

        report_data = self._embedded_report_data(output)
        projections = report_data["projects"][0]["projections"]
        without_test_cells = projections["without_test"]["cells"]
        with_test_cells = projections["with_test"]["cells"]
        task_cells = [cell for cell in without_test_cells if cell["kind"] == "task"]
        non_test_task = next(cell for cell in task_cells if not cell["is_test"])
        test_task = next(
            cell for cell in with_test_cells if cell["kind"] == "task" and cell["is_test"]
        )

        self.assertEqual(non_test_task["one_shot"], {"N": 1, "S": 1, "K": 1, "U": 0})
        self.assertEqual(non_test_task["final"], {"N": 1, "S": 0, "K": 1, "U": 0})
        self.assertEqual(test_task["one_shot"], {"N": 1, "S": 0, "K": 0, "U": 1})
        self.assertEqual(test_task["final"], {"N": 1, "S": 0, "K": 0, "U": 1})
        self.assertFalse(any(cell["is_test"] for cell in without_test_cells))
        self.assertNotIn("task_id", json.dumps(projections))

    def test_test_ranges_deduplicate_before_projecting_local_and_imported_conflicts(self) -> None:
        project = self._project("dedup-ranges")
        database = records.open_database(project)
        try:
            for task_id, is_test, first_result, current_result in (
                ("shared-prod", False, "pass", "pass"),
                ("shared-test", True, "fail", "fail"),
            ):
                records.start_task(
                    database,
                    task_id=task_id,
                    suite_id="dedup-suite",
                    suite_version="1.0",
                    harness="codex",
                    initiator="manual",
                    evidence="dedup-evidence",
                    is_test=is_test,
                )
                records.start_call(
                    database,
                    call_id=f"call-{task_id}",
                    task_id=task_id,
                    skill="summary",
                    executor="codex",
                    evidence="dedup-evidence",
                    expected_version="1.0",
                    expected_hash="dedup-hash",
                    trigger_kind="explicit",
                )
                database.execute(
                    "UPDATE tasks SET first_result = ?, current_result = ? WHERE task_id = ?",
                    (first_result, current_result, task_id),
                )
                database.execute(
                    "UPDATE calls SET first_result = ?, result = ? WHERE call_id = ?",
                    (first_result, current_result, f"call-{task_id}"),
                )
            self._insert_imported_conflict(
                database,
                origin_id="test-copy",
                subject_id="shared-prod",
                call_id="call-shared-prod",
                is_test=True,
                first_result="fail",
                current_result="fail",
            )
            self._insert_imported_conflict(
                database,
                origin_id="production-copy",
                subject_id="shared-test",
                call_id="call-shared-test",
                is_test=False,
                first_result="pass",
                current_result="pass",
            )
            database.commit()
            expected_without_test = records.stats(database, include_test=False)
            expected_with_test = records.stats(database, include_test=True)
        finally:
            database.close()

        result = read_project(project, display_name="dedup-ranges")

        self.assertEqual(result["status"], "ready")
        projections = result["projections"]
        self.assertEqual(
            projections["without_test"]["metrics"], self._stats_metrics(expected_without_test)
        )
        self.assertEqual(
            projections["with_test"]["metrics"], self._stats_metrics(expected_with_test)
        )
        self.assertFalse(any(cell["is_test"] for cell in projections["without_test"]["cells"]))
        for key in ("without_test", "with_test"):
            metrics = stats_report._metrics_for_cells(
                [
                    cell
                    for cell in projections[key]["cells"]
                    if cell["group"]["suite_id"] == "dedup-suite"
                ],
                include_test=True,
            )
            expected = {
                "without_test": {"N": 2, "S": 2, "K": 2, "U": 0},
                "with_test": {"N": 2, "S": 0, "K": 0, "U": 2},
            }[key]
            self.assertEqual(metrics["tasks"]["one_shot"], expected)
            self.assertEqual(metrics["tasks"]["final"], expected)
            self.assertEqual(metrics["calls"]["one_shot"], expected)
            self.assertEqual(metrics["calls"]["final"], expected)

    def test_deduplicates_sources_and_keeps_other_projects_when_one_fails(self) -> None:
        project = self._project("alpha")
        missing = self.root / "missing"
        output = self.root / "report.html"

        result = generate_stats_report([project, project / ".", missing], output, include_test=True)

        self.assertEqual(len(result["projects"]), 2)
        self.assertTrue(result["partial"])
        self.assertEqual(result["projects"][0]["status"], "ready")
        self.assertEqual(result["projects"][1]["status"], "error")
        self.assertEqual(result["projects"][1]["error"]["code"], "SOURCE_NOT_FOUND")

    def test_deduplicates_unavailable_sources_by_canonical_path(self) -> None:
        project = self._project("alpha")
        missing = self.root / "missing"
        output = self.root / "report.html"

        result = generate_stats_report(
            [project, missing, missing / "."], output, include_test=False
        )

        self.assertEqual(len(result["projects"]), 2)
        self.assertEqual(result["projects"][1]["error"]["code"], "SOURCE_NOT_FOUND")

    def test_rejects_wal_and_preserves_an_existing_report_on_total_failure(self) -> None:
        project = self._project("alpha")
        database_path = project / ".selc" / "records.sqlite3"
        (database_path.parent / "records.sqlite3-wal").write_bytes(b"active")
        output = self.root / "report.html"
        output.write_text("known-good", encoding="utf-8")

        with self.assertRaisesRegex(SelcError, "no selected project"):
            generate_stats_report([project], output, include_test=False)

        self.assertEqual(output.read_text(encoding="utf-8"), "known-good")

    def test_all_unavailable_sources_keep_old_report_and_identify_each_recovery(self) -> None:
        missing = self.root / "missing"
        future_protocol = self._project("future-protocol")
        database_path = future_protocol / ".selc" / "records.sqlite3"
        connection = sqlite3.connect(database_path)
        try:
            connection.execute("UPDATE meta SET value = '999' WHERE key = 'writer_protocol'")
            connection.commit()
        finally:
            connection.close()
        output = self.root / "report.html"
        output.write_text("known-good", encoding="utf-8")

        with self.assertRaises(SelcError) as captured:
            generate_stats_report([missing, future_protocol], output, include_test=False)

        error = captured.exception
        self.assertEqual(error.code, "ALL_PROJECTS_UNAVAILABLE")
        self.assertIn("missing [SOURCE_NOT_FOUND]", error.message)
        self.assertIn("future-protocol [UNSUPPORTED_DATABASE]", error.message)
        self.assertIn("repair each reported source", error.recovery)
        self.assertEqual(output.read_text(encoding="utf-8"), "known-good")

    def test_rejects_output_aliases_and_safe_encoding_before_replacement(self) -> None:
        project = self._project("alpha")
        output = self.root / "report.html"
        output.write_text("known-good", encoding="utf-8")
        link = self.root / "report-link.html"
        link.symlink_to(output)

        with self.assertRaisesRegex(SelcError, "symbolic link"):
            generate_stats_report([project], link, include_test=False)
        original_renderer = stats_report.render_report
        stats_report.render_report = lambda _report: "bad\ud800report"
        try:
            with self.assertRaisesRegex(SelcError, "UTF-8"):
                generate_stats_report([project], output, include_test=False)
        finally:
            stats_report.render_report = original_renderer

        self.assertEqual(output.read_text(encoding="utf-8"), "known-good")

    def test_rejects_hard_linked_and_source_directory_outputs(self) -> None:
        project = self._project("alpha")
        output = self.root / "report.html"
        output.write_text("known-good", encoding="utf-8")
        linked_output = self.root / "linked-report.html"
        os.link(output, linked_output)

        with self.assertRaisesRegex(SelcError, "unlinked regular file"):
            generate_stats_report([project], output, include_test=False)
        with self.assertRaisesRegex(SelcError, "inside a source .selc directory"):
            generate_stats_report([project], project / ".selc" / "report.html", include_test=False)

        self.assertEqual(output.read_text(encoding="utf-8"), "known-good")

    def test_malicious_dimensions_stay_data_and_csp_hashes_only_static_code(self) -> None:
        malicious_suite = '</script><!--<script><img src=x onerror="window.pwned=true">'
        project = self._project("alpha", malicious_suite=malicious_suite)
        output = self.root / "report.html"

        generate_stats_report([project], output, include_test=False)

        report = output.read_text(encoding="utf-8")
        self.assertNotIn(malicious_suite, report)
        self.assertIn("\\u003c/script\\u003e\\u003c!--\\u003cscript", report)
        self.assertNotIn("<!--<script>", report)
        self.assertIn("default-src 'none'", report)
        self.assertIn("script-src 'sha256-", report)
        self.assertIn("style-src 'sha256-", report)
        self.assertNotIn("'unsafe-inline'", report)

    def test_read_project_rejects_future_protocol_without_initializing_database(self) -> None:
        project = self._project("alpha")
        database_path = project / ".selc" / "records.sqlite3"
        connection = sqlite3.connect(database_path)
        try:
            connection.execute("UPDATE meta SET value = '999' WHERE key = 'writer_protocol'")
            connection.commit()
        finally:
            connection.close()
        before = database_path.read_bytes()

        result = read_project(project, display_name="alpha")

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "UNSUPPORTED_DATABASE")
        self.assertEqual(database_path.read_bytes(), before)

    def test_invalid_installation_registries_remain_unverified_and_nonfatal(self) -> None:
        invalid_registries = (
            "[]",
            '{"installations":{"sample":{"mode":[]}}}',
            '{"installations":{"sample":"basic"}}',
        )
        for index, registry_value in enumerate(invalid_registries):
            with self.subTest(registry_value=registry_value):
                project = self.root / f"invalid-registry-{index}"
                registry = project / ".selc" / "installations.json"
                registry.parent.mkdir(parents=True)
                registry.write_text(registry_value, encoding="utf-8")

                result = read_project(project, display_name=project.name)

                self.assertEqual(result["status"], "error")
                self.assertEqual(result["mode"], "unverified")
                self.assertEqual(result["error"]["code"], "MISSING_DATABASE")

    def test_reads_only_the_database_header_prefix(self) -> None:
        project = self._project("alpha")

        with patch.object(Path, "read_bytes", side_effect=AssertionError("whole-file read")):
            result = read_project(project, display_name="alpha")

        self.assertEqual(result["status"], "ready")

    def test_cli_writes_report_without_changing_existing_stats_json(self) -> None:
        project = self._project("alpha")
        output = self.root / "report.html"
        command = [
            sys.executable,
            str(REPOSITORY_ROOT / "scripts" / "selc.py"),
            "stats-report",
            "--project-root",
            str(project),
            "--output",
            str(output),
        ]

        report = subprocess.run(command, capture_output=True, check=False, text=True)
        existing_stats = subprocess.run(
            [
                sys.executable,
                str(REPOSITORY_ROOT / "scripts" / "selc.py"),
                "stats",
                "--project-root",
                str(project),
            ],
            capture_output=True,
            check=False,
            text=True,
        )

        self.assertEqual(report.returncode, 0, report.stdout + report.stderr)
        self.assertIn('"report"', report.stdout)
        self.assertEqual(
            existing_stats.returncode, 0, existing_stats.stdout + existing_stats.stderr
        )
        self.assertIn('"metric_contract"', existing_stats.stdout)
        self.assertTrue(output.is_file())

    def test_rejects_busy_source_without_creating_output_or_source_side_files(self) -> None:
        project = self._project("alpha")
        database_path = project / ".selc" / "records.sqlite3"
        output = self.root / "report.html"
        before = database_path.read_bytes()
        before_directory = sorted(path.name for path in database_path.parent.iterdir())
        lock = sqlite3.connect(database_path, timeout=0.1)
        lock.create_function("selc_writer_protocol", 0, lambda: records.WRITER_PROTOCOL_VERSION)
        try:
            lock.execute("BEGIN EXCLUSIVE")
            lock.execute(
                "UPDATE tasks SET suite_version = 'concurrent-writer' WHERE task_id = 'task-1'"
            )
            result = read_project(project, display_name="alpha")
        finally:
            lock.rollback()
            lock.close()

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "DATABASE_BUSY")
        self.assertFalse(output.exists())
        self.assertEqual(database_path.read_bytes(), before)
        self.assertEqual(
            sorted(path.name for path in database_path.parent.iterdir()), before_directory
        )

    def test_read_transaction_keeps_one_snapshot_during_a_rollback_writer_commit(self) -> None:
        project = self._project("concurrent")
        reader_has_snapshot = threading.Event()
        writer_staged = threading.Event()
        first_commit_attempted = threading.Event()
        first_commit_blocked = threading.Event()
        writer_committed_early = threading.Event()
        reader_finished = threading.Event()
        writer_committed = threading.Event()
        reader_results: list[dict[str, object]] = []
        writer_failures: list[BaseException] = []
        original_reader = records._deduplicated_samples

        def coordinated_reader(
            connection: sqlite3.Connection, kind: str, include_test: bool
        ) -> list[dict[str, object]]:
            rows = original_reader(connection, kind, include_test)
            if kind == "task" and not include_test and not reader_has_snapshot.is_set():
                reader_has_snapshot.set()
                self.assertTrue(writer_staged.wait(timeout=3), "writer did not stage its update")
                self.assertTrue(
                    first_commit_attempted.wait(timeout=3),
                    "writer did not attempt its first commit",
                )
                self.assertFalse(
                    writer_committed_early.is_set(),
                    "writer committed while the reader transaction was still open",
                )
                self.assertTrue(
                    first_commit_blocked.is_set(),
                    "reader transaction did not block the writer's first commit",
                )
            return rows

        def writer() -> None:
            database_path = project / ".selc" / "records.sqlite3"
            connection = sqlite3.connect(database_path, timeout=0.1)
            connection.create_function(
                "selc_writer_protocol", 0, lambda: records.WRITER_PROTOCOL_VERSION
            )
            try:
                self.assertTrue(
                    reader_has_snapshot.wait(timeout=3), "reader did not establish a snapshot"
                )
                connection.execute("PRAGMA busy_timeout = 100")
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "UPDATE tasks SET current_result = 'pass' WHERE task_id = 'task-1'"
                )
                connection.execute("UPDATE calls SET result = 'pass' WHERE call_id = 'call-1'")
                writer_staged.set()
                try:
                    connection.commit()
                except sqlite3.OperationalError as error:
                    if "locked" not in str(error).lower():
                        raise
                    first_commit_blocked.set()
                else:
                    writer_committed_early.set()
                finally:
                    first_commit_attempted.set()

                self.assertTrue(
                    first_commit_blocked.is_set(),
                    "writer committed before the reader released its snapshot",
                )
                self.assertTrue(
                    reader_finished.wait(timeout=3), "reader did not release its transaction"
                )
                connection.commit()
                writer_committed.set()
            except BaseException as error:  # Thread failures must reach the test assertion.
                writer_failures.append(error)
            finally:
                connection.close()

        writer_thread = threading.Thread(target=writer)
        writer_thread.start()
        try:
            with patch.object(records, "_deduplicated_samples", side_effect=coordinated_reader):
                reader_results.append(read_project(project, display_name="concurrent"))
        finally:
            reader_finished.set()
            writer_thread.join(timeout=3)

        self.assertFalse(writer_thread.is_alive())
        self.assertEqual(writer_failures, [])
        self.assertTrue(first_commit_attempted.is_set())
        self.assertTrue(first_commit_blocked.is_set())
        self.assertFalse(writer_committed_early.is_set())
        self.assertTrue(writer_committed.is_set())
        before_commit = reader_results[0]["projections"]["without_test"]["metrics"]
        self.assertEqual(before_commit["tasks"]["final"], {"N": 1, "S": 0, "K": 1, "U": 0})
        self.assertEqual(before_commit["calls"]["final"], {"N": 1, "S": 0, "K": 1, "U": 0})

        after_commit = read_project(project, display_name="concurrent")["projections"][
            "without_test"
        ]["metrics"]
        self.assertEqual(after_commit["tasks"]["final"], {"N": 1, "S": 1, "K": 1, "U": 0})
        self.assertEqual(after_commit["calls"]["final"], {"N": 1, "S": 1, "K": 1, "U": 0})

    def test_verified_basic_project_without_database_is_a_valid_empty_source(self) -> None:
        project = self.root / "basic-project"
        registry = project / ".selc" / "installations.json"
        registry.parent.mkdir(parents=True)
        registry.write_text(
            '{"schema_version":1,"installations":{"sample":{"mode":"basic"}}}',
            encoding="utf-8",
        )

        result = read_project(project, display_name="basic-project")

        self.assertEqual(result["status"], "basic_no_records")
        self.assertEqual(result["mode"], "basic")
        self.assertNotIn("metrics", result)

    def test_directory_fsync_warning_reports_a_published_new_report(self) -> None:
        project = self._project("alpha")
        output = self.root / "report.html"
        output.write_text("known-good", encoding="utf-8")

        with patch("selc.stats_report._fsync_parent_directory", side_effect=OSError("disk")):
            result = generate_stats_report([project], output, include_test=False)

        self.assertEqual(
            result["durability_warning"],
            "report was replaced, but parent-directory durability could not be confirmed",
        )
        self.assertNotEqual(output.read_text(encoding="utf-8"), "known-good")

    def test_precommit_output_failures_keep_old_report_and_clean_temporary_files(self) -> None:
        project = self._project("alpha")
        output = self.root / "report.html"
        output.write_text("known-good", encoding="utf-8")
        expected_files = sorted(path.name for path in self.root.iterdir())

        for target in ("fsync", "replace"):
            with self.subTest(target=target):
                with (
                    patch(f"selc.stats_report.os.{target}", side_effect=OSError("disk")),
                    self.assertRaisesRegex(SelcError, "could not be written safely"),
                ):
                    generate_stats_report([project], output, include_test=False)
                self.assertEqual(output.read_text(encoding="utf-8"), "known-good")
                self.assertEqual(sorted(path.name for path in self.root.iterdir()), expected_files)

    def test_unreadable_source_is_partial_without_weakening_output_validation(self) -> None:
        readable = self._project("readable")
        unreadable = self._project("unreadable")
        protected_directory = unreadable / ".selc"
        output = self.root / "report.html"
        original_mode = protected_directory.stat().st_mode
        protected_directory.chmod(0)
        try:
            result = generate_stats_report([readable, unreadable], output, include_test=False)
        finally:
            protected_directory.chmod(original_mode)

        self.assertTrue(result["partial"])
        error = result["projects"][1]["error"]
        self.assertEqual(error["code"], "SOURCE_UNREADABLE")
        self.assertIn("permissions", error["recovery"])
        self.assertTrue(output.is_file())

        linked_output = self.root / "linked-report.html"
        os.link(output, linked_output)
        with self.assertRaisesRegex(SelcError, "unlinked regular file"):
            generate_stats_report([readable, unreadable], linked_output, include_test=False)

    def test_recheck_refuses_an_existing_output_that_disappears(self) -> None:
        project = self._project("alpha")
        output = self.root / "report.html"
        output.write_text("known-good", encoding="utf-8")
        parent_identity = stats_report._path_identity(output.parent)
        target_before = output.lstat()
        output.unlink()

        with self.assertRaisesRegex(SelcError, "disappeared"):
            stats_report._recheck_output_target(
                output,
                [project],
                parent_identity,
                target_before,
            )

    def test_report_is_a_static_snapshot_until_explicit_regeneration(self) -> None:
        project = self._project("alpha")
        output = self.root / "report.html"
        generate_stats_report([project], output, include_test=False)
        first_snapshot = output.read_text(encoding="utf-8")
        database = records.open_database(project)
        try:
            database.execute("UPDATE tasks SET current_result = 'pass' WHERE task_id = 'task-1'")
            database.commit()
        finally:
            database.close()

        self.assertEqual(output.read_text(encoding="utf-8"), first_snapshot)
        generate_stats_report([project], output, include_test=False)

        self.assertNotEqual(output.read_text(encoding="utf-8"), first_snapshot)

    def test_packaged_portable_runtime_generates_the_same_report_command(self) -> None:
        project = self._project("alpha")
        suite = self.root / "portable-suite"
        package = self.root / "portable-suite.zip"
        unpacked = self.root / "unpacked-suite"
        output = self.root / "portable-report.html"
        create_suite("portable-suite", "1.0.0", suite, ["summary-report"])
        package_suite(suite, package)

        with zipfile.ZipFile(package) as archive:
            archive.extractall(unpacked)

        portable_reader = unpacked / "runtime" / "selc" / "stats_report.py"
        portable_template = unpacked / "runtime" / "selc" / "stats_report_template.py"
        self.assertEqual(
            portable_reader.read_bytes(),
            (REPOSITORY_ROOT / "src" / "selc" / "stats_report.py").read_bytes(),
        )
        self.assertEqual(
            portable_template.read_bytes(),
            (REPOSITORY_ROOT / "src" / "selc" / "stats_report_template.py").read_bytes(),
        )
        completed = subprocess.run(
            [
                sys.executable,
                str(unpacked / "bin" / "selc-suite.py"),
                "stats-report",
                "--project-root",
                str(project),
                "--output",
                str(output),
            ],
            capture_output=True,
            check=False,
            text=True,
        )

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn('"report"', completed.stdout)
        self.assertIn("本地统计快照", output.read_text(encoding="utf-8"))

    def _embedded_report_data(self, output: Path) -> dict[str, object]:
        source = output.read_text(encoding="utf-8")
        match = re.search(
            r'<script id="report-data" type="application/json">(.*?)</script>', source
        )
        self.assertIsNotNone(match)
        return json.loads(match.group(1))

    def _insert_imported_conflict(
        self,
        database: sqlite3.Connection,
        *,
        origin_id: str,
        subject_id: str,
        call_id: str,
        is_test: bool,
        first_result: str,
        current_result: str,
    ) -> None:
        values = {
            "origin_id": origin_id,
            "suite_id": "dedup-suite",
            "suite_version": "1.0",
            "harness": "codex",
            "is_test": int(is_test),
            "started_at": "2026-10-26T00:00:00Z",
            "initiator": "manual",
            "skill": "summary",
            "executor": "codex",
            "evidence": "dedup-evidence",
            "trigger_kind": "explicit",
            "expected_hash": "dedup-hash",
            "first_result": first_result,
            "current_result": current_result,
            "imported_at": "2026-10-26T00:00:00Z",
        }
        for kind, identifier in (("task", subject_id), ("call", call_id)):
            database.execute(
                """INSERT INTO imported_samples(
                origin_id, subject_kind, subject_id, suite_id, suite_version, harness,
                is_test, started_at, initiator, skill, executor, evidence, trigger_kind,
                expected_hash, first_result, current_result, imported_at)
                VALUES (
                :origin_id, :subject_kind, :subject_id, :suite_id, :suite_version, :harness,
                :is_test, :started_at, :initiator, :skill, :executor, :evidence, :trigger_kind,
                :expected_hash, :first_result, :current_result, :imported_at)""",
                {**values, "subject_kind": kind, "subject_id": identifier},
            )

    def _stats_metrics(self, value: dict[str, object]) -> dict[str, object]:
        return {
            collection: {
                axis: {key: value[collection][axis][key] for key in ("N", "S", "K", "U")}
                for axis in ("one_shot", "final")
            }
            for collection in ("tasks", "calls")
        }

    def _schema_snapshot(self, database_path: Path) -> list[tuple[str, str]]:
        connection = sqlite3.connect(database_path)
        try:
            return connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type IN ('table', 'index', 'trigger') ORDER BY name"
            ).fetchall()
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
