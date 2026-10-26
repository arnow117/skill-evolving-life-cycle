"""Project-owned native hook adapter for managed suites.

The adapter receives one host callback on standard input and retains only the
small allowlisted shape needed by :mod:`selc.managed_runtime`.  It deliberately
does not rewrite a command, grant a permission, or print a hook response.  A
host callback that lacks the complete native facts is recorded only when its
identity can be made durable; it never becomes a substitute for a future
callback or a session-wide "latest" receipt.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .errors import SelcError
from .install import install_root
from .managed_runtime import (
    PERMISSION_DENIED_PHASE,
    PERMISSION_PHASE,
    POST_PHASE,
    PRE_PHASE,
    STOP_PHASE,
    USER_PROMPT_PHASE,
    WORKBUDDY_SUPPORTED_CLIENT_VERSIONS,
    observe_request_boundary,
    parse_installed_managed_command,
)
from .manifest import load_manifest, normalize_declared_skills

HOOK_EVENT_TO_PHASE = {"PreToolUse": PRE_PHASE, "PostToolUse": POST_PHASE}
HOST_TOOL_NAMES = {
    "codex": {"Bash", "exec_command", "shell"},
    "workbuddy": {"Bash", "exec_command", "shell"},
}
WORKBUDDY_SKILL_TOOL_NAME = "Skill"


def receive_native_hook(
    *,
    project_root: Path,
    suite_id: str,
    host: str,
    payload: dict[str, object],
    environment: dict[str, str],
) -> dict[str, object]:
    """Persist one relevant native tool boundary without changing host behavior.

    The returned object is for local diagnostics.  The command-hook entrypoint
    discards it, keeping successful hooks silent and free of host-specific
    response fields such as ``updatedInput`` or permission decisions.
    """

    if host not in HOST_TOOL_NAMES:
        raise SelcError("NATIVE_HOST_UNSUPPORTED", "native host is unsupported")
    event_name = _required_string(payload, "hook_event_name")
    if event_name == USER_PROMPT_PHASE:
        return _receive_user_prompt(
            project_root=project_root,
            suite_id=suite_id,
            host=host,
            payload=payload,
            environment=environment,
        )
    if event_name == STOP_PHASE:
        return _receive_stop(
            project_root=project_root,
            suite_id=suite_id,
            host=host,
            payload=payload,
            environment=environment,
        )
    if event_name in {PERMISSION_PHASE, PERMISSION_DENIED_PHASE}:
        return _receive_permission_event(
            project_root=project_root,
            suite_id=suite_id,
            host=host,
            payload=payload,
            environment=environment,
            phase=event_name,
        )
    phase = HOOK_EVENT_TO_PHASE.get(event_name)
    if phase is None:
        return {"status": "ignored", "reason": "event-not-a-tool-boundary"}
    tool_name = _required_string(payload, "tool_name")
    if host == "workbuddy" and tool_name == WORKBUDDY_SKILL_TOOL_NAME:
        return _receive_workbuddy_skill_boundary(
            project_root=project_root,
            suite_id=suite_id,
            payload=payload,
            environment=environment,
            phase=phase,
        )
    if tool_name not in HOST_TOOL_NAMES.get(host, set()):
        return {"status": "ignored", "reason": "tool-not-shell-boundary"}
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        raise SelcError(
            "NATIVE_COLLECTOR_INVALID",
            "native hook tool input must be an object",
            "pause before managed execution and repair the host collector",
        )

    command = tool_input.get("command")
    if not isinstance(command, str):
        raise SelcError(
            "NATIVE_COLLECTOR_INVALID",
            "native hook shell command must be text",
            "pause before managed execution and repair the host collector",
        )
    managed_command = parse_installed_managed_command(
        command,
        suite_id,
        project_root=project_root,
    )

    identity = _hook_identity(host, payload, require_tool=True)
    _validate_hook_environment_identity(host, identity, environment)
    request = payload.get("managed_request")
    if not isinstance(request, dict):
        request = {"source": "user-prompt"}

    collector_payload: dict[str, object] = {
        "host": host,
        **_host_metadata(host, payload, environment),
        "cwd": _required_string(payload, "cwd"),
        "session_id": identity["session_id"],
        "generation_id": identity["generation_id"],
        "request_id": identity["request_id"],
        "tool_use_id": identity["tool_use_id"],
        "phase": phase,
        "tool_name": tool_name,
        "tool_input": {"command": command},
        "request": request,
        "transcript_path": _required_string(payload, "transcript_path"),
        "prefix_length": _prompt_prefix_length(payload),
        "causation": _native_causation(payload),
    }
    if managed_command is None:
        collector_payload["unmanaged_tool"] = True
    for key in ("message_id", "root_request_id", "parent_request_id", "parent_message_id"):
        value = identity.get(key)
        if value is not None:
            collector_payload[key] = value
    result = observe_request_boundary(
        project_root=project_root,
        suite_id=suite_id,
        payload=collector_payload,
        trusted_producer=True,
    )
    if managed_command is not None and phase == PRE_PHASE and result.get("status") != "recorded":
        raise SelcError(
            "NATIVE_REQUEST_UNAVAILABLE",
            "managed tool callback has no usable native request boundary",
            "pause before managed execution and repair the host collector",
        )
    return result


def _receive_workbuddy_skill_boundary(
    *,
    project_root: Path,
    suite_id: str,
    payload: dict[str, object],
    environment: dict[str, str],
    phase: str,
) -> dict[str, object]:
    """Record a WorkBuddy ``Skill`` hook without issuing a business receipt.

    WorkBuddy invokes its pre-hook while the pending native Skill call is still
    present in the transcript.  The runtime independently verifies that
    frozen fact before it records a request anchor.  This adapter retains only
    a parsed DTO digest and the original ``args`` digest, never the raw Skill
    arguments or an inferred command.
    """

    tool_input = payload.get("tool_input")
    request, skill_name, args_digest, valid = _workbuddy_skill_request(
        tool_input,
        declared_skills=_declared_skill_names(project_root, suite_id),
    )
    identity = _hook_identity("workbuddy", payload, require_tool=True)
    collector_payload: dict[str, object] = {
        "host": "workbuddy",
        **_host_metadata("workbuddy", payload, environment),
        "cwd": _required_string(payload, "cwd"),
        "session_id": identity["session_id"],
        "generation_id": identity["generation_id"],
        "request_id": identity["request_id"],
        "tool_use_id": identity["tool_use_id"],
        "phase": phase,
        "tool_name": WORKBUDDY_SKILL_TOOL_NAME,
        "request": request,
        "technical_skill_event": True,
        "technical_skill_pre": phase == PRE_PHASE,
        "technical_skill_valid": valid,
        "technical_skill_name": skill_name,
        "technical_skill_args_digest": args_digest,
        "transcript_path": _required_string(payload, "transcript_path"),
        "prefix_length": _prompt_prefix_length(payload),
        "causation": _native_causation(payload),
    }
    for key in ("message_id", "root_request_id", "parent_request_id", "parent_message_id"):
        value = identity.get(key)
        if value is not None:
            collector_payload[key] = value
    return observe_request_boundary(
        project_root=project_root,
        suite_id=suite_id,
        payload=collector_payload,
        trusted_producer=True,
    )


def _workbuddy_skill_request(
    tool_input: object,
    *,
    declared_skills: frozenset[str],
) -> tuple[dict[str, object], str, str, bool]:
    """Decode the one observed WorkBuddy ``Skill`` input object.

    Invalid variants are still represented by an immutable technical event so
    a later, different callback cannot replace the first failed proof.  They
    use the finite unknown DTO and never reach a business receipt.
    """

    skill_name = ""
    args_value: object = None
    if isinstance(tool_input, dict) and set(tool_input) == {"skill", "args"}:
        candidate_skill = tool_input.get("skill")
        if isinstance(candidate_skill, str):
            skill_name = candidate_skill
        args_value = tool_input.get("args")

    args_text = args_value if isinstance(args_value, str) else ""
    args_digest = hashlib.sha256(args_text.encode("utf-8")).hexdigest()
    parsed_input = _complete_json_object(args_text)
    if not skill_name or parsed_input is None:
        return (
            {"skill": skill_name, "input": {"classification": "unknown"}},
            skill_name,
            args_digest,
            False,
        )

    business_input = dict(parsed_input)
    has_correction = "selc" in business_input
    correction_value = business_input.pop("selc") if has_correction else None
    request: dict[str, object] = {"skill": skill_name, "input": business_input}
    if has_correction:
        request["correction"] = _correction_declaration(correction_value)
    return request, skill_name, args_digest, skill_name in declared_skills


def _complete_json_object(value: str) -> dict[str, object] | None:
    """Accept one complete JSON object without unwrapping or decoding extras."""

    try:
        parsed, end = json.JSONDecoder().raw_decode(value)
    except json.JSONDecodeError:
        return None
    if end != len(value) or not isinstance(parsed, dict):
        return None
    return parsed


def _receive_user_prompt(
    *,
    project_root: Path,
    suite_id: str,
    host: str,
    payload: dict[str, object],
    environment: dict[str, str],
) -> dict[str, object]:
    """Store a strict pre-append request boundary before any managed tool call.

    A finite object can optionally carry a genuine ``@skill`` prefix.  Other
    text is retained as a bounded hash with an unknown classification, rather
    than being silently discarded or promoted into an objective.
    """

    request = _managed_prompt_request(
        host,
        payload,
        declared_skills=_declared_skill_names(project_root, suite_id),
    )
    identity = _hook_identity(host, payload, require_tool=False)
    _validate_hook_environment_identity(host, identity, environment)
    collector_payload: dict[str, object] = {
        "host": host,
        **_host_metadata(host, payload, environment),
        "cwd": _required_string(payload, "cwd"),
        "session_id": identity["session_id"],
        "generation_id": identity["generation_id"],
        "request_id": identity["request_id"],
        "phase": USER_PROMPT_PHASE,
        "request": request,
        "transcript_path": _required_string(payload, "transcript_path"),
        "prefix_length": _prompt_prefix_length(payload),
        "causation": _native_causation(payload),
    }
    for key in ("message_id", "root_request_id", "parent_request_id", "parent_message_id"):
        value = identity.get(key)
        if value is not None:
            collector_payload[key] = value
    return observe_request_boundary(
        project_root=project_root,
        suite_id=suite_id,
        payload=collector_payload,
        trusted_producer=True,
    )


def _receive_stop(
    *,
    project_root: Path,
    suite_id: str,
    host: str,
    payload: dict[str, object],
    environment: dict[str, str],
) -> dict[str, object]:
    """Close activity only when a native stop has a complete exact identity."""

    try:
        identity = _hook_identity(host, payload, require_tool=False)
        _validate_hook_environment_identity(host, identity, environment)
        collector_payload: dict[str, object] = {
            "host": host,
            **_host_metadata(host, payload, environment),
            "cwd": _required_string(payload, "cwd"),
            "session_id": identity["session_id"],
            "generation_id": identity["generation_id"],
            "request_id": identity["request_id"],
            "phase": STOP_PHASE,
            "request": {"source": "user-prompt"},
            "transcript_path": _required_string(payload, "transcript_path"),
            "prefix_length": _prompt_prefix_length(payload),
            "causation": _native_causation(payload),
        }
    except SelcError:
        return {"status": "ignored", "reason": "stop-context-unavailable"}
    for key in ("message_id", "root_request_id", "parent_request_id", "parent_message_id"):
        value = identity.get(key)
        if value is not None:
            collector_payload[key] = value
    return observe_request_boundary(
        project_root=project_root,
        suite_id=suite_id,
        payload=collector_payload,
        trusted_producer=True,
    )


def _receive_permission_event(
    *,
    project_root: Path,
    suite_id: str,
    host: str,
    payload: dict[str, object],
    environment: dict[str, str],
    phase: str,
) -> dict[str, object]:
    """Record a technical permission boundary without returning a host decision."""

    try:
        identity = _hook_identity(host, payload, require_tool=False)
        _validate_hook_environment_identity(host, identity, environment)
        collector_payload: dict[str, object] = {
            "host": host,
            **_host_metadata(host, payload, environment),
            "cwd": _required_string(payload, "cwd"),
            "session_id": identity["session_id"],
            "generation_id": identity["generation_id"],
            "request_id": identity["request_id"],
            "phase": phase,
            "request": {"source": "user-prompt"},
            "transcript_path": _required_string(payload, "transcript_path"),
            "prefix_length": _prompt_prefix_length(payload),
            "causation": "permission",
        }
    except SelcError:
        return {"status": "ignored", "reason": "permission-context-unavailable"}
    if identity.get("tool_use_id"):
        collector_payload["tool_use_id"] = identity["tool_use_id"]
    return observe_request_boundary(
        project_root=project_root,
        suite_id=suite_id,
        payload=collector_payload,
        trusted_producer=True,
    )


def read_hook_payload(stream: Any) -> dict[str, object]:
    """Read one bounded JSON hook payload from standard input."""

    raw = stream.buffer.read(64 * 1024 + 1)
    if len(raw) > 64 * 1024:
        raise SelcError(
            "NATIVE_COLLECTOR_INVALID",
            "native hook payload exceeds the supported bound",
            "pause before managed execution and repair the host collector",
        )
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise SelcError(
            "NATIVE_COLLECTOR_INVALID",
            "native hook input is not JSON",
            "pause before managed execution and repair the host collector",
        ) from error
    if not isinstance(value, dict):
        raise SelcError(
            "NATIVE_COLLECTOR_INVALID",
            "native hook input must be a JSON object",
            "pause before managed execution and repair the host collector",
        )
    return value


def current_environment() -> dict[str, str]:
    """Expose a copy for the command wrapper and deterministic local tests."""

    return dict(os.environ)


def _is_managed_command(command: str, suite_id: str, project_root: Path | None = None) -> bool:
    return (
        parse_installed_managed_command(
            command,
            suite_id,
            project_root=project_root,
        )
        is not None
    )


def _declared_skill_names(project_root: Path, suite_id: str) -> frozenset[str]:
    """Read names only from the active installation for a native skill chip."""

    try:
        manifest = load_manifest(install_root(project_root) / suite_id / "current")
        return frozenset(entry["name"] for entry in normalize_declared_skills(manifest["skills"]))
    except (OSError, SelcError, TypeError, KeyError):
        return frozenset()


def _managed_prompt_request(
    host: str,
    payload: dict[str, object],
    *,
    declared_skills: frozenset[str] | None = None,
) -> dict[str, object]:
    prompt = payload.get("prompt")
    if not isinstance(prompt, str):
        raise SelcError("NATIVE_COLLECTOR_INVALID", "native prompt is not text")
    skill = ""
    encoded_input = prompt
    workbuddy_version = payload.get("version")
    workbuddy_skill_chip = (
        host == "workbuddy"
        and isinstance(workbuddy_version, str)
        and workbuddy_version in WORKBUDDY_SUPPORTED_CLIENT_VERSIONS
        and prompt.startswith("@//")
    )
    if workbuddy_skill_chip:
        parsed = _workbuddy_skill_chip_request(prompt)
        if parsed is None:
            return _unknown_prompt_request(prompt)
        skill, input_value = parsed
    elif prompt.startswith("@"):
        prefix, separator, encoded_input = prompt.partition(" ")
        skill = prefix[1:]
        if not separator or not skill:
            return _unknown_prompt_request(prompt)
        try:
            input_value = json.loads(encoded_input)
        except json.JSONDecodeError:
            return _unknown_prompt_request(prompt)
    else:
        try:
            input_value = json.loads(encoded_input)
        except json.JSONDecodeError:
            return _unknown_prompt_request(prompt)
    if not isinstance(input_value, dict):
        return _unknown_prompt_request(prompt)
    if workbuddy_skill_chip and (declared_skills is None or skill not in declared_skills):
        return _unknown_prompt_request(prompt)
    business_input = dict(input_value)
    has_correction = "selc" in business_input
    correction = business_input.pop("selc") if has_correction else None
    result: dict[str, object] = {
        "input": business_input,
        "raw_prompt_digest": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
    }
    if skill:
        result["skill"] = skill
    if has_correction:
        result["correction"] = _correction_declaration(correction)
    return result


def _workbuddy_skill_chip_request(prompt: str) -> tuple[str, object] | None:
    """Decode only the supported WorkBuddy skill-chip serialization we observed."""

    encoded = prompt.removeprefix("@//")
    skill, separator, body = encoded.partition("/")
    if not skill or not separator or not body or "/" in skill:
        return None
    try:
        value, end = json.JSONDecoder().raw_decode(body)
    except json.JSONDecodeError:
        return None
    if end != len(body) or not isinstance(value, dict):
        return None
    return skill, value


def _correction_declaration(value: object) -> dict[str, object]:
    """Keep a narrow correction opt-in outside the declared business DTO.

    The original prompt digest and the frozen native history prove this value
    later.  This parser does not attach a task, call, receipt, or any other
    caller-supplied runtime identifier to the request.
    """

    if not isinstance(value, dict) or set(value) != {"intent", "target", "preserve_goal"}:
        return {"valid": False}
    intent = value.get("intent")
    target = value.get("target")
    preserve_goal = value.get("preserve_goal")
    if (
        intent != "correct_delivery"
        or not isinstance(target, str)
        or not target
        or type(preserve_goal) is not bool
        or preserve_goal is not True
    ):
        return {"valid": False}
    return {
        "intent": intent,
        "target": target,
        "preserve_goal": True,
        "valid": True,
    }


def _unknown_prompt_request(prompt: str) -> dict[str, object]:
    return {
        "input": {
            "classification": "unknown",
        },
        "raw_prompt_digest": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
    }


def _hook_identity(host: str, payload: dict[str, object], *, require_tool: bool) -> dict[str, str]:
    """Read callback identities from the host JSON, never the hook process env."""

    session_id = _identity_value(payload, "session_id", "conversation_id", "sessionId")
    if host == "codex":
        generation_id = _identity_value(payload, "turn_id", "generation_id", "generationId")
        request_id = generation_id
    else:
        generation_id = _identity_value(payload, "generation_id", "turn_id", "generationId")
        request_id = _identity_value(
            payload,
            "request_id",
            "conversation_request_id",
            "conversationRequestId",
            default=generation_id,
        )
    identity = {
        "session_id": session_id,
        "generation_id": generation_id,
        "request_id": request_id,
    }
    tool_use_id = _identity_value(
        payload,
        "tool_use_id",
        "tool_call_id",
        "call_id",
        default="",
    )
    if require_tool and not tool_use_id:
        raise SelcError("NATIVE_CONTEXT_UNAVAILABLE", "native tool callback has no tool identity")
    if tool_use_id:
        identity["tool_use_id"] = tool_use_id
    for target, names in {
        "message_id": ("message_id", "conversation_message_id"),
        "root_request_id": ("root_request_id",),
        "parent_request_id": ("parent_request_id",),
        "parent_message_id": ("parent_id", "parentId"),
    }.items():
        value = _identity_value(payload, *names, default="")
        if value:
            identity[target] = value
    return identity


def _validate_hook_environment_identity(
    host: str, identity: dict[str, str], environment: dict[str, str]
) -> None:
    """Reject a supplied native identity only when it contradicts the callback.

    Codex's thread environment is not its callback turn.  The runtime records
    the callback turn/request and later uses the child session plus tool ID to
    select that pre-event, so this comparison deliberately excludes thread.
    """

    if host != "codex":
        return
    for key, expected in (
        ("CODEX_SESSION_ID", identity["session_id"]),
        ("CODEX_TOOL_CALL_ID", identity.get("tool_use_id", "")),
    ):
        actual = environment.get(key)
        if actual and expected and actual != expected:
            raise SelcError(
                "NATIVE_CONTEXT_CONFLICT",
                "Codex hook environment conflicts with the native callback identity",
                "pause before managed execution and repair the Codex collector",
            )


def _host_metadata(
    host: str, payload: dict[str, object], environment: dict[str, str]
) -> dict[str, str]:
    """Extract only metadata native to the reported host callback.

    WorkBuddy's known callbacks expose ``client`` and ``version``.  Codex hook
    callbacks commonly omit both, so its runtime version is taken from the
    child environment and the surface remains explicitly unknown until native
    metadata supplies it.
    """

    if host == "workbuddy":
        return {
            "surface": _required_string(payload, "client"),
            "client_version": _required_string(payload, "version"),
        }
    metadata = _codex_history_metadata(payload)
    callback_version = _first_string(payload, "version", "cli_version")
    environment_version = environment.get("CODEX_VERSION")
    history_version = metadata["version"]
    versions = {
        value
        for value in (callback_version, environment_version, history_version)
        if isinstance(value, str) and value
    }
    if len(versions) > 1:
        raise SelcError(
            "NATIVE_CONTEXT_CONFLICT",
            "Codex callback, environment, and session metadata versions disagree",
            "pause before managed execution and repair the Codex collector",
        )
    version = next(iter(versions), None)
    if not version:
        raise SelcError(
            "NATIVE_CONTEXT_UNAVAILABLE",
            "Codex callback has no current CODEX_VERSION",
            "pause before managed execution and repair the Codex collector",
        )
    return {
        "surface": metadata["surface"],
        "client_version": version,
    }


def _codex_history_metadata(payload: dict[str, object]) -> dict[str, str]:
    """Read one callback-owned Codex session metadata record exactly.

    A hook version is useful only when its native session record confirms the
    same session, canonical workspace, and CLI version.  The session metadata
    ``source`` is observational: only the observed ``exec`` source is labelled
    as the CLI surface; other sources deliberately remain unknown.
    """

    raw_path = payload.get("transcript_path")
    if not isinstance(raw_path, str) or not raw_path:
        raise SelcError(
            "NATIVE_CONTEXT_UNAVAILABLE",
            "Codex callback has no native session metadata path",
            "pause before managed execution and repair the Codex collector",
        )
    path = Path(raw_path)
    try:
        resolved = path.resolve(strict=True)
        codex_root = Path.home().resolve() / ".codex" / "sessions"
        resolved.relative_to(codex_root)
        with resolved.open("rb") as stream:
            first_line = stream.readline(64 * 1024 + 1)
        expected_cwd = Path(_required_string(payload, "cwd")).resolve(strict=True)
        expected_session = _identity_value(payload, "session_id", "conversation_id", "sessionId")
    except (OSError, ValueError) as error:
        raise SelcError(
            "NATIVE_CONTEXT_UNAVAILABLE",
            "Codex session metadata is unavailable or outside the native store",
            "pause before managed execution and repair the Codex collector",
        ) from error
    if len(first_line) > 64 * 1024:
        raise SelcError(
            "NATIVE_CONTEXT_UNAVAILABLE",
            "Codex session metadata exceeds the supported bound",
            "pause before managed execution and repair the Codex collector",
        )
    try:
        record = json.loads(first_line)
    except json.JSONDecodeError as error:
        raise SelcError(
            "NATIVE_CONTEXT_UNAVAILABLE",
            "Codex session metadata is not valid JSON",
            "pause before managed execution and repair the Codex collector",
        ) from error
    if not isinstance(record, dict) or record.get("type") != "session_meta":
        raise SelcError(
            "NATIVE_CONTEXT_UNAVAILABLE",
            "Codex session metadata has an unsupported serializer shape",
            "pause before managed execution and repair the Codex collector",
        )
    metadata = record.get("payload")
    if not isinstance(metadata, dict):
        raise SelcError(
            "NATIVE_CONTEXT_UNAVAILABLE",
            "Codex session metadata has no payload",
            "pause before managed execution and repair the Codex collector",
        )
    identities = {
        value
        for key in ("id", "session_id")
        if isinstance((value := metadata.get(key)), str) and value
    }
    if not identities:
        raise SelcError(
            "NATIVE_CONTEXT_UNAVAILABLE",
            "Codex session metadata has no session identity",
            "pause before managed execution and repair the Codex collector",
        )
    if len(identities) != 1 or identities != {expected_session}:
        raise SelcError(
            "NATIVE_CONTEXT_CONFLICT",
            "Codex callback and session metadata identities disagree",
            "pause before managed execution and repair the Codex collector",
        )
    metadata_cwd = metadata.get("cwd")
    if not isinstance(metadata_cwd, str) or not metadata_cwd:
        raise SelcError(
            "NATIVE_CONTEXT_UNAVAILABLE",
            "Codex session metadata has no workspace identity",
            "pause before managed execution and repair the Codex collector",
        )
    try:
        normalized_metadata_cwd = Path(metadata_cwd).resolve(strict=True)
    except OSError as error:
        raise SelcError(
            "NATIVE_CONTEXT_UNAVAILABLE",
            "Codex session metadata workspace is unavailable",
            "pause before managed execution and repair the Codex collector",
        ) from error
    if normalized_metadata_cwd != expected_cwd:
        raise SelcError(
            "NATIVE_CONTEXT_CONFLICT",
            "Codex callback and session metadata workspaces disagree",
            "pause before managed execution and repair the Codex collector",
        )
    version = metadata.get("cli_version")
    if not isinstance(version, str) or not version:
        raise SelcError(
            "NATIVE_CONTEXT_UNAVAILABLE",
            "Codex session metadata has no CLI version",
            "pause before managed execution and repair the Codex collector",
        )
    source = metadata.get("source")
    return {
        "version": version,
        "surface": "cli" if source == "exec" else "unknown",
    }


def _identity_value(payload: dict[str, object], *keys: str, default: str | None = None) -> str:
    values = {
        str(payload[key]) for key in keys if isinstance(payload.get(key), str) and payload[key]
    }
    if not values:
        if default is not None:
            return default
        raise SelcError("NATIVE_CONTEXT_UNAVAILABLE", f"native callback lacks {keys[0]}")
    if len(values) != 1:
        raise SelcError(
            "NATIVE_CONTEXT_CONFLICT", f"native callback identities disagree for {keys[0]}"
        )
    return values.pop()


def _required_string(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 512:
        raise SelcError(
            "NATIVE_COLLECTOR_INVALID",
            f"native hook {key} is invalid",
            "pause before managed execution and repair the host collector",
        )
    return value


def _first_string(payload: dict[str, object], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _required_nonnegative_int(payload: dict[str, object], key: str) -> int:
    value = payload.get(key)
    if type(value) is not int or value < 0:
        raise SelcError(
            "NATIVE_COLLECTOR_INVALID",
            f"native hook {key} is invalid",
            "pause before managed execution and repair the host collector",
        )
    return value


def _prompt_prefix_length(payload: dict[str, object]) -> int:
    """Use a host-provided length only for deterministic fixtures.

    Native callbacks provide the transcript path.  The collector itself reads
    the current byte length at callback time and the runtime then re-reads that
    exact prefix before projecting it.  An unavailable path deliberately
    becomes an unverifiable root proof instead of a guessed boundary.
    """

    value = payload.get("prefix_length")
    if type(value) is int and value >= 0:
        return value
    path = payload.get("transcript_path")
    if not isinstance(path, str) or not path:
        return -1
    try:
        return Path(path).stat().st_size
    except OSError:
        return -1


def _native_causation(payload: dict[str, object]) -> str:
    """Classify finite native markers without treating prose as a proven relation."""

    is_prompt = payload.get("hook_event_name") == USER_PROMPT_PHASE
    if payload.get("is_internal_continuation") is True:
        return "automatic_retry"
    if payload.get("is_input_preparation") is True:
        return "input_preparation"
    if payload.get("is_permission_boundary") is True:
        return "permission"
    if payload.get("is_business_correction") is True:
        return "business_correction"
    if payload.get("is_feedback") is True:
        return "audit"
    prompt = payload.get("prompt")
    if not is_prompt or not isinstance(prompt, str):
        return "unknown"
    normalized = prompt.strip().casefold()
    if normalized.startswith(
        (
            "纠正",
            "更正",
            "修正",
            "改正",
            "修复刚才",
            "correct ",
            "correction:",
            "fix the previous",
            "revise the previous",
        )
    ):
        return "correction_candidate"
    if normalized.startswith(("反馈", "评价", "feedback:", "review:")):
        return "audit"
    return "unknown"


def _request_id(host: str, context: dict[str, str], generation_id: str) -> str:
    """Use the native request namespace available on each host.

    Codex exposes a tool-call identifier for the pre/post boundary, not a
    separate request identifier.  Its host generation therefore carries the
    request context while the receipt still retains the exact tool-call ID.
    WorkBuddy supplies a distinct conversation request ID directly.
    """

    if host == "codex":
        return generation_id
    return context["request_id"]
