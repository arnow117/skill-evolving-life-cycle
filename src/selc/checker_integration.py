"""Installed-suite artifact checker coordination.

This module joins a validated suite declaration to the independent checker
engine.  It deliberately has no business-task side effect: ``check`` callers
receive a :class:`CheckerExecution` and may later attach it to a real delivery
through ``records.record_checked_delivery``.  A future managed host adapter can
therefore execute the checker before it opens its short SQLite projection
transaction.

The public internal DTO is ``CheckerExecution``.  It contains the declared
``check_id``, the engine's bound ``result``, and two distinct installation
identities: ``installation_digest`` is the active installation's typed-tree
digest, while ``result.composite_digest`` is the checker engine's SHA-256 over
the fixed composite-byte binding.  ``installation_version`` is retained so a
delivery cannot be mistaken for a different selected installation.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from contextlib import ExitStack
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .errors import SelcError
from .install import current_installation, install_root, registry_lock_path
from .manifest import load_manifest, normalize_artifact_checks, portable_tree_hash
from .util import exclusive_lock, write_json


CHECKER_REGISTRY_SCHEMA_VERSION = 1
CHECKER_REGISTRY_NAME = "checker-registry.json"
MAX_PROJECT_CHECKERS = 64
MAX_REGISTERED_RULES = 64
MAX_SCRIPT_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class CheckerExecution:
    """One actual engine result plus the selected installation identity."""

    check_id: str
    result: Any
    installation_digest: str
    installation_version: str


def checker_registry_path(project_root: Path) -> Path:
    """Return the project-local, maintainer-controlled checker registry path."""

    return project_root / ".selc" / CHECKER_REGISTRY_NAME


def register_script_checker(
    *,
    project_root: Path,
    name: str,
    version: str,
    script_path: Path,
    digest: str,
    supported_rules: list[str],
    timeout_seconds: float = 1.0,
) -> dict[str, object]:
    """Explicitly pin one local Python script in this project's registry.

    The manifest cannot create or amend this registration.  The current script
    bytes must match ``digest`` before the registry changes; each later run
    rechecks the same digest before it launches a private script snapshot.
    """

    entry = _registered_script_entry(
        name=name,
        version=version,
        script_path=script_path,
        digest=digest,
        supported_rules=supported_rules,
        timeout_seconds=timeout_seconds,
    )
    project_root.mkdir(parents=True, exist_ok=True)
    with exclusive_lock(registry_lock_path(project_root)):
        registry = _read_project_registry(project_root)
        entries = registry["checkers"]
        if any(
            item["name"] == entry["name"] and item["version"] == entry["version"]
            for item in entries
        ):
            raise SelcError(
                "CHECKER_ALREADY_REGISTERED",
                "a checker with this name and version is already registered",
                "choose a new version or remove the old project registration",
            )
        entries.append(entry)
        write_json(checker_registry_path(project_root), registry)
    return _public_registry_entry(entry)


def inspect_script_checkers(project_root: Path) -> list[dict[str, object]]:
    """Return only project-local trusted registration metadata, never script output."""

    with exclusive_lock(registry_lock_path(project_root)):
        registry = _read_project_registry(project_root)
    return [_public_registry_entry(entry) for entry in registry["checkers"]]


def run_installed_check(
    *,
    project_root: Path,
    suite_id: str,
    check_id: str,
    input_path: Path,
    artifact_path: Path,
    subject: Any,
    run_id: str,
) -> CheckerExecution:
    """Run one declared checker against frozen input and artifact source bytes.

    Registry then suite locking follows the repository-wide order.  The locks
    keep the selected installed runtime stable while the checker runs, but no
    SQLite connection or transaction is opened.  The paths are read before
    execution and compared again afterward; a late mutation changes the result
    to ``unknown`` without deleting or rewriting either source file.
    """

    with ExitStack() as locks:
        locks.enter_context(exclusive_lock(registry_lock_path(project_root)))
        installation = current_installation(project_root, suite_id)
        installation_digest = _required_installation_string(installation, "digest")
        installation_version = _required_installation_string(installation, "version")
        suite_lock = install_root(project_root) / suite_id / ".lock"
        locks.enter_context(exclusive_lock(suite_lock))

        suite_root = install_root(project_root) / suite_id / "current"
        actual_tree_digest = portable_tree_hash(suite_root)
        if actual_tree_digest != installation_digest:
            raise SelcError(
                "INSTALLATION_IDENTITY_DRIFT",
                "selected installation bytes do not match its recorded digest",
                "restore the installation or run updates recover before checking",
            )
        manifest = load_manifest(suite_root)
        declarations = normalize_artifact_checks(manifest.get("artifact_checks"))
        declaration = declarations.get(check_id)
        if declaration is None:
            raise SelcError(
                "MISSING_ARTIFACT_CHECK",
                f"suite {suite_id} has no declared artifact check: {check_id}",
                "declare the check in suite.json and reinstall the suite",
            )
        checker_module = _load_installed_checker_module(suite_root, installation_digest)
        input_bytes = _read_source_bytes(input_path, "input")
        artifact_bytes = _read_source_bytes(artifact_path, "artifact")
        resource_path = suite_root / "suite.json"
        resource_bytes = _read_source_bytes(resource_path, "suite manifest")
        registry, registry_error = _trusted_registry(checker_module, project_root)
        request = _build_request(
            checker_module=checker_module,
            declaration=declaration,
            registry=registry,
            run_id=run_id,
            subject=subject,
            input_bytes=input_bytes,
            artifact_bytes=artifact_bytes,
            resource_bytes=resource_bytes,
            installation_digest=installation_digest,
        )
        if registry_error is not None:
            result = _unknown_result(checker_module, request, registry_error)
        else:
            result = checker_module.check_artifact(request, registry)

        changed_reason = _source_change_reason(
            suite_root=suite_root,
            expected_tree_digest=installation_digest,
            input_path=input_path,
            input_bytes=input_bytes,
            artifact_path=artifact_path,
            artifact_bytes=artifact_bytes,
            resource_path=resource_path,
            resource_bytes=resource_bytes,
        )
        if changed_reason is not None:
            result = _unknown_result(checker_module, request, changed_reason)

    return CheckerExecution(
        check_id=check_id,
        result=result,
        installation_digest=installation_digest,
        installation_version=installation_version,
    )


def check_result_payload(execution: CheckerExecution) -> dict[str, object]:
    """Convert an engine result to the stable JSON object printed by ``check``."""

    result = execution.result
    return {
        "check_id": execution.check_id,
        "installation_digest": execution.installation_digest,
        "installation_version": execution.installation_version,
        "protocol": result.protocol,
        "run_id": result.run_id,
        "subject": result.subject.as_dict(),
        "checker": result.checker.as_dict(),
        "rule_version": result.rule_version,
        "resource_identity": result.resource_identity,
        "composite_identity": result.composite_identity,
        "input_digest": result.input_digest,
        "artifact_digest": result.artifact_digest,
        "resource_digest": result.resource_digest,
        "composite_digest": result.composite_digest,
        "rule_results": [
            {"rule_id": item.rule_id, "status": item.status, "reason": item.reason}
            for item in result.rule_results
        ],
        "status": result.status,
        "reason": result.reason,
    }


def _required_installation_string(installation: dict[str, object], key: str) -> str:
    value = installation.get(key)
    if not isinstance(value, str) or not value:
        raise SelcError(
            "INSTALLATION_IDENTITY_INVALID",
            f"selected installation has no valid {key}",
            "reinstall the suite before checking artifacts",
        )
    return value


def _load_installed_checker_module(suite_root: Path, installation_digest: str) -> Any:
    module_path = suite_root / "runtime" / "selc" / "checkers.py"
    if not module_path.is_file() or module_path.is_symlink():
        raise SelcError(
            "CHECKER_RUNTIME_UNAVAILABLE",
            "selected suite has no safe checker runtime module",
            "reinstall a framework that includes checkers.py",
        )
    module_name = f"_selc_installed_checkers_{installation_digest[:16]}"
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    specification = importlib.util.spec_from_file_location(module_name, module_path)
    if specification is None or specification.loader is None:
        raise SelcError(
            "CHECKER_RUNTIME_UNAVAILABLE",
            "selected checker runtime cannot be loaded",
            "reinstall the selected suite",
        )
    module = importlib.util.module_from_spec(specification)
    sys.modules[module_name] = module
    try:
        specification.loader.exec_module(module)
    except Exception as error:
        sys.modules.pop(module_name, None)
        raise SelcError(
            "CHECKER_RUNTIME_UNAVAILABLE",
            f"selected checker runtime failed to load: {type(error).__name__}",
            "reinstall a valid framework runtime",
        ) from error
    return module


def _build_request(
    *,
    checker_module: Any,
    declaration: dict[str, object],
    registry: Any,
    run_id: str,
    subject: Any,
    input_bytes: bytes,
    artifact_bytes: bytes,
    resource_bytes: bytes,
    installation_digest: str,
) -> Any:
    checker_declaration = declaration["checker"]
    assert isinstance(checker_declaration, dict)
    checker_name = str(checker_declaration["name"])
    checker_version = str(checker_declaration["version"])
    declared_digest = checker_declaration.get("digest")
    if isinstance(declared_digest, str):
        identity = checker_module.CheckerIdentity(checker_name, checker_version, declared_digest)
    else:
        try:
            identity = registry.identity(checker_name, checker_version)
        except KeyError:
            identity = checker_module.CheckerIdentity(checker_name, checker_version, "0" * 64)
    subject_identity = checker_module.SubjectIdentity(
        task_id=str(getattr(subject, "task_id", "")),
        call_id=str(getattr(subject, "call_id", "")),
        delivery_id=str(getattr(subject, "delivery_id", "")),
    )
    return checker_module.CheckRequest(
        protocol=checker_module.PROTOCOL_VERSION,
        run_id=run_id,
        subject=subject_identity,
        checker=identity,
        rule_version=str(declaration["rule_version"]),
        required_rule_ids=tuple(declaration["required_rule_ids"]),
        configuration=dict(declaration["configuration"]),
        input_bytes=input_bytes,
        artifact_bytes=artifact_bytes,
        resource_identity="suite.json",
        resource_bytes=resource_bytes,
        composite_identity=f"installed-typed-tree:{installation_digest}",
        composite_bytes=installation_digest.encode("ascii"),
        require_network_isolation=bool(declaration["require_network_isolation"]),
    )


def _trusted_registry(checker_module: Any, project_root: Path) -> tuple[Any, str | None]:
    registry = checker_module.builtin_registry()
    try:
        project_registry = _read_project_registry(project_root)
        for entry in project_registry["checkers"]:
            provider = checker_module.ScriptCheckerProvider(
                Path(entry["script"]),
                implementation_digest=entry["digest"],
                timeout_seconds=entry["timeout_seconds"],
                supported_rules=frozenset(entry["supported_rules"]),
            )
            identity = checker_module.CheckerIdentity(
                entry["name"], entry["version"], entry["digest"]
            )
            registry.register(identity, provider)
    except (OSError, ValueError, KeyError, TypeError, SelcError):
        return registry, "checker-registry-invalid"
    return registry, None


def _unknown_result(checker_module: Any, request: Any, reason: str) -> Any:
    rule_results = tuple(
        checker_module.RuleResult(rule_id, checker_module.STATUS_UNKNOWN, reason)
        for rule_id in request.required_rule_ids
    )
    return checker_module.CheckResult(
        protocol=request.protocol,
        run_id=request.run_id,
        subject=request.subject,
        checker=request.checker,
        rule_version=request.rule_version,
        resource_identity=request.resource_identity,
        composite_identity=request.composite_identity,
        input_digest=hashlib.sha256(request.input_bytes).hexdigest(),
        artifact_digest=hashlib.sha256(request.artifact_bytes).hexdigest(),
        resource_digest=hashlib.sha256(request.resource_bytes).hexdigest(),
        composite_digest=hashlib.sha256(request.composite_bytes).hexdigest(),
        rule_results=rule_results,
        status=checker_module.STATUS_UNKNOWN,
        reason=reason,
    )


def _read_source_bytes(path: Path, label: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as error:
        raise SelcError(
            "CHECK_SOURCE_UNAVAILABLE",
            f"cannot read {label} for artifact check",
            "restore the source file and retry",
        ) from error


def _source_change_reason(
    *,
    suite_root: Path,
    expected_tree_digest: str,
    input_path: Path,
    input_bytes: bytes,
    artifact_path: Path,
    artifact_bytes: bytes,
    resource_path: Path,
    resource_bytes: bytes,
) -> str | None:
    if not _path_keeps_bytes(input_path, input_bytes):
        return "input-source-changed"
    if not _path_keeps_bytes(artifact_path, artifact_bytes):
        return "artifact-source-changed"
    if not _path_keeps_bytes(resource_path, resource_bytes):
        return "resource-source-changed"
    try:
        if portable_tree_hash(suite_root) != expected_tree_digest:
            return "installation-source-changed"
    except (OSError, SelcError):
        return "installation-source-changed"
    return None


def _path_keeps_bytes(path: Path, expected: bytes) -> bool:
    try:
        return path.read_bytes() == expected
    except OSError:
        return False


def _registered_script_entry(
    *,
    name: str,
    version: str,
    script_path: Path,
    digest: str,
    supported_rules: list[str],
    timeout_seconds: float,
    verify_current_bytes: bool = True,
) -> dict[str, object]:
    if not _bounded_string(name) or not _bounded_string(version):
        raise SelcError(
            "INVALID_CHECKER_REGISTRATION",
            "checker name and version must be non-empty bounded strings",
            "supply a maintainer-owned checker identity",
        )
    if not _digest(digest):
        raise SelcError(
            "INVALID_CHECKER_REGISTRATION",
            "checker digest must be SHA-256 hex",
            "calculate the digest of the script bytes",
        )
    if (
        not supported_rules
        or len(supported_rules) > MAX_REGISTERED_RULES
        or any(not _bounded_string(rule_id) for rule_id in supported_rules)
        or len(set(supported_rules)) != len(supported_rules)
    ):
        raise SelcError(
            "INVALID_CHECKER_REGISTRATION",
            "checker rules must be unique bounded identifiers",
            "register one or more explicit supported rules",
        )
    if not isinstance(timeout_seconds, float) or not 0 < timeout_seconds <= MAX_SCRIPT_TIMEOUT_SECONDS:
        raise SelcError(
            "INVALID_CHECKER_REGISTRATION",
            "checker timeout must be greater than zero and at most ten seconds",
            "choose a bounded timeout",
        )
    script = script_path.expanduser().resolve()
    if verify_current_bytes and (script.is_symlink() or not script.is_file()):
        raise SelcError(
            "INVALID_CHECKER_REGISTRATION",
            "checker script must be a regular non-symlink file",
            "register a maintainer-owned Python file",
        )
    if verify_current_bytes:
        try:
            actual_digest = hashlib.sha256(script.read_bytes()).hexdigest()
        except OSError as error:
            raise SelcError(
                "INVALID_CHECKER_REGISTRATION",
                "checker script cannot be read",
                "restore the maintainer-owned script before registration",
            ) from error
        if actual_digest != digest:
            raise SelcError(
                "CHECKER_DIGEST_MISMATCH",
                "checker script bytes do not match the requested pinned digest",
                "supply the digest of the exact script bytes",
            )
    return {
        "name": name,
        "version": version,
        "digest": digest,
        "script": str(script),
        "supported_rules": list(supported_rules),
        "timeout_seconds": timeout_seconds,
    }


def _read_project_registry(project_root: Path) -> dict[str, list[dict[str, object]] | int]:
    path = checker_registry_path(project_root)
    if not path.exists():
        return {"schema_version": CHECKER_REGISTRY_SCHEMA_VERSION, "checkers": []}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SelcError(
            "INVALID_CHECKER_REGISTRY",
            "project checker registry is not valid JSON",
            "repair or remove .selc/checker-registry.json",
        ) from error
    if not isinstance(value, dict) or value.get("schema_version") != CHECKER_REGISTRY_SCHEMA_VERSION:
        raise SelcError(
            "INVALID_CHECKER_REGISTRY",
            "project checker registry schema is unsupported",
            "re-register local checkers with this runtime",
        )
    entries = value.get("checkers")
    if not isinstance(entries, list) or len(entries) > MAX_PROJECT_CHECKERS:
        raise SelcError(
            "INVALID_CHECKER_REGISTRY",
            "project checker registry has an invalid checker list",
            "keep at most 64 explicit registrations",
        )
    normalized_entries = []
    names_and_versions: set[tuple[str, str]] = set()
    for raw_entry in entries:
        if not isinstance(raw_entry, dict):
            raise SelcError(
                "INVALID_CHECKER_REGISTRY",
                "project checker registry contains a malformed entry",
                "repair the project checker registry",
            )
        try:
            entry = _registered_script_entry(
                name=raw_entry["name"],
                version=raw_entry["version"],
                script_path=Path(raw_entry["script"]),
                digest=raw_entry["digest"],
                supported_rules=raw_entry["supported_rules"],
                timeout_seconds=raw_entry["timeout_seconds"],
                verify_current_bytes=False,
            )
        except (KeyError, TypeError, SelcError) as error:
            raise SelcError(
                "INVALID_CHECKER_REGISTRY",
                "project checker registry contains an invalid registration",
                "repair the registration before checking artifacts",
            ) from error
        identity = (str(entry["name"]), str(entry["version"]))
        if identity in names_and_versions:
            raise SelcError(
                "INVALID_CHECKER_REGISTRY",
                "project checker registry repeats a checker name and version",
                "keep one digest-pinned registration per checker version",
            )
        names_and_versions.add(identity)
        normalized_entries.append(entry)
    return {"schema_version": CHECKER_REGISTRY_SCHEMA_VERSION, "checkers": normalized_entries}


def _public_registry_entry(entry: dict[str, object]) -> dict[str, object]:
    return {
        "name": entry["name"],
        "version": entry["version"],
        "digest": entry["digest"],
        "supported_rules": list(entry["supported_rules"]),
        "timeout_seconds": entry["timeout_seconds"],
    }


def _bounded_string(value: object) -> bool:
    return isinstance(value, str) and bool(value) and len(value.encode("utf-8")) <= 256


def _digest(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True
