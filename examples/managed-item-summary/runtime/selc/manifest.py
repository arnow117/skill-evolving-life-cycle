"""Suite manifest validation and portable suite construction."""

from __future__ import annotations

import json
import re
import shutil
import zipfile
from contextlib import suppress
from pathlib import Path
from typing import Any

from . import RUNTIME_VERSION, SCHEMA_VERSION
from .errors import SelcError
from .util import (
    canonical_json,
    file_hash,
    require_relative_path,
    run_argv,
    run_argv_result,
    tree_hash,
    typed_tree_hash,
    write_json,
)

NAME = re.compile(r"^[a-z][a-z0-9-]{1,62}$")
FRONTMATTER_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
RUNTIME_PROTOCOL = 1
SOURCE_BASELINE_NAME = "source-baseline.json"
RUNTIME_LICENSE_NAME = "SELC-LICENSE"
ASCII_SPACE = " "
MAX_ARTIFACT_CHECKS = 64
MAX_ARTIFACT_CHECK_CONFIGURATION_BYTES = 16_384
MAX_ARTIFACT_CHECK_RULES = 64
MAX_ARTIFACT_CHECK_IDENTIFIER_BYTES = 256
MANAGED_PROTOCOL = 1
MANAGED_ACTION_TYPES = {"boolean", "integer", "path", "string"}
MANAGED_ACTION_KINDS = {"text-report-v1", "python-script-v1", "instruction-staging-v1"}
MANAGED_HOSTS = {"codex", "workbuddy"}


def manifest_path(suite_root: Path) -> Path:
    return suite_root / "suite.json"


def suite_content_hash(suite_root: Path) -> str:
    """Hash all suite content while avoiding a self-referential manifest hash."""

    entries: list[tuple[str, str, bytes]] = []
    for path in _suite_files(suite_root):
        relative = path.relative_to(suite_root).as_posix()
        if path == manifest_path(suite_root):
            contents = json.loads(path.read_text(encoding="utf-8"))
            contents.pop("content_hash", None)
            payload = canonical_json(contents).encode("utf-8")
        else:
            payload = path.read_bytes()
        entries.append((relative, "file", payload))
    return typed_tree_hash(entries)


def _suite_files(suite_root: Path) -> list[Path]:
    """Return only portable code/resources and reject links that escape a suite."""

    allowed = (
        "suite.json",
        "framework.json",
        "skills",
        "resources",
        "runtime",
        "bin",
        "install.py",
        "MAINTENANCE.md",
    )
    files: list[Path] = []
    for relative_name in allowed:
        entry = suite_root / relative_name
        if not entry.exists() and not entry.is_symlink():
            if relative_name in {"framework.json", "resources"}:
                continue
            raise SelcError(
                "INVALID_MANIFEST",
                f"missing portable suite entry: {relative_name}",
                "recreate the suite before packaging",
            )
        if entry.is_symlink():
            raise SelcError(
                "UNSAFE_SYMLINK",
                f"portable suite entry cannot be a symlink: {relative_name}",
                "copy the declared code or resource into the suite",
            )
        if entry.is_file():
            files.append(entry)
            continue
        for path in sorted(entry.rglob("*")):
            if path.is_symlink():
                relative = path.relative_to(suite_root).as_posix()
                raise SelcError(
                    "UNSAFE_SYMLINK",
                    f"portable suite cannot package a symlink: {relative}",
                    "copy the declared code or resource into the suite",
                )
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                files.append(path)
    return sorted(files)


def framework_path(root: Path) -> Path:
    return root / "framework.json"


def load_framework(root: Path) -> dict[str, Any]:
    """Validate a standalone framework package or a framework embedded in a suite."""

    descriptor_path = framework_path(root)
    if descriptor_path.is_symlink():
        raise SelcError(
            "UNSAFE_SYMLINK",
            "framework descriptor cannot be a symlink",
            "copy framework.json into the framework package",
        )
    descriptor = _read_framework_descriptor(root)
    required = {"schema_version", "framework_version", "runtime_protocol", "runtime_hash"}
    missing = required.difference(descriptor)
    if missing:
        raise SelcError(
            "INVALID_FRAMEWORK",
            f"framework descriptor is missing: {', '.join(sorted(missing))}",
            "rebuild the framework package with SELC",
        )
    if descriptor["schema_version"] != SCHEMA_VERSION:
        raise SelcError(
            "UNSUPPORTED_SCHEMA",
            "framework descriptor schema is unsupported",
            "use a compatible framework package",
        )
    if descriptor["runtime_protocol"] != RUNTIME_PROTOCOL:
        raise SelcError(
            "UNSUPPORTED_RUNTIME_PROTOCOL",
            "framework runtime protocol is incompatible",
            "choose a framework package with runtime protocol 1",
        )
    if not isinstance(descriptor["framework_version"], str) or not descriptor["framework_version"]:
        raise SelcError(
            "INVALID_FRAMEWORK",
            "framework_version must be a non-empty string",
            "rebuild the framework package with an explicit version",
        )
    runtime_root = root / "runtime" / "selc"
    if not runtime_root.is_dir():
        raise SelcError(
            "INVALID_FRAMEWORK",
            "framework package has no runtime/selc directory",
            "provide a complete framework package",
        )
    for path in (root / "runtime").rglob("*"):
        if path.is_symlink():
            relative = path.relative_to(root).as_posix()
            raise SelcError(
                "UNSAFE_SYMLINK",
                f"framework runtime cannot contain a symlink: {relative}",
                "copy the runtime file into the framework package",
            )
    runtime_hash = tree_hash(root / "runtime")
    if descriptor["runtime_hash"] != runtime_hash:
        raise SelcError(
            "FRAMEWORK_HASH_MISMATCH",
            "framework runtime does not match framework.json",
            "restore or repackage the framework",
        )
    return descriptor


def _read_framework_descriptor(root: Path) -> dict[str, Any]:
    path = framework_path(root)
    if path.is_file():
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise SelcError(
                "INVALID_JSON",
                f"invalid JSON in {path}: {error.msg}",
                "repair or recreate framework.json",
            ) from error
        if not isinstance(value, dict):
            raise SelcError(
                "INVALID_FRAMEWORK",
                "framework.json must contain an object",
                "repair or recreate framework.json",
            )
        return value
    manifest = load_manifest(root)
    return {
        "schema_version": SCHEMA_VERSION,
        "framework_version": manifest["framework_version"],
        "runtime_protocol": manifest.get("runtime_protocol", RUNTIME_PROTOCOL),
        "runtime_hash": tree_hash(root / "runtime"),
    }


def materialize_composite_suite(
    business_root: Path, framework_root: Path, destination: Path
) -> dict[str, Any]:
    """Build a validated business suite with a compatible external framework runtime."""

    business_manifest = load_manifest(business_root)
    framework = load_framework(framework_root)
    business_protocol = business_manifest.get("runtime_protocol", RUNTIME_PROTOCOL)
    if business_protocol != framework["runtime_protocol"]:
        raise SelcError(
            "UNSUPPORTED_RUNTIME_PROTOCOL",
            "business suite and framework runtime protocols differ",
            "select matching protocol-1 business and framework sources",
        )
    copy_portable_suite(business_root, destination)
    runtime_destination = destination / "runtime"
    shutil.rmtree(runtime_destination)
    shutil.copytree(framework_root / "runtime", runtime_destination)
    descriptor_path = framework_path(framework_root)
    if descriptor_path.is_file():
        shutil.copy2(descriptor_path, framework_path(destination))
    else:
        write_json(framework_path(destination), framework)
    manifest = read_manifest(destination)
    manifest["framework_version"] = framework["framework_version"]
    manifest["runtime_protocol"] = framework["runtime_protocol"]
    write_json(manifest_path(destination), manifest)
    manifest["content_hash"] = suite_content_hash(destination)
    write_json(manifest_path(destination), manifest)
    return manifest


