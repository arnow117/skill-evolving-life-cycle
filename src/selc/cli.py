"""The single argparse schema for SELC's portable command-line interface."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from . import RUNTIME_VERSION, SCHEMA_VERSION
from .errors import SelcError
from .evolve import (
    confirm_release,
    evaluate_candidate,
    execute_contribution,
    prepare_candidate,
    prepare_contribution,
    prepare_release,
    query_remote_release,
    release_local,
    release_remote,
    verify_contribution,
    verify_local_release,
)
from .install import (
    current_installation,
    framework_update_check,
    install_root,
    install_suite,
    record_daily_update_check,
    recover_installation,
    registry_lock_path,
    update_check,
    verify_expected_version,
)
from .manifest import (
    create_suite,
    load_manifest,
    package_framework,
    package_suite,
    render_text_report,
    seal_suite,
)
from .records import (
    close_task,
    complete_call,
    delete_records,
    export_snapshot,
    import_snapshot,
    open_database,
    record_delivery,
    record_feedback,
    set_task_event,
    start_call,
    start_task,
    stats,
)
from .util import exclusive_lock, file_hash


class SelcArgumentParser(argparse.ArgumentParser):
    """Keep invalid invocation failures in the same machine-readable contract as runtime errors."""

    def error(self, message: str) -> None:
        raise SelcError(
            "INVALID_ARGUMENT", message, "run selc --help and supply the required arguments"
        )


def _path(value: str) -> Path:
    return Path(value).expanduser().resolve()


def _parser() -> argparse.ArgumentParser:
    parser = SelcArgumentParser(
        prog="selc", description="Portable Skill Evolving Life Cycle runtime"
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    create = subcommands.add_parser("create", help="create a portable skill suite")
    create.add_argument("--suite-id", required=True)
    create.add_argument("--version", required=True)
    create.add_argument("--output", type=_path, required=True)
    create.add_argument("--skill", action="append", required=True)

    validate = subcommands.add_parser("validate", help="validate a suite manifest and skill files")
    validate.add_argument("--suite", type=_path, required=True)

    seal = subcommands.add_parser("seal", help="refresh a validated authoring suite content hash")
    seal.add_argument("--suite", type=_path, required=True)

    package = subcommands.add_parser("package", help="create a full portable suite package")
    package.add_argument("--suite", type=_path, required=True)
    package.add_argument("--output", type=_path, required=True)
    package.add_argument("--framework-only", action="store_true")

    install = subcommands.add_parser(
        "install", help="install a suite into a project harness entrypoint"
    )
    install.add_argument("--source", required=True)
    install.add_argument("--project-root", type=_path, required=True)
    install.add_argument("--harness", choices=["codex", "workbuddy"], required=True)
    install.add_argument("--ref")
    install.add_argument("--subdir")
    install.add_argument("--framework-source")
    install.add_argument("--framework-ref")
    install.add_argument("--framework-subdir")

    install_local = subcommands.add_parser("install-local", help="portable package installer")
    install_local.add_argument("--project-root", type=_path, required=True)
    install_local.add_argument("--harness", choices=["codex", "workbuddy"], required=True)

    task = subcommands.add_parser("task", help="record a stable business task")
    task_commands = task.add_subparsers(dest="task_command", required=True)
    task_start = task_commands.add_parser("start")
    _project(task_start)
    task_start.add_argument("--id", required=True)
    task_start.add_argument("--suite-id", required=True)
    task_start.add_argument("--initiator", required=True)
    task_start.add_argument("--evidence", required=True)
    task_start.add_argument(
        "--harness", default="unknown", choices=["codex", "workbuddy", "unknown"]
    )
    task_start.add_argument("--test", action="store_true")
    task_close = task_commands.add_parser("close")
    _project(task_close)
    task_close.add_argument("--id", required=True)

    call = subcommands.add_parser("call", help="record a stable invocation under a task")
    call_commands = call.add_subparsers(dest="call_command", required=True)
    call_start = call_commands.add_parser("start")
    _project(call_start)
    call_start.add_argument("--id", required=True)
    call_start.add_argument("--task-id", required=True)
    call_start.add_argument("--skill", required=True)
    call_start.add_argument("--executor", required=True)
    call_start.add_argument("--evidence", required=True)
    call_start.add_argument("--expected-version", required=True)
    call_start.add_argument("--expected-hash")
    call_start.add_argument(
        "--trigger", default="explicit", choices=["explicit", "automatic", "retry"]
    )
    call_complete = call_commands.add_parser("complete")
    _project(call_complete)
    call_complete.add_argument("--id", required=True)
    call_complete.add_argument("--result", required=True, choices=["pass", "fail", "unknown"])

    event = subcommands.add_parser(
        "event", help="record input, permission, intervention, or session evidence"
    )
    _project(event)
    event.add_argument("--task-id", required=True)
    event.add_argument(
        "--kind",
        required=True,
        choices=[
            "input_ready",
            "permission_click",
            "business_intervention",
            "session_start",
            "session_close",
        ],
    )
    event.add_argument("--evidence", required=True)

    delivery = subcommands.add_parser("delivery", help="record an artifact check")
    _project(delivery)
    delivery.add_argument("--id", required=True)
    delivery.add_argument("--task-id", required=True)
    delivery.add_argument("--call-id")
    delivery.add_argument("--artifact-hash", required=True)
    delivery.add_argument("--rule-version", required=True)
    delivery.add_argument("--checker")
    delivery.add_argument("--input", type=_path)
    delivery.add_argument("--output", type=_path)
    delivery.add_argument("--result", required=True, choices=["pass", "fail", "unknown"])
    delivery.add_argument("--complete-observation-fixture", action="store_true")

    feedback = subcommands.add_parser("feedback", help="record a revisioned user feedback result")
    _project(feedback)
    feedback.add_argument("--id", required=True)
    feedback.add_argument("--task-id", required=True)
    feedback.add_argument("--delivery-id", required=True)
    feedback.add_argument("--revision", type=int, required=True)
    feedback.add_argument(
        "--scope", required=True, choices=["first", "final", "requirement_change"]
    )
    feedback.add_argument("--result", required=True, choices=["pass", "fail", "unknown"])
    feedback.add_argument("--body")

    statistics = subcommands.add_parser(
        "stats", help="show independent task and invocation metrics"
    )
    _project(statistics)
    statistics.add_argument("--include-test", action="store_true")

    export = subcommands.add_parser("export", help="export a minimal owner snapshot")
    _project(export)
    export.add_argument("--output", type=_path, required=True)
    importing = subcommands.add_parser(
        "import", help="import a complete minimal snapshot transactionally"
    )
    _project(importing)
    importing.add_argument("--input", type=_path, required=True)
    deleting = subcommands.add_parser("delete", help="preview or apply local record deletion")
    _project(deleting)
    deleting.add_argument("--suite-id")
    deleting.add_argument("--before")
    deleting.add_argument("--apply", action="store_true")

    updates = subcommands.add_parser("updates", help="inspect or explicitly apply a suite update")
    update_commands = updates.add_subparsers(dest="updates_command", required=True)
    update_check_parser = update_commands.add_parser("check")
    update_check_parser.add_argument("--source")
    update_check_parser.add_argument("--business-source")
    update_check_parser.add_argument("--framework-source")
    update_check_parser.add_argument("--ref")
    update_check_parser.add_argument("--subdir")
    update_check_parser.add_argument("--framework-ref")
    update_check_parser.add_argument("--framework-subdir")
    update_check_parser.add_argument("--force", action="store_true")
    update_apply = update_commands.add_parser("apply")
    update_apply.add_argument("--source")
    update_apply.add_argument("--business-source")
    update_apply.add_argument("--project-root", type=_path, required=True)
    update_apply.add_argument("--harness", choices=["codex", "workbuddy"], required=True)
    update_apply.add_argument("--suite-id")
    update_apply.add_argument("--ref")
    update_apply.add_argument("--subdir")
    update_apply.add_argument("--framework-source")
    update_apply.add_argument("--framework-ref")
    update_apply.add_argument("--framework-subdir")
    update_recover = update_commands.add_parser("recover")
    _project(update_recover)
    update_recover.add_argument("--suite-id", required=True)

    evolve = subcommands.add_parser("evolve", help="prepare and evaluate an isolated candidate")
    evolve_commands = evolve.add_subparsers(dest="evolve_command", required=True)
    evolve_prepare = evolve_commands.add_parser("prepare")
    evolve_prepare.add_argument("--suite", type=_path, required=True)
    evolve_prepare.add_argument("--output", type=_path, required=True)
    evolve_evaluate = evolve_commands.add_parser("evaluate")
    evolve_evaluate.add_argument("--candidate", type=_path, required=True)
    evolve_evaluate.add_argument("--output", type=_path, required=True)
    evolve_evaluate.add_argument("--max-runs", type=int, default=8)
    evolve_evaluate.add_argument("--model-calls", type=int, default=0)
    evolve_evaluate.add_argument("--candidate-number", type=int, default=1)
    evolve_evaluate.add_argument("--candidate-limit", type=int, default=2)
    evolve_evaluate.add_argument("--run-timeout-seconds", type=int, default=10)
    evolve_evaluate.add_argument("--total-timeout-seconds", type=int, default=60)

    contribute = subcommands.add_parser("contribute", help="prepare a branch-oriented contribution")
    contribute_commands = contribute.add_subparsers(dest="contribute_command", required=True)
    contribution_prepare = contribute_commands.add_parser("prepare")
    contribution_prepare.add_argument("--candidate", type=_path, required=True)
    contribution_prepare.add_argument("--suite", type=_path, required=True)
    contribution_prepare.add_argument("--evaluation", type=_path, required=True)
    contribution_prepare.add_argument("--source-repo", type=_path)
    contribution_prepare.add_argument("--project-root", type=_path)
    contribution_prepare.add_argument("--suite-id")
    contribution_prepare.add_argument("--workspace", type=_path, required=True)
    contribution_prepare.add_argument("--output", type=_path, required=True)
    contribution_execute = contribute_commands.add_parser("execute")
    contribution_execute.add_argument("--plan", type=_path, required=True)
    contribution_execute.add_argument(
        "--provider", choices=["local", "github", "gitlab"], required=True
    )
    contribution_execute.add_argument("--repository")
    contribution_execute.add_argument("--client")
    contribution_verify = contribute_commands.add_parser("verify")
    contribution_verify.add_argument("--plan", type=_path, required=True)
    contribution_verify.add_argument("--provider", choices=["github", "gitlab"], required=True)
    contribution_verify.add_argument("--client", required=True)

    release = subcommands.add_parser(
        "release", help="prepare, confirm, query, or execute a full suite release"
    )
    release_commands = release.add_subparsers(dest="release_command", required=True)
    release_prepare = release_commands.add_parser("prepare")
    release_prepare.add_argument("--suite", type=_path, required=True)
    release_prepare.add_argument("--candidate", type=_path, required=True)
    release_prepare.add_argument("--evaluation", type=_path, required=True)
    release_prepare.add_argument("--provider", choices=["local", "github", "gitlab"], required=True)
    release_prepare.add_argument("--target", required=True)
    release_prepare.add_argument("--tag", required=True)
    release_prepare.add_argument("--center", type=_path)
    release_prepare.add_argument("--repository")
    release_prepare.add_argument("--output", type=_path, required=True)
    release_confirm = release_commands.add_parser("confirm")
    release_confirm.add_argument("--plan", type=_path, required=True)
    release_confirm.add_argument("--summary-hash", required=True)
    release_confirm.add_argument("--output", type=_path, required=True)
    release_execute = release_commands.add_parser("execute")
    release_execute.add_argument("--plan", type=_path, required=True)
    release_execute.add_argument("--confirmation", type=_path, required=True)
    release_execute.add_argument("--center", type=_path)
    release_execute.add_argument("--client")
    release_verify = release_commands.add_parser("verify")
    release_verify.add_argument("--center", type=_path, required=True)
    release_verify.add_argument("--tag", required=True)
    release_query = release_commands.add_parser("query")
    release_query.add_argument("--provider", choices=["github", "gitlab"], required=True)
    release_query.add_argument("--client", required=True)
    release_query.add_argument("--tag", required=True)
    release_query.add_argument("--repository", required=True)

    run = subcommands.add_parser("run", help="run the deterministic text report example")
    run.add_argument("skill", choices=["text-report"])
    run.add_argument("--input", type=_path, required=True)
    run.add_argument("--output", type=_path, required=True)
    run.add_argument("--suite", type=_path)

    doctor = subcommands.add_parser("doctor", help="report the portable runtime contract")
    doctor.add_argument("--suite", type=_path)
    return parser


def _project(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project-root", type=_path, required=True)


def _render_text_report(
    input_path: Path, output_path: Path, suite_root: Path | None
) -> dict[str, Any]:
    if suite_root is None:
        raise SelcError(
            "MISSING_SUITE",
            "text-report needs an installed or explicit suite resource root",
            "pass --suite or invoke the generated suite wrapper",
        )
    load_manifest(suite_root)
    text = input_path.read_text(encoding="utf-8")
    report = render_text_report(text, suite_root / "resources")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report, encoding="utf-8")
    return {
        "output": str(output_path),
        "artifact_hash": file_hash(output_path),
        "lines": len(lines),
    }


def _verify_delivery_checker(arguments: argparse.Namespace) -> tuple[bool, str | None]:
    """Run the one v0.1 trusted checker without executing caller-provided code."""

    if arguments.checker != "text-report-v1":
        return False, None
    if arguments.rule_version != "text-report-v1":
        return False, None
    if arguments.input is None or arguments.output is None:
        return False, None
    database = open_database(arguments.project_root)
    try:
        task = database.execute(
            "SELECT suite_id FROM tasks WHERE task_id = ?", (arguments.task_id,)
        ).fetchone()
    finally:
        database.close()
    if task is None:
        return False, None
    try:
        installation = current_installation(arguments.project_root, task["suite_id"])
        resources_root = (
            install_root(arguments.project_root)
            / task["suite_id"]
            / "contents"
            / installation["digest"]
            / "resources"
        )
        expected = render_text_report(arguments.input.read_text(encoding="utf-8"), resources_root)
        actual = arguments.output.read_text(encoding="utf-8")
    except (OSError, SelcError):
        return False, None
    output_hash_matches = (
        arguments.output.is_file() and file_hash(arguments.output) == arguments.artifact_hash
    )
    return True, "pass" if output_hash_matches and actual == expected else "fail"


def _dispatch(arguments: argparse.Namespace, default_suite_root: Path | None) -> dict[str, Any]:
    command = arguments.command
    if command == "create":
        return {
            "suite": create_suite(
                arguments.suite_id, arguments.version, arguments.output, arguments.skill
            )
        }
    if command == "validate":
        return {"suite": load_manifest(arguments.suite), "valid": True}
    if command == "seal":
        return {"suite": seal_suite(arguments.suite), "sealed": True}
    if command == "package":
        if arguments.framework_only:
            return package_framework(arguments.suite, arguments.output)
        return package_suite(arguments.suite, arguments.output)
    if command == "install":
        return {
            "installation": install_suite(
                source=arguments.source,
                project_root=arguments.project_root,
                harness=arguments.harness,
                ref=arguments.ref,
                subdir=arguments.subdir,
                framework_source=arguments.framework_source,
                framework_ref=arguments.framework_ref,
                framework_subdir=arguments.framework_subdir,
            )
        }
    if command == "install-local":
        if default_suite_root is None:
            raise SelcError(
                "MISSING_SUITE", "portable installer has no suite root", "run install with --source"
            )
        return {
            "installation": install_suite(
                source=default_suite_root,
                project_root=arguments.project_root,
                harness=arguments.harness,
                ref=None,
            )
        }
    if command == "task":
        if arguments.task_command == "start":
            lock_path = install_root(arguments.project_root) / arguments.suite_id / ".lock"
            with ExitStack() as locks:
                locks.enter_context(exclusive_lock(registry_lock_path(arguments.project_root)))
                locks.enter_context(exclusive_lock(lock_path))
                database = open_database(arguments.project_root)
                try:
                    try:
                        installation = current_installation(
                            arguments.project_root, arguments.suite_id
                        )
                        daily_check = record_daily_update_check(
                            arguments.project_root, arguments.suite_id
                        )
                        suite_version = installation["version"]
                    except SelcError as error:
                        if error.code != "MISSING_INSTALLATION":
                            raise
                        daily_check = {"checked": False, "reason": "suite_not_installed"}
                        suite_version = "unknown"
                    result = start_task(
                        database,
                        task_id=arguments.id,
                        suite_id=arguments.suite_id,
                        initiator=arguments.initiator,
                        evidence=arguments.evidence,
                        is_test=arguments.test,
                        suite_version=suite_version,
                        harness=arguments.harness,
                    )
                    return {"task": result, "daily_update_check": daily_check}
                finally:
                    database.close()
        database = open_database(arguments.project_root)
        try:
            return {"task": close_task(database, arguments.id)}
        finally:
            database.close()
    if command == "call":
        if arguments.call_command == "start":
            with ExitStack() as locks:
                locks.enter_context(exclusive_lock(registry_lock_path(arguments.project_root)))
                lookup = open_database(arguments.project_root)
                try:
                    task_row = lookup.execute(
                        "SELECT suite_id FROM tasks WHERE task_id = ?", (arguments.task_id,)
                    ).fetchone()
                finally:
                    lookup.close()
                if task_row is None:
                    raise SelcError(
                        "MISSING_TASK",
                        f"task does not exist: {arguments.task_id}",
                        "start the task before the call",
                    )
                lock_path = install_root(arguments.project_root) / task_row["suite_id"] / ".lock"
                locks.enter_context(exclusive_lock(lock_path))
                database = open_database(arguments.project_root)
                try:
                    verify_expected_version(
                        arguments.project_root,
                        task_row["suite_id"],
                        arguments.expected_version,
                        arguments.expected_hash,
                    )
                    result = start_call(
                        database,
                        call_id=arguments.id,
                        task_id=arguments.task_id,
                        skill=arguments.skill,
                        executor=arguments.executor,
                        evidence=arguments.evidence,
                        expected_version=arguments.expected_version,
                        expected_hash=arguments.expected_hash,
                        trigger_kind=arguments.trigger,
                    )
                finally:
                    database.close()
            return {"call": result}
        database = open_database(arguments.project_root)
        try:
            return {"call": complete_call(database, arguments.id, arguments.result)}
        finally:
            database.close()
    if command == "event":
        if arguments.kind == "session_start":
            with ExitStack() as locks:
                locks.enter_context(exclusive_lock(registry_lock_path(arguments.project_root)))
                lookup = open_database(arguments.project_root)
                try:
                    task_row = lookup.execute(
                        "SELECT suite_id FROM tasks WHERE task_id = ?", (arguments.task_id,)
                    ).fetchone()
                finally:
                    lookup.close()
                if task_row is None:
                    raise SelcError(
                        "MISSING_TASK",
                        f"task does not exist: {arguments.task_id}",
                        "start the task before opening its session",
                    )
                lock_path = install_root(arguments.project_root) / task_row["suite_id"] / ".lock"
                locks.enter_context(exclusive_lock(lock_path))
                current_installation(arguments.project_root, task_row["suite_id"])
                database = open_database(arguments.project_root)
                try:
                    result = set_task_event(
                        database, arguments.task_id, arguments.kind, arguments.evidence
                    )
                finally:
                    database.close()
            return {"event": result}
        database = open_database(arguments.project_root)
        try:
            return {
                "event": set_task_event(
                    database, arguments.task_id, arguments.kind, arguments.evidence
                )
            }
        finally:
            database.close()
    if command == "delivery":
        checker_verified, checker_result = _verify_delivery_checker(arguments)
        database = open_database(arguments.project_root)
        try:
            return {
                "delivery": record_delivery(
                    database,
                    delivery_id=arguments.id,
                    task_id=arguments.task_id,
                    artifact_hash=arguments.artifact_hash,
                    rule_version=arguments.rule_version,
                    declared_result=arguments.result,
                    complete_fixture=arguments.complete_observation_fixture,
                    call_id=arguments.call_id,
                    checker_id=arguments.checker,
                    checker_verified=checker_verified,
                    checker_result=checker_result,
                )
            }
        finally:
            database.close()
    if command == "feedback":
        database = open_database(arguments.project_root)
        try:
            return {
                "feedback": record_feedback(
                    database,
                    feedback_id=arguments.id,
                    task_id=arguments.task_id,
                    delivery_id=arguments.delivery_id,
                    revision=arguments.revision,
                    scope=arguments.scope,
                    result=arguments.result,
                    body=arguments.body,
                )
            }
        finally:
            database.close()
    if command == "stats":
        database = open_database(arguments.project_root)
        try:
            return stats(database, include_test=arguments.include_test)
        finally:
            database.close()
    if command == "export":
        database = open_database(arguments.project_root)
        try:
            return {"snapshot": export_snapshot(database, arguments.output)}
        finally:
            database.close()
    if command == "import":
        database = open_database(arguments.project_root)
        try:
            return import_snapshot(database, arguments.input)
        finally:
            database.close()
    if command == "delete":
        with ExitStack() as locks:
            locks.enter_context(exclusive_lock(registry_lock_path(arguments.project_root)))
            lookup = open_database(arguments.project_root)
            try:
                filters = []
                values: list[str] = []
                if arguments.suite_id:
                    filters.append("suite_id = ?")
                    values.append(arguments.suite_id)
                if arguments.before:
                    filters.append("created_at < ?")
                    values.append(arguments.before)
                where = f"WHERE {' AND '.join(filters)}" if filters else ""
                suite_rows = lookup.execute(
                    f"SELECT DISTINCT suite_id FROM tasks {where}", values
                ).fetchall()
            finally:
                lookup.close()
            for row in sorted(suite_rows, key=lambda item: str(item["suite_id"])):
                suite_lock = install_root(arguments.project_root) / row["suite_id"] / ".lock"
                locks.enter_context(exclusive_lock(suite_lock))
            database = open_database(arguments.project_root)
            try:
                return delete_records(
                    database,
                    suite_id=arguments.suite_id,
                    before=arguments.before,
                    apply=arguments.apply,
                )
            finally:
                database.close()
    if command == "updates":
        if arguments.updates_command == "check":
            business_source = arguments.business_source or arguments.source
            if business_source is None:
                raise SelcError(
                    "INVALID_INPUT",
                    "updates check needs --source or --business-source",
                    "provide a business source",
                )
            result = {
                "business": update_check(business_source, arguments.ref, arguments.subdir),
                "forced": arguments.force,
            }
            if arguments.framework_source:
                result["framework"] = framework_update_check(
                    arguments.framework_source,
                    arguments.framework_ref,
                    arguments.framework_subdir,
                )
            return result
        if arguments.updates_command == "apply":
            business_source = arguments.business_source or arguments.source
            business_ref = arguments.ref
            business_subdir = arguments.subdir
            existing: dict[str, Any] | None = None
            if arguments.suite_id:
                existing = current_installation(arguments.project_root, arguments.suite_id)
            if business_source is None:
                if existing is None:
                    raise SelcError(
                        "INVALID_INPUT",
                        "framework-only apply requires --suite-id for the installed business source",
                        "provide --source or select an installed suite",
                    )
                saved_source = existing["source"]
                business_source = saved_source.get("repo")
                business_ref = saved_source.get("commit") or saved_source.get("base")
                business_subdir = saved_source.get("subdir")
                if not business_source:
                    raise SelcError(
                        "MISSING_SOURCE_BASELINE",
                        "installed suite has no reusable business source",
                        "supply --source for this update",
                    )
            if existing is None:
                try:
                    resolved_business = update_check(business_source, business_ref, business_subdir)
                    existing = current_installation(
                        arguments.project_root, resolved_business["suite_id"]
                    )
                except SelcError as error:
                    if error.code != "MISSING_INSTALLATION":
                        raise
            framework_source = arguments.framework_source
            framework_ref = arguments.framework_ref
            framework_subdir = arguments.framework_subdir
            if framework_source is None and existing is not None:
                saved_framework = existing["framework_source"]
                if saved_framework.get("status") == "active_external":
                    framework_source = saved_framework["repo"]
                    framework_ref = saved_framework.get("commit") or saved_framework.get("base")
                    framework_subdir = saved_framework.get("subdir")
            return {
                "installation": install_suite(
                    source=business_source,
                    project_root=arguments.project_root,
                    harness=arguments.harness,
                    ref=business_ref,
                    subdir=business_subdir,
                    framework_source=framework_source,
                    framework_ref=framework_ref,
                    framework_subdir=framework_subdir,
                )
            }
        return recover_installation(arguments.project_root, arguments.suite_id)
    if command == "evolve":
        if arguments.evolve_command == "prepare":
            return prepare_candidate(arguments.suite, arguments.output)
        return evaluate_candidate(
            arguments.candidate,
            arguments.output,
            max_runs=arguments.max_runs,
            model_calls=arguments.model_calls,
            candidate_number=arguments.candidate_number,
            candidate_limit=arguments.candidate_limit,
            run_timeout_seconds=arguments.run_timeout_seconds,
            total_timeout_seconds=arguments.total_timeout_seconds,
        )
    if command == "contribute":
        if arguments.contribute_command == "prepare":
            return prepare_contribution(
                arguments.candidate,
                arguments.suite,
                arguments.evaluation,
                arguments.source_repo,
                arguments.workspace,
                arguments.output,
                arguments.project_root,
                arguments.suite_id,
            )
        if arguments.contribute_command == "execute":
            return execute_contribution(
                arguments.plan,
                arguments.provider,
                arguments.repository,
                arguments.client,
            )
        return verify_contribution(arguments.plan, arguments.provider, arguments.client)
    if command == "release":
        if arguments.release_command == "prepare":
            return prepare_release(
                arguments.suite,
                arguments.candidate,
                arguments.evaluation,
                arguments.provider,
                arguments.target,
                arguments.tag,
                arguments.output,
                arguments.center,
                arguments.repository,
            )
        if arguments.release_command == "confirm":
            return confirm_release(arguments.plan, arguments.summary_hash, arguments.output)
        if arguments.release_command == "execute":
            if arguments.center is not None:
                return release_local(arguments.plan, arguments.confirmation, arguments.center)
            if arguments.client:
                return release_remote(arguments.plan, arguments.confirmation, arguments.client)
            raise SelcError(
                "INVALID_INPUT",
                "release execute needs --center for local plans or --client for remote plans",
                "supply the destination bound by the release plan",
            )
        if arguments.release_command == "verify":
            return verify_local_release(arguments.center, arguments.tag)
        return query_remote_release(
            arguments.provider,
            arguments.client,
            arguments.tag,
            arguments.repository,
        )
    if command == "run":
        return _render_text_report(
            arguments.input,
            arguments.output,
            arguments.suite or default_suite_root,
        )
    if command == "doctor":
        result: dict[str, Any] = {
            "runtime_version": RUNTIME_VERSION,
            "schema_version": SCHEMA_VERSION,
            "stdlib_only": True,
        }
        if arguments.suite:
            result["suite"] = load_manifest(arguments.suite)["suite_id"]
        return result
    raise SelcError("UNKNOWN_COMMAND", f"unsupported command: {command}", "run selc --help")


def main(
    argv: list[str] | None = None,
    *,
    default_suite_root: Path | None = None,
    force_command: str | None = None,
) -> int:
    effective_arguments = list(sys.argv[1:] if argv is None else argv)
    if force_command is not None:
        effective_arguments = [force_command, *effective_arguments]
    parser = _parser()
    try:
        arguments = parser.parse_args(effective_arguments)
        output = _dispatch(arguments, default_suite_root)
    except SelcError as error:
        output = {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "error": {"code": error.code, "message": error.message, "recovery": error.recovery},
        }
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
        return 2
    except OSError as error:
        output = {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "error": {
                "code": "IO_ERROR",
                "message": str(error),
                "recovery": "inspect the filesystem path and retry",
            },
        }
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
        return 2
    except sqlite3.OperationalError as error:
        output = {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "error": {
                "code": "DATABASE_BUSY",
                "message": str(error),
                "recovery": "retry after the active record transaction completes",
            },
        }
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
        return 2
    print(
        json.dumps(
            {"schema_version": SCHEMA_VERSION, "ok": True, **output},
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0
