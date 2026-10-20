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
ASCII_SPACE = " "


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
        normalized.append({"name": name, "path": relative.as_posix()})
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


def create_suite(suite_id: str, version: str, output: Path, skills: list[str]) -> dict[str, Any]:
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

    output.mkdir(parents=True)
    skill_entries: list[dict[str, str]] = []
    for skill in skills:
        skill_directory = output / "skills" / skill
        skill_directory.mkdir(parents=True)
        (skill_directory / "SKILL.md").write_text(
            "---\n"
            f"name: {json.dumps(skill)}\n"
            f"description: {skill} from the {suite_id} suite.\n"
            "---\n\n"
            f"# {skill}\n\n"
            "Use this installed suite through its own `bin/selc-suite.py` wrapper. Read "
            "`../../MAINTENANCE.md` before changing a skill or declared resource.\n\n"
            "For an actual selected execution, first start or reuse the business task, then record "
            "the selected skill call with the installed version and digest returned by `install`:\n\n"
            "```bash\n"
            "# Set SELC_PYTHON to a Python 3.11+ interpreter.\n"
            "$SELC_PYTHON ../../bin/selc-suite.py task start --project-root <project-root> "
            "--id <task-id> --suite-id <suite-id> --initiator <initiator> --evidence <evidence>\n"
            "$SELC_PYTHON ../../bin/selc-suite.py call start --project-root <project-root> "
            f"--id <call-id> --task-id <task-id> --skill {skill} --executor <executor> "
            "--evidence <evidence> --expected-version <installed-version> "
            "--expected-hash <installed-digest>\n"
            "```\n\n"
            "Run the declared business command, record its checked artifact with `delivery`, then "
            "finish the call and task. Scanning, discovery, or merely opening this skill is not a "
            "selected execution and must not create a task or call record. The `text-report` entry "
            "uses `$SELC_PYTHON ../../bin/selc-suite.py run text-report --input <input> --output <report>`.\n",
            encoding="utf-8",
        )
        skill_entries.append({"name": skill, "path": f"skills/{skill}"})

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
    write_json(manifest_path(output), manifest)
    _copy_portable_runtime(output)
    _write_portable_files(output)
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
    return manifest


def _copy_portable_runtime(output: Path) -> None:
    runtime_root = output / "runtime" / "selc"
    runtime_root.mkdir(parents=True)
    source_root = Path(__file__).resolve().parent
    for source in source_root.glob("*.py"):
        shutil.copy2(source, runtime_root / source.name)


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
        "Keep the installed suite immutable. For an uninstalled authoring source, run the available "
        "skill-creator validation after editing a declared skill or resource, then explicitly run "
        "`$SELC_PYTHON bin/selc-suite.py seal --suite <authoring-source>` before `validate` and "
        "`package`. Seal only refreshes that source manifest after checking its existing runtime; it "
        "does not adopt installed local edits or update an installation registry. Set `SELC_PYTHON` "
        "to a Python 3.11+ interpreter, then prepare an isolated candidate with "
        "`$SELC_PYTHON bin/selc-suite.py evolve prepare --suite . --output <candidate-parent>`, then edit "
        "only the resulting candidate `skills/` and declared `resources/` files. Evaluate it before "
        "contribution, and use `contribute prepare` with the project root and suite id so the saved "
        "source baseline is restored. Keep `runtime/` intact. Record only selected, executed skills "
        "with `task start`, `call start`, `delivery`, and completion commands through "
        "`bin/selc-suite.py`; discovery never creates lifecycle evidence. This suite works from Codex "
        "and WorkBuddy installation directories without a full SELC checkout.\n",
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
    remote_code: str | None = None
    with suppress(SelcError):
        remote_code = run_argv(["git", "config", "--get", "remote.origin.url"], cwd=repository_root)
    contribution_base = branch or _repository_contribution_base(repository_root, commit)
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "git",
        "repo": remote_code or str(repository_root.resolve()),
        "subdir": suite_root.resolve().relative_to(repository_root.resolve()).as_posix(),
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
