#!/opt/homebrew/bin/python3.11
"""Private fixture probe for comparing a host hook with its Python child context.

This is an experiment preparation tool. It does not authorize work, collect business
outputs, or establish that a host actually supplied native identity metadata.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import hmac
import json
import os
import secrets
import shlex
import shutil
import subprocess
import sys
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path


PROTOCOL = "native-context-probe-v1"
SUPPORTED_HOSTS = {"codex", "workbuddy"}
MAX_INPUT_BYTES = 64 * 1024
PRIVATE_DIRECTORY = ".native_context_probe"
FIXTURE_FILE = "fixture.json"
OBSERVATIONS_FILE = "observations.json"
OBSERVATIONS_LOCK_FILE = "observations.lock"
RECEIPTS_FILE = "receipts.json"
RUNS_DIRECTORY = "runs"
DEFAULT_TRANSPORT = "context"
UPDATED_INPUT_TRANSPORT = "workbuddy-updated-input"
RECEIPT_MAX_AGE_SECONDS = 60
HOOK_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "Stop",
    "SessionEnd",
)
ALLOWED_ENVIRONMENT_NAMES = (
    "CODEX_SESSION_ID",
    "CODEX_THREAD_ID",
    "CODEX_TOOL_CALL_ID",
    "CODEX_VERSION",
    "CODEBUDDY_SESSION_ID",
    "TOOL_CALL_ID",
    "CONVERSATION_REQUEST_ID",
    "CONVERSATION_MESSAGE_ID",
    "SANDBOX_BROKER_TOOL_CALL_ID",
)


class ProbeError(Exception):
    """A checked fixture-preparation or observation failure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def resolve_path(value: str | Path) -> Path:
    return Path(value).expanduser().resolve()


def private_directory(workspace: Path) -> Path:
    return workspace / PRIVATE_DIRECTORY


def fixture_path(workspace: Path) -> Path:
    return private_directory(workspace) / FIXTURE_FILE


def observations_path(workspace: Path) -> Path:
    return private_directory(workspace) / OBSERVATIONS_FILE


def observations_lock_path(workspace: Path) -> Path:
    return private_directory(workspace) / OBSERVATIONS_LOCK_FILE


def receipts_path(workspace: Path) -> Path:
    return private_directory(workspace) / RECEIPTS_FILE


def runs_directory(workspace: Path) -> Path:
    return private_directory(workspace) / RUNS_DIRECTORY


def within_fixture(path: Path, workspace: Path) -> bool:
    try:
        path.resolve().relative_to(workspace.resolve())
    except ValueError:
        return False
    return True


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def command_text(argv: tuple[str, ...] | list[str]) -> str:
    return shlex.join(argv)


