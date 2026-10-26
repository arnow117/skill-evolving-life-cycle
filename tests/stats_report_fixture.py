"""Create deterministic, non-sensitive input data for the browser report test."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from selc import records
from selc.stats_report import generate_stats_report

MALICIOUS_SUITE = '</script><!--<script><img src=x onerror="window.injected=true">'


def _record_project(
    project: Path,
    *,
    suite: str,
    version: str,
    harness: str,
    test_suite: str | None = None,
    test_harness: str | None = None,
) -> None:
    database = records.open_database(project)
    try:
        records.start_task(
            database,
            task_id="task-main",
            suite_id=suite,
            suite_version=version,
            harness=harness,
            initiator="manual",
            evidence="fixture-only",
            is_test=False,
        )
        records.start_task(
            database,
            task_id="task-test-filter",
            suite_id=test_suite or suite,
            suite_version=version,
            harness=test_harness or harness,
            initiator="fixture",
            evidence="fixture-only",
            is_test=True,
        )
        records.start_task(
            database,
            task_id="task-pass",
            suite_id=suite,
            suite_version=version,
            harness=harness,
            initiator="manual",
            evidence="fixture-only",
            is_test=False,
        )
        records.start_task(
            database,
            task_id="task-fail",
            suite_id=suite,
            suite_version=version,
            harness=harness,
            initiator="manual",
            evidence="fixture-only",
            is_test=False,
        )
        if suite == "suite-beta":
            records.start_task(
                database,
                task_id="task-alternate",
                suite_id=suite,
                suite_version="3.0",
                harness="codex",
                initiator="manual",
                evidence="fixture-only",
                is_test=False,
            )
        records.start_call(
            database,
            call_id="call-main",
            task_id="task-main",
            skill="summary",
            executor=harness,
            evidence="fixture-only",
            expected_version=version,
            expected_hash="fixture-hash",
            trigger_kind="explicit",
        )
        database.execute(
            "UPDATE tasks SET first_result = 'pass', current_result = 'fail' WHERE task_id = 'task-main'"
        )
        database.execute(
            "UPDATE tasks SET first_result = 'unknown', current_result = 'unknown' WHERE task_id = 'task-test-filter'"
        )
        database.execute(
            "UPDATE tasks SET first_result = 'pass', current_result = 'pass' WHERE task_id = 'task-pass'"
        )
        database.execute(
            "UPDATE tasks SET first_result = 'fail', current_result = 'fail' WHERE task_id = 'task-fail'"
        )
        database.execute(
            "UPDATE tasks SET first_result = 'fail', current_result = 'pass' WHERE task_id = 'task-alternate'"
        )
        database.execute(
            "UPDATE calls SET first_result = 'pass', result = 'fail' WHERE call_id = 'call-main'"
        )
        database.commit()
    finally:
        database.close()


def _record_conflict_project(project: Path) -> None:
    database = records.open_database(project)
    try:
        for task_id, is_test, first_result, current_result in (
            ("shared-prod", False, "pass", "pass"),
            ("shared-test", True, "fail", "fail"),
        ):
            records.start_task(
                database,
                task_id=task_id,
                suite_id="conflict-suite",
                suite_version="1.0",
                harness="codex",
                initiator="manual",
                evidence="fixture-conflict",
                is_test=is_test,
            )
            records.start_call(
                database,
                call_id=f"call-{task_id}",
                task_id=task_id,
                skill="summary",
                executor="codex",
                evidence="fixture-conflict",
                expected_version="1.0",
                expected_hash="fixture-conflict-hash",
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
        for origin_id, subject_id, call_id, is_test, first_result, current_result in (
            ("test-copy", "shared-prod", "call-shared-prod", True, "fail", "fail"),
            ("production-copy", "shared-test", "call-shared-test", False, "pass", "pass"),
        ):
            _insert_imported_conflict(
                database,
                origin_id=origin_id,
                subject_id=subject_id,
                call_id=call_id,
                is_test=is_test,
                first_result=first_result,
                current_result=current_result,
            )
        database.commit()
    finally:
        database.close()


def _insert_imported_conflict(
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
        "suite_id": "conflict-suite",
        "suite_version": "1.0",
        "harness": "codex",
        "is_test": int(is_test),
        "started_at": "2026-10-26T00:00:00Z",
        "initiator": "manual",
        "skill": "summary",
        "executor": "codex",
        "evidence": "fixture-conflict",
        "trigger_kind": "explicit",
        "expected_hash": "fixture-conflict-hash",
        "first_result": first_result,
        "current_result": current_result,
        "imported_at": "2026-10-26T00:00:00Z",
    }
    for kind, identifier in (("task", subject_id), ("call", call_id)):
        database.execute(
            """INSERT OR REPLACE INTO imported_samples(
            origin_id, subject_kind, subject_id, suite_id, suite_version, harness,
            is_test, started_at, initiator, skill, executor, evidence, trigger_kind,
            expected_hash, first_result, current_result, imported_at)
            VALUES (
            :origin_id, :subject_kind, :subject_id, :suite_id, :suite_version, :harness,
            :is_test, :started_at, :initiator, :skill, :executor, :evidence, :trigger_kind,
            :expected_hash, :first_result, :current_result, :imported_at)""",
            {**values, "subject_kind": kind, "subject_id": identifier},
        )


def _record_unknown_project(project: Path) -> None:
    database = records.open_database(project)
    try:
        records.start_task(
            database,
            task_id="unknown-task",
            suite_id="unknown-suite",
            suite_version="1.0",
            harness="codex",
            initiator="manual",
            evidence="fixture-only",
            is_test=False,
        )
        database.execute(
            "UPDATE tasks SET first_result = 'unknown', current_result = 'unknown' WHERE task_id = 'unknown-task'"
        )
        database.commit()
    finally:
        database.close()


def _record_test_only_project(project: Path) -> None:
    database = records.open_database(project)
    try:
        records.start_task(
            database,
            task_id="test-only-task",
            suite_id="test-only-suite",
            suite_version="1.0",
            harness="codex",
            initiator="fixture",
            evidence="fixture-only",
            is_test=True,
        )
        records.start_call(
            database,
            call_id="test-only-call",
            task_id="test-only-task",
            skill="summary",
            executor="codex",
            evidence="fixture-only",
            expected_version="1.0",
            expected_hash="fixture-hash",
            trigger_kind="explicit",
        )
        database.execute(
            "UPDATE tasks SET first_result = 'pass', current_result = 'pass' WHERE task_id = 'test-only-task'"
        )
        database.execute(
            "UPDATE calls SET first_result = 'pass', result = 'pass' WHERE call_id = 'test-only-call'"
        )
        database.commit()
    finally:
        database.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--include-test", action="store_true")
    arguments = parser.parse_args()
    root = arguments.output.parent
    root.mkdir(parents=True, exist_ok=True)
    alpha = root / "alpha"
    beta = root / "beta"
    basic = root / "basic"
    conflict = root / "conflict"
    malicious = root / "malicious"
    unknown = root / "unknown"
    test_only = root / "test-only"
    _record_project(
        alpha,
        suite="suite-alpha",
        version="1.0",
        harness="codex",
        test_suite="suite-test",
        test_harness="workbuddy",
    )
    _record_project(beta, suite="suite-beta", version="2.0", harness="workbuddy")
    _record_conflict_project(conflict)
    _record_project(malicious, suite=MALICIOUS_SUITE, version="1.0", harness="codex")
    _record_unknown_project(unknown)
    _record_test_only_project(test_only)
    registry = basic / ".selc" / "installations.json"
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(
        '{"schema_version":1,"installations":{"fixture":{"mode":"basic"}}}',
        encoding="utf-8",
    )
    generate_stats_report(
        [alpha, beta, root / "unavailable", basic, conflict, malicious, unknown, test_only],
        arguments.output,
        include_test=arguments.include_test,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