def read_manifest(suite_root: Path) -> dict[str, Any]:
    from .util import read_json

    return read_json(manifest_path(suite_root))


def portable_tree_hash(suite_root: Path) -> str:
    """Hash the same explicit portable surface copied by package and installation."""

    return typed_tree_hash(
        (path.relative_to(suite_root).as_posix(), "file", path.read_bytes())
        for path in _suite_files(suite_root)
    )


def copy_portable_suite(source: Path, destination: Path) -> None:
    """Copy only declared portable code/resources into a fresh installation directory."""

    if destination.exists():
        raise SelcError(
            "PATH_CONFLICT",
            f"portable destination already exists: {destination}",
            "choose an empty destination",
        )
    destination.mkdir(parents=True)
    for path in _suite_files(source):
        relative = path.relative_to(source)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def validate_manifest(manifest: dict[str, Any], suite_root: Path) -> None:
    required = {"schema_version", "suite_id", "version", "framework_version", "skills"}
    missing = required.difference(manifest)
    if missing:
        raise SelcError(
            "INVALID_MANIFEST", f"missing fields: {', '.join(sorted(missing))}", "repair suite.json"
        )
    if manifest["schema_version"] != SCHEMA_VERSION:
        raise SelcError(
            "UNSUPPORTED_SCHEMA",
            "suite manifest schema is unsupported",
            "use a compatible SELC runtime",
        )
    runtime_protocol = manifest.get("runtime_protocol", RUNTIME_PROTOCOL)
    if runtime_protocol != RUNTIME_PROTOCOL:
        raise SelcError(
            "UNSUPPORTED_RUNTIME_PROTOCOL",
            "suite runtime protocol is incompatible",
            "use a suite with runtime protocol 1",
        )
    if not isinstance(manifest["suite_id"], str) or not NAME.fullmatch(manifest["suite_id"]):
        raise SelcError(
            "INVALID_MANIFEST",
            "suite_id must be a lowercase kebab-case identifier",
            "choose a valid suite_id",
        )
    if not isinstance(manifest["version"], str) or not manifest["version"]:
        raise SelcError(
            "INVALID_MANIFEST", "version must be a non-empty string", "set an explicit version"
        )
    skills = manifest["skills"]
    if not isinstance(skills, list) or not skills:
        raise SelcError("INVALID_MANIFEST", "skills must contain at least one skill", "add a skill")
    validate_declared_skills(suite_root, skills)
    artifact_checks = normalize_artifact_checks(manifest.get("artifact_checks"))
    validate_skill_artifact_check_references(skills, artifact_checks)
    managed_configuration(manifest, suite_root)
    managed_host_versions(manifest)


def managed_configuration(manifest: dict[str, Any], suite_root: Path) -> dict[str, dict[str, Any]]:
    """Validate optional protocol-1 managed actions without altering manifest bytes."""

    managed = manifest.get("managed")
    if managed is None:
        return {}
    if not isinstance(managed, dict) or managed.get("protocol") != MANAGED_PROTOCOL:
        raise SelcError(
            "INVALID_MANAGED_PROTOCOL",
            "managed configuration must declare protocol 1",
            "use a supported managed protocol or remove the optional configuration",
        )
    actions = managed.get("actions")
    if not isinstance(actions, dict) or not actions:
        raise SelcError(
            "INVALID_MANAGED_ACTION",
            "managed actions must be a non-empty object",
            "declare one fixed managed action",
        )
    declared_skills = {
        str(skill["name"]) for skill in manifest["skills"] if isinstance(skill, dict)
    }
    normalized: dict[str, dict[str, Any]] = {}
    for action_id, raw_action in actions.items():
        if not isinstance(action_id, str) or not NAME.fullmatch(action_id):
            raise SelcError(
                "INVALID_MANAGED_ACTION",
                "managed action ids must be lowercase kebab-case",
                "choose a bounded action id",
            )
        if not isinstance(raw_action, dict):
            raise SelcError(
                "INVALID_MANAGED_ACTION",
                f"managed action {action_id} must be an object",
                "declare a fixed script, skill, kind, and input schema",
            )
        required = {"skill", "kind", "input"}
        if raw_action.get("kind") != "instruction-staging-v1":
            required.add("script")
        if required.difference(raw_action):
            raise SelcError(
                "INVALID_MANAGED_ACTION",
                f"managed action {action_id} is incomplete",
                "declare skill, kind, script, and input",
            )
        skill = raw_action["skill"]
        kind = raw_action["kind"]
        script = raw_action.get("script")
        if not isinstance(skill, str) or skill not in declared_skills:
            raise SelcError(
                "INVALID_MANAGED_ACTION",
                f"managed action {action_id} names an undeclared skill",
                "bind the action to a declared skill",
            )
        if kind not in MANAGED_ACTION_KINDS:
            raise SelcError(
                "INVALID_MANAGED_ACTION",
                f"managed action {action_id} has an unsupported fixed kind",
                "use a runtime-supported fixed action kind",
            )
        if kind == "instruction-staging-v1":
            if script is not None:
                raise SelcError(
                    "INVALID_MANAGED_ACTION",
                    f"managed action {action_id} instruction staging cannot supply a script",
                    "let the declared skill stage its private artifact bytes",
                )
            script_path = None
        elif not isinstance(script, str):
            raise SelcError(
                "INVALID_MANAGED_ACTION",
                f"managed action {action_id} script must be a relative file",
                "select an installed fixed Python script",
            )
        else:
            script_path = require_relative_path(script)
        if script_path is not None and not (suite_root / script_path).is_file():
            raise SelcError(
                "INVALID_MANAGED_ACTION",
                f"managed action {action_id} script is not installed",
                "package the declared fixed script with the suite",
            )
        input_schema = _normalize_managed_input_schema(
            raw_action["input"],
            action_id,
            allow_empty=kind == "instruction-staging-v1",
        )
        normalized[action_id] = {
            "skill": skill,
            "kind": kind,
            "script": script_path.as_posix() if script_path is not None else None,
            "input": input_schema,
        }
    return normalized


def managed_host_versions(manifest: dict[str, Any]) -> dict[str, frozenset[str]]:
    """Return the immutable native host versions allowed by a managed suite.

    A direct environment binding proves that a callback carried identity data;
    it does not make every host build safe to activate.  Production callers use
    this explicit, manifest-owned allowlist before supplying an original skill
    body.  Fixture callbacks remain local-test-only in the runtime and do not
    add a host version here.
    """

    managed = manifest.get("managed")
    if managed is None:
        return {}
    if not isinstance(managed, dict) or managed.get("protocol") != MANAGED_PROTOCOL:
        raise SelcError(
            "INVALID_MANAGED_PROTOCOL",
            "managed configuration must declare protocol 1",
            "use a supported managed protocol or remove the optional configuration",
        )
    raw_hosts = managed.get("native_hosts", {})
    if not isinstance(raw_hosts, dict) or set(raw_hosts).difference(MANAGED_HOSTS):
        raise SelcError(
            "INVALID_MANAGED_HOST",
            "managed native_hosts must name only codex or workbuddy",
            "declare explicit native host version allowlists",
        )
    normalized: dict[str, frozenset[str]] = {}
    for host, versions in raw_hosts.items():
        if not isinstance(versions, list) or not versions or len(versions) > 32:
            raise SelcError(
                "INVALID_MANAGED_HOST",
                f"managed native host {host} needs one or more versions",
                "declare a bounded version allowlist",
            )
        if any(
            not isinstance(version, str) or not version or len(version.encode("utf-8")) > 128
            for version in versions
        ) or len(set(versions)) != len(versions):
            raise SelcError(
                "INVALID_MANAGED_HOST",
                f"managed native host {host} has invalid versions",
                "use unique bounded version strings",
            )
        normalized[host] = frozenset(versions)
    return normalized