def write_private_bytes(path: Path, content: bytes, *, replace: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if replace:
        temporary_path = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        descriptor = os.open(temporary_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(content)
            os.replace(temporary_path, path)
            os.chmod(path, 0o600)
        except BaseException:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
            raise
        return

    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(content)


def write_private_json(path: Path, value: object, *, replace: bool = False) -> None:
    write_private_bytes(path, (canonical_json(value) + "\n").encode("utf-8"), replace=replace)


def read_private_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ProbeError("UNPREPARED_WORKSPACE", f"missing private fixture file: {path}") from error
    except json.JSONDecodeError as error:
        raise ProbeError("INVALID_FIXTURE", f"private fixture file is not JSON: {path}") from error


def read_fixture(workspace_value: str | Path) -> dict[str, str]:
    workspace = resolve_path(workspace_value)
    raw_fixture = read_private_json(fixture_path(workspace))
    if not isinstance(raw_fixture, dict):
        raise ProbeError("INVALID_FIXTURE", "fixture must be a JSON object")

    fixture = {
        name: raw_fixture.get(name)
        for name in ("protocol", "workspace", "host", "fixture_id")
    }
    transport = raw_fixture.get("transport", DEFAULT_TRANSPORT)
    if (
        fixture["protocol"] != PROTOCOL
        or fixture["workspace"] != str(workspace)
        or fixture["host"] not in SUPPORTED_HOSTS
        or not isinstance(fixture["fixture_id"], str)
        or transport not in {DEFAULT_TRANSPORT, UPDATED_INPUT_TRANSPORT}
    ):
        raise ProbeError("INVALID_FIXTURE", "fixture identity does not match this workspace")
    if transport == UPDATED_INPUT_TRANSPORT and fixture["host"] != "workbuddy":
        raise ProbeError("INVALID_FIXTURE", "updated-input transport requires a WorkBuddy fixture")
    return {**fixture, "transport": transport}  # type: ignore[return-value]


def hook_configuration_path(workspace: Path, host: str) -> Path:
    if host == "codex":
        return workspace / ".codex" / "hooks.json"
    return workspace / ".codebuddy" / "settings.json"


def receive_command(workspace: Path) -> str:
    workspace = resolve_path(workspace)
    return command_text(
        (
            sys.executable,
            str(Path(__file__).resolve()),
            "receive",
            "--workspace",
            str(workspace),
        )
    )


def hook_configuration(workspace: Path, host: str) -> dict[str, object]:
    matcher = "^Bash$" if host == "codex" else "^(Bash|exec_command)$"
    command = receive_command(workspace)
    hooks: dict[str, list[dict[str, object]]] = {}
    for event_name in HOOK_EVENTS:
        hook_entry: dict[str, object] = {
            "hooks": [{"type": "command", "command": command, "timeout": 5}],
        }
        if event_name in {"PreToolUse", "PostToolUse"}:
            hook_entry["matcher"] = matcher
        hooks[event_name] = [hook_entry]
    return {
        "description": f"{PROTOCOL} isolated {host} context candidate fixture.",
        "hooks": hooks,
    }


def run_command(workspace: Path) -> str:
    workspace = resolve_path(workspace)
    return command_text(
        (
            sys.executable,
            str(Path(__file__).resolve()),
            "run",
            "--workspace",
            str(workspace),
        )
    )


def running_instructions(workspace: Path) -> str:
    command = run_command(workspace)
    return "\n".join(
        (
            "Native context probe fixture",
            "",
            "Open this directory as the selected host workspace and review its local hook configuration.",
            "Run this exact command in the host shell tool from this fixture directory:",
            "",
            command,
            "",
            "The result is a private identity comparison labelled candidate. It does not prove native",
            "host behavior, skill discovery, business execution, or user intent.",
            "",
        )
    )


def prepare(
    workspace_value: str | Path,
    host: str,
    *,
    transport: str = DEFAULT_TRANSPORT,
) -> dict[str, object]:
    """Create one empty, project-scoped host fixture without touching existing content."""

    if host not in SUPPORTED_HOSTS:
        raise ProbeError("UNSUPPORTED_HOST", "host must be codex or workbuddy")
    if transport not in {DEFAULT_TRANSPORT, UPDATED_INPUT_TRANSPORT}:
        raise ProbeError(
            "UNSUPPORTED_TRANSPORT",
            "transport must be context or workbuddy-updated-input",
        )
    if transport == UPDATED_INPUT_TRANSPORT and host != "workbuddy":
        raise ProbeError("UNSUPPORTED_TRANSPORT", "workbuddy-updated-input requires host workbuddy")

    workspace = resolve_path(workspace_value)
    if workspace.exists() and any(workspace.iterdir()):
        raise ProbeError("WORKSPACE_NOT_EMPTY", f"refusing non-empty workspace: {workspace}")
    workspace.mkdir(parents=True, exist_ok=True)

    state_directory = private_directory(workspace)
    state_directory.mkdir(mode=0o700)
    fixture = {
        "protocol": PROTOCOL,
        "workspace": str(workspace),
        "host": host,
        "transport": transport,
        "fixture_id": secrets.token_hex(16),
        "evidence_label": "candidate",
    }
    write_private_json(fixture_path(workspace), fixture)
    write_private_json(observations_path(workspace), [])
    write_private_bytes(observations_lock_path(workspace), b"")
    if transport == UPDATED_INPUT_TRANSPORT:
        write_private_json(receipts_path(workspace), [])
    runs_directory(workspace).mkdir(mode=0o700)

    configuration_path = hook_configuration_path(workspace, host)
    write_private_json(configuration_path, hook_configuration(workspace, host))
    write_private_bytes(
        workspace / "RUNNING.txt",
        running_instructions(workspace).encode("utf-8"),
    )
    return {
        "protocol": PROTOCOL,
        "workspace": str(workspace),
        "host": host,
        "transport": transport,
        "config_path": str(configuration_path),
        "run_command": run_command(workspace),
        "evidence_label": "candidate",
    }


def scalar(payload: dict[str, object], *names: str) -> str | int | bool | None:
    for name in names:
        value = payload.get(name)
        if isinstance(value, (str, int, bool)) and not isinstance(value, float):
            if isinstance(value, str) and len(value) > 256:
                continue
            return value
    return None


def scalar_text(value: str | int | bool | None) -> str | None:
    return str(value) if value is not None else None


def normalize_payload(
    fixture: dict[str, str],
    payload: dict[str, object],
    *,
    source: str,
) -> dict[str, object]:
    cwd_value = scalar(payload, "cwd")
    if not isinstance(cwd_value, str):
        raise ProbeError("MISSING_CWD", "hook input has no cwd")
    cwd = resolve_path(cwd_value)
    workspace = Path(fixture["workspace"])
    if not within_fixture(cwd, workspace):
        raise ProbeError("CWD_OUTSIDE_FIXTURE", "hook cwd is outside this fixture")

    event_type = scalar_text(scalar(payload, "hook_event_name", "event_name", "event"))
    if not event_type:
        raise ProbeError("MISSING_EVENT_TYPE", "hook input has no event name")

    tool_input = payload.get("tool_input")
    tool_input_fields = sorted(tool_input) if isinstance(tool_input, dict) else []
    command_digest = None
    if event_type == "PreToolUse" and isinstance(tool_input, dict):
        command = tool_input.get("command")
        if isinstance(command, str):
            command_digest = digest(command)

    session_id = scalar_text(scalar(payload, "session_id", "sessionId"))
    tool_call_id = scalar_text(scalar(payload, "tool_call_id", "tool_use_id", "call_id"))
    tool_use_id = scalar_text(scalar(payload, "tool_use_id"))
    call_id = scalar_text(scalar(payload, "call_id"))
    request_id = scalar_text(scalar(payload, "request_id", "conversation_request_id"))
    generation_id = scalar_text(scalar(payload, "generation_id"))
    turn_id = scalar_text(scalar(payload, "turn_id"))
    message_id = scalar_text(scalar(payload, "message_id", "conversation_message_id"))
    thread_id = scalar_text(scalar(payload, "thread_id"))
    event_source = "hook_candidate" if source == "hook" else "fixture_candidate"
    identity_status = "stable" if session_id and tool_call_id else "unknown_id_arrival"
    stop_flags = {
        name: payload[name]
        for name in ("stop", "is_stop")
        if isinstance(payload.get(name), bool)
    }
    return {
        "arrival_id": secrets.token_hex(12),
        "received_at": utc_now(),
        "source": event_source,
        "evidence_label": "candidate",
        "event_type": event_type,
        "fixture_id": fixture["fixture_id"],
        "cwd": str(cwd),
        "client": scalar_text(scalar(payload, "client", "client_name")),
        "version": scalar_text(scalar(payload, "version", "client_version")),
        "tool_name": scalar_text(scalar(payload, "tool_name", "tool")),
        "session_id": session_id,
        "tool_call_id": tool_call_id,
        "tool_use_id": tool_use_id,
        "call_id": call_id,
        "request_id": request_id,
        "generation_id": generation_id,
        "turn_id": turn_id,
        "message_id": message_id,
        "thread_id": thread_id,
        "source_metadata": scalar_text(scalar(payload, "source")),
        "stop_flags": stop_flags,
        "payload_fields": sorted(str(name) for name in payload),
        "payload_digest": digest(payload),
        "tool_input_fields": [str(name) for name in tool_input_fields],
        "tool_input_digest": digest(tool_input) if tool_input is not None else None,
        "command_digest": command_digest,
        "identity_status": identity_status,
    }


def load_observations(workspace: Path) -> list[dict[str, object]]:
    raw_observations = read_private_json(observations_path(workspace))
    if not isinstance(raw_observations, list) or not all(
        isinstance(observation, dict) for observation in raw_observations
    ):
        raise ProbeError("INVALID_FIXTURE", "observations must be a JSON array")
    return raw_observations


def load_receipts(workspace: Path) -> list[dict[str, object]]:
    raw_receipts = read_private_json(receipts_path(workspace))
    if not isinstance(raw_receipts, list) or not all(
        isinstance(receipt, dict) for receipt in raw_receipts
    ):
        raise ProbeError("INVALID_FIXTURE", "receipts must be a JSON array")
    return raw_receipts


def parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def fixed_runtime_arguments(workspace: Path) -> tuple[str, ...]:
    canonical_workspace = resolve_path(workspace)
    return ("run", "--workspace", str(canonical_workspace))


def fixed_operation_digest(workspace: Path) -> str:
    return digest(
        {
            "python": str(Path(sys.executable).resolve()),
            "script": str(Path(__file__).resolve()),
            "runtime_arguments": fixed_runtime_arguments(workspace),
        }
    )


def resolves_to_current_python(token: str) -> bool:
    expected = Path(sys.executable).resolve()
    if "/" in token:
        return Path(token).expanduser().resolve() == expected
    resolved = shutil.which(token)
    return resolved is not None and Path(resolved).resolve() == expected


def parse_fixed_probe_command(
    command: str,
    *,
    workspace: Path,
    cwd: Path,
) -> dict[str, str] | None:
    """Accept only the generated probe command without shell composition."""

    if any(character in command for character in ("\r", "\n", "\x00")):
        return None
    try:
        arguments = shlex.split(command, posix=True)
    except ValueError:
        return None
    if not within_fixture(cwd, workspace):
        return None
    command_arguments = arguments
    if len(arguments) == 8 and arguments[0] == "cd" and arguments[2] == "&&":
        cd_target = Path(arguments[1]).expanduser()
        cd_workspace = (
            (cwd / cd_target).resolve()
            if not cd_target.is_absolute()
            else cd_target.resolve()
        )
        if cd_workspace != workspace:
            return None
        command_arguments = arguments[3:]
    if len(command_arguments) != 5:
        return None
    python, script, subcommand, workspace_flag, workspace_value = command_arguments
    if (
        not resolves_to_current_python(python)
        or Path(script).expanduser().resolve() != Path(__file__).resolve()
        or subcommand != "run"
        or workspace_flag != "--workspace"
        or Path(workspace_value).expanduser().resolve() != workspace
    ):
        return None
    return {
        "original_command_digest": digest(command),
        "operation_digest": fixed_operation_digest(workspace),
        "runtime_arguments_digest": digest(fixed_runtime_arguments(workspace)),
    }


def bounded_nonempty_text(payload: dict[str, object], name: str) -> str | None:
    value = payload.get(name)
    if isinstance(value, str) and 0 < len(value) <= 256:
        return value
    return None


def workbuddy_session_tool_identity(payload: dict[str, object]) -> dict[str, str] | None:
    session_id = bounded_nonempty_text(payload, "session_id")
    tool_use_id = bounded_nonempty_text(payload, "tool_use_id")
    if session_id is None or tool_use_id is None:
        return None
    return {"session_id": session_id, "tool_use_id": tool_use_id}


def native_workbuddy_identity(payload: dict[str, object]) -> dict[str, str] | None:
    identity = workbuddy_session_tool_identity(payload)
    if identity is None:
        return None
    generation_id = bounded_nonempty_text(payload, "generation_id")
    call_id = bounded_nonempty_text(payload, "call_id")
    if generation_id is None or call_id is None:
        return None
    return {**identity, "generation_id": generation_id, "call_id": call_id}


def receipt_payload_conflicts(
    receipts: list[dict[str, object]],
    identity: dict[str, str],
    payload_digest: object,
) -> bool:
    return any(
        receipt.get("session_id") == identity["session_id"]
        and receipt.get("tool_use_id") == identity["tool_use_id"]
        and receipt.get("payload_digest") != payload_digest
        for receipt in receipts
    )


def receipt_identity_is_tainted(
    receipts: list[dict[str, object]],
    session_id: str,
    tool_call_id: str,
) -> bool:
    return any(
        receipt.get("invalidated_at") is not None
        and receipt.get("session_id") == session_id
        and (
            receipt.get("tool_use_id") == tool_call_id
            or receipt.get("tainted_tool_call_id") == tool_call_id
        )
        for receipt in receipts
    )


def invalidate_receipts_for_conflict(
    fixture: dict[str, str],
    receipts: list[dict[str, object]],
    *,
    session_id: object,
    tool_call_id: object,
) -> bool:
    if (
        not isinstance(session_id, str)
        or not isinstance(tool_call_id, str)
        or not session_id
        or not tool_call_id
        or len(session_id) > 256
        or len(tool_call_id) > 256
    ):
        return False

    invalidated_at = utc_now()
    matching_receipts = [
        receipt
        for receipt in receipts
        if receipt.get("session_id") == session_id
        and (
            receipt.get("tool_use_id") == tool_call_id
            or receipt.get("tainted_tool_call_id") == tool_call_id
        )
    ]
    for receipt in matching_receipts:
        receipt["invalidated_at"] = invalidated_at
        receipt["invalidation_reason"] = "conflicting_native_callback"

    if not matching_receipts:
        receipts.append(
            {
                "fixture_id": fixture["fixture_id"],
                "session_id": session_id,
                "tainted_tool_call_id": tool_call_id,
                "invalidated_at": invalidated_at,
                "invalidation_reason": "conflicting_native_callback",
            }
        )
    return True


def updated_command(command: str, receipt: str) -> str:
    return f"{command} --probe-receipt {shlex.quote(receipt)}"


def receipt_hook_output(tool_input: dict[str, object], receipt: str) -> dict[str, object]:
    command = tool_input.get("command")
    if not isinstance(command, str):
        raise ProbeError("UNSUPPORTED_FIXED_COMMAND", "probe command is missing")
    updated_input = dict(tool_input)
    updated_input["command"] = updated_command(command, receipt)
    return {"hookEventName": "PreToolUse", "updatedInput": updated_input}


def issue_updated_input_receipt(
    fixture: dict[str, str],
    payload: dict[str, object],
    observation: dict[str, object],
    receipts: list[dict[str, object]],
    *,
    source: str,
) -> tuple[str, dict[str, object] | None]:
    """Persist one WorkBuddy receipt before returning a permission-neutral rewrite."""

    if source != "hook":
        return "fixture_source", None
    if observation["event_type"] != "PreToolUse":
        return "not_pre_tool_use", None
    identity = native_workbuddy_identity(payload)
    if identity is None:
        return "missing_native_identity", None
    if observation["identity_status"] == "conflict":
        return "conflict", None
    if receipt_identity_is_tainted(
        receipts,
        identity["session_id"],
        identity["tool_use_id"],
    ):
        return "conflict", None
    matching_identity = [
        receipt
        for receipt in receipts
        if all(receipt.get(name) == value for name, value in identity.items())
    ]
    has_different_payload = any(
        receipt.get("payload_digest") != observation["payload_digest"]
        for receipt in matching_identity
    )
    if has_different_payload:
        return "conflict", None

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return "unsupported_fixed_command", None
    command = tool_input.get("command")
    cwd = Path(str(observation["cwd"]))
    operation = (
        parse_fixed_probe_command(command, workspace=Path(fixture["workspace"]), cwd=cwd)
        if isinstance(command, str)
        else None
    )
    if operation is None:
        return "unsupported_fixed_command", None

    for receipt in matching_identity:
        if (
            receipt.get("payload_digest") == observation["payload_digest"]
            and receipt.get("operation_digest") == operation["operation_digest"]
        ):
            return "reused", receipt_hook_output(tool_input, str(receipt["receipt"]))
    if matching_identity:
        return "conflict", None

    issued_at = utc_now()
    expires_at = parse_timestamp(issued_at) + timedelta(seconds=RECEIPT_MAX_AGE_SECONDS)
    receipt = {
        "receipt": secrets.token_hex(32),
        "fixture_id": fixture["fixture_id"],
        "source": "hook_candidate",
        "issued_at": issued_at,
        "expires_at": expires_at.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "payload_digest": observation["payload_digest"],
        "original_command_digest": operation["original_command_digest"],
        "operation_digest": operation["operation_digest"],
        "runtime_arguments_digest": operation["runtime_arguments_digest"],
        "consumed_at": None,
        **identity,
    }
    receipts.append(receipt)
    return "issued", receipt_hook_output(tool_input, str(receipt["receipt"]))


@contextmanager
def observation_write_lock(workspace: Path):
    """Serialize read/append/replace across concurrent local hook processes."""

    path = observations_lock_path(workspace)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.chmod(path, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def receive_payload(
    workspace_value: str | Path,
    payload: dict[str, object],
    *,
    source: str = "hook",
) -> dict[str, object]:
    """Record a bounded hook candidate or an explicitly labeled fixture candidate."""

    if source not in {"hook", "fixture"}:
        raise ProbeError("INVALID_SOURCE", "source must be hook or fixture")
    if not isinstance(payload, dict):
        raise ProbeError("INVALID_PAYLOAD", "hook input must be a JSON object")

    workspace = resolve_path(workspace_value)
    fixture = read_fixture(workspace)
    observation = normalize_payload(fixture, payload, source=source)
    with observation_write_lock(workspace):
        observations = load_observations(workspace)
        session_id = observation["session_id"]
        tool_call_id = observation["tool_call_id"]
        stable_identity = isinstance(session_id, str) and isinstance(tool_call_id, str)
        if stable_identity:
            same_identity = [
                item
                for item in observations
                if item["source"] == observation["source"]
                and item["event_type"] == observation["event_type"]
                and item["session_id"] == session_id
                and item["tool_call_id"] == tool_call_id
            ]
            for existing in same_identity:
                if existing["payload_digest"] == observation["payload_digest"]:
                    status = "deduplicated"
                    break
            else:
                status = "conflict" if same_identity else "recorded"
            if status == "conflict":
                observation["identity_status"] = "conflict"
        else:
            status = "recorded"

        if status != "deduplicated":
            observations.append(observation)
            write_private_json(observations_path(workspace), observations, replace=True)

        response: dict[str, object] = {
            "protocol": PROTOCOL,
            "status": status,
            "source": observation["source"],
            "evidence_label": "candidate",
            "identity_status": observation["identity_status"],
        }
        if fixture["transport"] == UPDATED_INPUT_TRANSPORT:
            receipts = load_receipts(workspace)
            stable_native_identity = workbuddy_session_tool_identity(payload)
            transport_status, hook_output = issue_updated_input_receipt(
                fixture,
                payload,
                observation,
                receipts,
                source=source,
            )
            invalidated = False
            if source == "hook" and status == "conflict":
                invalidated = invalidate_receipts_for_conflict(
                    fixture,
                    receipts,
                    session_id=observation.get("session_id"),
                    tool_call_id=observation.get("tool_call_id"),
                )
            if (
                source == "hook"
                and observation["event_type"] == "PreToolUse"
                and stable_native_identity is not None
                and (
                    transport_status == "conflict"
                    or receipt_payload_conflicts(
                        receipts,
                        stable_native_identity,
                        observation["payload_digest"],
                    )
                )
            ):
                invalidated = (
                    invalidate_receipts_for_conflict(
                        fixture,
                        receipts,
                        session_id=stable_native_identity["session_id"],
                        tool_call_id=stable_native_identity["tool_use_id"],
                    )
                    or invalidated
                )
            if invalidated:
                write_private_json(receipts_path(workspace), receipts, replace=True)
            response["transport_status"] = transport_status
            if hook_output is not None:
                write_private_json(receipts_path(workspace), receipts, replace=True)
                response["hook_output"] = hook_output
        return response


def receipt_comparison(status: str, reason: str) -> dict[str, object]:
    return {
        "status": status,
        "reason": reason,
        "evidence_label": "candidate",
    }


def receipt_runtime_arguments(workspace: Path, receipt: str) -> tuple[str, ...]:
    return (*fixed_runtime_arguments(workspace), "--probe-receipt", receipt)


def receipt_matches_runtime(
    receipt: dict[str, object],
    workspace: Path,
    runtime_arguments: tuple[str, ...],
) -> bool:
    token = receipt.get("receipt")
    if not isinstance(token, str):
        return False
    expected_arguments = receipt_runtime_arguments(workspace, token)
    return (
        runtime_arguments == expected_arguments
        and receipt.get("fixture_id") is not None
        and receipt.get("operation_digest") == fixed_operation_digest(workspace)
        and receipt.get("runtime_arguments_digest") == digest(fixed_runtime_arguments(workspace))
    )


def consume_updated_input_receipt(
    fixture: dict[str, str],
    workspace: Path,
    environment: dict[str, str],
    receipt_token: str | None,
    runtime_arguments: tuple[str, ...],
) -> dict[str, object]:
    """Consume one local WorkBuddy receipt after checking its bound operation."""

    if receipt_token is None:
        return receipt_comparison("unknown", "missing_probe_receipt")
    expected_arguments = receipt_runtime_arguments(workspace, receipt_token)
    if runtime_arguments != expected_arguments:
        return receipt_comparison("unknown", "probe_receipt_arg_drift")

    with observation_write_lock(workspace):
        receipts = load_receipts(workspace)
        receipt = next(
            (
                item
                for item in receipts
                if isinstance(item.get("receipt"), str)
                and hmac.compare_digest(str(item["receipt"]), receipt_token)
            ),
            None,
        )
        if receipt is None:
            return receipt_comparison("unknown", "probe_receipt_missing")
        if receipt.get("fixture_id") != fixture["fixture_id"]:
            return receipt_comparison("unknown", "probe_receipt_fixture_mismatch")
        if not receipt_matches_runtime(receipt, workspace, runtime_arguments):
            return receipt_comparison("unknown", "probe_receipt_arg_drift")
        receipt_session_id = receipt.get("session_id")
        receipt_tool_use_id = receipt.get("tool_use_id")
        if (
            not isinstance(receipt_session_id, str)
            or not isinstance(receipt_tool_use_id, str)
            or receipt_identity_is_tainted(receipts, receipt_session_id, receipt_tool_use_id)
        ):
            return receipt_comparison("unknown", "probe_receipt_conflicted")

        try:
            expired = parse_timestamp(str(receipt["expires_at"])) <= datetime.now(UTC)
        except (KeyError, TypeError, ValueError):
            return receipt_comparison("unknown", "probe_receipt_invalid")
        if expired:
            return receipt_comparison("unknown", "probe_receipt_expired")
        if receipt.get("consumed_at") is not None:
            return receipt_comparison("unknown", "probe_receipt_spent")

        session_id = environment.get("CODEBUDDY_SESSION_ID")
        if not isinstance(session_id, str) or receipt.get("session_id") != session_id:
            return receipt_comparison("unknown", "probe_receipt_session_mismatch")
        request_id = environment.get("CONVERSATION_REQUEST_ID")
        if request_id is not None and receipt.get("generation_id") != request_id:
            return receipt_comparison("unknown", "probe_receipt_generation_mismatch")
        if any(
            not isinstance(receipt.get(name), str)
            for name in ("generation_id", "tool_use_id", "call_id")
        ):
            return receipt_comparison("unknown", "probe_receipt_invalid")

        receipt["consumed_at"] = utc_now()
        write_private_json(receipts_path(workspace), receipts, replace=True)
    return receipt_comparison("matched", "updated_input_receipt_consumed")


def receive_json_bytes(
    workspace_value: str | Path,
    content: bytes,
    *,
    source: str = "hook",
) -> dict[str, object]:
    if len(content) > MAX_INPUT_BYTES:
        raise ProbeError("INPUT_TOO_LARGE", "hook input exceeds 64 KiB")
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProbeError("INVALID_PAYLOAD", "hook input is not a JSON object") from error
    if not isinstance(payload, dict):
        raise ProbeError("INVALID_PAYLOAD", "hook input must be a JSON object")
    return receive_payload(workspace_value, payload, source=source)


def read_hook_stdin() -> bytes:
    return sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)


def allowed_environment() -> dict[str, str]:
    return {
        name: value
        for name in ALLOWED_ENVIRONMENT_NAMES
        if (value := os.environ.get(name)) is not None
    }


def child_environment(host: str, environment: dict[str, str]) -> dict[str, str | None]:
    if host == "codex":
        return {
            "session_id": environment.get("CODEX_SESSION_ID"),
            "thread_id": environment.get("CODEX_THREAD_ID"),
            "tool_call_id": environment.get("CODEX_TOOL_CALL_ID"),
            "request_id": None,
            "generation_id": None,
            "turn_id": None,
            "message_id": None,
            "version": environment.get("CODEX_VERSION"),
            "tool_identity_strategy": "CODEX_TOOL_CALL_ID",
        }
    return {
        "session_id": environment.get("CODEBUDDY_SESSION_ID"),
        "thread_id": None,
        "tool_call_id": environment.get("SANDBOX_BROKER_TOOL_CALL_ID")
        or environment.get("TOOL_CALL_ID"),
        "request_id": None,
        "generation_id": environment.get("CONVERSATION_REQUEST_ID"),
        "turn_id": None,
        "message_id": environment.get("CONVERSATION_MESSAGE_ID"),
        "version": None,
        "tool_identity_strategy": "SANDBOX_BROKER_TOOL_CALL_ID_or_TOOL_CALL_ID_candidate",
    }


def compare_child_context(
    fixture: dict[str, str],
    observations: list[dict[str, object]],
    environment: dict[str, str],
) -> dict[str, object]:
    child = child_environment(fixture["host"], environment)
    session_id = child["session_id"]
    tool_call_id = child["tool_call_id"]
    available_fields = sorted(name for name, value in child.items() if value is not None)
    if not isinstance(session_id, str) or not isinstance(tool_call_id, str):
        return {
            "status": "unknown",
            "reason": "missing_child_identity",
            "available_fields": available_fields,
            "evidence_label": "candidate",
        }

    matches = [
        item
        for item in observations
        if item["source"] == "hook_candidate"
        and item["event_type"] == "PreToolUse"
        and item["session_id"] == session_id
        and item["tool_call_id"] == tool_call_id
    ]
    if not matches:
        return {
            "status": "unknown",
            "reason": "no_unique_pretooluse_match",
            "available_fields": available_fields,
            "evidence_label": "candidate",
        }
    if len(matches) != 1:
        reason = "conflicting_pretooluse_payloads" if any(
            item["identity_status"] == "conflict" for item in matches
        ) else "ambiguous_pretooluse_matches"
        return {
            "status": "conflict",
            "reason": reason,
            "matching_arrival_ids": [item["arrival_id"] for item in matches],
            "available_fields": available_fields,
            "evidence_label": "candidate",
        }

    event = matches[0]
    conflicting_fields = [
        field
        for field in ("request_id", "generation_id", "turn_id", "message_id", "thread_id")
        if child[field] is not None and event[field] is not None and child[field] != event[field]
    ]
    if conflicting_fields:
        return {
            "status": "conflict",
            "reason": "context_identity_mismatch",
            "conflicting_fields": conflicting_fields,
            "matching_arrival_ids": [event["arrival_id"]],
            "available_fields": available_fields,
            "evidence_label": "candidate",
        }
    return {
        "status": "matched",
        "reason": "unique_pretooluse_match",
        "matching_arrival_ids": [event["arrival_id"]],
        "available_fields": available_fields,
        "evidence_label": "candidate",
    }


def observe_child_environment(workspace: Path, environment: dict[str, str]) -> dict[str, str]:
    """Run a fixed Python child with only the identity allowlist in its environment."""

    child_program = (
        "import json, os; "
        "names = "
        + repr(ALLOWED_ENVIRONMENT_NAMES)
        + "; print(json.dumps({name: os.environ[name] for name in names if name in os.environ}, sort_keys=True))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", child_program],
        check=True,
        cwd=workspace,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
    )
    try:
        observed = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise ProbeError("CHILD_OUTPUT_INVALID", "observed child did not return JSON") from error
    if not isinstance(observed, dict) or not all(
        name in ALLOWED_ENVIRONMENT_NAMES and isinstance(value, str)
        for name, value in observed.items()
    ):
        raise ProbeError("CHILD_OUTPUT_INVALID", "observed child returned unexpected fields")
    return observed


def run(
    workspace_value: str | Path,
    *,
    probe_receipt: str | None = None,
    runtime_arguments: tuple[str, ...] | None = None,
) -> dict[str, object]:
    """Observe the fixed child process and compare it to exactly one hook candidate."""

    workspace = resolve_path(workspace_value)
    fixture = read_fixture(workspace)
    if Path.cwd().resolve() != workspace:
        raise ProbeError("RUN_CWD_MISMATCH", "run must start from the fixture workspace root")

    environment = allowed_environment()
    child_values = observe_child_environment(workspace, environment)
    observations = load_observations(workspace)
    if fixture["transport"] == UPDATED_INPUT_TRANSPORT:
        expected_runtime_arguments = runtime_arguments or receipt_runtime_arguments(
            workspace,
            probe_receipt or "",
        )
        comparison = consume_updated_input_receipt(
            fixture,
            workspace,
            child_values,
            probe_receipt,
            expected_runtime_arguments,
        )
    else:
        comparison = compare_child_context(fixture, observations, child_values)
    run_record = {
        "protocol": PROTOCOL,
        "fixture_id": fixture["fixture_id"],
        "observed_at": utc_now(),
        "purpose": "child_identity_comparison_only",
        "evidence_label": "candidate",
        "child_environment": child_values,
        "comparison": comparison,
    }
    run_path = runs_directory(workspace) / f"{secrets.token_hex(12)}.json"
    write_private_json(run_path, run_record)
    return {
        **run_record,
        "run_path": str(run_path),
        "observation_count": len(observations),
    }


def inspect(workspace_value: str | Path) -> dict[str, object]:
    workspace = resolve_path(workspace_value)
    fixture = read_fixture(workspace)
    configuration_path = hook_configuration_path(workspace, fixture["host"])
    configuration = read_private_json(configuration_path)
    if not isinstance(configuration, dict):
        raise ProbeError("INVALID_FIXTURE", "hook configuration must be a JSON object")

    runs = []
    for path in sorted(runs_directory(workspace).glob("*.json")):
        run_record = read_private_json(path)
        if not isinstance(run_record, dict):
            raise ProbeError("INVALID_FIXTURE", f"run record is not an object: {path}")
        runs.append(run_record)
    return {
        "protocol": PROTOCOL,
        "workspace": str(workspace),
        "host": fixture["host"],
        "transport": fixture["transport"],
        "fixture_id": fixture["fixture_id"],
        "evidence_label": "candidate",
        "config_path": str(configuration_path),
        "config": configuration,
        "observations": load_observations(workspace),
        "receipts": load_receipts(workspace)
        if fixture["transport"] == UPDATED_INPUT_TRANSPORT
        else [],
        "runs": runs,
    }


def parser() -> argparse.ArgumentParser:
    argument_parser = argparse.ArgumentParser(description="prepare a private native context probe")
    commands = argument_parser.add_subparsers(dest="command", required=True)

    preparing = commands.add_parser("prepare", help="create an empty local host fixture")
    preparing.add_argument("--workspace", required=True)
    preparing.add_argument("--host", choices=sorted(SUPPORTED_HOSTS), required=True)
    preparing.add_argument(
        "--transport",
        choices=(DEFAULT_TRANSPORT, UPDATED_INPUT_TRANSPORT),
        default=DEFAULT_TRANSPORT,
        help="use the opt-in WorkBuddy updatedInput receipt transport",
    )

    receiving = commands.add_parser("receive", help="record bounded hook JSON from stdin")
    receiving.add_argument("--workspace", required=True)
    receiving.add_argument(
        "--fixture-source",
        action="store_true",
        help="label this local injection as a fixture candidate, never native proof",
    )

    running = commands.add_parser("run", help="compare one fixed child environment to a hook candidate")
    running.add_argument("--workspace", required=True)
    running.add_argument("--probe-receipt")

    inspecting = commands.add_parser("inspect", help="print private fixture metadata as JSON")
    inspecting.add_argument("--workspace", required=True)
    return argument_parser


def public_run_response(response: dict[str, object]) -> dict[str, object]:
    """Return the small result safe for a host tool's visible stdout."""

    comparison = response["comparison"]
    if not isinstance(comparison, dict):
        raise ProbeError("INVALID_FIXTURE", "run comparison must be a JSON object")
    return {
        "protocol": PROTOCOL,
        "evidence_label": "candidate",
        "status": comparison["status"],
        "reason": comparison["reason"],
        "report_path": response["run_path"],
        "observation_count": response["observation_count"],
    }


def main(arguments: list[str] | None = None) -> int:
    runtime_arguments = tuple(arguments) if arguments is not None else tuple(sys.argv[1:])
    parsed = parser().parse_args(runtime_arguments)
    try:
        if parsed.command == "prepare":
            response = prepare(parsed.workspace, parsed.host, transport=parsed.transport)
        elif parsed.command == "receive":
            source = "fixture" if parsed.fixture_source else "hook"
            response = receive_json_bytes(parsed.workspace, read_hook_stdin(), source=source)
        elif parsed.command == "run":
            response = run(
                parsed.workspace,
                probe_receipt=parsed.probe_receipt,
                runtime_arguments=runtime_arguments,
            )
        else:
            response = inspect(parsed.workspace)
    except (OSError, subprocess.SubprocessError) as error:
        response = {"protocol": PROTOCOL, "ok": False, "code": "LOCAL_IO_FAILED", "message": str(error)}
        print(json.dumps(response, sort_keys=True), file=sys.stderr)
        return 1
    except ProbeError as error:
        response = {"protocol": PROTOCOL, "ok": False, "code": error.code, "message": str(error)}
        print(json.dumps(response, sort_keys=True), file=sys.stderr)
        return 1

    if parsed.command == "receive" and "hook_output" in response:
        hook_response = {"hookSpecificOutput": response["hook_output"]}
        print(json.dumps(hook_response, separators=(",", ":"), sort_keys=True))
        return 0
    if parsed.command == "run":
        response = public_run_response(response)
    print(json.dumps({"protocol": PROTOCOL, "ok": True, **response}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
