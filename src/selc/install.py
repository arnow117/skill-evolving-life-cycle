"""Suite installation, source inspection, and explicit update transactions."""

from __future__ import annotations

import os
import shutil
import tempfile
import time
import zipfile
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from .errors import SelcError
from .manifest import (
    SOURCE_BASELINE_NAME,
    copy_portable_suite,
    framework_path,
    load_framework,
    load_manifest,
    materialize_composite_suite,
    portable_tree_hash,
)
from .records import active_leases, open_database
from .util import (
    copy_tree,
    exclusive_lock,
    read_json,
    run_argv,
    run_argv_result,
    temporary_directory,
    tree_hash,
    write_json,
)


def install_root(project_root: Path) -> Path:
    return project_root / ".selc" / "installs"


def registry_path(project_root: Path) -> Path:
    return project_root / ".selc" / "installations.json"


def registry_lock_path(project_root: Path) -> Path:
    """Serialize every read-modify-write of the shared installation registry."""

    return project_root / ".selc" / ".registry.lock"


def _registry(project_root: Path) -> dict[str, Any]:
    path = registry_path(project_root)
    if not path.exists():
        return {"schema_version": 1, "installations": {}}
    return read_json(path)


def _write_registry(project_root: Path, registry: dict[str, Any]) -> None:
    write_json(registry_path(project_root), registry)


def _safe_extract(archive_path: Path, destination: Path) -> Path:
    with zipfile.ZipFile(archive_path) as archive:
        members = archive.infolist()
        if len(members) > 5000:
            raise SelcError(
                "UNSAFE_ARCHIVE", "archive contains too many members", "use a smaller suite package"
            )
        total_size = sum(member.file_size for member in members)
        if total_size > 100 * 1024 * 1024:
            raise SelcError(
                "UNSAFE_ARCHIVE", "archive exceeds the size limit", "use a smaller suite package"
            )
        for member in members:
            target = (destination / member.filename).resolve()
            if destination.resolve() not in target.parents and target != destination.resolve():
                raise SelcError(
                    "UNSAFE_ARCHIVE",
                    "archive contains a path outside its root",
                    "repackage the suite",
                )
            if member.is_dir() or member.filename.endswith("/"):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
    return destination


def resolve_source(
    source: str | Path, ref: str | None, subdir: str | None = None
) -> tuple[Path, dict[str, Any]]:
    """Return a disposable/read-only suite root and explicit source baseline metadata."""

    source_text = str(source)
    local_source = Path(source_text.removeprefix("file://")).expanduser()
    if local_source.is_file() and local_source.suffix == ".zip":
        temporary = Path(tempfile.mkdtemp(prefix="selc-package-"))
        _safe_extract(local_source, temporary)
        baseline_path = temporary / SOURCE_BASELINE_NAME
        if baseline_path.is_file():
            baseline = read_json(baseline_path)
            required = {"kind", "repo", "subdir", "commit", "base"}
            if required.issubset(baseline) and baseline.get("kind") == "git":
                return temporary, {
                    "kind": "git",
                    "repo": str(baseline["repo"]),
                    "subdir": str(baseline["subdir"]),
                    "commit": str(baseline["commit"]),
                    "base": str(baseline["base"]),
                    "contribution_base": str(baseline.get("contribution_base") or baseline["base"]),
                }
        return temporary, {
            "kind": "package",
            "repo": str(local_source),
            "subdir": ".",
            "commit": None,
            "base": None,
        }
    if local_source.is_dir() and (local_source / "suite.json").is_file() and subdir in (None, "."):
        return local_source, {
            "kind": "directory",
            "repo": str(local_source.resolve()),
            "subdir": ".",
            "commit": None,
            "base": None,
        }
    is_git_source = _is_git_source(source_text, local_source)
    if is_git_source:
        with temporary_directory("selc-source-") as temporary:
            checkout = temporary / "checkout"
            run_argv(["git", "clone", "--no-checkout", source_text, str(checkout)])
            selected_ref = ref or _latest_stable_ref(checkout)
            run_argv(["git", "checkout", "--detach", selected_ref], cwd=checkout)
            commit = run_argv(["git", "rev-parse", "HEAD"], cwd=checkout)
            if subdir is not None:
                relative = Path(subdir)
                if relative.is_absolute() or ".." in relative.parts:
                    raise SelcError(
                        "UNSAFE_PATH",
                        "Git suite subdirectory must be relative",
                        "use a safe --subdir",
                    )
                suite_root = checkout / relative
                candidates = (
                    [suite_root / "suite.json"] if (suite_root / "suite.json").is_file() else []
                )
            else:
                candidates = list(checkout.rglob("suite.json"))
                suite_root = candidates[0].parent if len(candidates) == 1 else checkout
            if len(candidates) != 1:
                raise SelcError(
                    "SOURCE_AMBIGUOUS",
                    "Git source must contain exactly one suite.json",
                    "specify a suite subdirectory package",
                )
            stable_parent = Path(tempfile.mkdtemp(prefix="selc-git-suite-"))
            stable = stable_parent / "suite"
            copy_tree(suite_root, stable)
            return stable, {
                "kind": "git",
                "repo": source_text,
                "subdir": suite_root.relative_to(checkout).as_posix(),
                "commit": commit,
                "base": selected_ref,
                "contribution_base": _contribution_base(checkout, commit),
            }
    raise SelcError(
        "INVALID_SOURCE",
        f"unsupported suite source: {source}",
        "provide a suite directory, zip package, or local Git repository",
    )


