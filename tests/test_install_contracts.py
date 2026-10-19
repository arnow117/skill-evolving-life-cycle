"""Focused installation, portable provenance, protocol, and checker contracts."""

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
from selc.install import current_installation, install_suite, recover_installation
from selc.manifest import create_suite, package_suite, render_text_report, suite_content_hash
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
        (suite / "skills" / "gamma" / "SKILL.md").write_text("# gamma\n", encoding="utf-8")
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
