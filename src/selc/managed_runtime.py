"""Managed activation, action, and publication coordination.

This module is deliberately a narrow boundary between a native host collector
and SELC's durable records.  It accepts only the collector's allowlisted
metadata, stores hashes instead of request or artifact bodies, and refuses to
start a business operation when the active installation, receipt, or root
window cannot be verified.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os

try:
    import pwd
except ImportError:  # pragma: no cover - Windows has no POSIX account database.
    pwd = None
import re
import secrets
import shlex
import signal
import stat
import subprocess
import sys
import time
import uuid
from contextlib import ExitStack, contextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable
from urllib.parse import unquote_to_bytes, urlsplit

from . import records
from .checker_integration import run_installed_check
from .errors import SelcError
from .execution_lease import (
    ProcessIdentity,
    assert_no_live_execution,
    inspect_process,
    process_group_is_live,
)
from .install import (
    _assert_installation_state,
    current_installation,
    install_root,
    registry_lock_path,
)
from .manifest import (
    load_manifest,
    managed_configuration,
    managed_host_versions,
    normalize_declared_skills,
    portable_tree_hash,
)
from .util import canonical_json, exclusive_lock, file_hash

RECEIPT_TTL_SECONDS = 300
MAX_INPUT_BYTES = 1_048_576
MAX_TRANSCRIPT_PREFIX_BYTES = 1_048_576
MAX_MANAGED_COMMAND_BYTES = 16_384
MANAGED_OWNER = "managed-v2"
SUPPORTED_HOSTS = {"codex", "workbuddy"}
POST_PHASE = "PostToolUse"
PRE_PHASE = "PreToolUse"
STOP_PHASE = "Stop"
USER_PROMPT_PHASE = "UserPromptSubmit"
PERMISSION_PHASE = "PermissionRequest"
PERMISSION_DENIED_PHASE = "PermissionDenied"
CURRENT_REQUEST_ANCHOR_RULE_VERSION = "current-request-v2"
CODEX_ALPHA11_ARG0_ANCHOR_RULE_VERSION = "current-request-v3-codex-alpha11-arg0"
WORKBUDDY_SKILL_PRE_ANCHOR_RULE_VERSION = "current-request-v3-workbuddy-skill-pre"
WORKBUDDY_AUTOMATIC_SKILL_PRE_ANCHOR_RULE_VERSION = "current-request-v4-workbuddy-auto-skill-pre"
WORKBUDDY_SUPPORTED_CLIENT_VERSIONS = frozenset({"5.8.3", "5.9.2"})
CODEX_ALPHA11_ARG0_VERSION = "0.163.0-alpha.11"
BUSINESS_DTO_RULE_VERSION = "business-dto-v1"
_SAME_PROJECT_CD_PREFIX = re.compile(r"""^cd (?P<directory>/[^\s"'`$;&|<>()\\*?\[\]{}]+) && """)


def _managed_command_wrapper_prefix(
    command: str,
    prefix: list[str],
    project_root: Path | None,
) -> list[str] | None:
    """Allow one literal same-project ``cd`` before the fixed wrapper only.

    The raw command remains the evidence value.  This helper only removes a
    finite shell prelude from the parsed operation, after proving the literal
    directory is the caller-supplied project root.
    """

    if len(prefix) == 2:
        return prefix
    if len(prefix) != 5 or project_root is None:
        return None

    match = _SAME_PROJECT_CD_PREFIX.match(command)
    if match is None:
        return None
    directory = match.group("directory")
    if prefix[:3] != ["cd", directory, "&&"]:
        return None
    try:
        candidate_root = Path(directory)
        expected_root = project_root.resolve(strict=True)
        if (
            not candidate_root.is_absolute()
            or ".." in candidate_root.parts
            or candidate_root.resolve(strict=True) != expected_root
        ):
            return None
    except (OSError, ValueError):
        return None
    return prefix[3:]


def parse_installed_managed_command(
    command: str,
    suite_id: str,
    *,
    project_root: Path | None = None,
) -> dict[str, str] | None:
    """Parse only an installed wrapper invocation, never an arbitrary shell.

    Hook input is shell text, so accepting a JSON control DTO made the real
    installed path invisible.  This parser does not execute, expand, or
    reinterpret shell syntax.  It accepts the documented Python wrapper and a
    finite set of named arguments, preserving their canonical value binding for
    the receipt ledger.
    """

    try:
        legacy = json.loads(command)
    except json.JSONDecodeError:
        legacy = None
    if isinstance(legacy, dict) and set(legacy) == {"action", "skill", "suite"}:
        if legacy.get("suite") != suite_id:
            return None
        action = legacy.get("action")
        skill = legacy.get("skill")
        if isinstance(action, str) and isinstance(skill, str) and action and skill:
            return {"action": action, "skill": skill, "suite": suite_id, "format": "fixture"}
        return None

    try:
        argv = shlex.split(command, posix=True)
    except ValueError:
        return None
    if len(argv) < 5 or "managed" not in argv:
        return None
    managed_index = argv.index("managed")
    prefix = _managed_command_wrapper_prefix(command, argv[:managed_index], project_root)
    if prefix is None:
        return None
    if len(prefix) != 2 or not prefix[1].endswith("selc-suite.py"):
        return None
    if project_root is not None:
        try:
            wrapper = Path(prefix[1]).resolve(strict=True)
            expected_wrapper = (
                install_root(project_root) / suite_id / "current" / "bin" / "selc-suite.py"
            ).resolve(strict=True)
            interpreter = Path(prefix[0]).resolve(strict=True)
            expected_interpreter = Path(sys.executable).resolve(strict=True)
        except OSError:
            return None
        if wrapper != expected_wrapper or interpreter != expected_interpreter:
            return None
    operation_index = managed_index + 1
    if operation_index >= len(argv):
        return None
    operation = argv[operation_index]
    if operation not in {"activate", "action", "publish", "stage", "invoke"}:
        return None
    remainder = argv[operation_index + 1 :]
    if len(remainder) % 2:
        return None
    allowed = {
        "--project-root",
        "--suite",
        "--skill",
        "--action",
        "--request",
        "--artifact-bytes",
        "--checker-input",
        "--artifact",
        "--target",
        "--task-id",
        "--call-id",
        "--host",
        "--expected-version",
        "--expected-composite",
    }
    arguments: dict[str, str] = {}
    for index in range(0, len(remainder), 2):
        name, value = remainder[index : index + 2]
        if name not in allowed or name in arguments or not value:
            return None
        arguments[name] = value
    if arguments.get("--suite") != suite_id or "--project-root" not in arguments:
        return None
    if (
        arguments.get("--host") not in SUPPORTED_HOSTS
        or not arguments.get("--expected-version")
        or not arguments.get("--expected-composite")
    ):
        return None
    if project_root is not None:
        try:
            command_root = Path(arguments["--project-root"]).resolve(strict=True)
        except OSError:
            return None
        if command_root != project_root.resolve(strict=True):
            return None
        arguments["--project-root"] = str(command_root)
    for path_option in ("--request", "--artifact", "--artifact-bytes", "--checker-input"):
        if path_option in arguments:
            arguments[path_option] = str(Path(arguments[path_option]).expanduser().resolve())
    skill = arguments.get("--skill", "")
    if operation in {"activate", "action", "invoke"} and not skill:
        return None
    if operation == "action" and "--action" not in arguments:
        return None
    if operation == "action" and not {"--task-id", "--call-id", "--request"}.issubset(arguments):
        return None
    if operation == "stage" and not {
        "--skill",
        "--task-id",
        "--call-id",
        "--action",
        "--artifact-bytes",
        "--checker-input",
    }.issubset(arguments):
        return None
    if operation == "publish" and not {
        "--skill",
        "--task-id",
        "--call-id",
        "--artifact",
        "--target",
    }.issubset(arguments):
        return None
    parsed_action = arguments.get("--action", operation)
    if operation == "invoke":
        parsed_action = "activate"
    return {
        "action": parsed_action,
        "skill": skill,
        "suite": suite_id,
        "format": "shell",
        "operation": operation,
        "arguments": canonical_json({key: arguments[key] for key in sorted(arguments)}),
    }


def native_context_from_environment(host: str, environment: dict[str, str]) -> dict[str, str]:
    """Resolve the current host's documented native identity namespace.

    The collector calls this before it assembles a raw callback payload.  It
    intentionally rejects alias disagreement instead of selecting a latest
    session or transcript record.  The WorkBuddy aliases are accepted as
    transport candidates after the direct 5.8.3 environment probe; that does
    not claim its serializer, root proof, or publication window is supported.
    """

    if host == "codex":
        session_id = _environment_value(environment, "CODEX_SESSION_ID")
        thread_id = _environment_value(environment, "CODEX_THREAD_ID")
        if session_id != thread_id:
            raise SelcError(
                "NATIVE_CONTEXT_CONFLICT",
                "Codex child session and thread identities disagree",
                "pause before business execution and repair the Codex collector",
            )
        return {
            "session_id": session_id,
            "thread_id": thread_id,
            "tool_use_id": _environment_value(environment, "CODEX_TOOL_CALL_ID"),
            "client_version": _environment_value(environment, "CODEX_VERSION"),
        }
    if host == "workbuddy":
        return {
            "session_id": _environment_alias(
                environment,
                "CODEBUDDY_SESSION_ID",
                "CODEBUDDY_SANDBOX_BROKER_SESSION_ID",
            ),
            "request_id": _environment_value(environment, "CODEBUDDY_CONVERSATION_REQUEST_ID"),
            "message_id": _environment_alias(
                environment,
                "CODEBUDDY_CONVERSATION_MESSAGE_ID",
                "CODEBUDDY_MESSAGE_ID",
            ),
            "root_request_id": environment.get("CODEBUDDY_ROOT_REQUEST_ID", ""),
            "tool_use_id": _environment_alias(
                environment,
                "CODEBUDDY_TOOL_CALL_ID",
                "CODEBUDDY_SANDBOX_BROKER_TOOL_CALL_ID",
            ),
        }
    raise SelcError(
        "NATIVE_HOST_UNSUPPORTED",
        "native host is unsupported",
        "use the installed Codex or WorkBuddy collector",
    )


def observe_request_boundary(
    *,
    project_root: Path,
    suite_id: str,
    payload: dict[str, object],
    trusted_producer: bool = False,
    fixture: bool = False,
) -> dict[str, object]:
    """Durably record one raw native callback before issuing an opaque receipt.

    ``payload`` is produced by a host collector.  The function never trusts a
    supplied prefix hash, actor, completion flag, or host-support flag.  It
    recomputes bounded transcript-prefix metadata and accepts only a canonical
    managed command.  Fixture-shaped payloads can exercise the local flow but
    remain marked unverified and therefore cannot establish a production first
    pass or host-support claim.
    """

    if trusted_producer == fixture:
        raise SelcError(
            "NATIVE_PRODUCER_UNTRUSTED",
            "managed native observations require the installed collector or an explicit fixture",
            "invoke the installed collector instead of supplying a native DTO",
        )
    normalized = _normalize_native_payload(
        project_root,
        suite_id,
        payload,
        trusted_producer=trusted_producer,
        fixture=fixture,
    )
    with _locked_database(project_root, suite_id) as (database, installation):
        _assert_active_installation(project_root, suite_id, installation)
        # Raw native identity is a durable fact before any derived context,
        # receipt, or assessment projection.  A failed projection can therefore
        # be retried after reopen without re-running business work.
        with records.projection_transaction(database):
            event = _store_native_event(database, normalized)
            if event["status"] == "conflict":
                return {"status": "conflict"}
            if event["status"] == "suppressed":
                return {"status": "suppressed"}
        if event["status"] == "replayed" and event["projection_status"] == "applied":
            return {"status": "replayed"}
        projection_event = (
            normalized
            if event["status"] == "recorded"
            else _projection_event(database, event, normalized)
        )
        if projection_event is None:
            return {"status": "recorded_unusable"}
        with records.projection_transaction(database):
            result = _project_native_event(
                database,
                installation,
                projection_event,
                event_id=event["event_id"],
            )
            database.execute(
                "UPDATE managed_native_events SET projection_status = 'applied' WHERE event_id = ?",
                (event["event_id"],),
            )
            return result


def activate(*, project_root: Path, suite_id: str, skill: str, receipt: str) -> dict[str, object]:
    """Start or continue the managed task selected by an activation receipt."""

    with _locked_database(project_root, suite_id) as (database, installation):
        _assert_active_installation(project_root, suite_id, installation)
        manifest = load_manifest(_current_root(project_root, suite_id))
        host_versions = managed_host_versions(manifest)
        skills = {entry["name"] for entry in normalize_declared_skills(manifest["skills"])}
        if skill not in skills:
            raise SelcError(
                "MANAGED_SKILL_UNDECLARED",
                "activation skill is not declared by the active suite",
                "select an installed declared skill",
            )
        with records.projection_transaction(database):
            receipt_row = _consume_receipt(
                database,
                receipt=receipt,
                installation=installation,
                suite_id=suite_id,
                skill=skill,
                expected_action="activate",
                host_versions=host_versions,
            )
            return _activate_consumed_receipt(
                database,
                project_root=project_root,
                suite_id=suite_id,
                skill=skill,
                installation=installation,
                receipt_row=receipt_row,
            )


def activate_from_environment(
    *,
    project_root: Path,
    suite_id: str,
    skill: str,
    host: str,
    expected_version: str,
    expected_composite: str,
    environment: dict[str, str],
) -> dict[str, object]:
    """Activate from the child process's exact native environment identity.

    The hook token is deliberately never printed or passed through a skill.  A
    child must instead match one ready receipt on every native identifier.
    """

    context = native_context_from_environment(host, environment)
    with _locked_database(project_root, suite_id) as (database, installation):
        _assert_expected_installation(installation, expected_version, expected_composite)
        _assert_active_installation(project_root, suite_id, installation)
        manifest = load_manifest(_current_root(project_root, suite_id))
        host_versions = managed_host_versions(manifest)
        with records.projection_transaction(database):
            rows = _ready_receipts_for_environment(
                database,
                suite_id=suite_id,
                host=host,
                context=context,
                action="activate",
                skill=skill,
                expected_operation=_operation_digest(
                    operation="invoke",
                    action="activate",
                    suite_id=suite_id,
                    skill=skill,
                    arguments={
                        "--project-root": str(project_root.resolve()),
                        "--suite": suite_id,
                        "--skill": skill,
                        "--host": host,
                        "--expected-version": expected_version,
                        "--expected-composite": expected_composite,
                    },
                ),
            )
            receipt_row = _select_one_ready_receipt(rows)
            receipt_row = _consume_receipt_row(
                database,
                row=receipt_row,
                installation=installation,
                suite_id=suite_id,
                skill=skill,
                expected_action="activate",
                host_versions=host_versions,
            )
            return _activate_consumed_receipt(
                database,
                project_root=project_root,
                suite_id=suite_id,
                skill=skill,
                installation=installation,
                receipt_row=receipt_row,
            )


def _activate_consumed_receipt(
    database: Any,
    *,
    project_root: Path,
    suite_id: str,
    skill: str,
    installation: dict[str, Any],
    receipt_row: Any,
) -> dict[str, object]:
    context = _context_for_receipt(database, receipt_row)
    trigger_kind = _confirm_context_selection(database, context, skill)
    task_id = context["task_id"]
    fixture_receipt = _receipt_is_fixture(database, receipt_row)
    if task_id is None:
        if not bool(context["root_valid"]) and not fixture_receipt:
            raise SelcError(
                "ROOT_PROOF_UNAVAILABLE",
                "the native request has no verified initial prefix",
                "repair native collection before activating this skill",
            )
        task_id = f"managed-task-{secrets.token_hex(12)}"
        _insert_managed_task(
            database,
            task_id=task_id,
            suite_id=suite_id,
            version=str(installation["version"]),
            host=str(receipt_row["host"]),
            is_test=fixture_receipt,
        )
        database.execute(
            "UPDATE managed_contexts SET task_id = ? WHERE context_key = ?",
            (task_id, context["context_key"]),
        )
    else:
        database.execute(
            "UPDATE tasks SET status = 'active' WHERE task_id = ? AND assessment_owner = ?",
            (task_id, MANAGED_OWNER),
        )
    call_id = f"managed-call-{secrets.token_hex(12)}"
    _insert_managed_call(
        database,
        call_id=call_id,
        task_id=str(task_id),
        skill=skill,
        host=str(receipt_row["host"]),
        installation=installation,
        trigger_kind=trigger_kind,
    )
    database.execute(
        "UPDATE managed_receipts SET task_id = ?, call_id = ? WHERE receipt_digest = ?",
        (task_id, call_id, receipt_row["receipt_digest"]),
    )
    database.execute(
        "UPDATE managed_native_events SET task_id = ?, call_id = ? WHERE event_id = ?",
        (task_id, call_id, receipt_row["native_event_id"]),
    )
    reduce_managed_assessment(database, str(task_id))
    manifest = load_manifest(_current_root(project_root, suite_id))
    paths = {item["name"]: item["path"] for item in normalize_declared_skills(manifest["skills"])}
    skill_body_path = _current_root(project_root, suite_id) / paths[skill] / "SKILL.md"
    try:
        skill_body = skill_body_path.read_text(encoding="utf-8")
    except OSError as error:
        raise SelcError(
            "MANAGED_SKILL_UNDECLARED",
            "the declared immutable skill body is unavailable",
            "repair the active installed suite before activation",
        ) from error
    return {
        "task_id": str(task_id),
        "call_id": call_id,
        "skill_body": skill_body,
        "skill_body_path": str(skill_body_path),
        "native_support": "unverified" if fixture_receipt else "pending",
    }


def _assert_expected_installation(
    installation: dict[str, Any], expected_version: str, expected_composite: str
) -> None:
    """Reject a cached generated entry before it can consume a native receipt."""

    if (
        installation.get("version") != expected_version
        or installation.get("digest") != expected_composite
    ):
        raise SelcError(
            "INSTALLATION_IDENTITY_DRIFT",
            "the installed entry expects a different suite version or composite",
            "select the current installer-owned entry and retry",
        )


def _operation_digest(
    *,
    operation: str,
    action: str,
    suite_id: str,
    skill: str,
    arguments: dict[str, str],
) -> str:
    """Hash the finite shell operation schema persisted by the native receipt."""

    parsed = {
        "action": action,
        "skill": skill,
        "suite": suite_id,
        "format": "shell",
        "operation": operation,
        "arguments": canonical_json({key: arguments[key] for key in sorted(arguments)}),
    }
    return _digest(parsed)


def _ready_receipts_for_environment(
    database: Any,
    *,
    suite_id: str,
    host: str,
    context: dict[str, str],
    action: str | set[str],
    skill: str | None,
    expected_operation: str,
) -> list[Any]:
    """Locate a single ready receipt using only an exact child identity.

    Codex has a distinct turn and tool namespace.  Its child reports a native
    session and tool call, so the selected pre-event supplies the turn/request
    identity instead of treating the tool call as a request ID.
    """

    clauses = [
        "suite_id = ?",
        "host = ?",
        "session_id = ?",
        "tool_use_id = ?",
        "status = 'ready'",
        "operation_digest = ?",
    ]
    values: list[object] = [
        suite_id,
        host,
        context["session_id"],
        context["tool_use_id"],
        expected_operation,
    ]
    if host == "codex":
        clauses.append("host_version = ?")
        values.append(context["client_version"])
    if host == "workbuddy":
        clauses.append("request_id = ?")
        values.append(context["request_id"])
    if isinstance(action, set):
        clauses.append("action IN (" + ", ".join("?" for _ in action) + ")")
        values.extend(sorted(action))
    else:
        clauses.append("action = ?")
        values.append(action)
    if skill is not None:
        clauses.append("skill = ?")
        values.append(skill)
    return database.execute(
        "SELECT * FROM managed_receipts WHERE " + " AND ".join(clauses), values
    ).fetchall()


def _select_one_ready_receipt(rows: list[Any]) -> Any:
    if len(rows) != 1:
        raise SelcError(
            "RECEIPT_UNAVAILABLE",
            "current native child identity does not select one managed receipt",
            "invoke the current installer-owned entry from its observed native action",
        )
    return rows[0]


def run_action(
    *,
    project_root: Path,
    suite_id: str,
    skill: str,
    task_id: str,
    call_id: str,
    action_id: str,
    receipt: str | None,
    request: dict[str, object],
    host: str | None = None,
    expected_version: str | None = None,
    expected_composite: str | None = None,
    environment: dict[str, str] | None = None,
    operation_arguments: dict[str, str] | None = None,
) -> dict[str, object]:
    """Run one action while it exclusively owns its durable execution slot."""

    with _managed_execution_lock(project_root, suite_id):
        return _run_action_locked(
            project_root=project_root,
            suite_id=suite_id,
            skill=skill,
            task_id=task_id,
            call_id=call_id,
            action_id=action_id,
            receipt=receipt,
            request=request,
            host=host,
            expected_version=expected_version,
            expected_composite=expected_composite,
            environment=environment,
            operation_arguments=operation_arguments,
            execution_lock_held=True,
        )


def _run_action_locked(
    *,
    project_root: Path,
    suite_id: str,
    skill: str,
    task_id: str,
    call_id: str,
    action_id: str,
    receipt: str | None,
    request: dict[str, object],
    host: str | None = None,
    expected_version: str | None = None,
    expected_composite: str | None = None,
    environment: dict[str, str] | None = None,
    operation_arguments: dict[str, str] | None = None,
    execution_lock_held: bool = False,
) -> dict[str, object]:
    """Run a manifest-declared installed script with bounded staged inputs."""

    with _locked_database(project_root, suite_id, locks_held=execution_lock_held) as (
        database,
        installation,
    ):
        assert_no_live_execution(database, suite_id)
        if receipt is None:
            if (
                host is None
                or expected_version is None
                or expected_composite is None
                or environment is None
                or operation_arguments is None
            ):
                raise SelcError(
                    "RECEIPT_UNAVAILABLE", "managed action needs a native child identity"
                )
            _assert_expected_installation(installation, expected_version, expected_composite)
        _assert_active_installation(project_root, suite_id, installation)
        suite_root = _current_root(project_root, suite_id)
        manifest = load_manifest(suite_root)
        actions = managed_configuration(manifest, suite_root)
        host_versions = managed_host_versions(manifest)
        action = actions.get(action_id)
        if action is None or action["skill"] != skill:
            raise SelcError(
                "MANAGED_ACTION_UNDECLARED",
                "action is not declared for the selected skill",
                "use one fixed action from the active suite manifest",
            )
        normalized_request = _validate_action_request(action, request)
        request_digest = _digest({"skill": skill, "input": normalized_request})
        frozen_inputs = _freeze_declared_action_inputs(action, normalized_request)
        with records.projection_transaction(database):
            _assert_managed_call(database, task_id, call_id, suite_id, skill, installation)
            _assert_no_pending_action(database, task_id)
            if receipt is None:
                context = native_context_from_environment(host, environment)
                rows = _ready_receipts_for_environment(
                    database,
                    suite_id=suite_id,
                    host=host,
                    context=context,
                    action={"action", action_id},
                    skill=skill,
                    expected_operation=_operation_digest(
                        operation="action",
                        action=action_id,
                        suite_id=suite_id,
                        skill=skill,
                        arguments=operation_arguments,
                    ),
                )
                receipt_row = _select_one_ready_receipt(rows)
                receipt_row = _consume_receipt_row(
                    database,
                    row=receipt_row,
                    installation=installation,
                    suite_id=suite_id,
                    skill=skill,
                    expected_action={"action", action_id},
                    host_versions=host_versions,
                )
            else:
                receipt_row = _consume_receipt(
                    database,
                    receipt=receipt,
                    installation=installation,
                    suite_id=suite_id,
                    skill=skill,
                    expected_action={"action", action_id},
                    host_versions=host_versions,
                )
            context = _assert_receipt_subject(database, receipt_row, task_id, call_id)
            _assert_correction_binding_current(database, context, installation)
            _assert_context_selected_skill(context, skill)
            _assert_context_action_business_dto(context, normalized_request)
            _assert_correction_request_input(
                database,
                context,
                action,
                normalized_request,
                frozen_inputs,
            )
            if (
                not _context_uses_automatic_selection(context)
                and context["request_digest"]
                and context["request_digest"] != request_digest
            ):
                raise SelcError(
                    "RECEIPT_ARGUMENT_MISMATCH",
                    "managed action inputs differ from the native request bound to this receipt",
                    "invoke the exact declared operation and inputs observed by the native collector",
                )
            action_run_id = f"managed-action-{secrets.token_hex(12)}"
            database.execute(
                """INSERT INTO managed_actions(
                action_run_id, suite_id, task_id, call_id, action_id, receipt_digest,
                request_digest, status, detail, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'reserved', ?, ?)""",
                (
                    action_run_id,
                    suite_id,
                    task_id,
                    call_id,
                    action_id,
                    receipt_row["receipt_digest"],
                    request_digest,
                    _encoded({"kind": action["kind"]}),
                    records.utc_now(),
                ),
            )

        staging = _action_staging_root(project_root, suite_id, action_run_id)
        try:
            artifact_path, artifact_digest = _execute_declared_action(
                suite_root=suite_root,
                action=action,
                request=normalized_request,
                staging=staging,
                frozen_inputs=frozen_inputs,
                on_execution_started=lambda identity, heartbeat_path: _record_execution_started(
                    database, action_run_id, suite_id, identity, heartbeat_path
                ),
                on_execution_finished=lambda: _record_execution_finished(database, action_run_id),
            )
        except Exception as error:
            with records.projection_transaction(database):
                database.execute(
                    "UPDATE managed_actions SET status = 'failed', detail = ?, finished_at = ? "
                    "WHERE action_run_id = ?",
                    (_encoded({"error": type(error).__name__}), records.utc_now(), action_run_id),
                )
                reduce_managed_assessment(database, task_id)
            if isinstance(error, SelcError):
                raise
            raise SelcError(
                "MANAGED_ACTION_FAILED",
                "the fixed installed action did not finish successfully",
                "inspect the managed action status and repair the suite",
            ) from error

        with records.projection_transaction(database):
            _assert_managed_call(database, task_id, call_id, suite_id, skill, installation)
            database.execute(
                """UPDATE managed_actions
                SET artifact_path = ?, artifact_digest = ?, status = 'finished', detail = ?, finished_at = ?
                WHERE action_run_id = ?""",
                (
                    str(artifact_path),
                    artifact_digest,
                    _encoded({"kind": action["kind"], "artifact_digest": artifact_digest}),
                    records.utc_now(),
                    action_run_id,
                ),
            )
            reduce_managed_assessment(database, task_id)
        return {
            "action_run_id": action_run_id,
            "artifact_path": str(artifact_path),
            "artifact_digest": artifact_digest,
        }


def stage_instruction_artifact(
    *,
    project_root: Path,
    suite_id: str,
    task_id: str,
    call_id: str,
    action_id: str,
    artifact_bytes: bytes,
    checker_input_bytes: bytes = b"{}",
    receipt: str | None = None,
    host: str | None = None,
    expected_version: str | None = None,
    expected_composite: str | None = None,
    environment: dict[str, str] | None = None,
    operation_arguments: dict[str, str] | None = None,
) -> dict[str, object]:
    """Stage a pure instruction artifact without racing another executor."""

    with _managed_execution_lock(project_root, suite_id):
        return _stage_instruction_artifact_locked(
            project_root=project_root,
            suite_id=suite_id,
            task_id=task_id,
            call_id=call_id,
            action_id=action_id,
            artifact_bytes=artifact_bytes,
            checker_input_bytes=checker_input_bytes,
            receipt=receipt,
            host=host,
            expected_version=expected_version,
            expected_composite=expected_composite,
            environment=environment,
            operation_arguments=operation_arguments,
            execution_lock_held=True,
        )


def _stage_instruction_artifact_locked(
    *,
    project_root: Path,
    suite_id: str,
    task_id: str,
    call_id: str,
    action_id: str,
    artifact_bytes: bytes,
    checker_input_bytes: bytes = b"{}",
    receipt: str | None = None,
    host: str | None = None,
    expected_version: str | None = None,
    expected_composite: str | None = None,
    environment: dict[str, str] | None = None,
    operation_arguments: dict[str, str] | None = None,
    execution_lock_held: bool = False,
) -> dict[str, object]:
    """Store bytes produced by a supplied instruction in owned private staging.

    This path is intentionally data-only: it accepts no command, environment,
    output path, or executable.  The manifest still fixes the action identity
    and later publication still needs its separately observed native receipt.
    """

    if not isinstance(artifact_bytes, bytes) or len(artifact_bytes) > MAX_INPUT_BYTES:
        raise SelcError("MANAGED_INPUT_INVALID", "instruction artifact must be bounded bytes")
    if not isinstance(checker_input_bytes, bytes) or len(checker_input_bytes) > MAX_INPUT_BYTES:
        raise SelcError("MANAGED_INPUT_INVALID", "instruction checker input must be bounded bytes")
    with _locked_database(project_root, suite_id, locks_held=execution_lock_held) as (
        database,
        installation,
    ):
        assert_no_live_execution(database, suite_id)
        if receipt is None and any(
            value is not None
            for value in (
                host,
                expected_version,
                expected_composite,
                environment,
                operation_arguments,
            )
        ):
            if (
                host is None
                or expected_version is None
                or expected_composite is None
                or environment is None
                or operation_arguments is None
            ):
                raise SelcError(
                    "RECEIPT_UNAVAILABLE",
                    "managed instruction staging needs a native child identity",
                )
            _assert_expected_installation(installation, expected_version, expected_composite)
        _assert_active_installation(project_root, suite_id, installation)
        suite_root = _current_root(project_root, suite_id)
        action = managed_configuration(load_manifest(suite_root), suite_root).get(action_id)
        if action is None or action["kind"] != "instruction-staging-v1":
            raise SelcError(
                "MANAGED_ACTION_UNDECLARED",
                "instruction staging is not declared for this action",
            )
        host_versions = managed_host_versions(load_manifest(suite_root))
        with records.projection_transaction(database):
            _assert_managed_call(
                database,
                task_id,
                call_id,
                suite_id,
                str(action["skill"]),
                installation,
            )
            _assert_no_pending_action(database, task_id)
            if host is not None:
                context = native_context_from_environment(host, environment or {})
                rows = _ready_receipts_for_environment(
                    database,
                    suite_id=suite_id,
                    host=host,
                    context=context,
                    action={"stage", action_id},
                    skill=str(action["skill"]),
                    expected_operation=_operation_digest(
                        operation="stage",
                        action=action_id,
                        suite_id=suite_id,
                        skill=str(action["skill"]),
                        arguments=operation_arguments or {},
                    ),
                )
                receipt_row = _consume_receipt_row(
                    database,
                    row=_select_one_ready_receipt(rows),
                    installation=installation,
                    suite_id=suite_id,
                    skill=str(action["skill"]),
                    expected_action={"stage", action_id},
                    host_versions=host_versions,
                )
            elif receipt is not None:
                receipt_row = _consume_receipt(
                    database,
                    receipt=receipt,
                    installation=installation,
                    suite_id=suite_id,
                    skill=str(action["skill"]),
                    expected_action={"stage", action_id},
                    host_versions=host_versions,
                )
            else:
                receipt_row = None
            if receipt_row is not None:
                context = _assert_receipt_subject(database, receipt_row, task_id, call_id)
                _assert_correction_binding_current(database, context, installation)
                _assert_context_selected_skill(context, str(action["skill"]))
                checker_input_digest = hashlib.sha256(checker_input_bytes).hexdigest()
                _assert_correction_checker_input_digest(database, context, checker_input_digest)
            action_run_id = f"managed-action-{secrets.token_hex(12)}"
            staging = _action_staging_root(project_root, suite_id, action_run_id)
            artifact_path = staging / "artifact"
            _write_fsynced(staging / "input", checker_input_bytes)
            _write_fsynced(artifact_path, artifact_bytes)
            artifact_digest = hashlib.sha256(artifact_bytes).hexdigest()
            database.execute(
                """INSERT INTO managed_actions(
                action_run_id, suite_id, task_id, call_id, action_id, receipt_digest,
                request_digest, artifact_path, artifact_digest, status, detail, created_at, finished_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'finished', ?, ?, ?)""",
                (
                    action_run_id,
                    suite_id,
                    task_id,
                    call_id,
                    action_id,
                    (
                        str(receipt_row["receipt_digest"])
                        if receipt_row is not None
                        else _digest({"instruction": action_run_id})
                    ),
                    _digest(
                        {
                            "instruction": artifact_digest,
                            "checker_input": hashlib.sha256(checker_input_bytes).hexdigest(),
                        }
                    ),
                    str(artifact_path),
                    artifact_digest,
                    _encoded(
                        {"kind": "instruction-staging-v1", "artifact_digest": artifact_digest}
                    ),
                    records.utc_now(),
                    records.utc_now(),
                ),
            )
            reduce_managed_assessment(database, task_id)
    return {
        "action_run_id": action_run_id,
        "artifact_path": str(artifact_path),
        "artifact_digest": artifact_digest,
    }


def publish(
    *,
    project_root: Path,
    suite_id: str,
    task_id: str,
    call_id: str,
    receipt: str | None,
    artifact_path: Path,
    target: str,
    skill: str | None = None,
    host: str | None = None,
    expected_version: str | None = None,
    expected_composite: str | None = None,
    environment: dict[str, str] | None = None,
    operation_arguments: dict[str, str] | None = None,
) -> dict[str, object]:
    """Publish one action while old workers and recovery stay mutually exclusive."""

    with _managed_execution_lock(project_root, suite_id):
        return _publish_locked(
            project_root=project_root,
            suite_id=suite_id,
            task_id=task_id,
            call_id=call_id,
            receipt=receipt,
            artifact_path=artifact_path,
            target=target,
            skill=skill,
            host=host,
            expected_version=expected_version,
            expected_composite=expected_composite,
            environment=environment,
            operation_arguments=operation_arguments,
            execution_lock_held=True,
        )


def _publish_locked(
    *,
    project_root: Path,
    suite_id: str,
    task_id: str,
    call_id: str,
    receipt: str | None,
    artifact_path: Path,
    target: str,
    skill: str | None = None,
    host: str | None = None,
    expected_version: str | None = None,
    expected_composite: str | None = None,
    environment: dict[str, str] | None = None,
    operation_arguments: dict[str, str] | None = None,
    execution_lock_held: bool = False,
) -> dict[str, object]:
    """Reserve, stage, supply, and record one immutable checked publication."""

    with _locked_database(project_root, suite_id, locks_held=execution_lock_held) as (
        database,
        installation,
    ):
        assert_no_live_execution(database, suite_id)
        if receipt is None:
            if (
                host is None
                or expected_version is None
                or expected_composite is None
                or environment is None
                or operation_arguments is None
            ):
                raise SelcError(
                    "RECEIPT_UNAVAILABLE", "managed publication needs a native child identity"
                )
            _assert_expected_installation(installation, expected_version, expected_composite)
        _assert_active_installation(project_root, suite_id, installation)
        manifest = load_manifest(_current_root(project_root, suite_id))
        host_versions = managed_host_versions(manifest)
        with records.projection_transaction(database):
            _assert_managed_call(database, task_id, call_id, suite_id, None, installation)
            _assert_no_inflight_publication(database, task_id)
            if receipt is None:
                context = native_context_from_environment(host, environment)
                rows = _ready_receipts_for_environment(
                    database,
                    suite_id=suite_id,
                    host=host,
                    context=context,
                    action="publish",
                    skill=skill,
                    expected_operation=_operation_digest(
                        operation="publish",
                        action="publish",
                        suite_id=suite_id,
                        skill=skill or "",
                        arguments=operation_arguments,
                    ),
                )
                receipt_row = _select_one_ready_receipt(rows)
                receipt_row = _consume_receipt_row(
                    database,
                    row=receipt_row,
                    installation=installation,
                    suite_id=suite_id,
                    skill=None,
                    expected_action="publish",
                    host_versions=host_versions,
                )
            else:
                receipt_row = _consume_receipt(
                    database,
                    receipt=receipt,
                    installation=installation,
                    suite_id=suite_id,
                    skill=None,
                    expected_action="publish",
                    host_versions=host_versions,
                )
            context = _assert_receipt_subject(database, receipt_row, task_id, call_id)
            _assert_correction_binding_current(database, context, installation)
            action_row = _finished_action_for_artifact(database, task_id, call_id, artifact_path)
            _assert_correction_action_input(database, context, action_row)
            _assert_correction_publish_target(database, context, project_root, target)
            target_path = _safe_target_path(project_root, target)
            delivery_id, ordinal, predecessor = _reserve_publication(
                database,
                suite_id=suite_id,
                task_id=task_id,
                call_id=call_id,
                receipt_digest=str(receipt_row["receipt_digest"]),
                action_run_id=str(action_row["action_run_id"]),
                artifact_digest=str(action_row["artifact_digest"]),
                target_path=target_path,
            )

        action_snapshot = dict(action_row)
        receipt_snapshot = dict(receipt_row)
        manifest_snapshot = manifest
        target_snapshot = target_path

    staged_path = _publication_staging_path(project_root, suite_id, delivery_id)
    try:
        _prepare_publication_bytes(
            artifact_path,
            staged_path,
            expected_digest=str(action_snapshot["artifact_digest"]),
        )
    except Exception as error:
        _mark_publication_prepare_failed(
            project_root,
            suite_id,
            task_id,
            delivery_id,
            action_snapshot,
            error,
            locks_held=execution_lock_held,
        )
        if isinstance(error, SelcError):
            raise
        raise SelcError(
            "PUBLICATION_PREPARE_FAILED",
            "managed publication could not stage immutable artifact bytes",
            "repair the action output and start a new managed publication",
        ) from error

    with _locked_database(project_root, suite_id, locks_held=execution_lock_held) as (
        database,
        installation,
    ):
        _assert_active_installation(project_root, suite_id, installation)
        with records.projection_transaction(database):
            database.execute(
                """UPDATE managed_publications
                SET staging_path = ?, status = 'prepared', prepared_at = ? WHERE delivery_id = ?
                  AND status = 'reserved'""",
                (str(staged_path), records.utc_now(), delivery_id),
            )

    execution = _check_publication(
        project_root=project_root,
        suite_id=suite_id,
        manifest=manifest_snapshot,
        task_id=task_id,
        call_id=call_id,
        delivery_id=delivery_id,
        action_row=action_snapshot,
        artifact_path=staged_path,
        locks_held=execution_lock_held,
    )

    with _locked_database(project_root, suite_id, locks_held=execution_lock_held) as (
        database,
        installation,
    ):
        _assert_active_installation(project_root, suite_id, installation)
        publication = database.execute(
            """SELECT status, action_run_id, artifact_digest FROM managed_publications
            WHERE delivery_id = ? AND task_id = ? AND call_id = ?""",
            (delivery_id, task_id, call_id),
        ).fetchone()
        current_action = database.execute(
            "SELECT status, artifact_digest FROM managed_actions WHERE action_run_id = ?",
            (action_snapshot["action_run_id"],),
        ).fetchone()
        if (
            publication is None
            or publication["status"] != "prepared"
            or publication["action_run_id"] != action_snapshot["action_run_id"]
            or publication["artifact_digest"] != action_snapshot["artifact_digest"]
            or current_action is None
            or current_action["status"] != "finished"
            or current_action["artifact_digest"] != action_snapshot["artifact_digest"]
        ):
            raise SelcError(
                "PUBLICATION_FENCED",
                "managed publication reservation changed before visible supply",
                "recover or start a new managed publication",
            )
        try:
            context = _context_for_receipt(database, receipt_snapshot)
            _assert_correction_binding_current(database, context, installation)
            _assert_correction_action_input(database, context, action_snapshot)
            _assert_correction_checker_execution(database, context, execution)
            _assert_correction_publish_target(database, context, project_root, target)
            # Revalidate the project-relative parent after checker execution.
            # The later descriptor checks bind the exchange itself, while this
            # check rejects a directory that was redirected outside the project
            # before we record a durable publication intent.
            target_snapshot = _safe_target_path(project_root, target)
            with records.projection_transaction(database):
                intent = _record_publication_intent(
                    database,
                    delivery_id=delivery_id,
                    suite_id=suite_id,
                    task_id=task_id,
                    staged_path=staged_path,
                    target_path=target_snapshot,
                    previous_owned_digest=_owned_target_digest(database, task_id, target_path),
                )
            _supply_publication(staged_path, target_snapshot, intent=intent)
        except SelcError as error:
            with records.projection_transaction(database):
                database.execute(
                    "UPDATE managed_publication_intents SET state = ?, updated_at = ? "
                    "WHERE delivery_id = ?",
                    ("uncertain", records.utc_now(), delivery_id),
                )
                database.execute(
                    "UPDATE managed_publications SET status = 'target_conflict', detail = ? WHERE delivery_id = ?",
                    (_encoded({"code": error.code}), delivery_id),
                )
                database.execute(
                    "UPDATE managed_actions SET status = 'failed', finished_at = ? WHERE action_run_id = ?",
                    (records.utc_now(), action_snapshot["action_run_id"]),
                )
                reduce_managed_assessment(database, task_id)
            raise

        with records.projection_transaction(database):
            database.execute(
                "UPDATE managed_publications SET status = 'supplied', supplied_at = ? WHERE delivery_id = ?",
                (records.utc_now(), delivery_id),
            )
            _record_managed_delivery(
                database,
                delivery_id=delivery_id,
                task_id=task_id,
                call_id=call_id,
                ordinal=ordinal,
                execution=execution,
                installation=installation,
            )
            database.execute(
                """UPDATE managed_publications
                SET checker_json = ?, status = 'recorded', detail = ?, recorded_at = ?
                WHERE delivery_id = ?""",
                (
                    _encoded(_checker_summary(execution)),
                    _encoded({"predecessor_ordinal": predecessor}),
                    records.utc_now(),
                    delivery_id,
                ),
            )
            database.execute(
                "UPDATE managed_actions SET status = 'published' WHERE action_run_id = ?",
                (action_snapshot["action_run_id"],),
            )
            database.execute(
                "UPDATE managed_publication_intents SET state = 'recorded', updated_at = ? "
                "WHERE delivery_id = ?",
                (records.utc_now(), delivery_id),
            )
            _open_publication_window(database, receipt_snapshot, task_id, call_id, delivery_id)
            reduce_managed_assessment(database, task_id)
        return {"status": "published", "delivery_id": delivery_id, "ordinal": ordinal}


def _mark_publication_prepare_failed(
    project_root: Path,
    suite_id: str,
    task_id: str,
    delivery_id: str,
    action_row: dict[str, object],
    error: Exception,
    *,
    locks_held: bool = False,
) -> None:
    with (
        _locked_database(project_root, suite_id, locks_held=locks_held) as (
            database,
            _installation,
        ),
        records.projection_transaction(database),
    ):
        database.execute(
            "UPDATE managed_publications SET status = 'prepare_failed', detail = ? WHERE delivery_id = ?",
            (_encoded({"error": type(error).__name__}), delivery_id),
        )
        database.execute(
            "UPDATE managed_actions SET status = 'failed', finished_at = ? WHERE action_run_id = ?",
            (records.utc_now(), action_row["action_run_id"]),
        )
        reduce_managed_assessment(database, task_id)


def recover(project_root: Path, suite_id: str) -> dict[str, int]:
    """Recover only after a live managed action can no longer hold the slot."""

    with _managed_execution_lock(project_root, suite_id):
        return _recover_locked(project_root, suite_id)


def _recover_locked(project_root: Path, suite_id: str) -> dict[str, int]:
    """Mark interrupted publication state conservatively without re-executing it."""

    with _locked_database(project_root, suite_id, locks_held=True) as (database, installation):
        assert_no_live_execution(database, suite_id)
        _assert_active_installation(project_root, suite_id, installation)
        rows = database.execute(
            """SELECT delivery_id, task_id, status, staging_path, target_path
            FROM managed_publications WHERE suite_id = ? AND status IN ('reserved', 'prepared', 'supplied')""",
            (suite_id,),
        ).fetchall()
        action_rows = database.execute(
            """SELECT action_run_id, task_id FROM managed_actions
            WHERE suite_id = ? AND status = 'reserved'
              AND action_run_id NOT IN (SELECT action_run_id FROM managed_publications)""",
            (suite_id,),
        ).fetchall()
        uncertain = 0
        displaced_paths = {
            str(row["delivery_id"]): _recover_interrupted_publication_staging(
                database, str(row["delivery_id"])
            )
            for row in rows
        }
        with records.projection_transaction(database):
            for row in rows:
                status = str(row["status"])
                target_exists = Path(str(row["target_path"])).exists()
                next_status = "uncertain" if target_exists or status != "reserved" else "abandoned"
                database.execute(
                    "UPDATE managed_publications SET status = ?, detail = ? WHERE delivery_id = ?",
                    (next_status, _encoded({"recovery": status}), row["delivery_id"]),
                )
                database.execute(
                    "UPDATE managed_publication_intents SET state = ?, updated_at = ? "
                    "WHERE delivery_id = ? AND state != 'recorded'",
                    ("uncertain", records.utc_now(), row["delivery_id"]),
                )
                displaced_path = displaced_paths[str(row["delivery_id"])]
                if displaced_path is not None:
                    database.execute(
                        "UPDATE managed_publication_intents SET displaced_path = ? WHERE delivery_id = ?",
                        (displaced_path, row["delivery_id"]),
                    )
                database.execute(
                    "UPDATE managed_actions SET status = 'fenced', finished_at = ? WHERE action_run_id = "
                    "(SELECT action_run_id FROM managed_publications WHERE delivery_id = ?)",
                    (records.utc_now(), row["delivery_id"]),
                )
                reduce_managed_assessment(database, str(row["task_id"]))
                uncertain += 1
            for action in action_rows:
                database.execute(
                    "UPDATE managed_actions SET status = 'fenced', finished_at = ? WHERE action_run_id = ?",
                    (records.utc_now(), action["action_run_id"]),
                )
                reduce_managed_assessment(database, str(action["task_id"]))
                uncertain += 1
            _reconcile_stopped_execution_activity(database, suite_id)
        return {"recovered": uncertain}


def health(project_root: Path, suite_id: str, task_id: str) -> dict[str, object]:
    """Return a minimal durable health report without native payloads or bodies."""

    database = records.open_database(project_root)
    try:
        task = database.execute(
            "SELECT first_result, current_result FROM tasks WHERE task_id = ? AND suite_id = ?",
            (task_id, suite_id),
        ).fetchone()
        if task is None:
            raise SelcError(
                "MISSING_TASK", "managed task does not exist", "activate the installed skill"
            )
        assessment = database.execute(
            "SELECT first_result, final_result, evidence_json FROM managed_assessments WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if assessment is None:
            return {"first_result": "unknown", "final_result": "unknown", "status": "pending"}
        return {
            "first_result": str(assessment["first_result"]),
            "final_result": str(assessment["final_result"]),
            "status": "active",
            "evidence": json.loads(str(assessment["evidence_json"])),
        }
    finally:
        database.close()


def reduce_managed_assessment(database: Any, task_id: str) -> None:
    """The single durable reducer for managed-v2 task and call assessments."""

    task = database.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    if task is None or task["assessment_owner"] != MANAGED_OWNER:
        return
    publications = database.execute(
        "SELECT * FROM managed_publications WHERE task_id = ? ORDER BY ordinal", (task_id,)
    ).fetchall()
    recorded = [row for row in publications if row["status"] == "recorded"]
    first_publication = publications[0] if publications else None
    latest = publications[-1] if publications else None
    latest_recorded = latest if latest is not None and latest["status"] == "recorded" else None
    unresolved_before_first = (
        first_publication is not None and first_publication["status"] != "recorded"
    )
    latest_result = _publication_result(latest_recorded)
    latest_delivery = (
        database.execute(
            "SELECT * FROM deliveries WHERE delivery_id = ?", (latest_recorded["delivery_id"],)
        ).fetchone()
        if latest_recorded is not None
        else None
    )
    feedback_result = (
        records._delivery_result_with_feedback(database, task_id, latest_delivery)
        if latest_delivery is not None
        else "unknown"
    )
    # A newer uncertain reservation is newer evidence than a prior checked
    # delivery.  It can never inherit that prior pass.  Conversely, an older
    # gap remains visible in first-result evidence but does not erase a later,
    # independently checked current delivery.
    final_result = (
        "fail"
        if feedback_result == "fail"
        else feedback_result
        if latest_result != "unknown"
        else "unknown"
    )
    contexts = database.execute(
        "SELECT root_valid, request_digest, intervention, unknown_change "
        "FROM managed_contexts WHERE task_id = ?",
        (task_id,),
    ).fetchall()
    windows = database.execute(
        "SELECT status FROM managed_windows WHERE task_id = ? AND delivery_id IS NOT NULL",
        (task_id,),
    ).fetchall()
    native_conflicts = int(
        database.execute(
            """SELECT COUNT(*) FROM managed_native_conflicts AS conflicts
            JOIN managed_native_events AS events ON events.event_id = conflicts.original_event_id
            WHERE events.task_id = ?""",
            (task_id,),
        ).fetchone()[0]
    )
    first_delivery = (
        database.execute(
            "SELECT * FROM deliveries WHERE delivery_id = ?", (first_publication["delivery_id"],)
        ).fetchone()
        if first_publication is not None and first_publication["status"] == "recorded"
        else None
    )
    first_delivery_result = (
        records._delivery_result_with_feedback(database, task_id, first_delivery)
        if first_delivery is not None
        else "unknown"
    )
    known_intervention = any(bool(row["intervention"]) for row in contexts)
    first_complete = (
        first_delivery is not None
        and not unresolved_before_first
        and all(bool(row["root_valid"]) for row in contexts)
        and all(bool(row["request_digest"]) for row in contexts)
        and not any(bool(row["unknown_change"]) for row in contexts)
        and windows
        and all(row["status"] == "closed" for row in windows)
        and native_conflicts == 0
    )
    if known_intervention or first_delivery_result == "fail":
        first_result = "fail"
    elif first_complete and first_delivery_result == "pass":
        first_result = "pass"
    else:
        first_result = "unknown"
    evidence = {
        "publication_count": len(publications),
        "recorded_publications": len(recorded),
        "latest_publication_status": latest["status"] if latest is not None else None,
        "unresolved_before_first": unresolved_before_first,
        "contexts": len(contexts),
        "windows_closed": sum(row["status"] == "closed" for row in windows),
        "native_conflicts": native_conflicts,
    }
    database.execute(
        """INSERT INTO managed_assessments(task_id, first_result, final_result, evidence_json, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(task_id) DO UPDATE SET first_result = excluded.first_result,
          final_result = excluded.final_result, evidence_json = excluded.evidence_json,
          updated_at = excluded.updated_at""",
        (task_id, first_result, final_result, _encoded(evidence), records.utc_now()),
    )
    database.execute(
        "UPDATE tasks SET first_result = ?, current_result = ? WHERE task_id = ?",
        (first_result, final_result, task_id),
    )
    calls = database.execute(
        "SELECT call_id, status FROM calls WHERE task_id = ?", (task_id,)
    ).fetchall()
    for call in calls:
        call_id = str(call["call_id"])
        call_publications = [row for row in publications if row["call_id"] == call_id]
        current = call_publications[-1] if call_publications else None
        current_recorded = (
            current if current is not None and current["status"] == "recorded" else None
        )
        current_delivery = (
            database.execute(
                "SELECT * FROM deliveries WHERE delivery_id = ?", (current_recorded["delivery_id"],)
            ).fetchone()
            if current_recorded is not None
            else None
        )
        call_feedback = (
            records._delivery_result_with_feedback(database, task_id, current_delivery)
            if current_delivery is not None
            else "unknown"
        )
        call_final = (
            "fail"
            if call_feedback == "fail"
            else call_feedback
            if current_recorded is not None
            else "unknown"
        )
        call_first_publication = call_publications[0] if call_publications else None
        call_first_delivery = (
            database.execute(
                "SELECT * FROM deliveries WHERE delivery_id = ?",
                (call_first_publication["delivery_id"],),
            ).fetchone()
            if call_first_publication is not None and call_first_publication["status"] == "recorded"
            else None
        )
        call_first_result = (
            records._delivery_result_with_feedback(database, task_id, call_first_delivery)
            if call_first_delivery is not None
            else "unknown"
        )
        call_windows = database.execute(
            "SELECT status FROM managed_windows WHERE task_id = ? AND call_id = ? AND delivery_id IS NOT NULL",
            (task_id, call_id),
        ).fetchall()
        call_complete = (
            call_first_delivery is not None
            and call_first_publication is not None
            and call_first_publication["status"] == "recorded"
            and call_windows
            and all(row["status"] == "closed" for row in call_windows)
            and native_conflicts == 0
        )
        if call_first_result == "fail":
            call_first = "fail"
        elif call_complete and call_first_result == "pass":
            call_first = "pass"
        else:
            call_first = "unknown"
        database.execute(
            "UPDATE calls SET first_result = ?, result = ?, status = ? WHERE call_id = ?",
            (call_first, call_final, call["status"], call_id),
        )


def _locked_database(project_root: Path, suite_id: str, *, locks_held: bool = False):
    return _ManagedDatabaseLock(project_root, suite_id, locks_held=locks_held)


@contextmanager
def _managed_execution_lock(project_root: Path, suite_id: str):
    """Hold the shared registry→suite lease for a complete side effect."""

    root = install_root(project_root) / suite_id
    with ExitStack() as locks:
        locks.enter_context(exclusive_lock(registry_lock_path(project_root)))
        locks.enter_context(exclusive_lock(root / ".lock"))
        yield


class _ManagedDatabaseLock:
    def __init__(self, project_root: Path, suite_id: str, *, locks_held: bool = False) -> None:
        self.project_root = project_root
        self.suite_id = suite_id
        self.locks_held = locks_held
        self.stack = ExitStack()
        self.database: Any | None = None
        self.installation: dict[str, Any] | None = None

    def __enter__(self) -> tuple[Any, dict[str, Any]]:
        root = install_root(self.project_root) / self.suite_id
        if not self.locks_held:
            self.stack.enter_context(exclusive_lock(registry_lock_path(self.project_root)))
            self.stack.enter_context(exclusive_lock(root / ".lock"))
        self.installation = current_installation(self.project_root, self.suite_id)
        self.database = records.open_database(self.project_root)
        return self.database, self.installation

    def __exit__(self, *arguments: object) -> None:
        if self.database is not None:
            self.database.close()
        self.stack.close()


def _normalize_native_payload(
    project_root: Path,
    suite_id: str,
    payload: dict[str, object],
    *,
    trusted_producer: bool,
    fixture: bool,
) -> dict[str, object]:
    required = {
        "host",
        "surface",
        "client_version",
        "cwd",
        "session_id",
        "generation_id",
        "phase",
        "request",
        "transcript_path",
        "prefix_length",
    }
    if not isinstance(payload, dict) or required.difference(payload):
        raise SelcError(
            "NATIVE_COLLECTOR_INVALID",
            "native collector payload is incomplete",
            "repair the installed host collector before invoking the skill",
        )
    host = _required_string(payload, "host")
    if host not in SUPPORTED_HOSTS:
        raise SelcError(
            "NATIVE_HOST_UNSUPPORTED", "native host is unsupported", "use a supported collector"
        )
    phase = _required_string(payload, "phase")
    if phase not in {
        USER_PROMPT_PHASE,
        PRE_PHASE,
        POST_PHASE,
        STOP_PHASE,
        PERMISSION_PHASE,
        PERMISSION_DENIED_PHASE,
    }:
        raise SelcError(
            "NATIVE_COLLECTOR_INVALID",
            "native phase is unsupported",
            "use UserPromptSubmit, PreToolUse, PostToolUse, or Stop",
        )

    request_from_prompt = False
    command_value: dict[str, str] | None = None
    correction: dict[str, object] | None = None
    tool_command_digest = ""
    technical_skill_event = False
    technical_skill_pre = False
    technical_skill_valid = False
    technical_skill_args_digest = ""
    business_dto_digest: str | None = None
    if phase == USER_PROMPT_PHASE:
        request = payload["request"]
        if not isinstance(request, dict):
            raise SelcError(
                "NATIVE_COLLECTOR_INVALID",
                "native prompt request is not an object",
                "use the installed managed prompt form",
            )
        skill = _optional_string(request, "skill") or ""
        action = ""
        command_digest = _digest({"phase": USER_PROMPT_PHASE, "suite": suite_id})
        operation_digest = ""
        request_digest = _native_request_digest(payload, skill)
        raw_request_digest = _raw_request_digest(payload)
        correction = _correction_declaration(payload["request"])
        business_dto_digest = _business_dto_goal_digest_from_request(payload["request"])
        tool_use_id = ""
    elif phase in {STOP_PHASE, PERMISSION_PHASE, PERMISSION_DENIED_PHASE}:
        if payload["request"] != {"source": "user-prompt"}:
            raise SelcError(
                "NATIVE_COLLECTOR_INVALID",
                "native stop is not bound to a managed prompt context",
                "leave execution activity open when the native stop cannot be verified",
            )
        action = ""
        skill = ""
        command_digest = _digest({"phase": phase, "suite": suite_id})
        operation_digest = ""
        request_digest = ""
        raw_request_digest = ""
        request_from_prompt = True
        tool_use_id = _optional_string(payload, "tool_use_id") or ""
    else:
        if "tool_use_id" not in payload:
            raise SelcError(
                "NATIVE_COLLECTOR_INVALID",
                "native tool boundary has no tool identity",
                "pause before managed execution and repair the host collector",
            )
        technical_skill_event = payload.get("technical_skill_event") is True
        if technical_skill_event:
            if (
                host != "workbuddy"
                or _required_string(payload, "tool_name") != "Skill"
                or phase not in {PRE_PHASE, POST_PHASE}
            ):
                raise SelcError(
                    "NATIVE_COLLECTOR_INVALID",
                    "native technical Skill event has an unsupported host or phase",
                )
            technical_skill_pre = payload.get("technical_skill_pre") is True
            if technical_skill_pre != (phase == PRE_PHASE):
                raise SelcError(
                    "NATIVE_COLLECTOR_INVALID",
                    "native technical Skill event has an invalid Pre marker",
                )
            technical_skill_valid = payload.get("technical_skill_valid") is True
            technical_skill_args_digest = _required_string(payload, "technical_skill_args_digest")
            if not _is_sha256_digest(technical_skill_args_digest):
                raise SelcError(
                    "NATIVE_COLLECTOR_INVALID",
                    "native technical Skill arguments have no fixed digest",
                )
            skill = _optional_string(payload, "technical_skill_name") or ""
            request = payload["request"]
            if not isinstance(request, dict):
                raise SelcError(
                    "NATIVE_COLLECTOR_INVALID",
                    "native technical Skill request is not an object",
                )
            action = "technical_skill"
            command_digest = _digest(
                {
                    "phase": phase,
                    "tool": "Skill",
                    "skill": skill,
                    "args_digest": technical_skill_args_digest,
                }
            )
            operation_digest = ""
            request_digest = _native_request_digest(payload, skill)
            raw_request_digest = _raw_request_digest(payload)
            correction = _correction_declaration(request)
            business_dto_digest = _business_dto_goal_digest_from_request(request)
            tool_use_id = _required_string(payload, "tool_use_id")
        elif _required_string(payload, "tool_name") not in {"Bash", "exec_command", "shell"}:
            raise SelcError(
                "NATIVE_COLLECTOR_INVALID", "native tool is not an installed action tool"
            )
        else:
            tool_input = payload["tool_input"]
            if not isinstance(tool_input, dict) or set(tool_input) != {"command"}:
                raise SelcError(
                    "NATIVE_COLLECTOR_INVALID",
                    "native tool input is not a fixed managed command",
                )
            command = _required_managed_command(tool_input)
            tool_command_digest = hashlib.sha256(command.encode("utf-8")).hexdigest()
            parsed_command = parse_installed_managed_command(
                command,
                suite_id,
                project_root=project_root,
            )
            if parsed_command is None:
                if payload.get("unmanaged_tool") is not True:
                    raise SelcError(
                        "NATIVE_COLLECTOR_INVALID",
                        "native tool is not the fixed installed managed wrapper",
                    )
                action = "unmanaged"
                skill = ""
                command_value = {
                    "action": action,
                    "command_digest": hashlib.sha256(command.encode("utf-8")).hexdigest(),
                }
                command_digest = _digest(command_value)
                operation_digest = ""
            else:
                action = _required_string(parsed_command, "action")
                skill = _optional_string(parsed_command, "skill") or ""
                if (
                    parsed_command.get("format") == "shell"
                    and parsed_command.get("arguments", "")
                    and json.loads(parsed_command["arguments"]).get("--host") != host
                ):
                    raise SelcError(
                        "NATIVE_COLLECTOR_INVALID",
                        "managed wrapper host does not match the native callback host",
                    )
                command_value = parsed_command
                command_digest = _digest(command_value)
                operation_digest = command_digest if parsed_command.get("format") == "shell" else ""
            tool_use_id = _required_string(payload, "tool_use_id")
            request = payload["request"]
            request_from_prompt = request == {"source": "user-prompt"}
            request_digest = "" if request_from_prompt else _native_request_digest(payload, skill)
            raw_request_digest = "" if request_from_prompt else _raw_request_digest(payload)
            business_dto_digest = None

    surface = _required_string(payload, "surface")
    client_version = _required_string(payload, "client_version")
    _verify_collector_cwd(project_root, payload)
    try:
        transcript = _verified_transcript_prefix(
            project_root,
            payload,
            host=host,
            fixture=fixture,
        )
        root_proof_status = "verified"
    except SelcError as error:
        if error.code != "ROOT_PROOF_UNAVAILABLE":
            raise
        transcript = {"length": None, "sha256": None, "root_candidate": False}
        root_proof_status = error.code
    # The durable raw event needs a stable file identity to reconcile a later
    # post-tool cutoff.  It never needs the host transcript path itself.
    # Keeping only these proof fields prevents the records database from
    # becoming a copy of host-native location metadata.
    stored_prefix = {key: value for key, value in transcript.items() if key != "path"}
    session_id = _required_string(payload, "session_id")
    generation_id = _required_string(payload, "generation_id")
    request_id = _optional_string(payload, "request_id") or generation_id
    root_request_id = _optional_string(payload, "root_request_id") or ""
    parent_request_id = _optional_string(payload, "parent_request_id") or ""
    causation = payload.get("causation", "unknown")
    if causation not in {
        "original_request",
        "input_preparation",
        "permission",
        "automatic_retry",
        "business_correction",
        "correction_candidate",
        "audit",
        "unknown",
    }:
        causation = "unknown"
    stable_identity = _digest(
        {
            "host": host,
            "suite": suite_id,
            "session": session_id,
            "generation": generation_id,
            "request": request_id,
            "tool": tool_use_id,
            "phase": phase,
        }
    )
    operation_arguments: dict[str, str] = {}
    if isinstance(command_value, dict) and command_value.get("format") == "shell":
        encoded_arguments = command_value.get("arguments")
        if isinstance(encoded_arguments, str):
            decoded_arguments = json.loads(encoded_arguments)
            if isinstance(decoded_arguments, dict) and all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in decoded_arguments.items()
            ):
                operation_arguments = decoded_arguments
    payload_digest_input: dict[str, object] = {
        "identity": stable_identity,
        "command": command_value,
        "raw_request": raw_request_digest or "prompt-context",
        "source": "fixture" if fixture else "native-collector",
    }
    # Existing native rows predate technical Skill boundaries.  Their digest
    # formula is a durable replay contract, so only the new event family adds
    # its technical fields.
    if technical_skill_event:
        payload_digest_input["technical_skill"] = {
            "pre": technical_skill_pre,
            "valid": technical_skill_valid,
            "args_digest": technical_skill_args_digest,
        }

    return {
        "event_id": f"native-{secrets.token_hex(16)}",
        "stable_identity": stable_identity,
        "payload_digest": _digest(payload_digest_input),
        "payload_json": _encoded(
            {
                "action": action,
                "skill": skill,
                "command_digest": command_digest,
                "operation_digest": operation_digest,
                "operation_arguments": operation_arguments,
                "tool_command_digest": tool_command_digest or None,
                "technical_skill_event": technical_skill_event,
                "technical_skill_pre": technical_skill_pre,
                "technical_skill_valid": technical_skill_valid,
                "technical_skill_args_digest": technical_skill_args_digest or None,
                "request_digest": request_digest or None,
                "business_dto_digest": business_dto_digest,
                "business_dto_rule_version": (
                    BUSINESS_DTO_RULE_VERSION if business_dto_digest is not None else None
                ),
                "raw_request_digest": raw_request_digest or None,
                "request_from_prompt": request_from_prompt,
                "request_id": request_id,
                "surface": surface,
                "client_version": client_version,
                "message_id": _optional_string(payload, "message_id"),
                "root_request_id": _optional_string(payload, "root_request_id"),
                "parent_request_id": _optional_string(payload, "parent_request_id"),
                "parent_message_id": _optional_string(payload, "parent_message_id"),
                "causation": causation,
                "correction": correction,
                "prefix": stored_prefix,
                "root_proof_status": root_proof_status,
                "source": "fixture" if fixture else "native-collector",
            }
        ),
        "host": host,
        "suite_id": suite_id,
        "session_id": session_id,
        "generation_id": generation_id,
        "surface": surface,
        "client_version": client_version,
        "cwd": str(project_root.resolve()),
        "tool_use_id": tool_use_id,
        "request_id": request_id,
        "root_request_id": root_request_id,
        "parent_request_id": parent_request_id,
        "phase": phase,
        "action": action,
        "skill": skill,
        "command_digest": command_digest,
        "operation_digest": operation_digest,
        "operation_arguments": operation_arguments,
        "tool_command_digest": tool_command_digest,
        "technical_skill_event": technical_skill_event,
        "technical_skill_pre": technical_skill_pre,
        "technical_skill_valid": technical_skill_valid,
        "technical_skill_args_digest": technical_skill_args_digest,
        "request_digest": request_digest,
        "business_dto_digest": business_dto_digest,
        "business_dto_rule_version": (
            BUSINESS_DTO_RULE_VERSION if business_dto_digest is not None else None
        ),
        "raw_request_digest": raw_request_digest,
        "request_from_prompt": request_from_prompt,
        "causation": causation,
        "correction": correction,
        "prefix": transcript,
        "transcript_path": str(transcript.get("path") or ""),
        "root_valid": (
            bool(transcript["root_candidate"])
            and root_proof_status == "verified"
            and trusted_producer
        ),
        "fixture": fixture,
        "message_id": _optional_string(payload, "message_id") or "",
    }


def _verified_transcript_prefix(
    project_root: Path,
    payload: dict[str, object],
    *,
    host: str,
    fixture: bool,
) -> dict[str, object]:
    """Freeze an exact native-history prefix without accepting arbitrary files.

    The collector passes one already-known native session path.  This function
    never discovers or scans history.  Test fixtures retain their isolated
    local paths but cannot establish production root evidence.
    """

    path_value = _required_string(payload, "transcript_path")
    transcript_path = Path(path_value)
    try:
        resolved = transcript_path.resolve(strict=True)
    except OSError as error:
        raise SelcError(
            "ROOT_PROOF_UNAVAILABLE",
            "native transcript path is unavailable",
            "repair collector path ownership before activation",
        ) from error
    if transcript_path.is_symlink() or not resolved.is_file():
        raise SelcError("ROOT_PROOF_UNAVAILABLE", "native transcript is not a regular owned file")
    if not fixture:
        _verify_native_history_path(project_root, payload, host, resolved)
    prefix_length = payload["prefix_length"]
    if (
        type(prefix_length) is not int
        or prefix_length < 0
        or prefix_length > MAX_TRANSCRIPT_PREFIX_BYTES
    ):
        raise SelcError(
            "ROOT_PROOF_UNAVAILABLE", "native prefix length is outside the supported bound"
        )
    if resolved.stat().st_size < prefix_length:
        raise SelcError("ROOT_PROOF_UNAVAILABLE", "native transcript prefix is incomplete")
    with resolved.open("rb") as stream:
        prefix = stream.read(prefix_length)
    if len(prefix) != prefix_length:
        raise SelcError("ROOT_PROOF_UNAVAILABLE", "native transcript prefix is incomplete")
    stat = resolved.stat()
    is_codex_prompt_anchor = host == "codex" and payload.get("phase") == USER_PROMPT_PHASE
    return {
        "length": prefix_length,
        "sha256": hashlib.sha256(prefix).hexdigest(),
        "root_candidate": (
            False
            if is_codex_prompt_anchor
            else _metadata_only_prefix(
                prefix,
                host=host,
                session_id=_required_string(payload, "session_id"),
                cwd=project_root.resolve(),
                generation_id=_required_string(payload, "generation_id"),
                raw_prompt_digest=_raw_request_digest(payload),
            )
        ),
        "path_hash": hashlib.sha256(str(resolved).encode("utf-8")).hexdigest(),
        "device": stat.st_dev,
        "inode": stat.st_ino,
        "path": str(resolved),
    }


def _verify_native_history_path(
    project_root: Path, payload: dict[str, object], host: str, path: Path
) -> None:
    """Verify the exact callback path belongs to the reported host/session."""

    home = Path.home().resolve()
    session_id = _required_string(payload, "session_id")
    cwd = project_root.resolve()
    if host == "workbuddy":
        compressed_cwd = str(cwd).lstrip("/").replace("/", "-")
        expected_parents = {
            home / ".codebuddy" / "projects" / compressed_cwd,
            home / ".workbuddy" / "projects" / compressed_cwd,
        }
        if path.parent not in expected_parents or path.name != f"{session_id}.jsonl":
            raise SelcError(
                "ROOT_PROOF_UNAVAILABLE",
                "WorkBuddy transcript does not match its native project and session path",
            )
        return
    if host == "codex":
        codex_root = home / ".codex" / "sessions"
        try:
            path.relative_to(codex_root)
        except ValueError as error:
            raise SelcError(
                "ROOT_PROOF_UNAVAILABLE",
                "Codex transcript is outside the native session store",
            ) from error
        if path.suffix != ".jsonl":
            raise SelcError("ROOT_PROOF_UNAVAILABLE", "Codex transcript is not a JSONL session")
        return
    raise SelcError("NATIVE_HOST_UNSUPPORTED", "native host is unsupported")


def _metadata_only_prefix(
    prefix: bytes,
    *,
    host: str,
    session_id: str,
    cwd: Path,
    generation_id: str = "",
    raw_prompt_digest: str = "",
) -> bool:
    """Accept only a serializer-verified metadata prefix as a new root.

    Empty files are deliberately insufficient: an arbitrary empty file has no
    proof of native ownership.  The WorkBuddy serializer writes session-meta
    records before the first user message; Codex writes a session_meta record.
    """

    if not prefix:
        return False
    try:
        lines = [line for line in prefix.splitlines() if line.strip()]
        records_json = [json.loads(line) for line in lines]
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not records_json or not all(isinstance(record, dict) for record in records_json):
        return False
    unresolved_history_base = {
        "history_base",
        "historyBase",
        "forked_from",
        "forkedFrom",
        "compacted_from",
        "compactedFrom",
    }
    if any(
        _contains_unresolved_history_base(record, unresolved_history_base)
        for record in records_json
    ):
        return False
    if host == "workbuddy":
        if not all(record.get("type") == "session-meta" for record in records_json):
            return False
        return all(record.get("sessionId") == session_id for record in records_json)
    if host == "codex":
        return _codex_root_prefix_is_verified(
            records_json,
            session_id=session_id,
            cwd=cwd,
            generation_id=generation_id,
            raw_prompt_digest=raw_prompt_digest,
        )
    return False


def _codex_root_prefix_is_verified(
    history: list[dict[str, object]],
    *,
    session_id: str,
    cwd: Path,
    generation_id: str,
    raw_prompt_digest: str,
) -> bool:
    """Prove an initial Codex request without treating injected context as user input."""

    if not raw_prompt_digest or not history or history[0].get("type") != "session_meta":
        return False
    metadata = history[0].get("payload")
    if not isinstance(metadata, dict):
        return False
    identities = {
        value
        for key in ("id", "session_id")
        if isinstance((value := metadata.get(key)), str) and value
    }
    if identities != {session_id} or metadata.get("cwd") != str(cwd):
        return False

    known_injected_kinds = {
        "agents_md.instructions",
        "environments.environment_context",
    }
    known_developer_kinds = {
        "host_skills.instructions",
        "permissions.instructions",
        "multi_agent.usage_hint",
        "multi_agent.mode_instructions",
    }
    user_prompts = 0
    for record in history[1:]:
        record_type = record.get("type")
        if record_type == "world_state":
            payload = record.get("payload")
            if not _codex_world_state_is_verified(payload):
                return False
            continue
        if record_type == "turn_context":
            payload = record.get("payload")
            if not _codex_turn_context_is_verified(payload, generation_id, cwd):
                return False
            continue
        if record_type == "event_msg":
            payload = record.get("payload")
            if not _codex_initial_event_is_verified(payload, generation_id, session_id):
                return False
            continue
        if record_type != "response_item":
            return False
        payload = record.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "message":
            return False
        role = payload.get("role")
        metadata = payload.get("internal_chat_message_metadata_passthrough")
        if not isinstance(metadata, dict) or metadata.get("turn_id") != generation_id:
            return False
        kinds = metadata.get("content_item_kinds")
        if not isinstance(kinds, list) or not all(isinstance(kind, str) for kind in kinds):
            return False
        kind_set = set(kinds)
        if role == "developer":
            if not kind_set or not kind_set.issubset(known_developer_kinds):
                return False
            continue
        if role != "user":
            return False
        if "user.text" not in kind_set:
            if not kind_set or not kind_set.issubset(known_injected_kinds):
                return False
            continue
        if kinds != ["user.text"]:
            return False
        content = payload.get("content")
        if (
            not isinstance(content, list)
            or len(content) != 1
            or not isinstance(content[0], dict)
            or content[0].get("type") != "input_text"
            or not isinstance(content[0].get("text"), str)
        ):
            return False
        prompt_digest = hashlib.sha256(content[0]["text"].encode("utf-8")).hexdigest()
        if prompt_digest != raw_prompt_digest:
            return False
        user_prompts += 1
    return user_prompts == 1


def _codex_world_state_is_verified(payload: object) -> bool:
    """Validate the observed full world-state initialization shape."""

    return (
        isinstance(payload, dict)
        and set(payload) == {"full", "state"}
        and payload.get("full") is True
        and isinstance(payload.get("state"), dict)
    )


def _codex_turn_context_is_verified(
    payload: object,
    generation_id: str,
    cwd: Path,
) -> bool:
    """Bind an observed turn-context record to this initial request."""

    return (
        isinstance(payload, dict)
        and payload.get("turn_id") == generation_id
        and payload.get("root_turn_id") == generation_id
        and payload.get("cwd") == str(cwd)
    )


def _codex_initial_event_is_verified(
    payload: object,
    generation_id: str,
    session_id: str,
) -> bool:
    """Validate observed initial event records without inventing a parent relation."""

    if not isinstance(payload, dict):
        return False
    event_type = payload.get("type")
    if event_type == "thread_settings_applied":
        return payload.get("thread_id") == session_id
    if event_type != "task_started":
        return False
    attribution = payload.get("turn_attribution")
    return (
        payload.get("turn_id") == generation_id
        and payload.get("root_turn_id") == generation_id
        and isinstance(attribution, dict)
        and attribution.get("turn_id") == generation_id
        and attribution.get("root_turn_id") == generation_id
        and attribution.get("parent_turn_id") is None
    )


def _codex_initialization_prefix_is_verified(
    history: list[dict[str, object]],
    *,
    session_id: str,
    cwd: Path,
    generation_id: str,
) -> bool:
    """Verify the observed Codex pre-append initialization sequence.

    This anchor intentionally has no ``user.text`` record.  The original
    prompt appears only in the later PreToolUse snapshot and is verified there
    against the raw digest frozen by UserPromptSubmit.
    """

    if not history or history[0].get("type") != "session_meta":
        return False
    metadata = history[0].get("payload")
    if not isinstance(metadata, dict):
        return False
    identities = {
        value
        for key in ("id", "session_id")
        if isinstance((value := metadata.get(key)), str) and value
    }
    if identities != {session_id} or metadata.get("cwd") != str(cwd):
        return False

    known_injected_kinds = {
        "agents_md.instructions",
        "environments.environment_context",
    }
    known_developer_kinds = {
        "host_skills.instructions",
        "permissions.instructions",
        "multi_agent.usage_hint",
        "multi_agent.mode_instructions",
    }
    found_world_state = False
    found_turn_context = False
    found_task_started = False
    found_thread_settings = False
    found_developer_message = False
    found_injected_user_message = False

    for record in history[1:]:
        record_type = record.get("type")
        if record_type == "world_state":
            if found_world_state or not _codex_world_state_is_verified(record.get("payload")):
                return False
            found_world_state = True
            continue
        if record_type == "turn_context":
            if found_turn_context or not _codex_turn_context_is_verified(
                record.get("payload"), generation_id, cwd
            ):
                return False
            found_turn_context = True
            continue
        if record_type == "event_msg":
            payload = record.get("payload")
            if not isinstance(payload, dict):
                return False
            if payload.get("type") == "task_started":
                if found_task_started or not _codex_initial_event_is_verified(
                    payload, generation_id, session_id
                ):
                    return False
                found_task_started = True
                continue
            if payload.get("type") == "thread_settings_applied":
                if found_thread_settings or not _codex_initial_event_is_verified(
                    payload, generation_id, session_id
                ):
                    return False
                found_thread_settings = True
                continue
            return False
        if record_type != "response_item":
            return False
        payload = record.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "message":
            return False
        message_metadata = payload.get("internal_chat_message_metadata_passthrough")
        if (
            not isinstance(message_metadata, dict)
            or message_metadata.get("turn_id") != generation_id
        ):
            return False
        kinds = message_metadata.get("content_item_kinds")
        if not isinstance(kinds, list) or not all(isinstance(kind, str) for kind in kinds):
            return False
        kind_set = set(kinds)
        if payload.get("role") == "developer":
            if not kind_set or not kind_set.issubset(known_developer_kinds):
                return False
            found_developer_message = True
            continue
        if payload.get("role") == "user":
            if (
                not kind_set
                or "user.text" in kind_set
                or not kind_set.issubset(known_injected_kinds)
            ):
                return False
            found_injected_user_message = True
            continue
        return False

    return all(
        (
            found_world_state,
            found_turn_context,
            found_task_started,
            found_thread_settings,
            found_developer_message,
            found_injected_user_message,
        )
    )


def _contains_unresolved_history_base(value: object, names: set[str]) -> bool:
    if isinstance(value, dict):
        return any(
            key in names or _contains_unresolved_history_base(child, names)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return any(_contains_unresolved_history_base(child, names) for child in value)
    return False


def _projection_event(
    database: Any,
    stored: dict[str, str],
    incoming: dict[str, object],
) -> dict[str, object] | None:
    """Rebuild one projection from durable raw facts and a verified native path.

    A callback can fail after its native row commits but before a derived
    context, receipt, or Stop closure commits.  Replays may arrive after the
    transcript grows, so the original frozen prefix remains authoritative and
    the replay contributes only a matching file handle/path for bounded reads.
    """

    row = database.execute(
        """SELECT payload_json, host, suite_id, session_id, generation_id, request_id,
        tool_use_id, phase FROM managed_native_events WHERE event_id = ?""",
        (stored["event_id"],),
    ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(str(row["payload_json"]))
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    stored_prefix = payload.get("prefix")
    incoming_prefix = incoming.get("prefix")
    stored_fields = _frozen_prefix_fields(stored_prefix)
    incoming_fields = _frozen_prefix_fields(incoming_prefix)
    if stored_fields is None or incoming_fields is None:
        unavailable_prefix = {"length": None, "sha256": None, "root_candidate": False}
        if stored_prefix != unavailable_prefix or incoming_prefix != unavailable_prefix:
            return None
    else:
        _, _, stored_path_hash, stored_device, stored_inode = stored_fields
        _, _, incoming_path_hash, incoming_device, incoming_inode = incoming_fields
        if (
            stored_path_hash != incoming_path_hash
            or stored_device != incoming_device
            or stored_inode != incoming_inode
        ):
            return None
    source = payload.get("source")
    if source not in {"fixture", "native-collector"}:
        return None
    reconstructed = dict(incoming)
    reconstructed.update(
        {
            "event_id": stored["event_id"],
            "host": row["host"],
            "suite_id": row["suite_id"],
            "session_id": row["session_id"],
            "generation_id": row["generation_id"],
            "request_id": row["request_id"],
            "surface": payload.get("surface"),
            "client_version": payload.get("client_version"),
            "tool_use_id": row["tool_use_id"],
            "phase": row["phase"],
            "action": payload.get("action"),
            "skill": payload.get("skill", incoming.get("skill", "")),
            "command_digest": payload.get("command_digest"),
            "operation_digest": payload.get("operation_digest"),
            "operation_arguments": payload.get(
                "operation_arguments",
                incoming.get("operation_arguments", {}),
            ),
            "tool_command_digest": payload.get("tool_command_digest") or "",
            "technical_skill_event": payload.get("technical_skill_event") is True,
            "technical_skill_pre": payload.get("technical_skill_pre") is True,
            "technical_skill_valid": payload.get("technical_skill_valid") is True,
            "technical_skill_args_digest": payload.get("technical_skill_args_digest") or "",
            "request_digest": payload.get("request_digest") or "",
            "business_dto_digest": payload.get("business_dto_digest"),
            "business_dto_rule_version": payload.get("business_dto_rule_version"),
            "raw_request_digest": payload.get("raw_request_digest") or "",
            "request_from_prompt": payload.get("request_from_prompt") is True,
            "root_request_id": payload.get("root_request_id") or "",
            "parent_request_id": payload.get("parent_request_id") or "",
            "message_id": payload.get("message_id") or "",
            "causation": payload.get("causation") or "unknown",
            "correction": payload.get("correction"),
            "fixture": source == "fixture",
            "root_valid": bool(stored_prefix.get("root_candidate"))
            and source == "native-collector"
            and payload.get("root_proof_status") == "verified",
        }
    )
    reconstructed["prefix"] = dict(stored_prefix)
    reconstructed["transcript_path"] = incoming["transcript_path"]
    return reconstructed


def _project_native_event(
    database: Any,
    installation: dict[str, Any],
    event: dict[str, object],
    *,
    event_id: str,
) -> dict[str, object]:
    """Apply one idempotent projection after its immutable native row commits."""

    if event["phase"] == USER_PROMPT_PHASE:
        _record_prompt_context(database, event, event_id)
        return {"status": "prompt_recorded"}
    if event.get("technical_skill_event") is True and event.get("technical_skill_pre") is True:
        context = database.execute(
            "SELECT * FROM managed_contexts WHERE context_key = ?", (_context_key(event),)
        ).fetchone()
        if context is None or not _ensure_current_request_anchor(
            database, context, event, event_id
        ):
            return {"status": "technical_unusable"}
        return {"status": "technical_recorded"}
    if event["phase"] == STOP_PHASE:
        _close_pending_windows_on_stop(database, event, event_id)
        _close_execution_activity(database, event)
        _close_idle_managed_task(database, event)
        _close_correction_binding(database, event)
        return {"status": "stop_recorded"}
    if event["phase"] in {PERMISSION_PHASE, PERMISSION_DENIED_PHASE}:
        _record_permission_context(database, event)
        return {"status": "permission_recorded"}
    if event["phase"] == POST_PHASE:
        _close_matching_window(database, event)
        return {"status": "post_recorded"}
    receipt = _issue_receipt(database, installation, event, event_id)
    if receipt is None:
        return {"status": "recorded_unusable"}
    return {"status": "recorded", "receipt": receipt}


def _store_native_event(database: Any, event: dict[str, object]) -> dict[str, str]:
    suppressed = database.execute(
        "SELECT 1 FROM managed_suppressions WHERE stable_identity = ?", (event["stable_identity"],)
    ).fetchone()
    if suppressed is not None:
        return {"status": "suppressed", "event_id": ""}
    suppressed_context = database.execute(
        """SELECT 1 FROM managed_context_suppressions
        WHERE suite_id = ? AND host = ? AND session_id = ? AND generation_id = ? AND request_id = ?""",
        (
            event["suite_id"],
            event["host"],
            event["session_id"],
            event["generation_id"],
            event["request_id"],
        ),
    ).fetchone()
    if suppressed_context is not None:
        return {"status": "suppressed", "event_id": ""}
    existing = database.execute(
        "SELECT * FROM managed_native_events WHERE stable_identity = ?", (event["stable_identity"],)
    ).fetchone()
    if existing is not None:
        if existing["payload_digest"] == event["payload_digest"]:
            return {
                "status": "replayed",
                "event_id": str(existing["event_id"]),
                "projection_status": str(existing["projection_status"]),
            }
        database.execute(
            """INSERT INTO managed_native_conflicts(
            stable_identity, original_event_id, incoming_digest, received_at)
            VALUES (?, ?, ?, ?)""",
            (
                event["stable_identity"],
                existing["event_id"],
                event["payload_digest"],
                records.utc_now(),
            ),
        )
        database.execute(
            """UPDATE managed_receipts SET status = 'invalid', invalidated_at = ?
            WHERE native_event_id = ? AND status = 'ready'""",
            (records.utc_now(), existing["event_id"]),
        )
        context = database.execute(
            """SELECT context_key, task_id FROM managed_contexts
            WHERE suite_id = ? AND host = ? AND session_id = ? AND generation_id = ? AND request_id = ?""",
            (
                existing["suite_id"],
                existing["host"],
                existing["session_id"],
                existing["generation_id"],
                existing["request_id"],
            ),
        ).fetchone()
        task_id = existing["task_id"]
        if context is not None:
            database.execute(
                "UPDATE managed_contexts SET unknown_change = 1 WHERE context_key = ?",
                (context["context_key"],),
            )
        if task_id is None and context is not None:
            task_id = context["task_id"]
        if task_id is not None:
            reduce_managed_assessment(database, str(task_id))
        return {
            "status": "conflict",
            "event_id": str(existing["event_id"]),
            "projection_status": "applied",
        }
    database.execute(
        """INSERT INTO managed_native_events(
        event_id, stable_identity, payload_digest, payload_json, host, suite_id, session_id,
        generation_id, request_id, tool_use_id, phase, received_at, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'recorded')""",
        (
            event["event_id"],
            event["stable_identity"],
            event["payload_digest"],
            event["payload_json"],
            event["host"],
            event["suite_id"],
            event["session_id"],
            event["generation_id"],
            event["request_id"],
            event["tool_use_id"],
            event["phase"],
            records.utc_now(),
        ),
    )
    return {
        "status": "recorded",
        "event_id": str(event["event_id"]),
        "projection_status": "pending",
    }


def _context_key(event: dict[str, object]) -> str:
    """Keep request contexts scoped to one suite and exact native request."""

    return _digest(
        {
            "suite": event["suite_id"],
            "host": event["host"],
            "session": event["session_id"],
            "generation": event["generation_id"],
            "request": event["request_id"],
        }
    )


def _record_prompt_context(database: Any, event: dict[str, object], event_id: str) -> None:
    """Project one strict pre-append prompt boundary without retaining its body."""

    context_key = _context_key(event)
    context = database.execute(
        "SELECT * FROM managed_contexts WHERE context_key = ?", (context_key,)
    ).fetchone()
    if context is None:
        database.execute(
            """INSERT INTO managed_contexts(
            context_key, suite_id, host, session_id, generation_id, request_id, root_request_id,
            request_digest, input_skill, business_dto_digest, business_dto_rule_version,
            root_event_id, root_valid, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)""",
            (
                context_key,
                event["suite_id"],
                event["host"],
                event["session_id"],
                event["generation_id"],
                event["request_id"],
                event["root_request_id"],
                event["request_digest"],
                event["skill"],
                event["business_dto_digest"],
                event["business_dto_rule_version"],
                event_id,
                int(bool(event["root_valid"])),
                records.utc_now(),
            ),
        )
    elif (
        context["request_digest"] != event["request_digest"]
        or context["business_dto_digest"] != event["business_dto_digest"]
        or context["business_dto_rule_version"] != event["business_dto_rule_version"]
    ):
        database.execute(
            "UPDATE managed_contexts SET unknown_change = 1 WHERE context_key = ?", (context_key,)
        )
    if event["causation"] in {"business_correction", "correction_candidate"}:
        # Prose and a host marker may describe a correction, but neither proves
        # which fixed goal is being preserved.  Leave the context unlinked
        # until a verified current JSON declaration passes the durable binding
        # checks at its first PreToolUse boundary.
        database.execute(
            "UPDATE managed_contexts SET unknown_change = 1 WHERE context_key = ?", (context_key,)
        )
    elif event["causation"] in {"input_preparation", "automatic_retry", "permission"}:
        _link_explicit_followup_context(database, context_key, event)
    elif event["causation"] == "audit":
        database.execute(
            "UPDATE managed_contexts SET unknown_change = 1 WHERE context_key = ?", (context_key,)
        )


def _promote_codex_prompt_root(
    database: Any,
    context: Any,
    event: dict[str, object],
    event_id: str,
) -> bool:
    """Promote a Codex P0 anchor only after its frozen Pre snapshot proves the prompt.

    UserPromptSubmit happens before Codex appends the native ``user.text``
    record.  The stored Pre event supplies P1, while the original prompt event
    supplies the raw one-way digest and P0.  Replays always use the persisted
    event prefix; a later larger callback cannot become a new anchor.
    """

    if event["host"] != "codex" or not event["request_from_prompt"]:
        return bool(context["root_valid"])
    if bool(context["root_valid"]):
        return True
    root_event_id = context["root_event_id"]
    if not isinstance(root_event_id, str) or not root_event_id:
        return False
    root_row = database.execute(
        """SELECT phase, payload_json, host, session_id, generation_id, request_id
        FROM managed_native_events WHERE event_id = ?""",
        (root_event_id,),
    ).fetchone()
    pre_row = database.execute(
        """SELECT payload_json, host, session_id, generation_id, request_id
        FROM managed_native_events WHERE event_id = ? AND phase = ?""",
        (event_id, PRE_PHASE),
    ).fetchone()
    if root_row is None or pre_row is None or root_row["phase"] != USER_PROMPT_PHASE:
        return False
    identity = (
        event["host"],
        event["session_id"],
        event["generation_id"],
        event["request_id"],
    )
    if any(
        tuple(row[key] for key in ("host", "session_id", "generation_id", "request_id")) != identity
        for row in (root_row, pre_row)
    ):
        return False
    try:
        root_payload = json.loads(str(root_row["payload_json"]))
        pre_payload = json.loads(str(pre_row["payload_json"]))
    except json.JSONDecodeError:
        return False
    if not isinstance(root_payload, dict) or not isinstance(pre_payload, dict):
        return False
    raw_prompt_digest = root_payload.get("raw_request_digest")
    if not isinstance(raw_prompt_digest, str) or len(raw_prompt_digest) != 64:
        return False
    p0_fields = _frozen_prefix_fields(root_payload.get("prefix"))
    p1_fields = _frozen_prefix_fields(pre_payload.get("prefix"))
    if p0_fields is None or p1_fields is None:
        return False
    p0_hash, p0_length, p0_path_hash, p0_device, p0_inode = p0_fields
    p1_hash, p1_length, p1_path_hash, p1_device, p1_inode = p1_fields
    if (
        p1_length <= p0_length
        or p0_path_hash != p1_path_hash
        or p0_device != p1_device
        or p0_inode != p1_inode
    ):
        return False
    p1_raw = _read_frozen_prefix_buffer(event, p1_fields)
    if p1_raw is None:
        return False
    if (
        hashlib.sha256(p1_raw).hexdigest() != p1_hash
        or hashlib.sha256(p1_raw[:p0_length]).hexdigest() != p0_hash
    ):
        return False
    try:
        p0_history = _parse_native_history(p1_raw[:p0_length])
        p1_history = _parse_native_history(p1_raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    unresolved_history_base = {
        "history_base",
        "historyBase",
        "forked_from",
        "forkedFrom",
        "compacted_from",
        "compactedFrom",
    }
    if any(
        _contains_unresolved_history_base(record, unresolved_history_base) for record in p1_history
    ):
        return False
    if not _codex_initialization_prefix_is_verified(
        p0_history,
        session_id=str(event["session_id"]),
        cwd=Path(str(event["cwd"])),
        generation_id=str(event["generation_id"]),
    ):
        return False
    appended_history = p1_history[len(p0_history) :]
    if not _codex_appended_prompt_is_verified(
        appended_history,
        session_id=str(event["session_id"]),
        cwd=Path(str(event["cwd"])),
        generation_id=str(event["generation_id"]),
        raw_prompt_digest=raw_prompt_digest,
    ):
        return False
    if not _context_raw_prompt_matches_business_dto(context, root_payload, appended_history):
        return False
    database.execute(
        "UPDATE managed_contexts SET root_valid = 1 WHERE context_key = ?",
        (context["context_key"],),
    )
    return True


def _read_frozen_prefix_buffer(
    event: dict[str, object],
    fields: tuple[str, int, str, int, int],
) -> bytes | None:
    """Read exactly one persisted native prefix after rechecking its file identity."""

    expected_hash, expected_length, expected_path_hash, expected_device, expected_inode = fields
    try:
        transcript_path = Path(str(event["transcript_path"]))
        resolved = transcript_path.resolve(strict=True)
        path_hash = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()
        if path_hash != expected_path_hash:
            return None
        with resolved.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if opened.st_dev != expected_device or opened.st_ino != expected_inode:
                return None
            raw = stream.read(expected_length)
    except (KeyError, OSError, TypeError, ValueError):
        return None
    if len(raw) != expected_length or hashlib.sha256(raw).hexdigest() != expected_hash:
        return None
    return raw


def _parse_native_history(raw: bytes) -> list[dict[str, object]]:
    """Decode a complete frozen JSONL prefix without accepting partial records."""

    if raw and raw[-1:] != b"\n":
        raise json.JSONDecodeError("incomplete JSONL prefix", "", 0)
    records_json = [json.loads(line) for line in raw.splitlines() if line.strip()]
    if not records_json or not all(isinstance(record, dict) for record in records_json):
        raise json.JSONDecodeError("native prefix is not object JSONL", "", 0)
    return records_json


def _codex_appended_prompt_is_verified(
    history: list[dict[str, object]],
    *,
    session_id: str,
    cwd: Path,
    generation_id: str,
    raw_prompt_digest: str,
) -> bool:
    """Verify one exact current user message and the observed records after it."""

    if not history:
        return False
    first_payload = history[0].get("payload") if history[0].get("type") == "response_item" else None
    if not isinstance(first_payload, dict) or first_payload.get("type") != "message":
        return False
    first_metadata = first_payload.get("internal_chat_message_metadata_passthrough")
    first_content = first_payload.get("content")
    if (
        first_payload.get("role") != "user"
        or not isinstance(first_metadata, dict)
        or first_metadata.get("turn_id") != generation_id
        or first_metadata.get("content_item_kinds") != ["user.text"]
        or not isinstance(first_content, list)
        or len(first_content) != 1
        or not isinstance(first_content[0], dict)
        or first_content[0].get("type") != "input_text"
        or not isinstance(first_content[0].get("text"), str)
    ):
        return False
    prompt_digest = hashlib.sha256(first_content[0]["text"].encode("utf-8")).hexdigest()
    if prompt_digest != raw_prompt_digest:
        return False
    if not _codex_record_cwd_is_current(history[0], cwd):
        return False
    for record in history[1:]:
        if not _codex_record_cwd_is_current(record, cwd):
            return False
        record_type = record.get("type")
        if record_type == "event_msg":
            payload = record.get("payload")
            if (
                not isinstance(payload, dict)
                or payload.get("type") != "item_completed"
                or payload.get("thread_id") != session_id
                or payload.get("turn_id") != generation_id
            ):
                return False
            item = payload.get("item")
            if not isinstance(item, dict):
                return False
            if item.get("type") in {"UserMessage", "Reasoning"}:
                continue
            if item.get("type") == "AgentMessage" and _codex_commentary_item_is_verified(item):
                continue
            return False
        if record_type == "response_item":
            payload = record.get("payload")
            metadata = (
                payload.get("internal_chat_message_metadata_passthrough")
                if isinstance(payload, dict)
                else None
            )
            if (
                isinstance(payload, dict)
                and payload.get("type") == "message"
                and payload.get("role") == "assistant"
            ):
                if _codex_assistant_commentary_is_verified(payload, generation_id):
                    continue
                return False
            if (
                not isinstance(payload, dict)
                or payload.get("type") not in {"reasoning", "custom_tool_call"}
                or not isinstance(metadata, dict)
                or metadata.get("turn_id") != generation_id
            ):
                return False
            continue
        if record_type == "token_usage_record":
            payload = record.get("payload")
            if (
                not isinstance(payload, dict)
                or payload.get("session_id") != session_id
                or payload.get("thread_id") != session_id
                or payload.get("turn_id") != generation_id
                or payload.get("root_turn_id") != generation_id
            ):
                return False
            continue
        return False
    return True


def _context_raw_prompt_matches_business_dto(
    context: Any,
    prompt_payload: dict[str, object],
    appended_history: list[dict[str, object]],
) -> bool:
    """Reparse frozen native user text for selectorless DTOs without broad parsing."""

    if not _context_uses_automatic_selection(context):
        return True
    if (
        prompt_payload.get("business_dto_digest") != context["business_dto_digest"]
        or prompt_payload.get("business_dto_rule_version") != context["business_dto_rule_version"]
    ):
        return False
    raw_prompt = _codex_raw_prompt_from_appended_history(appended_history)
    return _business_dto_goal_digest_from_raw_prompt(raw_prompt) == context["business_dto_digest"]


def _codex_raw_prompt_from_appended_history(history: list[dict[str, object]]) -> str:
    """Read only the already-validated first current user record."""

    if not history:
        return ""
    first = history[0]
    payload = first.get("payload") if first.get("type") == "response_item" else None
    if not isinstance(payload, dict):
        return ""
    content = payload.get("content")
    if (
        not isinstance(content, list)
        or len(content) != 1
        or not isinstance(content[0], dict)
        or not isinstance(content[0].get("text"), str)
    ):
        return ""
    return content[0]["text"]


def _codex_commentary_item_is_verified(item: dict[str, object]) -> bool:
    """Accept the observed AgentMessage commentary shape without reading its text."""

    return item.get("phase") == "commentary" and _codex_commentary_content_is_verified(
        item.get("content"), "Text"
    )


def _codex_assistant_commentary_is_verified(payload: dict[str, object], generation_id: str) -> bool:
    """Bind an assistant commentary response to the current turn, not its prose."""

    metadata = payload.get("internal_chat_message_metadata_passthrough")
    return (
        payload.get("phase") == "commentary"
        and payload.get("status") == "completed"
        and isinstance(metadata, dict)
        and metadata.get("turn_id") == generation_id
        and metadata.get("content_item_kinds") == ["unknown"]
        and _codex_commentary_content_is_verified(payload.get("content"), "output_text")
    )


def _codex_commentary_content_is_verified(content: object, content_type: str) -> bool:
    """Validate only the finite serializer shape; commentary text has no semantics here."""

    return (
        isinstance(content, list)
        and len(content) == 1
        and isinstance(content[0], dict)
        and content[0].get("type") == content_type
        and isinstance(content[0].get("text"), str)
    )


def _codex_record_cwd_is_current(record: object, cwd: Path) -> bool:
    """Reject any later explicit workspace claim that disagrees with P0."""

    expected = str(cwd)
    if isinstance(record, dict):
        for key, value in record.items():
            if key == "cwd" and value != expected:
                return False
            if not _codex_record_cwd_is_current(value, cwd):
                return False
    elif isinstance(record, list):
        return all(_codex_record_cwd_is_current(value, cwd) for value in record)
    return True


def _close_execution_activity(database: Any, event: dict[str, object]) -> bool:
    """Close a request only when its executions and operations are terminal.

    A Stop callback records a native cutoff, not proof that a child process has
    exited.  Keep the call active while a durable execution remains live or
    ambiguous.  Recovery retries this exact operation after it has proved the
    execution group terminated.
    """

    context = database.execute(
        "SELECT task_id FROM managed_contexts WHERE context_key = ?", (_context_key(event),)
    ).fetchone()
    if context is None or context["task_id"] is None:
        return False
    task_id = str(context["task_id"])
    call_rows = database.execute(
        """SELECT DISTINCT call_id FROM managed_native_events
        WHERE suite_id = ? AND host = ? AND session_id = ? AND generation_id = ?
          AND request_id = ? AND call_id IS NOT NULL""",
        (
            event["suite_id"],
            event["host"],
            event["session_id"],
            event["generation_id"],
            event["request_id"],
        ),
    ).fetchall()
    call_ids = {str(row["call_id"]) for row in call_rows}
    if not call_ids:
        return False
    placeholders = ", ".join("?" for _ in call_ids)
    action_rows = database.execute(
        f"""SELECT action_run_id, status FROM managed_actions
        WHERE call_id IN ({placeholders})""",
        sorted(call_ids),
    ).fetchall()
    if any(str(row["status"]) == "reserved" for row in action_rows):
        return False

    action_run_ids = [str(row["action_run_id"]) for row in action_rows]
    try:
        assert_no_live_execution(
            database,
            str(event["suite_id"]),
            action_run_ids=action_run_ids,
        )
    except SelcError as error:
        if error.code not in {
            "MANAGED_EXECUTION_ACTIVE",
            "MANAGED_EXECUTION_UNCERTAIN",
            "EXECUTION_IDENTITY_UNAVAILABLE",
        }:
            raise
        return False

    inflight_publication = database.execute(
        f"""SELECT 1 FROM managed_publications
        WHERE call_id IN ({placeholders})
          AND status IN ('reserved', 'prepared', 'supplied') LIMIT 1""",
        sorted(call_ids),
    ).fetchone()
    if inflight_publication is not None:
        return False

    finished_action_ids = [
        str(row["action_run_id"]) for row in action_rows if str(row["status"]) == "finished"
    ]
    if finished_action_ids:
        finished_placeholders = ", ".join("?" for _ in finished_action_ids)
        database.execute(
            f"""UPDATE managed_actions AS action
            SET status = 'fenced', finished_at = ?
            WHERE action.action_run_id IN ({finished_placeholders})
              AND action.status = 'finished'
              AND NOT EXISTS (
                  SELECT 1 FROM managed_publications AS publication
                  WHERE publication.action_run_id = action.action_run_id
              )""",
            [records.utc_now(), *finished_action_ids],
        )

    database.execute(
        f"UPDATE calls SET status = 'closed' WHERE status = 'active' AND call_id IN ({placeholders})",
        sorted(call_ids),
    )
    reduce_managed_assessment(database, task_id)
    return True


def _close_idle_managed_task(database: Any, event: dict[str, object]) -> None:
    """Release task occupancy only after every execution fact is terminal."""

    context = database.execute(
        "SELECT task_id FROM managed_contexts WHERE context_key = ?", (_context_key(event),)
    ).fetchone()
    if context is None or context["task_id"] is None:
        return
    task_id = str(context["task_id"])
    active_calls = database.execute(
        "SELECT 1 FROM calls WHERE task_id = ? AND status = 'active' LIMIT 1", (task_id,)
    ).fetchone()
    inflight_action = database.execute(
        """SELECT 1 FROM managed_actions WHERE task_id = ?
        AND status IN ('reserved', 'finished') LIMIT 1""",
        (task_id,),
    ).fetchone()
    inflight_publication = database.execute(
        """SELECT 1 FROM managed_publications WHERE task_id = ?
        AND status IN ('reserved', 'prepared', 'supplied') LIMIT 1""",
        (task_id,),
    ).fetchone()
    active_execution = database.execute(
        """SELECT 1 FROM managed_executions AS execution
        JOIN managed_actions AS action ON action.action_run_id = execution.action_run_id
        WHERE action.task_id = ? AND execution.state = 'running' LIMIT 1""",
        (task_id,),
    ).fetchone()
    if any((active_calls, inflight_action, inflight_publication, active_execution)):
        return
    database.execute(
        "UPDATE sessions SET status = 'closed', closed_at = ? WHERE session_id = ? AND status = 'active'",
        (records.utc_now(), f"session:{task_id}"),
    )
    database.execute(
        "UPDATE tasks SET status = 'closed' WHERE task_id = ? AND status = 'active'", (task_id,)
    )
    reduce_managed_assessment(database, task_id)


def _reconcile_stopped_execution_activity(database: Any, suite_id: str) -> None:
    """Finish persisted Stops after recovery verifies their process groups ended."""

    stop_rows = database.execute(
        """SELECT suite_id, host, session_id, generation_id, request_id
        FROM managed_native_events WHERE suite_id = ? AND phase = ?
        GROUP BY suite_id, host, session_id, generation_id, request_id""",
        (suite_id, STOP_PHASE),
    ).fetchall()
    for row in stop_rows:
        event = {key: str(value) for key, value in dict(row).items()}
        _close_execution_activity(database, event)
        _close_idle_managed_task(database, event)
        _close_correction_binding(database, event)


def _record_permission_context(database: Any, event: dict[str, object]) -> None:
    """Attach a technical permission request without inferring a human outcome."""

    context = database.execute(
        "SELECT task_id FROM managed_contexts WHERE context_key = ?", (_context_key(event),)
    ).fetchone()
    if context is None or context["task_id"] is None:
        return
    # The native row itself is the record.  A request is neither an allow nor a
    # denial, and no caller, actor, or retry is invented from its presence.
    reduce_managed_assessment(database, str(context["task_id"]))


def _context_correction_declaration(database: Any, context: Any) -> dict[str, object] | None:
    """Read the prompt-side control without accepting a tool-side substitute."""

    row = database.execute(
        "SELECT payload_json FROM managed_native_events WHERE event_id = ?",
        (context["root_event_id"],),
    ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(str(row["payload_json"]))
    except json.JSONDecodeError:
        return {"valid": False}
    if not isinstance(payload, dict) or "correction" not in payload:
        return None
    value = payload["correction"]
    if value is None:
        return None
    if not isinstance(value, dict):
        return {"valid": False}
    return value


def _ensure_current_request_anchor(
    database: Any,
    context: Any,
    event: dict[str, object],
    event_id: str,
) -> bool:
    """Persist the first valid current-turn Pre proof without promoting a root.

    A correction may be a later turn in a session that already contains managed
    business.  Its P0/P1 pair proves only this new request, so ``root_valid``
    deliberately remains false.  Replays and later Pre callbacks reuse the
    first durable proof rather than rescanning a growing transcript.
    """

    technical_skill_pre = event.get("technical_skill_pre") is True
    if technical_skill_pre and _native_request_is_terminated(
        database,
        suite_id=str(event["suite_id"]),
        host=str(event["host"]),
        session_id=str(event["session_id"]),
        generation_id=str(event["generation_id"]),
        request_id=str(event["request_id"]),
        tool_use_id=str(event["tool_use_id"]),
    ):
        return False
    existing = database.execute(
        """SELECT p0_event_id, pre_event_id, rule_version
        FROM managed_request_anchors WHERE context_key = ?""",
        (context["context_key"],),
    ).fetchone()
    if existing is not None:
        return _durable_current_request_anchor_is_verified(database, context, event, existing)
    if event["host"] not in {"codex", "workbuddy"} or event["fixture"]:
        return False
    if not technical_skill_pre and not event["request_from_prompt"]:
        return False
    if technical_skill_pre and (
        _has_prior_workbuddy_skill_pre(database, context, event_id)
        or _request_has_prior_tool_boundary(database, context, event_id)
    ):
        return False
    prompt_row = database.execute(
        """SELECT payload_json, host, session_id, generation_id, request_id
        FROM managed_native_events WHERE event_id = ? AND phase = ?""",
        (context["root_event_id"], USER_PROMPT_PHASE),
    ).fetchone()
    if prompt_row is None:
        return False
    identity = (
        event["host"],
        event["session_id"],
        event["generation_id"],
        event["request_id"],
    )
    if (
        tuple(prompt_row[key] for key in ("host", "session_id", "generation_id", "request_id"))
        != identity
    ):
        return False
    try:
        prompt_payload = json.loads(str(prompt_row["payload_json"]))
    except json.JSONDecodeError:
        return False
    if not isinstance(prompt_payload, dict):
        return False
    raw_prompt_digest = prompt_payload.get("raw_request_digest")
    p0_fields = _frozen_prefix_fields(prompt_payload.get("prefix"))
    p1_fields = _frozen_prefix_fields(event.get("prefix"))
    if not isinstance(raw_prompt_digest, str) or len(raw_prompt_digest) != 64:
        return False
    if p0_fields is None or p1_fields is None:
        return False
    p0_hash, p0_length, p0_path_hash, p0_device, p0_inode = p0_fields
    p1_hash, p1_length, p1_path_hash, p1_device, p1_inode = p1_fields
    if (
        p1_length <= p0_length
        or p0_path_hash != p1_path_hash
        or p0_device != p1_device
        or p0_inode != p1_inode
    ):
        return False
    p1_raw = _read_frozen_prefix_buffer(event, p1_fields)
    if p1_raw is None or hashlib.sha256(p1_raw).hexdigest() != p1_hash:
        return False
    if hashlib.sha256(p1_raw[:p0_length]).hexdigest() != p0_hash:
        return False
    try:
        p0_history = _parse_native_history(p1_raw[:p0_length])
        p1_history = _parse_native_history(p1_raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    unresolved_history_base = {
        "history_base",
        "historyBase",
        "forked_from",
        "forkedFrom",
        "compacted_from",
        "compactedFrom",
    }
    if any(
        _contains_unresolved_history_base(record, unresolved_history_base) for record in p1_history
    ):
        return False
    appended_history = p1_history[len(p0_history) :]
    cwd = Path(str(event["cwd"]))
    if event["host"] == "codex":
        anchor_rule_version = _codex_current_turn_anchor_rule_version(
            p0_history,
            session_id=str(event["session_id"]),
            cwd=cwd,
            generation_id=str(event["generation_id"]),
            prompt_version=prompt_payload.get("client_version"),
            pre_version=event.get("client_version"),
        )
        current_request_valid = (
            anchor_rule_version is not None
            and _codex_appended_prompt_is_verified(
                appended_history,
                session_id=str(event["session_id"]),
                cwd=cwd,
                generation_id=str(event["generation_id"]),
                raw_prompt_digest=raw_prompt_digest,
            )
        )
    elif technical_skill_pre:
        current_request_valid = _workbuddy_skill_pre_request_is_verified(
            p0_history,
            appended_history,
            session_id=str(event["session_id"]),
            cwd=cwd,
            request_id=str(event["request_id"]),
            raw_prompt_digest=raw_prompt_digest,
            prompt_payload=prompt_payload,
            event=event,
        )
        anchor_rule_version = _workbuddy_anchor_rule_version(prompt_payload, current_request_valid)
    else:
        current_request_valid = _workbuddy_current_request_is_verified(
            p0_history,
            appended_history,
            session_id=str(event["session_id"]),
            cwd=cwd,
            request_id=str(event["request_id"]),
            raw_prompt_digest=raw_prompt_digest,
        )
        anchor_rule_version = CURRENT_REQUEST_ANCHOR_RULE_VERSION if current_request_valid else None
    if not current_request_valid:
        return False
    if event["host"] == "codex" and not _context_raw_prompt_matches_business_dto(
        context,
        prompt_payload,
        appended_history,
    ):
        return False
    assert anchor_rule_version is not None
    database.execute(
        """INSERT INTO managed_request_anchors(
        context_key, p0_event_id, pre_event_id, rule_version, created_at)
        VALUES (?, ?, ?, ?, ?)""",
        (
            context["context_key"],
            context["root_event_id"],
            event_id,
            anchor_rule_version,
            records.utc_now(),
        ),
    )
    if _context_uses_automatic_selection(context) and technical_skill_pre:
        database.execute(
            """UPDATE managed_contexts SET selected_skill = ?
            WHERE context_key = ? AND selected_skill IS NULL""",
            (event["skill"], context["context_key"]),
        )
    return True


def _current_request_anchor_rule_is_supported(host: str, rule_version: object) -> bool:
    """Accept durable request proofs only under their original host rule."""

    if rule_version == CURRENT_REQUEST_ANCHOR_RULE_VERSION:
        return host in {"codex", "workbuddy"}
    if rule_version == CODEX_ALPHA11_ARG0_ANCHOR_RULE_VERSION:
        return host == "codex"
    return host == "workbuddy" and rule_version in {
        WORKBUDDY_SKILL_PRE_ANCHOR_RULE_VERSION,
        WORKBUDDY_AUTOMATIC_SKILL_PRE_ANCHOR_RULE_VERSION,
    }


def _has_prior_workbuddy_skill_pre(database: Any, context: Any, event_id: str) -> bool:
    """Fence the first frozen Skill Pre against a later callback's bytes."""

    rows = database.execute(
        """SELECT event_id, payload_json FROM managed_native_events
        WHERE suite_id = ? AND host = ? AND session_id = ? AND generation_id = ?
          AND request_id = ? AND phase = ? AND event_id != ?""",
        (
            context["suite_id"],
            context["host"],
            context["session_id"],
            context["generation_id"],
            context["request_id"],
            PRE_PHASE,
            event_id,
        ),
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(str(row["payload_json"]))
        except json.JSONDecodeError:
            return True
        if isinstance(payload, dict) and payload.get("technical_skill_event") is True:
            return True
    return False


def _request_has_prior_tool_boundary(database: Any, context: Any, event_id: str) -> bool:
    """Do not establish a first technical anchor after tool activity began.

    The current event is already durable when this runs.  Every other matching
    Pre or Post row was therefore observed earlier and makes a later Skill
    proof unusable, regardless of its tool identifier.
    """

    row = database.execute(
        """SELECT 1 FROM managed_native_events
        WHERE suite_id = ? AND host = ? AND session_id = ? AND generation_id = ?
          AND request_id = ? AND event_id != ? AND phase IN (?, ?)
        LIMIT 1""",
        (
            context["suite_id"],
            context["host"],
            context["session_id"],
            context["generation_id"],
            context["request_id"],
            event_id,
            PRE_PHASE,
            POST_PHASE,
        ),
    ).fetchone()
    return row is not None


def _durable_current_request_anchor_is_verified(
    database: Any,
    context: Any,
    event: dict[str, object],
    anchor: Any,
) -> bool:
    """Validate the immutable native facts referenced by one persisted anchor.

    Reuse never rescans a transcript that may have grown.  It only checks the
    recorded prompt and Pre identities that originally established this exact
    context and rule.
    """

    pre_event_id = anchor["pre_event_id"]
    p0_event_id = anchor["p0_event_id"]
    rule_version = anchor["rule_version"]
    if (
        not isinstance(pre_event_id, str)
        or not pre_event_id
        or not isinstance(p0_event_id, str)
        or p0_event_id != context["root_event_id"]
        or not _current_request_anchor_rule_is_supported(str(context["host"]), rule_version)
    ):
        return False
    if any(
        event[key] != context[key]
        for key in ("suite_id", "host", "session_id", "generation_id", "request_id")
    ):
        return False
    rows = database.execute(
        """SELECT event_id, payload_json, suite_id, host, session_id, generation_id,
                  request_id, phase
        FROM managed_native_events
        WHERE event_id IN (?, ?)""",
        (p0_event_id, pre_event_id),
    ).fetchall()
    if len(rows) != 2:
        return False
    rows_by_id = {row["event_id"]: row for row in rows}
    prompt_row = rows_by_id.get(p0_event_id)
    pre_row = rows_by_id.get(pre_event_id)
    if prompt_row is None or pre_row is None:
        return False
    expected_scope = tuple(
        context[key] for key in ("suite_id", "host", "session_id", "generation_id", "request_id")
    )
    for row in (prompt_row, pre_row):
        row_scope = tuple(
            row[key] for key in ("suite_id", "host", "session_id", "generation_id", "request_id")
        )
        if row_scope != expected_scope:
            return False
    if prompt_row["phase"] != USER_PROMPT_PHASE or pre_row["phase"] != PRE_PHASE:
        return False
    try:
        prompt_payload = json.loads(str(prompt_row["payload_json"]))
        pre_payload = json.loads(str(pre_row["payload_json"]))
    except json.JSONDecodeError:
        return False
    if (
        not isinstance(prompt_payload, dict)
        or not isinstance(pre_payload, dict)
        or prompt_payload.get("source") != "native-collector"
        or pre_payload.get("source") != "native-collector"
    ):
        return False
    if rule_version in {
        WORKBUDDY_SKILL_PRE_ANCHOR_RULE_VERSION,
        WORKBUDDY_AUTOMATIC_SKILL_PRE_ANCHOR_RULE_VERSION,
    }:
        workbuddy_boundary_is_valid = (
            context["host"] == "workbuddy"
            and _workbuddy_client_versions_are_supported_and_equal(
                prompt_payload.get("client_version"),
                pre_payload.get("client_version"),
                event.get("client_version"),
            )
            and pre_payload.get("action") == "technical_skill"
            and pre_payload.get("technical_skill_event") is True
            and pre_payload.get("technical_skill_pre") is True
            and pre_payload.get("technical_skill_valid") is True
        )
        if not workbuddy_boundary_is_valid:
            return False
        if rule_version == WORKBUDDY_AUTOMATIC_SKILL_PRE_ANCHOR_RULE_VERSION:
            return (
                _context_uses_automatic_selection(context)
                and prompt_payload.get("skill") == ""
                and prompt_payload.get("business_dto_digest") == context["business_dto_digest"]
                and prompt_payload.get("business_dto_rule_version")
                == context["business_dto_rule_version"]
                and pre_payload.get("skill") == context["selected_skill"]
            )
        return (
            context["input_skill"]
            and prompt_payload.get("skill") == context["input_skill"]
            and pre_payload.get("skill") == context["input_skill"]
        )
    return (
        pre_payload.get("technical_skill_event") is not True
        and pre_payload.get("request_from_prompt") is True
    )


def _context_has_unusable_technical_skill_pre(
    database: Any, context: Any, event: dict[str, object]
) -> bool:
    """Fence every business receipt after an unusable first Skill boundary."""

    rows = database.execute(
        """SELECT event_id, payload_json FROM managed_native_events
        WHERE suite_id = ? AND host = ? AND session_id = ? AND generation_id = ?
          AND request_id = ? AND phase = ?
        ORDER BY rowid""",
        (
            context["suite_id"],
            context["host"],
            context["session_id"],
            context["generation_id"],
            context["request_id"],
            PRE_PHASE,
        ),
    ).fetchall()
    first_skill_event_id: str | None = None
    for row in rows:
        try:
            payload = json.loads(str(row["payload_json"]))
        except json.JSONDecodeError:
            return True
        if isinstance(payload, dict) and payload.get("technical_skill_event") is True:
            first_skill_event_id = str(row["event_id"])
            break
    if first_skill_event_id is None:
        return False
    anchor = database.execute(
        """SELECT p0_event_id, pre_event_id, rule_version
        FROM managed_request_anchors WHERE context_key = ?""",
        (context["context_key"],),
    ).fetchone()
    return (
        anchor is None
        or anchor["pre_event_id"] != first_skill_event_id
        or not _durable_current_request_anchor_is_verified(database, context, event, anchor)
    )


def _workbuddy_current_request_is_verified(
    p0_history: list[dict[str, object]],
    appended_history: list[dict[str, object]],
    *,
    session_id: str,
    cwd: Path,
    request_id: str,
    raw_prompt_digest: str,
) -> bool:
    """Bind one WorkBuddy user message to its callback without promoting a root.

    A WorkBuddy correction follows existing session history, so P0 can contain
    earlier business records.  P0 proves only the frozen native path/session
    scope; P1 must add exactly the observed user-message serializer form whose
    text bytes match the callback's original raw prompt digest.
    """

    if not p0_history or len(appended_history) != 1:
        return False
    if not all(
        _workbuddy_history_record_matches_scope(record, session_id, cwd) for record in p0_history
    ):
        return False
    record = appended_history[0]
    if not _workbuddy_history_record_matches_scope(record, session_id, cwd):
        return False
    if record.get("type") != "message" or record.get("role") != "user":
        return False
    content = record.get("content")
    if (
        not isinstance(content, list)
        or len(content) != 1
        or not isinstance(content[0], dict)
        or set(content[0]) != {"type", "text"}
        or content[0].get("type") != "input_text"
        or not isinstance(content[0].get("text"), str)
    ):
        return False
    provider = record.get("providerData")
    if (
        not isinstance(provider, dict)
        or provider.get("startsNewUserRequest") is not True
        or provider.get("conversationRequestId") != request_id
    ):
        return False
    return hashlib.sha256(content[0]["text"].encode("utf-8")).hexdigest() == raw_prompt_digest


def _workbuddy_client_versions_are_supported_and_equal(*versions: object) -> bool:
    """Require one explicit WorkBuddy release across a durable request boundary."""

    if not versions or any(not isinstance(version, str) for version in versions):
        return False
    if any(version not in WORKBUDDY_SUPPORTED_CLIENT_VERSIONS for version in versions):
        return False
    return len(set(versions)) == 1


def _context_uses_automatic_selection(context: Any) -> bool:
    """Recognize only a newly persisted selectorless, structured request."""

    return (
        context["input_skill"] == ""
        and isinstance(context["business_dto_digest"], str)
        and _is_sha256_digest(context["business_dto_digest"])
        and context["business_dto_rule_version"] == BUSINESS_DTO_RULE_VERSION
    )


def _workbuddy_anchor_rule_version(
    prompt_payload: dict[str, object], current_request_valid: bool
) -> str | None:
    """Keep explicit and selectorless Skill Pre proofs independently versioned."""

    if not current_request_valid:
        return None
    if prompt_payload.get("skill") == "":
        if (
            isinstance(prompt_payload.get("business_dto_digest"), str)
            and prompt_payload.get("business_dto_rule_version") == BUSINESS_DTO_RULE_VERSION
        ):
            return WORKBUDDY_AUTOMATIC_SKILL_PRE_ANCHOR_RULE_VERSION
        return None
    return WORKBUDDY_SKILL_PRE_ANCHOR_RULE_VERSION


def _workbuddy_skill_pre_request_is_verified(
    p0_history: list[dict[str, object]],
    appended_history: list[dict[str, object]],
    *,
    session_id: str,
    cwd: Path,
    request_id: str,
    raw_prompt_digest: str,
    prompt_payload: dict[str, object],
    event: dict[str, object],
) -> bool:
    """Verify the finite pending-``Skill`` serializer for supported WorkBuddy versions.

    This rule proves that the current user request reached a pending native
    Skill call.  It deliberately does not infer an executable path, a Skill
    result, or a discovery outcome.
    """

    event_version = event.get("client_version")
    prompt_version = prompt_payload.get("client_version")
    if (
        event.get("host") != "workbuddy"
        or event.get("surface") != "WorkBuddy"
        or prompt_payload.get("surface") != "WorkBuddy"
        or not _workbuddy_client_versions_are_supported_and_equal(
            prompt_version,
            event_version,
        )
        or event.get("technical_skill_event") is not True
        or event.get("technical_skill_pre") is not True
        or event.get("technical_skill_valid") is not True
        or event.get("session_id") != session_id
        or event.get("request_id") != request_id
        or event.get("cwd") != str(cwd)
    ):
        return False
    prompt_skill = prompt_payload.get("skill")
    prompt_request_digest = prompt_payload.get("request_digest")
    prompt_correction = prompt_payload.get("correction")
    prompt_is_automatic = prompt_skill == ""
    if not isinstance(prompt_skill, str):
        return False
    if prompt_is_automatic:
        if (
            not isinstance(prompt_payload.get("business_dto_digest"), str)
            or prompt_payload.get("business_dto_rule_version") != BUSINESS_DTO_RULE_VERSION
            or not isinstance(event.get("skill"), str)
            or not event["skill"]
        ):
            return False
    elif (
        not prompt_skill
        or event.get("skill") != prompt_skill
        or event.get("request_digest") != prompt_request_digest
        or event.get("correction") != prompt_correction
    ):
        return False
    expected_args_digest = event.get("technical_skill_args_digest")
    if (
        not isinstance(expected_args_digest, str)
        or not _is_sha256_digest(expected_args_digest)
        or len(appended_history) not in {4, 5, 6}
        or not p0_history
        or not all(
            _workbuddy_history_record_matches_scope(record, session_id, cwd)
            for record in p0_history
        )
    ):
        return False
    user_record = appended_history[0]
    snapshot_record = appended_history[1]
    current_index = 2
    title_record: dict[str, object] | None = None
    candidate_title = appended_history[current_index]
    if isinstance(candidate_title, dict) and candidate_title.get("type") == "ai-title":
        title_record = candidate_title
        current_index += 1
    if current_index >= len(appended_history):
        return False
    reasoning_record = appended_history[current_index]
    current_index += 1

    assistant_record: dict[str, object] | None = None
    if current_index < len(appended_history):
        candidate_assistant = appended_history[current_index]
        if isinstance(candidate_assistant, dict) and candidate_assistant.get("type") == "message":
            assistant_record = candidate_assistant
            current_index += 1
    if current_index != len(appended_history) - 1:
        return False
    call_record = appended_history[current_index]
    if not _workbuddy_user_message_is_verified(
        user_record,
        session_id=session_id,
        cwd=cwd,
        request_id=request_id,
        raw_prompt_digest=raw_prompt_digest,
    ):
        return False
    user_id = user_record.get("id")
    if (
        not isinstance(user_id, str)
        or not user_id
        or not _workbuddy_record_has_explicit_scope(user_record, session_id, cwd)
    ):
        return False
    if _workbuddy_initial_metadata_history_is_verified(p0_history, session_id, cwd):
        if "parentId" in user_record:
            return False
    else:
        p0_tail_id = p0_history[-1].get("id")
        if (
            not isinstance(p0_tail_id, str)
            or not p0_tail_id
            or user_record.get("parentId") != p0_tail_id
        ):
            return False
    callback_message_ids = (prompt_payload.get("message_id"), event.get("message_id"))
    if any(
        message_id not in {None, ""} and message_id != user_id
        for message_id in callback_message_ids
    ):
        return False
    if not _workbuddy_file_history_snapshot_is_verified(
        snapshot_record,
        user_id=user_id,
        cwd=cwd,
    ):
        return False
    if title_record is not None and not _workbuddy_ai_title_is_verified(
        title_record, session_id, cwd
    ):
        return False
    if not isinstance(reasoning_record, dict) or not _workbuddy_record_has_explicit_scope(
        reasoning_record, session_id, cwd
    ):
        return False
    reasoning_id = reasoning_record.get("id")
    reasoning_identity = _workbuddy_model_identity(reasoning_record, request_id)
    reasoning_message_id = reasoning_identity[0] if reasoning_identity is not None else None
    if (
        reasoning_record.get("type") != "reasoning"
        or not isinstance(reasoning_id, str)
        or not reasoning_id
        or reasoning_id == user_id
        or reasoning_record.get("parentId") != user_id
        or reasoning_message_id is None
        or reasoning_message_id == user_id
    ):
        return False
    assistant_id: str | None = None
    reasoning_step_sequence = _workbuddy_model_step_sequence(reasoning_record)
    if assistant_record is not None:
        assistant_id = _workbuddy_assistant_text_is_verified(
            assistant_record,
            session_id=session_id,
            cwd=cwd,
            request_id=request_id,
            reasoning_id=reasoning_id,
            reasoning_identity=reasoning_identity,
            reasoning_step_sequence=reasoning_step_sequence,
        )
        if assistant_id is None:
            return False

    if not isinstance(call_record, dict) or not _workbuddy_record_has_explicit_scope(
        call_record, session_id, cwd
    ):
        return False
    call_id = call_record.get("id")
    call_tool_id = call_record.get("callId")
    call_identity = _workbuddy_model_identity(call_record, request_id)
    call_message_id = call_identity[0] if call_identity is not None else None
    call_parent_id = assistant_id or reasoning_id
    call_matches_model_turn = call_identity == reasoning_identity
    if assistant_id is not None:
        call_matches_model_turn = (
            call_matches_model_turn
            and reasoning_step_sequence is not None
            and _workbuddy_model_step_sequence(call_record) == reasoning_step_sequence
        )
    if (
        call_record.get("type") != "function_call"
        or call_record.get("name") != "Skill"
        or not isinstance(call_id, str)
        or not call_id
        or call_id in {user_id, reasoning_id, assistant_id}
        or not isinstance(call_tool_id, str)
        or not call_tool_id
        or call_tool_id != event.get("tool_use_id")
        or call_record.get("parentId") != call_parent_id
        or "status" in call_record
        or call_message_id is None
        or not call_matches_model_turn
    ):
        return False
    current_records = [user_record, reasoning_record, call_record]
    if title_record is not None:
        current_records.append(title_record)
    if assistant_record is not None:
        current_records.append(assistant_record)
    if not _workbuddy_native_ids_are_unique(p0_history, current_records):
        return False
    if _workbuddy_p0_call_id_is_reused(p0_history, call_tool_id):
        return False
    if prompt_is_automatic:
        raw_prompt = _workbuddy_user_raw_prompt(user_record)
        if raw_prompt is None:
            return False
        expected_business_digest = prompt_payload["business_dto_digest"]
        if _business_dto_goal_digest_from_raw_prompt(raw_prompt) != expected_business_digest:
            return False
        return _workbuddy_skill_call_matches_business_dto(
            call_record,
            skill=str(event["skill"]),
            business_dto_digest=expected_business_digest,
            correction=prompt_correction,
            args_digest=expected_args_digest,
        )
    return _workbuddy_skill_call_matches_request(
        call_record,
        skill=prompt_skill,
        request_digest=str(prompt_request_digest),
        correction=prompt_correction,
        args_digest=expected_args_digest,
    )


def _workbuddy_user_message_is_verified(
    record: object,
    *,
    session_id: str,
    cwd: Path,
    request_id: str,
    raw_prompt_digest: str,
    message_id: str | None = None,
) -> bool:
    """Bind one current WorkBuddy user record to the callback's raw prompt."""

    if not _workbuddy_history_record_matches_scope(record, session_id, cwd):
        return False
    if (
        not isinstance(record, dict)
        or record.get("type") != "message"
        or record.get("role") != "user"
    ):
        return False
    if message_id is not None and record.get("id") != message_id:
        return False
    content = record.get("content")
    if (
        not isinstance(content, list)
        or len(content) != 1
        or not isinstance(content[0], dict)
        or set(content[0]) != {"type", "text"}
        or content[0].get("type") != "input_text"
        or not isinstance(content[0].get("text"), str)
    ):
        return False
    provider = record.get("providerData")
    if (
        not isinstance(provider, dict)
        or provider.get("startsNewUserRequest") is not True
        or provider.get("conversationRequestId") != request_id
    ):
        return False
    raw_prompt = _workbuddy_user_raw_prompt(record)
    return (
        raw_prompt is not None
        and hashlib.sha256(raw_prompt.encode("utf-8")).hexdigest() == raw_prompt_digest
    )


def _workbuddy_user_raw_prompt(record: object) -> str | None:
    """Extract the raw prompt only from the exact observed hidden-context envelope."""

    if not isinstance(record, dict):
        return None
    content = record.get("content")
    if (
        not isinstance(content, list)
        or len(content) != 1
        or not isinstance(content[0], dict)
        or content[0].get("type") != "input_text"
        or not isinstance(content[0].get("text"), str)
    ):
        return None
    return _workbuddy_user_envelope_raw_prompt(content[0]["text"])


def _workbuddy_user_envelope_matches_raw_prompt(text: str, raw_prompt_digest: str) -> bool:
    """Verify the one observed WorkBuddy hidden-context envelope without decoding it."""

    raw_prompt = _workbuddy_user_envelope_raw_prompt(text)
    return (
        raw_prompt is not None
        and hashlib.sha256(raw_prompt.encode("utf-8")).hexdigest() == raw_prompt_digest
    )


def _workbuddy_user_envelope_raw_prompt(text: str) -> str | None:
    """Decode no data: return only the final raw user-query segment when exact."""

    context_open = '<system-reminder data-role="user-context">\n'
    additional_open = '\n</system-reminder>\n<system-reminder data-role="additional-data">\n'
    query_open = "\n</system-reminder>\n<user_query>"
    query_close = "</user_query>"
    if not text.startswith(context_open) or not text.endswith(query_close):
        return None
    remaining = text[len(context_open) : -len(query_close)]
    if remaining.count(additional_open) != 1:
        return None
    opaque_context, remaining = remaining.split(additional_open, 1)
    if remaining.count(query_open) != 1:
        return None
    opaque_additional, raw_prompt = remaining.split(query_open, 1)
    structural_markers = (
        "<system-reminder",
        "</system-reminder",
        "<user_query",
        "</user_query",
    )
    if any(
        marker in value
        for value in (opaque_context, opaque_additional, raw_prompt)
        for marker in structural_markers
    ):
        return None
    return raw_prompt


def _workbuddy_initial_metadata_history_is_verified(
    history: list[dict[str, object]], session_id: str, cwd: Path
) -> bool:
    """Recognize only the existing metadata-only initial WorkBuddy profile."""

    if not history or not all(record.get("type") == "session-meta" for record in history):
        return False
    if not all(
        _workbuddy_history_record_matches_scope(record, session_id, cwd) for record in history
    ):
        return False
    try:
        prefix = b"".join(
            (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
            for record in history
        )
    except (TypeError, ValueError):
        return False
    return _metadata_only_prefix(
        prefix,
        host="workbuddy",
        session_id=session_id,
        cwd=cwd,
    )


def _workbuddy_assistant_text_is_verified(
    record: dict[str, object],
    *,
    session_id: str,
    cwd: Path,
    request_id: str,
    reasoning_id: str,
    reasoning_identity: tuple[str, str],
    reasoning_step_sequence: int | None,
) -> str | None:
    """Verify the one observed completed assistant text record between model and Skill."""

    expected_fields = {
        "_meta",
        "content",
        "cwd",
        "id",
        "parentId",
        "providerData",
        "role",
        "sessionId",
        "status",
        "timestamp",
        "type",
    }
    if set(record) != expected_fields:
        return None
    identifier = record.get("id")
    timestamp = record.get("timestamp")
    metadata = record.get("_meta")
    provider = record.get("providerData")
    content = record.get("content")
    expected_provider_fields = {
        "agent",
        "conversationRequestId",
        "messageId",
        "model",
        "requestModelId",
        "requestModelName",
        "stepSeq",
        "traceId",
    }
    string_provider_fields = expected_provider_fields - {"stepSeq"}
    if (
        not isinstance(identifier, str)
        or not identifier
        or record.get("type") != "message"
        or record.get("role") != "assistant"
        or record.get("status") != "completed"
        or record.get("parentId") != reasoning_id
        or record.get("sessionId") != session_id
        or record.get("cwd") != str(cwd)
        or type(timestamp) is not int
        or not isinstance(metadata, dict)
        or set(metadata) != {"baggage", "traceparent"}
        or not all(isinstance(value, str) and value for value in metadata.values())
        or not isinstance(provider, dict)
        or set(provider) != expected_provider_fields
        or not all(
            isinstance(provider.get(field), str) and provider[field]
            for field in string_provider_fields
        )
        or type(provider.get("stepSeq")) is not int
        or not isinstance(content, list)
        or len(content) != 1
        or not isinstance(content[0], dict)
        or set(content[0]) != {"providerData", "text", "type"}
        or content[0].get("type") != "output_text"
        or not isinstance(content[0].get("text"), str)
    ):
        return None
    content_provider = content[0].get("providerData")
    if (
        not isinstance(content_provider, dict)
        or set(content_provider) != {"annotations"}
        or content_provider.get("annotations") != []
        or _workbuddy_model_identity(record, request_id) != reasoning_identity
        or reasoning_step_sequence is None
        or _workbuddy_model_step_sequence(record) != reasoning_step_sequence
    ):
        return None
    return identifier


def _workbuddy_file_history_snapshot_is_verified(
    record: object,
    *,
    user_id: str,
    cwd: Path,
) -> bool:
    """Verify the observed snapshot form without treating it as a business event."""

    if not isinstance(record, dict):
        return False
    expected_fields = {
        "_meta",
        "cwd",
        "isSnapshotUpdate",
        "messageId",
        "snapshot",
        "timestamp",
        "type",
    }
    if set(record) != expected_fields:
        return False
    timestamp = record.get("timestamp")
    metadata = record.get("_meta")
    snapshot = record.get("snapshot")
    metadata_is_valid = (
        isinstance(metadata, dict)
        and set(metadata) == {"baggage", "traceparent"}
        and all(isinstance(value, str) and value for value in metadata.values())
    )
    snapshot_is_valid = (
        isinstance(snapshot, dict)
        and set(snapshot) == {"messageId", "trackedFileBackups"}
        and snapshot.get("messageId") == user_id
        and snapshot.get("trackedFileBackups") == {}
    )
    return (
        record.get("type") == "file-history-snapshot"
        and record.get("messageId") == user_id
        and record.get("cwd") == str(cwd)
        and type(timestamp) is int
        and type(record.get("isSnapshotUpdate")) is bool
        and metadata_is_valid
        and snapshot_is_valid
    )


def _workbuddy_ai_title_is_verified(record: dict[str, object], session_id: str, cwd: Path) -> bool:
    """Accept one observed title metadata record between snapshot and reasoning."""

    if set(record) != {"id", "type", "aiTitle", "timestamp", "sessionId", "cwd", "_meta"}:
        return False
    identifier = record.get("id")
    timestamp = record.get("timestamp")
    metadata = record.get("_meta")
    return (
        isinstance(identifier, str)
        and bool(identifier)
        and record.get("type") == "ai-title"
        and isinstance(record.get("aiTitle"), str)
        and type(timestamp) is int
        and record.get("sessionId") == session_id
        and record.get("cwd") == str(cwd)
        and isinstance(metadata, dict)
        and set(metadata) == {"baggage", "traceparent"}
        and all(isinstance(value, str) and value for value in metadata.values())
    )


def _workbuddy_native_ids_are_unique(
    p0_history: list[dict[str, object]], current_records: list[dict[str, object]]
) -> bool:
    """Keep current native identities distinct from every frozen P0 record."""

    p0_ids: set[str] = set()
    for record in p0_history:
        if "id" not in record:
            continue
        identifier = record["id"]
        if not isinstance(identifier, str) or not identifier or identifier in p0_ids:
            return False
        p0_ids.add(identifier)
    current_ids: set[str] = set()
    for record in current_records:
        identifier = record.get("id")
        if (
            not isinstance(identifier, str)
            or not identifier
            or identifier in p0_ids
            or identifier in current_ids
        ):
            return False
        current_ids.add(identifier)
    return True


def _workbuddy_p0_call_id_is_reused(p0_history: list[dict[str, object]], call_id: str) -> bool:
    """Reject only a current Skill call that reuses an earlier call/result ID."""

    return any(
        record.get("type") in {"function_call", "function_call_result"}
        and record.get("callId") == call_id
        for record in p0_history
    )


def _workbuddy_record_has_explicit_scope(
    record: dict[str, object], session_id: str, cwd: Path
) -> bool:
    """Require the native request scope on current user/model/tool records."""

    return record.get("sessionId") == session_id and record.get("cwd") == str(cwd)


def _workbuddy_model_identity(record: dict[str, object], request_id: str) -> tuple[str, str] | None:
    """Read the independent model message and model identities from one record."""

    provider = record.get("providerData")
    if not isinstance(provider, dict) or provider.get("conversationRequestId") != request_id:
        return None
    message_id = provider.get("messageId")
    model = provider.get("model")
    if not isinstance(message_id, str) or not message_id:
        return None
    if not isinstance(model, str) or not model:
        return None
    return message_id, model


def _workbuddy_model_step_sequence(record: dict[str, object]) -> int | None:
    """Read the observed integer model step shared by assistant-text Skill turns."""

    provider = record.get("providerData")
    if not isinstance(provider, dict):
        return None
    step_sequence = provider.get("stepSeq")
    return step_sequence if type(step_sequence) is int else None


def _workbuddy_skill_call_matches_request(
    record: dict[str, object],
    *,
    skill: str,
    request_digest: str,
    correction: object,
    args_digest: str,
) -> bool:
    arguments = record.get("arguments")
    if not isinstance(arguments, str):
        return False
    try:
        call_input = json.loads(arguments)
    except json.JSONDecodeError:
        return False
    if (
        not isinstance(call_input, dict)
        or set(call_input) != {"skill", "args"}
        or call_input.get("skill") != skill
        or not isinstance(call_input.get("args"), str)
    ):
        return False
    args = call_input["args"]
    if hashlib.sha256(args.encode("utf-8")).hexdigest() != args_digest:
        return False
    try:
        parsed_input, end = json.JSONDecoder().raw_decode(args)
    except json.JSONDecodeError:
        return False
    if end != len(args) or not isinstance(parsed_input, dict):
        return False
    business_input = dict(parsed_input)
    has_correction = "selc" in business_input
    correction_value = business_input.pop("selc") if has_correction else None
    call_correction = _workbuddy_raw_correction_declaration(correction_value, has_correction)
    try:
        call_digest = hashlib.sha256(
            canonical_json({"skill": skill, "input": business_input}).encode("utf-8")
        ).hexdigest()
    except (TypeError, ValueError):
        return False
    return call_digest == request_digest and call_correction == correction


def _workbuddy_skill_call_matches_business_dto(
    record: dict[str, object],
    *,
    skill: str,
    business_dto_digest: str,
    correction: object,
    args_digest: str,
) -> bool:
    """Bind selectorless raw Skill arguments to the Prompt's fixed DTO goal."""

    arguments = record.get("arguments")
    if not isinstance(arguments, str):
        return False
    try:
        call_input = json.loads(arguments)
    except json.JSONDecodeError:
        return False
    if (
        not isinstance(call_input, dict)
        or set(call_input) != {"skill", "args"}
        or call_input.get("skill") != skill
        or not isinstance(call_input.get("args"), str)
    ):
        return False
    args = call_input["args"]
    if hashlib.sha256(args.encode("utf-8")).hexdigest() != args_digest:
        return False
    if _business_dto_goal_digest_from_raw_prompt(args) != business_dto_digest:
        return False
    try:
        parsed_input, end = json.JSONDecoder().raw_decode(args)
    except json.JSONDecodeError:
        return False
    if end != len(args) or not isinstance(parsed_input, dict):
        return False
    has_correction = "selc" in parsed_input
    call_correction = _workbuddy_raw_correction_declaration(
        parsed_input.get("selc"), has_correction
    )
    return call_correction == correction


def _workbuddy_raw_correction_declaration(
    value: object,
    present: bool,
) -> dict[str, object] | None:
    """Normalize only the raw ``selc`` object carried inside Skill arguments."""

    if not present:
        return None
    if not isinstance(value, dict) or set(value) != {"intent", "target", "preserve_goal"}:
        return {"valid": False}
    if (
        value.get("intent") != "correct_delivery"
        or not isinstance(value.get("target"), str)
        or not value["target"]
        or value.get("preserve_goal") is not True
    ):
        return {"valid": False}
    return {
        "intent": "correct_delivery",
        "target": value["target"],
        "preserve_goal": True,
        "valid": True,
    }


def _business_dto_goal_digest_from_raw_prompt(raw_prompt: str) -> str | None:
    """Parse one complete JSON DTO without accepting surrounding prompt text."""

    try:
        dto, end = json.JSONDecoder().raw_decode(raw_prompt)
    except json.JSONDecodeError:
        return None
    if end != len(raw_prompt):
        return None
    return _business_dto_goal_digest(dto)


def _workbuddy_history_record_matches_scope(
    record: object,
    session_id: str,
    cwd: Path,
) -> bool:
    """Reject a conflicting native scope while leaving prior business opaque."""

    if not isinstance(record, dict):
        return False
    record_session = record.get("sessionId")
    if record_session is not None and record_session != session_id:
        return False
    record_cwd = record.get("cwd")
    return record_cwd is None or record_cwd == str(cwd)


def _codex_current_turn_suffix_is_verified(
    history: list[dict[str, object]],
    *,
    session_id: str,
    cwd: Path,
    generation_id: str,
) -> bool:
    """Accept only the observed resumed-turn initializer after prior history.

    Earlier records may describe closed business work.  They cannot be reused
    as a new root, and the current suffix must not include the initial-only
    developer/settings records or another user/business item.
    """

    start_indexes = [
        index
        for index, record in enumerate(history)
        if record.get("type") == "event_msg"
        and isinstance(record.get("payload"), dict)
        and record["payload"].get("type") == "task_started"
        and _codex_initial_event_is_verified(record["payload"], generation_id, session_id)
    ]
    if len(start_indexes) != 1:
        return False
    suffix = history[start_indexes[0] :]
    if len(suffix) != 4:
        return False
    task_started, environment_context, world_state, turn_context = suffix
    if not _codex_initial_event_is_verified(task_started.get("payload"), generation_id, session_id):
        return False
    environment_payload = environment_context.get("payload")
    environment_metadata = (
        environment_payload.get("internal_chat_message_metadata_passthrough")
        if isinstance(environment_payload, dict)
        else None
    )
    if (
        environment_context.get("type") != "response_item"
        or not isinstance(environment_payload, dict)
        or environment_payload.get("type") != "message"
        or environment_payload.get("role") != "user"
        or not isinstance(environment_metadata, dict)
        or environment_metadata.get("turn_id") != generation_id
        or environment_metadata.get("content_item_kinds") != ["environments.environment_context"]
        or not _codex_record_cwd_is_current(environment_context, cwd)
    ):
        return False
    return (
        world_state.get("type") == "world_state"
        and _codex_world_state_is_verified(world_state.get("payload"))
        and turn_context.get("type") == "turn_context"
        and _codex_turn_context_is_verified(turn_context.get("payload"), generation_id, cwd)
    )


def _codex_current_turn_anchor_rule_version(
    history: list[dict[str, object]],
    *,
    session_id: str,
    cwd: Path,
    generation_id: str,
    prompt_version: object,
    pre_version: object,
) -> str | None:
    """Select the exact durable rule that proved this Codex current turn.

    The original full-world-state shape remains the v2 proof.  Alpha.11 has a
    separately observed continuation shape that changes only Codex's temporary
    ``arg0`` path.  A pre-existing proof is never reclassified by this helper.
    """

    if _codex_current_turn_suffix_is_verified(
        history,
        session_id=session_id,
        cwd=cwd,
        generation_id=generation_id,
    ):
        return CURRENT_REQUEST_ANCHOR_RULE_VERSION
    if prompt_version != CODEX_ALPHA11_ARG0_VERSION or pre_version != CODEX_ALPHA11_ARG0_VERSION:
        return None
    if not _codex_alpha11_session_metadata_is_verified(history, session_id, cwd):
        return None
    if not _codex_alpha11_current_turn_delta_is_verified(
        history,
        session_id=session_id,
        cwd=cwd,
        generation_id=generation_id,
    ):
        return None
    return CODEX_ALPHA11_ARG0_ANCHOR_RULE_VERSION


def _codex_alpha11_session_metadata_is_verified(
    history: list[dict[str, object]],
    session_id: str,
    cwd: Path,
) -> bool:
    """Verify the one alpha.11 session record frozen with the current request."""

    metadata_records = [record for record in history if record.get("type") == "session_meta"]
    if len(metadata_records) != 1:
        return False
    metadata = metadata_records[0].get("payload")
    if not isinstance(metadata, dict):
        return False
    identities = {
        value
        for key in ("id", "session_id")
        if isinstance((value := metadata.get(key)), str) and value
    }
    return (
        identities == {session_id}
        and metadata.get("cwd") == str(cwd)
        and metadata.get("cli_version") == CODEX_ALPHA11_ARG0_VERSION
    )


def _codex_alpha11_current_turn_delta_is_verified(
    history: list[dict[str, object]],
    *,
    session_id: str,
    cwd: Path,
    generation_id: str,
) -> bool:
    """Verify alpha.11's closed-world arg0 replacement without reducing deltas."""

    start_indexes = [
        index
        for index, record in enumerate(history)
        if record.get("type") == "event_msg"
        and isinstance(record.get("payload"), dict)
        and record["payload"].get("type") == "task_started"
        and _codex_initial_event_is_verified(record["payload"], generation_id, session_id)
    ]
    if len(start_indexes) != 1:
        return False
    start_index = start_indexes[0]
    suffix = history[start_index:]
    if len(suffix) != 4:
        return False
    task_started, environment_context, world_state, turn_context = suffix
    if not _codex_initial_event_is_verified(task_started.get("payload"), generation_id, session_id):
        return False
    if not _codex_current_environment_context_is_verified(
        environment_context,
        generation_id=generation_id,
        cwd=cwd,
    ):
        return False
    if (
        world_state.get("type") != "world_state"
        or not _codex_alpha11_delta_world_state_is_verified(world_state.get("payload"))
        or turn_context.get("type") != "turn_context"
        or not _codex_turn_context_is_verified(turn_context.get("payload"), generation_id, cwd)
    ):
        return False

    baseline = _codex_alpha11_latest_full_world_state(history[:start_index])
    if baseline is None:
        return False
    baseline_index, previous_filesystem = baseline
    for record in history[baseline_index + 1 :]:
        if record.get("type") != "world_state":
            continue
        current_filesystem = _codex_alpha11_delta_filesystem(record.get("payload"))
        if current_filesystem is None:
            return False
        if not _codex_alpha11_arg0_transition_is_verified(
            previous_filesystem,
            current_filesystem,
        ):
            return False
        previous_filesystem = current_filesystem
    return _codex_environment_context_contains_filesystem_once(
        environment_context,
        previous_filesystem,
    )


def _codex_current_environment_context_is_verified(
    record: dict[str, object],
    *,
    generation_id: str,
    cwd: Path,
) -> bool:
    """Bind the current environment record before checking its opaque text."""

    payload = record.get("payload")
    metadata = (
        payload.get("internal_chat_message_metadata_passthrough")
        if isinstance(payload, dict)
        else None
    )
    return (
        record.get("type") == "response_item"
        and isinstance(payload, dict)
        and payload.get("type") == "message"
        and payload.get("role") == "user"
        and isinstance(metadata, dict)
        and metadata.get("turn_id") == generation_id
        and metadata.get("content_item_kinds") == ["environments.environment_context"]
        and _codex_record_cwd_is_current(record, cwd)
    )


def _codex_alpha11_latest_full_world_state(
    history: list[dict[str, object]],
) -> tuple[int, str] | None:
    """Return the latest valid full filesystem state before the resumed turn."""

    latest: tuple[int, str] | None = None
    for index, record in enumerate(history):
        if record.get("type") != "world_state":
            continue
        payload = record.get("payload")
        if not _codex_world_state_is_verified(payload):
            continue
        assert isinstance(payload, dict)
        state = payload["state"]
        environments = state.get("environments") if isinstance(state, dict) else None
        filesystem = environments.get("filesystem") if isinstance(environments, dict) else None
        if isinstance(filesystem, str):
            latest = (index, filesystem)
    return latest


def _codex_alpha11_delta_world_state_is_verified(payload: object) -> bool:
    """Accept only the observed alpha.11 filesystem delta serializer shape."""

    if not isinstance(payload, dict) or set(payload) != {"full", "state"}:
        return False
    if payload.get("full") is not False or not isinstance(payload.get("state"), dict):
        return False
    state = payload["state"]
    if set(state) != {"environments"} or not isinstance(state["environments"], dict):
        return False
    environments = state["environments"]
    return set(environments) == {"filesystem"} and isinstance(environments["filesystem"], str)


def _codex_alpha11_delta_filesystem(payload: object) -> str | None:
    """Read a filesystem string only after the strict delta shape has been proven."""

    if not _codex_alpha11_delta_world_state_is_verified(payload):
        return None
    assert isinstance(payload, dict)
    state = payload["state"]
    assert isinstance(state, dict)
    environments = state["environments"]
    assert isinstance(environments, dict)
    filesystem = environments["filesystem"]
    assert isinstance(filesystem, str)
    return filesystem


def _codex_alpha11_arg0_transition_is_verified(
    old_filesystem: str,
    new_filesystem: str,
) -> bool:
    """Require one changed, account-bound arg0 path and byte equality otherwise."""

    path_pattern = _codex_alpha11_arg0_path_pattern()
    if path_pattern is None:
        return False
    old_bytes = old_filesystem.encode("utf-8")
    new_bytes = new_filesystem.encode("utf-8")
    old_matches = list(path_pattern.finditer(old_bytes))
    new_matches = list(path_pattern.finditer(new_bytes))
    if len(old_matches) != 1 or len(new_matches) != 1:
        return False
    old_match = old_matches[0]
    new_match = new_matches[0]
    if old_match.group() == new_match.group():
        return False
    normalized_old = old_bytes[: old_match.start()] + old_bytes[old_match.end() :]
    normalized_new = new_bytes[: new_match.start()] + new_bytes[new_match.end() :]
    return normalized_old == normalized_new


def _codex_alpha11_arg0_path_pattern() -> re.Pattern[bytes] | None:
    """Match one observed full arg0 path for the trusted current POSIX account."""

    home = _trusted_posix_account_home()
    if home is None:
        return None
    try:
        home_bytes = str(home).encode("utf-8")
    except UnicodeEncodeError:
        return None
    return re.compile(
        rb"<path>"
        + re.escape(home_bytes)
        + rb"/\.codex/tmp/arg0-macos/codex-arg0[A-Za-z0-9]+</path>"
    )


def _trusted_posix_account_home() -> Path | None:
    """Read the current account home from the OS, never from HOME or a request."""

    if pwd is None:
        return None
    try:
        account = pwd.getpwuid(os.getuid())
    except (AttributeError, KeyError, OSError):
        return None
    home = Path(account.pw_dir)
    return home if home.is_absolute() else None


def _codex_environment_context_contains_filesystem_once(
    record: dict[str, object],
    filesystem: str,
) -> bool:
    """Keep environment text opaque while requiring the proven filesystem once."""

    payload = record.get("payload")
    if not isinstance(payload, dict):
        return False
    content = payload.get("content")
    if not isinstance(content, list):
        return False
    occurrences = 0
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "input_text":
            continue
        text = item.get("text")
        if isinstance(text, str):
            occurrences += text.count(filesystem)
    return occurrences == 1


def _bind_verified_correction(
    database: Any,
    *,
    project_root: Path,
    installation: dict[str, Any],
    context: Any,
    event: dict[str, object],
    declaration: dict[str, object],
    anchor_event_id: str,
) -> bool:
    """Bind one exact failed delivery to a new request without selecting a latest task.

    All reads run under the registry-to-suite lock already held by the caller
    and its short records transaction.  The stored binding is intentionally a
    snapshot: later publication cannot cause this correction to retarget.
    """

    if declaration.get("valid") is not True or declaration.get("intent") != "correct_delivery":
        return False
    target = declaration.get("target")
    if not isinstance(target, str) or declaration.get("preserve_goal") is not True:
        return False
    target_path = _correction_target_path(project_root, target)
    if target_path is None:
        return False
    existing = database.execute(
        "SELECT * FROM managed_correction_bindings WHERE context_key = ?",
        (context["context_key"],),
    ).fetchone()
    if existing is not None:
        if existing["business_dto_digest"] is not None and (
            existing["business_dto_digest"] != context["business_dto_digest"]
            or existing["business_dto_rule_version"] != context["business_dto_rule_version"]
        ):
            return False
        return _correction_binding_is_current(database, existing, installation)
    anchor = database.execute(
        """SELECT p0_event_id, pre_event_id, rule_version FROM managed_request_anchors
        WHERE context_key = ?""",
        (context["context_key"],),
    ).fetchone()
    if (
        anchor is None
        or anchor["pre_event_id"] != anchor_event_id
        or not _durable_current_request_anchor_is_verified(database, context, event, anchor)
    ):
        return False
    if context["task_id"] is not None or bool(context["root_valid"]):
        return False
    try:
        assert_no_live_execution(database, str(event["suite_id"]))
    except SelcError:
        return False

    source = _unique_owned_correction_source(
        database,
        suite_id=str(event["suite_id"]),
        target_path=target_path,
        context=context,
        actual_skill=str(event["skill"]),
        installation=installation,
    )
    if source is None:
        return False
    if not _native_relation_matches_source(database, context, event, source):
        return False
    active_binding = database.execute(
        """SELECT 1 FROM managed_correction_bindings
        WHERE task_id = ? AND status = 'active' LIMIT 1""",
        (source["task_id"],),
    ).fetchone()
    if active_binding is not None:
        return False
    task_id = str(source["task_id"])
    _assert_no_inflight_publication(database, task_id)
    database.execute(
        """INSERT INTO managed_correction_bindings(
        context_key, suite_id, task_id, source_delivery_id, source_ordinal,
        target_path, target_device, target_inode, target_digest,
        target_parent_device, target_parent_inode, input_digest,
        business_dto_digest, business_dto_rule_version,
        checker_input_digest, checker_name, checker_version, checker_digest, rule_version,
        resource_identity, resource_digest, composite_identity, composite_digest,
        installation_digest, installation_version, anchor_event_id, anchor_rule_version,
        status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)""",
        (
            context["context_key"],
            event["suite_id"],
            task_id,
            source["delivery_id"],
            source["ordinal"],
            str(target_path),
            source["target_device"],
            source["target_inode"],
            source["target_digest"],
            source["target_parent_device"],
            source["target_parent_inode"],
            source["input_digest"],
            source["business_dto_digest"],
            source["business_dto_rule_version"],
            source["checker_input_digest"],
            source["checker_name"],
            source["checker_version"],
            source["checker_digest"],
            source["rule_version"],
            source["resource_identity"],
            source["resource_digest"],
            source["composite_identity"],
            source["composite_digest"],
            installation["digest"],
            installation["version"],
            anchor_event_id,
            anchor["rule_version"],
            records.utc_now(),
        ),
    )
    database.execute(
        """UPDATE managed_contexts
        SET task_id = ?, intervention = 1
        WHERE context_key = ? AND task_id IS NULL AND root_valid = 0""",
        (task_id, context["context_key"]),
    )
    return (
        database.execute(
            "SELECT task_id FROM managed_contexts WHERE context_key = ?",
            (context["context_key"],),
        ).fetchone()["task_id"]
        == task_id
    )


def _correction_target_path(project_root: Path, target: str) -> Path | None:
    """Resolve an existing owned target without creating a directory for a claim."""

    relative = Path(target)
    if not target or relative.is_absolute() or ".." in relative.parts:
        return None
    candidate = project_root / relative
    try:
        parent = candidate.parent.resolve(strict=True)
        parent.relative_to(project_root.resolve(strict=True))
    except (OSError, ValueError):
        return None
    if candidate.is_symlink() or not candidate.is_file():
        return None
    return candidate


def _unique_owned_correction_source(
    database: Any,
    *,
    suite_id: str,
    target_path: Path,
    context: Any,
    actual_skill: str,
    installation: dict[str, Any],
) -> Any | None:
    """Return the one durable delivery whose owned object still names target."""

    rows = database.execute(
        """SELECT publications.delivery_id, publications.ordinal, publications.task_id,
        publications.target_path, intents.staged_device AS target_device,
        intents.staged_inode AS target_inode, intents.staged_digest AS target_digest,
        intents.target_parent_device, intents.target_parent_inode,
        actions.request_digest AS input_digest, actions.artifact_path AS action_artifact_path,
        calls.skill, calls.actual_harness,
        calls.expected_version, calls.expected_hash, checker_runs.checker_name,
        checker_runs.checker_version, checker_runs.checker_digest, checker_runs.rule_version,
        checker_runs.result AS checker_result, checker_runs.input_digest AS checker_input_digest,
        checker_runs.resource_identity, checker_runs.resource_digest,
        checker_runs.composite_identity, checker_runs.composite_digest,
        checker_runs.installation_digest AS checker_installation_digest,
        checker_runs.installation_version AS checker_installation_version,
        receipts.host AS receipt_host, receipts.session_id AS receipt_session,
        source_context.business_dto_digest, source_context.business_dto_rule_version,
        source_prompt.payload_json AS source_prompt_payload
        FROM managed_publications AS publications
        JOIN managed_publication_intents AS intents
          ON intents.delivery_id = publications.delivery_id
        JOIN managed_actions AS actions ON actions.action_run_id = publications.action_run_id
        JOIN calls ON calls.call_id = publications.call_id
        JOIN checker_runs ON checker_runs.delivery_id = publications.delivery_id
        JOIN managed_receipts AS receipts ON receipts.receipt_digest = publications.receipt_digest
        JOIN managed_contexts AS source_context
          ON source_context.suite_id = receipts.suite_id
          AND source_context.host = receipts.host
          AND source_context.session_id = receipts.session_id
          AND source_context.generation_id = receipts.generation_id
          AND source_context.request_id = receipts.request_id
        JOIN managed_native_events AS source_prompt
          ON source_prompt.event_id = source_context.root_event_id
          AND source_prompt.phase = ?
        WHERE publications.suite_id = ? AND publications.target_path = ?
          AND publications.status = 'recorded' AND intents.state = 'recorded'""",
        (USER_PROMPT_PHASE, suite_id, str(target_path)),
    ).fetchall()
    matching: list[Any] = []
    target_stat = target_path.stat()
    parent_stat = target_path.parent.stat()
    target_digest = file_hash(target_path)
    for row in rows:
        if (
            target_stat.st_dev != row["target_device"]
            or target_stat.st_ino != row["target_inode"]
            or target_digest != row["target_digest"]
            or parent_stat.st_dev != row["target_parent_device"]
            or parent_stat.st_ino != row["target_parent_inode"]
            or row["skill"] != actual_skill
            or row["actual_harness"] != context["host"]
            or row["receipt_host"] != context["host"]
            or row["receipt_session"] != context["session_id"]
            or row["expected_version"] != installation["version"]
            or row["expected_hash"] != installation["digest"]
            or row["checker_installation_version"] != installation["version"]
            or row["checker_installation_digest"] != installation["digest"]
            or row["checker_result"] != "fail"
            or _stored_action_checker_input_digest(row["action_artifact_path"])
            != row["checker_input_digest"]
        ):
            continue
        if not _correction_goal_is_preserved(database, context, row, actual_skill):
            continue
        closed_windows = database.execute(
            "SELECT status FROM managed_windows WHERE delivery_id = ?",
            (row["delivery_id"],),
        ).fetchall()
        if not closed_windows or any(window["status"] != "closed" for window in closed_windows):
            continue
        matching.append(row)
    return matching[0] if len(matching) == 1 else None


def _correction_goal_is_preserved(
    database: Any,
    context: Any,
    source: Any,
    actual_skill: str,
) -> bool:
    """Compare formal goals, using a narrow legacy explicit-only fallback.

    New records must carry the independently frozen business DTO digest on
    both sides.  A NULL legacy source is not evidence for a cross-selector
    correction, so the old action/request digest comparison remains available
    only when both immutable Prompt records explicitly named the same skill.
    """

    source_has_goal = _has_business_dto_evidence(
        source["business_dto_digest"], source["business_dto_rule_version"]
    )
    current_has_goal = _has_business_dto_evidence(
        context["business_dto_digest"], context["business_dto_rule_version"]
    )
    source_goal_valid = _is_valid_business_dto_evidence(
        source["business_dto_digest"], source["business_dto_rule_version"]
    )
    current_goal_valid = _is_valid_business_dto_evidence(
        context["business_dto_digest"], context["business_dto_rule_version"]
    )
    if source_goal_valid and current_goal_valid:
        return source["business_dto_digest"] == context["business_dto_digest"]
    if (source_has_goal and not source_goal_valid) or (current_has_goal and not current_goal_valid):
        return False

    source_selector = _immutable_prompt_selector(source["source_prompt_payload"])
    current_selector = _context_immutable_prompt_selector(database, context)
    return (
        source_selector == actual_skill
        and current_selector == actual_skill
        and source["input_digest"] == context["request_digest"]
    )


def _has_business_dto_evidence(digest: object, rule_version: object) -> bool:
    """Distinguish a legacy NULL pair from incomplete new-protocol evidence."""

    return digest is not None or rule_version is not None


def _is_valid_business_dto_evidence(digest: object, rule_version: object) -> bool:
    return (
        isinstance(digest, str)
        and _is_sha256_digest(digest)
        and rule_version == BUSINESS_DTO_RULE_VERSION
    )


def _context_immutable_prompt_selector(database: Any, context: Any) -> str | None:
    """Read the original selector only from the durable Prompt event."""

    row = database.execute(
        """SELECT payload_json FROM managed_native_events
        WHERE event_id = ? AND phase = ?""",
        (context["root_event_id"], USER_PROMPT_PHASE),
    ).fetchone()
    return None if row is None else _immutable_prompt_selector(row["payload_json"])


def _immutable_prompt_selector(payload_json: object) -> str | None:
    """Return a nonempty selector from one immutable Prompt payload only."""

    if not isinstance(payload_json, str):
        return None
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError:
        return None
    selector = payload.get("skill") if isinstance(payload, dict) else None
    return selector if isinstance(selector, str) and selector else None


def _native_relation_matches_source(
    database: Any,
    context: Any,
    event: dict[str, object],
    source: Any,
) -> bool:
    """Reject a supplied native ancestry relation when it contradicts the source."""

    relations = {
        str(event.get("root_request_id", "")),
        str(event.get("parent_request_id", "")),
    }
    relations.discard("")
    if not relations:
        return True
    placeholders = ", ".join("?" for _ in relations)
    rows = database.execute(
        f"""SELECT DISTINCT task_id FROM managed_contexts
        WHERE suite_id = ? AND host = ? AND session_id = ? AND task_id IS NOT NULL
          AND (request_id IN ({placeholders}) OR root_request_id IN ({placeholders}))""",
        (
            context["suite_id"],
            context["host"],
            context["session_id"],
            *sorted(relations),
            *sorted(relations),
        ),
    ).fetchall()
    return {str(row["task_id"]) for row in rows} == {str(source["task_id"])}


def _correction_binding_is_current(
    database: Any,
    binding: Any,
    installation: dict[str, Any],
) -> bool:
    """Fence a correction to its original delivery instead of a newer target version."""

    if binding["status"] != "active":
        return False
    if (
        binding["installation_digest"] != installation["digest"]
        or binding["installation_version"] != installation["version"]
    ):
        return False
    target_path = Path(str(binding["target_path"]))
    try:
        target_stat = target_path.stat()
        parent_stat = target_path.parent.stat()
    except OSError:
        return False
    if (
        target_path.is_symlink()
        or not target_path.is_file()
        or target_stat.st_dev != binding["target_device"]
        or target_stat.st_ino != binding["target_inode"]
        or file_hash(target_path) != binding["target_digest"]
        or parent_stat.st_dev != binding["target_parent_device"]
        or parent_stat.st_ino != binding["target_parent_inode"]
    ):
        return False
    source = database.execute(
        """SELECT publications.delivery_id, publications.ordinal, publications.status,
        publications.target_path, intents.state, intents.staged_device, intents.staged_inode,
        intents.staged_digest, actions.request_digest, actions.artifact_path AS action_artifact_path,
        checker_runs.input_digest AS checker_input_digest, checker_runs.checker_name,
        checker_runs.checker_version, checker_runs.checker_digest, checker_runs.rule_version,
        checker_runs.resource_identity, checker_runs.resource_digest,
        checker_runs.composite_identity, checker_runs.composite_digest,
        checker_runs.installation_digest, checker_runs.installation_version,
        source_context.business_dto_digest, source_context.business_dto_rule_version
        FROM managed_publications AS publications
        JOIN managed_publication_intents AS intents ON intents.delivery_id = publications.delivery_id
        JOIN managed_actions AS actions ON actions.action_run_id = publications.action_run_id
        JOIN checker_runs ON checker_runs.delivery_id = publications.delivery_id
        JOIN managed_receipts AS receipts ON receipts.receipt_digest = publications.receipt_digest
        JOIN managed_contexts AS source_context
          ON source_context.suite_id = receipts.suite_id
          AND source_context.host = receipts.host
          AND source_context.session_id = receipts.session_id
          AND source_context.generation_id = receipts.generation_id
          AND source_context.request_id = receipts.request_id
        WHERE publications.delivery_id = ? AND publications.task_id = ?""",
        (binding["source_delivery_id"], binding["task_id"]),
    ).fetchone()
    if source is None or source["status"] != "recorded" or source["state"] != "recorded":
        return False
    expected = {
        "ordinal": binding["source_ordinal"],
        "target_path": binding["target_path"],
        "staged_device": binding["target_device"],
        "staged_inode": binding["target_inode"],
        "staged_digest": binding["target_digest"],
        "request_digest": binding["input_digest"],
        "checker_input_digest": binding["checker_input_digest"],
        "checker_name": binding["checker_name"],
        "checker_version": binding["checker_version"],
        "checker_digest": binding["checker_digest"],
        "rule_version": binding["rule_version"],
        "resource_identity": binding["resource_identity"],
        "resource_digest": binding["resource_digest"],
        "composite_identity": binding["composite_identity"],
        "composite_digest": binding["composite_digest"],
        "installation_digest": binding["installation_digest"],
        "installation_version": binding["installation_version"],
    }
    if any(source[key] != value for key, value in expected.items()):
        return False
    if (
        binding["business_dto_digest"] is not None
        or binding["business_dto_rule_version"] is not None
    ) and (
        binding["business_dto_digest"] != source["business_dto_digest"]
        or binding["business_dto_rule_version"] != source["business_dto_rule_version"]
        or binding["business_dto_rule_version"] != BUSINESS_DTO_RULE_VERSION
    ):
        return False
    if (
        _stored_action_checker_input_digest(source["action_artifact_path"])
        != binding["checker_input_digest"]
    ):
        return False
    newer = database.execute(
        """SELECT 1 FROM managed_publications
        WHERE task_id = ? AND target_path = ? AND status = 'recorded' AND ordinal > ? LIMIT 1""",
        (binding["task_id"], binding["target_path"], binding["source_ordinal"]),
    ).fetchone()
    if newer is not None:
        return False
    windows = database.execute(
        "SELECT status FROM managed_windows WHERE delivery_id = ?",
        (binding["source_delivery_id"],),
    ).fetchall()
    return bool(windows) and all(window["status"] == "closed" for window in windows)


def _assert_correction_binding_current(
    database: Any,
    context: Any,
    installation: dict[str, Any],
) -> None:
    """Fence a bound correction at every state-changing consumption point."""

    binding = database.execute(
        "SELECT * FROM managed_correction_bindings WHERE context_key = ?",
        (context["context_key"],),
    ).fetchone()
    if binding is None:
        return
    if not _correction_binding_is_current(database, binding, installation):
        raise SelcError(
            "CORRECTION_BINDING_STALE",
            "the fixed failed delivery or owned target changed before this correction ran",
            "start a new verified correction request from the current delivered target",
        )


def _stored_action_checker_input_digest(artifact_path: object) -> str | None:
    """Hash the frozen checker input staged beside one managed artifact."""

    if not isinstance(artifact_path, str) or not artifact_path:
        return None
    input_path = Path(artifact_path).parent / "input"
    if input_path.is_symlink() or not input_path.is_file():
        return None
    try:
        return file_hash(input_path)
    except OSError:
        return None


def _assert_correction_checker_input_digest(
    database: Any,
    context: Any,
    checker_input_digest: str | None,
) -> None:
    """Require a correction to preserve the failed delivery's checker input bytes."""

    binding = database.execute(
        "SELECT checker_input_digest FROM managed_correction_bindings WHERE context_key = ?",
        (context["context_key"],),
    ).fetchone()
    if binding is None:
        return
    if not checker_input_digest or checker_input_digest != binding["checker_input_digest"]:
        raise SelcError(
            "CORRECTION_BINDING_STALE",
            "the correction's checker input no longer matches the failed delivery",
            "restore the fixed input bytes or start a new verified request",
        )


def _assert_correction_action_input(database: Any, context: Any, action_row: Any) -> None:
    """Recheck staged checker bytes before reserving or exchanging a correction."""

    _assert_correction_checker_input_digest(
        database,
        context,
        _stored_action_checker_input_digest(action_row["artifact_path"]),
    )


def _assert_correction_publish_target(
    database: Any,
    context: Any,
    project_root: Path,
    target: str,
) -> None:
    """Keep one correction on its bound owned target through the visible exchange."""

    binding = database.execute(
        "SELECT target_path FROM managed_correction_bindings WHERE context_key = ?",
        (context["context_key"],),
    ).fetchone()
    if binding is None:
        return
    relative = Path(target)
    if not target or relative.is_absolute() or ".." in relative.parts:
        raise SelcError(
            "CORRECTION_TARGET_MISMATCH",
            "the correction publication target is not the bound owned target",
            "publish only to the exact target named by the verified correction request",
        )
    requested = project_root / relative
    try:
        requested_parent = requested.parent.resolve(strict=True)
        requested_canonical = requested_parent / requested.name
        bound_canonical = Path(str(binding["target_path"])).resolve(strict=True)
    except OSError as error:
        raise SelcError(
            "CORRECTION_TARGET_MISMATCH",
            "the correction publication target is not the bound owned target",
            "publish only to the exact target named by the verified correction request",
        ) from error
    if requested_canonical != bound_canonical:
        raise SelcError(
            "CORRECTION_TARGET_MISMATCH",
            "the correction publication target is not the bound owned target",
            "publish only to the exact target named by the verified correction request",
        )


def _assert_correction_request_input(
    database: Any,
    context: Any,
    action: dict[str, object],
    request: dict[str, object],
    frozen_inputs: dict[str, bytes],
) -> None:
    """Compare the frozen primary input before starting corrected business work."""

    schema = action.get("input")
    fields = schema.get("fields") if isinstance(schema, dict) else None
    input_specification = fields.get("input") if isinstance(fields, dict) else None
    input_value = request.get("input")
    if (
        not isinstance(input_specification, dict)
        or input_specification.get("type") != "path"
        or not isinstance(input_value, str)
    ):
        _assert_correction_checker_input_digest(database, context, None)
        return
    input_bytes = frozen_inputs.get("input")
    if input_bytes is None:
        _assert_correction_checker_input_digest(database, context, None)
        return
    input_digest = hashlib.sha256(input_bytes).hexdigest()
    _assert_correction_checker_input_digest(database, context, input_digest)


def _assert_correction_checker_execution(
    database: Any,
    context: Any,
    execution: Any | None,
) -> None:
    """Reject a changed checker basis before exchanging corrected publication bytes."""

    binding = database.execute(
        "SELECT * FROM managed_correction_bindings WHERE context_key = ?",
        (context["context_key"],),
    ).fetchone()
    if binding is None:
        return
    if execution is None:
        raise SelcError(
            "CORRECTION_BINDING_STALE",
            "the correction has no independent checker execution",
            "run the fixed checker before publishing a correction",
        )
    result = getattr(execution, "result", None)
    checker = getattr(result, "checker", None)
    expected = {
        "checker_input_digest": getattr(result, "input_digest", None),
        "checker_name": getattr(checker, "name", None),
        "checker_version": getattr(checker, "version", None),
        "checker_digest": getattr(checker, "digest", None),
        "rule_version": getattr(result, "rule_version", None),
        "resource_identity": getattr(result, "resource_identity", None),
        "resource_digest": getattr(result, "resource_digest", None),
        "composite_identity": getattr(result, "composite_identity", None),
        "composite_digest": getattr(result, "composite_digest", None),
        "installation_digest": getattr(execution, "installation_digest", None),
        "installation_version": getattr(execution, "installation_version", None),
    }
    if any(binding[key] != value for key, value in expected.items()):
        raise SelcError(
            "CORRECTION_BINDING_STALE",
            "the correction checker basis differs from the failed delivery",
            "restore the fixed checker and input basis before publishing",
        )


def _close_correction_binding(database: Any, event: dict[str, object]) -> None:
    """Release correction ownership only after its task has no live activity."""

    binding = database.execute(
        """SELECT task_id FROM managed_correction_bindings
        WHERE context_key = ? AND status = 'active'""",
        (_context_key(event),),
    ).fetchone()
    if binding is None:
        return
    task_id = str(binding["task_id"])
    active_work = database.execute(
        """SELECT 1
        FROM calls
        WHERE task_id = ? AND status = 'active'
        UNION ALL
        SELECT 1
        FROM managed_actions
        WHERE task_id = ? AND status IN ('reserved', 'finished')
        UNION ALL
        SELECT 1
        FROM managed_publications
        WHERE task_id = ? AND status IN ('reserved', 'prepared', 'supplied')
        LIMIT 1""",
        (task_id, task_id, task_id),
    ).fetchone()
    if active_work is not None:
        return
    database.execute(
        """UPDATE managed_correction_bindings SET status = 'closed'
        WHERE context_key = ? AND status = 'active'""",
        (_context_key(event),),
    )


def _issue_receipt(
    database: Any, installation: dict[str, Any], event: dict[str, object], event_id: str
) -> str | None:
    if event["phase"] != PRE_PHASE:
        return None
    context_key = _context_key(event)
    context = database.execute(
        "SELECT * FROM managed_contexts WHERE context_key = ?", (context_key,)
    ).fetchone()
    if context is None:
        if event["request_from_prompt"] or (event["host"] == "codex" and not event["fixture"]):
            return None
        database.execute(
            """INSERT INTO managed_contexts(
            context_key, suite_id, host, session_id, generation_id, request_id, root_request_id,
            request_digest, input_skill, business_dto_digest, business_dto_rule_version,
            root_event_id, root_valid, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, 'active', ?)""",
            (
                context_key,
                event["suite_id"],
                event["host"],
                event["session_id"],
                event["generation_id"],
                event["request_id"],
                event["root_request_id"],
                event["request_digest"],
                event["skill"],
                event_id,
                int(bool(event["root_valid"])),
                records.utc_now(),
            ),
        )
        context = database.execute(
            "SELECT * FROM managed_contexts WHERE context_key = ?", (context_key,)
        ).fetchone()
        if context is None:
            return None
    elif event["request_from_prompt"]:
        correction = _context_correction_declaration(database, context)
        if event["action"] != "unmanaged" and not _context_accepts_receipt_skill(
            database, context, event
        ):
            database.execute(
                "UPDATE managed_contexts SET unknown_change = 1 WHERE context_key = ?",
                (context_key,),
            )
            return None
        if (
            correction is None
            and event["host"] == "codex"
            and not _promote_codex_prompt_root(
                database,
                context,
                event,
                event_id,
            )
        ):
            database.execute(
                "UPDATE managed_contexts SET unknown_change = 1 WHERE context_key = ?",
                (context_key,),
            )
            return None
        context = database.execute(
            "SELECT * FROM managed_contexts WHERE context_key = ?", (context_key,)
        ).fetchone()
        if context is None:
            return None
    elif (
        context["request_digest"] != event["request_digest"]
        or context["input_skill"] != event["skill"]
    ):
        database.execute(
            "UPDATE managed_contexts SET unknown_change = 1 WHERE context_key = ?", (context_key,)
        )
        return None
    correction = _context_correction_declaration(database, context)
    if _context_has_unusable_technical_skill_pre(database, context, event):
        database.execute(
            "UPDATE managed_contexts SET unknown_change = 1 WHERE context_key = ?",
            (context_key,),
        )
        return None
    if _native_request_is_terminated(
        database,
        suite_id=str(event["suite_id"]),
        host=str(event["host"]),
        session_id=str(event["session_id"]),
        generation_id=str(event["generation_id"]),
        request_id=str(event["request_id"]),
        tool_use_id=str(event["tool_use_id"]),
    ):
        return None
    if correction is not None:
        if not _ensure_current_request_anchor(database, context, event, event_id):
            database.execute(
                "UPDATE managed_contexts SET unknown_change = 1 WHERE context_key = ?",
                (context_key,),
            )
            return None
        anchor = database.execute(
            """SELECT pre_event_id FROM managed_request_anchors
            WHERE context_key = ?""",
            (context_key,),
        ).fetchone()
        if (
            anchor is None
            or not isinstance(anchor["pre_event_id"], str)
            or not anchor["pre_event_id"]
        ):
            database.execute(
                "UPDATE managed_contexts SET unknown_change = 1 WHERE context_key = ?",
                (context_key,),
            )
            return None
        if not _bind_verified_correction(
            database,
            project_root=Path(str(event["cwd"])),
            installation=installation,
            context=context,
            event=event,
            declaration=correction,
            anchor_event_id=anchor["pre_event_id"],
        ):
            database.execute(
                "UPDATE managed_contexts SET unknown_change = 1 WHERE context_key = ?",
                (context_key,),
            )
            return None
        context = database.execute(
            "SELECT * FROM managed_contexts WHERE context_key = ?", (context_key,)
        ).fetchone()
        if context is None:
            return None
    effective_causation = str(event["causation"])
    if event["request_from_prompt"] and effective_causation == "unknown":
        effective_causation = _prompt_context_causation(database, context)
    unbound_correction = effective_causation in {"business_correction", "correction_candidate"}
    if correction is None and (unbound_correction or effective_causation == "audit"):
        database.execute(
            "UPDATE managed_contexts SET unknown_change = 1 WHERE context_key = ?", (context_key,)
        )
        return None
    elif effective_causation in {"input_preparation", "automatic_retry", "permission"}:
        _link_explicit_followup_context(database, context_key, event)
    if event["action"] == "unmanaged":
        if effective_causation in {"unknown", "business_correction", "correction_candidate"}:
            database.execute(
                "UPDATE managed_contexts SET unknown_change = 1 WHERE context_key = ?",
                (context_key,),
            )
        return None
    existing = database.execute(
        "SELECT 1 FROM managed_receipts WHERE native_event_id = ?", (event_id,)
    ).fetchone()
    if existing is not None:
        return None
    raw_receipt = secrets.token_urlsafe(32)
    receipt_digest = hashlib.sha256(raw_receipt.encode("utf-8")).hexdigest()
    expires_at = (datetime.now(UTC) + timedelta(seconds=RECEIPT_TTL_SECONDS)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    activation_rows = database.execute(
        """SELECT task_id, call_id FROM managed_receipts
        WHERE suite_id = ? AND host = ? AND session_id = ? AND generation_id = ?
          AND request_id = ? AND action = 'activate' AND task_id IS NOT NULL AND call_id IS NOT NULL""",
        (
            event["suite_id"],
            event["host"],
            event["session_id"],
            event["generation_id"],
            event["request_id"],
        ),
    ).fetchall()
    activation_subjects = {(str(row["task_id"]), str(row["call_id"])) for row in activation_rows}
    if event["action"] == "activate":
        task_id, call_id = None, None
    else:
        requested_subject = _operation_subject(event)
        if requested_subject is not None:
            activation_subjects.intersection_update({requested_subject})
        if len(activation_subjects) != 1:
            database.execute(
                "UPDATE managed_contexts SET unknown_change = 1 WHERE context_key = ?",
                (context_key,),
            )
            return None
        task_id, call_id = next(iter(activation_subjects))
    database.execute(
        """INSERT INTO managed_receipts(
        receipt_digest, native_event_id, installation_digest, host, suite_id, skill, action,
        command_digest, operation_digest, session_id, generation_id, request_id, tool_use_id, host_version,
        expires_at, status, task_id, call_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ready', ?, ?)""",
        (
            receipt_digest,
            event_id,
            installation["digest"],
            event["host"],
            event["suite_id"],
            event["skill"],
            event["action"],
            event["command_digest"],
            event["operation_digest"],
            event["session_id"],
            event["generation_id"],
            event["request_id"],
            event["tool_use_id"],
            event["client_version"],
            expires_at,
            task_id,
            call_id,
        ),
    )
    return raw_receipt


def _context_accepts_receipt_skill(database: Any, context: Any, event: dict[str, object]) -> bool:
    """Check selector and automatic choice before a business receipt exists."""

    actual_skill = event.get("skill")
    if not isinstance(actual_skill, str) or not actual_skill:
        return False
    if context["input_skill"]:
        return context["input_skill"] == actual_skill
    selected_skill = context["selected_skill"]
    if selected_skill is not None:
        return selected_skill == actual_skill
    if not _context_uses_automatic_selection(context):
        return event["action"] == "activate"
    if event["host"] != "codex" or event["action"] != "activate":
        return False
    rows = database.execute(
        """SELECT skill FROM managed_receipts
        WHERE suite_id = ? AND host = ? AND session_id = ? AND generation_id = ?
          AND request_id = ? AND action = 'activate' AND status IN ('ready', 'consumed')""",
        (
            context["suite_id"],
            context["host"],
            context["session_id"],
            context["generation_id"],
            context["request_id"],
        ),
    ).fetchall()
    return all(row["skill"] == actual_skill for row in rows)


def _prompt_context_causation(database: Any, context: Any) -> str:
    """Read the immutable prompt classification for its matching tool boundary."""

    row = database.execute(
        "SELECT payload_json FROM managed_native_events WHERE event_id = ?",
        (context["root_event_id"],),
    ).fetchone()
    if row is None:
        return "unknown"
    try:
        payload = json.loads(str(row["payload_json"]))
    except json.JSONDecodeError:
        return "unknown"
    causation = payload.get("causation") if isinstance(payload, dict) else None
    return causation if isinstance(causation, str) else "unknown"


def _operation_subject(event: dict[str, object]) -> tuple[str, str] | None:
    arguments = event.get("operation_arguments")
    if not isinstance(arguments, dict):
        return None
    task_id = arguments.get("--task-id")
    call_id = arguments.get("--call-id")
    if isinstance(task_id, str) and task_id and isinstance(call_id, str) and call_id:
        return task_id, call_id
    return None


def _link_explicit_followup_context(
    database: Any, context_key: str, event: dict[str, object]
) -> None:
    """Reuse a task only when the producer supplies an exact native relation.

    A matching session, text, or generation is never enough.  Only technical
    preparation, automatic retry, and permission contexts can link through a
    trusted root request or direct parent request.
    An ambiguous relation remains unassigned so activation pauses instead of
    hiding an intervention in a new task.
    """

    relations = {
        str(event["root_request_id"]),
        str(event.get("parent_request_id", "")),
    }
    relations.discard("")
    if not relations:
        return
    relation_values = sorted(relations)
    placeholders = ", ".join("?" for _ in relation_values)
    matches = database.execute(
        f"""SELECT task_id FROM managed_contexts
        WHERE suite_id = ? AND host = ? AND session_id = ?
          AND (root_request_id IN ({placeholders}) OR request_id IN ({placeholders}))
          AND context_key != ? AND task_id IS NOT NULL""",
        (
            event["suite_id"],
            event["host"],
            event["session_id"],
            *relation_values,
            *relation_values,
            context_key,
        ),
    ).fetchall()
    task_ids = {str(row["task_id"]) for row in matches}
    if len(task_ids) != 1:
        if len(task_ids) > 1:
            database.execute(
                "UPDATE managed_contexts SET unknown_change = 1 WHERE context_key = ?",
                (context_key,),
            )
        return
    database.execute(
        "UPDATE managed_contexts SET task_id = ? WHERE context_key = ?",
        (task_ids.pop(), context_key),
    )


def _consume_receipt(
    database: Any,
    *,
    receipt: str,
    installation: dict[str, Any],
    suite_id: str,
    skill: str | None,
    expected_action: str | set[str],
    host_versions: dict[str, frozenset[str]],
) -> Any:
    if not isinstance(receipt, str) or len(receipt) < 24:
        raise SelcError(
            "RECEIPT_UNAVAILABLE",
            "managed receipt is missing",
            "invoke through the installed entry",
        )
    digest = hashlib.sha256(receipt.encode("utf-8")).hexdigest()
    row = database.execute(
        "SELECT * FROM managed_receipts WHERE receipt_digest = ?", (digest,)
    ).fetchone()
    if row is None:
        raise SelcError("RECEIPT_UNAVAILABLE", "managed receipt was not issued by this project")
    return _consume_receipt_row(
        database,
        row=row,
        installation=installation,
        suite_id=suite_id,
        skill=skill,
        expected_action=expected_action,
        host_versions=host_versions,
    )


def _consume_receipt_row(
    database: Any,
    *,
    row: Any,
    installation: dict[str, Any],
    suite_id: str,
    skill: str | None,
    expected_action: str | set[str],
    host_versions: dict[str, frozenset[str]],
) -> Any:
    """Consume one preselected receipt after checking every durable binding."""

    if row["status"] == "consumed":
        raise SelcError("RECEIPT_ALREADY_CONSUMED", "managed receipt was already consumed")
    if row["status"] != "ready":
        raise SelcError("RECEIPT_INVALID", "managed receipt is no longer valid")
    if row["expires_at"] < records.utc_now():
        database.execute(
            "UPDATE managed_receipts SET status = 'expired', invalidated_at = ? WHERE receipt_digest = ?",
            (records.utc_now(), row["receipt_digest"]),
        )
        raise SelcError(
            "RECEIPT_EXPIRED", "managed receipt has expired", "invoke the installed entry again"
        )
    expected_actions = {expected_action} if isinstance(expected_action, str) else expected_action
    if (
        row["installation_digest"] != installation["digest"]
        or row["suite_id"] != suite_id
        or row["action"] not in expected_actions
        or (skill is not None and row["skill"] != skill)
    ):
        raise SelcError("RECEIPT_INVALID", "receipt does not bind this installation and operation")
    if not _receipt_is_fixture(database, row):
        allowed_versions = host_versions.get(str(row["host"]), frozenset())
        if str(row["host_version"]) not in allowed_versions:
            raise SelcError(
                "NATIVE_VERSION_UNVERIFIED",
                "the current native host version is not declared by this installation",
                "pause before business execution and install a suite that declares this host version",
            )
    if _native_request_is_terminated(
        database,
        suite_id=str(row["suite_id"]),
        host=str(row["host"]),
        session_id=str(row["session_id"]),
        generation_id=str(row["generation_id"]),
        request_id=str(row["request_id"]),
        tool_use_id=str(row["tool_use_id"]),
    ):
        database.execute(
            "UPDATE managed_receipts SET status = 'invalid', invalidated_at = ? WHERE receipt_digest = ?",
            (records.utc_now(), row["receipt_digest"]),
        )
        raise SelcError(
            "RECEIPT_INVALID",
            "the native request or tool boundary already terminated",
            "wait for a new verified native request before starting managed work",
        )
    context = _context_for_receipt(database, row)
    _assert_correction_binding_current(database, context, installation)
    database.execute(
        "UPDATE managed_receipts SET status = 'consumed', consumed_at = ? WHERE receipt_digest = ?",
        (records.utc_now(), row["receipt_digest"]),
    )
    return row


def _native_request_is_terminated(
    database: Any,
    *,
    suite_id: str,
    host: str,
    session_id: str,
    generation_id: str,
    request_id: str,
    tool_use_id: str,
) -> bool:
    """Fence a late Pre against its exact Post or the request's durable Stop."""

    row = database.execute(
        """SELECT 1 FROM managed_native_events
        WHERE suite_id = ? AND host = ? AND session_id = ? AND generation_id = ?
          AND request_id = ?
          AND (
              phase = ?
              OR (phase = ? AND tool_use_id = ?)
          )
        LIMIT 1""",
        (
            suite_id,
            host,
            session_id,
            generation_id,
            request_id,
            STOP_PHASE,
            POST_PHASE,
            tool_use_id,
        ),
    ).fetchone()
    return row is not None


def _context_for_receipt(database: Any, receipt: Any) -> Any:
    row = database.execute(
        """SELECT * FROM managed_contexts
        WHERE suite_id = ? AND host = ? AND session_id = ? AND generation_id = ? AND request_id = ?""",
        (
            receipt["suite_id"],
            receipt["host"],
            receipt["session_id"],
            receipt["generation_id"],
            receipt["request_id"],
        ),
    ).fetchone()
    if row is None:
        raise SelcError("ROOT_PROOF_UNAVAILABLE", "native request context was not committed")
    return row


def _assert_receipt_subject(database: Any, receipt: Any, task_id: str, call_id: str) -> Any:
    """Bind consumption to the exact native activation, including its call.

    The context chooses the business task, while the activation receipt records
    the particular call that owns a native tool boundary.  Checking only the
    task would let a later correction call spend a receipt for an older call.
    """

    context = _context_for_receipt(database, receipt)
    if str(context["task_id"] or "") != task_id or str(receipt["call_id"] or "") != call_id:
        raise SelcError(
            "RECEIPT_SUBJECT_MISMATCH",
            "managed receipt belongs to another task or activation call",
            "invoke the operation from the exact native activation that issued the receipt",
        )
    return context


def _insert_managed_task(
    database: Any, *, task_id: str, suite_id: str, version: str, host: str, is_test: bool
) -> None:
    origin = database.execute("SELECT value FROM meta WHERE key = 'origin_id'").fetchone()[0]
    database.execute(
        """INSERT INTO tasks(
        task_id, suite_id, suite_version, harness, initiator, evidence, is_test, status,
        created_at, owner_origin, assessment_owner)
        VALUES (?, ?, ?, ?, 'unknown', 'managed-native', ?, 'active', ?, ?, ?)""",
        (task_id, suite_id, version, host, int(is_test), records.utc_now(), origin, MANAGED_OWNER),
    )


def _insert_managed_call(
    database: Any,
    *,
    call_id: str,
    task_id: str,
    skill: str,
    host: str,
    installation: dict[str, Any],
    trigger_kind: str,
) -> None:
    database.execute(
        """INSERT INTO calls(
        call_id, task_id, skill, executor, actual_harness, evidence, expected_version, expected_hash,
        trigger_kind, status, created_at, assessment_owner)
        VALUES (?, ?, ?, ?, ?, 'managed-native', ?, ?, ?, 'active', ?, ?)""",
        (
            call_id,
            task_id,
            skill,
            host,
            host,
            installation["version"],
            installation["digest"],
            trigger_kind,
            records.utc_now(),
            MANAGED_OWNER,
        ),
    )


def _confirm_context_selection(database: Any, context: Any, skill: str) -> str:
    """Freeze the selected skill when an activation receipt is actually consumed."""

    if context["input_skill"]:
        if context["input_skill"] != skill:
            raise SelcError(
                "MANAGED_CONTEXT_INVALID",
                "the activated skill differs from the user-selected skill",
            )
        original_selector = _context_immutable_prompt_selector(database, context)
        if original_selector is None:
            return "unknown"
        if original_selector != skill:
            raise SelcError(
                "MANAGED_CONTEXT_INVALID",
                "the activated skill differs from the immutable user-selected skill",
            )
        return "explicit"
    if not _context_uses_automatic_selection(context):
        selected_skill = context["selected_skill"]
        if selected_skill is not None and selected_skill != skill:
            raise SelcError(
                "MANAGED_CONTEXT_INVALID",
                "the activated skill differs from the prior unknown selection",
            )
        database.execute(
            """UPDATE managed_contexts SET selected_skill = ?
            WHERE context_key = ? AND selected_skill IS NULL""",
            (skill, context["context_key"]),
        )
        return "unknown"
    selected_skill = context["selected_skill"]
    if selected_skill is not None and selected_skill != skill:
        raise SelcError(
            "MANAGED_CONTEXT_INVALID",
            "the activated skill differs from the fixed automatic selection",
        )
    database.execute(
        """UPDATE managed_contexts SET selected_skill = ?
        WHERE context_key = ? AND selected_skill IS NULL""",
        (skill, context["context_key"]),
    )
    selected = database.execute(
        "SELECT selected_skill FROM managed_contexts WHERE context_key = ?",
        (context["context_key"],),
    ).fetchone()
    if selected is None or selected["selected_skill"] != skill:
        raise SelcError(
            "MANAGED_CONTEXT_INVALID",
            "another activation reserved this automatic request for a different skill",
        )
    return "automatic"


def _assert_context_selected_skill(context: Any, skill: str) -> None:
    """Keep later action and staging receipts on the activation's fixed skill."""

    if context["input_skill"]:
        if context["input_skill"] != skill:
            raise SelcError(
                "MANAGED_CONTEXT_INVALID",
                "managed action differs from the user-selected skill",
            )
        return
    if context["selected_skill"] != skill:
        raise SelcError(
            "MANAGED_CONTEXT_INVALID",
            "managed action differs from the activated skill selection",
        )


def _assert_context_action_business_dto(context: Any, request: dict[str, object]) -> None:
    """Independently compare an automatic action's DTO with its frozen Prompt goal."""

    if not _context_uses_automatic_selection(context):
        return
    if _business_dto_goal_digest(request) != context["business_dto_digest"]:
        raise SelcError(
            "RECEIPT_ARGUMENT_MISMATCH",
            "managed action DTO differs from the native automatic request",
            "invoke the exact structured business DTO selected by the native request",
        )


def _assert_managed_call(
    database: Any,
    task_id: str,
    call_id: str,
    suite_id: str,
    skill: str | None,
    installation: dict[str, Any],
) -> None:
    row = database.execute(
        """SELECT tasks.assessment_owner, tasks.suite_id, tasks.status AS task_status,
        calls.skill, calls.status AS call_status, calls.expected_version, calls.expected_hash
        FROM tasks JOIN calls ON calls.task_id = tasks.task_id
        WHERE tasks.task_id = ? AND calls.call_id = ?""",
        (task_id, call_id),
    ).fetchone()
    if row is None or row["assessment_owner"] != MANAGED_OWNER or row["suite_id"] != suite_id:
        raise SelcError(
            "MANAGED_CONTEXT_INVALID", "managed task and call do not belong to this suite"
        )
    if row["task_status"] != "active" or row["call_status"] != "active":
        raise SelcError(
            "MANAGED_CONTEXT_CLOSED",
            "managed action subject is no longer active",
            "start a new managed invocation after closing or deleting the prior subject",
        )
    if skill is not None and row["skill"] != skill:
        raise SelcError(
            "MANAGED_CONTEXT_INVALID", "managed action does not match the activated skill"
        )
    if (
        row["expected_version"] != installation["version"]
        or row["expected_hash"] != installation["digest"]
    ):
        raise SelcError(
            "INSTALLATION_IDENTITY_DRIFT", "managed call belongs to another installation"
        )


def _validate_action_request(
    action: dict[str, object], request: dict[str, object]
) -> dict[str, object]:
    if not isinstance(request, dict):
        raise SelcError("MANAGED_INPUT_INVALID", "managed action request must be an object")
    schema = action["input"]
    if not isinstance(schema, dict):
        raise SelcError("MANAGED_INPUT_INVALID", "managed action schema is invalid")
    fields = schema["fields"]
    required = schema["required"]
    if set(request) != set(fields) or not all(field in request for field in required):
        raise SelcError(
            "MANAGED_INPUT_INVALID", "managed action request must match the declared schema"
        )
    normalized: dict[str, object] = {}
    for name, specification in fields.items():
        value = request[name]
        kind = specification["type"]
        if kind == "path":
            if not isinstance(value, str):
                raise SelcError("MANAGED_INPUT_INVALID", f"managed input {name} must be a path")
            normalized[name] = value
        elif (
            kind == "string"
            and isinstance(value, str)
            or kind == "integer"
            and type(value) is int
            or kind == "boolean"
            and type(value) is bool
        ):
            normalized[name] = value
        else:
            raise SelcError("MANAGED_INPUT_INVALID", f"managed input {name} has the wrong type")
        allowed = specification.get("enum")
        if allowed is not None and normalized[name] not in allowed:
            raise SelcError(
                "MANAGED_INPUT_INVALID", f"managed input {name} is outside its declared enum"
            )
    return normalized


def _action_staging_root(project_root: Path, suite_id: str, action_run_id: str) -> Path:
    root = project_root / ".selc" / "managed-staging" / suite_id / action_run_id
    root.mkdir(parents=True, exist_ok=False)
    return root


def _execute_declared_action(
    *,
    suite_root: Path,
    action: dict[str, object],
    request: dict[str, object],
    staging: Path,
    frozen_inputs: dict[str, bytes],
    on_execution_started: Callable[[ProcessIdentity, Path], None] | None = None,
    on_execution_finished: Callable[[], None] | None = None,
) -> tuple[Path, str]:
    if action["kind"] == "instruction-staging-v1":
        raise SelcError(
            "MANAGED_INSTRUCTION_STAGING_REQUIRED",
            "instruction-generated artifacts must be staged by the managed instruction helper",
        )
    script_name = action["script"]
    if not isinstance(script_name, str):
        raise SelcError("MANAGED_SCRIPT_UNAVAILABLE", "managed fixed script is unavailable")
    script = suite_root / script_name
    try:
        script.resolve(strict=True).relative_to(suite_root.resolve())
    except (OSError, ValueError) as error:
        raise SelcError(
            "MANAGED_SCRIPT_UNAVAILABLE", "managed script escapes the installed suite"
        ) from error
    if script.is_symlink() or not script.is_file():
        raise SelcError("MANAGED_SCRIPT_UNAVAILABLE", "managed script is unavailable")
    artifact = staging / "artifact"
    kind = str(action["kind"])
    if kind == "text-report-v1":
        input_value = request.get("input")
        if not isinstance(input_value, str):
            raise SelcError(
                "MANAGED_INPUT_INVALID", "the installed text action needs an input path"
            )
        input_bytes = frozen_inputs.get("input")
        if input_bytes is None:
            raise SelcError("MANAGED_INPUT_INVALID", "the managed input snapshot is unavailable")
        staged_input = staging / "input"
        _write_fsynced(staged_input, input_bytes)
        completed = _run_managed_subprocess(
            [
                sys.executable,
                str(script),
                "run",
                "text-report",
                "--input",
                str(staged_input),
                "--output",
                str(artifact),
                "--suite",
                str(suite_root),
            ],
            staging=staging,
            on_execution_started=on_execution_started,
            on_execution_finished=on_execution_finished,
        )
        if completed.returncode != 0 or not artifact.is_file() or artifact.is_symlink():
            raise SelcError("MANAGED_ACTION_FAILED", "fixed installed text-report action failed")
    elif kind == "python-script-v1":
        staged_request = _stage_fixed_action_inputs(action, request, staging, frozen_inputs)
        request_path = staging / "request.json"
        _write_fsynced(request_path, canonical_json(staged_request).encode("utf-8"))
        completed = _run_managed_subprocess(
            [
                sys.executable,
                str(script),
                "managed-run",
                "--request",
                str(request_path),
                "--output",
                str(artifact),
            ],
            staging=staging,
            on_execution_started=on_execution_started,
            on_execution_finished=on_execution_finished,
        )
        if completed.returncode != 0 or not artifact.is_file() or artifact.is_symlink():
            raise SelcError("MANAGED_ACTION_FAILED", "fixed installed managed script failed")
    else:
        raise SelcError("MANAGED_ACTION_UNDECLARED", "managed action kind is unsupported")
    output_bytes = artifact.read_bytes()
    if len(output_bytes) > MAX_INPUT_BYTES:
        raise SelcError(
            "MANAGED_ACTION_FAILED", "managed action artifact exceeds the supported bound"
        )
    return artifact, hashlib.sha256(output_bytes).hexdigest()


def _record_execution_started(
    database: Any,
    action_run_id: str,
    suite_id: str,
    identity: ProcessIdentity,
    heartbeat_path: Path,
) -> None:
    """Commit the runner identity before it receives permission to exec business code."""

    with records.projection_transaction(database):
        database.execute(
            """INSERT INTO managed_executions(
            action_run_id, suite_id, pid, process_group, started_identity, heartbeat_path,
            state, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?)""",
            (
                action_run_id,
                suite_id,
                identity.pid,
                identity.process_group,
                identity.started,
                str(heartbeat_path),
                records.utc_now(),
                records.utc_now(),
            ),
        )


def _record_execution_finished(database: Any, action_run_id: str) -> None:
    """Record a supervisor exit without treating it as process-tree termination."""

    with records.projection_transaction(database):
        database.execute(
            "UPDATE managed_executions SET updated_at = ? WHERE action_run_id = ?",
            (records.utc_now(), action_run_id),
        )


def _run_managed_subprocess(
    arguments: list[str],
    *,
    staging: Path,
    on_execution_started: Callable[[ProcessIdentity, Path], None] | None,
    on_execution_finished: Callable[[], None] | None,
) -> subprocess.CompletedProcess[bytes]:
    """Start a waiting runner, durably identify it, then release its fixed argv.

    The runner never executes the business script until its parent has committed
    the process identity.  If the runtime dies before release, the runner times
    out without starting business work.  Once released, it supervises the fixed
    child in its own process group and writes a short-lived handshake heartbeat.
    That heartbeat remains live after a parent runtime crash, so an advisory lock
    release can never be mistaken for the end of the business process tree.
    """

    ready_path = staging / ".runner-ready"
    release_path = staging / ".runner-release"
    heartbeat_path = staging / ".runner-heartbeat"
    config_path = staging / ".runner-config.json"
    runner_path = staging / ".managed-runner.py"
    release_token = secrets.token_urlsafe(24)
    _write_fsynced(
        config_path,
        canonical_json(
            {
                "argv": arguments,
                "ready": str(ready_path),
                "release": str(release_path),
                "heartbeat": str(heartbeat_path),
                "token": release_token,
            }
        ).encode("utf-8"),
    )
    _write_fsynced(runner_path, _MANAGED_RUNNER_SOURCE.encode("utf-8"))
    runner = subprocess.Popen(
        [sys.executable, str(runner_path), str(config_path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    identity: ProcessIdentity | None = None
    released = False
    try:
        deadline = time.monotonic() + 2
        while not ready_path.exists() and runner.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        if not ready_path.is_file() or runner.poll() is not None:
            raise SelcError(
                "MANAGED_EXECUTION_UNAVAILABLE",
                "managed runner did not complete its pre-execution handshake",
            )
        identity = inspect_process(runner.pid, release_token)
        if identity is None:
            raise SelcError(
                "MANAGED_EXECUTION_UNAVAILABLE",
                "managed runner ended before its identity could be recorded",
            )
        if on_execution_started is not None:
            on_execution_started(identity, heartbeat_path)
        _write_fsynced(release_path, release_token.encode("utf-8"))
        released = True
        try:
            returncode = runner.wait(timeout=10)
        except subprocess.TimeoutExpired as error:
            _terminate_managed_process_group(identity)
            with suppress(subprocess.TimeoutExpired):
                runner.wait(timeout=1)
            raise SelcError(
                "MANAGED_ACTION_TIMEOUT",
                "managed action exceeded its fixed execution timeout",
            ) from error
        if process_group_is_live(identity.process_group):
            raise SelcError(
                "MANAGED_EXECUTION_UNCERTAIN",
                "managed runner exited while its process group remains live",
                "pause managed work until the retained execution group has terminated",
            )
        return subprocess.CompletedProcess(arguments, returncode, b"", b"")
    finally:
        if not released and runner.poll() is None:
            _terminate_managed_process_group(identity)
        if on_execution_finished is not None and identity is not None and runner.poll() is not None:
            on_execution_finished()


def _terminate_managed_process_group(identity: ProcessIdentity | None) -> None:
    if identity is None:
        return
    try:
        os.killpg(identity.process_group, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 1
    while process_group_is_live(identity.process_group) and time.monotonic() < deadline:
        time.sleep(0.02)
    if process_group_is_live(identity.process_group):
        try:
            os.killpg(identity.process_group, signal.SIGKILL)
        except ProcessLookupError:
            return
    deadline = time.monotonic() + 1
    while process_group_is_live(identity.process_group) and time.monotonic() < deadline:
        time.sleep(0.02)
    if process_group_is_live(identity.process_group):
        raise SelcError(
            "MANAGED_EXECUTION_UNCERTAIN",
            "managed process group remained live after bounded termination",
            "pause managed work and inspect the retained execution record",
        )


_MANAGED_RUNNER_SOURCE = """\
import json
import os
import subprocess
import sys
import time
from pathlib import Path

config = json.loads(Path(sys.argv[1]).read_text(encoding=\"utf-8\"))
ready = Path(config[\"ready\"])
release = Path(config[\"release\"])
heartbeat = Path(config[\"heartbeat\"])

def write_synced(path, value):
    descriptor = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.write(descriptor, value)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

write_synced(ready, b\"ready\")
deadline = time.monotonic() + 10
while time.monotonic() < deadline:
    try:
        if release.read_text(encoding=\"utf-8\") == config[\"token\"]:
            child = subprocess.Popen(
                config[\"argv\"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            while child.poll() is None:
                value = f\"{config['token']} running {time.time()}\".encode(\"utf-8\")
                write_synced(heartbeat, value)
                time.sleep(0.05)
            sys.exit(child.returncode)
    except FileNotFoundError:
        pass
    time.sleep(0.01)
sys.exit(75)
"""


def _read_managed_input(source: Path) -> bytes:
    if source.is_symlink() or not source.is_file():
        raise SelcError("MANAGED_INPUT_INVALID", "managed input must be a regular readable file")
    input_bytes = source.read_bytes()
    if len(input_bytes) > MAX_INPUT_BYTES:
        raise SelcError("MANAGED_INPUT_TOO_LARGE", "managed input exceeds the supported bound")
    return input_bytes


def _freeze_declared_action_inputs(
    action: dict[str, object], request: dict[str, object]
) -> dict[str, bytes]:
    """Read every declared path input exactly once before business execution.

    The snapshot is both the correction comparison value and the only source
    that reaches the managed script.  A caller can therefore never change a
    path between validation and staging to alter the business subprocess.
    """

    schema = action["input"]
    if not isinstance(schema, dict) or not isinstance(schema.get("fields"), dict):
        raise SelcError("MANAGED_INPUT_INVALID", "managed action schema is invalid")
    frozen: dict[str, bytes] = {}
    for field, specification in schema["fields"].items():
        if not isinstance(specification, dict) or specification.get("type") != "path":
            continue
        value = request.get(field)
        if not isinstance(value, str):
            raise SelcError("MANAGED_INPUT_INVALID", f"managed input {field} must be a path")
        frozen[str(field)] = _read_managed_input(Path(value))
    return frozen


def _stage_fixed_action_inputs(
    action: dict[str, object],
    request: dict[str, object],
    staging: Path,
    frozen_inputs: dict[str, bytes],
) -> dict[str, object]:
    """Copy declared path values before launching a fixed generic script."""

    schema = action["input"]
    if not isinstance(schema, dict) or not isinstance(schema.get("fields"), dict):
        raise SelcError("MANAGED_INPUT_INVALID", "managed action schema is invalid")
    staged = dict(request)
    for field, specification in schema["fields"].items():
        if not isinstance(specification, dict) or specification.get("type") != "path":
            continue
        value = request.get(field)
        if not isinstance(value, str):
            raise SelcError("MANAGED_INPUT_INVALID", f"managed input {field} must be a path")
        private_path = staging / "input" if field == "input" else staging / "inputs" / str(field)
        input_bytes = frozen_inputs.get(str(field))
        if input_bytes is None:
            raise SelcError("MANAGED_INPUT_INVALID", "the managed input snapshot is unavailable")
        _write_fsynced(private_path, input_bytes)
        staged[str(field)] = str(private_path)
    return staged


def _safe_target_path(project_root: Path, target: str) -> Path:
    relative = Path(target)
    if not target or relative.is_absolute() or ".." in relative.parts:
        raise SelcError(
            "PUBLICATION_TARGET_INVALID", "publication target must be a project-relative path"
        )
    candidate = project_root / relative
    parent = candidate.parent
    parent.mkdir(parents=True, exist_ok=True)
    try:
        parent.resolve().relative_to(project_root.resolve())
    except ValueError as error:
        raise SelcError(
            "PUBLICATION_TARGET_INVALID", "publication target escapes the project"
        ) from error
    return candidate


def _finished_action_for_artifact(
    database: Any, task_id: str, call_id: str, artifact_path: Path
) -> Any:
    row = database.execute(
        """SELECT * FROM managed_actions WHERE task_id = ? AND call_id = ? AND artifact_path = ?
        AND status = 'finished'""",
        (task_id, call_id, str(artifact_path)),
    ).fetchone()
    if row is None:
        raise SelcError(
            "MANAGED_ACTION_UNAVAILABLE",
            "publication artifact was not produced by this managed call",
        )
    return row


def _assert_no_pending_action(database: Any, task_id: str) -> None:
    """Fence a task while a prior action can still publish visible bytes."""

    active = database.execute(
        """SELECT action_run_id FROM managed_actions
        WHERE task_id = ? AND status IN ('reserved', 'finished') LIMIT 1""",
        (task_id,),
    ).fetchone()
    if active is not None:
        raise SelcError(
            "MANAGED_ACTION_FENCED",
            "an earlier managed action is still eligible to publish",
            "publish, fail, or recover the earlier action before starting another",
        )


def _assert_no_inflight_publication(database: Any, task_id: str) -> None:
    """Require recovery before a later publication can race an earlier worker."""

    active = database.execute(
        """SELECT delivery_id FROM managed_publications
        WHERE task_id = ? AND status IN ('reserved', 'prepared', 'supplied') LIMIT 1""",
        (task_id,),
    ).fetchone()
    if active is not None:
        raise SelcError(
            "PUBLICATION_FENCED",
            "an earlier managed publication has no durable terminal state",
            "recover the earlier publication before starting a later one",
        )


def _reserve_publication(
    database: Any,
    *,
    suite_id: str,
    task_id: str,
    call_id: str,
    receipt_digest: str,
    action_run_id: str,
    artifact_digest: str,
    target_path: Path,
) -> tuple[str, int, int | None]:
    counter = database.execute(
        "SELECT next_ordinal FROM managed_publication_counters WHERE task_id = ?", (task_id,)
    ).fetchone()
    ordinal = int(counter["next_ordinal"]) if counter is not None else 1
    predecessor = ordinal - 1 if ordinal > 1 else None
    if counter is None:
        database.execute(
            "INSERT INTO managed_publication_counters(task_id, next_ordinal) VALUES (?, ?)",
            (task_id, ordinal + 1),
        )
    else:
        database.execute(
            "UPDATE managed_publication_counters SET next_ordinal = ? WHERE task_id = ?",
            (ordinal + 1, task_id),
        )
    delivery_id = f"managed-delivery-{secrets.token_hex(12)}"
    database.execute(
        """INSERT INTO managed_publications(
        delivery_id, suite_id, task_id, call_id, receipt_digest, action_run_id, ordinal,
        predecessor_ordinal, artifact_digest, staging_path, target_path, status, detail, reserved_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, 'reserved', '{}', ?)""",
        (
            delivery_id,
            suite_id,
            task_id,
            call_id,
            receipt_digest,
            action_run_id,
            ordinal,
            predecessor,
            artifact_digest,
            str(target_path),
            records.utc_now(),
        ),
    )
    return delivery_id, ordinal, predecessor


def _publication_staging_path(project_root: Path, suite_id: str, delivery_id: str) -> Path:
    root = project_root / ".selc" / "managed-publications" / suite_id
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{delivery_id}.prepared"


def _prepare_publication_bytes(
    artifact_path: Path, staged_path: Path, *, expected_digest: str
) -> None:
    if artifact_path.is_symlink() or not artifact_path.is_file():
        raise SelcError(
            "MANAGED_ACTION_UNAVAILABLE", "managed artifact is unavailable for publication"
        )
    artifact_bytes = artifact_path.read_bytes()
    if hashlib.sha256(artifact_bytes).hexdigest() != expected_digest:
        raise SelcError(
            "MANAGED_ACTION_UNAVAILABLE",
            "managed action artifact changed after the fixed action completed",
            "run a new managed action before publication",
        )
    _write_fsynced(staged_path, artifact_bytes)


def _owned_target_digest(database: Any, task_id: str, target_path: Path) -> str | None:
    """Return a recorded or durably exchanged owned target identity.

    An exchange can finish before its delivery row is recorded.  Its intent is
    sufficient to recognise the new visible object for a later correction only
    after recovery leaves the interrupted publication uncertain; it never
    upgrades that prior assessment to recorded or pass.
    """

    intent = database.execute(
        """SELECT intents.staged_digest, intents.staged_device, intents.staged_inode
        FROM managed_publication_intents AS intents
        LEFT JOIN managed_publications AS publications
          ON publications.delivery_id = intents.delivery_id
        WHERE intents.task_id = ? AND intents.target_path = ?
          AND intents.state IN ('prepared', 'uncertain', 'recorded')
        ORDER BY COALESCE(publications.ordinal, -1) DESC, intents.rowid DESC LIMIT 1""",
        (task_id, str(target_path)),
    ).fetchone()
    if intent is None or target_path.is_symlink() or not target_path.is_file():
        return None
    target_stat = target_path.stat()
    if (
        target_stat.st_dev != int(intent["staged_device"])
        or target_stat.st_ino != int(intent["staged_inode"])
        or file_hash(target_path) != str(intent["staged_digest"])
    ):
        return None
    return str(intent["staged_digest"])


def _record_publication_intent(
    database: Any,
    *,
    delivery_id: str,
    suite_id: str,
    task_id: str,
    staged_path: Path,
    target_path: Path,
    previous_owned_digest: str | None,
) -> dict[str, object]:
    """Commit the exact exchange inputs before making a visible filesystem change."""

    if staged_path.is_symlink() or not staged_path.is_file():
        raise SelcError("PUBLICATION_FENCED", "prepared publication bytes are unavailable")
    staged_stat = staged_path.stat()
    staged_parent_stat = staged_path.parent.stat()
    target_parent_stat = target_path.parent.stat()
    staged_digest = file_hash(staged_path)
    previous: dict[str, object] = {
        "digest": previous_owned_digest,
        "device": None,
        "inode": None,
    }
    if target_path.exists() or target_path.is_symlink():
        if (
            previous_owned_digest is None
            or target_path.is_symlink()
            or not target_path.is_file()
            or file_hash(target_path) != previous_owned_digest
        ):
            raise SelcError(
                "PUBLICATION_TARGET_CONFLICT", "publication target has external or changed bytes"
            )
        target_stat = target_path.stat()
        previous["device"] = target_stat.st_dev
        previous["inode"] = target_stat.st_ino
    now = records.utc_now()
    database.execute(
        """INSERT INTO managed_publication_intents(
        delivery_id, suite_id, task_id, target_path, staged_path, previous_digest,
        previous_device, previous_inode, staged_digest, staged_device, staged_inode,
        target_parent_device, target_parent_inode, staging_parent_device, staging_parent_inode,
        state, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'prepared', ?, ?)
        ON CONFLICT(delivery_id) DO UPDATE SET
          previous_digest = excluded.previous_digest,
          previous_device = excluded.previous_device,
          previous_inode = excluded.previous_inode,
          staged_digest = excluded.staged_digest,
          staged_device = excluded.staged_device,
          staged_inode = excluded.staged_inode,
          target_parent_device = excluded.target_parent_device,
          target_parent_inode = excluded.target_parent_inode,
          staging_parent_device = excluded.staging_parent_device,
          staging_parent_inode = excluded.staging_parent_inode,
          state = 'prepared', updated_at = excluded.updated_at""",
        (
            delivery_id,
            suite_id,
            task_id,
            str(target_path),
            str(staged_path),
            previous["digest"],
            previous["device"],
            previous["inode"],
            staged_digest,
            staged_stat.st_dev,
            staged_stat.st_ino,
            target_parent_stat.st_dev,
            target_parent_stat.st_ino,
            staged_parent_stat.st_dev,
            staged_parent_stat.st_ino,
            now,
            now,
        ),
    )
    return {
        "delivery_id": delivery_id,
        "previous_digest": previous["digest"],
        "previous_device": previous["device"],
        "previous_inode": previous["inode"],
        "staged_digest": staged_digest,
        "staged_device": staged_stat.st_dev,
        "staged_inode": staged_stat.st_ino,
        "target_parent_device": target_parent_stat.st_dev,
        "target_parent_inode": target_parent_stat.st_ino,
        "staging_parent_device": staged_parent_stat.st_dev,
        "staging_parent_inode": staged_parent_stat.st_ino,
    }


def _supply_publication(
    staged_path: Path,
    target_path: Path,
    *,
    intent: dict[str, object] | None = None,
    previous_owned_digest: str | None = None,
) -> None:
    """Install prepared bytes using no-clobber creation or Darwin exchange.

    A new target uses hard-link creation, whose ``EEXIST`` result preserves an
    intervening user file.  A verified owned target uses ``RENAME_SWAP`` on
    Darwin: the displaced object remains at ``staged_path`` for independent
    identity and digest verification before it is removed.  It is an exchange,
    not a compare-and-swap primitive; a final user race becomes uncertain and
    leaves the displaced bytes available for recovery.
    """

    if intent is None:
        intent = _legacy_publication_intent(staged_path, target_path, previous_owned_digest)
    _assert_staged_identity(staged_path, intent)
    target_exists = target_path.exists() or target_path.is_symlink()
    if not target_exists:
        _create_publication_target(staged_path, target_path, intent)
        return
    _exchange_owned_publication(staged_path, target_path, intent)


def _legacy_publication_intent(
    staged_path: Path, target_path: Path, previous_owned_digest: str | None
) -> dict[str, object]:
    """Retain the narrow direct helper used by deterministic filesystem tests."""

    target_path.parent.mkdir(parents=True, exist_ok=True)
    staged_stat = staged_path.stat()
    staged_parent_stat = staged_path.parent.stat()
    target_parent_stat = target_path.parent.stat()
    previous_device: int | None = None
    previous_inode: int | None = None
    if target_path.exists() and target_path.is_file() and not target_path.is_symlink():
        target_stat = target_path.stat()
        previous_device = target_stat.st_dev
        previous_inode = target_stat.st_ino
    return {
        "previous_digest": previous_owned_digest,
        "previous_device": previous_device,
        "previous_inode": previous_inode,
        "staged_digest": file_hash(staged_path),
        "staged_device": staged_stat.st_dev,
        "staged_inode": staged_stat.st_ino,
        "target_parent_device": target_parent_stat.st_dev,
        "target_parent_inode": target_parent_stat.st_ino,
        "staging_parent_device": staged_parent_stat.st_dev,
        "staging_parent_inode": staged_parent_stat.st_ino,
    }


def _assert_staged_identity(staged_path: Path, intent: dict[str, object]) -> None:
    if staged_path.is_symlink() or not staged_path.is_file():
        raise SelcError("PUBLICATION_FENCED", "prepared publication bytes are unavailable")
    stat = staged_path.stat()
    if (
        stat.st_dev != intent["staged_device"]
        or stat.st_ino != intent["staged_inode"]
        or file_hash(staged_path) != intent["staged_digest"]
    ):
        raise SelcError("PUBLICATION_FENCED", "prepared publication bytes changed before supply")


def _open_verified_publication_directory(
    path: Path, intent: dict[str, object], *, label: str
) -> int:
    """Open a publication directory and bind it to the durable intent."""

    if label == "target":
        expected_device = intent.get("target_parent_device")
        expected_inode = intent.get("target_parent_inode")
    else:
        expected_device = intent.get("staging_parent_device")
        expected_inode = intent.get("staging_parent_inode")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as error:
        raise SelcError(
            "PUBLICATION_TARGET_CONFLICT",
            f"{label} publication directory is unavailable",
        ) from error
    directory_stat = os.fstat(descriptor)
    if (
        expected_device is None
        or expected_inode is None
        or directory_stat.st_dev != int(expected_device)
        or directory_stat.st_ino != int(expected_inode)
    ):
        os.close(descriptor)
        raise SelcError(
            "PUBLICATION_TARGET_CONFLICT",
            f"{label} publication directory changed before supply",
        )
    return descriptor


def _regular_file_identity_at(directory: int, name: str) -> tuple[os.stat_result, str]:
    """Hash one regular non-symlink file through an already checked directory."""

    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise SelcError("PUBLICATION_TARGET_CONFLICT", "publication object is not a file")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 131_072)
            if not chunk:
                break
            digest.update(chunk)
        return file_stat, digest.hexdigest()
    finally:
        os.close(descriptor)


def _fsync_file_descriptor(directory: int, name: str) -> None:
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory_descriptor(descriptor: int) -> None:
    try:
        os.fsync(descriptor)
    except OSError as error:
        raise SelcError(
            "PUBLICATION_ATOMIC_UNAVAILABLE",
            "publication filesystem cannot durably sync its directory",
        ) from error


def _visible_target_matches_directory(
    target_path: Path,
    target_directory: int,
    target_name: str,
) -> bool:
    """Verify that the caller's requested path still names the opened directory.

    Directory descriptors prevent the exchange itself from following a late
    symlink.  They do not prove that the user-visible path still reaches that
    descriptor after a concurrent rename.  Recording a success in that case
    would claim publication at a path that now resolves elsewhere.
    """

    try:
        directory_stat = os.fstat(target_directory)
        visible_parent = target_path.parent.stat()
        visible_target = target_path.stat()
        descriptor_target, _digest = _regular_file_identity_at(target_directory, target_name)
    except OSError:
        return False
    return (
        visible_parent.st_dev == directory_stat.st_dev
        and visible_parent.st_ino == directory_stat.st_ino
        and visible_target.st_dev == descriptor_target.st_dev
        and visible_target.st_ino == descriptor_target.st_ino
    )


def _create_publication_target(
    staged_path: Path, target_path: Path, intent: dict[str, object]
) -> None:
    target_directory = _open_verified_publication_directory(
        target_path.parent,
        intent,
        label="target",
    )
    staged_directory = _open_verified_publication_directory(
        staged_path.parent,
        intent,
        label="staging",
    )
    try:
        os.link(
            staged_path.name,
            target_path.name,
            src_dir_fd=staged_directory,
            dst_dir_fd=target_directory,
            follow_symlinks=False,
        )
        target_stat, target_digest = _regular_file_identity_at(target_directory, target_path.name)
        if (
            target_stat.st_dev != intent["staged_device"]
            or target_stat.st_ino != intent["staged_inode"]
            or target_digest != intent["staged_digest"]
        ):
            raise SelcError("PUBLICATION_EXCHANGE_UNCERTAIN", "new target identity changed")
        if not _visible_target_matches_directory(target_path, target_directory, target_path.name):
            raise SelcError(
                "PUBLICATION_EXCHANGE_UNCERTAIN",
                "publication target path changed after creation",
            )
        _fsync_file_descriptor(target_directory, target_path.name)
        os.unlink(staged_path.name, dir_fd=staged_directory)
        _fsync_directory_descriptor(target_directory)
        _fsync_directory_descriptor(staged_directory)
    except FileExistsError as error:
        raise SelcError(
            "PUBLICATION_TARGET_CONFLICT", "publication target already exists"
        ) from error
    except OSError as error:
        raise SelcError(
            "PUBLICATION_ATOMIC_UNAVAILABLE",
            "publication filesystem cannot atomically install the staged snapshot",
            "use a same-filesystem project target and recover the reserved publication",
        ) from error
    finally:
        os.close(staged_directory)
        os.close(target_directory)


def _exchange_owned_publication(
    staged_path: Path, target_path: Path, intent: dict[str, object]
) -> None:
    expected_digest = intent.get("previous_digest")
    if (
        not isinstance(expected_digest, str)
        or target_path.is_symlink()
        or not target_path.is_file()
    ):
        raise SelcError(
            "PUBLICATION_TARGET_CONFLICT", "publication target is not a verified owned file"
        )
    target_directory = _open_verified_publication_directory(
        target_path.parent,
        intent,
        label="target",
    )
    staged_directory = _open_verified_publication_directory(
        staged_path.parent,
        intent,
        label="staging",
    )
    try:
        before, before_digest = _regular_file_identity_at(target_directory, target_path.name)
        if (
            before.st_dev != intent.get("previous_device")
            or before.st_ino != intent.get("previous_inode")
            or before_digest != expected_digest
        ):
            raise SelcError(
                "PUBLICATION_TARGET_CONFLICT", "publication target changed before exchange"
            )
        _rename_exchange_at(
            staged_directory,
            staged_path.name,
            target_directory,
            target_path.name,
        )
        _fsync_file_descriptor(target_directory, target_path.name)
        _fsync_file_descriptor(staged_directory, staged_path.name)
        _fsync_directory_descriptor(target_directory)
        _fsync_directory_descriptor(staged_directory)
        target_stat, target_digest = _regular_file_identity_at(target_directory, target_path.name)
        displaced_stat, displaced_digest = _regular_file_identity_at(
            staged_directory, staged_path.name
        )
        target_matches = (
            target_stat.st_dev == intent["staged_device"]
            and target_stat.st_ino == intent["staged_inode"]
            and target_digest == intent["staged_digest"]
        )
        displaced_matches = (
            displaced_stat.st_dev == intent["previous_device"]
            and displaced_stat.st_ino == intent["previous_inode"]
            and displaced_digest == expected_digest
        )
        visible_target = _visible_target_matches_directory(
            target_path,
            target_directory,
            target_path.name,
        )
        if target_matches and displaced_matches and visible_target:
            os.unlink(staged_path.name, dir_fd=staged_directory)
            _fsync_directory_descriptor(staged_directory)
            return
        _preserve_displaced_conflict_at(
            staged_directory,
            staged_path.name,
            target_directory,
            intent,
        )
        raise SelcError(
            "PUBLICATION_EXCHANGE_UNCERTAIN",
            "publication exchange detected external drift; preserved displaced bytes for recovery",
        )
    finally:
        os.close(staged_directory)
        os.close(target_directory)


def _rename_exchange_at(
    staged_directory: int,
    staged_name: str,
    target_directory: int,
    target_name: str,
) -> None:
    if sys.platform != "darwin":
        raise SelcError(
            "PUBLICATION_ATOMIC_UNAVAILABLE",
            "owned publication replacement requires Darwin rename exchange support",
        )
    try:
        libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        renameatx_np = libc.renameatx_np
    except (AttributeError, OSError) as error:
        raise SelcError(
            "PUBLICATION_ATOMIC_UNAVAILABLE",
            "Darwin rename exchange is unavailable on this filesystem",
        ) from error
    renameatx_np.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameatx_np.restype = ctypes.c_int
    rename_swap = 0x00000002
    result = renameatx_np(
        staged_directory,
        os.fsencode(staged_name),
        target_directory,
        os.fsencode(target_name),
        rename_swap,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise SelcError(
            "PUBLICATION_ATOMIC_UNAVAILABLE",
            f"Darwin rename exchange failed with errno {error_number}",
        )


def _preserve_displaced_conflict_at(
    staged_directory: int,
    staged_name: str,
    target_directory: int,
    intent: dict[str, object],
) -> str:
    """Save unknown displaced bytes before any ordinary staging cleanup."""

    _regular_file_identity_at(staged_directory, staged_name)
    conflict_name = ".selc-publication-conflicts"
    try:
        with suppress(FileExistsError):
            os.mkdir(conflict_name, mode=0o700, dir_fd=target_directory)
        conflict_directory = os.open(
            conflict_name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=target_directory,
        )
        try:
            destination = f"{intent.get('delivery_id', 'unknown')}-{secrets.token_hex(8)}"
            os.link(
                staged_name,
                destination,
                src_dir_fd=staged_directory,
                dst_dir_fd=conflict_directory,
                follow_symlinks=False,
            )
            _fsync_file_descriptor(conflict_directory, destination)
            _fsync_directory_descriptor(conflict_directory)
        finally:
            os.close(conflict_directory)
    except (FileExistsError, OSError) as error:
        raise SelcError(
            "PUBLICATION_EXCHANGE_UNCERTAIN",
            "could not preserve displaced publication bytes",
        ) from error
    return destination


def _recover_interrupted_publication_staging(database: Any, delivery_id: str) -> str | None:
    """Resolve an interrupted exchange without repeating its visible swap.

    A swap can finish after the durable intent and before the publication row.
    Recovery never infers success from a digest alone.  It removes a retained
    object only when its device, inode, and digest prove it is the old managed
    target; otherwise it first saves the bytes in the project-visible conflict
    directory and records that location for deletion and audit closure.
    """

    row = database.execute(
        """SELECT target_path, staged_path, previous_digest, previous_device, previous_inode,
        staged_digest, staged_device, staged_inode, target_parent_device, target_parent_inode,
        staging_parent_device, staging_parent_inode
        FROM managed_publication_intents WHERE delivery_id = ?""",
        (delivery_id,),
    ).fetchone()
    if row is None:
        return None
    intent = dict(row)
    target_path = Path(str(intent["target_path"]))
    staged_path = Path(str(intent["staged_path"]))
    try:
        staged_directory = _open_verified_publication_directory(
            staged_path.parent,
            intent,
            label="staging",
        )
    except SelcError:
        if not staged_path.exists() and not staged_path.is_symlink():
            return None
        raise
    try:
        try:
            staged_stat, staged_digest = _regular_file_identity_at(
                staged_directory, staged_path.name
            )
        except FileNotFoundError:
            return None
        previous_matches = (
            intent["previous_digest"] is not None
            and staged_stat.st_dev == intent["previous_device"]
            and staged_stat.st_ino == intent["previous_inode"]
            and staged_digest == intent["previous_digest"]
        )
        staged_matches = (
            staged_stat.st_dev == intent["staged_device"]
            and staged_stat.st_ino == intent["staged_inode"]
            and staged_digest == intent["staged_digest"]
        )
        if previous_matches or staged_matches:
            os.unlink(staged_path.name, dir_fd=staged_directory)
            _fsync_directory_descriptor(staged_directory)
            return None
        target_directory = _open_verified_publication_directory(
            target_path.parent,
            intent,
            label="target",
        )
        try:
            destination = _preserve_displaced_conflict_at(
                staged_directory,
                staged_path.name,
                target_directory,
                intent,
            )
        finally:
            os.close(target_directory)
        os.unlink(staged_path.name, dir_fd=staged_directory)
        _fsync_directory_descriptor(staged_directory)
        return str(target_path.parent / ".selc-publication-conflicts" / destination)
    finally:
        os.close(staged_directory)


def _check_publication(
    *,
    project_root: Path,
    suite_id: str,
    manifest: dict[str, Any],
    task_id: str,
    call_id: str,
    delivery_id: str,
    action_row: Any,
    artifact_path: Path,
    locks_held: bool = False,
) -> Any | None:
    """Run the manifest-selected T009 checker outside any records transaction."""

    action_id = str(action_row["action_id"])
    action = managed_configuration(manifest, _current_root(project_root, suite_id)).get(action_id)
    if action is None:
        raise SelcError("MANAGED_ACTION_UNDECLARED", "publication action is no longer declared")
    skill = str(action["skill"])
    skill_entry = next((entry for entry in manifest["skills"] if entry.get("name") == skill), None)
    check_id = skill_entry.get("artifact_check") if isinstance(skill_entry, dict) else None
    if not isinstance(check_id, str):
        return None
    staged_input = Path(str(action_row["artifact_path"])).parent / "input"
    if not staged_input.is_file():
        raise SelcError(
            "CHECK_SOURCE_UNAVAILABLE", "managed action has no staged input for its checker"
        )
    subject = SimpleNamespace(task_id=task_id, call_id=call_id, delivery_id=delivery_id)
    return run_installed_check(
        project_root=project_root,
        suite_id=suite_id,
        check_id=check_id,
        input_path=staged_input,
        artifact_path=artifact_path,
        subject=subject,
        run_id=f"managed-check-{delivery_id}",
        locks_held=locks_held,
    )


def _record_managed_delivery(
    database: Any,
    *,
    delivery_id: str,
    task_id: str,
    call_id: str,
    ordinal: int,
    execution: Any | None,
    installation: dict[str, Any],
) -> None:
    if execution is None:
        return
    records.record_checked_delivery(database, execution, _managed_sequence=ordinal)


def _checker_summary(execution: Any | None) -> dict[str, object]:
    if execution is None:
        return {"status": "unknown", "reason": "no-declared-trusted-checker"}
    result = execution.result
    return {
        "check_id": execution.check_id,
        "status": result.status,
        "input_hash": result.input_digest,
        "artifact_hash": result.artifact_digest,
        "resource_hash": result.resource_digest,
        "composite_hash": result.composite_digest,
        "reason": result.reason,
    }


def _open_publication_window(
    database: Any, receipt_row: Any, task_id: str, call_id: str, delivery_id: str
) -> None:
    native_event = database.execute(
        "SELECT payload_json FROM managed_native_events WHERE event_id = ?",
        (receipt_row["native_event_id"],),
    ).fetchone()
    prefix: dict[str, object] = {}
    if native_event is not None:
        try:
            candidate = json.loads(str(native_event["payload_json"])).get("prefix", {})
            prefix = candidate if isinstance(candidate, dict) else {}
        except json.JSONDecodeError:
            prefix = {}
    database.execute(
        """INSERT INTO managed_windows(
        window_id, task_id, call_id, delivery_id, host, session_id, generation_id, tool_use_id,
        request_id, pre_event_id, pre_prefix_hash, pre_prefix_length, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)""",
        (
            f"managed-window-{uuid.uuid4()}",
            task_id,
            call_id,
            delivery_id,
            receipt_row["host"],
            receipt_row["session_id"],
            receipt_row["generation_id"],
            receipt_row["tool_use_id"],
            receipt_row["request_id"],
            receipt_row["native_event_id"],
            str(prefix.get("sha256") or ""),
            int(prefix.get("length") or -1),
            records.utc_now(),
        ),
    )


def _close_matching_window(database: Any, event: dict[str, object]) -> None:
    pre_event = database.execute(
        """SELECT event_id, payload_json FROM managed_native_events
        WHERE host = ? AND suite_id = ? AND session_id = ? AND generation_id = ?
          AND request_id = ? AND tool_use_id = ? AND phase = ?""",
        (
            event["host"],
            event["suite_id"],
            event["session_id"],
            event["generation_id"],
            event["request_id"],
            event["tool_use_id"],
            PRE_PHASE,
        ),
    ).fetchone()
    if pre_event is None:
        return
    windows = database.execute(
        "SELECT * FROM managed_windows WHERE pre_event_id = ? AND status = 'open'",
        (pre_event["event_id"],),
    ).fetchall()
    for window in windows:
        status = _publication_window_status(pre_event, window, event)
        if status == "pending":
            continue
        if status != "closed":
            database.execute(
                "UPDATE managed_contexts SET unknown_change = 1 WHERE task_id = ?",
                (window["task_id"],),
            )
            reduce_managed_assessment(database, str(window["task_id"]))
            continue
        database.execute(
            "UPDATE managed_windows SET post_event_id = ?, status = 'closed', closed_at = ? WHERE window_id = ?",
            (event["event_id"], records.utc_now(), window["window_id"]),
        )
        reduce_managed_assessment(database, str(window["task_id"]))


def _close_pending_windows_on_stop(database: Any, event: dict[str, object], event_id: str) -> None:
    """Reconcile a pending Post callback at the native request's real Stop."""

    windows = database.execute(
        """SELECT * FROM managed_windows
        WHERE host = ? AND session_id = ? AND generation_id = ? AND request_id = ?
          AND status = 'open'""",
        (event["host"], event["session_id"], event["generation_id"], event["request_id"]),
    ).fetchall()
    for window in windows:
        pre_event = database.execute(
            "SELECT event_id, payload_json FROM managed_native_events WHERE event_id = ?",
            (window["pre_event_id"],),
        ).fetchone()
        if pre_event is None:
            continue
        status = _publication_window_status(pre_event, window, event)
        if status == "closed":
            database.execute(
                """UPDATE managed_windows SET post_event_id = ?, status = 'closed', closed_at = ?
                WHERE window_id = ?""",
                (event_id, records.utc_now(), window["window_id"]),
            )
        else:
            database.execute(
                "UPDATE managed_contexts SET unknown_change = 1 WHERE task_id = ?",
                (window["task_id"],),
            )
        reduce_managed_assessment(database, str(window["task_id"]))


def _publication_window_status(pre_event: Any, window: Any, event: dict[str, object]) -> str:
    """Return ``closed``, ``pending``, or ``invalid`` for a native cutoff."""

    try:
        pre_payload = json.loads(str(pre_event["payload_json"]))
        pre_fields = _frozen_prefix_fields(pre_payload.get("prefix"))
        cutoff_fields = _frozen_prefix_fields(event.get("prefix"))
        if pre_fields is None or cutoff_fields is None:
            return "invalid"
        expected_hash, expected_length, expected_path_hash, expected_device, expected_inode = (
            pre_fields
        )
        cutoff_hash, cutoff_length, cutoff_path_hash, cutoff_device, cutoff_inode = cutoff_fields
        if (
            cutoff_length < expected_length
            or cutoff_path_hash != expected_path_hash
            or cutoff_device != expected_device
            or cutoff_inode != expected_inode
        ):
            return "invalid"
        transcript_path = Path(str(event["transcript_path"]))
        resolved = transcript_path.resolve(strict=True)
        path_hash = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()
        if path_hash != expected_path_hash:
            return "invalid"
        with resolved.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if opened.st_dev != expected_device or opened.st_ino != expected_inode:
                return "invalid"
            cutoff = stream.read(cutoff_length)
        if not (
            len(cutoff) == cutoff_length
            and hashlib.sha256(cutoff).hexdigest() == cutoff_hash
            and hashlib.sha256(cutoff[:expected_length]).hexdigest() == expected_hash
            and str(window["pre_prefix_hash"]) == expected_hash
            and int(window["pre_prefix_length"]) == expected_length
        ):
            return "invalid"
        reconciliation_event = dict(event)
        for key in (
            "tool_command_digest",
            "command_digest",
            "operation_digest",
            "request_digest",
            "raw_request_digest",
        ):
            value = pre_payload.get(key)
            if isinstance(value, str) and value:
                reconciliation_event[key] = value
        reconciliation_event["pre_prefix_length"] = expected_length
        if _history_contains_publication_result(
            reconciliation_event,
            str(window["tool_use_id"]),
            raw=cutoff,
        ):
            return "closed"
        return "pending"
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return "invalid"


def _frozen_prefix_fields(prefix: object) -> tuple[str, int, str, int, int] | None:
    """Return one complete frozen transcript prefix proof, or no proof."""

    if not isinstance(prefix, dict):
        return None
    sha256 = prefix.get("sha256")
    length = prefix.get("length")
    path_hash = prefix.get("path_hash")
    device = prefix.get("device")
    inode = prefix.get("inode")
    if (
        not isinstance(sha256, str)
        or not sha256
        or type(length) is not int
        or length < 0
        or length > MAX_TRANSCRIPT_PREFIX_BYTES
        or not isinstance(path_hash, str)
        or not path_hash
        or type(device) is not int
        or type(inode) is not int
    ):
        return None
    return sha256, length, path_hash, device, inode


def _history_contains_publication_result(
    event: dict[str, object],
    tool_use_id: str,
    *,
    raw: bytes | None = None,
) -> bool:
    """Require the host serializer's matching native tool result, never file growth."""

    prefix = event.get("prefix")
    if not isinstance(prefix, dict):
        return False
    length = prefix.get("length")
    if type(length) is not int or length < 0:
        return False
    if raw is None:
        path = Path(str(event.get("transcript_path") or ""))
        if not path:
            return False
        try:
            with path.open("rb") as stream:
                raw = stream.read(length)
        except OSError:
            return False
    if len(raw) != length:
        return False
    try:
        history = [json.loads(line) for line in raw.splitlines() if line.strip()]
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not all(isinstance(record, dict) for record in history):
        return False
    if event["host"] == "workbuddy":
        return _workbuddy_history_contains_publication_result(history, event, tool_use_id)
    if event["host"] == "codex":
        pre_length = event.get("pre_prefix_length")
        if (
            type(pre_length) is not int
            or pre_length < 0
            or pre_length > len(raw)
            or (pre_length and raw[pre_length - 1 : pre_length] != b"\n")
        ):
            return False
        if any(
            _contains_unresolved_history_base(
                record,
                {
                    "history_base",
                    "historyBase",
                    "forked_from",
                    "forkedFrom",
                    "compacted_from",
                    "compactedFrom",
                },
            )
            for record in history
        ):
            return False
        try:
            current_window = [
                json.loads(line) for line in raw[pre_length:].splitlines() if line.strip()
            ]
        except (UnicodeDecodeError, json.JSONDecodeError):
            return False
        if not all(isinstance(record, dict) for record in current_window):
            return False
        return _codex_history_contains_publication_result(current_window, event, tool_use_id)
    return False


def _workbuddy_history_contains_publication_result(
    history: list[object], event: dict[str, object], tool_use_id: str
) -> bool:
    """Match the flat WorkBuddy call/result pair and its native parent chain."""

    records_by_id = {
        record["id"]: record
        for record in history
        if isinstance(record, dict) and isinstance(record.get("id"), str)
    }
    expected_session = str(event["session_id"])
    expected_cwd = str(event["cwd"])
    expected_request = str(event["request_id"])
    expected_command_digest = str(event.get("tool_command_digest") or "")
    if not expected_command_digest:
        return False
    calls: list[dict[str, object]] = []
    for record in history:
        if not isinstance(record, dict) or record.get("type") != "function_call":
            continue
        provider = record.get("providerData")
        if (
            record.get("callId") != tool_use_id
            or record.get("sessionId") != expected_session
            or record.get("cwd") != expected_cwd
            or not isinstance(provider, dict)
            or provider.get("conversationRequestId") != expected_request
            or not isinstance(record.get("id"), str)
            or not isinstance(record.get("arguments"), str)
        ):
            continue
        try:
            arguments = json.loads(record["arguments"])
        except json.JSONDecodeError:
            continue
        command = arguments.get("command") if isinstance(arguments, dict) else None
        if (
            not isinstance(command, str)
            or hashlib.sha256(command.encode("utf-8")).hexdigest() != expected_command_digest
        ):
            continue
        if not _workbuddy_parent_chain_is_complete(
            records_by_id, record, expected_session, expected_cwd, expected_request
        ):
            continue
        calls.append(record)
    for call in calls:
        for record in history:
            if not isinstance(record, dict) or record.get("type") != "function_call_result":
                continue
            provider = record.get("providerData")
            if (
                record.get("callId") == tool_use_id
                and record.get("parentId") == call["id"]
                and record.get("sessionId") == expected_session
                and record.get("cwd") == expected_cwd
                and isinstance(provider, dict)
                and provider.get("conversationRequestId") == expected_request
                and isinstance(record.get("output"), dict)
            ):
                return True
    return False


def _workbuddy_parent_chain_is_complete(
    records_by_id: dict[str, dict[str, object]],
    record: dict[str, object],
    session_id: str,
    cwd: str,
    request_id: str,
) -> bool:
    """Require a resolved current-request user boundary for a native tool call.

    A follow-up user message can legitimately point to an earlier request's
    result.  That relation is separately used to link a correction task.  For
    this operation's result window, the current user message is the verified
    request boundary; continuing through its cross-request parent would reject
    an otherwise exact current call/result pair.
    """

    current = record
    visited: set[str] = set()
    while True:
        provider = current.get("providerData")
        if not isinstance(provider, dict) or provider.get("conversationRequestId") != request_id:
            return False
        if (
            current.get("type") == "message"
            and current.get("role") == "user"
            and current.get("sessionId") == session_id
            and current.get("cwd") == cwd
            and provider.get("startsNewUserRequest") is True
        ):
            return True
        parent_id = current.get("parentId")
        if parent_id is None:
            return False
        if not isinstance(parent_id, str) or parent_id in visited:
            return False
        visited.add(parent_id)
        parent = records_by_id.get(parent_id)
        if parent is None or parent.get("sessionId") != session_id or parent.get("cwd") != cwd:
            return False
        current = parent


def _codex_command_execution_cwd_is_current(item_cwd: object, expected_cwd: str) -> bool:
    """Match a native CommandExecution workspace to the collector workspace.

    Codex currently writes the CommandExecution workspace as either the
    collector's absolute POSIX path or a local ``file:`` URI.  URI decoding is
    deliberately narrow because this comparison binds a native completion to
    the active project.
    """

    if not isinstance(item_cwd, str):
        return False
    if item_cwd == expected_cwd:
        return True
    if not item_cwd.startswith("file:"):
        return False
    if any(control in item_cwd for control in ("\x00", "\t", "\r", "\n")):
        return False

    try:
        parsed = urlsplit(item_cwd)
    except ValueError:
        return False
    if (
        parsed.scheme != "file"
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/")
        or not _file_uri_path_has_valid_percent_escapes(parsed.path)
    ):
        return False

    try:
        decoded_path = unquote_to_bytes(parsed.path)
        if b"\x00" in decoded_path:
            return False
        candidate = Path(decoded_path.decode("utf-8")).resolve(strict=True)
        expected = Path(expected_cwd).resolve(strict=True)
    except (OSError, UnicodeDecodeError, UnicodeEncodeError, ValueError):
        return False
    return candidate == expected


def _file_uri_path_has_valid_percent_escapes(path: str) -> bool:
    """Reject URI paths whose percent escapes cannot represent path bytes."""

    hex_digits = "0123456789abcdefABCDEF"
    index = 0
    while index < len(path):
        if path[index] != "%":
            index += 1
            continue
        if (
            index + 2 >= len(path)
            or path[index + 1] not in hex_digits
            or path[index + 2] not in hex_digits
        ):
            return False
        index += 3
    return True


def _codex_history_contains_publication_result(
    history: list[object], event: dict[str, object], tool_use_id: str
) -> bool:
    """Match one native Codex CommandExecution completion to one hook tool.

    The outer ``exec`` response item serializes JavaScript and uses a separate
    call-ID namespace.  Only the observed ``event_msg/item_completed`` record
    carries the hook tool ID, native turn, and shell command needed for a
    publication window.
    """

    expected_session = event.get("session_id")
    expected_turn = event.get("generation_id")
    expected_cwd = event.get("cwd")
    expected_command_digest = event.get("tool_command_digest")
    expected_operation = event.get("operation_digest")
    if not all(
        isinstance(value, str) and value
        for value in (
            expected_session,
            expected_turn,
            expected_cwd,
            expected_command_digest,
            expected_operation,
        )
    ):
        return False

    matches = 0
    for record in history:
        if not isinstance(record, dict) or record.get("type") != "event_msg":
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "item_completed":
            continue
        item = payload.get("item")
        if not isinstance(item, dict) or item.get("type") != "CommandExecution":
            continue
        if item.get("id") != tool_use_id:
            continue
        command = item.get("command")
        is_exact_command = (
            isinstance(command, list)
            and len(command) == 3
            and command[0] == "/bin/zsh"
            and command[1] == "-lc"
            and isinstance(command[2], str)
            and hashlib.sha256(command[2].encode("utf-8")).hexdigest() == expected_command_digest
        )
        is_exact_completion = (
            payload.get("thread_id") == expected_session
            and payload.get("turn_id") == expected_turn
            and _codex_command_execution_cwd_is_current(item.get("cwd"), expected_cwd)
            and item.get("status") == "completed"
            and type(item.get("exit_code")) is int
            and item.get("exit_code") == 0
            and is_exact_command
        )
        if not is_exact_completion:
            return False
        matches += 1
    return matches == 1


def _publication_result(publication: Any | None) -> str:
    if publication is None:
        return "unknown"
    try:
        value = json.loads(str(publication["checker_json"])).get("status")
    except (TypeError, json.JSONDecodeError):
        return "unknown"
    return value if value in {"pass", "fail", "unknown"} else "unknown"


def _receipt_is_fixture(database: Any, receipt_row: Any) -> bool:
    event = database.execute(
        "SELECT payload_json FROM managed_native_events WHERE event_id = ?",
        (receipt_row["native_event_id"],),
    ).fetchone()
    return event is not None and json.loads(str(event["payload_json"])).get("source") == "fixture"


def _assert_active_installation(
    project_root: Path, suite_id: str, installation: dict[str, Any]
) -> None:
    root = install_root(project_root) / suite_id
    current = _current_root(project_root, suite_id)
    if not current.is_dir() or portable_tree_hash(current) != installation["digest"]:
        raise SelcError(
            "INSTALLATION_IDENTITY_DRIFT", "active suite bytes do not match installation identity"
        )
    entries = installation.get("managed_entries", {})
    for harness, skills in entries.items():
        if not isinstance(skills, dict):
            raise SelcError("INSTALLATION_ENTRY_INVALID", "managed entry mapping is invalid")
        for skill, expected_hash in skills.items():
            entry = root / "entries" / str(harness) / str(skill) / "SKILL.md"
            if not entry.is_file() or file_hash(entry) != expected_hash:
                raise SelcError(
                    "INSTALLATION_ENTRY_DRIFT",
                    "managed discovery entry was modified or is missing",
                    "run updates recover before activation",
                )
    _assert_installation_state(project_root, root, suite_id, installation)


def _current_root(project_root: Path, suite_id: str) -> Path:
    return install_root(project_root) / suite_id / "current"


def _write_fsynced(path: Path, contents: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(contents)
        stream.flush()
        os.fsync(stream.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_file(path: Path) -> None:
    with path.open("rb") as stream:
        os.fsync(stream.fileno())


def _required_string(value: dict[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item or len(item.encode("utf-8")) > 512:
        raise SelcError("NATIVE_COLLECTOR_INVALID", f"native {key} is invalid")
    return item


def _required_managed_command(value: dict[str, object]) -> str:
    """Read a bounded shell invocation without applying identifier limits.

    A normal generated action binds absolute request and artifact paths, so its
    command is naturally longer than a session or tool identifier.  It remains
    bounded by the native hook payload and is parsed as the finite wrapper
    schema immediately afterwards.
    """

    command = value.get("command")
    if (
        not isinstance(command, str)
        or not command
        or len(command.encode("utf-8")) > MAX_MANAGED_COMMAND_BYTES
    ):
        raise SelcError("NATIVE_COLLECTOR_INVALID", "native command is invalid")
    return command


def _verify_collector_cwd(project_root: Path, payload: dict[str, object]) -> None:
    """Require the trusted producer's current project directory to be exact."""

    cwd = Path(_required_string(payload, "cwd"))
    try:
        if cwd.resolve(strict=True) != project_root.resolve(strict=True):
            raise ValueError("collector cwd differs from project root")
    except (OSError, ValueError) as error:
        raise SelcError(
            "NATIVE_COLLECTOR_INVALID",
            "native collector cwd is unavailable or belongs to another project",
            "pause before business execution and repair the host collector",
        ) from error


def _optional_string(value: dict[str, object], key: str) -> str | None:
    item = value.get(key)
    if item is None:
        return None
    return _required_string(value, key)


def _native_request_digest(payload: dict[str, object], skill: str) -> str:
    request = payload["request"]
    if not isinstance(request, dict) or set(request).difference(
        {"skill", "input", "raw_prompt_digest", "correction"}
    ):
        raise SelcError(
            "NATIVE_COLLECTOR_INVALID",
            "native managed request must be a finite object",
            "repair the collector request serializer",
        )
    request_skill = request.get("skill", "")
    input_value = request.get("input")
    if request_skill != skill or not isinstance(input_value, dict):
        raise SelcError(
            "NATIVE_COLLECTOR_INVALID",
            "native managed request does not match the selected skill",
            "repair the collector request serializer",
        )
    if input_value.get("classification") == "unknown":
        if set(input_value) != {"classification"}:
            raise SelcError(
                "NATIVE_COLLECTOR_INVALID",
                "native unknown request contains unconfirmed business fields",
                "repair the collector request serializer",
            )
        return ""
    try:
        semantic_request = {"skill": request_skill, "input": input_value}
        encoded = canonical_json(semantic_request).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise SelcError(
            "NATIVE_COLLECTOR_INVALID",
            "native managed request is not JSON serializable",
            "repair the collector request serializer",
        ) from error
    if len(encoded) > 16_384:
        raise SelcError(
            "NATIVE_COLLECTOR_INVALID",
            "native managed request exceeds the bounded collector schema",
            "reduce the declared managed request",
        )
    return hashlib.sha256(encoded).hexdigest()


def _business_dto_goal_digest(value: object) -> str | None:
    """Hash one complete business DTO after removing only its control declaration.

    This is a goal comparison, not an action request digest.  It deliberately
    has no skill name, path inference, trigger classification, or caller-supplied
    digest field.  Nested ``selc`` keys remain ordinary business data.
    """

    if not isinstance(value, dict) or value.get("classification") == "unknown":
        return None
    business_dto = dict(value)
    has_control = "selc" in business_dto
    control = business_dto.pop("selc", None)
    if has_control:
        declaration = _workbuddy_raw_correction_declaration(control, True)
        if declaration is None or declaration.get("valid") is not True:
            return None
    try:
        encoded = canonical_json(business_dto).encode("utf-8")
    except (TypeError, ValueError):
        return None
    if len(encoded) > 16_384:
        return None
    return hashlib.sha256(encoded).hexdigest()


def _business_dto_goal_digest_from_request(request: object) -> str | None:
    """Read the trusted collector's DTO after rejecting malformed control data."""

    if not isinstance(request, dict) or not isinstance(request.get("input"), dict):
        return None
    if "correction" in request:
        declaration = _correction_declaration(request)
        if declaration is None or declaration.get("valid") is not True:
            return None
    return _business_dto_goal_digest(request["input"])


def _correction_declaration(request: object) -> dict[str, object] | None:
    """Return only the finite correction control declared beside a DTO.

    ``request`` itself originates at the native adapter.  The current-request
    anchor later proves that the adapter's declaration came from the exact raw
    JSON user message before it can bind a pre-existing task.
    """

    if not isinstance(request, dict) or "correction" not in request:
        return None
    declaration = request.get("correction")
    if not isinstance(declaration, dict):
        return {"valid": False}
    if set(declaration) != {"intent", "target", "preserve_goal", "valid"}:
        return {"valid": False}
    if (
        declaration.get("intent") != "correct_delivery"
        or not isinstance(declaration.get("target"), str)
        or not declaration["target"]
        or declaration.get("preserve_goal") is not True
        or declaration.get("valid") is not True
    ):
        return {"valid": False}
    return {
        "intent": "correct_delivery",
        "target": declaration["target"],
        "preserve_goal": True,
        "valid": True,
    }


def _raw_request_digest(payload: dict[str, object]) -> str:
    """Bind callback immutability without treating natural-language text as input.

    The collector supplies a one-way prompt summary only for an unparsed
    request.  It protects a callback from replacement while the separate
    confirmed-input digest controls whether an action may execute.
    """

    request = payload.get("request")
    if not isinstance(request, dict):
        return ""
    supplied = request.get("raw_prompt_digest")
    if isinstance(supplied, str) and len(supplied) == 64:
        return supplied
    try:
        encoded = canonical_json(request).encode("utf-8")
    except (TypeError, ValueError):
        return ""
    return hashlib.sha256(encoded).hexdigest()


def _is_sha256_digest(value: str) -> bool:
    """Recognize the fixed lowercase digest encoding stored in native facts."""

    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _environment_value(environment: dict[str, str], key: str) -> str:
    value = environment.get(key)
    if not isinstance(value, str) or not value:
        raise SelcError(
            "NATIVE_CONTEXT_UNAVAILABLE",
            f"required native environment value is missing: {key}",
            "pause before business execution and repair the host collector",
        )
    return value


def _environment_alias(environment: dict[str, str], *keys: str) -> str:
    values = {key: environment[key] for key in keys if environment.get(key)}
    if not values:
        raise SelcError(
            "NATIVE_CONTEXT_UNAVAILABLE",
            "required native environment aliases are missing",
            "pause before business execution and repair the host collector",
        )
    distinct = set(values.values())
    if len(distinct) != 1:
        raise SelcError(
            "NATIVE_CONTEXT_CONFLICT",
            "native environment aliases disagree",
            "pause before business execution and inspect the host collector",
        )
    return next(iter(distinct))


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _encoded(value: object) -> str:
    return canonical_json(value)