def _normalize_managed_input_schema(
    value: object,
    action_id: str,
    *,
    allow_empty: bool = False,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value).difference({"fields", "required"}):
        raise SelcError(
            "INVALID_MANAGED_ACTION",
            f"managed action {action_id} input must contain only fields and required",
            "use the limited protocol-1 object schema",
        )
    fields = value.get("fields")
    required = value.get("required")
    if not isinstance(fields, dict) or (not fields and not allow_empty) or len(fields) > 16:
        raise SelcError(
            "INVALID_MANAGED_ACTION",
            f"managed action {action_id} input fields are invalid",
            "declare at most 16 bounded fields",
        )
    if not isinstance(required, list) or any(not isinstance(item, str) for item in required):
        raise SelcError(
            "INVALID_MANAGED_ACTION",
            f"managed action {action_id} required fields are invalid",
            "list declared required fields",
        )
    if len(required) != len(set(required)) or not set(required).issubset(fields):
        raise SelcError(
            "INVALID_MANAGED_ACTION",
            f"managed action {action_id} required fields must be declared once",
            "repair the input schema",
        )
    normalized_fields: dict[str, dict[str, Any]] = {}
    for name, descriptor in fields.items():
        if not isinstance(name, str) or not NAME.fullmatch(name.replace("_", "-")):
            raise SelcError(
                "INVALID_MANAGED_ACTION",
                f"managed action {action_id} has an invalid input field",
                "use bounded identifier field names",
            )
        if not isinstance(descriptor, dict) or set(descriptor).difference({"type", "enum"}):
            raise SelcError(
                "INVALID_MANAGED_ACTION",
                f"managed action {action_id} field {name} is invalid",
                "use a type and optional enum",
            )
        field_type = descriptor.get("type")
        if field_type not in MANAGED_ACTION_TYPES:
            raise SelcError(
                "INVALID_MANAGED_ACTION",
                f"managed action {action_id} field {name} has an unsupported type",
                "use a protocol-1 scalar or path type",
            )
        enum = descriptor.get("enum")
        if enum is not None and (not isinstance(enum, list) or not enum or len(enum) > 64):
            raise SelcError(
                "INVALID_MANAGED_ACTION",
                f"managed action {action_id} field {name} enum is invalid",
                "use a non-empty bounded enum",
            )
        normalized_fields[name] = {"type": field_type, **({"enum": list(enum)} if enum else {})}
    return {"fields": normalized_fields, "required": list(required)}


def normalize_artifact_checks(value: object) -> dict[str, dict[str, Any]]:
    """Validate optional protocol-1 artifact-check declarations.

    Declarations select an already trusted checker identity and its business
    rules. They deliberately contain no executable paths, argv values, or
    environment settings. A project-local maintainer registry is responsible
    for any external implementation selected at runtime.
    """

    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SelcError(
            "INVALID_MANIFEST",
            "artifact_checks must be an object keyed by check id",
            "declare each artifact check as an object",
        )
    if len(value) > MAX_ARTIFACT_CHECKS:
        raise SelcError(
            "INVALID_MANIFEST",
            "artifact_checks exceeds the supported check limit",
            "split the suite or keep at most 64 checks",
        )

    normalized: dict[str, dict[str, Any]] = {}
    for check_id, raw_declaration in value.items():
        if not isinstance(check_id, str) or not NAME.fullmatch(check_id):
            raise SelcError(
                "INVALID_MANIFEST",
                "artifact check ids must be unique lowercase kebab-case identifiers",
                "repair the artifact_checks keys",
            )
        if not isinstance(raw_declaration, dict):
            raise SelcError(
                "INVALID_MANIFEST",
                f"artifact check {check_id} must be an object",
                "repair the check declaration",
            )
        allowed = {
            "checker",
            "rule_version",
            "required_rule_ids",
            "configuration",
            "require_network_isolation",
        }
        unexpected = set(raw_declaration).difference(allowed)
        if unexpected:
            raise SelcError(
                "INVALID_MANIFEST",
                f"artifact check {check_id} has unsupported fields: {', '.join(sorted(unexpected))}",
                "keep executable registration outside suite.json",
            )

        checker = raw_declaration.get("checker")
        if not isinstance(checker, dict) or set(checker).difference({"name", "version", "digest"}):
            raise SelcError(
                "INVALID_MANIFEST",
                f"artifact check {check_id} needs checker name and version",
                "repair the checker declaration",
            )
        checker_name = checker.get("name")
        checker_version = checker.get("version")
        if not _bounded_artifact_check_string(checker_name) or not _bounded_artifact_check_string(
            checker_version
        ):
            raise SelcError(
                "INVALID_MANIFEST",
                f"artifact check {check_id} checker name and version must be bounded strings",
                "repair the checker declaration",
            )
        checker_digest = checker.get("digest")
        if checker_digest is not None and not _artifact_check_digest(checker_digest):
            raise SelcError(
                "INVALID_MANIFEST",
                f"artifact check {check_id} checker digest must be SHA-256 hex",
                "omit it or use the trusted implementation digest",
            )

        rule_version = raw_declaration.get("rule_version")
        if not _bounded_artifact_check_string(rule_version):
            raise SelcError(
                "INVALID_MANIFEST",
                f"artifact check {check_id} rule_version must be a bounded string",
                "set the rule version",
            )
        required_rule_ids = raw_declaration.get("required_rule_ids")
        if (
            not isinstance(required_rule_ids, list)
            or not required_rule_ids
            or len(required_rule_ids) > MAX_ARTIFACT_CHECK_RULES
            or any(not _bounded_artifact_check_string(item) for item in required_rule_ids)
            or len(set(required_rule_ids)) != len(required_rule_ids)
        ):
            raise SelcError(
                "INVALID_MANIFEST",
                f"artifact check {check_id} required_rule_ids must be unique bounded strings",
                "declare one or more unique required rules",
            )
        configuration = raw_declaration.get("configuration")
        if not isinstance(configuration, dict):
            raise SelcError(
                "INVALID_MANIFEST",
                f"artifact check {check_id} configuration must be an object",
                "use an object with JSON-compatible values",
            )
        try:
            configuration_bytes = canonical_json(configuration).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise SelcError(
                "INVALID_MANIFEST",
                f"artifact check {check_id} configuration is not JSON-compatible",
                "use JSON-compatible configuration values",
            ) from error
        if len(configuration_bytes) > MAX_ARTIFACT_CHECK_CONFIGURATION_BYTES:
            raise SelcError(
                "INVALID_MANIFEST",
                f"artifact check {check_id} configuration exceeds the size limit",
                "keep configuration at or below 16384 bytes",
            )
        require_network_isolation = raw_declaration.get("require_network_isolation", False)
        if not isinstance(require_network_isolation, bool):
            raise SelcError(
                "INVALID_MANIFEST",
                f"artifact check {check_id} require_network_isolation must be boolean",
                "use true or false",
            )
        normalized[check_id] = {
            "checker": {
                "name": checker_name,
                "version": checker_version,
                **({"digest": checker_digest} if checker_digest is not None else {}),
            },
            "rule_version": rule_version,
            "required_rule_ids": list(required_rule_ids),
            "configuration": dict(configuration),
            "require_network_isolation": require_network_isolation,
        }
    return normalized


def validate_skill_artifact_check_references(
    skills: list[Any], artifact_checks: dict[str, dict[str, Any]]
) -> None:
    """Ensure an optional skill ``artifact_check`` points to a declared check."""

    for skill in skills:
        if not isinstance(skill, dict):
            continue
        reference = skill.get("artifact_check")
        if reference is None:
            continue
        if not isinstance(reference, str) or reference not in artifact_checks:
            raise SelcError(
                "INVALID_MANIFEST",
                "skill artifact_check must name a declared artifact check",
                "declare the check in artifact_checks or remove the reference",
            )


def _bounded_artifact_check_string(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value.encode("utf-8")) <= MAX_ARTIFACT_CHECK_IDENTIFIER_BYTES
    )