def resolve_framework_source(
    source: str | Path, ref: str | None, subdir: str | None = None
) -> tuple[Path, dict[str, Any]]:
    """Resolve a runtime-only package or a complete suite with an embedded framework."""

    source_text = str(source)
    local_source = Path(source_text.removeprefix("file://")).expanduser()
    if local_source.is_file() and local_source.suffix == ".zip":
        temporary = Path(tempfile.mkdtemp(prefix="selc-framework-package-"))
        _safe_extract(local_source, temporary)
        return temporary, {
            "kind": "framework_package",
            "repo": str(local_source.resolve()),
            "subdir": ".",
            "commit": None,
            "base": None,
        }
    if local_source.is_dir() and framework_path(local_source).is_file():
        return local_source, {
            "kind": "framework_directory",
            "repo": str(local_source.resolve()),
            "subdir": ".",
            "commit": None,
            "base": None,
        }
    if local_source.is_dir() and (local_source / "suite.json").is_file() and subdir is None:
        return local_source, {
            "kind": "framework_suite",
            "repo": str(local_source.resolve()),
            "subdir": ".",
            "commit": None,
            "base": None,
        }
    is_git_source = _is_git_source(source_text, local_source)
    if not is_git_source:
        raise SelcError(
            "INVALID_FRAMEWORK",
            f"unsupported framework source: {source}",
            "provide a framework package, suite, or Git repository",
        )
    with temporary_directory("selc-framework-source-") as temporary:
        checkout = temporary / "checkout"
        run_argv(["git", "clone", "--no-checkout", source_text, str(checkout)])
        selected_ref = ref or _latest_stable_ref(checkout)
        run_argv(["git", "checkout", "--detach", selected_ref], cwd=checkout)
        commit = run_argv(["git", "rev-parse", "HEAD"], cwd=checkout)
        if subdir is not None:
            relative = Path(subdir)
            if relative.is_absolute() or ".." in relative.parts:
                raise SelcError(
                    "UNSAFE_PATH",
                    "framework subdirectory must be relative",
                    "use a safe --framework-subdir",
                )
            candidates = [checkout / relative]
        else:
            candidates = [
                candidate.parent
                for candidate in checkout.rglob("framework.json")
                if ".git" not in candidate.parts
            ]
            if not candidates:
                candidates = [
                    candidate.parent
                    for candidate in checkout.rglob("suite.json")
                    if ".git" not in candidate.parts
                ]
        if len(candidates) != 1 or not candidates[0].is_dir():
            raise SelcError(
                "SOURCE_AMBIGUOUS",
                "framework Git source must contain exactly one framework or suite",
                "specify a framework subdirectory",
            )
        framework_root = candidates[0]
        stable_parent = Path(tempfile.mkdtemp(prefix="selc-git-framework-"))
        stable = stable_parent / "framework"
        descriptor = framework_path(framework_root)
        if descriptor.is_file():
            stable.mkdir()
            shutil.copy2(descriptor, framework_path(stable), follow_symlinks=False)
            shutil.copytree(framework_root / "runtime", stable / "runtime", symlinks=True)
        else:
            copy_portable_suite(framework_root, stable)
        return stable, {
            "kind": "framework_git",
            "repo": source_text,
            "subdir": framework_root.relative_to(checkout).as_posix(),
            "commit": commit,
            "base": selected_ref,
        }


def _is_git_source(source_text: str, local_source: Path) -> bool:
    if not local_source.exists():
        return True
    if source_text.startswith(("file://", "git@", "ssh://", "https://", "http://")):
        return True
    return local_source.is_dir() and (
        (local_source / ".git").exists()
        or ((local_source / "HEAD").is_file() and (local_source / "objects").is_dir())
    )


def _latest_stable_ref(repository: Path) -> str:
    tags = run_argv(["git", "tag", "--sort=-v:refname"], cwd=repository).splitlines()
    stable = [tag for tag in tags if "-" not in tag]
    if not stable:
        raise SelcError(
            "MISSING_STABLE_RELEASE",
            "source has no stable tag",
            "create or specify an explicit stable ref",
        )
    return stable[0]


def _contribution_base(repository: Path, fallback_commit: str) -> str:
    """Keep a writable branch distinct from the stable tag/ref selected for install."""

    code, output, _ = run_argv_result(
        ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], cwd=repository
    )
    if code == 0 and output.startswith("origin/"):
        return output.removeprefix("origin/")
    branches = run_argv(
        ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads"], cwd=repository
    )
    names = [name for name in branches.splitlines() if name]
    for preferred in ("main", "master"):
        if preferred in names:
            return preferred
    return names[0] if len(names) == 1 else fallback_commit


