"""Focused reducer tests for outcome evidence, revisions, and snapshot transport."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
from selc import records
from selc.errors import SelcError
from selc.util import json_hash, read_json, write_json


class RecordContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.original_clock = records.utc_now
        records.utc_now = lambda: "2026-10-20T02:00:00Z"

    def tearDown(self) -> None:
        records.utc_now = self.original_clock
        self.temporary_directory.cleanup()

    def database(self, name: str):
        return records.open_database(self.root / name)

    def start_task(
        self, database, *, task_id: str = "task", is_test: bool = True, ready: bool = True
    ) -> None:
        records.start_task(
            database,
            task_id=task_id,
            suite_id="suite",
            suite_version="1.0.0",
            harness="codex",
            initiator="author",
            evidence="fixture",
            is_test=is_test,
        )
        if ready:
            records.set_task_event(database, task_id, "input_ready", "inputs supplied")

    def delivery(
        self,
        database,
        delivery_id: str,
        result: str,
        *,
        call_id: str | None = None,
        checker_id: str | None = None,
    ) -> None:
        records.record_delivery(
            database,
            delivery_id=delivery_id,
            task_id="task",
            call_id=call_id,
            artifact_hash=f"artifact-{delivery_id}",
            rule_version="rule-v1",
            declared_result=result,
            complete_fixture=checker_id is None,
            checker_id=checker_id,
            checker_verified=checker_id is not None,
            checker_result=result if checker_id is not None else None,
        )

    def task_outcome(self, database) -> tuple[str, str]:
        row = database.execute(
            "SELECT first_result, current_result FROM tasks WHERE task_id = 'task'"
        ).fetchone()
        return row["first_result"], row["current_result"]

    def test_call_self_report_requires_linked_artifact_check(self) -> None:
        database = self.database("call-proof")
        self.start_task(database, is_test=False)
        records.start_call(
            database,
            call_id="call",
            task_id="task",
            skill="report",
            executor="workbuddy",
            evidence="self-report",
            expected_version="2.0.0",
            expected_hash="digest",
            trigger_kind="explicit",
        )
        completed = records.complete_call(database, "call", "pass")
        self.assertEqual(completed["result"], "unknown")
        self.assertEqual(records.stats(database, include_test=False)["calls"]["final"]["U"], 1)
        self.delivery(database, "checked", "pass", call_id="call", checker_id="independent-rule")
        call = database.execute(
            "SELECT first_result, result, actual_harness FROM calls WHERE call_id = 'call'"
        ).fetchone()
        self.assertEqual((call["first_result"], call["result"]), ("unknown", "pass"))
        self.assertEqual(call["actual_harness"], "workbuddy")

    def test_original_feedback_refutes_both_axes_and_remedy_only_repairs_final(self) -> None:
        database = self.database("feedback")
        self.start_task(database)
        self.delivery(database, "initial", "pass")
        records.record_feedback(
            database,
            feedback_id="negative",
            task_id="task",
            delivery_id="initial",
            revision=1,
            scope="final",
            result="fail",
            body="original artifact missed a required point",
        )
        self.assertEqual(self.task_outcome(database), ("fail", "fail"))
        self.delivery(database, "remedy", "pass")
        self.assertEqual(self.task_outcome(database), ("fail", "pass"))
        records.record_feedback(
            database,
            feedback_id="requirement-change",
            task_id="task",
            delivery_id="initial",
            revision=1,
            scope="requirement_change",
            result="fail",
            body="a new requirement does not judge the original delivery",
        )
        self.assertEqual(self.task_outcome(database), ("fail", "pass"))
        records.record_feedback(
            database,
            feedback_id="negative",
            task_id="task",
            delivery_id="initial",
            revision=2,
            scope="first",
            result="pass",
            body="the original feedback thread is corrected",
        )
        self.assertEqual(self.task_outcome(database), ("pass", "pass"))
        records.record_feedback(
            database,
            feedback_id="independent-complaint",
            task_id="task",
            delivery_id="initial",
            revision=1,
            scope="first",
            result="fail",
            body="an independent original-requirement failure remains effective",
        )
        self.assertEqual(self.task_outcome(database), ("fail", "pass"))
        records.record_feedback(
            database,
            feedback_id="independent-complaint",
            task_id="task",
            delivery_id="initial",
            revision=2,
            scope="final",
            result="pass",
            body="the independent thread is resolved",
        )
        self.assertEqual(self.task_outcome(database), ("pass", "pass"))

    def test_active_call_prevents_task_close_and_update_lease_bypass(self) -> None:
        database = self.database("active-call")
        self.start_task(database)
        records.start_call(
            database,
            call_id="running",
            task_id="task",
            skill="report",
            executor="codex",
            evidence="fixture",
            expected_version="1.0.0",
            expected_hash=None,
            trigger_kind="explicit",
        )
        self.assertEqual(records.active_leases(database, "suite")["calls"], 1)
        with self.assertRaisesRegex(SelcError, "active call") as captured:
            records.close_task(database, "task")
        self.assertEqual(captured.exception.code, "ACTIVE_CALL")
        records.complete_call(database, "running", "unknown")
        records.close_task(database, "task")
        with self.assertRaisesRegex(SelcError, "closed task") as captured:
            records.start_call(
                database,
                call_id="after-close",
                task_id="task",
                skill="report",
                executor="codex",
                evidence="fixture",
                expected_version="1.0.0",
                expected_hash=None,
                trigger_kind="explicit",
            )
        self.assertEqual(captured.exception.code, "TASK_CLOSED")

    def test_delete_apply_preserves_active_task_call_and_session_leases(self) -> None:
        database = self.database("delete-active")
        self.start_task(database)
        records.start_call(
            database,
            call_id="call",
            task_id="task",
            skill="report",
            executor="codex",
            evidence="fixture",
            expected_version="1.0.0",
            expected_hash=None,
            trigger_kind="explicit",
        )
        records.set_task_event(database, "task", "session_start", "host session")
        preview = records.delete_records(database, suite_id="suite", before=None, apply=False)
        self.assertEqual(
            preview["preview"]["active_leases"], {"tasks": 1, "calls": 1, "sessions": 1}
        )
        with self.assertRaises(SelcError) as captured:
            records.delete_records(database, suite_id="suite", before=None, apply=True)
        self.assertEqual(captured.exception.code, "ACTIVE_LEASE")
        self.assertEqual(database.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 1)
        records.complete_call(database, "call", "unknown")
        records.set_task_event(database, "task", "session_close", "host session closed")
        records.close_task(database, "task")
        deleted = records.delete_records(database, suite_id="suite", before=None, apply=True)
        self.assertEqual(deleted["deleted"]["local_tasks"], 1)

    def test_sequence_readiness_and_permission_events_are_preserved(self) -> None:
        database = self.database("sequence")
        self.start_task(database, ready=False)
        records.set_task_event(database, "task", "permission_click", "permission accepted")
        self.delivery(database, "z-first", "fail")
        self.delivery(database, "a-second", "pass")
        self.assertEqual(self.task_outcome(database), ("fail", "pass"))
        events = database.execute(
            "SELECT kind, evidence FROM events WHERE task_id = 'task' ORDER BY event_id"
        ).fetchall()
        self.assertIn(("permission_click", "permission accepted"), [tuple(row) for row in events])

        ready_database = self.database("not-ready")
        self.start_task(ready_database, ready=False)
        self.delivery(ready_database, "only", "pass")
        self.assertEqual(self.task_outcome(ready_database)[0], "unknown")
        records.set_task_event(ready_database, "task", "input_ready", "inputs later supplied")
        self.assertEqual(self.task_outcome(ready_database), ("pass", "pass"))

    def test_snapshot_preserves_call_provenance_and_axes(self) -> None:
        source = self.database("source")
        target = self.database("target")
        self.start_task(source, is_test=False)
        records.start_call(
            source,
            call_id="call",
            task_id="task",
            skill="report",
            executor="workbuddy",
            evidence="checker-adapter",
            expected_version="2.0.0",
            expected_hash="digest",
            trigger_kind="automatic",
        )
        self.delivery(source, "artifact", "pass", call_id="call", checker_id="independent-rule")
        records.complete_call(source, "call", "pass")
        snapshot = self.root / "snapshot.json"
        records.export_snapshot(source, snapshot)
        call = json.loads(snapshot.read_text(encoding="utf-8"))["calls"][0]
        self.assertEqual(call["harness"], "workbuddy")
        self.assertEqual(call["skill"], "report")
        self.assertEqual(call["first_result"], "unknown")
        records.import_snapshot(target, snapshot)
        self.assertEqual(
            records.stats(target, include_test=False)["calls"],
            records.stats(source, include_test=False)["calls"],
        )

    def test_task_groups_keep_initiator_separate_from_harness(self) -> None:
        database = self.database("initiators")
        self.start_task(database, task_id="codex-task")
        records.start_task(
            database,
            task_id="workbuddy-task",
            suite_id="suite",
            suite_version="1.0.0",
            harness="codex",
            initiator="workbuddy",
            evidence="fixture",
            is_test=True,
        )
        groups = records.stats(database, include_test=True)["groups"]["tasks"]
        self.assertEqual(len(groups), 2)
        self.assertEqual({group["group"]["initiator"] for group in groups}, {"author", "workbuddy"})

    def test_snapshot_identity_axes_delete_time_and_revision_are_safe(self) -> None:
        def snapshot(origin: str, *, suite_id: str, first: str, final: str) -> Path:
            payload = {
                "schema_version": 1,
                "origin_id": origin,
                "snapshot_revision": 1,
                "as_of": "2026-10-20T02:00:00Z",
                "metric_contract": {
                    "groups": ["tasks.one_shot", "tasks.final", "calls.one_shot", "calls.final"],
                    "measures": ["N", "S", "K", "U", "S_over_N", "S_over_K", "K_over_N"],
                    "privacy": "fixture",
                },
                "tasks": [
                    {
                        "task_id": "shared",
                        "suite_id": suite_id,
                        "suite_version": "1.0.0",
                        "harness": "codex",
                        "is_test": False,
                        "started_at": "2020-01-01T00:00:00Z",
                        "first_result": first,
                        "current_result": final,
                    }
                ],
                "calls": [],
            }
            payload["snapshot_hash"] = json_hash(payload)
            path = self.root / f"{origin}.json"
            write_json(path, payload)
            return path

        database = self.database("dedupe")
        records.import_snapshot(
            database, snapshot("origin-a", suite_id="suite", first="pass", final="pass")
        )
        records.import_snapshot(
            database, snapshot("origin-b", suite_id="other-suite", first="pass", final="pass")
        )
        self.assertEqual(records.stats(database, include_test=False)["tasks"]["final"]["U"], 1)
        preview = records.delete_records(
            database, suite_id="suite", before="2021-01-01T00:00:00Z", apply=False
        )
        self.assertEqual(preview["preview"]["imported_samples"], 1)

        source = self.root / "revision-source"
        records.open_database(source).close()
        barrier = threading.Barrier(2)
        revisions: list[int] = []

        def export_snapshot(index: int) -> None:
            database_for_thread = records.open_database(source)
            barrier.wait(timeout=5)
            result = records.export_snapshot(
                database_for_thread, self.root / f"revision-{index}.json"
            )
            revisions.append(result["snapshot_revision"])
            database_for_thread.close()

        threads = [threading.Thread(target=export_snapshot, args=(index,)) for index in (1, 2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(sorted(revisions), [1, 2])

    def test_imported_call_identity_preserves_initiator_and_marks_conflicts_unknown(self) -> None:
        def snapshot(origin: str, initiator: str, first: str, final: str) -> Path:
            payload = {
                "schema_version": 1,
                "origin_id": origin,
                "snapshot_revision": 1,
                "as_of": "2026-10-20T02:00:00Z",
                "metric_contract": {
                    "groups": ["tasks.one_shot", "tasks.final", "calls.one_shot", "calls.final"],
                    "measures": ["N", "S", "K", "U", "S_over_N", "S_over_K", "K_over_N"],
                    "privacy": "fixture",
                },
                "tasks": [],
                "calls": [
                    {
                        "call_id": "shared-call",
                        "task_id": "source-task",
                        "suite_id": "suite",
                        "suite_version": "1.0.0",
                        "harness": "codex",
                        "initiator": initiator,
                        "skill": "text-report",
                        "executor": "codex",
                        "evidence": "fixture",
                        "trigger_kind": "explicit",
                        "expected_hash": "digest",
                        "is_test": False,
                        "started_at": "2026-10-20T02:00:00Z",
                        "first_result": first,
                        "current_result": final,
                    }
                ],
            }
            payload["snapshot_hash"] = json_hash(payload)
            path = self.root / f"{origin}.json"
            write_json(path, payload)
            return path

        database = self.database("call-identity")
        records.import_snapshot(database, snapshot("origin-a", "author", "pass", "pass"))
        records.import_snapshot(database, snapshot("origin-b", "workbuddy", "pass", "pass"))
        metrics = records.stats(database, include_test=False)["calls"]
        self.assertEqual(metrics["final"]["U"], 1)

        database = self.database("call-axis-conflict")
        records.import_snapshot(database, snapshot("origin-c", "author", "pass", "pass"))
        records.import_snapshot(database, snapshot("origin-d", "author", "unknown", "pass"))
        metrics = records.stats(database, include_test=False)["calls"]
        self.assertEqual(metrics["one_shot"]["U"], 1)

    def test_lifecycle_replays_and_rejects_conflicting_evidence(self) -> None:
        database = self.database("rejections")
        self.start_task(database, is_test=False)
        replay = records.start_task(
            database,
            task_id="task",
            suite_id="suite",
            suite_version="1.0.0",
            harness="codex",
            initiator="author",
            evidence="fixture",
            is_test=False,
        )
        self.assertTrue(replay["replayed"])
        with self.assertRaisesRegex(SelcError, "already belongs"):
            records.start_task(
                database,
                task_id="task",
                suite_id="other-suite",
                suite_version="1.0.0",
                harness="codex",
                initiator="author",
                evidence="fixture",
                is_test=False,
            )
        with self.assertRaisesRegex(SelcError, "unsupported event"):
            records.set_task_event(database, "task", "opened", "unsupported")
        records.set_task_event(database, "task", "session_start", "host session")
        self.assertEqual(records.active_leases(database, "suite")["sessions"], 1)
        records.set_task_event(database, "task", "session_close", "host session closed")
        self.assertEqual(records.active_leases(database, "suite")["sessions"], 0)

        with self.assertRaisesRegex(SelcError, "task does not exist"):
            records.start_call(
                database,
                call_id="missing-task",
                task_id="no-task",
                skill="report",
                executor="codex",
                evidence="fixture",
                expected_version="1.0.0",
                expected_hash=None,
                trigger_kind="explicit",
            )
        records.start_call(
            database,
            call_id="call",
            task_id="task",
            skill="report",
            executor="codex",
            evidence="fixture",
            expected_version="1.0.0",
            expected_hash=None,
            trigger_kind="explicit",
        )
        self.assertTrue(
            records.start_call(
                database,
                call_id="call",
                task_id="task",
                skill="report",
                executor="codex",
                evidence="fixture",
                expected_version="1.0.0",
                expected_hash=None,
                trigger_kind="explicit",
            )["replayed"]
        )
        with self.assertRaisesRegex(SelcError, "already belongs"):
            records.start_call(
                database,
                call_id="call",
                task_id="task",
                skill="other-report",
                executor="codex",
                evidence="fixture",
                expected_version="1.0.0",
                expected_hash=None,
                trigger_kind="explicit",
            )
        with self.assertRaisesRegex(SelcError, "does not exist"):
            records.complete_call(database, "missing-call", "pass")
        records.complete_call(database, "call", "pass")
        with self.assertRaisesRegex(SelcError, "already has result"):
            records.complete_call(database, "call", "fail")

        with self.assertRaisesRegex(SelcError, "same task"):
            records.record_delivery(
                database,
                delivery_id="bad-link",
                task_id="task",
                call_id="missing-call",
                artifact_hash="artifact-bad-link",
                rule_version="rule-v1",
                declared_result="pass",
                complete_fixture=False,
            )
        with self.assertRaisesRegex(SelcError, "limited to test tasks"):
            records.record_delivery(
                database,
                delivery_id="not-a-fixture",
                task_id="task",
                artifact_hash="artifact-fixture",
                rule_version="rule-v1",
                declared_result="pass",
                complete_fixture=True,
            )

        test_task = "test-task"
        records.start_task(
            database,
            task_id=test_task,
            suite_id="suite",
            suite_version="1.0.0",
            harness="codex",
            initiator="author",
            evidence="fixture",
            is_test=True,
        )
        records.set_task_event(database, test_task, "input_ready", "inputs supplied")
        records.record_delivery(
            database,
            delivery_id="test-delivery",
            task_id=test_task,
            artifact_hash="artifact-test",
            rule_version="rule-v1",
            declared_result="pass",
            complete_fixture=True,
        )
        with self.assertRaisesRegex(SelcError, "must bind an existing delivery"):
            records.record_feedback(
                database,
                feedback_id="missing-delivery",
                task_id=test_task,
                delivery_id="no-delivery",
                revision=1,
                scope="final",
                result="fail",
                body=None,
            )
        records.record_feedback(
            database,
            feedback_id="feedback",
            task_id=test_task,
            delivery_id="test-delivery",
            revision=1,
            scope="final",
            result="unknown",
            body=None,
        )
        self.assertTrue(
            records.record_feedback(
                database,
                feedback_id="feedback",
                task_id=test_task,
                delivery_id="test-delivery",
                revision=1,
                scope="final",
                result="unknown",
                body=None,
            )["replayed"]
        )
        with self.assertRaisesRegex(SelcError, "expected feedback revision"):
            records.record_feedback(
                database,
                feedback_id="skipped-revision",
                task_id=test_task,
                delivery_id="test-delivery",
                revision=3,
                scope="final",
                result="pass",
                body="too far ahead",
            )
        records.close_task(database, test_task)
        self.assertTrue(records.close_task(database, test_task)["replayed"])

    def test_completed_call_replay_precedes_closed_task_rejection(self) -> None:
        database = self.database("completed-call-replay")
        self.start_task(database)
        call = {
            "call_id": "call",
            "task_id": "task",
            "skill": "report",
            "executor": "codex",
            "evidence": "fixture",
            "expected_version": "1.0.0",
            "expected_hash": "digest",
            "trigger_kind": "explicit",
        }
        records.start_call(database, **call)
        records.complete_call(database, "call", "unknown")
        records.close_task(database, "task")

        self.assertTrue(records.start_call(database, **call)["replayed"])
        with self.assertRaises(SelcError) as new_call:
            records.start_call(database, **{**call, "call_id": "new-call"})
        self.assertEqual(new_call.exception.code, "TASK_CLOSED")
        with self.assertRaises(SelcError) as changed_call:
            records.start_call(database, **{**call, "evidence": "other evidence"})
        self.assertEqual(changed_call.exception.code, "ID_CONFLICT")

    def test_delivery_replay_binds_checker_identity_and_legacy_rows_are_unverifiable(self) -> None:
        database = self.database("delivery-replay")
        self.start_task(database)
        delivery = {
            "delivery_id": "delivery",
            "task_id": "task",
            "artifact_hash": "artifact",
            "rule_version": "text-report-v1",
            "declared_result": "pass",
            "complete_fixture": False,
            "checker_id": "text-report-v1",
            "checker_verified": True,
            "checker_result": "pass",
            "checker_identity": {
                "checker_input_hash": "input-a",
                "checker_artifact_hash": "artifact-a",
                "checker_resource_hash": "resource-a",
                "checker_composite_hash": "digest-a",
            },
        }
        self.assertFalse(records.record_delivery(database, **delivery)["replayed"])
        self.assertTrue(records.record_delivery(database, **delivery)["replayed"])
        changed = dict(delivery)
        changed["checker_identity"] = {
            **delivery["checker_identity"],
            "checker_input_hash": "input-b",
        }
        with self.assertRaises(SelcError) as checker_conflict:
            records.record_delivery(database, **changed)
        self.assertEqual(checker_conflict.exception.code, "ID_CONFLICT")
        stored = database.execute(
            "SELECT checker_input_hash FROM deliveries WHERE delivery_id = 'delivery'"
        ).fetchone()
        self.assertEqual(stored["checker_input_hash"], "input-a")

        database.execute(
            "UPDATE deliveries SET checker_identity_version = 0 WHERE delivery_id = 'delivery'"
        )
        database.commit()
        with self.assertRaises(SelcError) as legacy_replay:
            records.record_delivery(database, **delivery)
        self.assertEqual(legacy_replay.exception.code, "ID_CONFLICT")

    def test_import_rejects_malformed_types_before_noop_or_write(self) -> None:
        source = self.database("malformed-source")
        self.start_task(source, is_test=False)
        snapshot_path = self.root / "valid-snapshot.json"
        records.export_snapshot(source, snapshot_path)
        original = read_json(snapshot_path)
        target = self.database("malformed-target")

        malformed_payloads = []
        bool_schema = dict(original)
        bool_schema["schema_version"] = True
        malformed_payloads.append(bool_schema)
        bool_revision = dict(original)
        bool_revision["snapshot_revision"] = True
        malformed_payloads.append(bool_revision)
        list_result = json.loads(json.dumps(original))
        list_result["tasks"][0]["first_result"] = []
        malformed_payloads.append(list_result)

        for index, payload in enumerate(malformed_payloads):
            with self.subTest(index=index):
                payload["snapshot_hash"] = json_hash(
                    {key: value for key, value in payload.items() if key != "snapshot_hash"}
                )
                invalid_path = self.root / f"invalid-{index}.json"
                write_json(invalid_path, payload)
                with self.assertRaises(SelcError) as captured:
                    records.import_snapshot(target, invalid_path)
                self.assertIn(captured.exception.code, {"UNSUPPORTED_SCHEMA", "INVALID_SNAPSHOT"})
                self.assertEqual(
                    target.execute("SELECT COUNT(*) FROM imported_snapshots").fetchone()[0], 0
                )
                self.assertEqual(
                    records.stats(target, include_test=False)["tasks"]["final"]["N"], 0
                )

    def test_snapshot_revisions_reject_tampering_and_keep_newer_samples(self) -> None:
        source = self.database("snapshot-source")
        self.start_task(source, is_test=False)
        snapshot_path = self.root / "snapshot.json"
        records.export_snapshot(source, snapshot_path)
        snapshot = read_json(snapshot_path)
        target = self.database("snapshot-target")
        records.import_snapshot(target, snapshot_path)
        self.assertEqual(
            records.import_snapshot(target, snapshot_path)["imported"], "noop_same_revision"
        )

        tampered = dict(snapshot)
        tampered["snapshot_hash"] = "not-a-real-hash"
        tampered_path = self.root / "tampered.json"
        write_json(tampered_path, tampered)
        with self.assertRaisesRegex(SelcError, "content hash"):
            records.import_snapshot(target, tampered_path)

        newer = dict(snapshot)
        newer["snapshot_revision"] = 2
        newer["tasks"] = [dict(snapshot["tasks"][0], current_result="unknown")]
        newer["snapshot_hash"] = json_hash(
            {key: value for key, value in newer.items() if key != "snapshot_hash"}
        )
        newer_path = self.root / "newer.json"
        write_json(newer_path, newer)
        self.assertEqual(records.import_snapshot(target, newer_path)["snapshot_revision"], 2)

        older = dict(snapshot)
        older["snapshot_hash"] = json_hash(
            {key: value for key, value in older.items() if key != "snapshot_hash"}
        )
        older_path = self.root / "older.json"
        write_json(older_path, older)
        self.assertEqual(
            records.import_snapshot(target, older_path)["imported"], "ignored_older_revision"
        )

        conflicting = dict(newer)
        conflicting["tasks"] = [dict(newer["tasks"][0], suite_id="other-suite")]
        conflicting["snapshot_hash"] = json_hash(
            {key: value for key, value in conflicting.items() if key != "snapshot_hash"}
        )
        conflicting_path = self.root / "conflicting.json"
        write_json(conflicting_path, conflicting)
        with self.assertRaisesRegex(SelcError, "same origin and revision"):
            records.import_snapshot(target, conflicting_path)

    def test_legacy_feedback_rows_migrate_into_independent_threads(self) -> None:
        project = self.root / "legacy-feedback"
        database = records.open_database(project)
        self.start_task(database, is_test=True)
        records.record_delivery(
            database,
            delivery_id="legacy-delivery",
            task_id="task",
            artifact_hash="legacy-artifact",
            rule_version="rule-v1",
            declared_result="pass",
            complete_fixture=True,
        )
        database.execute(
            """INSERT INTO feedback(
            feedback_id, task_id, delivery_id, revision, scope, result, body_hash, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "legacy-feedback",
                "task",
                "legacy-delivery",
                1,
                "final",
                "fail",
                None,
                records.utc_now(),
            ),
        )
        database.commit()
        database.close()

        migrated = records.open_database(project)
        row = migrated.execute(
            """SELECT feedback_id, revision, scope, result FROM feedback_revisions
            WHERE feedback_id = 'legacy-feedback'"""
        ).fetchone()
        self.assertEqual(tuple(row), ("legacy-feedback", 1, "final", "fail"))
        self.assertEqual(self.task_outcome(migrated), ("fail", "fail"))
        migrated.close()
