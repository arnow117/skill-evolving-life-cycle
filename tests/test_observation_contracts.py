"""Contract tests for durable observation ingestion and reduction."""

from __future__ import annotations

import io
import json
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from selc import observation, records
from selc.errors import SelcError
from selc.util import json_hash, write_json


class ObservationContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def database(self):
        return records.open_database(self.root / "project")

    def event(
        self,
        *,
        kind: str,
        native_event_id: str | None,
        facts: dict[str, object],
        phase: str = "lifecycle",
        occurred_at: str = "2026-10-20T02:00:00Z",
        source_context: str = "context-a",
        source_epoch: int = 1,
    ) -> dict[str, object]:
        return {
            "protocol": 1,
            "adapter": {"name": "fixture-adapter", "version": "1"},
            "identity": {
                "installation_id": "installation-a",
                "host": "codex",
                "surface": "test",
                "session_id": "session-a",
                "turn_id": "turn-a",
                "native_event_id": native_event_id,
                "phase": phase,
            },
            "source_context": {"id": source_context, "epoch": source_epoch},
            "kind": kind,
            "occurred_at": occurred_at,
            "classification": {
                "version": "observation-v1",
                "facts": facts,
                "evidence": ["fixture"],
            },
        }

    def save_formal_pass(
        self,
        database: sqlite3.Connection,
        *,
        task_id: str = "task-a",
        call_id: str = "call-a",
        delivery_id: str = "delivery-a",
        publication_ordinal: int = 1,
        predecessor_ordinal: int | None = None,
        native_prefix: str = "normal",
        source_context: str = "context-a",
        source_epoch: int = 1,
        occurred_at: str = "2026-10-20T02:00:00Z",
        include_task_and_input: bool = True,
    ) -> None:
        """Persist a literal, independently specified normal formal fixture."""

        task = {
            "task_id": task_id,
            "suite_id": "suite-a",
            "suite_version": "1.0.0",
            "harness": "codex",
            "initiator": "author",
            "actor": "human",
            "origin": "managed_semantic",
            "causation": "original_request",
        }
        call = {
            "task_id": task_id,
            "call_id": call_id,
            "skill": "report",
            "executor": "codex",
            "expected_version": "1.0.0",
            "expected_hash": "expected-digest",
            "trigger_kind": "automatic",
            "actor": "agent",
            "origin": "managed_semantic",
            "causation": "original_request",
        }
        delivery = {
            "task_id": task_id,
            "call_id": call_id,
            "delivery_id": delivery_id,
            "artifact_hash": f"artifact-{delivery_id}",
            "rule_version": "rule-v1",
            "checker_id": "fixture-checker",
            "checker_result": "pass",
            "checker_input_hash": f"input-{delivery_id}",
            "checker_artifact_hash": f"artifact-{delivery_id}",
            "checker_resource_hash": "rule-digest",
            "checker_composite_hash": f"checker-{delivery_id}",
            "publication_ordinal": publication_ordinal,
            "predecessor_ordinal": predecessor_ordinal,
            "delivery_role": "user_visible",
            "actor": "agent",
            "origin": "managed_semantic",
            "causation": "original_request",
        }
        common = {
            "source_context": source_context,
            "source_epoch": source_epoch,
            "occurred_at": occurred_at,
        }
        if include_task_and_input:
            observation.ingest(
                database,
                self.event(
                    kind="task_started",
                    native_event_id=f"{native_prefix}-task",
                    facts=task,
                    **common,
                ),
            )
            observation.ingest(
                database,
                self.event(
                    kind="input_ready",
                    native_event_id=f"{native_prefix}-input",
                    facts={
                        "task_id": task_id,
                        "actor": "human",
                        "origin": "managed_semantic",
                        "causation": "input_preparation",
                    },
                    **common,
                ),
            )
        observation.ingest(
            database,
            self.event(kind="call_started", native_event_id=f"{native_prefix}-call", facts=call, **common),
        )
        observation.ingest(
            database,
            self.event(
                kind="delivery_published",
                native_event_id=f"{native_prefix}-delivery",
                facts=delivery,
                **common,
            ),
        )

    def test_saved_semantic_evidence_projects_a_formal_normal_pass(self) -> None:
        database = self.database()
        task = {
            "task_id": "task-a",
            "suite_id": "suite-a",
            "suite_version": "1.0.0",
            "harness": "codex",
            "initiator": "author",
            "actor": "human",
            "origin": "managed_semantic",
            "causation": "original_request",
        }
        call = {
            "task_id": "task-a",
            "call_id": "call-a",
            "skill": "report",
            "executor": "codex",
            "expected_version": "1.0.0",
            "expected_hash": "expected-digest",
            "trigger_kind": "automatic",
            "actor": "agent",
            "origin": "managed_semantic",
            "causation": "original_request",
        }
        delivery = {
            "task_id": "task-a",
            "call_id": "call-a",
            "delivery_id": "delivery-a",
            "artifact_hash": "artifact-digest",
            "rule_version": "rule-v1",
            "checker_id": "fixture-checker",
            "checker_result": "pass",
            "checker_input_hash": "input-digest",
            "checker_artifact_hash": "artifact-digest",
            "checker_resource_hash": "rule-digest",
            "checker_composite_hash": "checker-digest",
            "publication_ordinal": 1,
            "predecessor_ordinal": None,
            "delivery_role": "user_visible",
            "actor": "agent",
            "origin": "managed_semantic",
            "causation": "original_request",
        }

        observation.ingest(database, self.event(kind="task_started", native_event_id="task", facts=task))
        observation.ingest(
            database,
            self.event(
                kind="input_ready",
                native_event_id="input",
                facts={
                    "task_id": "task-a",
                    "actor": "human",
                    "origin": "managed_semantic",
                    "causation": "input_preparation",
                },
            ),
        )
        observation.ingest(
            database,
            self.event(
                kind="permission_granted",
                native_event_id="permission",
                facts={
                    "task_id": "task-a",
                    "actor": "human",
                    "origin": "managed_semantic",
                    "causation": "permission",
                },
            ),
        )
        observation.ingest(
            database,
            self.event(
                kind="automatic_retry",
                native_event_id="retry",
                facts={
                    "task_id": "task-a",
                    "actor": "agent",
                    "origin": "managed_semantic",
                    "causation": "automatic_retry",
                },
            ),
        )
        observation.ingest(database, self.event(kind="call_started", native_event_id="call", facts=call))
        observation.ingest(
            database,
            self.event(kind="delivery_published", native_event_id="delivery", facts=delivery),
        )

        assessment = observation.assessment(database, "task-a")
        self.assertEqual((assessment["first_result"], assessment["final_result"]), ("pass", "pass"))
        task_row = database.execute(
            "SELECT first_result, current_result FROM tasks WHERE task_id = 'task-a'"
        ).fetchone()
        self.assertEqual(tuple(task_row), ("pass", "pass"))
        observation.ingest(
            database,
            self.event(
                kind="business_intervention",
                native_event_id="post-delivery-note",
                phase="post_delivery",
                facts={
                    "task_id": "task-a",
                    "actor": "human",
                    "origin": "managed_semantic",
                    "causation": "business_correction",
                },
            ),
        )
        post_delivery_result = observation.assessment(database, "task-a")
        self.assertEqual(
            (post_delivery_result["first_result"], post_delivery_result["final_result"]),
            ("pass", "pass"),
        )

    def test_unknown_execution_does_not_erase_a_verified_final_or_known_failure(self) -> None:
        database = self.database()
        self.save_formal_pass(database)

        observation.ingest(
            database,
            self.event(
                kind="automatic_retry",
                native_event_id="uncertain-retry",
                phase="pre_delivery",
                facts={
                    "task_id": "task-a",
                    "call_id": "call-a",
                    "actor": "unknown",
                    "origin": "managed_semantic",
                    "causation": "unknown",
                },
            ),
        )
        task_assessment = observation.assessment(database, "task-a")
        call_assessment = database.execute(
            "SELECT first_result, result FROM calls WHERE call_id = 'call-a'"
        ).fetchone()
        self.assertEqual(
            (task_assessment["first_result"], task_assessment["final_result"]),
            ("unknown", "pass"),
        )
        self.assertEqual(tuple(call_assessment), ("unknown", "pass"))

        observation.ingest(
            database,
            self.event(
                kind="feedback",
                native_event_id="verified-fail",
                phase="post_delivery",
                facts={
                    "task_id": "task-a",
                    "delivery_id": "delivery-a",
                    "feedback_id": "review-a",
                    "feedback_revision": 1,
                    "feedback_scope": "final",
                    "feedback_result": "fail",
                    "feedback_body_hash": "review-digest",
                    "actor": "human",
                    "origin": "managed_semantic",
                    "causation": "voluntary_feedback",
                },
            ),
        )
        known_failure = observation.assessment(database, "task-a")
        self.assertEqual(
            (known_failure["first_result"], known_failure["final_result"]),
            ("fail", "fail"),
        )

    def test_unknown_checker_never_establishes_a_positive_formal_result(self) -> None:
        database = self.database()
        task = {
            "task_id": "task-a",
            "suite_id": "suite-a",
            "suite_version": "1.0.0",
            "harness": "codex",
            "initiator": "author",
            "actor": "human",
            "origin": "managed_semantic",
            "causation": "original_request",
        }
        delivery = {
            "task_id": "task-a",
            "delivery_id": "delivery-a",
            "artifact_hash": "artifact-digest",
            "rule_version": "rule-v1",
            "checker_id": "fixture-checker",
            "checker_result": "unknown",
            "checker_input_hash": "input-digest",
            "checker_artifact_hash": "artifact-digest",
            "checker_resource_hash": "rule-digest",
            "checker_composite_hash": "checker-digest",
            "publication_ordinal": 1,
            "predecessor_ordinal": None,
            "delivery_role": "user_visible",
            "actor": "agent",
            "origin": "managed_semantic",
            "causation": "original_request",
        }
        observation.ingest(database, self.event(kind="task_started", native_event_id="task", facts=task))
        observation.ingest(
            database,
            self.event(
                kind="input_ready",
                native_event_id="input",
                facts={
                    "task_id": "task-a",
                    "actor": "human",
                    "origin": "managed_semantic",
                    "causation": "input_preparation",
                },
            ),
        )
        observation.ingest(
            database,
            self.event(kind="delivery_published", native_event_id="delivery", facts=delivery),
        )
        result = observation.assessment(database, "task-a")
        self.assertEqual((result["first_result"], result["final_result"]), ("unknown", "unknown"))

        with self.assertRaisesRegex(SelcError, "unsupported fields"):
            observation.ingest(
                database,
                self.event(
                    kind="delivery_published",
                    native_event_id="forged",
                    facts={**delivery, "complete": "true"},
                ),
            )

        invalid_protocol = self.event(kind="automatic_retry", native_event_id="protocol", facts={})
        invalid_protocol["protocol"] = 2
        with self.assertRaisesRegex(SelcError, "protocol is unsupported"):
            observation.ingest(database, invalid_protocol)
        oversized_fact = self.event(
            kind="automatic_retry",
            native_event_id="oversized",
            facts={"task_id": "x" * (observation.MAX_FACT_VALUE_BYTES + 1)},
        )
        with self.assertRaisesRegex(SelcError, "task_id is too large"):
            observation.ingest(database, oversized_fact)

    def test_publication_order_replays_d2_after_d1_and_keeps_call_results_isolated(self) -> None:
        database = self.database()
        task = {
            "task_id": "task-a",
            "suite_id": "suite-a",
            "suite_version": "1.0.0",
            "harness": "codex",
            "initiator": "author",
            "actor": "human",
            "origin": "managed_semantic",
            "causation": "original_request",
        }
        observation.ingest(database, self.event(kind="task_started", native_event_id="task", facts=task))
        observation.ingest(
            database,
            self.event(
                kind="input_ready",
                native_event_id="input",
                facts={
                    "task_id": "task-a",
                    "actor": "human",
                    "origin": "managed_semantic",
                    "causation": "input_preparation",
                },
            ),
        )
        for call_id in ("call-a", "call-b"):
            observation.ingest(
                database,
                self.event(
                    kind="call_started",
                    native_event_id=call_id,
                    facts={
                        "task_id": "task-a",
                        "call_id": call_id,
                        "skill": "report",
                        "executor": "codex",
                        "expected_version": "1.0.0",
                        "expected_hash": "expected-digest",
                        "trigger_kind": "automatic",
                        "actor": "agent",
                        "origin": "managed_semantic",
                        "causation": "original_request",
                    },
                ),
            )

        def delivery(delivery_id: str, call_id: str, ordinal: int, result: str) -> dict[str, object]:
            return {
                "task_id": "task-a",
                "call_id": call_id,
                "delivery_id": delivery_id,
                "artifact_hash": f"artifact-{delivery_id}",
                "rule_version": "rule-v1",
                "checker_id": "fixture-checker",
                "checker_result": result,
                "checker_input_hash": f"input-{delivery_id}",
                "checker_artifact_hash": f"artifact-{delivery_id}",
                "checker_resource_hash": "rule-digest",
                "checker_composite_hash": f"checker-{delivery_id}",
                "publication_ordinal": ordinal,
                "predecessor_ordinal": None if ordinal == 1 else ordinal - 1,
                "delivery_role": "user_visible",
                "actor": "agent",
                "origin": "managed_semantic",
                "causation": "original_request",
            }

        pending = observation.ingest(
            database,
            self.event(
                kind="delivery_published",
                native_event_id="d2",
                facts=delivery("d2", "call-a", 2, "pass"),
            ),
        )
        self.assertEqual(pending["replay"]["pending"], 1)
        observation.ingest(
            database,
            self.event(
                kind="delivery_published",
                native_event_id="d1",
                facts=delivery("d1", "call-a", 1, "fail"),
            ),
        )
        observation.ingest(
            database,
            self.event(
                kind="delivery_published",
                native_event_id="other",
                facts=delivery("other", "call-b", 3, "pass"),
            ),
        )

        task_result = observation.assessment(database, "task-a")
        self.assertEqual((task_result["first_result"], task_result["final_result"]), ("fail", "pass"))
        calls = database.execute(
            "SELECT call_id, first_result, result FROM calls ORDER BY call_id"
        ).fetchall()
        self.assertEqual(
            [tuple(row) for row in calls],
            [("call-a", "fail", "pass"), ("call-b", "pass", "pass")],
        )

    def test_stable_conflict_keeps_original_and_missing_native_ids_do_not_merge(self) -> None:
        database = self.database()
        task = {
            "task_id": "task-a",
            "suite_id": "suite-a",
            "suite_version": "1.0.0",
            "harness": "codex",
            "initiator": "author",
            "actor": "human",
            "origin": "managed_semantic",
            "causation": "original_request",
        }
        first = observation.ingest(
            database, self.event(kind="task_started", native_event_id="same", facts=task)
        )
        replay = observation.ingest(
            database, self.event(kind="task_started", native_event_id="same", facts=task)
        )
        conflict = observation.ingest(
            database,
            self.event(
                kind="task_started", native_event_id="same", facts={**task, "suite_id": "other"}
            ),
        )
        self.assertTrue(replay["replayed"])
        self.assertEqual(conflict["status"], "conflict")
        self.assertEqual(
            database.execute("SELECT COUNT(*) FROM observation_events").fetchone()[0], 1
        )
        self.assertEqual(
            database.execute("SELECT COUNT(*) FROM observation_conflicts").fetchone()[0], 1
        )
        self.assertEqual(first["event_id"], replay["event_id"])

        no_id = self.event(kind="automatic_retry", native_event_id=None, facts={"task_id": "task-a"})
        observation.ingest(database, no_id)
        observation.ingest(database, no_id)
        self.assertEqual(
            database.execute(
                "SELECT COUNT(*) FROM observation_events WHERE stable_identity IS NULL"
            ).fetchone()[0],
            2,
        )

    def test_conflicting_call_evidence_invalidates_only_its_assessment(self) -> None:
        database = self.database()
        self.save_formal_pass(database, native_prefix="call-a")
        self.save_formal_pass(
            database,
            call_id="call-b",
            delivery_id="delivery-b",
            publication_ordinal=2,
            predecessor_ordinal=1,
            native_prefix="call-b",
            include_task_and_input=False,
        )
        baseline = database.execute(
            "SELECT call_id, first_result, result FROM calls ORDER BY call_id"
        ).fetchall()
        self.assertEqual(
            [tuple(row) for row in baseline],
            [("call-a", "pass", "pass"), ("call-b", "pass", "pass")],
        )

        conflicting_call = self.event(
            kind="call_started",
            native_event_id="call-a-call",
            facts={
                "task_id": "task-a",
                "call_id": "call-a",
                "skill": "conflicting-skill",
                "executor": "codex",
                "expected_version": "1.0.0",
                "expected_hash": "expected-digest",
                "trigger_kind": "automatic",
                "actor": "agent",
                "origin": "managed_semantic",
                "causation": "original_request",
            },
        )
        conflict = observation.ingest(database, conflicting_call)
        self.assertEqual(conflict["status"], "conflict")
        immediate_task = observation.assessment(database, "task-a")
        immediate_calls = database.execute(
            "SELECT call_id, first_result, result FROM calls ORDER BY call_id"
        ).fetchall()
        self.assertEqual(
            (immediate_task["first_result"], immediate_task["final_result"]),
            ("unknown", "pass"),
        )
        self.assertEqual(
            [tuple(row) for row in immediate_calls],
            [("call-a", "unknown", "pass"), ("call-b", "pass", "pass")],
        )

        self.assertEqual(observation.replay(database)["pending"], 0)
        database.close()
        reopened = records.open_database(self.root / "project")
        self.assertEqual(observation.replay(reopened)["pending"], 0)
        reopened_task = observation.assessment(reopened, "task-a")
        reopened_calls = reopened.execute(
            "SELECT call_id, first_result, result FROM calls ORDER BY call_id"
        ).fetchall()
        self.assertEqual(
            (reopened_task["first_result"], reopened_task["final_result"]),
            ("unknown", "pass"),
        )
        self.assertEqual(
            [tuple(row) for row in reopened_calls],
            [("call-a", "unknown", "pass"), ("call-b", "pass", "pass")],
        )
        reopened.close()

    def test_conflicted_current_delivery_cannot_establish_a_formal_pass(self) -> None:
        database = self.database()
        self.save_formal_pass(database)
        conflicting_delivery = observation.ingest(
            database,
            self.event(
                kind="delivery_published",
                native_event_id="normal-delivery",
                facts={
                    "task_id": "task-a",
                    "call_id": "call-a",
                    "delivery_id": "delivery-a",
                    "artifact_hash": "artifact-delivery-a",
                    "rule_version": "rule-v1",
                    "checker_id": "fixture-checker",
                    "checker_result": "fail",
                    "checker_input_hash": "input-delivery-a",
                    "checker_artifact_hash": "artifact-delivery-a",
                    "checker_resource_hash": "rule-digest",
                    "checker_composite_hash": "checker-delivery-a",
                    "publication_ordinal": 1,
                    "predecessor_ordinal": None,
                    "delivery_role": "user_visible",
                    "actor": "agent",
                    "origin": "managed_semantic",
                    "causation": "original_request",
                },
            ),
        )
        self.assertEqual(conflicting_delivery["status"], "conflict")
        task_assessment = observation.assessment(database, "task-a")
        call_assessment = database.execute(
            "SELECT first_result, result FROM calls WHERE call_id = 'call-a'"
        ).fetchone()
        self.assertEqual(
            (task_assessment["first_result"], task_assessment["final_result"]),
            ("unknown", "unknown"),
        )
        self.assertEqual(tuple(call_assessment), ("unknown", "unknown"))

    def test_raw_conflict_refreshes_after_a_crash_before_ingest_can_assess(self) -> None:
        database = self.database()
        self.save_formal_pass(database, native_prefix="call-a")
        raw_conflict = self.event(
            kind="call_started",
            native_event_id="call-a-call",
            facts={
                "task_id": "task-a",
                "call_id": "call-a",
                "skill": "conflicting-skill",
                "executor": "codex",
                "expected_version": "1.0.0",
                "expected_hash": "expected-digest",
                "trigger_kind": "automatic",
                "actor": "agent",
                "origin": "managed_semantic",
                "causation": "original_request",
            },
        )
        stored = observation._store_raw(database, observation._normalize_envelope(raw_conflict))
        self.assertEqual(stored["status"], "conflict")
        database.close()

        reopened = records.open_database(self.root / "project")
        self.assertEqual(observation.replay(reopened)["pending"], 0)
        task_assessment = observation.assessment(reopened, "task-a")
        call_assessment = reopened.execute(
            "SELECT first_result, result FROM calls WHERE call_id = 'call-a'"
        ).fetchone()
        self.assertEqual(
            (task_assessment["first_result"], task_assessment["final_result"]),
            ("unknown", "pass"),
        )
        self.assertEqual(tuple(call_assessment), ("unknown", "pass"))
        reopened.close()

    def test_unresolved_dependency_and_quarantine_refresh_assessment_evidence(self) -> None:
        database = self.database()
        self.save_formal_pass(database)
        pending = observation.ingest(
            database,
            self.event(
                kind="call_completed",
                native_event_id="unresolved",
                facts={
                    "task_id": "task-a",
                    "call_id": "not-started",
                    "result": "unknown",
                    "actor": "unknown",
                    "origin": "native_unverified",
                    "causation": "unknown",
                },
            ),
        )
        self.assertEqual(pending["status"], "pending")
        pending_task = observation.assessment(database, "task-a")
        unrelated_call = database.execute(
            "SELECT first_result, result FROM calls WHERE call_id = 'call-a'"
        ).fetchone()
        self.assertEqual(
            (pending_task["first_result"], pending_task["final_result"]),
            ("unknown", "pass"),
        )
        self.assertEqual(tuple(unrelated_call), ("pass", "pass"))
        database.close()

        reopened = records.open_database(self.root / "project")
        self.assertEqual(observation.replay(reopened)["pending"], 1)
        reopened_task = observation.assessment(reopened, "task-a")
        self.assertEqual(
            (reopened_task["first_result"], reopened_task["final_result"]),
            ("unknown", "pass"),
        )
        reopened.close()

        quarantined_database = self.database()
        self.save_formal_pass(quarantined_database)
        quarantined_database.execute(
            """INSERT INTO observation_source_epochs(
            source_context, invalid_before_epoch, deleted_at) VALUES (?, ?, ?)""",
            ("quarantined-source", 1, "2026-10-20T02:00:00Z"),
        )
        quarantined_database.commit()
        quarantined = observation.ingest(
            quarantined_database,
            self.event(
                kind="automatic_retry",
                native_event_id="quarantined-retry",
                source_context="quarantined-source",
                facts={
                    "task_id": "task-a",
                    "actor": "agent",
                    "origin": "managed_semantic",
                    "causation": "automatic_retry",
                },
            ),
        )
        self.assertEqual(quarantined["status"], "quarantined")
        quarantined_task = observation.assessment(quarantined_database, "task-a")
        self.assertEqual(
            (quarantined_task["first_result"], quarantined_task["final_result"]),
            ("unknown", "pass"),
        )

    def test_native_identity_separates_derived_kinds_and_delimiter_fields(self) -> None:
        database = self.database()
        task = {
            "task_id": "task-a",
            "suite_id": "suite-a",
            "suite_version": "1.0.0",
            "harness": "codex",
            "initiator": "author",
            "actor": "human",
            "origin": "managed_semantic",
            "causation": "original_request",
        }
        call = {
            "task_id": "task-a",
            "call_id": "call-a",
            "skill": "report",
            "executor": "codex",
            "expected_version": "1.0.0",
            "expected_hash": "expected-digest",
            "trigger_kind": "automatic",
            "actor": "agent",
            "origin": "managed_semantic",
            "causation": "original_request",
        }
        task_event = self.event(kind="task_started", native_event_id="native-call", facts=task)
        call_event = self.event(kind="call_started", native_event_id="native-call", facts=call)
        task_result = observation.ingest(database, task_event)
        call_result = observation.ingest(database, call_event)
        self.assertNotEqual(task_result["event_id"], call_result["event_id"])
        self.assertEqual(
            database.execute("SELECT COUNT(*) FROM observation_events").fetchone()[0], 2
        )
        self.assertTrue(observation.ingest(database, task_event)["replayed"])
        conflict = observation.ingest(
            database,
            self.event(
                kind="task_started",
                native_event_id="native-call",
                facts={**task, "suite_id": "other-suite"},
            ),
        )
        self.assertEqual(conflict["status"], "conflict")

        first_delimited = self.event(
            kind="task_started",
            native_event_id="native",
            facts={**task, "task_id": "delimited-a"},
        )
        first_delimited["identity"] = {
            **first_delimited["identity"],
            "installation_id": "installation|host",
            "host": "codex",
        }
        second_delimited = self.event(
            kind="task_started",
            native_event_id="native",
            facts={**task, "task_id": "delimited-b"},
        )
        second_delimited["identity"] = {
            **second_delimited["identity"],
            "installation_id": "installation",
            "host": "host|codex",
        }
        first_identity = observation.ingest(database, first_delimited)["event_id"]
        second_identity = observation.ingest(database, second_delimited)["event_id"]
        self.assertNotEqual(first_identity, second_identity)

    def test_delete_quarantines_pending_old_context_but_fresh_epoch_can_start_again(self) -> None:
        database = self.database()
        pending = observation.ingest(
            database,
            self.event(
                kind="delivery_published",
                native_event_id=None,
                facts={
                    "task_id": "task-a",
                    "call_id": "missing-call",
                    "delivery_id": "late",
                    "artifact_hash": "artifact",
                    "rule_version": "rule-v1",
                    "checker_id": "check",
                    "checker_result": "pass",
                    "checker_input_hash": "input",
                    "checker_artifact_hash": "artifact",
                    "checker_resource_hash": "resource",
                    "checker_composite_hash": "composite",
                    "publication_ordinal": 1,
                    "predecessor_ordinal": None,
                    "delivery_role": "user_visible",
                    "actor": "agent",
                    "origin": "managed_semantic",
                    "causation": "original_request",
                },
            ),
        )
        self.assertEqual(pending["replay"]["pending"], 1)
        task = {
            "task_id": "task-a",
            "suite_id": "suite-a",
            "suite_version": "1.0.0",
            "harness": "codex",
            "initiator": "author",
            "actor": "human",
            "origin": "managed_semantic",
            "causation": "original_request",
        }
        observation.ingest(database, self.event(kind="task_started", native_event_id="task", facts=task))
        observation.ingest(
            database,
            self.event(kind="task_closed", native_event_id="closed", facts={"task_id": "task-a"}),
        )
        records.delete_records(
            database, suite_id="suite-a", before="2026-10-21T00:00:00Z", apply=True
        )
        late_dependency = observation.ingest(
            database,
            self.event(
                kind="call_started",
                native_event_id=None,
                facts={
                    "task_id": "task-a",
                    "call_id": "missing-call",
                    "skill": "report",
                    "executor": "codex",
                    "expected_version": "1.0.0",
                    "expected_hash": "digest",
                    "trigger_kind": "automatic",
                    "actor": "agent",
                    "origin": "managed_semantic",
                    "causation": "original_request",
                },
            ),
        )
        self.assertEqual(late_dependency["status"], "quarantined")

        old_event_from_another_arrival = observation.ingest(
            database,
            self.event(
                kind="automatic_retry",
                native_event_id="old-clock",
                source_context="unseen-context",
                occurred_at="2026-10-20T02:00:00Z",
                facts={"task_id": "task-a", "suite_id": "suite-a"},
            ),
        )
        self.assertEqual(old_event_from_another_arrival["status"], "quarantined")

        fresh_task = {
            **task,
            "task_id": "fresh-task",
        }
        fresh = observation.ingest(
            database,
            self.event(
                kind="task_started",
                native_event_id="fresh",
                facts=fresh_task,
                occurred_at="2026-10-22T02:00:00Z",
                source_epoch=2,
            ),
        )
        self.assertEqual(fresh["status"], "pending")
        self.assertEqual(
            database.execute("SELECT COUNT(*) FROM tasks WHERE task_id = 'fresh-task'").fetchone()[0], 1
        )

    def test_full_history_delete_captures_a_boundary_without_blocking_fresh_contexts(self) -> None:
        database = self.database()
        self.save_formal_pass(database)
        records.complete_call(database, "call-a", "pass")
        records.close_task(database, "task-a")

        with patch.object(records, "utc_now", return_value="2026-10-20T02:00:10Z"):
            records.delete_records(database, suite_id="suite-a", before=None, apply=True)

        deletion_range = database.execute(
            "SELECT before_time FROM observation_deletion_ranges WHERE suite_id = 'suite-a'"
        ).fetchone()
        self.assertEqual(deletion_range["before_time"], "2026-10-20T02:00:10Z")

        fresh = observation.ingest(
            database,
            self.event(
                kind="task_started",
                native_event_id="fresh-context-task",
                source_context="fresh-context",
                source_epoch=2,
                occurred_at="2026-10-20T02:00:11Z",
                facts={
                    "task_id": "fresh-task",
                    "suite_id": "suite-a",
                    "suite_version": "1.0.0",
                    "harness": "codex",
                    "initiator": "author",
                    "actor": "human",
                    "origin": "managed_semantic",
                    "causation": "original_request",
                },
            ),
        )
        self.assertEqual(fresh["status"], "pending")
        self.assertEqual(
            database.execute("SELECT COUNT(*) FROM tasks WHERE task_id = 'fresh-task'").fetchone()[0],
            1,
        )

        resurrected = observation.ingest(
            database,
            self.event(
                kind="task_started",
                native_event_id="old-subject-new-context",
                source_context="fresh-context",
                source_epoch=2,
                occurred_at="2026-10-20T02:00:11Z",
                facts={
                    "task_id": "task-a",
                    "suite_id": "suite-a",
                    "suite_version": "1.0.0",
                    "harness": "codex",
                    "initiator": "author",
                    "actor": "human",
                    "origin": "managed_semantic",
                    "causation": "original_request",
                },
            ),
        )
        self.assertEqual(resurrected["status"], "quarantined")
        self.assertEqual(
            database.execute("SELECT COUNT(*) FROM tasks WHERE task_id = 'task-a'").fetchone()[0],
            0,
        )

    def test_future_protocol_refuses_before_writes(self) -> None:
        database = self.database()
        path = self.root / "project" / ".selc" / "records.sqlite3"
        task_count = database.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        database.execute("UPDATE meta SET value = '3' WHERE key = 'writer_protocol'")
        database.commit()
        database.close()
        with self.assertRaisesRegex(SelcError, "newer SELC runtime"):
            records.open_database(self.root / "project")
        raw = sqlite3.connect(path)
        self.assertEqual(
            raw.execute("SELECT value FROM meta WHERE key = 'writer_protocol'").fetchone()[0], "3"
        )
        self.assertEqual(raw.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], task_count)
        raw.close()

    def test_future_protocol_rejection_closes_the_opened_connection(self) -> None:
        database = self.database()
        path = self.root / "project" / ".selc" / "records.sqlite3"
        database.execute("UPDATE meta SET value = '3' WHERE key = 'writer_protocol'")
        database.commit()
        database.close()

        class TrackingConnection(sqlite3.Connection):
            was_closed = False

            def close(self) -> None:
                self.was_closed = True
                super().close()

        original_connect = records.sqlite3.connect
        opened: list[TrackingConnection] = []

        def tracked_connect(*args: object, **kwargs: object) -> TrackingConnection:
            connection = original_connect(*args, factory=TrackingConnection, **kwargs)
            opened.append(connection)
            return connection

        with patch.object(records.sqlite3, "connect", side_effect=tracked_connect):
            with self.assertRaisesRegex(SelcError, "newer SELC runtime"):
                records.open_database(self.root / "project")
        self.assertTrue(opened[0].was_closed)
        self.assertEqual(path, records.database_path(self.root / "project"))

    def test_raw_commit_survives_a_reducer_crash_and_replays_after_reopen(self) -> None:
        database = self.database()
        event = self.event(
            kind="task_started",
            native_event_id="durable-task",
            facts={
                "task_id": "task-a",
                "suite_id": "suite-a",
                "suite_version": "1.0.0",
                "harness": "codex",
                "initiator": "author",
                "actor": "human",
                "origin": "managed_semantic",
                "causation": "original_request",
            },
        )
        stored = observation._store_raw(database, observation._normalize_envelope(event))
        self.assertEqual(stored["status"], "pending")
        database.close()

        recovered = records.open_database(self.root / "project")
        self.assertEqual(observation.replay(recovered)["applied"], 1)
        raw = recovered.execute(
            "SELECT event_digest, classification_version, facts_json, status FROM observation_events"
        ).fetchone()
        self.assertEqual(len(raw["event_digest"]), 64)
        self.assertEqual(raw["classification_version"], "observation-v1")
        self.assertEqual(json.loads(raw["facts_json"])["task_id"], "task-a")
        self.assertEqual(raw["status"], "applied")
        self.assertEqual(
            recovered.execute("SELECT COUNT(*) FROM tasks WHERE task_id = 'task-a'").fetchone()[0],
            1,
        )
        recovered.close()

    def test_projection_error_keeps_the_raw_event_for_recovery(self) -> None:
        database = self.database()
        unprojectable = self.event(
            kind="task_started",
            native_event_id="incomplete-task",
            facts={"task_id": "task-a"},
        )
        with self.assertRaisesRegex(SelcError, "suite_id is required"):
            observation.ingest(database, unprojectable)
        raw = database.execute(
            "SELECT status, facts_json FROM observation_events WHERE native_event_id = 'incomplete-task'"
        ).fetchone()
        self.assertEqual(raw["status"], "pending")
        self.assertEqual(json.loads(raw["facts_json"]), {"task_id": "task-a"})

    def test_started_interruption_is_retained_as_an_unknown_task_and_call(self) -> None:
        database = self.database()
        observation.ingest(
            database,
            self.event(
                kind="task_started",
                native_event_id="task",
                facts={
                    "task_id": "task-a",
                    "suite_id": "suite-a",
                    "suite_version": "1.0.0",
                    "harness": "codex",
                    "initiator": "author",
                    "actor": "human",
                    "origin": "managed_semantic",
                    "causation": "original_request",
                },
            ),
        )
        observation.ingest(
            database,
            self.event(
                kind="call_started",
                native_event_id="call",
                facts={
                    "task_id": "task-a",
                    "call_id": "call-a",
                    "skill": "report",
                    "executor": "codex",
                    "expected_version": "1.0.0",
                    "expected_hash": "expected-digest",
                    "trigger_kind": "automatic",
                    "actor": "agent",
                    "origin": "managed_semantic",
                    "causation": "original_request",
                },
            ),
        )
        observation.ingest(
            database,
            self.event(
                kind="task_interrupted",
                native_event_id="interrupted",
                facts={"task_id": "task-a", "call_id": "call-a"},
            ),
        )
        task = observation.assessment(database, "task-a")
        call = database.execute(
            "SELECT status, first_result, result FROM calls WHERE call_id = 'call-a'"
        ).fetchone()
        self.assertEqual((task["first_result"], task["final_result"]), ("unknown", "unknown"))
        self.assertEqual(tuple(call), ("closed", "unknown", "unknown"))
        self.assertEqual(records.stats(database, include_test=False)["tasks"]["final"]["U"], 1)

    def test_second_writer_fails_within_the_configured_busy_window(self) -> None:
        database = self.database()
        path = self.root / "project" / ".selc" / "records.sqlite3"
        lock_holder = sqlite3.connect(path)
        lock_holder.execute("BEGIN IMMEDIATE")
        try:
            started = time.monotonic()
            with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                observation.ingest(
                    database,
                    self.event(
                        kind="task_started",
                        native_event_id="blocked-task",
                        facts={
                            "task_id": "task-a",
                            "suite_id": "suite-a",
                            "suite_version": "1.0.0",
                            "harness": "codex",
                            "initiator": "author",
                            "actor": "human",
                            "origin": "managed_semantic",
                            "causation": "original_request",
                        },
                    ),
                )
            self.assertLess(time.monotonic() - started, 1.0)
        finally:
            lock_holder.rollback()
            lock_holder.close()

    def test_migration_is_idempotent_and_frozen_legacy_writer_cannot_mutate(self) -> None:
        database = self.database()
        records.start_task(
            database,
            task_id="legacy-task",
            suite_id="suite-a",
            suite_version="1.0.0",
            harness="codex",
            initiator="author",
            evidence="legacy",
            is_test=False,
        )
        records.start_task(
            database,
            task_id="feedback-task",
            suite_id="suite-a",
            suite_version="1.0.0",
            harness="codex",
            initiator="author",
            evidence="legacy",
            is_test=False,
        )
        records.record_delivery(
            database,
            delivery_id="legacy-delivery",
            task_id="feedback-task",
            artifact_hash="artifact",
            rule_version="rule-v1",
            declared_result="pass",
            complete_fixture=False,
        )
        records.record_feedback(
            database,
            feedback_id="legacy-feedback",
            task_id="feedback-task",
            delivery_id="legacy-delivery",
            revision=1,
            scope="final",
            result="fail",
            body="preserved only as a digest",
        )
        path = self.root / "project" / ".selc" / "records.sqlite3"
        database.close()

        raw = sqlite3.connect(path)
        trigger_names = raw.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'writer_protocol_%'"
        ).fetchall()
        for (name,) in trigger_names:
            raw.execute(f'DROP TRIGGER "{name}"')
        for table in (
            "observation_call_assessments",
            "observation_assessments",
            "observation_conflicts",
            "observation_events",
            "observation_tombstones",
            "observation_deletion_ranges",
            "observation_source_epochs",
        ):
            raw.execute(f"DROP TABLE {table}")
        raw.execute("DELETE FROM meta WHERE key = 'writer_protocol'")
        raw.commit()
        raw.close()

        migrated = records.open_database(self.root / "project")
        self.assertEqual(
            migrated.execute("SELECT value FROM meta WHERE key = 'writer_protocol'").fetchone()[0],
            "2",
        )
        legacy_task = migrated.execute(
            "SELECT task_id, first_result, current_result FROM tasks WHERE task_id = 'legacy-task'"
        ).fetchone()
        self.assertEqual(tuple(legacy_task), ("legacy-task", None, None))
        self.assertEqual(records.stats(migrated, include_test=False)["tasks"]["final"]["U"], 1)
        feedback = migrated.execute(
            "SELECT feedback_id, revision, result FROM feedback_revisions WHERE feedback_id = 'legacy-feedback'"
        ).fetchone()
        self.assertEqual(tuple(feedback), ("legacy-feedback", 1, "fail"))
        migrated.close()

        frozen_root = self.root / "frozen-runtime"
        frozen_root.mkdir()
        archive = subprocess.run(
            ["git", "archive", "--format=tar", "a311224", "src/selc"],
            check=True,
            cwd=REPO_ROOT,
            capture_output=True,
        )
        with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as bundle:
            bundle.extractall(frozen_root, filter="data")
        frozen_source = frozen_root / "src"
        script = "\n".join(
            (
                "import sys",
                f"sys.path.insert(0, {str(frozen_source)!r})",
                "from pathlib import Path",
                "from selc import records",
                f"database = records.open_database(Path({str(self.root / 'project')!r}))",
                "records.start_task(database, task_id='old-writer-task', suite_id='suite-a', "
                "suite_version='1.0.0', harness='codex', initiator='author', "
                "evidence='legacy', is_test=False)",
            )
        )
        frozen_writer = subprocess.run(
            [sys.executable, "-c", script],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(frozen_writer.returncode, 0)
        self.assertIn("selc_writer_protocol", frozen_writer.stderr)

        reopened = records.open_database(self.root / "project")
        self.assertEqual(
            reopened.execute("SELECT value FROM meta WHERE key = 'writer_protocol'").fetchone()[0],
            "2",
        )
        reopened.close()

    def test_delete_range_blocks_old_minimal_import_but_allows_newer_source(self) -> None:
        database = self.database()
        records.delete_records(
            database,
            suite_id="suite-a",
            before="2026-10-21T00:00:00Z",
            apply=True,
        )

        def snapshot(task_id: str, started_at: str, origin_id: str) -> Path:
            payload = {
                "schema_version": 1,
                "origin_id": origin_id,
                "snapshot_revision": 1,
                "as_of": "2026-10-22T02:00:00Z",
                "metric_contract": {
                    "groups": ["tasks.one_shot", "tasks.final", "calls.one_shot", "calls.final"],
                    "measures": ["N", "S", "K", "U", "S_over_N", "S_over_K", "K_over_N"],
                    "privacy": "result samples omit business artifacts, conversation, and feedback bodies",
                },
                "tasks": [
                    {
                        "task_id": task_id,
                        "suite_id": "suite-a",
                        "suite_version": "1.0.0",
                        "harness": "codex",
                        "is_test": False,
                        "started_at": started_at,
                        "first_result": "pass",
                        "current_result": "pass",
                    }
                ],
                "calls": [],
            }
            payload["snapshot_hash"] = json_hash(payload)
            path = self.root / f"{origin_id}.json"
            write_json(path, payload)
            return path

        records.import_snapshot(
            database, snapshot("old-task", "2026-10-20T02:00:00Z", "old-origin")
        )
        self.assertEqual(
            database.execute("SELECT COUNT(*) FROM imported_samples").fetchone()[0], 0
        )
        records.import_snapshot(
            database, snapshot("fresh-task", "2026-10-22T02:00:00Z", "fresh-origin")
        )
        imported = database.execute(
            "SELECT subject_id FROM imported_samples WHERE origin_id = 'fresh-origin'"
        ).fetchone()
        self.assertEqual(imported["subject_id"], "fresh-task")


if __name__ == "__main__":
    unittest.main()
