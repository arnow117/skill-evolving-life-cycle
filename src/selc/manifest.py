"""Suite manifest validation and portable suite construction."""

from __future__ import annotations

import json
import re
import shutil
import zipfile
from contextlib import suppress
from hashlib import sha256
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
    write_json,
)

NAME = re.compile(r"^[a-z][a-z0-9-]{1,62}$")
RUNTIME_PROTOCOL = 1
SOURCE_BASELINE_NAME = "source-baseline.json"


def manifest_path(suite_root: Path) -> Path:
    return suite_root / "suite.json"


def suite_content_hash(suite_root: Path) -> str:
    """Hash all suite content while avoiding a self-referential manifest hash."""

    digest = sha256()
    for path in _suite_files(suite_root):
        relative = path.relative_to(suite_root).as_posix()
        digest.update(f"file:{relative}:".encode("utf-8"))
        if path == manifest_path(suite_root):
            contents = json.loads(path.read_text(encoding="utf-8"))
            contents.pop("content_hash", None)
            digest.update(canonical_json(contents).encode("utf-8"))
        else:
            digest.update(path.read_bytes())
    return digest.hexdigest()


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

    digest = sha256()
    for path in _suite_files(suite_root):
        relative = path.relative_to(suite_root).as_posix()
        digest.update(f"file:{relative}:".encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


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
    names: set[str] = set()
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
        relative = require_relative_path(skill.get("path", f"skills/{name}"))
        skill_file = suite_root / relative / "SKILL.md"
        if not skill_file.is_file():
            raise SelcError(
                "INVALID_MANIFEST",
                f"missing skill file: {relative}/SKILL.md",
                "create the declared skill",
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
    try:
        configuration = json.loads(configuration_path.read_text(encoding="utf-8"))
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
            f"name: {skill}\n"
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
