"""Focused installation, portable provenance, protocol, and checker contracts."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from selc import records
from selc.errors import SelcError
from selc.evolve import (
    _remote_release_state,
    confirm_release,
    evaluate_candidate,
    prepare_candidate,
    prepare_contribution,
    prepare_release,
    release_local,
    verify_local_release,
)
from selc.install import (
    current_installation,
    framework_update_check,
    install_suite,
    recover_installation,
    update_check,
)
from selc.manifest import (
    create_suite,
    load_manifest,
    package_suite,
    portable_tree_hash,
    render_text_report,
    seal_suite,
    suite_content_hash,
)
from selc.util import exclusive_lock, file_hash, tree_hash


def git(arguments: list[str], cwd: Path) -> str:
    completed = subprocess.run(
        ["git", *arguments], cwd=cwd, capture_output=True, check=True, text=True
    )
    return completed.stdout.strip()


class InstallationContractsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def create_suite(self, suite_id: str, skills: list[str]) -> Path:
        suite = self.root / suite_id
        create_suite(suite_id, "1.0.0", suite, skills)
        return suite

    def cli(self, *arguments: str, expected: int = 0) -> dict[str, object]:
        completed = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "selc.py"), *arguments],
            cwd=REPO_ROOT,
            capture_output=True,
            check=False,
            text=True,
        )
        self.assertEqual(completed.returncode, expected, completed.stdout + completed.stderr)
        return json.loads(completed.stdout)

    def cli_in_process(self, *arguments: str) -> tuple[int, dict[str, object]]:
        """Invoke the public argparse path while an installation seam is controlled."""

        import selc.cli as cli_module

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = cli_module.main(list(arguments))
        return exit_code, json.loads(output.getvalue())

    def copy_external_framework(self, suite: Path, name: str = "external-framework") -> Path:
        """Build a runtime-only source with exactly the suite's initial framework bytes."""

        framework = self.root / name
        framework.mkdir()
        shutil.copytree(suite / "runtime", framework / "runtime")
        shutil.copy2(suite / "framework.json", framework / "framework.json")
        return framework

    def change_external_framework(self, framework: Path) -> None:
        """Make an external runtime distinguishable from the business embedded runtime."""

        runtime_init = framework / "runtime" / "selc" / "__init__.py"
        runtime_init.write_text(
            runtime_init.read_text(encoding="utf-8") + "\nEXTERNAL_RUNTIME_MARKER = True\n",
            encoding="utf-8",
        )
        descriptor_path = framework / "framework.json"
        descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
        descriptor["framework_version"] = "external-0.1.0"
        descriptor["runtime_hash"] = tree_hash(framework / "runtime")
        descriptor_path.write_text(json.dumps(descriptor), encoding="utf-8")

    def change_embedded_framework(self, suite: Path) -> None:
        """Make a later business package carry a detectably different embedded runtime."""

        manifest_path = suite / "suite.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["version"] = "1.1.0"
        manifest["framework_version"] = "0.2.0"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        runtime_init = suite / "runtime" / "selc" / "__init__.py"
        runtime_init.write_text(
            runtime_init.read_text(encoding="utf-8")
            + "\nBUSINESS_EMBEDDED_RUNTIME_MARKER = True\n",
            encoding="utf-8",
        )
        descriptor_path = suite / "framework.json"
        descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
        descriptor["framework_version"] = "0.2.0"
        descriptor["runtime_hash"] = tree_hash(suite / "runtime")
        descriptor_path.write_text(json.dumps(descriptor), encoding="utf-8")
        seal_suite(suite)

    def test_same_digest_external_framework_selection_is_persisted_and_preserved(self) -> None:
        suite = self.create_suite("selection-suite", ["text-report"])
        framework = self.copy_external_framework(suite)
        project = self.root / "project"
        initial = install_suite(source=suite, project_root=project, harness="codex", ref=None)

        selected = self.cli(
            "updates",
            "apply",
            "--suite-id",
            "selection-suite",
            "--framework-source",
            str(framework),
            "--project-root",
            str(project),
            "--harness",
            "codex",
        )
        self.assertTrue(selected["installation"]["reused"])
        selected_installation = current_installation(project, "selection-suite")
        self.assertEqual(selected_installation["framework_source"]["status"], "active_external")
        self.assertEqual(
            selected_installation["framework_source"]["repo"], str(framework.resolve())
        )
        self.assertEqual(selected_installation["source"]["subdir"], ".")
        self.assertEqual(selected_installation["digest"], initial["digest"])

        second_harness = install_suite(
            source=suite,
            project_root=project,
            harness="workbuddy",
            ref=None,
        )
        self.assertTrue(second_harness["reused"])
        registered = current_installation(project, "selection-suite")
        self.assertEqual(registered["harnesses"], ["codex", "workbuddy"])
        self.assertEqual(registered["framework_source"]["status"], "active_external")

        self.change_embedded_framework(suite)
        updated = self.cli(
            "updates",
            "apply",
            "--source",
            str(suite),
            "--suite-id",
            "selection-suite",
            "--project-root",
            str(project),
            "--harness",
            "codex",
        )
        self.assertFalse(updated["installation"]["reused"])
        current = project / ".selc" / "installs" / "selection-suite" / "current"
        runtime_init = (current / "runtime" / "selc" / "__init__.py").read_text(encoding="utf-8")
        self.assertNotIn("BUSINESS_EMBEDDED_RUNTIME_MARKER", runtime_init)
        preserved = current_installation(project, "selection-suite")
        self.assertEqual(preserved["framework_source"]["status"], "active_external")
        self.assertEqual(preserved["framework_source"]["repo"], str(framework.resolve()))

    def test_implicit_install_preserves_a_distinct_external_runtime(self) -> None:
        suite = self.create_suite("distinct-selection", ["text-report"])
        framework = self.copy_external_framework(suite)
        self.change_external_framework(framework)
        project = self.root / "project"

        initial = install_suite(
            source=suite,
            project_root=project,
            harness="codex",
            ref=None,
            framework_source=framework,
        )
        repeated = install_suite(
            source=suite,
            project_root=project,
            harness="workbuddy",
            ref=None,
        )

        self.assertTrue(repeated["reused"])
        self.assertEqual(repeated["digest"], initial["digest"])
        current = project / ".selc" / "installs" / "distinct-selection" / "current"
        runtime_init = (current / "runtime" / "selc" / "__init__.py").read_text(encoding="utf-8")
        self.assertIn("EXTERNAL_RUNTIME_MARKER", runtime_init)
        self.assertEqual(tree_hash(current / "runtime"), tree_hash(framework / "runtime"))
        installation = current_installation(project, "distinct-selection")
        self.assertEqual(installation["framework_source"]["status"], "active_external")
        self.assertEqual(installation["harnesses"], ["codex", "workbuddy"])

    def test_skill_path_defaults_are_normalized_without_rewriting_manifest_bytes(self) -> None:
        for kind in ("omitted", "explicit-default", "explicit-custom"):
            with self.subTest(kind=kind):
                suite = self.create_suite(f"path-{kind}", ["text-report"])
                manifest_path = suite / "suite.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                skill = manifest["skills"][0]
                if kind == "omitted":
                    del skill["path"]
                elif kind == "explicit-custom":
                    shutil.move(str(suite / "skills" / "text-report"), suite / "skills" / "custom")
                    skill["path"] = "skills/custom"
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                seal_suite(suite)

                loaded_manifest = load_manifest(suite)
                self.assertEqual(
                    loaded_manifest, json.loads(manifest_path.read_text(encoding="utf-8"))
                )
                package = self.root / f"path-{kind}.zip"
                package_suite(suite, package)

                candidate_parent = self.root / f"path-{kind}-candidate"
                candidate = Path(prepare_candidate(suite, candidate_parent)["candidate"])
                evaluation = self.root / f"path-{kind}-evaluation.json"
                result = evaluate_candidate(
                    candidate,
                    evaluation,
                    max_runs=8,
                    model_calls=0,
                    candidate_number=1,
                    candidate_limit=2,
                    run_timeout_seconds=10,
                    total_timeout_seconds=60,
                )
                self.assertEqual(result["status"], "passed")

                project = self.root / f"path-{kind}-project"
                install_suite(source=package, project_root=project, harness="codex", ref=None)
                install_suite(source=package, project_root=project, harness="workbuddy", ref=None)
                self.assertEqual(
                    recover_installation(project, f"path-{kind}"),
                    {"recovered": False, "state": "no_journal"},
                )
                expected_relative = (
                    "skills/custom" if kind == "explicit-custom" else "skills/text-report"
                )
                link = project / ".agents" / "skills" / "text-report"
                self.assertTrue(link.is_symlink())
                self.assertEqual(
                    link.resolve(),
                    (
                        project
                        / ".selc"
                        / "installs"
                        / f"path-{kind}"
                        / "current"
                        / expected_relative
                    ).resolve(),
                )

    def test_omitted_skill_path_recovers_an_interrupted_dual_harness_registration(self) -> None:
        """Journal recovery uses the same in-memory default path interpretation."""

        suite = self.create_suite("path-omitted-recovery", ["text-report"])
        manifest_path = suite / "suite.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        del manifest["skills"][0]["path"]
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        seal_suite(suite)

        package = self.root / "path-omitted-recovery.zip"
        package_suite(suite, package)
        project = self.root / "path-omitted-recovery-project"
        first = install_suite(source=package, project_root=project, harness="codex", ref=None)

        import selc.install as install_module

        original_write_registry = install_module._write_registry
        writes = 0

        def fail_once(project_root: Path, registry: dict[str, object]) -> None:
            nonlocal writes
            writes += 1
            if writes == 1:
                raise OSError("injected registry interruption")
            original_write_registry(project_root, registry)

        install_module._write_registry = fail_once
        try:
            with self.assertRaises(OSError):
                install_suite(source=package, project_root=project, harness="workbuddy", ref=None)
        finally:
            install_module._write_registry = original_write_registry

        journal = project / ".selc" / "installs" / "path-omitted-recovery" / "journal.json"
        self.assertTrue(journal.is_file())
        recovered = recover_installation(project, "path-omitted-recovery")
        self.assertTrue(recovered["recovered"])
        self.assertFalse(journal.exists())

        active = current_installation(project, "path-omitted-recovery")
        self.assertEqual(active["digest"], first["digest"])
        self.assertEqual(active["harnesses"], ["codex", "workbuddy"])
        self.assertEqual(
            active["registrations"],
            {"codex": ["text-report"], "workbuddy": ["text-report"]},
        )
        expected_skill = (
            project
            / ".selc"
            / "installs"
            / "path-omitted-recovery"
            / "current"
            / "skills"
            / "text-report"
        ).resolve()
        for harness_path in (
            project / ".agents" / "skills" / "text-report",
            project / ".codebuddy" / "skills" / "text-report",
        ):
            self.assertTrue(harness_path.is_symlink())
            self.assertEqual(harness_path.resolve(), expected_skill)

    def test_explicit_framework_uses_a_private_snapshot_for_metadata_and_runtime(self) -> None:
        """A later directory edit cannot split framework metadata from runtime bytes."""

        suite = self.create_suite("framework-input-snapshot", ["text-report"])
        framework = self.copy_external_framework(suite)
        project = self.root / "project"
        original_descriptor = json.loads((framework / "framework.json").read_text(encoding="utf-8"))
        original_runtime_hash = tree_hash(framework / "runtime")

        import selc.install as install_module

        original_materialize = install_module.materialize_composite_suite
        changed = False

        def change_after_snapshot(
            business_root: Path, framework_root: Path, destination: Path
        ) -> dict[str, object]:
            nonlocal changed
            self.assertEqual(tree_hash(framework_root / "runtime"), original_runtime_hash)
            if not changed:
                changed = True
                self.change_external_framework(framework)
            return original_materialize(business_root, framework_root, destination)

        install_module.materialize_composite_suite = change_after_snapshot
        try:
            installed = install_suite(
                source=suite,
                project_root=project,
                harness="codex",
                ref=None,
                framework_source=framework,
            )
        finally:
            install_module.materialize_composite_suite = original_materialize

        self.assertTrue(changed)
        current = project / ".selc" / "installs" / "framework-input-snapshot" / "current"
        installation = current_installation(project, "framework-input-snapshot")
        self.assertEqual(tree_hash(current / "runtime"), original_runtime_hash)
        self.assertEqual(installation["framework_source"]["digest"], original_runtime_hash)
        self.assertEqual(
            installation["framework_source"]["version"], original_descriptor["framework_version"]
        )
        self.assertEqual(installed["framework"]["digest"], original_runtime_hash)
        self.assertNotEqual(tree_hash(framework / "runtime"), original_runtime_hash)

    def test_implicit_changed_framework_requires_an_explicit_new_selection(self) -> None:
        suite = self.create_suite("implicit-framework-snapshot", ["text-report"])
        framework = self.copy_external_framework(suite)
        project = self.root / "project"
        install_suite(
            source=suite,
            project_root=project,
            harness="codex",
            ref=None,
            framework_source=framework,
        )
        selected = current_installation(project, "implicit-framework-snapshot")
        selected_digest = selected["digest"]
        selected_framework = dict(selected["framework_source"])
        self.change_external_framework(framework)

        with self.assertRaises(SelcError) as captured:
            install_suite(
                source=suite,
                project_root=project,
                harness="workbuddy",
                ref=None,
            )
        self.assertEqual(captured.exception.code, "STALE_FRAMEWORK_SELECTION")
        unchanged = current_installation(project, "implicit-framework-snapshot")
        self.assertEqual(unchanged["digest"], selected_digest)
        self.assertEqual(unchanged["framework_source"], selected_framework)
        self.assertEqual(unchanged["harnesses"], ["codex"])

        refreshed = install_suite(
            source=suite,
            project_root=project,
            harness="workbuddy",
            ref=None,
            framework_source=framework,
        )
        current = project / ".selc" / "installs" / "implicit-framework-snapshot" / "current"
        self.assertEqual(refreshed["framework"]["digest"], tree_hash(framework / "runtime"))
        self.assertEqual(tree_hash(current / "runtime"), tree_hash(framework / "runtime"))
        self.assertEqual(
            current_installation(project, "implicit-framework-snapshot")["harnesses"],
            ["codex", "workbuddy"],
        )

    def test_framework_only_update_keeps_the_business_input_snapshot(self) -> None:
        """A mutable directory source cannot introduce business bytes after precheck."""

        suite = self.create_suite("business-input-snapshot", ["text-report"])
        framework = self.copy_external_framework(suite)
        project = self.root / "project"
        initial = install_suite(source=suite, project_root=project, harness="codex", ref=None)

        import selc.install as install_module

        original_materialize = install_module.materialize_composite_suite
        changed = False

        def change_after_snapshot(
            business_root: Path, framework_root: Path, destination: Path
        ) -> dict[str, object]:
            nonlocal changed
            self.assertEqual(portable_tree_hash(business_root), initial["digest"])
            if not changed:
                changed = True
                self.change_embedded_framework(suite)
            return original_materialize(business_root, framework_root, destination)

        install_module.materialize_composite_suite = change_after_snapshot
        try:
            exit_code, result = self.cli_in_process(
                "updates",
                "apply",
                "--suite-id",
                "business-input-snapshot",
                "--framework-source",
                str(framework),
                "--project-root",
                str(project),
                "--harness",
                "codex",
            )
        finally:
            install_module.materialize_composite_suite = original_materialize

        self.assertEqual(exit_code, 0, result)
        self.assertTrue(changed)
        current = project / ".selc" / "installs" / "business-input-snapshot" / "current"
        installed = current_installation(project, "business-input-snapshot")
        self.assertEqual(installed["version"], "1.0.0")
        self.assertEqual(installed["business_digest"], initial["digest"])
        self.assertNotIn(
            "BUSINESS_EMBEDDED_RUNTIME_MARKER",
            (current / "runtime" / "selc" / "__init__.py").read_text(encoding="utf-8"),
        )

    def test_explicit_business_update_uses_the_copied_business_input(self) -> None:
        suite = self.create_suite("business-copy-input", ["text-report"])
        framework = self.copy_external_framework(suite)
        project = self.root / "project"
        initial = install_suite(
            source=suite,
            project_root=project,
            harness="codex",
            ref=None,
            framework_source=framework,
        )

        import selc.install as install_module

        original_materialize = install_module.materialize_composite_suite
        changed = False

        def change_after_snapshot(
            business_root: Path, framework_root: Path, destination: Path
        ) -> dict[str, object]:
            nonlocal changed
            self.assertEqual(portable_tree_hash(business_root), initial["digest"])
            if not changed:
                changed = True
                self.change_embedded_framework(suite)
            return original_materialize(business_root, framework_root, destination)

        install_module.materialize_composite_suite = change_after_snapshot
        try:
            repeated = install_suite(
                source=suite,
                project_root=project,
                harness="workbuddy",
                ref=None,
                framework_source=framework,
            )
        finally:
            install_module.materialize_composite_suite = original_materialize

        self.assertTrue(changed)
        self.assertTrue(repeated["reused"])
        current = project / ".selc" / "installs" / "business-copy-input" / "current"
        installation = current_installation(project, "business-copy-input")
        self.assertEqual(installation["version"], "1.0.0")
        self.assertEqual(installation["business_digest"], initial["digest"])
        self.assertNotIn(
            "BUSINESS_EMBEDDED_RUNTIME_MARKER",
            (current / "runtime" / "selc" / "__init__.py").read_text(encoding="utf-8"),
        )

    def test_inconsistent_framework_snapshot_is_rejected_and_cleaned(self) -> None:
        """Snapshot validation rejects a partial descriptor/runtime change without resealing."""

        suite = self.create_suite("framework-input-invalid", ["text-report"])
        framework = self.copy_external_framework(suite)
        runtime_file = framework / "runtime" / "selc" / "__init__.py"
        runtime_file.write_text(
            runtime_file.read_text(encoding="utf-8") + "\nPARTIAL_RUNTIME_CHANGE = True\n",
            encoding="utf-8",
        )
        project = self.root / "project"

        import selc.install as install_module

        original_temporary_directory = install_module.temporary_directory
        owned_snapshots: list[Path] = []

        @contextlib.contextmanager
        def track_temporary_directory(prefix: str):
            with original_temporary_directory(prefix) as temporary:
                if prefix == "selc-install-input-":
                    owned_snapshots.append(temporary)
                yield temporary

        install_module.temporary_directory = track_temporary_directory
        try:
            with self.assertRaises(SelcError) as captured:
                install_suite(
                    source=suite,
                    project_root=project,
                    harness="codex",
                    ref=None,
                    framework_source=framework,
                )
        finally:
            install_module.temporary_directory = original_temporary_directory

        self.assertEqual(captured.exception.code, "FRAMEWORK_HASH_MISMATCH")
        self.assertTrue(owned_snapshots)
        self.assertTrue(all(not snapshot.exists() for snapshot in owned_snapshots))
        self.assertFalse((project / ".selc" / "installations.json").exists())

    def test_framework_snapshot_rejects_runtime_and_descriptor_symlinks(self) -> None:
        suite = self.create_suite("framework-symlink-input", ["text-report"])

        for name, entry in (
            ("runtime", "runtime/selc/__init__.py"),
            ("descriptor", "framework.json"),
        ):
            with self.subTest(entry=entry):
                framework = self.copy_external_framework(suite, f"framework-{name}")
                original = framework / entry
                outside = self.root / f"outside-{name}"
                outside.write_bytes(original.read_bytes())
                original.unlink()
                original.symlink_to(outside)

                with self.assertRaises(SelcError) as captured:
                    install_suite(
                        source=suite,
                        project_root=self.root / f"project-{name}",
                        harness="codex",
                        ref=None,
                        framework_source=framework,
                    )

                self.assertEqual(captured.exception.code, "UNSAFE_SYMLINK")

    def test_public_cli_rejects_framework_runtime_root_symlinks(self) -> None:
        suite = self.create_suite("framework-root-symlink", ["text-report"])

        directory_framework = self.copy_external_framework(suite, "directory-framework")
        directory_control = self.cli(
            "install",
            "--source",
            str(suite),
            "--framework-source",
            str(directory_framework),
            "--project-root",
            str(self.root / "directory-control"),
            "--harness",
            "codex",
        )
        self.assertTrue(directory_control["ok"])

        directory_link = self.copy_external_framework(suite, "directory-framework-link")
        directory_runtime = directory_link / "runtime"
        directory_outside = self.root / "directory-outside-runtime"
        shutil.move(str(directory_runtime), directory_outside)
        directory_runtime.symlink_to(directory_outside, target_is_directory=True)
        directory_project = self.root / "directory-link-project"
        directory_rejected = self.cli(
            "install",
            "--source",
            str(suite),
            "--framework-source",
            str(directory_link),
            "--project-root",
            str(directory_project),
            "--harness",
            "codex",
            expected=2,
        )
        self.assertEqual(directory_rejected["error"]["code"], "UNSAFE_SYMLINK")
        self.assertFalse((directory_project / ".selc" / "installations.json").exists())
        self.assertFalse((directory_project / ".agents" / "skills" / "text-report").exists())

        def framework_repository(name: str, *, root_link: bool) -> Path:
            repository = self.root / name
            repository.mkdir()
            git(["init"], repository)
            git(["config", "user.email", "fixture@example.invalid"], repository)
            git(["config", "user.name", "SELC fixture"], repository)
            framework = repository / "framework"
            framework.mkdir()
            shutil.copytree(suite / "runtime", framework / "runtime")
            shutil.copy2(suite / "framework.json", framework / "framework.json")
            if root_link:
                outside = self.root / f"{name}-outside-runtime"
                shutil.move(str(framework / "runtime"), outside)
                (framework / "runtime").symlink_to(outside, target_is_directory=True)
            git(["add", "."], repository)
            git(["commit", "-m", "framework fixture"], repository)
            return repository

        git_control = framework_repository("framework-git-control", root_link=False)
        git_control_result = self.cli(
            "install",
            "--source",
            str(suite),
            "--framework-source",
            str(git_control),
            "--framework-ref",
            "HEAD",
            "--framework-subdir",
            "framework",
            "--project-root",
            str(self.root / "git-control"),
            "--harness",
            "codex",
        )
        self.assertTrue(git_control_result["ok"])

        git_link = framework_repository("framework-git-link", root_link=True)
        git_link_project = self.root / "git-link-project"
        git_link_rejected = self.cli(
            "install",
            "--source",
            str(suite),
            "--framework-source",
            str(git_link),
            "--framework-ref",
            "HEAD",
            "--framework-subdir",
            "framework",
            "--project-root",
            str(git_link_project),
            "--harness",
            "codex",
            expected=2,
        )
        self.assertEqual(git_link_rejected["error"]["code"], "UNSAFE_SYMLINK")
        self.assertFalse((git_link_project / ".selc" / "installations.json").exists())
        self.assertFalse((git_link_project / ".agents" / "skills" / "text-report").exists())

    def test_successful_install_cleans_its_owned_input_snapshot(self) -> None:
        suite = self.create_suite("framework-input-cleanup", ["text-report"])
        project = self.root / "project"

        import selc.install as install_module

        original_temporary_directory = install_module.temporary_directory
        owned_snapshots: list[Path] = []

        @contextlib.contextmanager
        def track_temporary_directory(prefix: str):
            with original_temporary_directory(prefix) as temporary:
                if prefix == "selc-install-input-":
                    owned_snapshots.append(temporary)
                yield temporary

        install_module.temporary_directory = track_temporary_directory
        try:
            install_suite(source=suite, project_root=project, harness="codex", ref=None)
        finally:
            install_module.temporary_directory = original_temporary_directory

        self.assertTrue(owned_snapshots)
        self.assertTrue(all(not snapshot.exists() for snapshot in owned_snapshots))

    def test_resolvers_release_owned_zip_and_git_directories_without_touching_inputs(self) -> None:
        suite = self.create_suite("resolver-cleanup", ["text-report"])
        archive = self.root / "resolver-cleanup.zip"
        package_suite(suite, archive)
        repository = self.root / "resolver-cleanup-repository"
        shutil.copytree(suite, repository)
        git(["init", "-b", "main"], repository)
        git(["config", "user.name", "SELC Fixture"], repository)
        git(["config", "user.email", "fixture@example.test"], repository)
        git(["add", "."], repository)
        git(["commit", "-m", "suite"], repository)
        git(["tag", "v1.0.0"], repository)

        import selc.install as install_module

        original_temporary_directory = install_module.temporary_directory
        owned_directories: list[Path] = []

        @contextlib.contextmanager
        def track_temporary_directory(prefix: str):
            with original_temporary_directory(prefix) as temporary:
                if prefix in {
                    "selc-package-",
                    "selc-source-",
                    "selc-framework-source-",
                }:
                    owned_directories.append(temporary)
                yield temporary

        install_module.temporary_directory = track_temporary_directory
        try:
            self.assertTrue(update_check(archive, None)["checked"])
            self.assertTrue(update_check(repository, "v1.0.0")["checked"])
            self.assertTrue(framework_update_check(repository, "v1.0.0")["checked"])
            invalid_archive = self.root / "invalid-suite.zip"
            with zipfile.ZipFile(invalid_archive, "w"):
                pass
            with self.assertRaises(SelcError):
                update_check(invalid_archive, None)
        finally:
            install_module.temporary_directory = original_temporary_directory

        self.assertTrue(owned_directories)
        self.assertTrue(all(not temporary.exists() for temporary in owned_directories))
        self.assertTrue(suite.is_dir())
        self.assertTrue(archive.is_file())

    def test_implicit_install_rechecks_an_empty_framework_selection_snapshot(self) -> None:
        """A default install cannot overwrite a concurrent explicit selection."""

        suite = self.create_suite("selection-interleaving", ["text-report"])
        framework = self.copy_external_framework(suite)
        project = self.root / "project"
        install_suite(source=suite, project_root=project, harness="codex", ref=None)

        import selc.install as install_module

        original_selection = install_module._saved_external_framework_selection
        injected = False

        def select_then_change(project_root: Path, suite_id: str) -> dict[str, object] | None:
            nonlocal injected
            selection = original_selection(project_root, suite_id)
            self.assertIsNone(selection)
            if not injected:
                injected = True
                selected = install_suite(
                    source=suite,
                    project_root=project,
                    harness="codex",
                    ref=None,
                    framework_source=framework,
                )
                self.assertTrue(selected["reused"])
            return selection

        install_module._saved_external_framework_selection = select_then_change
        try:
            with self.assertRaises(SelcError) as captured:
                install_suite(
                    source=suite,
                    project_root=project,
                    harness="workbuddy",
                    ref=None,
                )
        finally:
            install_module._saved_external_framework_selection = original_selection

        self.assertEqual(captured.exception.code, "STALE_FRAMEWORK_SELECTION")
        installation = current_installation(project, "selection-interleaving")
        self.assertEqual(installation["framework_source"]["status"], "active_external")
        self.assertEqual(installation["harnesses"], ["codex"])

    def test_implicit_install_with_no_external_selection_is_idempotent(self) -> None:
        suite = self.create_suite("embedded-repeat", ["text-report"])
        project = self.root / "project"
        initial = install_suite(source=suite, project_root=project, harness="codex", ref=None)

        repeated = install_suite(
            source=suite,
            project_root=project,
            harness="workbuddy",
            ref=None,
        )

        self.assertTrue(repeated["reused"])
        self.assertEqual(repeated["digest"], initial["digest"])
        installation = current_installation(project, "embedded-repeat")
        self.assertEqual(installation["framework_source"]["status"], "active_embedded")
        self.assertEqual(installation["harnesses"], ["codex", "workbuddy"])

    def test_cli_update_apply_preserves_omitted_framework_selection(self) -> None:
        suite = self.create_suite("cli-implicit-framework", ["text-report"])
        framework = self.copy_external_framework(suite)
        project = self.root / "project"
        install_suite(
            source=suite,
            project_root=project,
            harness="codex",
            ref=None,
            framework_source=framework,
        )

        updated = self.cli(
            "updates",
            "apply",
            "--source",
            str(suite),
            "--project-root",
            str(project),
            "--harness",
            "workbuddy",
        )

        self.assertTrue(updated["installation"]["reused"])
        installation = current_installation(project, "cli-implicit-framework")
        self.assertEqual(installation["framework_source"]["repo"], str(framework.resolve()))
        self.assertEqual(installation["harnesses"], ["codex", "workbuddy"])

    def test_cli_update_apply_rechecks_omitted_framework_selection(self) -> None:
        """CLI omission retains the same compare-and-swap as ordinary install."""

        suite = self.create_suite("cli-framework-race", ["text-report"])
        framework_a = self.copy_external_framework(suite, "framework-a")
        framework_b = self.copy_external_framework(suite, "framework-b")
        self.change_external_framework(framework_b)
        project = self.root / "project"
        install_suite(
            source=suite,
            project_root=project,
            harness="codex",
            ref=None,
            framework_source=framework_a,
        )

        import selc.install as install_module

        original_install = install_module._install_materialized_suite
        injected = False

        def select_framework_b(**arguments: object) -> dict[str, object]:
            nonlocal injected
            if not injected:
                injected = True
                install_suite(
                    source=suite,
                    project_root=project,
                    harness="codex",
                    ref=None,
                    framework_source=framework_b,
                )
            return original_install(**arguments)

        install_module._install_materialized_suite = select_framework_b
        try:
            exit_code, result = self.cli_in_process(
                "updates",
                "apply",
                "--source",
                str(suite),
                "--project-root",
                str(project),
                "--harness",
                "workbuddy",
            )
        finally:
            install_module._install_materialized_suite = original_install

        self.assertEqual(exit_code, 2)
        self.assertEqual(result["error"]["code"], "STALE_FRAMEWORK_SELECTION")
        installation = current_installation(project, "cli-framework-race")
        self.assertEqual(installation["framework_source"]["repo"], str(framework_b.resolve()))
        self.assertEqual(installation["harnesses"], ["codex"])
        current = project / ".selc" / "installs" / "cli-framework-race" / "current"
        self.assertEqual(tree_hash(current / "runtime"), tree_hash(framework_b / "runtime"))

    def test_framework_only_apply_keeps_matching_directory_business(self) -> None:
        suite = self.create_suite("framework-only-normal", ["text-report"])
        framework = self.copy_external_framework(suite)
        project = self.root / "project"
        install_suite(source=suite, project_root=project, harness="codex", ref=None)

        updated = self.cli(
            "updates",
            "apply",
            "--suite-id",
            "framework-only-normal",
            "--framework-source",
            str(framework),
            "--project-root",
            str(project),
            "--harness",
            "codex",
        )

        self.assertEqual(updated["installation"]["version"], "1.0.0")
        installation = current_installation(project, "framework-only-normal")
        self.assertEqual(installation["framework_source"]["repo"], str(framework.resolve()))

    def test_framework_only_apply_rejects_changed_directory_business(self) -> None:
        suite = self.create_suite("framework-only-drift", ["text-report"])
        framework = self.copy_external_framework(suite)
        project = self.root / "project"
        install_suite(source=suite, project_root=project, harness="codex", ref=None)
        self.change_embedded_framework(suite)

        rejected = self.cli(
            "updates",
            "apply",
            "--suite-id",
            "framework-only-drift",
            "--framework-source",
            str(framework),
            "--project-root",
            str(project),
            "--harness",
            "codex",
            expected=2,
        )

        self.assertEqual(rejected["error"]["code"], "STALE_BUSINESS_SELECTION")
        installation = current_installation(project, "framework-only-drift")
        self.assertEqual(installation["version"], "1.0.0")
        current = project / ".selc" / "installs" / "framework-only-drift" / "current"
        runtime = (current / "runtime" / "selc" / "__init__.py").read_text(encoding="utf-8")
        self.assertNotIn("BUSINESS_EMBEDDED_RUNTIME_MARKER", runtime)

    def test_framework_only_apply_rechecks_business_snapshot_at_activation(self) -> None:
        repository = self.root / "business-repository"
        repository.mkdir()
        suite = repository / "suite"
        project = self.root / "project"
        create_suite("framework-only-race", "1.0.0", suite, ["text-report"])
        framework = self.copy_external_framework(suite)
        git(["init", "-b", "main"], repository)
        git(["config", "user.name", "Fixture"], repository)
        git(["config", "user.email", "fixture@example.test"], repository)
        git(["add", "."], repository)
        git(["commit", "-m", "business v1"], repository)
        first_commit = git(["rev-parse", "HEAD"], repository)
        git(["tag", "v1.0.0"], repository)
        install_suite(
            source=repository,
            subdir="suite",
            project_root=project,
            harness="codex",
            ref="v1.0.0",
        )

        self.change_embedded_framework(suite)
        skill_path = suite / "skills" / "text-report" / "SKILL.md"
        skill_path.write_text(
            skill_path.read_text(encoding="utf-8") + "\nBUSINESS_V2_CONTENT_MARKER\n",
            encoding="utf-8",
        )
        seal_suite(suite)
        git(["add", "."], repository)
        git(["commit", "-m", "business v2"], repository)
        second_commit = git(["rev-parse", "HEAD"], repository)
        git(["tag", "v1.1.0"], repository)

        import selc.install as install_module

        original_install = install_module._install_materialized_suite
        injected = False

        def apply_business_v2(**arguments: object) -> dict[str, object]:
            nonlocal injected
            if not injected:
                injected = True
                install_suite(
                    source=repository,
                    subdir="suite",
                    project_root=project,
                    harness="codex",
                    ref="v1.1.0",
                )
            return original_install(**arguments)

        install_module._install_materialized_suite = apply_business_v2
        try:
            exit_code, result = self.cli_in_process(
                "updates",
                "apply",
                "--suite-id",
                "framework-only-race",
                "--framework-source",
                str(framework),
                "--project-root",
                str(project),
                "--harness",
                "codex",
            )
        finally:
            install_module._install_materialized_suite = original_install

        self.assertEqual(exit_code, 2)
        self.assertEqual(result["error"]["code"], "STALE_BUSINESS_SELECTION")
        installation = current_installation(project, "framework-only-race")
        self.assertEqual(installation["version"], "1.1.0")
        self.assertEqual(installation["source"]["commit"], second_commit)
        self.assertNotEqual(installation["source"]["commit"], first_commit)
        current = project / ".selc" / "installs" / "framework-only-race" / "current"
        skill = (current / "skills" / "text-report" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("BUSINESS_V2_CONTENT_MARKER", skill)

    def test_implicit_install_preserves_external_git_source_baseline(self) -> None:
        suite = self.create_suite("git-selection", ["text-report"])
        framework_checkout = self.copy_external_framework(suite, "framework-checkout")
        git(["init", "-b", "main"], framework_checkout)
        git(["config", "user.name", "Fixture"], framework_checkout)
        git(["config", "user.email", "fixture@example.test"], framework_checkout)
        git(["add", "."], framework_checkout)
        git(["commit", "-m", "framework baseline"], framework_checkout)
        git(["tag", "v1.0.0"], framework_checkout)
        framework_bare = self.root / "framework.git"
        git(["clone", "--bare", str(framework_checkout), str(framework_bare)], self.root)

        project = self.root / "project"
        initial = install_suite(
            source=suite,
            project_root=project,
            harness="codex",
            ref=None,
            framework_source=framework_bare,
            framework_ref="v1.0.0",
        )
        repeated = install_suite(
            source=suite,
            project_root=project,
            harness="workbuddy",
            ref=None,
        )

        self.assertTrue(repeated["reused"])
        selected = current_installation(project, "git-selection")["framework_source"]
        self.assertEqual(selected["status"], "active_external")
        self.assertEqual(selected["repo"], str(framework_bare))
        self.assertEqual(selected["base"], "v1.0.0")
        self.assertEqual(selected["commit"], initial["framework"]["commit"])

    def test_same_digest_framework_selection_respects_active_leases(self) -> None:
        suite = self.create_suite("selection-lease", ["text-report"])
        framework = self.copy_external_framework(suite)
        project = self.root / "project"
        install_suite(source=suite, project_root=project, harness="codex", ref=None)
        self.cli(
            "task",
            "start",
            "--project-root",
            str(project),
            "--id",
            "active-task",
            "--suite-id",
            "selection-lease",
            "--initiator",
            "fixture",
            "--evidence",
            "fixture",
            "--test",
        )

        blocked = self.cli(
            "updates",
            "apply",
            "--suite-id",
            "selection-lease",
            "--framework-source",
            str(framework),
            "--project-root",
            str(project),
            "--harness",
            "codex",
            expected=2,
        )
        self.assertEqual(blocked["error"]["code"], "ACTIVE_LEASE")
        self.assertEqual(
            current_installation(project, "selection-lease")["framework_source"]["status"],
            "active_embedded",
        )

    def test_same_digest_framework_selection_recovers_registry_transaction(self) -> None:
        suite = self.create_suite("selection-recover", ["text-report"])
        framework = self.copy_external_framework(suite)
        project = self.root / "project"
        install_suite(source=suite, project_root=project, harness="codex", ref=None)

        import selc.install as install_module

        original_write_registry = install_module._write_registry
        calls = 0

        def fail_once(project_root: Path, registry: dict[str, object]) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("injected selection registry interruption")
            original_write_registry(project_root, registry)

        install_module._write_registry = fail_once
        try:
            with self.assertRaises(OSError):
                install_suite(
                    source=suite,
                    project_root=project,
                    harness="codex",
                    ref=None,
                    framework_source=framework,
                )
        finally:
            install_module._write_registry = original_write_registry

        recovered = recover_installation(project, "selection-recover")
        self.assertTrue(recovered["recovered"])
        active = current_installation(project, "selection-recover")
        self.assertEqual(active["framework_source"]["status"], "active_external")
        self.assertEqual(active["framework_source"]["repo"], str(framework.resolve()))

    def test_only_trusted_resource_checker_can_prove_a_real_delivery(self) -> None:
        suite = self.create_suite("checked-suite", ["text-report"])
        project = self.root / "project"
        installation = install_suite(source=suite, project_root=project, harness="codex", ref=None)
        current = project / ".selc" / "installs" / "checked-suite" / "current"
        input_path = self.root / "input.txt"
        report_path = self.root / "report.md"
        input_path.write_text("checked line\n", encoding="utf-8")
        ran = subprocess.run(
            [
                sys.executable,
                str(current / "bin" / "selc-suite.py"),
                "run",
                "text-report",
                "--input",
                str(input_path),
                "--output",
                str(report_path),
            ],
            capture_output=True,
            check=False,
            text=True,
        )
        self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
        self.cli(
            "task",
            "start",
            "--project-root",
            str(project),
            "--id",
            "ordinary",
            "--suite-id",
            "checked-suite",
            "--initiator",
            "person",
            "--evidence",
            "host",
            "--harness",
            "codex",
        )
        self.cli(
            "event",
            "--project-root",
            str(project),
            "--task-id",
            "ordinary",
            "--kind",
            "input_ready",
            "--evidence",
            "input",
        )
        arbitrary = self.cli(
            "delivery",
            "--project-root",
            str(project),
            "--id",
            "arbitrary",
            "--task-id",
            "ordinary",
            "--artifact-hash",
            file_hash(report_path),
            "--rule-version",
            "whatever",
            "--checker",
            "arbitrary-name",
            "--result",
            "pass",
        )
        self.assertEqual(arbitrary["delivery"]["effective_result"], "unknown")
        trusted = self.cli(
            "delivery",
            "--project-root",
            str(project),
            "--id",
            "trusted",
            "--task-id",
            "ordinary",
            "--artifact-hash",
            file_hash(report_path),
            "--rule-version",
            "text-report-v1",
            "--checker",
            "text-report-v1",
            "--input",
            str(input_path),
            "--output",
            str(report_path),
            "--result",
            "pass",
        )
        self.assertEqual(trusted["delivery"]["effective_result"], "pass")
        metrics = self.cli("stats", "--project-root", str(project))
        self.assertEqual(metrics["tasks"]["one_shot"]["U"], 1)
        self.assertEqual(metrics["tasks"]["final"]["S"], 1)
        self.assertEqual(installation["version"], "1.0.0")

    def test_manifest_input_and_resource_errors_are_explicit(self) -> None:
        with self.assertRaisesRegex(SelcError, "suite_id"):
            create_suite("Invalid", "1.0.0", self.root / "invalid-name", ["text-report"])
        with self.assertRaisesRegex(SelcError, "version is required"):
            create_suite("missing-version", "", self.root / "missing-version", ["text-report"])
        with self.assertRaisesRegex(SelcError, "unique"):
            create_suite(
                "duplicate-skills",
                "1.0.0",
                self.root / "duplicate-skills",
                ["text-report", "text-report"],
            )

        suite = self.create_suite("resource-errors", ["text-report"])
        with self.assertRaisesRegex(SelcError, "outside the suite"):
            package_suite(suite, suite / "suite.zip")
        existing_package = self.root / "existing.zip"
        existing_package.write_bytes(b"existing package")
        with self.assertRaisesRegex(SelcError, "already exists"):
            package_suite(suite, existing_package)

        resource = suite / "resources" / "text-report.json"
        resource.unlink()
        with self.assertRaisesRegex(SelcError, "resource is missing"):
            render_text_report("line", suite / "resources")
        resource.write_text("not JSON", encoding="utf-8")
        with self.assertRaisesRegex(SelcError, "not valid JSON"):
            render_text_report("line", suite / "resources")
        resource.write_text('{"heading": 3, "bullet_prefix": "- "}', encoding="utf-8")
        with self.assertRaisesRegex(SelcError, "must define string"):
            render_text_report("line", suite / "resources")

    def test_portable_skill_validator_rejects_non_string_descriptions(self) -> None:
        suite = self.create_suite("skill-structure-suite", ["text-report"])
        skill_file = suite / "skills" / "text-report" / "SKILL.md"
        original = skill_file.read_text(encoding="utf-8")

        for description in ("123", "# only a comment", "TRUE", "[not, a, string]"):
            with self.subTest(description=description):
                skill_file.write_text(
                    original.replace(
                        "description: text-report from the skill-structure-suite suite.",
                        f"description: {description}",
                    ),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(SelcError, "description|string") as captured:
                    load_manifest(suite)
                self.assertEqual(captured.exception.code, "INVALID_SKILL")

        skill_file.write_text(original, encoding="utf-8")
        self.assertEqual(load_manifest(suite)["suite_id"], "skill-structure-suite")

    def test_tree_hashes_frame_paths_and_content_without_marker_collisions(self) -> None:
        first = self.root / "first-tree"
        second = self.root / "second-tree"
        first.mkdir()
        second.mkdir()
        (first / "a.py").write_bytes(b"prefix\x00b.py\x00suffix")
        (second / "a.py").write_bytes(b"prefix")
        (second / "b.py").write_bytes(b"suffix")
        self.assertNotEqual(tree_hash(first), tree_hash(second))

        first_suite = self.root / "first-suite"
        second_suite = self.root / "second-suite"
        create_suite("hash-suite", "1.0.0", first_suite, ["text-report"])
        create_suite("hash-suite", "1.0.0", second_suite, ["text-report"])
        (first_suite / "resources" / "a.py").write_bytes(b"prefix\x00resources/b.py\x00suffix")
        (second_suite / "resources" / "a.py").write_bytes(b"prefix")
        (second_suite / "resources" / "b.py").write_bytes(b"suffix")
        self.assertNotEqual(portable_tree_hash(first_suite), portable_tree_hash(second_suite))
        self.assertNotEqual(suite_content_hash(first_suite), suite_content_hash(second_suite))

    def test_embedded_future_runtime_protocol_is_rejected(self) -> None:
        suite = self.create_suite("protocol-suite", ["text-report"])
        manifest_path = suite / "suite.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["runtime_protocol"] = 999
        manifest["content_hash"] = None
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        manifest["content_hash"] = suite_content_hash(suite)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(SelcError, "runtime protocol") as captured:
            install_suite(
                source=suite,
                project_root=self.root / "project",
                harness="codex",
                ref=None,
            )
        self.assertEqual(captured.exception.code, "UNSUPPORTED_RUNTIME_PROTOCOL")

    def test_remote_release_query_rejects_wrong_target_or_missing_asset(self) -> None:
        package = self.root / "release.zip"
        package.write_bytes(b"release package")
        plan = {
            "provider": "github",
            "tag": "v1.0.0",
            "target_commit": "expected-commit",
            "package": str(package),
            "package_hash": file_hash(package),
        }
        client = self.root / "fake-gh"
        client.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = release ] && [ "$2" = view ]; then\n'
            '  printf \'{"tagName":"v1.0.0","targetCommitish":"wrong-commit","assets":[]}\'\n'
            "fi\n",
            encoding="utf-8",
        )
        client.chmod(0o755)
        wrong_target = _remote_release_state(plan, str(client), "owner/repository")
        self.assertEqual(wrong_target["state"], "conflict")

        client.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = release ] && [ "$2" = view ]; then\n'
            '  printf \'{"tagName":"v1.0.0","targetCommitish":"expected-commit","assets":[]}\'\n'
            "fi\n",
            encoding="utf-8",
        )
        missing_asset = _remote_release_state(plan, str(client), "owner/repository")
        self.assertEqual(missing_asset["state"], "conflict")

    def test_update_synchronizes_every_registered_harness(self) -> None:
        suite = self.create_suite("sync-suite", ["alpha", "beta"])
        project = self.root / "project"
        install_suite(source=suite, project_root=project, harness="codex", ref=None)
        install_suite(source=suite, project_root=project, harness="workbuddy", ref=None)

        shutil.rmtree(suite / "skills" / "beta")
        (suite / "skills" / "gamma").mkdir()
        (suite / "skills" / "gamma" / "SKILL.md").write_text(
            "---\n"
            "name: gamma\n"
            "description: A replacement portable report skill.\n"
            "---\n\n"
            "# Gamma\n\n"
            "Use the suite wrapper to produce a checked report.\n",
            encoding="utf-8",
        )
        manifest_path = suite / "suite.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["version"] = "2.0.0"
        manifest["skills"] = [
            {"name": "alpha", "path": "skills/alpha"},
            {"name": "gamma", "path": "skills/gamma"},
        ]
        manifest["content_hash"] = None
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        manifest["content_hash"] = suite_content_hash(suite)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        installed = install_suite(source=suite, project_root=project, harness="codex", ref=None)
        self.assertEqual(installed["version"], "2.0.0")
        for base in (project / ".agents" / "skills", project / ".codebuddy" / "skills"):
            self.assertTrue((base / "alpha").is_symlink())
            self.assertTrue((base / "gamma").is_symlink())
            self.assertFalse((base / "beta").exists())
            self.assertFalse((base / "beta").is_symlink())
        installation = current_installation(project, "sync-suite")
        self.assertEqual(installation["registrations"]["codex"], ["alpha", "gamma"])
        self.assertEqual(installation["registrations"]["workbuddy"], ["alpha", "gamma"])

    def test_registry_failure_requires_and_completes_recovery(self) -> None:
        suite = self.create_suite("recover-suite", ["text-report"])
        project = self.root / "project"
        first = install_suite(source=suite, project_root=project, harness="codex", ref=None)
        manifest_path = suite / "suite.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["version"] = "2.0.0"
        manifest["content_hash"] = None
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        manifest["content_hash"] = suite_content_hash(suite)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        import selc.install as install_module

        original_write_registry = install_module._write_registry
        calls = 0

        def fail_once(project_root: Path, registry: dict[str, object]) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("injected registry write interruption")
            original_write_registry(project_root, registry)

        install_module._write_registry = fail_once
        try:
            with self.assertRaises(OSError):
                install_suite(source=suite, project_root=project, harness="codex", ref=None)
        finally:
            install_module._write_registry = original_write_registry

        with self.assertRaises(SelcError) as captured:
            current_installation(project, "recover-suite")
        self.assertEqual(captured.exception.code, "PENDING_RECOVERY")
        recovered = recover_installation(project, "recover-suite")
        self.assertTrue(recovered["recovered"])
        active = current_installation(project, "recover-suite")
        self.assertEqual(active["version"], "2.0.0")
        self.assertNotEqual(active["digest"], first["digest"])
        self.assertFalse(
            (project / ".selc" / "installs" / "recover-suite" / "journal.json").exists()
        )

    def test_process_exit_releases_lock_but_preserves_installation_journal(self) -> None:
        suite = self.create_suite("crash-suite", ["text-report"])
        project = self.root / "crash-project"
        child = (
            "import os; from pathlib import Path; import selc.install as install; "
            "install._write_registry=lambda *unused: os._exit(86); "
            f"install.install_suite(source=Path({str(suite)!r}), project_root=Path({str(project)!r}), "
            "harness='codex', ref=None)"
        )
        environment = {
            **os.environ,
            "PYTHONPATH": str(REPO_ROOT / "src"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        crashed = subprocess.run(
            [sys.executable, "-c", child],
            capture_output=True,
            check=False,
            text=True,
            env=environment,
        )
        self.assertEqual(crashed.returncode, 86, crashed.stdout + crashed.stderr)
        self.assertTrue((project / ".selc" / "installs" / "crash-suite" / "journal.json").is_file())
        recovered = recover_installation(project, "crash-suite")
        self.assertTrue(recovered["recovered"])
        self.assertEqual(current_installation(project, "crash-suite")["version"], "1.0.0")

    def test_active_call_blocks_replacement_until_explicitly_closed(self) -> None:
        suite = self.create_suite("leased-suite", ["text-report"])
        project = self.root / "lease-project"
        install_suite(source=suite, project_root=project, harness="codex", ref=None)
        database = records.open_database(project)
        records.start_task(
            database,
            task_id="task",
            suite_id="leased-suite",
            suite_version="1.0.0",
            harness="codex",
            initiator="fixture",
            evidence="fixture",
            is_test=True,
        )
        records.start_call(
            database,
            call_id="call",
            task_id="task",
            skill="text-report",
            executor="codex",
            evidence="fixture",
            expected_version="1.0.0",
            expected_hash=None,
            trigger_kind="explicit",
        )
        database.close()
        manifest_path = suite / "suite.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["version"] = "2.0.0"
        manifest["content_hash"] = None
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        manifest["content_hash"] = suite_content_hash(suite)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(SelcError) as captured:
            install_suite(source=suite, project_root=project, harness="codex", ref=None)
        self.assertEqual(captured.exception.code, "ACTIVE_LEASE")
        database = records.open_database(project)
        records.complete_call(database, "call", "unknown")
        records.close_task(database, "task")
        database.close()
        updated = install_suite(source=suite, project_root=project, harness="codex", ref=None)
        self.assertEqual(updated["version"], "2.0.0")

    def test_cli_session_start_checks_pending_installation_journal_under_locks(self) -> None:
        suite = self.create_suite("session-suite", ["text-report"])
        project = self.root / "session-project"
        install_suite(source=suite, project_root=project, harness="codex", ref=None)
        self.cli(
            "task",
            "start",
            "--project-root",
            str(project),
            "--id",
            "task",
            "--suite-id",
            "session-suite",
            "--initiator",
            "fixture",
            "--evidence",
            "fixture",
            "--test",
        )
        started = self.cli(
            "event",
            "--project-root",
            str(project),
            "--task-id",
            "task",
            "--kind",
            "session_start",
            "--evidence",
            "fixture session",
        )
        self.assertEqual(started["event"]["kind"], "session_start")
        journal = project / ".selc" / "installs" / "session-suite" / "journal.json"
        journal.write_text("{}", encoding="utf-8")
        blocked = self.cli(
            "event",
            "--project-root",
            str(project),
            "--task-id",
            "task",
            "--kind",
            "session_start",
            "--evidence",
            "fixture session",
            expected=2,
        )
        self.assertEqual(blocked["error"]["code"], "PENDING_RECOVERY")

    def test_process_owned_lock_rejects_live_owner(self) -> None:
        lock = self.root / "process.lock"
        child = (
            "import sys\n"
            "import time\n"
            "from pathlib import Path\n"
            "from selc.util import exclusive_lock\n"
            "with exclusive_lock(Path(sys.argv[1])):\n"
            "    print('locked', flush=True)\n"
            "    time.sleep(5)\n"
        )
        environment = {**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")}
        holder = subprocess.Popen(
            [sys.executable, "-c", child, str(lock)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )
        try:
            assert holder.stdout is not None
            self.assertEqual(holder.stdout.readline().strip(), "locked")
            with self.assertRaises(SelcError) as captured, exclusive_lock(lock):
                self.fail("the live process lock must not be acquired")
            self.assertEqual(captured.exception.code, "LOCKED")
        finally:
            holder.terminate()
            holder.wait(timeout=5)
            if holder.stdout is not None:
                holder.stdout.close()
            if holder.stderr is not None:
                holder.stderr.close()

    def test_provenance_package_supports_portable_contribution_without_checkout(self) -> None:
        checkout = self.root / "source-checkout"
        checkout.mkdir()
        git(["init"], checkout)
        git(["config", "user.name", "Fixture"], checkout)
        git(["config", "user.email", "fixture@example.test"], checkout)
        suite = checkout / "suite"
        create_suite("portable-suite", "1.0.0", suite, ["text-report"])
        git(["add", "."], checkout)
        git(["commit", "-m", "baseline"], checkout)
        branch = git(["branch", "--show-current"], checkout)
        bare = self.root / "source.git"
        git(["clone", "--bare", str(checkout), str(bare)], self.root)
        git(["remote", "add", "origin", str(bare)], checkout)
        package = self.root / "portable-suite.zip"
        package_suite(suite, package)

        project = self.root / "consumer"
        install_suite(source=package, project_root=project, harness="workbuddy", ref=None)
        current = project / ".selc" / "installs" / "portable-suite" / "current"
        input_path = self.root / "input.txt"
        output_path = self.root / "report.md"
        input_path.write_text("portable contribution\n", encoding="utf-8")
        run = subprocess.run(
            [
                sys.executable,
                str(current / "bin" / "selc-suite.py"),
                "run",
                "text-report",
                "--input",
                str(input_path),
                "--output",
                str(output_path),
            ],
            capture_output=True,
            check=False,
            text=True,
        )
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        shutil.rmtree(checkout)

        candidate_parent = self.root / "candidate"
        prepared = subprocess.run(
            [
                sys.executable,
                str(current / "bin" / "selc-suite.py"),
                "evolve",
                "prepare",
                "--suite",
                str(current),
                "--output",
                str(candidate_parent),
            ],
            capture_output=True,
            check=False,
            text=True,
        )
        self.assertEqual(prepared.returncode, 0, prepared.stdout + prepared.stderr)
        candidate = candidate_parent / "candidate"
        candidate_skill = candidate / "skills" / "text-report" / "SKILL.md"
        candidate_skill.write_text(
            candidate_skill.read_text(encoding="utf-8") + "portable edit\n", encoding="utf-8"
        )
        evaluation = self.root / "evaluation.json"
        evaluated = subprocess.run(
            [
                sys.executable,
                str(current / "bin" / "selc-suite.py"),
                "evolve",
                "evaluate",
                "--candidate",
                str(candidate),
                "--output",
                str(evaluation),
            ],
            capture_output=True,
            check=False,
            text=True,
        )
        self.assertEqual(evaluated.returncode, 0, evaluated.stdout + evaluated.stderr)
        plan = self.root / "contribution.json"
        contribution = subprocess.run(
            [
                sys.executable,
                str(current / "bin" / "selc-suite.py"),
                "contribute",
                "prepare",
                "--candidate",
                str(candidate),
                "--suite",
                str(current),
                "--evaluation",
                str(evaluation),
                "--project-root",
                str(project),
                "--suite-id",
                "portable-suite",
                "--workspace",
                str(self.root / "workspace"),
                "--output",
                str(plan),
            ],
            capture_output=True,
            check=False,
            text=True,
        )
        self.assertEqual(contribution.returncode, 0, contribution.stdout + contribution.stderr)
        plan_value = json.loads(plan.read_text(encoding="utf-8"))
        self.assertEqual(plan_value["source_repository"], str(bare))
        self.assertEqual(plan_value["base_branch"], branch)
        workspace_skill = self.root / "workspace" / "suite" / "skills" / "text-report" / "SKILL.md"
        workspace_skill.write_text(
            workspace_skill.read_text(encoding="utf-8") + "unevaluated workspace edit\n",
            encoding="utf-8",
        )
        rejected = subprocess.run(
            [
                sys.executable,
                str(current / "bin" / "selc-suite.py"),
                "contribute",
                "execute",
                "--plan",
                str(plan),
                "--provider",
                "local",
            ],
            capture_output=True,
            check=False,
            text=True,
        )
        self.assertEqual(rejected.returncode, 2, rejected.stdout + rejected.stderr)
        self.assertEqual(
            json.loads(rejected.stdout)["error"]["code"], "UNEVALUATED_WORKSPACE_CHANGE"
        )

    def test_release_package_keeps_git_provenance_and_safe_nested_tag(self) -> None:
        checkout = self.root / "release-checkout"
        checkout.mkdir()
        git(["init", "-b", "main"], checkout)
        git(["config", "user.name", "Fixture"], checkout)
        git(["config", "user.email", "fixture@example.test"], checkout)
        suite = checkout / "suite"
        create_suite("release-suite", "1.0.0", suite, ["text-report"])
        git(["add", "."], checkout)
        git(["commit", "-m", "baseline"], checkout)
        git(["tag", "v1.0.0"], checkout)
        bare = self.root / "release-source.git"
        git(["clone", "--bare", str(checkout), str(bare)], self.root)
        git(["remote", "add", "origin", str(bare)], checkout)
        candidate_parent = self.root / "release-candidate"
        candidate = Path(prepare_candidate(suite, candidate_parent)["candidate"])
        candidate_skill = candidate / "skills" / "text-report" / "SKILL.md"
        candidate_skill.write_text(
            candidate_skill.read_text(encoding="utf-8") + "evaluated release edit\n",
            encoding="utf-8",
        )
        evaluation_path = self.root / "release-evaluation.json"
        evaluation = evaluate_candidate(
            candidate,
            evaluation_path,
            max_runs=8,
            model_calls=0,
            candidate_number=1,
            candidate_limit=2,
            run_timeout_seconds=10,
            total_timeout_seconds=60,
        )
        self.assertEqual(evaluation["status"], "passed")
        shutil.rmtree(suite / "skills")
        shutil.copytree(candidate / "skills", suite / "skills")
        shutil.rmtree(suite / "resources")
        shutil.copytree(candidate / "resources", suite / "resources")
        manifest_path = suite / "suite.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["content_hash"] = suite_content_hash(suite)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        git(["add", "."], checkout)
        git(["commit", "-m", "evaluated candidate"], checkout)
        git(["push", "origin", "main"], checkout)

        stable_project = self.root / "stable-tag-consumer"
        install_suite(
            source=bare,
            project_root=stable_project,
            harness="codex",
            ref=None,
            subdir="suite",
        )
        stable_current = stable_project / ".selc" / "installs" / "release-suite" / "current"
        stable_contribution = prepare_contribution(
            candidate,
            stable_current,
            evaluation_path,
            None,
            self.root / "stable-tag-workspace",
            self.root / "stable-tag-plan.json",
            stable_project,
            "release-suite",
        )
        self.assertEqual(stable_contribution["base_branch"], "main")

        with self.assertRaises(SelcError) as unsafe_tag:
            prepare_release(
                suite,
                candidate,
                evaluation_path,
                "local",
                "HEAD",
                "../../escaped-release",
                self.root / "unsafe-plan.json",
                self.root / "release-center",
                None,
            )
        self.assertEqual(unsafe_tag.exception.code, "UNSAFE_TAG")

        plan_path = self.root / "release-plan.json"
        plan = prepare_release(
            suite,
            candidate,
            evaluation_path,
            "local",
            "HEAD",
            "nested/v1.0.0",
            plan_path,
            self.root / "release-center",
            None,
        )
        confirmation_path = self.root / "release-confirmation.json"
        confirm_release(plan_path, str(plan["plan_hash"]), confirmation_path)
        self.assertEqual(tree_hash(suite), plan["target_suite_hash"])
        released = release_local(plan_path, confirmation_path, self.root / "release-center")
        release_directory = Path(released["location"])
        self.assertTrue(release_directory.is_relative_to((self.root / "release-center").resolve()))
        verify_local_release(self.root / "release-center", "nested/v1.0.0")
        with self.assertRaises(SelcError) as unsafe_verify:
            verify_local_release(self.root / "release-center", "../../escaped-release")
        self.assertEqual(unsafe_verify.exception.code, "UNSAFE_TAG")
        package = Path(str(plan["package"]))
        with zipfile.ZipFile(package) as archive:
            baseline = json.loads(archive.read("source-baseline.json"))
        self.assertEqual(baseline["repo"], str(bare))
        self.assertEqual(baseline["contribution_base"], "main")

        shutil.rmtree(checkout)
        consumer = self.root / "release-consumer"
        install_suite(source=package, project_root=consumer, harness="workbuddy", ref=None)
        current = consumer / ".selc" / "installs" / "release-suite" / "current"
        next_candidate_parent = self.root / "next-release-candidate"
        next_candidate = Path(prepare_candidate(current, next_candidate_parent)["candidate"])
        next_skill = next_candidate / "skills" / "text-report" / "SKILL.md"
        next_skill.write_text(
            next_skill.read_text(encoding="utf-8") + "portable follow-up edit\n",
            encoding="utf-8",
        )
        next_evaluation = self.root / "next-release-evaluation.json"
        evaluate_candidate(
            next_candidate,
            next_evaluation,
            max_runs=8,
            model_calls=0,
            candidate_number=1,
            candidate_limit=2,
            run_timeout_seconds=10,
            total_timeout_seconds=60,
        )
        contribution = prepare_contribution(
            next_candidate,
            current,
            next_evaluation,
            None,
            self.root / "portable-release-workspace",
            self.root / "portable-release-plan.json",
            consumer,
            "release-suite",
        )
        self.assertEqual(contribution["source_repository"], str(bare))
        self.assertEqual(contribution["base_branch"], "main")
