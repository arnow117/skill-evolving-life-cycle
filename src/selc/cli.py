"""The single argparse schema for SELC's portable command-line interface."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from . import RUNTIME_VERSION, SCHEMA_VERSION
from .checker_integration import (
    check_result_payload,
    inspect_script_checkers,
    register_script_checker,
    run_installed_check,
)
from .checkers import SubjectIdentity
from .creator import prepare_creator_request, verify_creator_result
from .errors import SelcError
from .evaluation import (
    bind_real_evaluation_candidate,
    collect_evaluation_attempt,
    desktop_request,
    finalize_real_evaluation,
    launch_codex_cli_attempt,
    native_evidence_template,
    prepare_evaluation_attempt,
    prepare_real_evaluation_plan,
    public_evaluation_summary,
    write_codex_policy_template,
)
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
    business_selection_snapshot,
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
from .managed_runtime import (
    activate as managed_activate,
)
from .managed_runtime import (
    activate_from_environment as managed_activate_from_environment,
)
from .managed_runtime import (
    health as managed_health,
)
from .managed_runtime import (
    observe_request_boundary,
    stage_instruction_artifact,
)
from .managed_runtime import (
    publish as managed_publish,
)
from .managed_runtime import (
    recover as recover_managed_runtime,
)
from .managed_runtime import (
    run_action as run_managed_action,
)
from .manifest import (
    create_suite,
    load_manifest,
    package_framework,
    package_suite,
    render_text_report,
    render_text_report_bytes,
    seal_suite,
)
from .native_adapter import current_environment, read_hook_payload, receive_native_hook
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
    create.add_argument(
        "--managed-declaration",
        type=_path,
        help=(
            "a protocol-1 declaration JSON file: managed actions/native_hosts plus optional "
            "top-level artifact_checks and skill_artifact_checks"
        ),
    )

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
    install.add_argument("--mode", choices=["basic", "observed"])

    install_local = subcommands.add_parser("install-local", help="portable package installer")
    install_local.add_argument("--project-root", type=_path, required=True)
    install_local.add_argument("--harness", choices=["codex", "workbuddy"], required=True)
    install_local.add_argument("--mode", choices=["basic", "observed"])

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
    call_start.add_argument(
        "--expected-hash",
        required=True,
        help="installed composite digest returned by install; stale or missing entries are rejected",
    )
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

    checker = subcommands.add_parser(
        "checker", help="register or inspect project-local trusted checker scripts"
    )
    checker_commands = checker.add_subparsers(dest="checker_command", required=True)
    checker_register = checker_commands.add_parser(
        "register-script", help="pin a maintainer-owned Python checker script"
    )
    _project(checker_register)
    checker_register.add_argument("--name", required=True)
    checker_register.add_argument("--version", required=True)
    checker_register.add_argument("--script", type=_path, required=True)
    checker_register.add_argument("--digest", required=True)
    checker_register.add_argument("--rule", action="append", required=True)
    checker_register.add_argument("--timeout-seconds", type=float, default=1.0)
    checker_inspect = checker_commands.add_parser(
        "inspect", help="show registered checker identities without running them"
    )
    _project(checker_inspect)

    check = subcommands.add_parser(
        "check", help="diagnose one declared installed-suite artifact check"
    )
    _project(check)
    check.add_argument("--suite", required=True)
    check.add_argument("--check-id", required=True)
    check.add_argument("--input", type=_path, required=True)
    check.add_argument("--artifact", type=_path, required=True)

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
    evolve_plan = evolve_commands.add_parser(
        "plan", help="freeze a real-harness evaluation plan without launching a provider"
    )
    evolve_plan.add_argument("--suite", type=_path, required=True)
    evolve_plan.add_argument("--cases", type=_path, required=True)
    evolve_plan.add_argument("--output", type=_path, required=True)
    evolve_policy_template = evolve_commands.add_parser(
        "policy-template",
        help=(
            "write the frozen Codex policy and normal hook-trust inheritance template "
            "before creating a plan"
        ),
    )
    evolve_policy_template.add_argument(
        "--host-version",
        required=True,
        help="the exact Codex version whose native policy shape was observed",
    )
    evolve_policy_template.add_argument(
        "--python-read-root",
        type=_path,
        required=True,
        help="the narrow readonly Python dependency root required by the fixed evaluator",
    )
    evolve_policy_template.add_argument("--output", type=_path, required=True)
    evolve_bind = evolve_commands.add_parser(
        "bind", help="bind one editable candidate to a frozen real evaluation plan"
    )
    evolve_bind.add_argument("--plan", type=_path, required=True)
    evolve_bind.add_argument("--candidate", type=_path, required=True)
    evolve_bind.add_argument("--output", type=_path, required=True)
    evolve_bind.add_argument(
        "--variant",
        help="a declared candidate version variant; required when the plan has more than one candidate variant",
    )
    evolve_attempt = evolve_commands.add_parser(
        "attempt", help="prepare, launch, or collect one frozen real-evaluation attempt"
    )
    evolve_attempt_commands = evolve_attempt.add_subparsers(dest="attempt_command", required=True)
    evolve_attempt_prepare = evolve_attempt_commands.add_parser("prepare")
    evolve_attempt_prepare.add_argument("--plan", type=_path, required=True)
    evolve_attempt_prepare.add_argument("--binding", type=_path, required=True)
    evolve_attempt_prepare.add_argument("--case", required=True)
    evolve_attempt_prepare.add_argument("--matrix", required=True)
    evolve_attempt_prepare.add_argument("--attempt", dest="attempt_number", type=int, required=True)
    evolve_attempt_prepare.add_argument("--output", type=_path, required=True)
    evolve_attempt_launch = evolve_attempt_commands.add_parser("launch")
    evolve_attempt_launch.add_argument("--attempt-path", type=_path, required=True)
    evolve_attempt_desktop = evolve_attempt_commands.add_parser("desktop-request")
    evolve_attempt_desktop.add_argument("--attempt-path", type=_path, required=True)
    evolve_attempt_evidence = evolve_attempt_commands.add_parser(
        "native-evidence-template",
        help="show the bounded raw native-session evidence required by collect",
    )
    evolve_attempt_evidence.add_argument("--attempt-path", type=_path, required=True)
    evolve_attempt_collect = evolve_attempt_commands.add_parser("collect")
    evolve_attempt_collect.add_argument("--attempt-path", type=_path, required=True)
    evolve_finalize = evolve_commands.add_parser(
        "finalize", help="reconstruct a real evaluation report from collected attempts"
    )
    evolve_finalize.add_argument("--plan", type=_path, required=True)
    evolve_finalize.add_argument(
        "--binding",
        type=_path,
        action="append",
        required=True,
        help="one immutable binding for each version variant included in the report",
    )
    evolve_finalize.add_argument("--attempt-path", type=_path, action="append", required=True)
    evolve_finalize.add_argument("--output", type=_path, required=True)
    evolve_summary = evolve_commands.add_parser(
        "summary", help="emit a pathless public summary of an evaluation report"
    )
    evolve_summary.add_argument("--report", type=_path, required=True)
    evolve_creator = evolve_commands.add_parser(
        "creator", help="prepare or verify an isolated creator-only candidate edit"
    )
    evolve_creator_commands = evolve_creator.add_subparsers(dest="creator_command", required=True)
    evolve_creator_prepare = evolve_creator_commands.add_parser("prepare")
    evolve_creator_prepare.add_argument("--plan", type=_path, required=True)
    evolve_creator_prepare.add_argument("--candidate", type=_path, required=True)
    evolve_creator_prepare.add_argument("--output", type=_path, required=True)
    evolve_creator_prepare.add_argument(
        "--brief",
        type=_path,
        required=True,
        help="maintainer-owned UTF-8 regression objective; never supply holdout or oracle material",
    )
    evolve_creator_prepare.add_argument(
        "--skill-creator-root",
        type=_path,
        required=True,
        help="trusted readonly skill-creator directory containing SKILL.md and scripts/quick_validate.py",
    )
    evolve_creator_prepare.add_argument(
        "--creator-python-root",
        type=_path,
        required=True,
        help="trusted readonly Python runtime dependency directory used by skill-creator",
    )
    evolve_creator_prepare.add_argument(
        "--binary",
        type=_path,
        required=True,
        help="the exact supported Codex CLI executable to hash into the fixed creator profile",
    )
    evolve_creator_prepare.add_argument(
        "--binary-version",
        required=True,
        help="the exact supported Codex CLI version recorded by the native creator session",
    )
    evolve_creator_verify = evolve_creator_commands.add_parser("verify")
    evolve_creator_verify.add_argument("--request", type=_path, required=True)
    evolve_creator_verify.add_argument("--candidate", type=_path, required=True)
    evolve_creator_verify.add_argument(
        "--native-evidence",
        type=_path,
        required=True,
        help="bounded raw native creator-session policy evidence for the prepared request",
    )

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

    managed = subcommands.add_parser("managed", help="run the installed managed lifecycle boundary")
    managed_commands = managed.add_subparsers(dest="managed_command", required=True)
    managed_observe = managed_commands.add_parser("observe")
    _project(managed_observe)
    managed_observe.add_argument("--suite", required=True)
    managed_observe.add_argument("--payload", type=_path, required=True)
    managed_observe.add_argument("--fixture", action="store_true")
    managed_invoke = managed_commands.add_parser(
        "invoke", help="activate through the installed discovery entry and current native child"
    )
    _project(managed_invoke)
    managed_invoke.add_argument("--suite", required=True)
    managed_invoke.add_argument("--skill", required=True)
    managed_invoke.add_argument("--host", choices=["codex", "workbuddy"], required=True)
    managed_invoke.add_argument("--expected-version", required=True)
    managed_invoke.add_argument("--expected-composite", required=True)
    managed_activate_parser = managed_commands.add_parser("activate")
    _project(managed_activate_parser)
    managed_activate_parser.add_argument("--suite", required=True)
    managed_activate_parser.add_argument("--skill", required=True)
    managed_activate_parser.add_argument("--receipt", required=True)
    managed_action = managed_commands.add_parser("action")
    _project(managed_action)
    managed_action.add_argument("--suite", required=True)
    managed_action.add_argument("--skill", required=True)
    managed_action.add_argument("--task-id", required=True)
    managed_action.add_argument("--call-id", required=True)
    managed_action.add_argument("--action", required=True)
    managed_action.add_argument("--request", type=_path, required=True)
    managed_action.add_argument("--host", choices=["codex", "workbuddy"], required=True)
    managed_action.add_argument("--expected-version", required=True)
    managed_action.add_argument("--expected-composite", required=True)
    managed_stage = managed_commands.add_parser("stage")
    _project(managed_stage)
    managed_stage.add_argument("--suite", required=True)
    managed_stage.add_argument("--skill", required=True)
    managed_stage.add_argument("--task-id", required=True)
    managed_stage.add_argument("--call-id", required=True)
    managed_stage.add_argument("--action", required=True)
    managed_stage.add_argument("--artifact-bytes", type=_path, required=True)
    managed_stage.add_argument("--checker-input", type=_path, required=True)
    managed_stage.add_argument("--host", choices=["codex", "workbuddy"], required=True)
    managed_stage.add_argument("--expected-version", required=True)
    managed_stage.add_argument("--expected-composite", required=True)
    managed_publish_parser = managed_commands.add_parser("publish")
    _project(managed_publish_parser)
    managed_publish_parser.add_argument("--suite", required=True)
    managed_publish_parser.add_argument("--skill", required=True)
    managed_publish_parser.add_argument("--task-id", required=True)
    managed_publish_parser.add_argument("--call-id", required=True)
    managed_publish_parser.add_argument("--artifact", type=_path, required=True)
    managed_publish_parser.add_argument("--target", required=True)
    managed_publish_parser.add_argument("--host", choices=["codex", "workbuddy"], required=True)
    managed_publish_parser.add_argument("--expected-version", required=True)
    managed_publish_parser.add_argument("--expected-composite", required=True)
    managed_health_parser = managed_commands.add_parser("health")
    _project(managed_health_parser)
    managed_health_parser.add_argument("--suite", required=True)
    managed_health_parser.add_argument("--task-id", required=True)
    managed_recover = managed_commands.add_parser("recover")
    _project(managed_recover)
    managed_recover.add_argument("--suite", required=True)
    managed_hook = managed_commands.add_parser(
        "hook", help="receive one installer-owned native callback on standard input"
    )
    _project(managed_hook)
    managed_hook.add_argument("--suite", required=True)
    managed_hook.add_argument("--host", choices=["codex", "workbuddy"], required=True)

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


def _managed_declaration(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SelcError(
            "INVALID_MANAGED_DECLARATION",
            "managed declaration must be a readable JSON object",
            "provide a protocol-1 managed declaration file",
        ) from error
    if not isinstance(value, dict):
        raise SelcError(
            "INVALID_MANAGED_DECLARATION",
            "managed declaration must be a JSON object",
            "provide a protocol-1 managed declaration file",
        )
    return value


def _verify_delivery_checker(
    arguments: argparse.Namespace,
) -> tuple[bool, str | None, dict[str, str] | None]:
    """Run the one v0.1 trusted checker without executing caller-provided code."""

    if arguments.checker != "text-report-v1":
        return False, None, None
    if arguments.rule_version != "text-report-v1":
        return False, None, None
    if arguments.input is None or arguments.output is None:
        return False, None, None
    database = open_database(arguments.project_root)
    try:
        task = database.execute(
            "SELECT suite_id FROM tasks WHERE task_id = ?", (arguments.task_id,)
        ).fetchone()
    finally:
        database.close()
    if task is None:
        return False, None, None
    try:
        installation = current_installation(arguments.project_root, task["suite_id"])
        resources_root = (
            install_root(arguments.project_root)
            / task["suite_id"]
            / "contents"
            / installation["digest"]
            / "resources"
        )
        input_bytes = arguments.input.read_bytes()
        output_bytes = arguments.output.read_bytes()
        resource_bytes = (resources_root / "text-report.json").read_bytes()
        expected = render_text_report_bytes(input_bytes.decode("utf-8"), resource_bytes)
        actual = output_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError, SelcError):
        return False, None, None
    actual_artifact_hash = hashlib.sha256(output_bytes).hexdigest()
    identity = {
        "checker_input_hash": hashlib.sha256(input_bytes).hexdigest(),
        "checker_artifact_hash": actual_artifact_hash,
        "checker_resource_hash": hashlib.sha256(resource_bytes).hexdigest(),
        "checker_composite_hash": str(installation["digest"]),
    }
    output_hash_matches = actual_artifact_hash == arguments.artifact_hash
    return True, "pass" if output_hash_matches and actual == expected else "fail", identity


def _dispatch(arguments: argparse.Namespace, default_suite_root: Path | None) -> dict[str, Any]:
    command = arguments.command
    if command == "create":
        return {
            "suite": create_suite(
                arguments.suite_id,
                arguments.version,
                arguments.output,
                arguments.skill,
                managed=_managed_declaration(arguments.managed_declaration),
            )
        }
    if command == "managed":
        if arguments.managed_command == "hook":
            payload = read_hook_payload(sys.stdin)
            return {
                "managed": receive_native_hook(
                    project_root=arguments.project_root,
                    suite_id=arguments.suite,
                    host=arguments.host,
                    payload=payload,
                    environment=current_environment(),
                )
            }
        if arguments.managed_command == "observe":
            try:
                payload = json.loads(arguments.payload.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise SelcError(
                    "NATIVE_COLLECTOR_INVALID",
                    "managed observer payload is not a readable JSON object",
                    "supply the native collector payload file",
                ) from error
            if not isinstance(payload, dict):
                raise SelcError(
                    "NATIVE_COLLECTOR_INVALID",
                    "managed observer payload must be an object",
                    "supply the native collector payload file",
                )
            return {
                "managed": observe_request_boundary(
                    project_root=arguments.project_root,
                    suite_id=arguments.suite,
                    payload=payload,
                    fixture=arguments.fixture,
                )
            }
        if arguments.managed_command == "invoke":
            return {
                "managed": managed_activate_from_environment(
                    project_root=arguments.project_root,
                    suite_id=arguments.suite,
                    skill=arguments.skill,
                    host=arguments.host,
                    expected_version=arguments.expected_version,
                    expected_composite=arguments.expected_composite,
                    environment=current_environment(),
                )
            }
        if arguments.managed_command == "activate":
            return {
                "managed": managed_activate(
                    project_root=arguments.project_root,
                    suite_id=arguments.suite,
                    skill=arguments.skill,
                    receipt=arguments.receipt,
                )
            }
        if arguments.managed_command == "action":
            try:
                request = json.loads(arguments.request.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise SelcError(
                    "MANAGED_INPUT_INVALID",
                    "managed action request is not a readable JSON object",
                    "supply the declared managed input object",
                ) from error
            if not isinstance(request, dict):
                raise SelcError(
                    "MANAGED_INPUT_INVALID",
                    "managed action request must be an object",
                    "supply the declared managed input object",
                )
            return {
                "managed": run_managed_action(
                    project_root=arguments.project_root,
                    suite_id=arguments.suite,
                    skill=arguments.skill,
                    task_id=arguments.task_id,
                    call_id=arguments.call_id,
                    action_id=arguments.action,
                    receipt=None,
                    request=request,
                    host=arguments.host,
                    expected_version=arguments.expected_version,
                    expected_composite=arguments.expected_composite,
                    environment=current_environment(),
                    operation_arguments={
                        "--project-root": str(arguments.project_root),
                        "--suite": arguments.suite,
                        "--skill": arguments.skill,
                        "--task-id": arguments.task_id,
                        "--call-id": arguments.call_id,
                        "--action": arguments.action,
                        "--request": str(arguments.request),
                        "--host": arguments.host,
                        "--expected-version": arguments.expected_version,
                        "--expected-composite": arguments.expected_composite,
                    },
                )
            }
        if arguments.managed_command == "stage":
            try:
                artifact_bytes = arguments.artifact_bytes.read_bytes()
                checker_input_bytes = arguments.checker_input.read_bytes()
            except OSError as error:
                raise SelcError(
                    "MANAGED_INPUT_INVALID",
                    "managed instruction staging inputs are unavailable",
                    "supply readable artifact and checker-input files",
                ) from error
            return {
                "managed": stage_instruction_artifact(
                    project_root=arguments.project_root,
                    suite_id=arguments.suite,
                    task_id=arguments.task_id,
                    call_id=arguments.call_id,
                    action_id=arguments.action,
                    artifact_bytes=artifact_bytes,
                    checker_input_bytes=checker_input_bytes,
                    host=arguments.host,
                    expected_version=arguments.expected_version,
                    expected_composite=arguments.expected_composite,
                    environment=current_environment(),
                    operation_arguments={
                        "--project-root": str(arguments.project_root),
                        "--suite": arguments.suite,
                        "--skill": arguments.skill,
                        "--task-id": arguments.task_id,
                        "--call-id": arguments.call_id,
                        "--action": arguments.action,
                        "--artifact-bytes": str(arguments.artifact_bytes),
                        "--checker-input": str(arguments.checker_input),
                        "--host": arguments.host,
                        "--expected-version": arguments.expected_version,
                        "--expected-composite": arguments.expected_composite,
                    },
                )
            }
        if arguments.managed_command == "publish":
            return {
                "managed": managed_publish(
                    project_root=arguments.project_root,
                    suite_id=arguments.suite,
                    task_id=arguments.task_id,
                    call_id=arguments.call_id,
                    receipt=None,
                    artifact_path=arguments.artifact,
                    target=arguments.target,
                    skill=arguments.skill,
                    host=arguments.host,
                    expected_version=arguments.expected_version,
                    expected_composite=arguments.expected_composite,
                    environment=current_environment(),
                    operation_arguments={
                        "--project-root": str(arguments.project_root),
                        "--suite": arguments.suite,
                        "--skill": arguments.skill,
                        "--task-id": arguments.task_id,
                        "--call-id": arguments.call_id,
                        "--artifact": str(arguments.artifact),
                        "--target": arguments.target,
                        "--host": arguments.host,
                        "--expected-version": arguments.expected_version,
                        "--expected-composite": arguments.expected_composite,
                    },
                )
            }
        if arguments.managed_command == "health":
            return {
                "managed": managed_health(
                    arguments.project_root, arguments.suite, arguments.task_id
                )
            }
        return {"managed": recover_managed_runtime(arguments.project_root, arguments.suite)}
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
                mode=arguments.mode,
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
                mode=arguments.mode,
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
        checker_verified, checker_result, checker_identity = _verify_delivery_checker(arguments)
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
                    checker_identity=checker_identity,
                )
            }
        finally:
            database.close()
    if command == "checker":
        if arguments.checker_command == "register-script":
            return {
                "checker": register_script_checker(
                    project_root=arguments.project_root,
                    name=arguments.name,
                    version=arguments.version,
                    script_path=arguments.script,
                    digest=arguments.digest,
                    supported_rules=arguments.rule,
                    timeout_seconds=arguments.timeout_seconds,
                )
            }
        return {"checkers": inspect_script_checkers(arguments.project_root)}
    if command == "check":
        execution = run_installed_check(
            project_root=arguments.project_root,
            suite_id=arguments.suite,
            check_id=arguments.check_id,
            input_path=arguments.input,
            artifact_path=arguments.artifact,
            subject=SubjectIdentity("diagnostic", "diagnostic", "diagnostic"),
            run_id=f"diagnostic:{arguments.suite}:{arguments.check_id}",
        )
        return {"check": check_result_payload(execution)}
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
        database = open_database(arguments.project_root)
        try:
            # ``delete_records`` owns registry→selected-suite locking for both
            # public API and CLI callers.  Keeping this path thin prevents a
            # CLI-only lock flag from bypassing its durable execution check.
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
            prepared_business_snapshot: dict[str, Any] | None = None
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
                prepared_business_snapshot = business_selection_snapshot(existing)
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
            return {
                "installation": install_suite(
                    source=business_source,
                    project_root=arguments.project_root,
                    harness=arguments.harness,
                    ref=business_ref,
                    subdir=business_subdir,
                    framework_source=arguments.framework_source,
                    framework_ref=arguments.framework_ref,
                    framework_subdir=arguments.framework_subdir,
                    prepared_business_snapshot=prepared_business_snapshot,
                )
            }
        return recover_installation(arguments.project_root, arguments.suite_id)
    if command == "evolve":
        if arguments.evolve_command == "prepare":
            return prepare_candidate(arguments.suite, arguments.output)
        if arguments.evolve_command == "evaluate":
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
        if arguments.evolve_command == "plan":
            return prepare_real_evaluation_plan(arguments.suite, arguments.cases, arguments.output)
        if arguments.evolve_command == "policy-template":
            return write_codex_policy_template(
                arguments.host_version, arguments.python_read_root, arguments.output
            )
        if arguments.evolve_command == "bind":
            return bind_real_evaluation_candidate(
                arguments.plan, arguments.candidate, arguments.output, arguments.variant
            )
        if arguments.evolve_command == "attempt":
            if arguments.attempt_command == "prepare":
                return prepare_evaluation_attempt(
                    arguments.plan,
                    arguments.binding,
                    arguments.case,
                    arguments.matrix,
                    arguments.attempt_number,
                    arguments.output,
                )
            if arguments.attempt_command == "launch":
                return launch_codex_cli_attempt(arguments.attempt_path)
            if arguments.attempt_command == "desktop-request":
                return desktop_request(arguments.attempt_path)
            if arguments.attempt_command == "native-evidence-template":
                return native_evidence_template(arguments.attempt_path)
            return collect_evaluation_attempt(arguments.attempt_path)
        if arguments.evolve_command == "finalize":
            return finalize_real_evaluation(
                arguments.plan,
                arguments.binding,
                arguments.attempt_path,
                arguments.output,
            )
        if arguments.evolve_command == "creator":
            if arguments.creator_command == "prepare":
                return prepare_creator_request(
                    arguments.plan,
                    arguments.candidate,
                    arguments.output,
                    executor_binary=arguments.binary,
                    executor_version=arguments.binary_version,
                    brief_path=arguments.brief,
                    skill_creator_root=arguments.skill_creator_root,
                    creator_python_root=arguments.creator_python_root,
                )
            return verify_creator_result(
                arguments.request, arguments.candidate, arguments.native_evidence
            )
        return public_evaluation_summary(arguments.report)
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
        if arguments.command == "managed" and arguments.managed_command == "hook":
            if arguments.host == "codex":
                # Codex Stop hooks require JSON stdout.  An empty object keeps
                # the callback observational: it grants no permission and
                # carries no command rewrite or host control field.
                print("{}")
            return 0
    except SelcError as error:
        error_code = error.code
        error_message = error.message
        recovery = error.recovery
        if error.code == "LOCKED" and getattr(arguments, "command", None) == "event":
            error_code = "DATABASE_BUSY"
            error_message = "the managed suite is executing or being deleted"
            recovery = "retry after the managed operation completes"
        output = {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "error": {
                "code": error_code,
                "message": error_message,
                "recovery": recovery,
            },
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
