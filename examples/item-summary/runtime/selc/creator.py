"""Prepare and verify a constrained creator invocation without launching it.

The production caller owns the actual Codex invocation.  This module freezes
the editable candidate surface and a native-profile request so a later runner
cannot turn a candidate proposal into access to holdout, oracle, runtime, or
production records.  It intentionally does not simulate an operating-system
permission profile: only the native CLI evidence collected by the caller can
establish that the profile was applied.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .errors import SelcError
from .evaluation import (
    CODEX_ALPHA11_VERSION,
    REAL_EXECUTION_TYPE,
    _is_alpha11_arg0_path,
    _load_real_plan,
    _normalise_native_policy_entries,
)
from .managed_runtime import _trusted_posix_account_home
from .util import file_hash, json_hash, read_json, tree_hash, write_json

CREATOR_MODEL = "gpt-5.6-terra"
MAX_CREATOR_BRIEF_BYTES = 64 * 1024
CREATOR_DISABLED_FEATURES = (
    "apps",
    "plugins",
    "remote_plugin",
    "skill_search",
    "multi_agent",
    "computer_use",
    "browser_use",
    "image_generation",
)


def prepare_creator_request(
    plan_path: Path,
    candidate_root: Path,
    output: Path,
    *,
    executor_binary: Path | None = None,
    executor_version: str | None = None,
    brief_path: Path | None = None,
    skill_creator_root: Path | None = None,
    creator_python_root: Path | None = None,
) -> dict[str, Any]:
    """Freeze a creator-only edit request and its native profile declaration."""

    if output.exists():
        raise SelcError(
            "PATH_CONFLICT", "creator request already exists", "choose a new request path"
        )
    plan = _load_real_plan(plan_path)
    _assert_candidate_surface(candidate_root)
    editable = [candidate_root / "skills", candidate_root / "resources"]
    protected = _protected_paths(plan, candidate_root)
    protected_hashes = _protected_hashes(protected)
    executor = _creator_executor(executor_binary, executor_version)
    brief = _creator_brief(brief_path)
    dependencies = _creator_readonly_dependencies(
        skill_creator_root,
        creator_python_root,
        candidate_root,
        protected,
    )
    profile = {
        "schema_version": 1,
        "adapter": "codex-cli",
        "model": CREATOR_MODEL,
        "network": "restricted",
        "approval_policy": "never",
        "filesystem": {
            "write": [str(path.resolve()) for path in editable],
            "read": [":minimal", str(candidate_root.resolve()), *dependencies["read_roots"]],
            "deny": [str(path.resolve()) for path in protected],
        },
        "project_context": {
            "project_doc_max_bytes": 0,
            "project_root_markers": [],
        },
        "readonly_dependencies": dependencies,
        "tools": {
            "skill_creator": "enabled",
            "mcp": "disabled",
            "apps": "disabled",
            "browser": "disabled",
            "agents": "disabled",
        },
        "fixed_arguments": _creator_cli_arguments(candidate_root, protected, dependencies),
    }
    if executor is not None:
        profile["executor"] = executor
    candidate_before_hash = tree_hash(candidate_root)
    run_id = json_hash(
        {
            "plan_hash": plan["plan_hash"],
            "candidate_before_hash": candidate_before_hash,
            "profile": profile,
            "brief": brief,
        }
    )
    request = {
        "schema_version": 1,
        "execution_type": REAL_EXECUTION_TYPE,
        "plan_path": str(plan_path.resolve()),
        "plan_hash": plan["plan_hash"],
        "candidate_path": str(candidate_root.resolve()),
        "candidate_before_hash": candidate_before_hash,
        "protected_before_hashes": protected_hashes,
        "profile": profile,
        "profile_hash": json_hash(profile),
        "brief": brief,
        "run_id": run_id,
        "prompt": _creator_prompt(brief, run_id),
    }
    request["request_hash"] = _request_hash(request)
    write_json(output, request)
    return request


def verify_creator_result(
    request_path: Path,
    candidate_root: Path,
    native_evidence_path: Path | None = None,
) -> dict[str, Any]:
    """Verify the candidate changed only on the surface the request granted."""

    request = read_json(request_path)
    if not isinstance(request, dict) or request.get("execution_type") != REAL_EXECUTION_TYPE:
        raise SelcError(
            "INVALID_CREATOR_REQUEST", "creator request is malformed", "prepare a new request"
        )
    request_hash = request.get("request_hash")
    unsigned = dict(request)
    unsigned.pop("request_hash", None)
    if request_hash != _request_hash(unsigned):
        raise SelcError(
            "CREATOR_REQUEST_TAMPERED", "creator request hash changed", "prepare a new request"
        )
    if Path(str(request.get("candidate_path", ""))).resolve() != candidate_root.resolve():
        raise SelcError(
            "CREATOR_REQUEST_TAMPERED",
            "creator result names another candidate",
            "use the bound candidate",
        )
    _assert_candidate_surface(candidate_root)
    before = request.get("candidate_before_hash")
    after = tree_hash(candidate_root)
    if before == after:
        raise SelcError(
            "NO_CONTRIBUTION_CHANGES",
            "creator did not change the candidate",
            "make a candidate-only proposal",
        )
    profile = request.get("profile")
    if not isinstance(profile, dict) or request.get("profile_hash") != json_hash(profile):
        raise SelcError(
            "CREATOR_REQUEST_TAMPERED", "creator profile changed", "prepare a new request"
        )
    protected = _protected_paths(_load_real_plan(Path(request["plan_path"])), candidate_root)
    expected_hashes = request.get("protected_before_hashes")
    current_hashes = _protected_hashes(protected)
    if not isinstance(expected_hashes, dict) or current_hashes != expected_hashes:
        raise SelcError(
            "CREATOR_PROTECTED_CONTENT_CHANGED",
            "protected plan, oracle, runtime, or record content changed during creator work",
            "prepare a fresh candidate and creator request",
        )
    if native_evidence_path is None:
        raise SelcError(
            "CREATOR_ISOLATION_UNVERIFIED",
            "candidate edits are visible, but no native permission evidence proves creator isolation",
            "provide the bounded raw native session evidence from the fixed creator invocation",
        )
    evidence = _read_native_evidence(native_evidence_path, request, profile, expected_hashes)
    return {
        "candidate_hash": after,
        "profile_hash": request["profile_hash"],
        "changed": True,
        "isolation_verified": True,
        "native_session_hash": evidence["raw_session_sha256"],
    }


def _creator_executor(binary: Path | None, version: str | None) -> dict[str, str] | None:
    """Freeze a maintainer-selected CLI executable when one is supplied."""

    if binary is None and version is None:
        return None
    if binary is None or not isinstance(version, str) or not version:
        raise SelcError(
            "INVALID_CREATOR_EXECUTOR",
            "creator executable path and version must be supplied together",
            "pass the exact supported Codex CLI path and version",
        )
    if binary.is_symlink() or not binary.is_file():
        raise SelcError(
            "INVALID_CREATOR_EXECUTOR",
            "creator executable must be a regular file",
            "pass the supported Codex CLI executable",
        )
    return {
        "path": str(binary.resolve()),
        "digest": file_hash(binary),
        "version": version,
    }


def _creator_cli_arguments(
    candidate_root: Path, protected: list[Path], dependencies: dict[str, object]
) -> list[str]:
    """Return the fixed public Codex argument suffix for a creator session.

    Candidate content cannot add arguments, environment variables, tools, or an
    alternative executor. A production caller also pins its executable and
    version in the prepared request.
    """

    skills = candidate_root / "skills"
    resources = candidate_root / "resources"
    candidate = str(candidate_root.resolve())
    read_roots = dependencies.get("read_roots")
    if not isinstance(read_roots, list) or not all(isinstance(path, str) for path in read_roots):
        raise SelcError(
            "INVALID_CREATOR_DEPENDENCIES",
            "creator readonly dependencies are not a frozen path list",
            "prepare the creator request with explicit trusted roots",
        )
    permissions = "permissions.selc-creator.filesystem={"
    permissions += '":minimal"="read",'
    permissions += f'{json.dumps(candidate)}="read",'
    permissions += f'{json.dumps(str(skills.resolve()))}="write",'
    permissions += f'{json.dumps(str(resources.resolve()))}="write"'
    for path in read_roots:
        permissions += f',{json.dumps(path)}="read"'
    for path in sorted({str(path.resolve()) for path in protected}):
        permissions += f',{json.dumps(path)}="deny"'
    permissions += "}"
    arguments = [
        "-a",
        "never",
        "exec",
        "--ignore-user-config",
        "--skip-git-repo-check",
        "--strict-config",
        "--json",
        "--color",
        "never",
        "-m",
        CREATOR_MODEL,
        "-c",
        'model_provider="openai"',
        "-c",
        'web_search="disabled"',
        "--enable",
        "hooks",
    ]
    for feature in CREATOR_DISABLED_FEATURES:
        arguments.extend(("--disable", feature))
    arguments.extend(
        (
            "-C",
            str(candidate_root.resolve()),
            "-c",
            'default_permissions="selc-creator"',
            "-c",
            permissions,
            "-c",
            "permissions.selc-creator.network.enabled=false",
            "-c",
            "project_doc_max_bytes=0",
            "-c",
            "project_root_markers=[]",
            "-",
        )
    )
    return arguments


def _creator_brief(path: Path | None) -> dict[str, object]:
    """Bind only a maintainer-supplied regression objective into the prompt.

    SELC never discovers oracle, holdout, or result paths on the creator's
    behalf.  A missing brief remains visible as diagnostic input rather than a
    false claim that a concrete improvement target was supplied.
    """

    if path is None:
        return {"status": "diagnostic-missing"}
    if path.is_symlink() or not path.is_file():
        raise SelcError(
            "INVALID_CREATOR_BRIEF",
            "creator brief must be a regular UTF-8 text file",
            "supply the maintainer regression brief explicitly",
        )
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise SelcError(
            "INVALID_CREATOR_BRIEF",
            "creator brief must contain strict UTF-8 text",
            "supply a short maintainer-owned regression brief",
        ) from error
    if not raw or len(raw) > MAX_CREATOR_BRIEF_BYTES or "\x00" in text:
        raise SelcError(
            "INVALID_CREATOR_BRIEF",
            "creator brief must be non-empty bounded text without NUL bytes",
            "supply only the explicit regression objective",
        )
    return {
        "status": "bound",
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "text": text,
    }


def _creator_readonly_dependencies(
    skill_creator_root: Path | None,
    creator_python_root: Path | None,
    candidate_root: Path,
    protected: list[Path],
) -> dict[str, object]:
    """Freeze the smallest trusted authoring roots needed by the fixed profile."""

    if skill_creator_root is None and creator_python_root is None:
        return {"status": "diagnostic-missing", "read_roots": []}
    if skill_creator_root is None or creator_python_root is None:
        raise SelcError(
            "INVALID_CREATOR_DEPENDENCIES",
            "skill-creator and Python runtime roots must be supplied together",
            "bind both trusted readonly roots before preparing a creator run",
        )
    skill_root = _trusted_readonly_root(
        skill_creator_root, "skill-creator", candidate_root, protected
    )
    python_root = _trusted_readonly_root(
        creator_python_root, "Python runtime", candidate_root, protected
    )
    scripts = skill_root / "scripts"
    validator = scripts / "quick_validate.py"
    if (
        (skill_root / "SKILL.md").is_symlink()
        or not (skill_root / "SKILL.md").is_file()
        or scripts.is_symlink()
        or not scripts.is_dir()
        or not validator.is_file()
        or validator.is_symlink()
    ):
        raise SelcError(
            "INVALID_CREATOR_DEPENDENCIES",
            "skill-creator root must contain SKILL.md and scripts/quick_validate.py",
            "supply the trusted system skill-creator root",
        )
    return {
        "status": "bound",
        "skill_creator_root": str(skill_root),
        "quick_validator": str(validator.resolve()),
        "creator_python_root": str(python_root),
        "read_roots": [str(skill_root), str(python_root)],
    }


def _trusted_readonly_root(
    path: Path, label: str, candidate_root: Path, protected: list[Path]
) -> Path:
    if path.is_symlink() or not path.is_dir():
        raise SelcError(
            "INVALID_CREATOR_DEPENDENCIES",
            f"{label} readonly root must be a regular directory",
            "supply a concrete trusted dependency directory",
        )
    resolved = path.resolve()
    if resolved == Path(resolved.anchor):
        raise SelcError(
            "INVALID_CREATOR_DEPENDENCIES",
            f"{label} readonly root is too broad",
            "supply the exact dependency directory",
        )
    protected_paths = {str(item.resolve()) for item in protected}
    candidate_parent = candidate_root.resolve().parent
    if _path_encloses(str(resolved), protected_paths | {str(candidate_parent)}):
        raise SelcError(
            "INVALID_CREATOR_DEPENDENCIES",
            f"{label} readonly root encloses protected or candidate-parent content",
            "supply only the trusted dependency subtree",
        )
    return resolved


def _creator_prompt(brief: dict[str, object], run_id: str) -> str:
    instructions = (
        "Improve only the supplied candidate skill bodies and declared resources. "
        "Use the native skill-creator capability and its quick validator to validate the edited skill body. "
        "Do not inspect or modify evaluation rules, runtime, records, results, or any material outside "
        "the editable candidate surface."
    )
    if brief.get("status") != "bound":
        return (
            f"{instructions}\n\nNo maintainer regression brief is bound. This is a diagnostic-only creator "
            f"request and cannot claim a concrete improvement goal. Frozen creator run identity: {run_id}."
        )
    text = brief.get("text")
    if not isinstance(text, str):
        raise SelcError(
            "INVALID_CREATOR_BRIEF",
            "bound creator brief is missing text",
            "prepare a new creator request",
        )
    return (
        f"{instructions}\n\nMaintainer regression brief:\n{text}\n\n"
        f"Frozen creator run identity: {run_id}."
    )


def _read_native_evidence(
    path: Path,
    request: dict[str, Any],
    profile: dict[str, Any],
    protected_hashes: dict[str, str | None],
) -> dict[str, str]:
    """Validate one bounded raw native session and its enforced policy facts."""

    try:
        evidence = read_json(path)
    except SelcError as error:
        raise SelcError(
            "CREATOR_ISOLATION_UNVERIFIED",
            "creator native permission evidence is unreadable",
            "preserve the fixed creator session and export its raw policy evidence",
        ) from error
    raw_path = evidence.get("raw_session_path")
    raw_hash = evidence.get("raw_session_sha256")
    cutoff = evidence.get("raw_session_cutoff_bytes")
    if (
        not isinstance(raw_path, str)
        or not isinstance(raw_hash, str)
        or type(cutoff) is not int
        or not 1 <= cutoff <= 8 * 1024 * 1024
    ):
        raise SelcError(
            "CREATOR_ISOLATION_UNVERIFIED",
            "creator evidence has no bounded raw native session reference",
            "export the raw session path, hash, and cutoff from the fixed invocation",
        )
    session = Path(raw_path)
    if session.is_symlink() or not session.is_file() or session.stat().st_size < cutoff:
        raise SelcError(
            "CREATOR_ISOLATION_UNVERIFIED",
            "creator raw native session is unavailable at its recorded cutoff",
            "preserve the original native session before verifying the candidate",
        )
    resolved_session = session.resolve(strict=True)
    try:
        resolved_session.relative_to(Path.home().resolve() / ".codex" / "sessions")
    except ValueError as error:
        raise SelcError(
            "CREATOR_ISOLATION_UNVERIFIED",
            "creator raw session is outside the supported native Codex store",
            "preserve the raw session emitted by the fixed Codex CLI",
        ) from error
    contents = session.read_bytes()[:cutoff]
    if hashlib.sha256(contents).hexdigest() != raw_hash:
        raise SelcError(
            "CREATOR_ISOLATION_UNVERIFIED",
            "creator raw native session bytes differ from the recorded cutoff",
            "preserve the original native session and retry verification",
        )
    native = _find_native_policy(
        contents, request, candidate_root=Path(str(request["candidate_path"]))
    )
    if native is None:
        raise SelcError(
            "CREATOR_ISOLATION_UNVERIFIED",
            "creator raw native session has no matching session, turn, tool, and permission context",
            "preserve the fixed creator session with its native policy records",
        )
    policy = native["policy"]
    if policy["model"] != profile["model"] or policy["approval_policy"] != "never":
        raise SelcError(
            "CREATOR_ISOLATION_UNVERIFIED",
            "creator model or approval policy differs from the frozen profile",
            "use the prepared fixed creator invocation",
        )
    dependencies = profile.get("readonly_dependencies")
    if not isinstance(dependencies, dict) or dependencies.get("status") != "bound":
        raise SelcError(
            "CREATOR_ISOLATION_UNVERIFIED",
            "creator request has no frozen skill-creator and Python readonly dependencies",
            "prepare a non-diagnostic creator request with explicit trusted roots",
        )
    if policy["network"] is not False or not _policy_allows_only_candidate(
        policy, request, native["cli_version"]
    ):
        raise SelcError(
            "CREATOR_ISOLATION_UNVERIFIED",
            "creator native policy does not prove the required restricted filesystem and network profile",
            "use an OS-enforced creator profile that grants only candidate skills and resources",
        )
    _assert_native_executor(profile, native)
    if (
        evidence.get("protected_before_hashes") != protected_hashes
        or evidence.get("protected_after_hashes") != protected_hashes
    ):
        raise SelcError(
            "CREATOR_ISOLATION_UNVERIFIED",
            "creator evidence does not preserve protected-content hashes",
            "verify the protected content before and after the same native creator session",
        )
    tools = native["tools"]
    if tools != {"exec"}:
        raise SelcError(
            "CREATOR_ISOLATION_UNVERIFIED",
            "creator raw session does not prove the fixed tool surface",
            "use only the fixed native skill-creator tool surface",
        )
    return {"raw_session_sha256": raw_hash}


def _assert_native_executor(profile: dict[str, Any], native: dict[str, object]) -> None:
    """Require a bound executable when a caller claims isolation was verified.

    The native ``session_meta`` version and turn context must match the
    prepared executable and candidate working directory. The local digest is
    re-read from the prepared executable; it is not accepted from an evidence
    JSON field supplied by the caller.
    """

    executor = profile.get("executor")
    if not isinstance(executor, dict):
        raise SelcError(
            "CREATOR_ISOLATION_UNVERIFIED",
            "creator request did not bind a fixed executable",
            "prepare the request with the supported CLI path and version",
        )
    path = Path(str(executor.get("path", "")))
    digest = executor.get("digest")
    version = executor.get("version")
    if (
        not path.is_file()
        or path.is_symlink()
        or not isinstance(digest, str)
        or file_hash(path) != digest
        or not isinstance(version, str)
        or native.get("cli_version") != version
        or native.get("cwd") != str(Path(str(native["candidate_path"])).resolve())
    ):
        raise SelcError(
            "CREATOR_ISOLATION_UNVERIFIED",
            "creator raw session or executable differs from the prepared native profile",
            "use the exact bound CLI executable and version",
        )


def _find_native_policy(
    raw_session: bytes,
    request: dict[str, Any],
    *,
    candidate_root: Path,
) -> dict[str, object] | None:
    """Read one complete Codex session/turn/tool tuple from supported JSONL.

    The parser intentionally recognizes only the observed ``session_meta``,
    ``turn_context``, user ``response_item``, and custom-tool record layouts.
    It does not recursively accept similarly named data supplied in arbitrary
    nested JSON.
    """

    if len(raw_session) > 8 * 1024 * 1024:
        return None
    try:
        records = [json.loads(line) for line in raw_session.splitlines() if line]
    except json.JSONDecodeError:
        return None
    if not records or not all(isinstance(record, dict) for record in records):
        return None
    first = records[0]
    metadata = first.get("payload") if first.get("type") == "session_meta" else None
    if not isinstance(metadata, dict):
        return None
    session_id = metadata.get("session_id")
    cli_version = metadata.get("cli_version")
    source = metadata.get("source")
    if (
        not isinstance(session_id, str)
        or metadata.get("id") != session_id
        or not isinstance(cli_version, str)
        or source != "exec"
    ):
        return None
    matching_turns: set[str] = set()
    contexts: dict[str, dict[str, object]] = {}
    tools: dict[str, set[str]] = {}
    for record in records:
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        if (
            record.get("type") == "response_item"
            and payload.get("type") == "message"
            and payload.get("role") == "user"
        ):
            turn_id = _native_turn_id(payload)
            if turn_id and _native_prompt_matches(payload.get("content"), str(request["prompt"])):
                matching_turns.add(turn_id)
        elif record.get("type") == "turn_context":
            turn_id = payload.get("turn_id")
            if isinstance(turn_id, str) and turn_id:
                contexts[turn_id] = payload
        elif record.get("type") == "response_item" and payload.get("type") == "custom_tool_call":
            turn_id = _native_turn_id(payload)
            name = payload.get("name")
            call_id = payload.get("call_id")
            if (
                isinstance(turn_id, str)
                and isinstance(name, str)
                and name
                and isinstance(call_id, str)
                and call_id
                and "input" in payload
                and isinstance(payload.get("id"), str)
                and isinstance(payload.get("status"), str)
            ):
                tools.setdefault(turn_id, set()).add(name)
    if len(matching_turns) != 1:
        return None
    turn_id = next(iter(matching_turns))
    context = contexts.get(turn_id)
    if context is None:
        return None
    required = {
        "model",
        "permission_profile",
        "sandbox_policy",
        "approval_policy",
        "file_system_sandbox_policy",
        "cwd",
    }
    if not required.issubset(context) or context.get("cwd") != str(candidate_root.resolve()):
        return None
    names = tools.get(turn_id, set())
    if names != {"exec"}:
        return None
    return {
        "session_id": session_id,
        "turn_id": turn_id,
        "cli_version": cli_version,
        "cwd": context["cwd"],
        "candidate_path": str(candidate_root.resolve()),
        "tools": names,
        "policy": {
            "model": context["model"],
            "approval_policy": context["approval_policy"],
            "network": _native_network_value(context),
            "permission_profile": context["permission_profile"],
            "sandbox_policy": context["sandbox_policy"],
            "file_system_sandbox_policy": context["file_system_sandbox_policy"],
        },
    }


def _native_turn_id(payload: dict[str, object]) -> str | None:
    metadata = payload.get("internal_chat_message_metadata_passthrough")
    if not isinstance(metadata, dict):
        return None
    turn_id = metadata.get("turn_id")
    return turn_id if isinstance(turn_id, str) and turn_id else None


def _native_prompt_matches(content: object, expected: str) -> bool:
    return (
        isinstance(content, list)
        and len(content) == 1
        and isinstance(content[0], dict)
        and content[0].get("type") == "input_text"
        and content[0].get("text") == expected
    )


def _native_network_value(policy: dict[str, object]) -> bool | None:
    sandbox = policy.get("sandbox_policy")
    permission = policy.get("permission_profile")
    if isinstance(sandbox, dict) and isinstance(sandbox.get("network_access"), bool):
        return sandbox["network_access"]
    if isinstance(permission, dict):
        network = permission.get("network")
        if network == "restricted":
            return False
    return None


def _policy_allows_only_candidate(
    policy: dict[str, object], request: dict[str, Any], native_version: object
) -> bool:
    """Require the fixed creator filesystem and alpha.11 bootstrap read."""

    entries = _creator_native_filesystem_entries(policy)
    if entries is None or native_version != CODEX_ALPHA11_VERSION:
        return False
    candidate = Path(str(request["candidate_path"])).resolve()
    allowed = {str(candidate / "skills"), str(candidate / "resources")}
    profile = request.get("profile")
    if not isinstance(profile, dict):
        return False
    dependencies = profile.get("readonly_dependencies")
    if not isinstance(dependencies, dict) or dependencies.get("status") != "bound":
        return False
    roots = dependencies.get("read_roots")
    if not isinstance(roots, list) or not all(isinstance(root, str) for root in roots):
        return False
    expected_readable = {str(candidate)}
    expected_readable.update(str(Path(root).resolve()) for root in roots)
    protected = {
        str(path.resolve())
        for path in _protected_paths(_load_real_plan(Path(str(request["plan_path"]))), candidate)
    }
    account_home = _trusted_posix_account_home()
    if account_home is None:
        return False
    arg0_parent = str(account_home / ".codex" / "tmp" / "arg0-macos")
    writable: set[str] = set()
    readable: set[str] = set()
    denied: set[str] = set()
    minimal_reads = 0
    for entry in entries:
        path = entry.get("path")
        access = entry.get("access")
        if path == {"type": "special", "value": {"kind": "minimal"}}:
            if access != "read":
                return False
            minimal_reads += 1
            continue
        if not isinstance(path, dict) or path.get("type") != "path":
            return False
        location = path.get("path")
        if not isinstance(location, str):
            return False
        if access == "write":
            writable.add(location)
        elif access == "read":
            readable.add(location)
        elif access == "deny":
            denied.add(location)
        else:
            return False
    bootstrap_paths = {
        path for path in readable if _is_alpha11_arg0_path(path, str(account_home), arg0_parent)
    }
    if len(bootstrap_paths) != 1:
        return False
    expected_readable.update(bootstrap_paths)
    if minimal_reads != 1 or writable != allowed or readable != expected_readable:
        return False
    if not protected.issubset(denied):
        return False
    return not any(_path_encloses(path, protected) for path in readable)


def _creator_native_filesystem_entries(
    policy: dict[str, object],
) -> list[dict[str, object]] | None:
    """Validate both repeated native filesystem declarations without widening either."""

    profile = policy.get("permission_profile")
    filesystem = policy.get("file_system_sandbox_policy")
    if (
        not isinstance(profile, dict)
        or set(profile) != {"type", "file_system", "network"}
        or profile.get("type") != "managed"
        or profile.get("network") != "restricted"
        or not isinstance(filesystem, dict)
        or set(filesystem) != {"kind", "entries"}
        or filesystem.get("kind") != "restricted"
    ):
        return None
    profile_filesystem = profile.get("file_system")
    if (
        not isinstance(profile_filesystem, dict)
        or set(profile_filesystem) != {"type", "entries"}
        or profile_filesystem.get("type") != "restricted"
    ):
        return None
    profile_entries = _normalise_native_policy_entries(profile_filesystem.get("entries"))
    sandbox_entries = _normalise_native_policy_entries(filesystem.get("entries"))
    if profile_entries is None or sandbox_entries is None or profile_entries != sandbox_entries:
        return None
    return profile_entries


def _path_encloses(path: str, protected: set[str]) -> bool:
    candidate = Path(path)
    for protected_path in protected:
        try:
            Path(protected_path).relative_to(candidate)
        except ValueError:
            continue
        return True
    return False


def _protected_hashes(paths: list[Path]) -> dict[str, str | None]:
    hashes: dict[str, str | None] = {}
    for path in paths:
        key = str(path.resolve())
        if path.is_symlink():
            raise SelcError("INVALID_CANDIDATE", "protected creator path cannot be a symlink")
        if not path.exists():
            hashes[key] = None
        elif path.is_file():
            hashes[key] = file_hash(path)
        elif path.is_dir():
            hashes[key] = tree_hash(path)
        else:
            hashes[key] = None
    return hashes


def _protected_paths(plan: dict[str, Any], candidate_root: Path) -> list[Path]:
    baseline = Path(str(plan["baseline_snapshot"]["path"]))
    plan_root = baseline.parent
    protected = [
        plan_root,
        baseline,
        plan_root / "case-inputs",
        plan_root / "attempts",
        plan_root / "oracle",
        plan_root / "holdout",
        plan_root / "expected",
        candidate_root.parent / "baseline-resources",
        candidate_root.parent / "protected-oracle.json",
        candidate_root.parent / "expected-results",
        candidate_root.parent / "evaluation-results",
        candidate_root.parent / "runtime",
        candidate_root.parent / "assembly",
        candidate_root.parent / "records",
        candidate_root.parent / "results",
        candidate_root.parent / ".selc",
    ]
    return protected


def _assert_candidate_surface(candidate_root: Path) -> None:
    if not candidate_root.is_dir() or candidate_root.is_symlink():
        raise SelcError(
            "INVALID_CANDIDATE", "candidate root is unavailable", "prepare a new candidate"
        )
    entries = {path.name for path in candidate_root.iterdir()}
    if entries != {"skills", "resources"}:
        raise SelcError(
            "INVALID_CANDIDATE",
            "creator candidate may contain only skills and resources",
            "use the isolated candidate directory",
        )
    for entry in candidate_root.rglob("*"):
        if entry.is_symlink():
            raise SelcError(
                "INVALID_CANDIDATE", "creator candidate cannot contain symlinks", "copy the file"
            )


def _request_hash(value: dict[str, Any]) -> str:
    return json_hash(value)