def _artifact_check_digest(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def normalize_declared_skills(skills: list[Any]) -> list[dict[str, str]]:
    """Return manifest skill entries with the documented default path applied.

    The normal form is intentionally derived in memory.  Consumers share it for
    file lookup and registration, while ``load_manifest`` continues to preserve
    the exact manifest bytes and its content hash.
    """

    names: set[str] = set()
    normalized: list[dict[str, str]] = []
    for skill in skills:
        if not isinstance(skill, dict) or not isinstance(skill.get("name"), str):
            raise SelcError("INVALID_MANIFEST", "each skill requires a name", "repair suite.json")
        name = skill["name"]
        if not NAME.fullmatch(name) or name in names:
            raise SelcError(
                "INVALID_MANIFEST",
                f"invalid or duplicate skill name: {name}",
                "use unique kebab-case names",
            )
        names.add(name)
        relative_value = skill.get("path", f"skills/{name}")
        if not isinstance(relative_value, str):
            raise SelcError(
                "INVALID_MANIFEST",
                f"skill path must be a string: {name}",
                "repair the declared skill path",
            )
        relative = require_relative_path(relative_value)
        declaration = {"name": name, "path": relative.as_posix()}
        basic_path_value = skill.get("basic_path")
        if basic_path_value is not None:
            if not isinstance(basic_path_value, str):
                raise SelcError(
                    "INVALID_MANIFEST",
                    f"skill basic_path must be a string: {name}",
                    "declare a safe relative ordinary skill directory",
                )
            basic_path = require_relative_path(basic_path_value)
            if basic_path.parts[0] != "skills":
                raise SelcError(
                    "INVALID_MANIFEST",
                    f"skill basic_path must stay below skills/: {name}",
                    "declare the ordinary body below the portable skills directory",
                )
            declaration["basic_path"] = basic_path.as_posix()
        normalized.append(declaration)
    return normalized


def validate_declared_skills(suite_root: Path, skills: list[Any]) -> None:
    """Validate manifest-owned skill names, paths, and portable bodies."""

    for skill in normalize_declared_skills(skills):
        name = skill["name"]
        relative = Path(skill["path"])
        skill_file = suite_root / relative / "SKILL.md"
        if not skill_file.is_file():
            raise SelcError(
                "INVALID_MANIFEST",
                f"missing skill file: {relative}/SKILL.md",
                "create the declared skill",
            )
        validate_skill_structure(skill_file, name)
        basic_path = skill.get("basic_path")
        if basic_path is not None:
            basic_skill_file = suite_root / str(basic_path) / "SKILL.md"
            if not basic_skill_file.is_file():
                raise SelcError(
                    "INVALID_MANIFEST",
                    f"missing ordinary skill file: {basic_path}/SKILL.md",
                    "create the declared basic_path skill body",
                )
            validate_skill_structure(basic_skill_file, name)


def basic_registration_paths(manifest: dict[str, Any], suite_root: Path) -> dict[str, str]:
    """Return ordinary skill directories or reject managed-only declarations.

    A managed action may keep a distinct observed body, but ordinary installation
    must never expose that body as though it were independently executable.
    """

    declared_skills = normalize_declared_skills(manifest["skills"])
    managed_skills = {
        action["skill"] for action in managed_configuration(manifest, suite_root).values()
    }
    paths: dict[str, str] = {}
    for skill in declared_skills:
        name = skill["name"]
        basic_path = skill.get("basic_path")
        if basic_path is None and name in managed_skills:
            raise SelcError(
                "BASIC_ENTRY_UNAVAILABLE",
                f"managed skill {name} has no ordinary basic_path body",
                "add an independently usable basic_path body or install with --mode observed",
            )
        paths[name] = basic_path or skill["path"]
    return paths


def validate_skill_structure(skill_file: Path, declared_name: str) -> None:
    """Validate the small portable SKILL.md contract without a YAML dependency.

    SELC needs only three portable guarantees: a bounded frontmatter block, a
    matching scalar ``name``, and non-empty scalar ``description`` and body.
    This deliberately is not a replacement for skill-creator's full YAML
    validation, which remains an authoring-tool check outside the runtime.
    """

    try:
        source = skill_file.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise SelcError(
            "INVALID_SKILL",
            f"cannot read declared skill: {skill_file}",
            "restore the declared SKILL.md file",
        ) from error
    _validate_portable_frontmatter_characters(_raw_frontmatter_source(source))
    lines = source.splitlines()
    if not lines or lines[0] != "---":
        raise SelcError(
            "INVALID_SKILL",
            f"skill frontmatter must start at the first line: {skill_file}",
            "add supported name and description frontmatter",
        )
    try:
        closing_index = lines.index("---", 1)
    except ValueError as error:
        raise SelcError(
            "INVALID_SKILL",
            f"skill frontmatter has no closing boundary: {skill_file}",
            "close the supported frontmatter block",
        ) from error
    values: dict[str, str] = {}
    for line in lines[1:closing_index]:
        if not line.strip(ASCII_SPACE) or line.lstrip(ASCII_SPACE).startswith("#"):
            continue
        if line[0] == ASCII_SPACE:
            raise SelcError(
                "INVALID_SKILL",
                f"skill frontmatter has an unsupported nested field: {skill_file}",
                "use supported top-level name and description scalars",
            )
        if ":" not in line:
            raise SelcError(
                "INVALID_SKILL",
                f"skill frontmatter has an unsupported field: {skill_file}",
                "use simple scalar name and description fields",
            )
        key, raw_value = line.split(":", maxsplit=1)
        if not FRONTMATTER_KEY.fullmatch(key):
            raise SelcError(
                "INVALID_SKILL",
                "portable skill frontmatter has an unsupported top-level key",
                "use an unquoted ASCII identifier key without surrounding whitespace",
            )
        if key not in {"name", "description"}:
            continue
        if raw_value and raw_value[0] != ASCII_SPACE:
            raise SelcError(
                "INVALID_SKILL",
                "portable skill frontmatter requires whitespace after a required field colon",
                "write required fields as 'name: value' and 'description: value'",
            )
        value = _portable_frontmatter_scalar(raw_value)
        if key in {"name", "description"}:
            if key in values:
                raise SelcError(
                    "INVALID_SKILL",
                    f"skill frontmatter repeats {key}: {skill_file}",
                    "keep one supported name and description field",
                )
            values[key] = value
    if values.get("name") != declared_name:
        raise SelcError(
            "INVALID_SKILL",
            f"skill name does not match its declared manifest name: {skill_file}",
            "make frontmatter name match suite.json",
        )
    if not values.get("description"):
        raise SelcError(
            "INVALID_SKILL",
            f"skill description must be a non-empty string: {skill_file}",
            "provide a supported scalar description",
        )
    if not "\n".join(lines[closing_index + 1 :]).strip():
        raise SelcError(
            "INVALID_SKILL",
            f"skill body must be non-empty: {skill_file}",
            "write the portable skill instructions below frontmatter",
        )


def _portable_frontmatter_scalar(raw_value: str) -> str:
    """Parse the deliberately small YAML-string subset portable suites support.

    This parser accepts only forms that can be shown to be strings without a
    YAML dependency.  It intentionally does not attempt to support every YAML
    scalar accepted by the authoring validator.
    """

    value = raw_value.lstrip(ASCII_SPACE)
    if not value or value.startswith("#"):
        return ""
    if value.startswith('"'):
        return _double_quoted_frontmatter_string(value)
    if value.startswith("'"):
        return _single_quoted_frontmatter_string(value)

    plain = _plain_frontmatter_string(value)
    if not plain:
        return ""
    if "\t" in plain:
        raise SelcError(
            "INVALID_SKILL",
            "portable skill frontmatter permits tabs only inside quoted strings",
            "quote text containing a tab or replace it with an ASCII space",
        )
    if not (plain[0].isalpha() or plain[0] == "_"):
        raise SelcError(
            "INVALID_SKILL",
            "portable skill frontmatter needs a string scalar",
            "quote numeric, date-like, or symbolic values",
        )
    if plain.casefold() in {"~", "null", "true", "false", "yes", "no", "on", "off"}:
        raise SelcError(
            "INVALID_SKILL",
            "portable skill frontmatter needs a string scalar",
            "quote reserved YAML values",
        )
    if re.search(r":(?= |$)", plain):
        raise SelcError(
            "INVALID_SKILL",
            "portable skill frontmatter has an ambiguous mapping value",
            "quote text that contains a mapping-like colon",
        )
    return plain


def _double_quoted_frontmatter_string(value: str) -> str:
    """Accept a JSON string followed by only whitespace or a YAML comment."""

    try:
        decoded, end = json.JSONDecoder().raw_decode(value)
    except json.JSONDecodeError as error:
        raise SelcError(
            "INVALID_SKILL",
            "portable skill frontmatter has an invalid double-quoted string",
            "use JSON string syntax or a simple single-quoted string",
        ) from error
    if not isinstance(decoded, str):
        raise SelcError(
            "INVALID_SKILL",
            "portable skill frontmatter needs a string scalar",
            "quote the value as a JSON string",
        )
    _assert_scalar_tail(value[end:])
    return decoded.strip(ASCII_SPACE)


def _single_quoted_frontmatter_string(value: str) -> str:
    """Accept YAML's simple single-quoted string with doubled-quote escapes."""

    characters: list[str] = []
    index = 1
    while index < len(value):
        character = value[index]
        if character != "'":
            characters.append(character)
            index += 1
            continue
        if index + 1 < len(value) and value[index + 1] == "'":
            characters.append("'")
            index += 2
            continue
        _assert_scalar_tail(value[index + 1 :])
        return "".join(characters).strip(ASCII_SPACE)
    raise SelcError(
        "INVALID_SKILL",
        "portable skill frontmatter has an unterminated single-quoted string",
        "close the string or use JSON double-quoted syntax",
    )


def _assert_scalar_tail(tail: str) -> None:
    """Require an explicit whitespace boundary before an outside comment."""

    if not tail:
        return
    if tail[0] != ASCII_SPACE:
        raise SelcError(
            "INVALID_SKILL",
            "portable skill frontmatter has text after a quoted string",
            "separate a trailing comment with whitespace",
        )
    remainder = tail.lstrip(ASCII_SPACE)
    if remainder and not remainder.startswith("#"):
        raise SelcError(
            "INVALID_SKILL",
            "portable skill frontmatter has text after a quoted string",
            "put only a comment after the quoted string",
        )


def _plain_frontmatter_string(value: str) -> str:
    """Strip only a YAML comment whose hash has a preceding whitespace boundary."""

    for index, character in enumerate(value):
        if character == "#" and index > 0 and value[index - 1] == ASCII_SPACE:
            return value[:index].rstrip(ASCII_SPACE)
    return value.rstrip(ASCII_SPACE)


def _raw_frontmatter_source(source: str) -> str:
    """Return frontmatter source without normalizing control characters first."""

    offset = 0
    first_newline = source.find("\n")
    if first_newline < 0 or source[:first_newline].rstrip("\r") != "---":
        return source
    offset = first_newline + 1
    while offset <= len(source):
        newline = source.find("\n", offset)
        line_end = len(source) if newline < 0 else newline
        raw_line = source[offset:line_end]
        if raw_line.rstrip("\r") == "---":
            return source[: line_end if newline < 0 else newline + 1]
        if newline < 0:
            return source
        offset = newline + 1
    return source


def _validate_portable_frontmatter_characters(source: str) -> None:
    """Reject raw frontmatter characters outside YAML's printable character set.

    This check intentionally sees source characters before JSON or single-quote
    escape handling. A legal JSON escape such as ``\\u0000`` remains ordinary
    printable source text; a literal NUL remains invalid source input.
    """

    for character in source:
        code_point = ord(character)
        is_printable = (
            code_point == 0x09
            or code_point == 0x0A
            or code_point == 0x0D
            or 0x20 <= code_point <= 0x7E
            or code_point == 0x85
            or 0xA0 <= code_point <= 0xD7FF
            or 0xE000 <= code_point <= 0xFFFD
            or 0x10000 <= code_point <= 0x10FFFF
        )
        if not is_printable:
            raise SelcError(
                "INVALID_SKILL",
                "portable skill frontmatter contains an unsupported control character",
                "remove the raw control character or use a supported quoted escape",
            )


def render_text_report(text: str, resources_root: Path) -> str:
    """Render the fixture report from the portable, declared resource contract."""

    configuration_path = resources_root / "text-report.json"
    if not configuration_path.is_file():
        raise SelcError(
            "MISSING_RESOURCE",
            "text report resource is missing",
            "restore resources/text-report.json from the installed suite",
        )
    return render_text_report_bytes(text, configuration_path.read_bytes())


def render_text_report_bytes(text: str, configuration_bytes: bytes) -> str:
    """Render from one caller-owned resource snapshot used for verification hashes."""

    try:
        configuration = json.loads(configuration_bytes.decode("utf-8"))
    except json.JSONDecodeError as error:
        raise SelcError(
            "INVALID_RESOURCE",
            "text report resource is not valid JSON",
            "restore resources/text-report.json",
        ) from error
    heading = configuration.get("heading") if isinstance(configuration, dict) else None
    bullet_prefix = configuration.get("bullet_prefix") if isinstance(configuration, dict) else None
    if not isinstance(heading, str) or not isinstance(bullet_prefix, str):
        raise SelcError(
            "INVALID_RESOURCE",
            "text report resource must define string heading and bullet_prefix values",
            "restore resources/text-report.json",
        )
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    bullets = "\n".join(f"{bullet_prefix}{line}" for line in lines)
    return f"# {heading}\n\n{bullets}\n"


def load_manifest(suite_root: Path) -> dict[str, Any]:
    from .util import read_json

    manifest = read_json(manifest_path(suite_root))
    validate_manifest(manifest, suite_root)
    declared_hash = manifest.get("content_hash")
    if declared_hash is not None and declared_hash != suite_content_hash(suite_root):
        raise SelcError(
            "CONTENT_HASH_MISMATCH",
            "suite manifest content hash does not match",
            "restore or recreate the suite",
        )
    descriptor_path = framework_path(suite_root)
    if descriptor_path.is_file():
        framework = load_framework(suite_root)
        if framework["framework_version"] != manifest["framework_version"]:
            raise SelcError(
                "FRAMEWORK_MISMATCH",
                "suite manifest and embedded framework use different framework versions",
                "rebuild the suite with a matching framework descriptor",
            )
        if framework["runtime_protocol"] != manifest.get("runtime_protocol", RUNTIME_PROTOCOL):
            raise SelcError(
                "FRAMEWORK_MISMATCH",
                "suite manifest and embedded framework use different runtime protocols",
                "rebuild the suite with a matching framework descriptor",
            )
    return manifest


def seal_suite(suite_root: Path) -> dict[str, Any]:
    """Refresh an authoring suite's declared content hash after explicit validation.

    Sealing is deliberately narrower than suite creation or framework assembly.  It
    verifies the portable surface and existing embedded framework before changing
    only ``suite.json``'s self-excluded content hash.  It cannot make a changed
    runtime look valid by rewriting ``framework.json``.
    """

    from .util import read_json

    manifest = read_json(manifest_path(suite_root))
    validate_manifest(manifest, suite_root)
    _suite_files(suite_root)

    descriptor_path = framework_path(suite_root)
    if not descriptor_path.is_file():
        raise SelcError(
            "INVALID_FRAMEWORK",
            "seal requires the existing embedded framework descriptor",
            "restore framework.json before sealing this suite",
        )
    framework = load_framework(suite_root)
    manifest_protocol = manifest.get("runtime_protocol", RUNTIME_PROTOCOL)
    if framework["framework_version"] != manifest["framework_version"]:
        raise SelcError(
            "FRAMEWORK_MISMATCH",
            "suite manifest and embedded framework use different framework versions",
            "rebuild the suite with a matching framework descriptor",
        )
    if framework["runtime_protocol"] != manifest_protocol:
        raise SelcError(
            "FRAMEWORK_MISMATCH",
            "suite manifest and embedded framework use different runtime protocols",
            "rebuild the suite with a matching framework descriptor",
        )

    sealed_manifest = dict(manifest)
    sealed_manifest["content_hash"] = suite_content_hash(suite_root)
    write_json(manifest_path(suite_root), sealed_manifest)
    return sealed_manifest


def create_suite(
    suite_id: str,
    version: str,
    output: Path,
    skills: list[str],
    *,
    managed: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if output.exists():
        raise SelcError(
            "PATH_CONFLICT", f"output already exists: {output}", "choose an empty output path"
        )
    if not NAME.fullmatch(suite_id):
        raise SelcError(
            "INVALID_INPUT",
            "suite_id must be a lowercase kebab-case identifier",
            "choose a valid suite_id",
        )
    if not version:
        raise SelcError("INVALID_INPUT", "version is required", "pass --version")
    if (
        len(set(skills)) != len(skills)
        or not skills
        or any(not NAME.fullmatch(skill) for skill in skills)
    ):
        raise SelcError(
            "INVALID_INPUT", "provide unique kebab-case --skill values", "repair the skill list"
        )

    managed_configuration_input, skill_artifact_checks, artifact_checks = _create_declaration_parts(
        managed, skills
    )
    if managed_configuration_input is None and managed is None and "text-report" in skills:
        managed_configuration_input = _default_text_report_managed_configuration()
    managed_actions = _managed_actions_by_skill(managed_configuration_input)
    basic_paths = _created_basic_paths(skills, managed_actions)
    output.mkdir(parents=True)
    skill_entries: list[dict[str, Any]] = []
    for skill in skills:
        skill_directory = output / "skills" / skill
        skill_directory.mkdir(parents=True)
        (skill_directory / "SKILL.md").write_text(
            _created_skill_body(skill, suite_id, managed_actions.get(skill)),
            encoding="utf-8",
        )
        entry: dict[str, Any] = {"name": skill, "path": f"skills/{skill}"}
        action = managed_actions.get(skill)
        if action is not None:
            basic_path = basic_paths[skill]
            basic_directory = output / basic_path
            basic_directory.mkdir(parents=True)
            (basic_directory / "SKILL.md").write_text(
                _created_basic_skill_body(skill, suite_id, action), encoding="utf-8"
            )
            entry["basic_path"] = basic_path
        artifact_check = skill_artifact_checks.get(skill)
        if artifact_check is not None:
            entry["artifact_check"] = artifact_check
        skill_entries.append(entry)

    resources = output / "resources"
    resources.mkdir()
    write_json(resources / "text-report.json", {"heading": "Text report", "bullet_prefix": "- "})

    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "suite_id": suite_id,
        "version": version,
        "framework_version": RUNTIME_VERSION,
        "runtime_protocol": RUNTIME_PROTOCOL,
        "skills": skill_entries,
        "source": {"kind": "created", "repo": None, "subdir": ".", "commit": None, "base": None},
    }
    if managed_configuration_input is not None:
        manifest["managed"] = managed_configuration_input
    if artifact_checks:
        manifest["artifact_checks"] = artifact_checks
    write_json(manifest_path(output), manifest)
    _copy_portable_runtime(output)
    _write_portable_files(output)
    _create_declared_resource_scripts(output, managed_configuration_input)
    write_json(
        framework_path(output),
        {
            "schema_version": SCHEMA_VERSION,
            "framework_version": RUNTIME_VERSION,
            "runtime_protocol": RUNTIME_PROTOCOL,
            "runtime_hash": tree_hash(output / "runtime"),
        },
    )
    manifest["content_hash"] = suite_content_hash(output)
    write_json(manifest_path(output), manifest)
    return load_manifest(output)


def _default_text_report_managed_configuration() -> dict[str, Any]:
    return {
        "protocol": MANAGED_PROTOCOL,
        "actions": {
            "text-report": {
                "skill": "text-report",
                "kind": "text-report-v1",
                "script": "bin/selc-suite.py",
                "input": {
                    "fields": {"input": {"type": "path"}},
                    "required": ["input"],
                },
            }
        },
    }


def _managed_actions_by_skill(managed: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not isinstance(managed, dict):
        return {}
    actions = managed.get("actions")
    if not isinstance(actions, dict):
        return {}
    by_skill: dict[str, dict[str, Any]] = {}
    for action in actions.values():
        if not isinstance(action, dict):
            continue
        skill = action.get("skill")
        if isinstance(skill, str) and skill not in by_skill:
            by_skill[skill] = action
    return by_skill


def _created_basic_paths(
    skills: list[str], managed_actions: dict[str, dict[str, Any]]
) -> dict[str, str]:
    """Allocate ordinary-body directories without colliding with declared skills."""

    reserved_names = set(skills)
    paths: dict[str, str] = {}
    for skill in skills:
        if skill not in managed_actions:
            continue
        base_name = f"{skill}-basic"
        directory_name = base_name
        suffix = 2
        while directory_name in reserved_names:
            directory_name = f"{base_name}-{suffix}"
            suffix += 1
        reserved_names.add(directory_name)
        paths[skill] = f"skills/{directory_name}"
    return paths


def _created_skill_body(
    skill: str,
    suite_id: str,
    managed_action: dict[str, Any] | None,
) -> str:
    frontmatter = (
        "---\n"
        f"name: {json.dumps(skill)}\n"
        f"description: {skill} from the {suite_id} suite.\n"
        "---\n\n"
        f"# {skill}\n\n"
    )
    if managed_action is None:
        return (
            frontmatter
            + "Follow this skill's ordinary task instructions and write its requested output. "
            "Read `../../MAINTENANCE.md` before changing a skill or declared resource.\n"
        )
    return (
        frontmatter
        + "This observed skill body is supplied only after the installed managed runtime verifies "
        "the current native activation. Discovery or scanning creates no lifecycle record. "
        "Use the declared managed action to record the task, invocation, checked publication, "
        "and timeline. Read `../../MAINTENANCE.md` before changing a skill or declared resource.\n"
    )


def _created_basic_skill_body(skill: str, suite_id: str, action: dict[str, Any]) -> str:
    frontmatter = (
        "---\n"
        f"name: {json.dumps(skill)}\n"
        f"description: {skill} ordinary-use entry from the {suite_id} suite.\n"
        "---\n\n"
        f"# {skill}\n\n"
    )
    kind = action.get("kind")
    if kind == "text-report-v1":
        return (
            frontmatter
            + "Set `SELC_PYTHON` to the absolute path of a Python 3.11+ executable, and set "
            "`SELC_SKILL_FILE` to the absolute path of this loaded `SKILL.md`. Resolve its "
            "physical directory before using its grandparent as `SELC_SUITE`; keep the project working directory unchanged "
            "so project-relative inputs keep working. This path does not require native hooks "
            "or create observation records.\n\n"
            "```sh\n"
            'SELC_PYTHON="<absolute path to a Python 3.11+ executable>"\n'
            'SELC_SKILL_FILE="<absolute path to this loaded SKILL.md>"\n'
            'SELC_SKILL_DIR="$(cd -P "$(dirname "$SELC_SKILL_FILE")" && pwd)"\n'
            'SELC_SUITE="$(cd "$SELC_SKILL_DIR/../.." && pwd)"\n'
            "```\n\n"
            "```sh\n"
            '"$SELC_PYTHON" "$SELC_SUITE/bin/selc-suite.py" run text-report --input <input.txt> --output <report.md>\n'
            "```\n"
        )
    if kind == "python-script-v1":
        script = action.get("script")
        if isinstance(script, str):
            return (
                frontmatter
                + "Set `SELC_PYTHON` to the absolute path of a Python 3.11+ executable, and set "
                "`SELC_SKILL_FILE` to the absolute path of this loaded `SKILL.md`. Resolve its "
                "physical directory before using its grandparent as `SELC_SUITE`; keep the project working directory unchanged "
                "so project-relative inputs keep working. This path does not require a runtime receipt.\n\n"
                "```sh\n"
                'SELC_PYTHON="<absolute path to a Python 3.11+ executable>"\n'
                'SELC_SKILL_FILE="<absolute path to this loaded SKILL.md>"\n'
                'SELC_SKILL_DIR="$(cd -P "$(dirname "$SELC_SKILL_FILE")" && pwd)"\n'
                'SELC_SUITE="$(cd "$SELC_SKILL_DIR/../.." && pwd)"\n'
                "```\n\n"
                "```sh\n"
                f'"$SELC_PYTHON" "$SELC_SUITE/{script}" managed-run --request <request.json> --output <artifact>\n'
                "```\n"
            )
    return (
        frontmatter + "Write the ordinary task steps and output contract for this skill here. "
        "This basic entry intentionally does not require managed activation or a native receipt.\n"
    )


def _create_declaration_parts(
    declaration: dict[str, Any] | None,
    skills: list[str],
) -> tuple[dict[str, Any] | None, dict[str, str], dict[str, Any]]:
    """Separate managed actions from public top-level checker declarations.

    Older callers can pass the protocol-1 managed object directly. New generic
    authoring declarations must use the explicit envelope so artifact checks
    remain ordinary manifest fields rather than undocumented managed metadata.
    """

    if declaration is None:
        return None, {}, {}
    if "managed" not in declaration:
        return declaration, {}, {}
    allowed = {"managed", "artifact_checks", "skill_artifact_checks"}
    unknown = set(declaration).difference(allowed)
    if unknown:
        raise SelcError(
            "INVALID_MANAGED_DECLARATION",
            "managed authoring declaration has unsupported top-level fields",
            "use managed, artifact_checks, and skill_artifact_checks only",
        )
    managed = declaration.get("managed")
    checks = declaration.get("artifact_checks", {})
    bindings = declaration.get("skill_artifact_checks", {})
    if (
        not isinstance(managed, dict)
        or not isinstance(checks, dict)
        or not isinstance(bindings, dict)
    ):
        raise SelcError(
            "INVALID_MANAGED_DECLARATION",
            "managed authoring declaration sections must be JSON objects",
            "declare managed actions, artifact_checks, and skill_artifact_checks as objects",
        )
    if set(bindings).difference(skills) or any(
        not isinstance(skill, str) or not isinstance(check_id, str)
        for skill, check_id in bindings.items()
    ):
        raise SelcError(
            "INVALID_MANAGED_DECLARATION",
            "skill_artifact_checks must map declared skill names to check ids",
            "bind each check only to a --skill declared for this suite",
        )
    return managed, dict(bindings), checks


def _copy_portable_runtime(output: Path) -> None:
    runtime_root = output / "runtime" / "selc"
    runtime_root.mkdir(parents=True)
    source_root = Path(__file__).resolve().parent
    for source in source_root.glob("*.py"):
        shutil.copy2(source, runtime_root / source.name)
    framework_license = source_root / RUNTIME_LICENSE_NAME
    if not framework_license.is_file() or framework_license.is_symlink():
        raise SelcError(
            "FRAMEWORK_LICENSE_UNAVAILABLE",
            "the embedded SELC framework license is unavailable",
            "restore src/selc/SELC-LICENSE before creating a suite",
        )
    # A generated suite is itself a framework source when its bundled
    # ``bin/selc-suite.py`` creates another suite.  Keep the exact MIT text at
    # both the packaged runtime root (the external package contract) and next
    # to the copied ``selc`` modules (the portable source contract).
    shutil.copy2(framework_license, runtime_root / RUNTIME_LICENSE_NAME)
    shutil.copy2(framework_license, output / "runtime" / RUNTIME_LICENSE_NAME)


def _create_declared_resource_scripts(output: Path, managed: dict[str, Any] | None) -> None:
    """Materialize editable resource-script placeholders for generic creation.

    A generated suite can declare arbitrarily named ``python-script-v1``
    actions without placing editable business code in the protected runtime or
    wrapper.  The creator replaces the bounded resource script before sealing.
    """

    if not isinstance(managed, dict):
        return
    actions = managed.get("actions")
    if not isinstance(actions, dict):
        return
    for action in actions.values():
        if not isinstance(action, dict) or action.get("kind") != "python-script-v1":
            continue
        script = action.get("script")
        if not isinstance(script, str):
            continue
        relative = require_relative_path(script)
        if relative.parts[0] != "resources":
            continue
        destination = output / relative
        if destination.exists():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            "#!/usr/bin/env python3\n"
            "raise SystemExit('Replace this generated managed action resource before use.')\n",
            encoding="utf-8",
        )


def _write_portable_files(output: Path) -> None:
    bin_directory = output / "bin"
    bin_directory.mkdir()
    portable_cli = bin_directory / "selc-suite.py"
    portable_cli.write_text(
        "#!/usr/bin/env python3\n"
        "from pathlib import Path\n"
        "import sys\n\n"
        "if sys.version_info < (3, 11):\n"
        "    raise SystemExit('SELC suite requires Python 3.11+; invoke with a supported interpreter.')\n"
        "sys.dont_write_bytecode = True\n"
        "ROOT = Path(__file__).resolve().parents[1]\n"
        "sys.path.insert(0, str(ROOT / 'runtime'))\n"
        "from selc.cli import main\n\n"
        "if __name__ == '__main__':\n"
        "    raise SystemExit(main(default_suite_root=ROOT))\n",
        encoding="utf-8",
    )
    portable_cli.chmod(0o755)
    (output / "install.py").write_text(
        "#!/usr/bin/env python3\n"
        "from pathlib import Path\n"
        "import sys\n\n"
        "if sys.version_info < (3, 11):\n"
        "    raise SystemExit('SELC suite requires Python 3.11+; invoke with a supported interpreter.')\n"
        "sys.dont_write_bytecode = True\n"
        "ROOT = Path(__file__).resolve().parent\n"
        "sys.path.insert(0, str(ROOT / 'runtime'))\n"
        "from selc.cli import main\n\n"
        "if __name__ == '__main__':\n"
        "    raise SystemExit(main(default_suite_root=ROOT, force_command='install-local'))\n",
        encoding="utf-8",
    )
    (output / "MAINTENANCE.md").write_text(
        "# Maintaining this suite\n\n"
        "Install normal use with `install.py --project-root <project> --harness <codex|workbuddy>`; "
        "the omitted mode is `basic`. Basic registration points directly to the declared ordinary body, "
        "adds no hooks, needs no receipt, and does not create automatic statistics or evaluation evidence. "
        "A managed skill needs an independently usable `basic_path`; legacy observed-only packages must use "
        "`--mode observed` instead of rewriting their managed body. Select `--mode observed` only when "
        "automatic observation is required. Existing installations, updates, and recovery preserve the saved "
        "mode; old registrations without one remain observed for compatibility. The observed hook, native "
        "receipt, automatic-statistics, one-shot, creator/evaluation, contribution, and release path is "
        "EXPERIMENTAL. It can return `RECEIPT_UNAVAILABLE` and does not establish desktop or new-host/model "
        "support, nor does it qualify a managed business candidate.\n\n"
        "Keep the installed suite immutable. For an uninstalled authoring source, run the available "
        "skill-creator validation after editing a declared skill or resource, then explicitly run "
        "`$SELC_PYTHON bin/selc-suite.py seal --suite <authoring-source>` before `validate` and "
        "`package`. Seal only refreshes that source manifest after checking its existing runtime; it "
        "does not adopt installed local edits or update an installation registry. Set `SELC_PYTHON` "
        "to a Python 3.11+ interpreter, then prepare an isolated candidate with "
        "`$SELC_PYTHON bin/selc-suite.py evolve prepare --suite . --output <candidate-parent>`, then edit "
        "only the resulting candidate `skills/` and declared `resources/` files. `evolve evaluate` is a "
        "fixture diagnostic only. Before a supported Codex plan, generate a complete private alpha.11 "
        "`evolve policy-template` with its narrow Python readonly root and copy it into the matrix; raw "
        "policy-digest-only plans remain unverified. The template freezes approval, network, tools, CLI "
        "configuration, and filesystem roots, allowing only the isolated project and the account-bound "
        "Codex arg0 bootstrap path to vary. A production contribution or release needs a frozen `evolve plan` that "
        "declares each version scope and actual manifest version, an isolated creator request with a bound "
        "maintainer regression brief, native session/turn/tool policy evidence, explicit protected-path denies, "
        "and only trusted skill-creator/Python readonly dependencies, a bound full candidate snapshot, "
        "one durable attempt per required matrix unit, and "
        "`evolve finalize`; unknown, cancelled, and timed-out attempts are never retried. A candidate pass "
        "never authorizes a different snapshot. Use `evolve attempt native-evidence-template` to identify "
        "the original host-store session cutoff SHA-256, path hash, device, and inode required for reconstruction. "
        "Codex qualification recomputes root/pre/Post prefixes and binds a hook tool through its matching "
        "CommandExecution result; outer exec IDs are a separate namespace. Schema-3 Codex templates bind one "
        "explicit installer-owned routing entry after installation: the frozen explicit @skill JSON request must "
        "select one declared and registered fixed-script action, then the private binding derives exactly one "
        "`/bin/cat <absolute-routing-entry>` command and its protected entry/reader identities. The matching "
        "initialization must contain one indexed generic developer fragment rendered from that exact command; its "
        "pathless preparation summary is not a tools-policy proof. Schema-1 and schema-2 templates retain their "
        "original profiles and argv. WorkBuddy has a bounded "
        "native-session diagnostic for model identity and completed current-request tools, but no effective policy; "
        "its qualification remains unverified. "
        "`contribute` and `release` independently reconstruct this real evidence before each prepare and "
        "execute step. Keep `runtime/`, including `runtime/SELC-LICENSE`, intact. Installed managed actions "
        "record selected executions automatically; discovery never creates lifecycle evidence. Legacy record "
        "commands remain diagnostics for legacy subjects and cannot modify managed-v2 tasks. This suite works "
        "from Codex and WorkBuddy installation directories without a full SELC checkout.\n",
        encoding="utf-8",
    )


def package_suite(
    suite_root: Path, output: Path, *, source_baseline: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Package only declared portable content and optional saved Git provenance.

    Release preparation materializes a detached target into a temporary directory,
    so it supplies its already-validated source baseline explicitly.  Ordinary
    packaging derives the same descriptor from the checkout containing the suite.
    """

    manifest = load_manifest(suite_root)
    try:
        output.resolve().relative_to(suite_root.resolve())
    except ValueError:
        pass
    else:
        raise SelcError(
            "UNSAFE_PACKAGE_OUTPUT",
            "package output must be outside the suite root",
            "choose an output path outside the suite",
        )
    if output.exists():
        raise SelcError(
            "PATH_CONFLICT", f"package already exists: {output}", "choose a new package path"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in _suite_files(suite_root):
            archive.write(path, path.relative_to(suite_root).as_posix())
        provenance = (
            source_baseline if source_baseline is not None else _git_source_baseline(suite_root)
        )
        if provenance is not None:
            _validate_source_baseline(provenance)
            archive.writestr(SOURCE_BASELINE_NAME, json.dumps(provenance, indent=2) + "\n")
    return {"manifest": manifest, "package_hash": file_hash(output), "output": str(output)}


def _git_source_baseline(suite_root: Path) -> dict[str, Any] | None:
    repository_root: Path | None = None
    for candidate in (suite_root, *suite_root.parents):
        if (candidate / ".git").exists():
            repository_root = candidate
            break
    if repository_root is None:
        return None
    try:
        commit = run_argv(["git", "rev-parse", "HEAD"], cwd=repository_root)
        branch = run_argv(["git", "branch", "--show-current"], cwd=repository_root)
    except SelcError:
        return None
    try:
        relative_root = suite_root.resolve().relative_to(repository_root.resolve())
    except ValueError:
        return None
    portable_paths = [
        path.relative_to(repository_root).as_posix() for path in _suite_files(suite_root)
    ]
    if not portable_paths:
        return None
    try:
        run_argv(["git", "ls-files", "--error-unmatch", "--", *portable_paths], cwd=repository_root)
        status_code, status_output, _ = run_argv_result(
            [
                "git",
                "status",
                "--porcelain",
                "--untracked-files=all",
                "--",
                relative_root.as_posix(),
            ],
            cwd=repository_root,
        )
        diff_code, _diff_output, _diff_error = run_argv_result(
            ["git", "diff", "--quiet", "HEAD", "--", relative_root.as_posix()], cwd=repository_root
        )
    except SelcError:
        return None
    if status_code != 0 or status_output or diff_code != 0:
        return None
    remote_code: str | None = None
    with suppress(SelcError):
        remote_code = run_argv(["git", "config", "--get", "remote.origin.url"], cwd=repository_root)
    contribution_base = branch or _repository_contribution_base(repository_root, commit)
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "git",
        "repo": remote_code or str(repository_root.resolve()),
        "subdir": relative_root.as_posix(),
        "commit": commit,
        "base": branch or commit,
        "contribution_base": contribution_base,
    }


def _repository_contribution_base(repository_root: Path, fallback_commit: str) -> str:
    code, output, _ = run_argv_result(
        ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], cwd=repository_root
    )
    if code == 0 and output.startswith("origin/"):
        return output.removeprefix("origin/")
    branches = run_argv(
        ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads"], cwd=repository_root
    ).splitlines()
    for preferred in ("main", "master"):
        if preferred in branches:
            return preferred
    return branches[0] if len(branches) == 1 else fallback_commit


def _validate_source_baseline(provenance: dict[str, Any]) -> None:
    required = {"schema_version", "kind", "repo", "subdir", "commit", "base"}
    missing = required.difference(provenance)
    if missing or provenance.get("kind") != "git":
        raise SelcError(
            "INVALID_SOURCE_BASELINE",
            "source baseline must describe a complete Git source",
            "repackage from a saved Git baseline",
        )
    for key in ("repo", "subdir", "commit", "base"):
        if not isinstance(provenance[key], str) or not provenance[key]:
            raise SelcError(
                "INVALID_SOURCE_BASELINE",
                f"source baseline {key} must be a non-empty string",
                "repackage from a saved Git baseline",
            )
    subdir = Path(str(provenance["subdir"]))
    if subdir.is_absolute() or ".." in subdir.parts:
        raise SelcError(
            "INVALID_SOURCE_BASELINE",
            "source baseline subdirectory must remain inside its repository",
            "repackage from a safe Git subdirectory",
        )


def package_framework(root: Path, output: Path) -> dict[str, Any]:
    """Create a runtime-only framework package with no suite business content."""

    framework = load_framework(root)
    try:
        output.resolve().relative_to(root.resolve())
    except ValueError:
        pass
    else:
        raise SelcError(
            "UNSAFE_PACKAGE_OUTPUT",
            "framework package output must be outside the framework root",
            "choose an output path outside the framework",
        )
    if output.exists():
        raise SelcError(
            "PATH_CONFLICT", f"package already exists: {output}", "choose a new package path"
        )
    framework_files = [framework_path(root)]
    for path in sorted((root / "runtime").rglob("*")):
        if path.is_symlink():
            relative = path.relative_to(root).as_posix()
            raise SelcError(
                "UNSAFE_SYMLINK",
                f"framework package cannot include a symlink: {relative}",
                "copy the runtime file into the framework package",
            )
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
            framework_files.append(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in framework_files:
            archive.write(path, path.relative_to(root).as_posix())
    return {
        "framework": framework,
        "package_hash": file_hash(output),
        "output": str(output),
    }