def _assert_not_locally_edited(current: Path, expected_digest: str) -> None:
    if current.exists() and tree_hash(current) != expected_digest:
        raise SelcError(
            "LOCAL_EDIT_CONFLICT",
            "installed suite has local edits",
            "preserve or remove the local edits before updating",
        )


def _harness_base(project_root: Path, harness: str) -> Path:
    if harness == "codex":
        return project_root / ".agents" / "skills"
    if harness == "workbuddy":
        return project_root / ".codebuddy" / "skills"
    raise SelcError(
        "UNSUPPORTED_HARNESS", f"unsupported harness: {harness}", "choose codex or workbuddy"
    )


def _manifest_skill_names(manifest: dict[str, Any]) -> list[str]:
    return sorted(str(skill["name"]) for skill in manifest["skills"])


def _registered_names(installation: dict[str, Any] | None, harness: str) -> list[str]:
    if not installation:
        return []
    registrations = installation.get("registrations")
    if isinstance(registrations, dict) and isinstance(registrations.get(harness), list):
        return sorted(str(name) for name in registrations[harness])
    return []


def _is_owned_link(link: Path, root: Path) -> bool:
    """Only replace a symlink whose resolved target stays in this suite's contents."""

    if not link.is_symlink():
        return False
    try:
        link.resolve().relative_to((root / "contents").resolve())
    except ValueError:
        return False
    return True


def _assert_registration_available(
    project_root: Path,
    root: Path,
    manifest: dict[str, Any],
    harnesses: list[str],
    previous: dict[str, Any] | None,
) -> None:
    desired_names = set(_manifest_skill_names(manifest))
    for harness in harnesses:
        base = _harness_base(project_root, harness)
        existing_names = set(_registered_names(previous, harness))
        for skill_name in existing_names | desired_names:
            link = base / skill_name
            if not (link.exists() or link.is_symlink()):
                continue
            if _is_owned_link(link, root):
                continue
            raise SelcError(
                "NAME_CONFLICT",
                f"skill registration already exists: {link}",
                "remove or rename the conflicting skill",
            )


def _sync_harness_skills(
    project_root: Path,
    root: Path,
    manifest: dict[str, Any],
    harness: str,
    previous_names: list[str],
) -> list[str]:
    """Make one harness point to `current`, removing only links owned by this suite."""

    base = _harness_base(project_root, harness)
    base.mkdir(parents=True, exist_ok=True)
    paths = {str(skill["name"]): str(skill["path"]) for skill in manifest["skills"]}
    desired_names = set(paths)
    for skill_name in set(previous_names) - desired_names:
        link = base / skill_name
        if link.exists() or link.is_symlink():
            if not _is_owned_link(link, root):
                raise SelcError(
                    "NAME_CONFLICT",
                    f"old skill registration is no longer owned by this suite: {link}",
                    "resolve the conflicting registration before updating",
                )
            link.unlink()
    for skill_name, relative_path in paths.items():
        link = base / skill_name
        target = root / "current" / relative_path
        expected = os.path.relpath(target, start=base)
        if link.exists() or link.is_symlink():
            if not _is_owned_link(link, root):
                raise SelcError(
                    "NAME_CONFLICT",
                    f"skill registration already exists: {link}",
                    "remove or rename the conflicting skill",
                )
            if os.readlink(link) == expected:
                continue
            link.unlink()
        link.symlink_to(expected, target_is_directory=True)
    return sorted(desired_names)


def _sync_all_harnesses(
    project_root: Path,
    root: Path,
    manifest: dict[str, Any],
    harnesses: list[str],
    previous: dict[str, Any] | None,
) -> dict[str, list[str]]:
    return {
        harness: _sync_harness_skills(
            project_root,
            root,
            manifest,
            harness,
            _registered_names(previous, harness),
        )
        for harness in harnesses
    }


def _saved_external_framework_selection(project_root: Path, suite_id: str) -> dict[str, Any] | None:
    """Read a prior explicit framework choice before assembling a repeat install.

    The caller cannot build the right composite from the business package alone:
    an omitted framework argument means "keep the selected external source", not
    "select the business package's embedded runtime".  Read the selection while
    holding the same registry-then-suite lock order used by activation.  The
    activation transaction rechecks this snapshot before making any change.
    """

    root = install_root(project_root) / suite_id
    journal = root / "journal.json"
    with ExitStack() as locks:
        locks.enter_context(exclusive_lock(registry_lock_path(project_root)))
        locks.enter_context(exclusive_lock(root / ".lock"))
        if journal.exists():
            raise SelcError(
                "PENDING_RECOVERY",
                "an interrupted installation transaction must be recovered first",
                "run updates recover for this suite before installing or updating",
            )
        installation = _registry(project_root)["installations"].get(suite_id)
        if not isinstance(installation, dict):
            return None
        framework = installation.get("framework_source")
        if not isinstance(framework, dict) or framework.get("status") != "active_external":
            return None
        repository = framework.get("repo")
        if not isinstance(repository, str) or not repository:
            raise SelcError(
                "MISSING_SOURCE_BASELINE",
                "installed external framework has no reusable source",
                "select a framework source explicitly before repeating the install",
            )
        return dict(framework)


