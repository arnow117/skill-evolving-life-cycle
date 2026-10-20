"""SQLite-backed task, invocation, result, and minimal-snapshot records."""

from __future__ import annotations

import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

from . import SCHEMA_VERSION
from .errors import SelcError
from .util import json_hash, read_json, write_json


WRITER_PROTOCOL_VERSION = 2


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def database_path(project_root: Path) -> Path:
    return project_root / ".selc" / "records.sqlite3"


def open_database(project_root: Path) -> sqlite3.Connection:
    project_root.mkdir(parents=True, exist_ok=True)
    path = database_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    database = sqlite3.connect(path)
    database.row_factory = sqlite3.Row
    _register_writer_protocol(database)
    database.execute("PRAGMA foreign_keys = ON")
    database.execute("PRAGMA busy_timeout = 250")
    try:
        _reject_future_writer_protocol(database)
        database.execute("BEGIN IMMEDIATE")
        _execute_schema_script(
            database,
            """
        CREATE TABLE IF NOT EXISTS meta (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tasks (
          task_id TEXT PRIMARY KEY,
          suite_id TEXT NOT NULL,
          suite_version TEXT NOT NULL DEFAULT 'unknown',
          harness TEXT NOT NULL DEFAULT 'unknown',
          initiator TEXT NOT NULL,
          evidence TEXT NOT NULL,
          is_test INTEGER NOT NULL,
          status TEXT NOT NULL,
          input_ready INTEGER NOT NULL DEFAULT 0,
          business_intervention INTEGER NOT NULL DEFAULT 0,
          first_result TEXT,
          current_result TEXT,
          created_at TEXT NOT NULL,
          closed_at TEXT,
          owner_origin TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS calls (
          call_id TEXT PRIMARY KEY,
          task_id TEXT NOT NULL REFERENCES tasks(task_id),
          skill TEXT NOT NULL,
          executor TEXT NOT NULL,
          actual_harness TEXT NOT NULL DEFAULT 'unknown',
          evidence TEXT NOT NULL,
          expected_version TEXT NOT NULL,
          expected_hash TEXT,
          trigger_kind TEXT NOT NULL,
          declared_result TEXT,
          result TEXT,
          first_result TEXT,
          status TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS deliveries (
          delivery_id TEXT PRIMARY KEY,
          task_id TEXT NOT NULL REFERENCES tasks(task_id),
          call_id TEXT,
          artifact_hash TEXT NOT NULL,
          rule_version TEXT NOT NULL,
          checker_id TEXT,
          checker_verified INTEGER NOT NULL DEFAULT 0,
          checker_result TEXT,
          checker_input_hash TEXT,
          checker_artifact_hash TEXT,
          checker_resource_hash TEXT,
          checker_composite_hash TEXT,
          checker_identity_version INTEGER NOT NULL DEFAULT 0,
          declared_result TEXT NOT NULL,
          effective_result TEXT NOT NULL,
          complete_fixture INTEGER NOT NULL,
          sequence INTEGER NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS checker_runs (
          delivery_id TEXT PRIMARY KEY REFERENCES deliveries(delivery_id) ON DELETE CASCADE,
          task_id TEXT NOT NULL REFERENCES tasks(task_id),
          call_id TEXT NOT NULL REFERENCES calls(call_id),
          subject_delivery_id TEXT NOT NULL,
          check_id TEXT NOT NULL,
          run_id TEXT NOT NULL,
          checker_name TEXT NOT NULL,
          checker_version TEXT NOT NULL,
          checker_digest TEXT NOT NULL,
          rule_version TEXT NOT NULL,
          result TEXT NOT NULL,
          reason TEXT NOT NULL,
          input_digest TEXT NOT NULL,
          artifact_digest TEXT NOT NULL,
          resource_identity TEXT NOT NULL,
          resource_digest TEXT NOT NULL,
          composite_identity TEXT NOT NULL,
          composite_digest TEXT NOT NULL,
          installation_digest TEXT NOT NULL,
          installation_version TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS checker_rule_results (
          delivery_id TEXT NOT NULL REFERENCES checker_runs(delivery_id) ON DELETE CASCADE,
          rule_id TEXT NOT NULL,
          result TEXT NOT NULL,
          reason TEXT NOT NULL,
          PRIMARY KEY(delivery_id, rule_id)
        );
        CREATE TABLE IF NOT EXISTS feedback (
          feedback_id TEXT PRIMARY KEY,
          task_id TEXT NOT NULL REFERENCES tasks(task_id),
          delivery_id TEXT,
          revision INTEGER NOT NULL,
          scope TEXT NOT NULL,
          result TEXT NOT NULL,
          body_hash TEXT,
          created_at TEXT NOT NULL,
          UNIQUE(task_id, revision)
        );
        CREATE TABLE IF NOT EXISTS feedback_revisions (
          feedback_id TEXT NOT NULL,
          task_id TEXT NOT NULL REFERENCES tasks(task_id),
          delivery_id TEXT NOT NULL,
          revision INTEGER NOT NULL,
          scope TEXT NOT NULL,
          result TEXT NOT NULL,
          body_hash TEXT,
          created_at TEXT NOT NULL,
          PRIMARY KEY(feedback_id, revision)
        );
        CREATE TABLE IF NOT EXISTS sessions (
          session_id TEXT PRIMARY KEY,
          suite_id TEXT NOT NULL,
          status TEXT NOT NULL,
          evidence TEXT NOT NULL,
          created_at TEXT NOT NULL,
          closed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS events (
          event_id INTEGER PRIMARY KEY AUTOINCREMENT,
          task_id TEXT NOT NULL REFERENCES tasks(task_id),
          kind TEXT NOT NULL,
          evidence TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS imported_snapshots (
          origin_id TEXT PRIMARY KEY,
          revision INTEGER NOT NULL,
          snapshot_hash TEXT NOT NULL,
          imported_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS imported_samples (
          origin_id TEXT NOT NULL,
          subject_kind TEXT NOT NULL,
          subject_id TEXT NOT NULL,
          suite_id TEXT NOT NULL,
          suite_version TEXT NOT NULL DEFAULT 'unknown',
          harness TEXT NOT NULL DEFAULT 'unknown',
          is_test INTEGER NOT NULL,
          started_at TEXT NOT NULL DEFAULT '',
          initiator TEXT,
          skill TEXT,
          executor TEXT,
          evidence TEXT,
          trigger_kind TEXT,
          expected_hash TEXT,
          first_result TEXT,
          current_result TEXT,
          imported_at TEXT NOT NULL,
          PRIMARY KEY(origin_id, subject_kind, subject_id)
        );
        CREATE TABLE IF NOT EXISTS suppressions (
          origin_id TEXT NOT NULL,
          subject_kind TEXT NOT NULL,
          subject_id TEXT NOT NULL,
          created_at TEXT NOT NULL,
          PRIMARY KEY(origin_id, subject_kind, subject_id)
        );
            """
        )
        _ensure_column(database, "calls", "actual_harness", "TEXT NOT NULL DEFAULT 'unknown'")
        _ensure_column(database, "calls", "declared_result", "TEXT")
        _ensure_column(database, "deliveries", "call_id", "TEXT")
        _ensure_column(database, "deliveries", "checker_id", "TEXT")
        _ensure_column(database, "deliveries", "checker_verified", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(database, "deliveries", "checker_result", "TEXT")
        _ensure_column(database, "deliveries", "checker_input_hash", "TEXT")
        _ensure_column(database, "deliveries", "checker_artifact_hash", "TEXT")
        _ensure_column(database, "deliveries", "checker_resource_hash", "TEXT")
        _ensure_column(database, "deliveries", "checker_composite_hash", "TEXT")
        _ensure_column(database, "deliveries", "checker_identity_version", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(database, "deliveries", "sequence", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(database, "feedback", "delivery_id", "TEXT")
        _ensure_column(database, "imported_samples", "started_at", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(database, "imported_samples", "initiator", "TEXT")
        _ensure_column(database, "imported_samples", "skill", "TEXT")
        _ensure_column(database, "imported_samples", "executor", "TEXT")
        _ensure_column(database, "imported_samples", "evidence", "TEXT")
        _ensure_column(database, "imported_samples", "trigger_kind", "TEXT")
        _ensure_column(database, "imported_samples", "expected_hash", "TEXT")
        _backfill_delivery_sequences(database)
        _migrate_feedback_revisions(database)
        if database.execute("SELECT 1 FROM meta WHERE key = 'origin_id'").fetchone() is None:
            database.execute(
                "INSERT INTO meta(key, value) VALUES('origin_id', ?)", (str(uuid.uuid4()),)
            )
            database.execute("INSERT INTO meta(key, value) VALUES('snapshot_revision', '0')")
        _create_observation_tables(database)
        database.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('writer_protocol', ?)",
            (str(WRITER_PROTOCOL_VERSION),),
        )
        _install_writer_protocol_triggers(database)
        database.commit()
    except Exception:
        database.rollback()
        database.close()
        raise
    return database


def _register_writer_protocol(database: sqlite3.Connection) -> None:
    """Mark a connection as a current records writer for guarded triggers."""

    database.create_function("selc_writer_protocol", 0, lambda: WRITER_PROTOCOL_VERSION)


def _reject_future_writer_protocol(database: sqlite3.Connection) -> None:
    meta_exists = database.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'"
    ).fetchone()
    if meta_exists is None:
        return
    row = database.execute("SELECT value FROM meta WHERE key = 'writer_protocol'").fetchone()
    if row is None:
        return
    try:
        protocol = int(row["value"])
    except (TypeError, ValueError) as error:
        raise SelcError(
            "UNSUPPORTED_DATABASE_PROTOCOL",
            "database writer protocol is invalid",
            "restore a supported records database or update SELC",
        ) from error
    if protocol > WRITER_PROTOCOL_VERSION:
        raise SelcError(
            "UNSUPPORTED_DATABASE_PROTOCOL",
            "database was written by a newer SELC runtime",
            "update SELC before opening this records database",
        )


def _execute_schema_script(database: sqlite3.Connection, script: str) -> None:
    """Execute schema DDL inside the caller's transaction.

    ``sqlite3.Connection.executescript`` commits a pending transaction first.  A
    migration needs the opposite property, so this deliberately executes each
    complete statement through ``execute``.
    """

    for statement in script.split(";\n"):
        if statement.strip():
            database.execute(statement)


def _create_observation_tables(database: sqlite3.Connection) -> None:
    statements = (
        """CREATE TABLE IF NOT EXISTS observation_events (
            event_id TEXT PRIMARY KEY,
            stable_identity TEXT,
            event_digest TEXT NOT NULL,
            protocol INTEGER NOT NULL,
            adapter_name TEXT NOT NULL,
            adapter_version TEXT NOT NULL,
            installation_id TEXT NOT NULL,
            host TEXT NOT NULL,
            surface TEXT NOT NULL,
            session_id TEXT,
            turn_id TEXT,
            native_event_id TEXT,
            phase TEXT NOT NULL,
            source_context TEXT NOT NULL,
            source_epoch INTEGER NOT NULL,
            suite_id TEXT,
            task_id TEXT,
            kind TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            received_at TEXT NOT NULL,
            classification_version TEXT NOT NULL,
            facts_json TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            status TEXT NOT NULL,
            reason TEXT,
            applied_at TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS observation_conflicts (
            conflict_id INTEGER PRIMARY KEY AUTOINCREMENT,
            stable_identity TEXT NOT NULL,
            original_event_id TEXT NOT NULL,
            incoming_digest TEXT NOT NULL,
            classification_version TEXT NOT NULL,
            facts_json TEXT NOT NULL,
            received_at TEXT NOT NULL,
            reason TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS observation_assessments (
            task_id TEXT PRIMARY KEY,
            first_result TEXT NOT NULL,
            final_result TEXT NOT NULL,
            assessment_version TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS observation_call_assessments (
            call_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            first_result TEXT NOT NULL,
            final_result TEXT NOT NULL,
            assessment_version TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS observation_tombstones (
            stable_identity TEXT PRIMARY KEY,
            source_context TEXT NOT NULL,
            source_epoch INTEGER NOT NULL,
            suite_id TEXT,
            task_id TEXT,
            deleted_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS observation_deletion_ranges (
            deletion_id INTEGER PRIMARY KEY AUTOINCREMENT,
            suite_id TEXT NOT NULL,
            before_time TEXT,
            delete_epoch INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS observation_source_epochs (
            source_context TEXT PRIMARY KEY,
            invalid_before_epoch INTEGER NOT NULL,
            deleted_at TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS observation_events_status_idx ON observation_events(status, event_id)",
        "CREATE INDEX IF NOT EXISTS observation_events_task_idx ON observation_events(task_id, status)",
        "CREATE INDEX IF NOT EXISTS observation_events_identity_idx ON observation_events(stable_identity)",
        """CREATE UNIQUE INDEX IF NOT EXISTS observation_events_stable_identity_unique
        ON observation_events(stable_identity) WHERE stable_identity IS NOT NULL""",
    )
    for statement in statements:
        database.execute(statement)


def _install_writer_protocol_triggers(database: sqlite3.Connection) -> None:
    protected_tables = (
        "tasks",
        "calls",
        "deliveries",
        "feedback",
        "feedback_revisions",
        "events",
        "observation_events",
        "observation_conflicts",
        "observation_assessments",
        "observation_call_assessments",
        "observation_tombstones",
        "observation_deletion_ranges",
        "observation_source_epochs",
    )
    for table in protected_tables:
        for operation in ("INSERT", "UPDATE", "DELETE"):
            database.execute(
                f"""CREATE TRIGGER IF NOT EXISTS writer_protocol_{table}_{operation.lower()}
                BEFORE {operation} ON {table}
                WHEN selc_writer_protocol() != {WRITER_PROTOCOL_VERSION}
                BEGIN
                    SELECT RAISE(ABORT, 'SELC_WRITER_PROTOCOL_REQUIRED');
                END"""
            )


def _ensure_column(database: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row["name"] for row in database.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        database.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _backfill_delivery_sequences(database: sqlite3.Connection) -> None:
    task_ids = [
        row["task_id"] for row in database.execute("SELECT DISTINCT task_id FROM deliveries")
    ]
    for task_id in task_ids:
        rows = database.execute(
            """SELECT delivery_id, sequence FROM deliveries
            WHERE task_id = ? ORDER BY sequence, created_at, delivery_id""",
            (task_id,),
        ).fetchall()
        if any(row["sequence"] == 0 for row in rows):
            for position, row in enumerate(rows, start=1):
                database.execute(
                    "UPDATE deliveries SET sequence = ? WHERE delivery_id = ?",
                    (position, row["delivery_id"]),
                )


def _migrate_feedback_revisions(database: sqlite3.Connection) -> None:
    """Preserve pre-threaded feedback rows from earlier v0.1 databases."""

    task_rows = database.execute(
        "SELECT DISTINCT task_id FROM feedback WHERE delivery_id IS NOT NULL"
    ).fetchall()
    database.execute(
        """INSERT OR IGNORE INTO feedback_revisions(
        feedback_id, task_id, delivery_id, revision, scope, result, body_hash, created_at)
        SELECT feedback_id, task_id, delivery_id, revision, scope, result, body_hash, created_at
        FROM feedback WHERE delivery_id IS NOT NULL"""
    )
    for row in task_rows:
        task_id = str(row["task_id"])
        _recompute_task_results(database, task_id)
        call_rows = database.execute(
            "SELECT DISTINCT call_id FROM deliveries WHERE task_id = ? AND call_id IS NOT NULL",
            (task_id,),
        ).fetchall()
        for call_row in call_rows:
            _recompute_call_results(database, str(call_row["call_id"]))


def _origin_id(database: sqlite3.Connection) -> str:
    return str(
        database.execute("SELECT value FROM meta WHERE key = 'origin_id'").fetchone()["value"]
    )


def _task(database: sqlite3.Connection, task_id: str) -> sqlite3.Row:
    row = database.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    if row is None:
        raise SelcError(
            "MISSING_TASK", f"task does not exist: {task_id}", "start the task before recording it"
        )
    return row


def _validate_result(result: str) -> None:
    if not isinstance(result, str) or result not in {"pass", "fail", "unknown"}:
        raise SelcError(
            "INVALID_INPUT", "result must be pass, fail, or unknown", "provide a supported result"
        )


@contextmanager
def _transaction_if_needed(database: sqlite3.Connection) -> Iterable[None]:
    """Use the caller's projection transaction without committing it early."""

    if database.in_transaction:
        yield
        return
    with database:
        yield


def start_task(
    database: sqlite3.Connection,
    *,
    task_id: str,
    suite_id: str,
    initiator: str,
    evidence: str,
    is_test: bool,
    suite_version: str = "unknown",
    harness: str = "unknown",
) -> dict[str, Any]:
    with database:
        current = database.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        if current is not None:
            expected = {
                "suite_id": suite_id,
                "suite_version": suite_version,
                "harness": harness,
                "initiator": initiator,
                "evidence": evidence,
                "is_test": int(is_test),
            }
            if all(current[key] == value for key, value in expected.items()):
                return {"task_id": task_id, "replayed": True}
            raise SelcError(
                "ID_CONFLICT",
                f"task id already belongs to another task: {task_id}",
                "choose a new task id",
            )
        database.execute(
            """INSERT INTO tasks(task_id, suite_id, suite_version, harness, initiator, evidence, is_test, status, created_at, owner_origin)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)""",
            (
                task_id,
                suite_id,
                suite_version,
                harness,
                initiator,
                evidence,
                int(is_test),
                utc_now(),
                _origin_id(database),
            ),
        )
    return {"task_id": task_id, "replayed": False}


def set_task_event(
    database: sqlite3.Connection, task_id: str, kind: str, evidence: str
) -> dict[str, Any]:
    allowed = {
        "input_ready",
        "permission_click",
        "business_intervention",
        "session_start",
        "session_close",
    }
    if kind not in allowed:
        raise SelcError(
            "INVALID_EVENT", f"unsupported event: {kind}", "use a documented event kind"
        )
    with database:
        task = _task(database, task_id)
        if kind == "session_start" and task["status"] != "active":
            raise SelcError(
                "TASK_CLOSED",
                f"cannot start a session for closed task: {task_id}",
                "start a new task before recording a host session",
            )
        database.execute(
            "INSERT INTO events(task_id, kind, evidence, created_at) VALUES (?, ?, ?, ?)",
            (task_id, kind, evidence, utc_now()),
        )
        if kind == "input_ready":
            database.execute("UPDATE tasks SET input_ready = 1 WHERE task_id = ?", (task_id,))
            _recompute_task_results(database, task_id)
        elif kind == "business_intervention":
            database.execute(
                "UPDATE tasks SET business_intervention = 1 WHERE task_id = ?", (task_id,)
            )
            _recompute_task_results(database, task_id)
        elif kind == "session_start":
            database.execute(
                "INSERT OR REPLACE INTO sessions(session_id, suite_id, status, evidence, created_at) VALUES (?, ?, 'active', ?, ?)",
                (f"session:{task_id}", task["suite_id"], evidence, utc_now()),
            )
        elif kind == "session_close":
            database.execute(
                "UPDATE sessions SET status = 'closed', closed_at = ? WHERE session_id = ?",
                (utc_now(), f"session:{task_id}"),
            )
    return {"task_id": task_id, "kind": kind, "evidence": evidence}


def start_call(
    database: sqlite3.Connection,
    *,
    call_id: str,
    task_id: str,
    skill: str,
    executor: str,
    evidence: str,
    expected_version: str,
    expected_hash: str | None,
    trigger_kind: str,
) -> dict[str, Any]:
    actual_harness = executor if executor in {"codex", "workbuddy"} else "unknown"
    with database:
        task = _task(database, task_id)
        current = database.execute("SELECT * FROM calls WHERE call_id = ?", (call_id,)).fetchone()
        if current is not None:
            expected = {
                "task_id": task_id,
                "skill": skill,
                "executor": executor,
                "actual_harness": actual_harness,
                "evidence": evidence,
                "expected_version": expected_version,
                "expected_hash": expected_hash,
                "trigger_kind": trigger_kind,
            }
            if all(current[key] == value for key, value in expected.items()):
                return {"call_id": call_id, "replayed": True}
            raise SelcError(
                "ID_CONFLICT",
                f"call id already belongs to another call: {call_id}",
                "choose a new call id",
            )
        if task["status"] != "active":
            raise SelcError(
                "TASK_CLOSED",
                f"cannot start a call for closed task: {task_id}",
                "start a new task before recording another invocation",
            )
        database.execute(
            """INSERT INTO calls(call_id, task_id, skill, executor, actual_harness, evidence, expected_version, expected_hash, trigger_kind, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)""",
            (
                call_id,
                task_id,
                skill,
                executor,
                actual_harness,
                evidence,
                expected_version,
                expected_hash,
                trigger_kind,
                utc_now(),
            ),
        )
    return {"call_id": call_id, "replayed": False}


def complete_call(database: sqlite3.Connection, call_id: str, result: str) -> dict[str, Any]:
    _validate_result(result)
    with database:
        row = database.execute("SELECT * FROM calls WHERE call_id = ?", (call_id,)).fetchone()
        if row is None:
            raise SelcError(
                "MISSING_CALL", f"call does not exist: {call_id}", "start the call first"
            )
        if row["declared_result"] is not None and row["declared_result"] != result:
            raise SelcError(
                "ID_CONFLICT",
                f"call already has result {row['declared_result']}",
                "create a new retry call",
            )
        database.execute(
            "UPDATE calls SET declared_result = ?, status = 'closed' WHERE call_id = ?",
            (result, call_id),
        )
        _recompute_call_results(database, call_id)
        effective = database.execute(
            "SELECT first_result, result FROM calls WHERE call_id = ?", (call_id,)
        ).fetchone()
    return {
        "call_id": call_id,
        "declared_result": result,
        "first_result": effective["first_result"],
        "result": effective["result"],
    }


def record_delivery(
    database: sqlite3.Connection,
    *,
    delivery_id: str,
    task_id: str,
    artifact_hash: str,
    rule_version: str,
    declared_result: str,
    complete_fixture: bool,
    call_id: str | None = None,
    checker_id: str | None = None,
    checker_verified: bool = False,
    checker_result: str | None = None,
    checker_identity: dict[str, str] | None = None,
) -> dict[str, Any]:
    _validate_result(declared_result)
    if checker_result is not None:
        _validate_result(checker_result)
    with _transaction_if_needed(database):
        task = _task(database, task_id)
        if call_id is not None:
            call = database.execute(
                "SELECT task_id FROM calls WHERE call_id = ?", (call_id,)
            ).fetchone()
            if call is None or call["task_id"] != task_id:
                raise SelcError(
                    "INVALID_DELIVERY_LINK",
                    "delivery call must exist under the same task",
                    "start the invocation before linking its artifact check",
                )
        if complete_fixture and not task["is_test"]:
            raise SelcError(
                "UNTRUSTED_OBSERVATION",
                "complete observation fixtures are limited to test tasks",
                "record normal host evidence as unknown or create an isolated test task",
            )
        normalized_identity = checker_identity or {}
        identity_keys = {
            "checker_input_hash",
            "checker_artifact_hash",
            "checker_resource_hash",
            "checker_composite_hash",
        }
        if any(
            key not in identity_keys or not isinstance(value, str)
            for key, value in normalized_identity.items()
        ):
            raise SelcError(
                "INVALID_DELIVERY_CHECK",
                "checker identity contains an unsupported value",
                "record only content hashes from the trusted checker",
            )
        current = database.execute(
            "SELECT * FROM deliveries WHERE delivery_id = ?", (delivery_id,)
        ).fetchone()
        if current is not None:
            expected = {
                "task_id": task_id,
                "call_id": call_id,
                "artifact_hash": artifact_hash,
                "rule_version": rule_version,
                "checker_id": checker_id,
                "declared_result": declared_result,
                "complete_fixture": int(complete_fixture),
                "checker_verified": int(checker_verified),
                "checker_result": checker_result,
                "checker_input_hash": normalized_identity.get("checker_input_hash"),
                "checker_artifact_hash": normalized_identity.get("checker_artifact_hash"),
                "checker_resource_hash": normalized_identity.get("checker_resource_hash"),
                "checker_composite_hash": normalized_identity.get("checker_composite_hash"),
                "checker_identity_version": 1,
            }
            if all(current[key] == value for key, value in expected.items()):
                return {"delivery_id": delivery_id, "replayed": True}
            raise SelcError(
                "ID_CONFLICT",
                f"delivery id already exists: {delivery_id}",
                "choose a new delivery id",
            )
        if complete_fixture:
            effective = declared_result
        elif checker_verified and checker_result is not None:
            effective = checker_result
        else:
            effective = "unknown"
        sequence = database.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM deliveries WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0]
        database.execute(
            """INSERT INTO deliveries(
            delivery_id, task_id, call_id, artifact_hash, rule_version, checker_id,
            checker_verified, checker_result, checker_input_hash, checker_artifact_hash,
            checker_resource_hash, checker_composite_hash, declared_result, effective_result,
            checker_identity_version, complete_fixture, sequence, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                delivery_id,
                task_id,
                call_id,
                artifact_hash,
                rule_version,
                checker_id,
                int(checker_verified),
                checker_result,
                normalized_identity.get("checker_input_hash"),
                normalized_identity.get("checker_artifact_hash"),
                normalized_identity.get("checker_resource_hash"),
                normalized_identity.get("checker_composite_hash"),
                declared_result,
                effective,
                1,
                int(complete_fixture),
                sequence,
                utc_now(),
            ),
        )
        _recompute_task_results(database, task_id)
        if call_id is not None:
            _recompute_call_results(database, call_id)
    return {
        "delivery_id": delivery_id,
        "effective_result": effective,
        "sequence": sequence,
        "replayed": False,
    }


def record_checked_delivery(database: sqlite3.Connection, execution: Any) -> dict[str, Any]:
    """Atomically link a completed checker execution to its exact delivery.

    ``execution`` is the internal ``CheckerExecution`` DTO from
    ``checker_integration``.  Checkers run before this function is called, so
    a long-running trusted script never holds the SQLite transaction.  Callers
    that already own ``projection_transaction`` can safely use this helper: it
    detects the active transaction and leaves commit ownership with the caller.
    """

    result = getattr(execution, "result", None)
    check_id = getattr(execution, "check_id", None)
    installation_digest = getattr(execution, "installation_digest", None)
    installation_version = getattr(execution, "installation_version", None)
    subject = getattr(result, "subject", None)
    checker = getattr(result, "checker", None)
    if (
        not isinstance(check_id, str)
        or not isinstance(installation_digest, str)
        or not isinstance(installation_version, str)
        or subject is None
        or checker is None
    ):
        raise SelcError(
            "INVALID_DELIVERY_CHECK",
            "checked delivery needs a complete trusted checker execution",
            "run the installed checker before recording the delivery",
        )
    task_id = getattr(subject, "task_id", None)
    call_id = getattr(subject, "call_id", None)
    delivery_id = getattr(subject, "delivery_id", None)
    if not all(isinstance(value, str) and value for value in (task_id, call_id, delivery_id)):
        raise SelcError(
            "INVALID_DELIVERY_CHECK",
            "checker subject must identify task, call, and delivery",
            "bind the checker to the actual delivery identity",
        )
    _validate_result(getattr(result, "status", ""))
    rule_results = tuple(getattr(result, "rule_results", ()))
    if not rule_results:
        raise SelcError(
            "INVALID_DELIVERY_CHECK",
            "checker result must include required rule outcomes",
            "run a declared checker with one or more required rules",
        )

    checker_identity = {
        "checker_input_hash": str(getattr(result, "input_digest", "")),
        "checker_artifact_hash": str(getattr(result, "artifact_digest", "")),
        "checker_resource_hash": str(getattr(result, "resource_digest", "")),
        "checker_composite_hash": str(getattr(result, "composite_digest", "")),
    }
    with _transaction_if_needed(database):
        task = _task(database, task_id)
        call = database.execute(
            "SELECT task_id, expected_version, expected_hash FROM calls WHERE call_id = ?",
            (call_id,),
        ).fetchone()
        if call is None or call["task_id"] != task_id:
            raise SelcError(
                "INVALID_DELIVERY_LINK",
                "checked delivery call must exist under the same task",
                "record the managed call before its checked delivery",
            )
        if (
            task["suite_version"] != installation_version
            or call["expected_version"] != installation_version
            or call["expected_hash"] != installation_digest
        ):
            raise SelcError(
                "INSTALLATION_IDENTITY_DRIFT",
                "checker execution does not match the task and call installation identity",
                "start a new managed call for the selected installation",
            )
        delivery = record_delivery(
            database,
            delivery_id=delivery_id,
            task_id=task_id,
            call_id=call_id,
            artifact_hash=checker_identity["checker_artifact_hash"],
            rule_version=str(getattr(result, "rule_version", "")),
            declared_result=str(getattr(result, "status", "")),
            complete_fixture=False,
            checker_id=f"{checker.name}@{checker.version}",
            checker_verified=True,
            checker_result=str(getattr(result, "status", "")),
            checker_identity=checker_identity,
        )
        _store_checker_execution(
            database,
            execution=execution,
            task_id=task_id,
            call_id=call_id,
            delivery_id=delivery_id,
            rule_results=rule_results,
        )
    return delivery


def _store_checker_execution(
    database: sqlite3.Connection,
    *,
    execution: Any,
    task_id: str,
    call_id: str,
    delivery_id: str,
    rule_results: tuple[Any, ...],
) -> None:
    result = execution.result
    checker = result.checker
    expected = {
        "delivery_id": delivery_id,
        "task_id": task_id,
        "call_id": call_id,
        "subject_delivery_id": result.subject.delivery_id,
        "check_id": execution.check_id,
        "run_id": result.run_id,
        "checker_name": checker.name,
        "checker_version": checker.version,
        "checker_digest": checker.digest,
        "rule_version": result.rule_version,
        "result": result.status,
        "reason": result.reason,
        "input_digest": result.input_digest,
        "artifact_digest": result.artifact_digest,
        "resource_identity": result.resource_identity,
        "resource_digest": result.resource_digest,
        "composite_identity": result.composite_identity,
        "composite_digest": result.composite_digest,
        "installation_digest": execution.installation_digest,
        "installation_version": execution.installation_version,
    }
    existing = database.execute(
        "SELECT * FROM checker_runs WHERE delivery_id = ?", (delivery_id,)
    ).fetchone()
    expected_rules = [
        (str(item.rule_id), str(item.status), str(item.reason)) for item in rule_results
    ]
    if len({item[0] for item in expected_rules}) != len(expected_rules):
        raise SelcError(
            "INVALID_DELIVERY_CHECK",
            "checker result repeats a rule id",
            "keep one result for each required rule",
        )
    if existing is not None:
        existing_rules = [
            (str(row["rule_id"]), str(row["result"]), str(row["reason"]))
            for row in database.execute(
                "SELECT rule_id, result, reason FROM checker_rule_results "
                "WHERE delivery_id = ? ORDER BY rule_id",
                (delivery_id,),
            ).fetchall()
        ]
        if (
            all(existing[key] == value for key, value in expected.items())
            and sorted(existing_rules) == sorted(expected_rules)
        ):
            return
        raise SelcError(
            "ID_CONFLICT",
            "delivery already has different checker evidence",
            "preserve the original checker result and use a new delivery id",
        )
    columns = ", ".join(expected)
    placeholders = ", ".join("?" for _ in expected)
    database.execute(
        f"INSERT INTO checker_runs({columns}, created_at) VALUES ({placeholders}, ?)",
        [*expected.values(), utc_now()],
    )
    database.executemany(
        """INSERT INTO checker_rule_results(delivery_id, rule_id, result, reason)
        VALUES (?, ?, ?, ?)""",
        [(delivery_id, rule_id, status, reason) for rule_id, status, reason in expected_rules],
    )


def record_feedback(
    database: sqlite3.Connection,
    *,
    feedback_id: str,
    task_id: str,
    delivery_id: str,
    revision: int,
    scope: str,
    result: str,
    body: str | None,
) -> dict[str, Any]:
    _validate_result(result)
    if scope not in {"first", "final", "requirement_change"}:
        raise SelcError(
            "INVALID_INPUT",
            "feedback scope must be first, final, or requirement_change",
            "use a supported feedback scope",
        )
    if revision < 1:
        raise SelcError(
            "INVALID_INPUT", "feedback revision must be positive", "start at revision 1"
        )
    body_hash = json_hash(body) if body is not None else None
    with database:
        _task(database, task_id)
        delivery = database.execute(
            "SELECT call_id FROM deliveries WHERE delivery_id = ? AND task_id = ?",
            (delivery_id, task_id),
        ).fetchone()
        if delivery is None:
            raise SelcError(
                "INVALID_FEEDBACK_LINK",
                "feedback must bind an existing delivery in the same task",
                "record the artifact delivery before its feedback",
            )
        same_revision = database.execute(
            """SELECT * FROM feedback_revisions
            WHERE feedback_id = ? AND revision = ?""",
            (feedback_id, revision),
        ).fetchone()
        if same_revision is not None:
            if (
                same_revision["task_id"] == task_id
                and same_revision["delivery_id"] == delivery_id
                and same_revision["scope"] == scope
                and same_revision["result"] == result
                and same_revision["body_hash"] == body_hash
            ):
                return {"feedback_id": feedback_id, "revision": revision, "replayed": True}
            raise SelcError(
                "ID_CONFLICT",
                "feedback revision already contains different evidence",
                "use a new feedback id for independent feedback",
            )
        thread = database.execute(
            """SELECT task_id, delivery_id, scope, MAX(revision) AS revision
            FROM feedback_revisions WHERE feedback_id = ?""",
            (feedback_id,),
        ).fetchone()
        if thread is not None and thread["revision"] is not None:
            legacy_scope_revision = {str(thread["scope"]), scope}.issubset({"first", "final"})
            if (
                thread["task_id"] != task_id
                or thread["delivery_id"] != delivery_id
                or (thread["scope"] != scope and not legacy_scope_revision)
            ):
                raise SelcError(
                    "ID_CONFLICT",
                    "feedback id is already bound to different evidence",
                    "use a new feedback id for independent feedback",
                )
            expected_revision = int(thread["revision"]) + 1
        else:
            expected_revision = 1
        if revision != expected_revision:
            raise SelcError(
                "REVISION_CONFLICT",
                f"expected feedback revision {expected_revision}",
                "retry with the next revision for this feedback id",
            )
        database.execute(
            """INSERT INTO feedback_revisions(
            feedback_id, task_id, delivery_id, revision, scope, result, body_hash, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                feedback_id,
                task_id,
                delivery_id,
                revision,
                scope,
                result,
                body_hash,
                utc_now(),
            ),
        )
        _recompute_task_results(database, task_id)
        if delivery["call_id"] is not None:
            _recompute_call_results(database, delivery["call_id"])
    return {"feedback_id": feedback_id, "revision": revision, "replayed": False}


def close_task(database: sqlite3.Connection, task_id: str) -> dict[str, Any]:
    with database:
        task = _task(database, task_id)
        if task["status"] == "closed":
            return {"task_id": task_id, "replayed": True, "result": task["current_result"]}
        active_calls = database.execute(
            "SELECT COUNT(*) FROM calls WHERE task_id = ? AND status = 'active'", (task_id,)
        ).fetchone()[0]
        if active_calls:
            raise SelcError(
                "ACTIVE_CALL",
                f"task has {active_calls} active call(s): {task_id}",
                "complete or abandon the active calls before closing the task",
            )
        _recompute_task_results(database, task_id)
        recalculated = _task(database, task_id)
        current = recalculated["current_result"] or "unknown"
        first = recalculated["first_result"] or current
        database.execute(
            "UPDATE tasks SET status = 'closed', first_result = ?, current_result = ?, closed_at = ? WHERE task_id = ?",
            (first, current, utc_now(), task_id),
        )
    return {"task_id": task_id, "replayed": False, "result": current, "first_result": first}


@contextmanager
def projection_transaction(database: sqlite3.Connection) -> Iterable[None]:
    """Run reducer projections without public-method auto-commits.

    Public record functions intentionally retain their single-operation commit
    behavior.  The observation reducer uses this internal transaction boundary
    so business projection and an event's consumed state commit together.
    """

    database.execute("BEGIN IMMEDIATE")
    try:
        yield
    except Exception:
        database.rollback()
        raise
    else:
        database.commit()


def _project_start_task(
    database: sqlite3.Connection,
    *,
    task_id: str,
    suite_id: str,
    suite_version: str,
    harness: str,
    initiator: str,
    evidence: str,
) -> bool:
    current = database.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    if current is not None:
        expected = {
            "suite_id": suite_id,
            "suite_version": suite_version,
            "harness": harness,
            "initiator": initiator,
            "evidence": evidence,
            "is_test": 0,
        }
        if all(current[key] == value for key, value in expected.items()):
            return True
        raise SelcError(
            "ID_CONFLICT",
            f"task id already belongs to another task: {task_id}",
            "preserve the original task and use a new business identity",
        )
    database.execute(
        """INSERT INTO tasks(
        task_id, suite_id, suite_version, harness, initiator, evidence, is_test,
        status, created_at, owner_origin)
        VALUES (?, ?, ?, ?, ?, ?, 0, 'active', ?, ?)""",
        (
            task_id,
            suite_id,
            suite_version,
            harness,
            initiator,
            evidence,
            utc_now(),
            _origin_id(database),
        ),
    )
    return False


def _project_task_event(
    database: sqlite3.Connection, *, task_id: str, kind: str, evidence: str
) -> None:
    allowed = {
        "input_ready",
        "permission_click",
        "business_intervention",
        "session_start",
        "session_close",
        "automatic_retry",
        "interrupted",
    }
    if kind not in allowed:
        raise SelcError("INVALID_EVENT", f"unsupported event: {kind}", "use a supported event")
    task = _task(database, task_id)
    database.execute(
        "INSERT INTO events(task_id, kind, evidence, created_at) VALUES (?, ?, ?, ?)",
        (task_id, kind, evidence, utc_now()),
    )
    if kind == "input_ready":
        database.execute("UPDATE tasks SET input_ready = 1 WHERE task_id = ?", (task_id,))
    elif kind == "business_intervention":
        database.execute("UPDATE tasks SET business_intervention = 1 WHERE task_id = ?", (task_id,))
    elif kind == "session_start":
        if task["status"] != "active":
            raise SelcError("TASK_CLOSED", "cannot start a session for a closed task", "start a task")
        database.execute(
            """INSERT OR REPLACE INTO sessions(session_id, suite_id, status, evidence, created_at)
            VALUES (?, ?, 'active', ?, ?)""",
            (f"session:{task_id}", task["suite_id"], evidence, utc_now()),
        )
    elif kind == "session_close":
        database.execute(
            "UPDATE sessions SET status = 'closed', closed_at = ? WHERE session_id = ?",
            (utc_now(), f"session:{task_id}"),
        )
    _recompute_task_results(database, task_id)


def _project_start_call(
    database: sqlite3.Connection,
    *,
    call_id: str,
    task_id: str,
    skill: str,
    executor: str,
    evidence: str,
    expected_version: str,
    expected_hash: str | None,
    trigger_kind: str,
) -> bool:
    actual_harness = executor if executor in {"codex", "workbuddy"} else "unknown"
    task = _task(database, task_id)
    current = database.execute("SELECT * FROM calls WHERE call_id = ?", (call_id,)).fetchone()
    if current is not None:
        expected = {
            "task_id": task_id,
            "skill": skill,
            "executor": executor,
            "actual_harness": actual_harness,
            "evidence": evidence,
            "expected_version": expected_version,
            "expected_hash": expected_hash,
            "trigger_kind": trigger_kind,
        }
        if all(current[key] == value for key, value in expected.items()):
            return True
        raise SelcError(
            "ID_CONFLICT",
            f"call id already belongs to another call: {call_id}",
            "preserve the original invocation and use a new native identity",
        )
    if task["status"] != "active":
        raise SelcError("TASK_CLOSED", "cannot start a call for a closed task", "start a new task")
    database.execute(
        """INSERT INTO calls(
        call_id, task_id, skill, executor, actual_harness, evidence, expected_version,
        expected_hash, trigger_kind, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)""",
        (
            call_id,
            task_id,
            skill,
            executor,
            actual_harness,
            evidence,
            expected_version,
            expected_hash,
            trigger_kind,
            utc_now(),
        ),
    )
    return False


def _project_complete_call(database: sqlite3.Connection, *, call_id: str, result: str) -> None:
    _validate_result(result)
    row = database.execute("SELECT * FROM calls WHERE call_id = ?", (call_id,)).fetchone()
    if row is None:
        raise SelcError("MISSING_CALL", f"call does not exist: {call_id}", "start the call first")
    if row["declared_result"] is not None and row["declared_result"] != result:
        raise SelcError(
            "ID_CONFLICT",
            f"call already has result {row['declared_result']}",
            "create a new retry call",
        )
    database.execute(
        "UPDATE calls SET declared_result = ?, status = 'closed' WHERE call_id = ?",
        (result, call_id),
    )
    _recompute_call_results(database, call_id)


def _project_delivery(
    database: sqlite3.Connection,
    *,
    delivery_id: str,
    task_id: str,
    call_id: str | None,
    artifact_hash: str,
    rule_version: str,
    checker_id: str,
    checker_result: str,
    checker_identity: dict[str, str],
    sequence: int,
) -> bool:
    _validate_result(checker_result)
    _task(database, task_id)
    if call_id is not None:
        call = database.execute("SELECT task_id FROM calls WHERE call_id = ?", (call_id,)).fetchone()
        if call is None or call["task_id"] != task_id:
            raise SelcError(
                "INVALID_DELIVERY_LINK",
                "delivery call must exist under the same task",
                "start the invocation before recording delivery evidence",
            )
    expected = {
        "task_id": task_id,
        "call_id": call_id,
        "artifact_hash": artifact_hash,
        "rule_version": rule_version,
        "checker_id": checker_id,
        "checker_result": checker_result,
        "checker_input_hash": checker_identity["checker_input_hash"],
        "checker_artifact_hash": checker_identity["checker_artifact_hash"],
        "checker_resource_hash": checker_identity["checker_resource_hash"],
        "checker_composite_hash": checker_identity["checker_composite_hash"],
        "sequence": sequence,
    }
    current = database.execute(
        "SELECT * FROM deliveries WHERE delivery_id = ?", (delivery_id,)
    ).fetchone()
    if current is not None:
        if all(current[key] == value for key, value in expected.items()):
            return True
        raise SelcError(
            "ID_CONFLICT",
            f"delivery id already exists: {delivery_id}",
            "preserve the original publication and use a new identity",
        )
    ordinal_conflict = database.execute(
        "SELECT delivery_id FROM deliveries WHERE task_id = ? AND sequence = ?",
        (task_id, sequence),
    ).fetchone()
    if ordinal_conflict is not None:
        raise SelcError(
            "PUBLICATION_CONFLICT",
            f"publication ordinal {sequence} already belongs to {ordinal_conflict['delivery_id']}",
            "retain the recorded publication order",
        )
    database.execute(
        """INSERT INTO deliveries(
        delivery_id, task_id, call_id, artifact_hash, rule_version, checker_id,
        checker_verified, checker_result, checker_input_hash, checker_artifact_hash,
        checker_resource_hash, checker_composite_hash, declared_result, effective_result,
        checker_identity_version, complete_fixture, sequence, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, 1, 0, ?, ?)""",
        (
            delivery_id,
            task_id,
            call_id,
            artifact_hash,
            rule_version,
            checker_id,
            checker_result,
            checker_identity["checker_input_hash"],
            checker_identity["checker_artifact_hash"],
            checker_identity["checker_resource_hash"],
            checker_identity["checker_composite_hash"],
            checker_result,
            checker_result,
            sequence,
            utc_now(),
        ),
    )
    _recompute_task_results(database, task_id)
    if call_id is not None:
        _recompute_call_results(database, call_id)
    return False


def _project_feedback(
    database: sqlite3.Connection,
    *,
    feedback_id: str,
    task_id: str,
    delivery_id: str,
    revision: int,
    scope: str,
    result: str,
    body_hash: str | None,
) -> bool:
    _validate_result(result)
    if scope not in {"first", "final", "requirement_change"}:
        raise SelcError("INVALID_INPUT", "feedback scope is invalid", "use a supported scope")
    if revision < 1:
        raise SelcError("INVALID_INPUT", "feedback revision must be positive", "start at 1")
    _task(database, task_id)
    delivery = database.execute(
        "SELECT call_id FROM deliveries WHERE delivery_id = ? AND task_id = ?",
        (delivery_id, task_id),
    ).fetchone()
    if delivery is None:
        raise SelcError(
            "INVALID_FEEDBACK_LINK",
            "feedback must bind an existing delivery in the same task",
            "record its publication before feedback",
        )
    existing = database.execute(
        "SELECT * FROM feedback_revisions WHERE feedback_id = ? AND revision = ?",
        (feedback_id, revision),
    ).fetchone()
    if existing is not None:
        expected = (task_id, delivery_id, scope, result, body_hash)
        actual = (
            existing["task_id"],
            existing["delivery_id"],
            existing["scope"],
            existing["result"],
            existing["body_hash"],
        )
        if actual == expected:
            return True
        raise SelcError("ID_CONFLICT", "feedback revision has different evidence", "use a new id")
    latest = database.execute(
        "SELECT MAX(revision) FROM feedback_revisions WHERE feedback_id = ?", (feedback_id,)
    ).fetchone()[0]
    expected_revision = int(latest) + 1 if latest is not None else 1
    if revision != expected_revision:
        if revision > expected_revision:
            raise SelcError(
                "MISSING_FEEDBACK_REVISION",
                f"feedback revision {expected_revision} is still pending",
                "replay after the missing earlier revision arrives",
            )
        raise SelcError(
            "REVISION_CONFLICT",
            f"expected feedback revision {expected_revision}",
            "retry with the next revision",
        )
    database.execute(
        """INSERT INTO feedback_revisions(
        feedback_id, task_id, delivery_id, revision, scope, result, body_hash, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (feedback_id, task_id, delivery_id, revision, scope, result, body_hash, utc_now()),
    )
    _recompute_task_results(database, task_id)
    if delivery["call_id"] is not None:
        _recompute_call_results(database, delivery["call_id"])
    return False


def _combine_with_feedback(base_result: str, feedback_results: list[str]) -> str:
    """Apply active feedback threads without allowing praise to erase a failure."""

    if base_result == "fail" or "fail" in feedback_results:
        return "fail"
    if base_result == "unknown" or "unknown" in feedback_results:
        return "unknown"
    return "pass"


def _feedback_for_delivery(
    database: sqlite3.Connection, task_id: str, delivery_id: str
) -> list[str]:
    """Return current original-requirement assessments for one delivery.

    Older clients called the field ``scope`` and supplied ``first`` or ``final``.
    Those values describe the same original requirement, not independent metric axes.
    ``requirement_change`` is recorded for history but deliberately does not rewrite
    the assessment of the delivery that satisfied the earlier requirement.
    """

    rows = database.execute(
        """SELECT active.result FROM feedback_revisions AS active
        JOIN (
          SELECT feedback_id, MAX(revision) AS revision
          FROM feedback_revisions
          WHERE task_id = ? AND delivery_id = ? AND scope != 'requirement_change'
          GROUP BY feedback_id
        ) AS latest
        ON active.feedback_id = latest.feedback_id AND active.revision = latest.revision
        WHERE active.task_id = ? AND active.delivery_id = ?
          AND active.scope != 'requirement_change'""",
        (task_id, delivery_id, task_id, delivery_id),
    ).fetchall()
    return [str(row["result"]) for row in rows]


def _delivery_result_with_feedback(
    database: sqlite3.Connection, task_id: str, delivery: sqlite3.Row
) -> str:
    return _combine_with_feedback(
        str(delivery["effective_result"]),
        _feedback_for_delivery(database, task_id, str(delivery["delivery_id"])),
    )


def _observed_first_result(base_result: str, complete_observation: bool, input_ready: bool) -> str:
    if base_result == "fail":
        return "fail"
    if not complete_observation or not input_ready:
        return "unknown"
    return base_result


def _recompute_task_results(database: sqlite3.Connection, task_id: str) -> None:
    formal = _formal_assessment(database, task_id)
    if formal is not None:
        database.execute(
            "UPDATE tasks SET first_result = ?, current_result = ? WHERE task_id = ?",
            (formal["first_result"], formal["final_result"], task_id),
        )
        return
    task = _task(database, task_id)
    deliveries = database.execute(
        """SELECT delivery_id, effective_result, complete_fixture FROM deliveries
        WHERE task_id = ? ORDER BY sequence""",
        (task_id,),
    ).fetchall()
    if deliveries:
        first_delivery = deliveries[0]
        current_delivery = deliveries[-1]
        first_base = _observed_first_result(
            _delivery_result_with_feedback(database, task_id, first_delivery),
            bool(first_delivery["complete_fixture"]),
            bool(task["input_ready"]),
        )
        current_base = _delivery_result_with_feedback(database, task_id, current_delivery)
    else:
        first_delivery = None
        current_delivery = None
        first_base = "unknown"
        current_base = "unknown"
    if task["business_intervention"]:
        first_base = "fail"
    first = first_base
    current = current_base
    database.execute(
        "UPDATE tasks SET first_result = ?, current_result = ? WHERE task_id = ?",
        (first, current, task_id),
    )


def _formal_assessment(database: sqlite3.Connection, task_id: str) -> sqlite3.Row | None:
    """Return an observation-derived assessment when the v2 table exists."""

    try:
        return database.execute(
            """SELECT first_result, final_result FROM observation_assessments
            WHERE task_id = ?""",
            (task_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None


def _recompute_call_results(database: sqlite3.Connection, call_id: str) -> None:
    call = database.execute("SELECT * FROM calls WHERE call_id = ?", (call_id,)).fetchone()
    if call is None:
        return
    if call["status"] != "closed":
        first = "unknown"
        current = "unknown"
    else:
        task = _task(database, str(call["task_id"]))
        deliveries = database.execute(
            """SELECT delivery_id, effective_result, complete_fixture FROM deliveries
            WHERE call_id = ? ORDER BY sequence""",
            (call_id,),
        ).fetchall()
        if deliveries:
            first_delivery = deliveries[0]
            current_delivery = deliveries[-1]
            first_base = _observed_first_result(
                _delivery_result_with_feedback(database, task["task_id"], first_delivery),
                bool(first_delivery["complete_fixture"]),
                bool(task["input_ready"]),
            )
            current_base = _delivery_result_with_feedback(
                database, task["task_id"], current_delivery
            )
            first = first_base
            current = current_base
        else:
            first = "unknown"
            current = "unknown"
    database.execute(
        "UPDATE calls SET first_result = ?, result = ? WHERE call_id = ?",
        (first, current, call_id),
    )


def active_leases(database: sqlite3.Connection, suite_id: str) -> dict[str, int]:
    tasks = database.execute(
        "SELECT COUNT(*) FROM tasks WHERE suite_id = ? AND status = 'active'", (suite_id,)
    ).fetchone()[0]
    sessions = database.execute(
        "SELECT COUNT(*) FROM sessions WHERE suite_id = ? AND status = 'active'", (suite_id,)
    ).fetchone()[0]
    calls = database.execute(
        """SELECT COUNT(*) FROM calls
        JOIN tasks ON calls.task_id = tasks.task_id
        WHERE tasks.suite_id = ? AND calls.status = 'active'""",
        (suite_id,),
    ).fetchone()[0]
    return {"tasks": int(tasks), "calls": int(calls), "sessions": int(sessions)}


def _metrics(rows: Iterable[Any], result_key: str) -> dict[str, int | float | None]:
    samples = list(rows)
    results = [sample[result_key] for sample in samples]
    known = sum(result in {"pass", "fail"} for result in results)
    success = sum(result == "pass" for result in results)
    total = len(results)
    unknown = total - known
    return {
        "N": total,
        "S": success,
        "K": known,
        "U": unknown,
        "S_over_N": success / total if total else None,
        "S_over_K": success / known if known else None,
        "K_over_N": known / total if total else None,
    }


def _deduplicated_samples(
    database: sqlite3.Connection, kind: str, include_test: bool
) -> list[dict[str, Any]]:
    if kind == "task":
        local_rows = [dict(row) for row in database.execute("SELECT * FROM tasks")]
        imported_rows = [
            {
                "subject_id": row["subject_id"],
                "suite_id": row["suite_id"],
                "is_test": row["is_test"],
                "suite_version": row["suite_version"],
                "harness": row["harness"],
                "started_at": row["started_at"],
                "initiator": row["initiator"],
                "skill": row["skill"],
                "executor": row["executor"],
                "evidence": row["evidence"],
                "trigger_kind": row["trigger_kind"],
                "expected_hash": row["expected_hash"],
                "first_result": row["first_result"],
                "current_result": row["current_result"],
            }
            for row in database.execute(
                "SELECT * FROM imported_samples WHERE subject_kind = 'task'"
            )
        ]
        local_rows = [
            {"subject_id": row["task_id"], "started_at": row["created_at"], **row}
            for row in local_rows
        ]
    else:
        local_rows = [
            {"subject_id": row["call_id"], **dict(row)}
            for row in database.execute(
                """SELECT calls.*, tasks.suite_id, tasks.is_test, tasks.initiator,
                          calls.expected_version AS suite_version,
                          calls.actual_harness AS harness,
                          calls.created_at AS started_at
                FROM calls JOIN tasks ON calls.task_id = tasks.task_id"""
            )
        ]
        imported_rows = [
            {
                "subject_id": row["subject_id"],
                "suite_id": row["suite_id"],
                "is_test": row["is_test"],
                "suite_version": row["suite_version"],
                "harness": row["harness"],
                "started_at": row["started_at"],
                "initiator": row["initiator"],
                "skill": row["skill"],
                "executor": row["executor"],
                "evidence": row["evidence"],
                "trigger_kind": row["trigger_kind"],
                "expected_hash": row["expected_hash"],
                "first_result": row["first_result"],
                "result": row["current_result"],
            }
            for row in database.execute(
                "SELECT * FROM imported_samples WHERE subject_kind = 'call'"
            )
        ]
    values: dict[str, dict[str, Any]] = {}
    for row in [*local_rows, *imported_rows]:
        row = _normalize_sample_provenance(row)
        if not include_test and row["is_test"]:
            continue
        current = values.get(row["subject_id"])
        if current is None:
            values[row["subject_id"]] = row
            continue
        identity_keys = ("suite_id", "suite_version", "harness", "evidence")
        if kind == "task":
            identity_keys += ("initiator",)
        if kind == "call":
            identity_keys += (
                "initiator",
                "skill",
                "executor",
                "evidence",
                "trigger_kind",
                "expected_hash",
            )
        if any(current.get(key) != row.get(key) for key in identity_keys):
            result_keys = (
                ("first_result", "current_result") if kind == "task" else ("first_result", "result")
            )
            for key in result_keys:
                current[key] = "unknown"
            continue
        result_keys = (
            ("first_result", "current_result") if kind == "task" else ("first_result", "result")
        )
        for key in result_keys:
            if current.get(key) != row.get(key):
                current[key] = "unknown"
    return list(values.values())


def _normalize_sample_provenance(row: dict[str, Any]) -> dict[str, Any]:
    """Keep pre-API009 nullable provenance visible as ``unknown`` in metrics."""

    normalized = dict(row)
    for key in (
        "suite_version",
        "harness",
        "initiator",
        "skill",
        "executor",
        "evidence",
        "trigger_kind",
        "expected_hash",
    ):
        if normalized.get(key) is None:
            normalized[key] = "unknown"
    return normalized


def stats(database: sqlite3.Connection, *, include_test: bool) -> dict[str, Any]:
    test_clause = "" if include_test else "WHERE is_test = 0"
    del test_clause
    task_rows = _deduplicated_samples(database, "task", include_test)
    call_rows = _deduplicated_samples(database, "call", include_test)
    return {
        "tasks": {
            "one_shot": _metrics(task_rows, "first_result"),
            "final": _metrics(task_rows, "current_result"),
        },
        "calls": {
            "one_shot": _metrics(call_rows, "first_result"),
            "final": _metrics(call_rows, "result"),
        },
        "groups": {
            "tasks": _group_metrics(
                task_rows,
                ("suite_id", "suite_version", "harness", "initiator"),
                "current_result",
            ),
            "calls": _group_metrics(
                call_rows,
                ("suite_id", "suite_version", "harness", "initiator", "skill", "executor"),
                "result",
            ),
        },
        "metric_contract": {
            "version": 1,
            "axes": ["one_shot", "final"],
            "measures": ["N", "S", "K", "U", "S_over_N", "S_over_K", "K_over_N"],
        },
        "includes_test": include_test,
        "generated_at": utc_now(),
    }


def _group_metrics(
    rows: list[dict[str, Any]], keys: tuple[str, ...], result_key: str
) -> list[dict[str, Any]]:
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for row in rows:
        group_key = tuple(row.get(key, "unknown") for key in keys)
        groups.setdefault(group_key, []).append(row)
    return [
        {
            "group": dict(zip(keys, group_key, strict=True)),
            "one_shot": _metrics(samples, "first_result"),
            "final": _metrics(samples, result_key),
        }
        for group_key, samples in sorted(groups.items())
    ]


def export_snapshot(database: sqlite3.Connection, output: Path) -> dict[str, Any]:
    origin_id = _origin_id(database)
    database.execute("BEGIN IMMEDIATE")
    try:
        revision = (
            int(
                database.execute(
                    "SELECT value FROM meta WHERE key = 'snapshot_revision'"
                ).fetchone()[0]
            )
            + 1
        )
        database.execute(
            "UPDATE meta SET value = ? WHERE key = 'snapshot_revision'", (str(revision),)
        )
        tasks = [
            dict(row)
            for row in database.execute(
                """SELECT task_id, suite_id, suite_version, harness, initiator, evidence, is_test,
                          first_result, current_result, created_at AS started_at, closed_at
                FROM tasks WHERE owner_origin = ?""",
                (origin_id,),
            )
        ]
        calls = [
            dict(row)
            for row in database.execute(
                """SELECT calls.call_id, calls.task_id, tasks.suite_id,
                          calls.expected_version AS suite_version, calls.actual_harness AS harness,
                          tasks.is_test, tasks.initiator, calls.skill, calls.executor, calls.evidence,
                          calls.trigger_kind, calls.expected_hash, calls.first_result,
                          calls.result AS current_result, calls.created_at AS started_at
                FROM calls JOIN tasks ON calls.task_id = tasks.task_id WHERE tasks.owner_origin = ?""",
                (origin_id,),
            )
        ]
        database.commit()
    except Exception:
        database.rollback()
        raise
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "origin_id": origin_id,
        "snapshot_revision": revision,
        "as_of": utc_now(),
        "metric_contract": {
            "groups": ["tasks.one_shot", "tasks.final", "calls.one_shot", "calls.final"],
            "measures": ["N", "S", "K", "U", "S_over_N", "S_over_K", "K_over_N"],
            "privacy": "result samples omit business artifacts, conversation, and feedback bodies",
        },
        "tasks": tasks,
        "calls": calls,
    }
    payload["snapshot_hash"] = json_hash(payload)
    write_json(output, payload)
    return {
        "output": str(output),
        "origin_id": origin_id,
        "snapshot_revision": revision,
        "snapshot_hash": payload["snapshot_hash"],
    }


def _validate_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Validate every snapshot field before deciding whether it is a no-op.

    Imported files are an external boundary.  Validation therefore happens before
    origin/revision handling and produces normalized provenance values only after
    the supplied hash has been checked.  A malformed same-origin file cannot hide
    behind a no-op response or partially replace prior imported samples.
    """
    required = {
        "schema_version",
        "origin_id",
        "snapshot_revision",
        "as_of",
        "metric_contract",
        "tasks",
        "calls",
        "snapshot_hash",
    }
    if required.difference(snapshot):
        raise SelcError(
            "INVALID_SNAPSHOT", "snapshot misses required fields", "export a fresh snapshot"
        )
    if type(snapshot["schema_version"]) is not int or snapshot["schema_version"] != SCHEMA_VERSION:
        raise SelcError(
            "UNSUPPORTED_SCHEMA", "snapshot schema is unsupported", "use a compatible snapshot"
        )
    unsigned = dict(snapshot)
    supplied_hash = unsigned.pop("snapshot_hash")
    if not isinstance(supplied_hash, str) or supplied_hash != json_hash(unsigned):
        raise SelcError(
            "SNAPSHOT_HASH_MISMATCH",
            "snapshot content hash does not match",
            "do not modify exported files",
        )
    if type(snapshot["snapshot_revision"]) is not int or snapshot["snapshot_revision"] < 1:
        raise SelcError(
            "INVALID_SNAPSHOT",
            "snapshot revision must be a positive integer",
            "export a fresh snapshot",
        )
    if not isinstance(snapshot["origin_id"], str) or not snapshot["origin_id"]:
        raise SelcError(
            "INVALID_SNAPSHOT", "snapshot origin id is invalid", "export a fresh snapshot"
        )
    if not isinstance(snapshot["as_of"], str) or not snapshot["as_of"]:
        raise SelcError("INVALID_SNAPSHOT", "snapshot as_of is invalid", "export a fresh snapshot")
    contract = snapshot["metric_contract"]
    expected_groups = {"tasks.one_shot", "tasks.final", "calls.one_shot", "calls.final"}
    expected_measures = {"N", "S", "K", "U", "S_over_N", "S_over_K", "K_over_N"}
    if (
        not isinstance(contract, dict)
        or not isinstance(contract.get("groups"), list)
        or not isinstance(contract.get("measures"), list)
        or not all(isinstance(value, str) for value in contract["groups"])
        or not all(isinstance(value, str) for value in contract["measures"])
        or set(contract["groups"]) != expected_groups
        or set(contract["measures"]) != expected_measures
        or not isinstance(contract.get("privacy"), str)
    ):
        raise SelcError(
            "INVALID_SNAPSHOT",
            "snapshot metric contract is unsupported",
            "export a fresh snapshot",
        )
    normalized: dict[str, Any] = dict(snapshot)
    for kind in ("tasks", "calls"):
        if not isinstance(snapshot[kind], list):
            raise SelcError("INVALID_SNAPSHOT", f"{kind} must be a list", "export a fresh snapshot")
        normalized[kind] = _validate_snapshot_samples(kind, snapshot[kind])
    return normalized


def _validate_snapshot_samples(kind: str, samples: list[Any]) -> list[dict[str, Any]]:
    id_key = "task_id" if kind == "tasks" else "call_id"
    required = {id_key, "suite_id", "is_test", "started_at", "first_result", "current_result"}
    if kind == "calls":
        required.update({"task_id", "skill"})
    provenance = {
        "suite_version",
        "harness",
        "initiator",
        "skill",
        "executor",
        "evidence",
        "trigger_kind",
        "expected_hash",
    }
    seen_ids: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for sample in samples:
        if not isinstance(sample, dict) or required.difference(sample):
            raise SelcError(
                "INVALID_SNAPSHOT",
                f"{kind} sample misses required identity or result fields",
                "export a fresh snapshot",
            )
        subject_id = sample[id_key]
        if not isinstance(subject_id, str) or not subject_id or subject_id in seen_ids:
            raise SelcError(
                "INVALID_SNAPSHOT",
                f"{kind} contains an invalid or duplicate id",
                "export a fresh snapshot",
            )
        seen_ids.add(subject_id)
        if not isinstance(sample["suite_id"], str) or not sample["suite_id"]:
            raise SelcError(
                "INVALID_SNAPSHOT", f"{kind} suite id is invalid", "export a fresh snapshot"
            )
        if not isinstance(sample["started_at"], str) or not sample["started_at"]:
            raise SelcError(
                "INVALID_SNAPSHOT", f"{kind} start time is invalid", "export a fresh snapshot"
            )
        if type(sample["is_test"]) not in {bool, int} or int(sample["is_test"]) not in {0, 1}:
            raise SelcError(
                "INVALID_SNAPSHOT", f"{kind} test flag is invalid", "export a fresh snapshot"
            )
        for result_key in ("first_result", "current_result"):
            result = sample[result_key]
            if result is not None and (
                not isinstance(result, str) or result not in {"pass", "fail", "unknown"}
            ):
                raise SelcError(
                    "INVALID_SNAPSHOT",
                    f"{kind} result is invalid",
                    "export a fresh snapshot",
                )
        if kind == "calls":
            if not isinstance(sample["task_id"], str) or not sample["task_id"]:
                raise SelcError(
                    "INVALID_SNAPSHOT", "call task link is invalid", "export a fresh snapshot"
                )
            if not isinstance(sample["skill"], str) or not sample["skill"]:
                raise SelcError(
                    "INVALID_SNAPSHOT", "call skill is invalid", "export a fresh snapshot"
                )
        value = dict(sample)
        value["is_test"] = int(sample["is_test"])
        for field in provenance:
            if field not in value or value[field] is None:
                value[field] = "unknown"
            elif not isinstance(value[field], str):
                raise SelcError(
                    "INVALID_SNAPSHOT",
                    f"{kind} provenance field {field} is invalid",
                    "export a fresh snapshot",
                )
        normalized.append(value)
    return normalized


def import_snapshot(database: sqlite3.Connection, input_path: Path) -> dict[str, Any]:
    snapshot = read_json(input_path)
    snapshot = _validate_snapshot(snapshot)
    origin_id = str(snapshot["origin_id"])
    revision = int(snapshot["snapshot_revision"])
    snapshot_hash = str(snapshot["snapshot_hash"])
    if origin_id == _origin_id(database):
        return {"imported": "noop_same_origin", "origin_id": origin_id}
    with database:
        previous = database.execute(
            "SELECT * FROM imported_snapshots WHERE origin_id = ?", (origin_id,)
        ).fetchone()
        if previous is not None:
            if previous["revision"] == revision:
                if previous["snapshot_hash"] == snapshot_hash:
                    return {"imported": "noop_same_revision", "origin_id": origin_id}
                raise SelcError(
                    "SNAPSHOT_CONFLICT",
                    "same origin and revision have different content",
                    "resolve the source snapshot conflict",
                )
            if previous["revision"] > revision:
                return {"imported": "ignored_older_revision", "origin_id": origin_id}
            database.execute("DELETE FROM imported_samples WHERE origin_id = ?", (origin_id,))
        for subject_kind, samples, id_key in (
            ("task", snapshot["tasks"], "task_id"),
            ("call", snapshot["calls"], "call_id"),
        ):
            for sample in samples:
                if not isinstance(sample, dict) or not isinstance(sample.get(id_key), str):
                    raise SelcError(
                        "INVALID_SNAPSHOT",
                        f"invalid {subject_kind} sample",
                        "export a fresh snapshot",
                    )
                subject_id = sample[id_key]
                suppressed = database.execute(
                    "SELECT 1 FROM suppressions WHERE origin_id = ? AND subject_kind = ? AND subject_id = ?",
                    (origin_id, subject_kind, subject_id),
                ).fetchone()
                deleted_by_range = _snapshot_in_deleted_range(
                    database,
                    str(sample.get("suite_id", "unknown")),
                    str(sample.get("started_at", "")),
                )
                if suppressed is None and not deleted_by_range:
                    database.execute(
                        """INSERT INTO imported_samples(
                        origin_id, subject_kind, subject_id, suite_id, suite_version, harness,
                        is_test, started_at, initiator, skill, executor, evidence, trigger_kind,
                        expected_hash, first_result, current_result, imported_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            origin_id,
                            subject_kind,
                            subject_id,
                            str(sample.get("suite_id", "unknown")),
                            str(sample.get("suite_version", "unknown")),
                            str(sample.get("harness", "unknown")),
                            int(bool(sample.get("is_test", False))),
                            str(sample.get("started_at", "")),
                            sample.get("initiator"),
                            sample.get("skill"),
                            sample.get("executor"),
                            sample.get("evidence"),
                            sample.get("trigger_kind"),
                            sample.get("expected_hash"),
                            sample.get("first_result"),
                            sample.get("current_result"),
                            utc_now(),
                        ),
                    )
        database.execute(
            "INSERT OR REPLACE INTO imported_snapshots(origin_id, revision, snapshot_hash, imported_at) VALUES (?, ?, ?, ?)",
            (origin_id, revision, snapshot_hash, utc_now()),
        )
    return {
        "imported": "replaced_origin_snapshot",
        "origin_id": origin_id,
        "snapshot_revision": revision,
    }


def _snapshot_in_deleted_range(
    database: sqlite3.Connection, suite_id: str, started_at: str
) -> bool:
    """Keep an old minimal snapshot from restoring a locally deleted sample."""

    try:
        rows = database.execute(
            """SELECT suite_id, before_time FROM observation_deletion_ranges
            WHERE suite_id = ? OR suite_id = '*'""",
            (suite_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        return False
    return any(row["before_time"] is None or started_at < row["before_time"] for row in rows)


def delete_records(
    database: sqlite3.Connection,
    *,
    suite_id: str | None,
    before: str | None,
    apply: bool,
) -> dict[str, Any]:
    _register_writer_protocol(database)
    filters = []
    imported_filters = []
    arguments: list[object] = []
    if suite_id:
        filters.append("suite_id = ?")
        imported_filters.append("suite_id = ?")
        arguments.append(suite_id)
    if before:
        filters.append("created_at < ?")
        imported_filters.append("started_at < ?")
        arguments.append(before)
    where = f"WHERE {' AND '.join(filters)}" if filters else ""
    imported_where = f"WHERE {' AND '.join(imported_filters)}" if imported_filters else ""

    def selection() -> tuple[
        list[sqlite3.Row], list[sqlite3.Row], list[sqlite3.Row], dict[str, Any]
    ]:
        local_rows = database.execute(
            f"SELECT task_id, owner_origin, status, suite_id, created_at FROM tasks {where}", arguments
        ).fetchall()
        imported_rows = database.execute(
            f"SELECT origin_id, subject_kind, subject_id FROM imported_samples {imported_where}",
            arguments,
        ).fetchall()
        preview = {
            "local_tasks": len(local_rows),
            "imported_samples": len(imported_rows),
            "apply": apply,
        }
        observation_filters = []
        observation_arguments: list[object] = []
        if suite_id:
            observation_filters.append("suite_id = ?")
            observation_arguments.append(suite_id)
        if before:
            observation_filters.append("occurred_at < ?")
            observation_arguments.append(before)
        observation_where = (
            f"WHERE {' AND '.join(observation_filters)}" if observation_filters else ""
        )
        observation_rows = database.execute(
            """SELECT event_id, stable_identity, source_context, source_epoch, suite_id, task_id
            FROM observation_events """
            + observation_where,
            observation_arguments,
        ).fetchall()
        preview["observations"] = len(observation_rows)
        local_task_ids = [str(row["task_id"]) for row in local_rows]
        if local_task_ids:
            placeholders = ", ".join("?" for _ in local_task_ids)
            active_calls = int(
                database.execute(
                    f"SELECT COUNT(*) FROM calls WHERE status = 'active' AND task_id IN ({placeholders})",
                    local_task_ids,
                ).fetchone()[0]
            )
            active_sessions = int(
                database.execute(
                    f"SELECT COUNT(*) FROM sessions WHERE status = 'active' "
                    f"AND session_id IN ({placeholders})",
                    [f"session:{task_id}" for task_id in local_task_ids],
                ).fetchone()[0]
            )
        else:
            active_calls = 0
            active_sessions = 0
        active_tasks = sum(row["status"] == "active" for row in local_rows)
        preview["active_leases"] = {
            "tasks": active_tasks,
            "calls": active_calls,
            "sessions": active_sessions,
        }
        return local_rows, imported_rows, observation_rows, preview

    if not apply:
        _, _, _, preview = selection()
        return {"preview": preview}

    # A reserved SQLite write transaction closes the direct-API race as well as
    # the CLI path: a concurrent start either finishes before this recheck (and
    # causes ACTIVE_LEASE) or receives a busy error before it can create a session.
    with database:
        database.execute("BEGIN IMMEDIATE")
        deletion_boundary = before or utc_now()
        local_rows, imported_rows, observation_rows, preview = selection()
        active_leases = preview["active_leases"]
        if any(int(count) for count in active_leases.values()):
            raise SelcError(
                "ACTIVE_LEASE",
                "cannot delete records that still own active tasks, calls, or sessions",
                "close the active task, call, and session explicitly before applying deletion",
            )
        deleted_suite = suite_id or "*"
        deleted_epoch = int(
            database.execute(
                "SELECT COALESCE(MAX(delete_epoch), 0) + 1 FROM observation_deletion_ranges"
            ).fetchone()[0]
        )
        database.execute(
            """INSERT INTO observation_deletion_ranges(
            suite_id, before_time, delete_epoch, created_at) VALUES (?, ?, ?, ?)""",
            (deleted_suite, deletion_boundary, deleted_epoch, utc_now()),
        )
        for observation in observation_rows:
            stable_identity = observation["stable_identity"]
            if stable_identity is not None:
                database.execute(
                    """INSERT OR REPLACE INTO observation_tombstones(
                    stable_identity, source_context, source_epoch, suite_id, task_id, deleted_at)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        stable_identity,
                        observation["source_context"],
                        observation["source_epoch"],
                        observation["suite_id"],
                        observation["task_id"],
                        utc_now(),
                    ),
                )
            database.execute(
                """INSERT INTO observation_source_epochs(
                source_context, invalid_before_epoch, deleted_at) VALUES (?, ?, ?)
                ON CONFLICT(source_context) DO UPDATE SET
                  invalid_before_epoch = MAX(
                    observation_source_epochs.invalid_before_epoch,
                    excluded.invalid_before_epoch
                  ),
                  deleted_at = excluded.deleted_at""",
                (observation["source_context"], observation["source_epoch"], utc_now()),
            )
            database.execute(
                "DELETE FROM observation_conflicts WHERE original_event_id = ?",
                (observation["event_id"],),
            )
            database.execute(
                "DELETE FROM observation_events WHERE event_id = ?", (observation["event_id"],)
            )
        for row in local_rows:
            database.execute(
                "INSERT OR IGNORE INTO suppressions(origin_id, subject_kind, subject_id, created_at) VALUES (?, 'task', ?, ?)",
                (row["owner_origin"], row["task_id"], utc_now()),
            )
            database.execute("DELETE FROM deliveries WHERE task_id = ?", (row["task_id"],))
            database.execute("DELETE FROM feedback_revisions WHERE task_id = ?", (row["task_id"],))
            database.execute("DELETE FROM feedback WHERE task_id = ?", (row["task_id"],))
            call_rows = database.execute(
                "SELECT call_id FROM calls WHERE task_id = ?", (row["task_id"],)
            ).fetchall()
            for call in call_rows:
                database.execute(
                    """INSERT OR IGNORE INTO suppressions(
                    origin_id, subject_kind, subject_id, created_at) VALUES (?, 'call', ?, ?)""",
                    (row["owner_origin"], call["call_id"], utc_now()),
                )
            database.execute("DELETE FROM calls WHERE task_id = ?", (row["task_id"],))
            database.execute("DELETE FROM events WHERE task_id = ?", (row["task_id"],))
            database.execute(
                "DELETE FROM observation_assessments WHERE task_id = ?", (row["task_id"],)
            )
            database.execute(
                "DELETE FROM observation_call_assessments WHERE task_id = ?", (row["task_id"],)
            )
            database.execute("DELETE FROM tasks WHERE task_id = ?", (row["task_id"],))
        for row in imported_rows:
            database.execute(
                "INSERT OR IGNORE INTO suppressions(origin_id, subject_kind, subject_id, created_at) VALUES (?, ?, ?, ?)",
                (row["origin_id"], row["subject_kind"], row["subject_id"], utc_now()),
            )
        database.execute(f"DELETE FROM imported_samples {imported_where}", arguments)
        _bump_snapshot_revision(database)
    return {"deleted": preview}


def _bump_snapshot_revision(database: sqlite3.Connection) -> int:
    revision = (
        int(
            database.execute("SELECT value FROM meta WHERE key = 'snapshot_revision'").fetchone()[0]
        )
        + 1
    )
    database.execute("UPDATE meta SET value = ? WHERE key = 'snapshot_revision'", (str(revision),))
    return revision
