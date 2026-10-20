"""End-to-end tests for the small, portable SELC v0.1 surface."""

from __future__ import annotations

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
CLI = REPO_ROOT / "scripts" / "selc.py"
sys.path.insert(0, str(REPO_ROOT / "src"))


def run_cli(*arguments: str, expected: int = 0) -> dict[str, object]:
    completed = subprocess.run(
        [sys.executable, str(CLI), *arguments],
        cwd=REPO_ROOT,
        capture_output=True,
        check=False,
        text=True,
    )
    if completed.returncode != expected:
        raise AssertionError(
            f"expected exit {expected}, got {completed.returncode}\n"
            f"stdout: {completed.stdout}\nstderr: {completed.stderr}"
        )
    return json.loads(completed.stdout)


def write_release_fake(
    path: Path,
    state: Path,
    provider: str,
    *,
    ambiguous_create: bool = False,
    contribution_center: Path | None = None,
) -> None:
    """Provide the documented query/create/download shape without a real provider."""

    if provider == "github":
        metadata = 'printf \'{\\"tagName\\":\\"%s\\",\\"targetCommitish\\":\\"%s\\",\\"assets\\":[{\\"name\\":\\"%s\\"}]}\' "$tag" "$target" "$(basename "$package")"'
    else:
        metadata = (
            'printf \'{\\"tag_name\\":\\"%s\\",\\"commit\\":{\\"id\\":\\"%s\\"}}\' "$tag" "$target"'
        )
    contribution_adapter = ""
    if provider == "github" and contribution_center is not None:
        request_state = state.with_suffix(".contribution-request")
        contribution_adapter = (
            'if [ "$1" = pr ] && [ "$2" = list ]; then\n'
            f'  if [ ! -f "{request_state}" ]; then printf "[]"; exit 0; fi\n'
            f'  IFS="|" read branch commit < "{request_state}"\n'
            '  printf \'[{"number":1,"headRefName":"%s","baseRefName":"main","headRefOid":"%s","headRepository":{"nameWithOwner":"owner/control-repository"},"headRepositoryOwner":{"login":"owner"}}]\' "$branch" "$commit"\n'
            "  exit 0\n"
            "fi\n"
            'if [ "$1" = pr ] && [ "$2" = create ]; then\n'
            "  branch=\n"
            "  previous=\n"
            '  for argument in "$@"; do\n'
            '    if [ "$previous" = --head ]; then branch=$argument; break; fi\n'
            "    previous=$argument\n"
            "  done\n"
            f'  commit=$(git --git-dir "{contribution_center}" rev-parse "refs/heads/$branch")\n'
            f'  printf "%s|%s\\n" "$branch" "$commit" > "{request_state}"\n'
            "  printf 'created\\n'\n"
            "  exit 0\n"
            "fi\n"
        )
    if provider == "gitlab" and contribution_center is not None:
        request_state = state.with_suffix(".contribution-request")
        contribution_adapter = (
            'if [ "$1" = api ]; then\n'
            f'  if [ ! -f "{request_state}" ]; then printf "[]"; exit 0; fi\n'
            f'  IFS="|" read branch commit < "{request_state}"\n'
            '  printf \'[{"state":"opened","source_branch":"%s","target_branch":"main","sha":"%s","source_project_id":17,"target_project_id":17}]\' "$branch" "$commit"\n'
            "  exit 0\n"
            "fi\n"
            'if [ "$1" = mr ] && [ "$2" = create ]; then\n'
            "  branch=\n"
            "  previous=\n"
            '  for argument in "$@"; do\n'
            '    if [ "$previous" = --source-branch ]; then branch=$argument; break; fi\n'
            "    previous=$argument\n"
            "  done\n"
            f'  commit=$(git --git-dir "{contribution_center}" rev-parse "refs/heads/$branch")\n'
            f'  printf "%s|%s\\n" "$branch" "$commit" > "{request_state}"\n'
            "  printf 'created\\n'\n"
            "  exit 0\n"
            "fi\n"
        )
    path.write_text(
        "#!/bin/sh\n"
        f"state={state}\n"
        'if [ "$1" = release ] && [ "$2" = view ]; then\n'
        "  if [ ! -f \"$state\" ]; then printf 'release not found\\n' >&2; exit 1; fi\n"
        "  IFS='|' read tag target package < \"$state\"\n"
        f"  {metadata}\n"
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = release ] && [ "$2" = create ]; then\n'
        "  tag=$3\n"
        "  package=$4\n"
        "  target=\n"
        "  shift 4\n"
        "  while [ $# -gt 0 ]; do\n"
        '    if [ "$1" = --target ] || [ "$1" = --ref ]; then target=$2; shift 2; continue; fi\n'
        "    shift\n"
        "  done\n"
        '  printf \'%s|%s|%s\\n\' "$tag" "$target" "$package" > "$state"\n'
        "  printf 'created\\n'\n"
        f"  exit {1 if ambiguous_create else 0}\n"
        "fi\n"
        'if [ "$1" = release ] && [ "$2" = download ]; then\n'
        '  if [ ! -f "$state" ]; then exit 1; fi\n'
        "  IFS='|' read tag target package < \"$state\"\n"
        "  directory=\n"
        '  for argument in "$@"; do\n'
        '    if [ "$previous" = --dir ]; then directory=$argument; break; fi\n'
        "    previous=$argument\n"
        "  done\n"
        '  cp "$package" "$directory/$(basename "$package")"\n'
        "  exit 0\n"
        "fi\n"
        + contribution_adapter
        + 'if [ "$1" = pr ] || [ "$1" = mr ]; then\n'
        + '  if [ "$2" = list ]; then printf "[]"; exit 0; fi\n'
        + f"  printf '{provider} adapter invoked\\n'\n"
        + "  exit 0\n"
        + "fi\n"
        f"printf '{provider} adapter invoked\\n'\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