def _active_external_framework_selection(
    installation: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Return the external selection represented by an installed suite, if any."""

    if installation is None:
        return None
    framework = installation.get("framework_source")
    if not isinstance(framework, dict) or framework.get("status") != "active_external":
        return None
    return framework


def business_selection_snapshot(installation: dict[str, Any]) -> dict[str, Any]:
    """Capture the business identity that a framework-only update must retain."""

    suite_id = installation.get("suite_id")
    source = installation.get("source")
    version = installation.get("version")
    business_digest = installation.get("business_digest")
    if (
        not isinstance(suite_id, str)
        or not isinstance(source, dict)
        or not isinstance(version, str)
        or not isinstance(business_digest, str)
    ):
        raise SelcError(
            "MISSING_SOURCE_BASELINE",
            "installed suite has no complete reusable business source",
            "supply --source for this update",
        )
    return {
        "suite_id": suite_id,
        "source": dict(source),
        "version": version,
        "business_digest": business_digest,
    }


def _business_candidate_matches_snapshot(
    manifest: dict[str, Any],
    source_info: dict[str, Any],
    business_digest: str,
    snapshot: dict[str, Any],
) -> bool:
    expected_source = snapshot["source"]
    return (
        manifest["suite_id"] == snapshot["suite_id"]
        and manifest["version"] == snapshot["version"]
        and business_digest == snapshot["business_digest"]
        and source_info.get("kind") == expected_source.get("kind")
        and source_info.get("repo") == expected_source.get("repo")
        and source_info.get("subdir") == expected_source.get("subdir")
        and source_info.get("commit") == expected_source.get("commit")
    )


def _installed_business_matches_snapshot(
    installation: dict[str, Any] | None,
    snapshot: dict[str, Any],
) -> bool:
    if installation is None:
        return False
    return (
        installation.get("suite_id") == snapshot["suite_id"]
        and installation.get("source") == snapshot["source"]
        and installation.get("version") == snapshot["version"]
        and installation.get("business_digest") == snapshot["business_digest"]
    )


def _restore_business_source_baseline(
    source_info: dict[str, Any], snapshot: dict[str, Any]
) -> dict[str, Any]:
    """Keep the saved stable ref after resolving its pinned commit."""

    restored = dict(source_info)
    expected_source = snapshot["source"]
    restored["base"] = expected_source.get("base")
    if "contribution_base" in expected_source:
        restored["contribution_base"] = expected_source["contribution_base"]
    return restored


def _snapshot_business_source(source_root: Path, destination: Path) -> tuple[dict[str, Any], str]:
    """Copy and validate the portable business bytes used by one install call.

    Directory sources are mutable.  Metadata taken from a live directory cannot
    safely describe files copied later from that directory, so every install
    derives its manifest and business digest from this private copy instead.
    ``copy_portable_suite`` also limits the snapshot to the same portable
    surface that installation eventually activates.
    """

    copy_portable_suite(source_root, destination)
    manifest = load_manifest(destination)
    return manifest, portable_tree_hash(destination)


def _snapshot_framework_source(source_root: Path, destination: Path) -> dict[str, Any]:
    """Copy and validate framework descriptor/runtime bytes for one install call.

    A runtime-only framework has just ``framework.json`` and ``runtime``;
    a complete suite can also serve as a framework source.  Copy the complete
    portable surface for the latter so its embedded descriptor is derived from
    the same stable bytes.  Validation occurs after copying, which rejects a
    partially changed descriptor/runtime pair instead of resealing it.
    """

    descriptor = framework_path(source_root)
    if descriptor.is_file():
        destination.mkdir(parents=True)
        shutil.copy2(descriptor, framework_path(destination), follow_symlinks=False)
        shutil.copytree(source_root / "runtime", destination / "runtime", symlinks=True)
    else:
        copy_portable_suite(source_root, destination)
    return load_framework(destination)


def _framework_matches_selection(framework_info: dict[str, Any], selection: dict[str, Any]) -> bool:
    """Check that an implicit source still represents the saved selection."""

    return (
        framework_info.get("kind") == selection.get("kind")
        and framework_info.get("repo") == selection.get("repo")
        and framework_info.get("subdir") == selection.get("subdir")
        and framework_info.get("commit") == selection.get("commit")
        and framework_info.get("version") == selection.get("version")
        and framework_info.get("runtime_protocol") == selection.get("runtime_protocol")
        and framework_info.get("digest") == selection.get("digest")
    )


def install_suite(
    *,
    source: str | Path,
    project_root: Path,
    harness: str,
    ref: str | None,
    subdir: str | None = None,
    framework_source: str | Path | None = None,
    framework_ref: str | None = None,
    framework_subdir: str | None = None,
    prepared_business_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    business_source_root, source_info = resolve_source(source, ref, subdir)
    with temporary_directory("selc-install-input-") as temporary:
        business_root = temporary / "business"
        manifest, business_digest = _snapshot_business_source(business_source_root, business_root)
        if prepared_business_snapshot is not None:
            if not _business_candidate_matches_snapshot(
                manifest, source_info, business_digest, prepared_business_snapshot
            ):
                raise SelcError(
                    "STALE_BUSINESS_SELECTION",
                    "the saved business source no longer matches the installed business",
                    "retry with an explicit --source after reviewing the business update",
                )
            source_info = _restore_business_source_baseline(source_info, prepared_business_snapshot)
        implicit_framework_selection_observed = framework_source is None
        preserved_external_selection: dict[str, Any] | None = None
        if implicit_framework_selection_observed:
            preserved_external_selection = _saved_external_framework_selection(
                project_root, manifest["suite_id"]
            )
        if framework_source is None and preserved_external_selection is None:
            framework_info = {
                "kind": "embedded",
                "version": manifest["framework_version"],
                "runtime_protocol": manifest.get("runtime_protocol", 1),
                "digest": tree_hash(business_root / "runtime"),
                "status": "active_embedded",
            }
            return _install_materialized_suite(
                suite_root=business_root,
                source_info=source_info,
                framework_info=framework_info,
                business_digest=business_digest,
                project_root=project_root,
                harness=harness,
                preserved_external_selection=preserved_external_selection,
                implicit_framework_selection_observed=implicit_framework_selection_observed,
                prepared_business_snapshot=prepared_business_snapshot,
            )

        if preserved_external_selection is not None:
            framework_source = str(preserved_external_selection["repo"])
            framework_ref = preserved_external_selection.get(
                "commit"
            ) or preserved_external_selection.get("base")
            framework_subdir = preserved_external_selection.get("subdir")

        framework_source_root, framework_source_info = resolve_framework_source(
            framework_source, framework_ref, framework_subdir
        )
        framework_root = temporary / "framework"
        framework = _snapshot_framework_source(framework_source_root, framework_root)
        framework_info = framework_source_info | {
            "version": framework["framework_version"],
            "runtime_protocol": framework["runtime_protocol"],
            "digest": framework["runtime_hash"],
            "status": "active_external",
        }
        if preserved_external_selection is not None:
            framework_info["base"] = preserved_external_selection.get("base")
            if not _framework_matches_selection(framework_info, preserved_external_selection):
                raise SelcError(
                    "STALE_FRAMEWORK_SELECTION",
                    "the saved external framework source changed before this install",
                    "select the changed framework explicitly to apply its new runtime",
                )
        with temporary_directory("selc-composite-") as composite_temporary:
            composite = composite_temporary / "suite"
            materialize_composite_suite(business_root, framework_root, composite)
            return _install_materialized_suite(
                suite_root=composite,
                source_info=source_info,
                framework_info=framework_info,
                business_digest=business_digest,
                project_root=project_root,
                harness=harness,
                preserved_external_selection=preserved_external_selection,
                implicit_framework_selection_observed=implicit_framework_selection_observed,
                prepared_business_snapshot=prepared_business_snapshot,
            )


def _install_materialized_suite(
    *,
    suite_root: Path,
    source_info: dict[str, Any],
    framework_info: dict[str, Any],
    business_digest: str,
    project_root: Path,
    harness: str,
    preserved_external_selection: dict[str, Any] | None = None,
    implicit_framework_selection_observed: bool = False,
    prepared_business_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    manifest = load_manifest(suite_root)
    digest = portable_tree_hash(suite_root)
    suite_id = manifest["suite_id"]
    root = install_root(project_root) / suite_id
    contents = root / "contents" / digest
    current = root / "current"
    journal = root / "journal.json"
    with ExitStack() as locks:
        locks.enter_context(exclusive_lock(registry_lock_path(project_root)))
        locks.enter_context(exclusive_lock(root / ".lock"))
        if journal.exists():
            raise SelcError(
                "PENDING_RECOVERY",
                "an interrupted installation transaction must be recovered first",
                "run updates recover for this suite before installing or updating",
            )
        registry = _registry(project_root)
        existing = registry["installations"].get(suite_id)
        database = open_database(project_root)
        leases = active_leases(database, suite_id)
        database.close()
        if prepared_business_snapshot is not None and not _installed_business_matches_snapshot(
            existing, prepared_business_snapshot
        ):
            raise SelcError(
                "STALE_BUSINESS_SELECTION",
                "the installed business changed while the framework-only update was prepared",
                "retry the framework update against the current business version",
            )
        if existing:
            current_external_selection = _active_external_framework_selection(existing)
            if (
                implicit_framework_selection_observed
                and current_external_selection != preserved_external_selection
            ):
                raise SelcError(
                    "STALE_FRAMEWORK_SELECTION",
                    "the framework selection changed while the install was prepared",
                    "retry the install to use the current framework selection",
                )
            _assert_not_locally_edited(current, existing["digest"])
            if existing["source"]["repo"] != source_info["repo"]:
                raise SelcError(
                    "NAME_CONFLICT",
                    f"suite id already belongs to another source: {suite_id}",
                    "use the saved source or install under a different suite id",
                )
            if existing["digest"] == digest and existing["source"]["repo"] == source_info["repo"]:
                selection_changed = (
                    existing.get("source") != source_info
                    or existing.get("framework_source") != framework_info
                    or existing.get("business_digest") != business_digest
                )
                if selection_changed and (leases["tasks"] or leases["calls"] or leases["sessions"]):
                    raise SelcError(
                        "ACTIVE_LEASE",
                        "active tasks, calls, or sessions prevent changing the suite source selection",
                        "close them explicitly and retry",
                    )
                harnesses = sorted(set(existing.get("harnesses", []) + [harness]))
                _assert_registration_available(project_root, root, manifest, harnesses, existing)
                new_installation = dict(existing)
                new_installation["version"] = manifest["version"]
                new_installation["framework_version"] = manifest["framework_version"]
                new_installation["framework_source"] = framework_info
                new_installation["business_digest"] = business_digest
                new_installation["source"] = source_info
                new_installation["harnesses"] = harnesses
                new_installation["registrations"] = {
                    harness_name: _manifest_skill_names(manifest) for harness_name in harnesses
                }
                write_json(
                    journal,
                    {
                        "state": "activating",
                        "new_digest": digest,
                        "old_digest": existing["digest"],
                        "old_installation": existing,
                        "new_installation": new_installation,
                    },
                )
                registrations = _sync_all_harnesses(
                    project_root, root, manifest, harnesses, existing
                )
                new_installation["registrations"] = registrations
                journal_record = read_json(journal)
                journal_record["new_installation"] = new_installation
                write_json(journal, journal_record)
                registry["installations"][suite_id] = new_installation
                _write_registry(project_root, registry)
                _assert_installation_state(project_root, root, suite_id, new_installation)
                journal.unlink()
                return {
                    "suite_id": suite_id,
                    "version": manifest["version"],
                    "digest": digest,
                    "reused": True,
                    "registered_skills": registrations[harness],
                    "maintenance": str(current / "MAINTENANCE.md"),
                    "source": source_info,
                    "framework": new_installation["framework_source"],
                }
            if leases["tasks"] or leases["calls"] or leases["sessions"]:
                raise SelcError(
                    "ACTIVE_LEASE",
                    "active tasks, calls, or sessions prevent suite replacement",
                    "close them explicitly and retry",
                )
        harnesses = sorted(set((existing or {}).get("harnesses", []) + [harness]))
        _assert_registration_available(project_root, root, manifest, harnesses, existing)
        root.mkdir(parents=True, exist_ok=True)
        new_installation = {
            "suite_id": suite_id,
            "version": manifest["version"],
            "framework_version": manifest["framework_version"],
            "framework_source": framework_info,
            "digest": digest,
            "business_digest": business_digest,
            "source": source_info,
            "harnesses": harnesses,
            "registrations": {
                harness_name: _manifest_skill_names(manifest) for harness_name in harnesses
            },
            "state": "active",
        }
        write_json(
            journal,
            {
                "state": "prepared",
                "new_digest": digest,
                "old_digest": existing["digest"] if existing else None,
                "old_installation": existing,
                "new_installation": new_installation,
            },
        )
        if not contents.exists():
            temporary_contents = root / f".contents-{digest}.tmp"
            if temporary_contents.exists():
                shutil.rmtree(temporary_contents)
            copy_portable_suite(suite_root, temporary_contents)
            contents.parent.mkdir(parents=True, exist_ok=True)
            os.replace(temporary_contents, contents)
        temporary_current = root / ".current.tmp"
        temporary_current.unlink(missing_ok=True)
        temporary_current.symlink_to(Path("contents") / digest, target_is_directory=True)
        write_json(
            journal,
            {
                "state": "activating",
                "new_digest": digest,
                "old_digest": existing["digest"] if existing else None,
                "old_installation": existing,
                "new_installation": new_installation,
            },
        )
        os.replace(temporary_current, current)
        registrations = _sync_all_harnesses(project_root, root, manifest, harnesses, existing)
        new_installation["registrations"] = registrations
        journal_record = read_json(journal)
        journal_record["new_installation"] = new_installation
        write_json(journal, journal_record)
        registry["installations"][suite_id] = new_installation
        _write_registry(project_root, registry)
        _assert_installation_state(project_root, root, suite_id, new_installation)
        journal.unlink(missing_ok=True)
        if existing and existing["digest"] != digest:
            old_contents = root / "contents" / existing["digest"]
            shutil.rmtree(old_contents, ignore_errors=True)
    return {
        "suite_id": suite_id,
        "version": manifest["version"],
        "digest": digest,
        "reused": False,
        "registered_skills": registrations[harness],
        "maintenance": str(current / "MAINTENANCE.md"),
        "source": source_info,
        "framework": framework_info,
    }


def current_installation(project_root: Path, suite_id: str) -> dict[str, Any]:
    _assert_no_pending_recovery(project_root, suite_id)
    installation = _registry(project_root)["installations"].get(suite_id)
    if installation is None:
        raise SelcError(
            "MISSING_INSTALLATION", f"suite is not installed: {suite_id}", "install the suite first"
        )
    return installation


def _assert_no_pending_recovery(project_root: Path, suite_id: str) -> None:
    journal = install_root(project_root) / suite_id / "journal.json"
    if journal.exists():
        raise SelcError(
            "PENDING_RECOVERY",
            "suite installation has an unfinished transaction",
            "run updates recover before starting tasks, calls, or updates",
        )


def _assert_installation_state(
    project_root: Path,
    root: Path,
    suite_id: str,
    installation: dict[str, Any],
) -> None:
    current = root / "current"
    expected = root / "contents" / str(installation["digest"])
    if not current.is_symlink() or current.resolve() != expected.resolve():
        raise SelcError(
            "RECOVERY_INCOMPLETE",
            "installation current pointer does not match the transaction digest",
            "run updates recover and inspect the journal",
        )
    if not expected.is_dir() or portable_tree_hash(expected) != installation["digest"]:
        raise SelcError(
            "RECOVERY_INCOMPLETE",
            "installation contents do not match the transaction digest",
            "run updates recover and restore the package contents",
        )
    manifest = load_manifest(expected)
    skill_paths = {str(skill["name"]): str(skill["path"]) for skill in manifest["skills"]}
    registry = _registry(project_root)
    if registry["installations"].get(suite_id) != installation:
        raise SelcError(
            "RECOVERY_INCOMPLETE",
            "shared installation registry does not match the active contents",
            "run updates recover before using the suite",
        )
    registrations = installation.get("registrations", {})
    for harness, skills in registrations.items():
        base = _harness_base(project_root, harness)
        for skill_name in skills:
            link = base / str(skill_name)
            relative_path = skill_paths.get(str(skill_name))
            if relative_path is None:
                raise SelcError(
                    "RECOVERY_INCOMPLETE",
                    f"registry lists an undeclared skill registration: {skill_name}",
                    "run updates recover and inspect the registration metadata",
                )
            expected_target = os.path.relpath(root / "current" / relative_path, start=base)
            if not link.is_symlink() or os.readlink(link) != expected_target:
                raise SelcError(
                    "RECOVERY_INCOMPLETE",
                    f"harness registration does not match the active suite: {link}",
                    "run updates recover before using the suite",
                )


def record_daily_update_check(project_root: Path, suite_id: str) -> dict[str, Any]:
    """Read configured upstreams once a day without blocking the business task on failure."""

    installation = current_installation(project_root, suite_id)
    cache_path = project_root / ".selc" / "update-cache.json"
    cache = read_json(cache_path) if cache_path.exists() else {"schema_version": 1, "suites": {}}
    today = time.strftime("%Y-%m-%d", time.gmtime())
    previous = cache["suites"].get(suite_id)
    if previous and previous.get("date") == today:
        return {
            "checked": False,
            "cached": True,
            "date": today,
            "result": previous["result"],
        }
    result = {
        "checked_at": utc_timestamp(),
        "business": _check_saved_business(installation),
        "framework": _check_saved_framework(installation),
    }
    cache["suites"][suite_id] = {"date": today, "result": result}
    write_json(cache_path, cache)
    return {"checked": True, "cached": False, "date": today, "result": result}


def utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _check_saved_business(installation: dict[str, Any]) -> dict[str, Any]:
    source = installation["source"]
    repository = source.get("repo")
    if not repository:
        return {"status": "unconfigured"}
    try:
        is_git = source.get("kind") == "git"
        ref = None if is_git else source.get("base")
        subdir = source.get("subdir") if is_git else None
        checked = update_check(repository, ref, subdir)
    except SelcError as error:
        return {"status": "unknown", "error": error.code}
    current_digest = installation.get("business_digest", installation["digest"])
    return {
        "status": "checked",
        "current_version": installation["version"],
        "upstream_version": checked["version"],
        "update_available": checked["digest"] != current_digest,
    }


def _check_saved_framework(installation: dict[str, Any]) -> dict[str, Any]:
    framework = installation["framework_source"]
    if framework.get("kind") == "embedded":
        return {"status": "embedded_with_business", "update_available": False}
    repository = framework.get("repo")
    if not repository:
        return {"status": "unconfigured"}
    try:
        is_git = framework.get("kind") == "framework_git"
        ref = None if is_git else framework.get("base")
        subdir = framework.get("subdir") if is_git else None
        checked = framework_update_check(repository, ref, subdir)
    except SelcError as error:
        return {"status": "unknown", "error": error.code}
    return {
        "status": "checked",
        "current_version": framework["version"],
        "upstream_version": checked["framework_version"],
        "update_available": checked["runtime_hash"] != framework["digest"],
    }


def verify_expected_version(
    project_root: Path,
    suite_id: str,
    expected_version: str,
    expected_hash: str | None = None,
) -> None:
    installation = current_installation(project_root, suite_id)
    if installation["version"] != expected_version:
        raise SelcError(
            "STALE_RUNTIME",
            f"expected suite version {expected_version}, found {installation['version']}",
            "reload the skill entry and retry with the current version",
        )
    if expected_hash is not None and installation["digest"] != expected_hash:
        raise SelcError(
            "STALE_RUNTIME",
            "expected suite content hash does not match the installed suite",
            "reload the skill entry and retry with the current hash",
        )


def update_check(source: str | Path, ref: str | None, subdir: str | None = None) -> dict[str, Any]:
    suite_root, source_info = resolve_source(source, ref, subdir)
    manifest = load_manifest(suite_root)
    return {
        "checked": True,
        "suite_id": manifest["suite_id"],
        "version": manifest["version"],
        "digest": portable_tree_hash(suite_root),
        "source": source_info,
        "applied": False,
    }


def framework_update_check(
    source: str | Path, ref: str | None, subdir: str | None = None
) -> dict[str, Any]:
    framework_root, source_info = resolve_framework_source(source, ref, subdir)
    framework = load_framework(framework_root)
    return {
        "checked": True,
        "framework_version": framework["framework_version"],
        "runtime_protocol": framework["runtime_protocol"],
        "runtime_hash": framework["runtime_hash"],
        "source": source_info,
        "applied": False,
    }


def recover_installation(project_root: Path, suite_id: str) -> dict[str, Any]:
    root = install_root(project_root) / suite_id
    journal = root / "journal.json"
    with ExitStack() as locks:
        locks.enter_context(exclusive_lock(registry_lock_path(project_root)))
        locks.enter_context(exclusive_lock(root / ".lock"))
        if not journal.exists():
            return {"recovered": False, "state": "no_journal"}
        record = read_json(journal)
        new_installation = record.get("new_installation")
        if not isinstance(new_installation, dict):
            registry = _registry(project_root)
            candidate = registry["installations"].get(suite_id)
            if isinstance(candidate, dict) and candidate.get("digest") == record.get("new_digest"):
                try:
                    _assert_installation_state(project_root, root, suite_id, candidate)
                except SelcError:
                    return {"recovered": False, "state": "needs_recovery", "journal": record}
                journal.unlink()
                return {
                    "recovered": True,
                    "state": "verified_legacy_active",
                    "digest": candidate["digest"],
                }
        if not isinstance(new_installation, dict):
            return {"recovered": False, "state": "needs_recovery", "journal": record}
        new_digest = str(record.get("new_digest", ""))
        new_contents = root / "contents" / new_digest
        try:
            if new_contents.is_dir() and portable_tree_hash(new_contents) == new_digest:
                manifest = load_manifest(new_contents)
                harnesses = [str(name) for name in new_installation.get("harnesses", [])]
                previous = record.get("old_installation")
                old_installation = previous if isinstance(previous, dict) else None
                _assert_registration_available(
                    project_root, root, manifest, harnesses, old_installation
                )
                current = root / "current"
                temporary_current = root / ".current.recover.tmp"
                temporary_current.unlink(missing_ok=True)
                temporary_current.symlink_to(
                    Path("contents") / new_digest, target_is_directory=True
                )
                os.replace(temporary_current, current)
                registrations = _sync_all_harnesses(
                    project_root, root, manifest, harnesses, old_installation
                )
                new_installation["registrations"] = registrations
                registry = _registry(project_root)
                registry["installations"][suite_id] = new_installation
                _write_registry(project_root, registry)
                _assert_installation_state(project_root, root, suite_id, new_installation)
                journal.unlink()
                old_digest = record.get("old_digest")
                if old_digest and old_digest != new_digest:
                    shutil.rmtree(root / "contents" / str(old_digest), ignore_errors=True)
                return {"recovered": True, "state": "completed", "digest": new_digest}
        except SelcError:
            pass

        old_installation = record.get("old_installation")
        old_digest = record.get("old_digest")
        if isinstance(old_installation, dict) and old_digest:
            old_contents = root / "contents" / str(old_digest)
            try:
                if old_contents.is_dir() and portable_tree_hash(old_contents) == str(old_digest):
                    old_manifest = load_manifest(old_contents)
                    harnesses = [str(name) for name in old_installation.get("harnesses", [])]
                    _assert_registration_available(
                        project_root, root, old_manifest, harnesses, new_installation
                    )
                    temporary_current = root / ".current.rollback.tmp"
                    temporary_current.unlink(missing_ok=True)
                    temporary_current.symlink_to(
                        Path("contents") / str(old_digest), target_is_directory=True
                    )
                    os.replace(temporary_current, root / "current")
                    registrations = _sync_all_harnesses(
                        project_root, root, old_manifest, harnesses, new_installation
                    )
                    old_installation["registrations"] = registrations
                    registry = _registry(project_root)
                    registry["installations"][suite_id] = old_installation
                    _write_registry(project_root, registry)
                    _assert_installation_state(project_root, root, suite_id, old_installation)
                    journal.unlink()
                    return {"recovered": True, "state": "rolled_back", "digest": old_digest}
            except SelcError:
                pass
    return {"recovered": False, "state": "needs_recovery", "journal": record}