class SelcSmokeTests(unittest.TestCase):
    def test_create_install_run_and_stats(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            suite = temporary / "suite"
            project = temporary / "project"
            package = temporary / "suite.zip"
            input_file = temporary / "input.txt"
            report_file = temporary / "report.md"

            created = run_cli(
                "create",
                "--suite-id",
                "simple-reports",
                "--version",
                "1.0.0",
                "--output",
                str(suite),
                "--skill",
                "text-report",
                "--skill",
                "text-check",
            )
            self.assertEqual(created["suite"]["suite_id"], "simple-reports")
            run_cli("validate", "--suite", str(suite))
            (suite / ".selc").mkdir()
            (suite / ".selc" / "business-output.txt").write_text("private", encoding="utf-8")
            (suite / ".git").mkdir()
            (suite / ".git" / "config").write_text("private", encoding="utf-8")
            run_cli("package", "--suite", str(suite), "--output", str(package))
            with zipfile.ZipFile(package) as archive:
                names = set(archive.namelist())
                skill_body = archive.read("skills/text-report/SKILL.md").decode("utf-8")
            self.assertNotIn(".selc/business-output.txt", names)
            self.assertNotIn(".git/config", names)
            self.assertIn("task start", skill_body)
            self.assertIn("Scanning, discovery", skill_body)
            unsafe_package = run_cli(
                "package",
                "--suite",
                str(suite),
                "--output",
                str(suite / "inside-suite.zip"),
                expected=2,
            )
            self.assertEqual(unsafe_package["error"]["code"], "UNSAFE_PACKAGE_OUTPUT")

            direct_project = temporary / "direct-project"
            run_cli(
                "install",
                "--source",
                str(suite),
                "--project-root",
                str(direct_project),
                "--harness",
                "codex",
            )
            direct_contents = direct_project / ".selc" / "installs" / "simple-reports" / "current"
            self.assertFalse((direct_contents / ".selc").exists())
            self.assertFalse((direct_contents / ".git").exists())
            outside_resource = temporary / "outside-resource.txt"
            outside_resource.write_text("outside", encoding="utf-8")
            unsafe_link = suite / "resources" / "outside-resource.txt"
            unsafe_link.symlink_to(outside_resource)
            unsafe_symlink = run_cli(
                "package",
                "--suite",
                str(suite),
                "--output",
                str(temporary / "unsafe-symlink.zip"),
                expected=2,
            )
            self.assertEqual(unsafe_symlink["error"]["code"], "UNSAFE_SYMLINK")
            unsafe_link.unlink()

            installed = run_cli(
                "install",
                "--source",
                str(package),
                "--project-root",
                str(project),
                "--harness",
                "codex",
            )
            self.assertEqual(installed["installation"]["version"], "1.0.0")
            expected_hash = str(installed["installation"]["digest"])
            workbuddy = run_cli(
                "install",
                "--source",
                str(package),
                "--project-root",
                str(project),
                "--harness",
                "workbuddy",
            )
            self.assertTrue(workbuddy["installation"]["reused"])

            input_file.write_text("first point\nsecond point\n", encoding="utf-8")
            portable_cli = (
                project
                / ".selc"
                / "installs"
                / "simple-reports"
                / "current"
                / "bin"
                / "selc-suite.py"
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    str(portable_cli),
                    "run",
                    "text-report",
                    "--input",
                    str(input_file),
                    "--output",
                    str(report_file),
                ],
                capture_output=True,
                check=False,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("first point", report_file.read_text(encoding="utf-8"))

            run_cli(
                "task",
                "start",
                "--project-root",
                str(project),
                "--id",
                "task-1",
                "--suite-id",
                "simple-reports",
                "--initiator",
                "author",
                "--evidence",
                "fixture",
                "--test",
            )
            stale_runtime = run_cli(
                "call",
                "start",
                "--project-root",
                str(project),
                "--id",
                "stale-call",
                "--task-id",
                "task-1",
                "--skill",
                "text-report",
                "--executor",
                "fixture-runner",
                "--evidence",
                "fixture",
                "--expected-version",
                "1.0.0",
                "--expected-hash",
                "wrong-hash",
                expected=2,
            )
            self.assertEqual(stale_runtime["error"]["code"], "STALE_RUNTIME")
            run_cli(
                "call",
                "start",
                "--project-root",
                str(project),
                "--id",
                "call-1",
                "--task-id",
                "task-1",
                "--skill",
                "text-report",
                "--executor",
                "fixture-runner",
                "--evidence",
                "fixture",
                "--expected-version",
                "1.0.0",
                "--expected-hash",
                expected_hash,
            )
            run_cli(
                "event",
                "--project-root",
                str(project),
                "--task-id",
                "task-1",
                "--kind",
                "input_ready",
                "--evidence",
                "fixture input file",
            )
            run_cli(
                "delivery",
                "--project-root",
                str(project),
                "--id",
                "delivery-1",
                "--task-id",
                "task-1",
                "--call-id",
                "call-1",
                "--artifact-hash",
                "abc123",
                "--rule-version",
                "v1",
                "--result",
                "pass",
                "--complete-observation-fixture",
            )
            run_cli(
                "call",
                "complete",
                "--project-root",
                str(project),
                "--id",
                "call-1",
                "--result",
                "pass",
            )
            run_cli("task", "close", "--project-root", str(project), "--id", "task-1")

            stats = run_cli("stats", "--project-root", str(project), "--include-test")
            self.assertEqual(stats["tasks"]["final"]["N"], 1)
            self.assertEqual(stats["calls"]["final"]["N"], 1)
            snapshot = temporary / "task-call-snapshot.json"
            run_cli("export", "--project-root", str(project), "--output", str(snapshot))
            snapshot_value = json.loads(snapshot.read_text(encoding="utf-8"))
            self.assertIn("as_of", snapshot_value)
            self.assertEqual(snapshot_value["calls"][0]["current_result"], "pass")
            imported_project = temporary / "imported-project"
            run_cli("import", "--project-root", str(imported_project), "--input", str(snapshot))
            imported_stats = run_cli(
                "stats", "--project-root", str(imported_project), "--include-test"
            )
            self.assertEqual(imported_stats["calls"]["one_shot"]["S"], 1)
            self.assertEqual(imported_stats["calls"]["final"]["S"], 1)

    def test_unknown_observation_and_snapshot_replay_are_safe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            project = temporary / "project"
            snapshot = temporary / "snapshot.json"
            run_cli(
                "task",
                "start",
                "--project-root",
                str(project),
                "--id",
                "real-task",
                "--suite-id",
                "suite",
                "--initiator",
                "agent",
                "--evidence",
                "self-report",
            )
            run_cli(
                "delivery",
                "--project-root",
                str(project),
                "--id",
                "real-delivery",
                "--task-id",
                "real-task",
                "--artifact-hash",
                "hash",
                "--rule-version",
                "v1",
                "--result",
                "pass",
            )
            run_cli("task", "close", "--project-root", str(project), "--id", "real-task")
            stats = run_cli("stats", "--project-root", str(project))
            self.assertEqual(stats["tasks"]["final"]["U"], 1)

            run_cli("export", "--project-root", str(project), "--output", str(snapshot))
            first_import = run_cli(
                "import", "--project-root", str(project), "--input", str(snapshot)
            )
            second_import = run_cli(
                "import", "--project-root", str(project), "--input", str(snapshot)
            )
            self.assertEqual(first_import["imported"], "noop_same_origin")
            self.assertEqual(second_import["imported"], "noop_same_origin")

    def test_cross_origin_import_delete_and_feedback_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            source_project = temporary / "source"
            target_project = temporary / "target"
            snapshot = temporary / "snapshot.json"
            run_cli(
                "task",
                "start",
                "--project-root",
                str(source_project),
                "--id",
                "task",
                "--suite-id",
                "suite",
                "--initiator",
                "fixture",
                "--evidence",
                "fixture",
                "--test",
            )
            run_cli(
                "delivery",
                "--project-root",
                str(source_project),
                "--id",
                "delivery",
                "--task-id",
                "task",
                "--artifact-hash",
                "hash",
                "--rule-version",
                "v1",
                "--result",
                "pass",
                "--complete-observation-fixture",
            )
            run_cli(
                "feedback",
                "--project-root",
                str(source_project),
                "--id",
                "feedback-1",
                "--task-id",
                "task",
                "--delivery-id",
                "delivery",
                "--revision",
                "1",
                "--scope",
                "final",
                "--result",
                "fail",
            )
            run_cli(
                "feedback",
                "--project-root",
                str(source_project),
                "--id",
                "feedback-1",
                "--task-id",
                "task",
                "--delivery-id",
                "delivery",
                "--revision",
                "2",
                "--scope",
                "final",
                "--result",
                "pass",
            )
            run_cli("task", "close", "--project-root", str(source_project), "--id", "task")
            self.assertEqual(
                run_cli("stats", "--project-root", str(source_project), "--include-test")["tasks"][
                    "final"
                ]["S"],
                1,
            )
            run_cli("export", "--project-root", str(source_project), "--output", str(snapshot))
            imported = run_cli(
                "import", "--project-root", str(target_project), "--input", str(snapshot)
            )
            self.assertEqual(imported["imported"], "replaced_origin_snapshot")
            run_cli("export", "--project-root", str(source_project), "--output", str(snapshot))
            newer = run_cli(
                "import", "--project-root", str(target_project), "--input", str(snapshot)
            )
            self.assertEqual(newer["imported"], "replaced_origin_snapshot")
            self.assertEqual(
                run_cli("stats", "--project-root", str(target_project), "--include-test")["tasks"][
                    "final"
                ]["N"],
                1,
            )
            run_cli(
                "delete", "--project-root", str(target_project), "--suite-id", "suite", "--apply"
            )
            self.assertEqual(
                run_cli("stats", "--project-root", str(target_project), "--include-test")["tasks"][
                    "final"
                ]["N"],
                0,
            )
            self.assertEqual(
                run_cli("import", "--project-root", str(target_project), "--input", str(snapshot))[
                    "imported"
                ],
                "noop_same_revision",
            )

    def test_git_update_leases_local_edits_candidate_and_release(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            repository = temporary / "repository"
            suite = repository / "suite"
            project = temporary / "project"
            candidate_parent = temporary / "candidate"
            evaluation = temporary / "evaluation.json"
            plan = temporary / "release-plan.json"
            confirmation = temporary / "confirmation.json"
            center = temporary / "release-center"
            repository.mkdir()
            subprocess.run(["git", "init"], cwd=repository, check=True, capture_output=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.invalid"], cwd=repository, check=True
            )
            subprocess.run(["git", "config", "user.name", "SELC Test"], cwd=repository, check=True)
            run_cli(
                "create",
                "--suite-id",
                "update-suite",
                "--version",
                "1.0.0",
                "--output",
                str(suite),
                "--skill",
                "text-report",
            )
            subprocess.run(["git", "add", "."], cwd=repository, check=True)
            subprocess.run(
                ["git", "commit", "-m", "v1"], cwd=repository, check=True, capture_output=True
            )
            subprocess.run(["git", "tag", "v1.0.0"], cwd=repository, check=True)
            run_cli(
                "install",
                "--source",
                str(repository),
                "--subdir",
                "suite",
                "--ref",
                "v1.0.0",
                "--project-root",
                str(project),
                "--harness",
                "codex",
                "--framework-source",
                str(repository),
                "--framework-ref",
                "v1.0.0",
            )
            current = project / ".selc" / "installs" / "update-suite" / "current"
            input_file = temporary / "input.txt"
            report_file = temporary / "report.md"
            input_file.write_text("update evidence\n", encoding="utf-8")
            portable_run = subprocess.run(
                [
                    sys.executable,
                    str(current / "bin" / "selc-suite.py"),
                    "run",
                    "text-report",
                    "--input",
                    str(input_file),
                    "--output",
                    str(report_file),
                ],
                capture_output=True,
                check=False,
                text=True,
            )
            self.assertEqual(portable_run.returncode, 0, portable_run.stderr)
            from selc.manifest import portable_tree_hash

            installed_digest_before_import = portable_tree_hash(current)
            module_import = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import sys; sys.path.insert(0, sys.argv[1]); import selc.cli",
                    str(current / "runtime"),
                ],
                capture_output=True,
                check=False,
                text=True,
            )
            self.assertEqual(module_import.returncode, 0, module_import.stderr)
            self.assertEqual(installed_digest_before_import, portable_tree_hash(current))
            active_started = run_cli(
                "task",
                "start",
                "--project-root",
                str(project),
                "--id",
                "active",
                "--suite-id",
                "update-suite",
                "--initiator",
                "fixture",
                "--evidence",
                "fixture",
                "--test",
            )
            self.assertEqual(
                active_started["daily_update_check"]["result"]["business"]["status"],
                "checked",
            )
            manifest_path = suite / "suite.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["version"] = "1.1.0"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            runtime_init = suite / "runtime" / "selc" / "__init__.py"
            runtime_init.write_text(
                runtime_init.read_text(encoding="utf-8") + "\nFRAMEWORK_MARKER = 'v1.1'\n",
                encoding="utf-8",
            )
            from selc.manifest import suite_content_hash
            from selc.util import tree_hash

            framework_descriptor = suite / "framework.json"
            framework_value = json.loads(framework_descriptor.read_text(encoding="utf-8"))
            framework_value["runtime_hash"] = tree_hash(suite / "runtime")
            framework_descriptor.write_text(json.dumps(framework_value), encoding="utf-8")
            manifest["content_hash"] = suite_content_hash(suite)
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repository, check=True)
            subprocess.run(
                ["git", "commit", "-m", "v2"], cwd=repository, check=True, capture_output=True
            )
            subprocess.run(["git", "tag", "v1.1.0"], cwd=repository, check=True)
            checked = run_cli(
                "updates",
                "check",
                "--business-source",
                str(repository),
                "--framework-source",
                str(repository),
                "--subdir",
                "suite",
                "--ref",
                "v1.1.0",
                "--force",
            )
            self.assertEqual(checked["business"]["version"], "1.1.0")
            self.assertTrue(checked["forced"])
            self.assertIn("framework", checked)
            self.assertFalse(checked["framework"]["applied"])
            framework_apply = run_cli(
                "updates",
                "apply",
                "--source",
                str(repository),
                "--framework-source",
                str(repository),
                "--subdir",
                "suite",
                "--ref",
                "v1.1.0",
                "--project-root",
                str(project),
                "--harness",
                "codex",
                expected=2,
            )
            self.assertEqual(framework_apply["error"]["code"], "ACTIVE_LEASE")
            rejected = run_cli(
                "updates",
                "apply",
                "--source",
                str(repository),
                "--subdir",
                "suite",
                "--ref",
                "v1.1.0",
                "--project-root",
                str(project),
                "--harness",
                "codex",
                expected=2,
            )
            self.assertEqual(rejected["error"]["code"], "ACTIVE_LEASE")
            run_cli("task", "close", "--project-root", str(project), "--id", "active")
            cached_started = run_cli(
                "task",
                "start",
                "--project-root",
                str(project),
                "--id",
                "cached-check",
                "--suite-id",
                "update-suite",
                "--initiator",
                "fixture",
                "--evidence",
                "fixture",
                "--test",
            )
            self.assertTrue(cached_started["daily_update_check"]["cached"])
            run_cli("task", "close", "--project-root", str(project), "--id", "cached-check")
            business_updated = run_cli(
                "updates",
                "apply",
                "--source",
                str(repository),
                "--subdir",
                "suite",
                "--ref",
                "v1.1.0",
                "--project-root",
                str(project),
                "--harness",
                "codex",
            )
            self.assertEqual(business_updated["installation"]["version"], "1.1.0")
            framework_updated = run_cli(
                "updates",
                "apply",
                "--source",
                str(repository),
                "--subdir",
                "suite",
                "--ref",
                "v1.1.0",
                "--framework-source",
                str(repository),
                "--framework-ref",
                "v1.1.0",
                "--project-root",
                str(project),
                "--harness",
                "codex",
            )
            self.assertEqual(
                framework_updated["installation"]["framework"]["status"], "active_external"
            )
            self.assertIn(
                "FRAMEWORK_MARKER = 'v1.1'",
                (current / "runtime" / "selc" / "__init__.py").read_text(encoding="utf-8"),
            )
            framework_package = temporary / "framework.zip"
            run_cli(
                "package",
                "--suite",
                str(suite),
                "--output",
                str(framework_package),
                "--framework-only",
            )
            with zipfile.ZipFile(framework_package) as archive:
                self.assertNotIn("skills/text-report/SKILL.md", archive.namelist())
                self.assertIn("framework.json", archive.namelist())
            bad_framework = temporary / "bad-framework"
            (bad_framework / "runtime").mkdir(parents=True)
            shutil.copytree(suite / "runtime" / "selc", bad_framework / "runtime" / "selc")
            bad_descriptor = json.loads((suite / "framework.json").read_text(encoding="utf-8"))
            bad_descriptor["runtime_protocol"] = 2
            (bad_framework / "framework.json").write_text(
                json.dumps(bad_descriptor), encoding="utf-8"
            )
            incompatible = run_cli(
                "updates",
                "apply",
                "--source",
                str(repository),
                "--subdir",
                "suite",
                "--ref",
                "v1.1.0",
                "--framework-source",
                str(bad_framework),
                "--project-root",
                str(project),
                "--harness",
                "codex",
                expected=2,
            )
            self.assertEqual(incompatible["error"]["code"], "UNSUPPORTED_RUNTIME_PROTOCOL")
            self.assertIn(
                "FRAMEWORK_MARKER = 'v1.1'",
                (current / "runtime" / "selc" / "__init__.py").read_text(encoding="utf-8"),
            )
            current = project / ".selc" / "installs" / "update-suite" / "current"
            journal = current.parent / "journal.json"
            journal.write_text(
                json.dumps(
                    {
                        "state": "activating",
                        "new_digest": current.resolve().name,
                        "old_digest": None,
                    }
                ),
                encoding="utf-8",
            )
            recovered = run_cli(
                "updates",
                "recover",
                "--project-root",
                str(project),
                "--suite-id",
                "update-suite",
            )
            self.assertTrue(recovered["recovered"])
            other_source = temporary / "other-source"
            run_cli(
                "create",
                "--suite-id",
                "update-suite",
                "--version",
                "9.0.0",
                "--output",
                str(other_source),
                "--skill",
                "text-report",
            )
            source_conflict = run_cli(
                "install",
                "--source",
                str(other_source),
                "--project-root",
                str(project),
                "--harness",
                "codex",
                expected=2,
            )
            self.assertEqual(source_conflict["error"]["code"], "NAME_CONFLICT")
            current_skill = (
                project
                / ".selc"
                / "installs"
                / "update-suite"
                / "current"
                / "skills"
                / "text-report"
                / "SKILL.md"
            )
            current_skill.write_text(
                current_skill.read_text(encoding="utf-8") + "local edit\n", encoding="utf-8"
            )
            conflict = run_cli(
                "install",
                "--source",
                str(repository),
                "--subdir",
                "suite",
                "--ref",
                "v1.1.0",
                "--project-root",
                str(project),
                "--harness",
                "codex",
                expected=2,
            )
            self.assertEqual(conflict["error"]["code"], "LOCAL_EDIT_CONFLICT")

            run_cli("evolve", "prepare", "--suite", str(suite), "--output", str(candidate_parent))
            candidate = candidate_parent / "candidate"
            budget = run_cli(
                "evolve",
                "evaluate",
                "--candidate",
                str(candidate),
                "--output",
                str(evaluation),
                "--max-runs",
                "2",
            )
            self.assertEqual(budget["status"], "budget_exhausted")
            candidate_limit = run_cli(
                "evolve",
                "evaluate",
                "--candidate",
                str(candidate),
                "--output",
                str(evaluation),
                "--candidate-number",
                "3",
            )
            self.assertEqual(candidate_limit["status"], "budget_exhausted")
            self.assertEqual(candidate_limit["budget"], "candidate_limit")
            broken_resources = candidate / "resources" / "text-report.json"
            broken_resources.write_text(
                '{"heading":"Broken","bullet_prefix":"* "}\n', encoding="utf-8"
            )
            broken = run_cli(
                "evolve", "evaluate", "--candidate", str(candidate), "--output", str(evaluation)
            )
            self.assertEqual(broken["status"], "failed")
            shutil.rmtree(candidate_parent)

            central = temporary / "control-center.git"
            subprocess.run(
                ["git", "clone", "--bare", str(repository), str(central)],
                check=True,
                capture_output=True,
            )
            hosted_upstream = "https://github.com/owner/control-repository.git"
            subprocess.run(
                ["git", "remote", "add", "origin", hosted_upstream],
                cwd=repository,
                check=True,
                capture_output=True,
            )
            environment_keys = (
                "GIT_CONFIG_COUNT",
                "GIT_CONFIG_KEY_0",
                "GIT_CONFIG_VALUE_0",
                "GIT_CONFIG_KEY_1",
                "GIT_CONFIG_VALUE_1",
            )
            previous_environment = {key: os.environ.get(key) for key in environment_keys}
            os.environ.update(
                {
                    "GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": f"url.{central}.insteadOf",
                    "GIT_CONFIG_VALUE_0": hosted_upstream,
                }
            )

            def restore_git_url_rewrite() -> None:
                for key, value in previous_environment.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

            self.addCleanup(restore_git_url_rewrite)

            fake_github = temporary / "fake-gh"
            write_release_fake(
                fake_github,
                temporary / "github-release.state",
                "github",
                contribution_center=central,
            )
            fake_gitlab = temporary / "fake-glab"
            write_release_fake(
                fake_gitlab,
                temporary / "gitlab-release.state",
                "gitlab",
                contribution_center=central,
            )

            candidate_parent = temporary / "candidate-one"
            run_cli("evolve", "prepare", "--suite", str(suite), "--output", str(candidate_parent))
            candidate = candidate_parent / "candidate"
            candidate_skill = candidate / "skills" / "text-report" / "SKILL.md"
            candidate_skill.write_text(
                candidate_skill.read_text(encoding="utf-8") + "Candidate release note one.\n",
                encoding="utf-8",
            )
            run_cli(
                "evolve", "evaluate", "--candidate", str(candidate), "--output", str(evaluation)
            )
            contribution_plan = temporary / "contribution-one.json"
            contribution = run_cli(
                "contribute",
                "prepare",
                "--candidate",
                str(candidate),
                "--suite",
                str(suite),
                "--evaluation",
                str(evaluation),
                "--source-repo",
                str(repository),
                "--workspace",
                str(temporary / "contribution-workspace-one"),
                "--output",
                str(contribution_plan),
            )
            self.assertTrue(contribution["branch"].startswith("codex/contribution-"))
            contributed = run_cli(
                "contribute",
                "execute",
                "--plan",
                str(contribution_plan),
                "--provider",
                "github",
                "--repository",
                "owner/control-repository",
                "--client",
                str(fake_github),
            )
            self.assertTrue(contributed["pushed"])
            verified_contribution = run_cli(
                "contribute",
                "verify",
                "--plan",
                str(contribution_plan),
                "--provider",
                "github",
                "--client",
                str(fake_github),
            )
            self.assertEqual(
                json.loads(verified_contribution["output"])[0]["headRepository"]["nameWithOwner"],
                "owner/control-repository",
            )
            prepared = run_cli(
                "release",
                "prepare",
                "--suite",
                str(suite),
                "--candidate",
                str(candidate),
                "--evaluation",
                str(evaluation),
                "--provider",
                "local",
                "--target",
                contributed["commit"],
                "--tag",
                "v1.1.0",
                "--center",
                str(center),
                "--output",
                str(plan),
            )
            tampered_plan = temporary / "tampered-release-plan.json"
            tampered = json.loads(plan.read_text(encoding="utf-8"))
            tampered["target_ref"] = "another-target"
            tampered_plan.write_text(json.dumps(tampered), encoding="utf-8")
            tampered_confirmation = run_cli(
                "release",
                "confirm",
                "--plan",
                str(tampered_plan),
                "--summary-hash",
                prepared["plan_hash"],
                "--output",
                str(temporary / "tampered-confirmation.json"),
                expected=2,
            )
            self.assertEqual(tampered_confirmation["error"]["code"], "PLAN_TAMPERED")
            run_cli(
                "release",
                "confirm",
                "--plan",
                str(plan),
                "--summary-hash",
                prepared["plan_hash"],
                "--output",
                str(confirmation),
            )
            candidate_skill.write_text("changed after confirmation\n", encoding="utf-8")
            stale = run_cli(
                "release",
                "execute",
                "--plan",
                str(plan),
                "--confirmation",
                str(confirmation),
                "--center",
                str(center),
                expected=2,
            )
            self.assertEqual(stale["error"]["code"], "CONFIRMATION_STALE")

            candidate_parent = temporary / "candidate-two"
            plan = temporary / "release-plan-two.json"
            confirmation = temporary / "confirmation-two.json"
            gitlab_upstream = "https://gitlab.com/owner/control-repository.git"
            subprocess.run(
                ["git", "remote", "set-url", "origin", gitlab_upstream],
                cwd=repository,
                check=True,
                capture_output=True,
            )
            os.environ.update(
                {
                    "GIT_CONFIG_COUNT": "2",
                    "GIT_CONFIG_KEY_1": f"url.{central}.insteadOf",
                    "GIT_CONFIG_VALUE_1": gitlab_upstream,
                }
            )
            run_cli("evolve", "prepare", "--suite", str(suite), "--output", str(candidate_parent))
            candidate = candidate_parent / "candidate"
            candidate_skill = candidate / "skills" / "text-report" / "SKILL.md"
            candidate_skill.write_text(
                candidate_skill.read_text(encoding="utf-8") + "Candidate release note two.\n",
                encoding="utf-8",
            )
            run_cli(
                "evolve", "evaluate", "--candidate", str(candidate), "--output", str(evaluation)
            )
            contribution_plan = temporary / "contribution-two.json"
            contribution = run_cli(
                "contribute",
                "prepare",
                "--candidate",
                str(candidate),
                "--suite",
                str(suite),
                "--evaluation",
                str(evaluation),
                "--source-repo",
                str(repository),
                "--workspace",
                str(temporary / "contribution-workspace-two"),
                "--output",
                str(contribution_plan),
            )
            contributed = run_cli(
                "contribute",
                "execute",
                "--plan",
                str(contribution_plan),
                "--provider",
                "gitlab",
                "--repository",
                "owner/control-repository",
                "--client",
                str(fake_gitlab),
            )
            verified_contribution = run_cli(
                "contribute",
                "verify",
                "--plan",
                str(contribution_plan),
                "--provider",
                "gitlab",
                "--client",
                str(fake_gitlab),
            )
            self.assertEqual(
                json.loads(verified_contribution["output"])[0]["target_project_id"],
                17,
            )
            prepared = run_cli(
                "release",
                "prepare",
                "--suite",
                str(suite),
                "--candidate",
                str(candidate),
                "--evaluation",
                str(evaluation),
                "--provider",
                "local",
                "--target",
                contributed["commit"],
                "--tag",
                "v1.1.0",
                "--center",
                str(center),
                "--output",
                str(plan),
            )
            run_cli(
                "release",
                "confirm",
                "--plan",
                str(plan),
                "--summary-hash",
                prepared["plan_hash"],
                "--output",
                str(confirmation),
            )
            wrong_center = run_cli(
                "release",
                "execute",
                "--plan",
                str(plan),
                "--confirmation",
                str(confirmation),
                "--center",
                str(temporary / "wrong-center"),
                expected=2,
            )
            self.assertEqual(wrong_center["error"]["code"], "CONFIRMATION_STALE")
            run_cli(
                "release",
                "execute",
                "--plan",
                str(plan),
                "--confirmation",
                str(confirmation),
                "--center",
                str(center),
            )
            run_cli("release", "verify", "--center", str(center), "--tag", "v1.1.0")
            released_package = center / "releases" / "v1.1.0" / plan.with_suffix(".zip").name
            with zipfile.ZipFile(released_package) as released_archive:
                released_skill = released_archive.read("skills/text-report/SKILL.md").decode(
                    "utf-8"
                )
            self.assertIn("Candidate release note two.", released_skill)
            reinstalled = run_cli(
                "install",
                "--source",
                str(released_package),
                "--project-root",
                str(temporary / "reinstalled-project"),
                "--harness",
                "workbuddy",
            )
            self.assertEqual(reinstalled["installation"]["version"], "1.1.0")

            for provider, client, tag in (
                ("github", fake_github, "v1.1.1"),
                ("gitlab", fake_gitlab, "v1.1.2"),
            ):
                remote_plan = temporary / f"{provider}-release.json"
                remote_confirmation = temporary / f"{provider}-confirmation.json"
                remote_prepared = run_cli(
                    "release",
                    "prepare",
                    "--suite",
                    str(suite),
                    "--candidate",
                    str(candidate),
                    "--evaluation",
                    str(evaluation),
                    "--provider",
                    provider,
                    "--target",
                    contributed["commit"],
                    "--tag",
                    tag,
                    "--repository",
                    "owner/control-repository",
                    "--output",
                    str(remote_plan),
                )
                run_cli(
                    "release",
                    "confirm",
                    "--plan",
                    str(remote_plan),
                    "--summary-hash",
                    remote_prepared["plan_hash"],
                    "--output",
                    str(remote_confirmation),
                )
                remote_result = run_cli(
                    "release",
                    "execute",
                    "--plan",
                    str(remote_plan),
                    "--confirmation",
                    str(remote_confirmation),
                    "--client",
                    str(client),
                )
                self.assertEqual(remote_result["released"], "created")
                self.assertEqual(remote_result["provider"], provider)
            ambiguous_client = temporary / "ambiguous-gh"
            write_release_fake(
                ambiguous_client,
                temporary / "ambiguous-release.state",
                "github",
                ambiguous_create=True,
            )
            ambiguous_plan = temporary / "ambiguous-release.json"
            ambiguous_confirmation = temporary / "ambiguous-confirmation.json"
            ambiguous_prepared = run_cli(
                "release",
                "prepare",
                "--suite",
                str(suite),
                "--candidate",
                str(candidate),
                "--evaluation",
                str(evaluation),
                "--provider",
                "github",
                "--target",
                contributed["commit"],
                "--tag",
                "v1.1.3",
                "--repository",
                "owner/control-repository",
                "--output",
                str(ambiguous_plan),
            )
            run_cli(
                "release",
                "confirm",
                "--plan",
                str(ambiguous_plan),
                "--summary-hash",
                ambiguous_prepared["plan_hash"],
                "--output",
                str(ambiguous_confirmation),
            )
            ambiguous_result = run_cli(
                "release",
                "execute",
                "--plan",
                str(ambiguous_plan),
                "--confirmation",
                str(ambiguous_confirmation),
                "--client",
                str(ambiguous_client),
            )
            self.assertEqual(ambiguous_result["released"], "recovered_after_ambiguous_create")
            queried = run_cli(
                "release",
                "query",
                "--provider",
                "github",
                "--client",
                str(fake_github),
                "--tag",
                "v1.1.1",
                "--repository",
                "owner/control-repository",
            )
            self.assertIn("tagName", queried["output"])
            missing_client = run_cli(
                "release",
                "query",
                "--provider",
                "gitlab",
                "--client",
                str(temporary / "missing-glab"),
                "--tag",
                "v1.1.0",
                "--repository",
                "owner/control-repository",
                expected=2,
            )
            self.assertEqual(missing_client["error"]["code"], "MISSING_DEPENDENCY")
            from selc.evolve import remote_release_argv

            adapter = remote_release_argv(
                "gitlab",
                "controlled-glab",
                "v1.1.0",
                "full-commit",
                temporary / "notes.md",
                "owner/control-repository",
            )
            self.assertIn("--no-update", adapter)
            self.assertEqual(adapter[-1], "owner/control-repository")


if __name__ == "__main__":
    unittest.main()
