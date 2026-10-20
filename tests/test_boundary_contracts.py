"""Regression coverage for contribution, deletion, and release boundary contracts."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from selc import records
from selc.errors import SelcError
from selc.evolve import (
    _parse_contribution_request,
    confirm_release,
    contribution_query_argv,
    evaluate_candidate,
    execute_contribution,
    prepare_candidate,
    prepare_contribution,
    prepare_release,
    release_local,
    verify_contribution,
    verify_local_release,
)
from selc.install import install_suite
from selc.manifest import create_suite, load_manifest, package_suite, seal_suite, suite_content_hash


def git(arguments: list[str], cwd: Path) -> str:
    completed = subprocess.run(
        ["git", *arguments], cwd=cwd, capture_output=True, check=True, text=True
    )
    return completed.stdout.strip()


class BoundaryContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def create_repository(self, name: str, subdir: str) -> tuple[Path, Path]:
        repository = self.root / name
        suite = repository if subdir == "." else repository / subdir
        create_suite(name, "1.0.0", suite, ["text-report"])
        git(["init", "-b", "main"], repository)
        git(["config", "user.name", "SELC Fixture"], repository)
        git(["config", "user.email", "fixture@example.test"], repository)
        git(["add", "."], repository)
        git(["commit", "-m", "baseline"], repository)
        return repository, suite

    def candidate_and_evaluation(self, suite: Path, label: str) -> tuple[Path, Path]:
        candidate_parent = self.root / f"{label}-candidate"
        candidate = Path(prepare_candidate(suite, candidate_parent)["candidate"])
        skill = candidate / "skills" / "text-report" / "SKILL.md"
        skill.write_text(skill.read_text(encoding="utf-8") + "Reviewed change.\n", encoding="utf-8")
        evaluation = self.root / f"{label}-evaluation.json"
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
        return candidate, evaluation

    def test_root_and_subdirectory_contributions_use_portable_suite_hashes(self) -> None:
        for subdir in (".", "suite"):
            with self.subTest(subdir=subdir):
                repository, suite = self.create_repository(
                    f"suite-{subdir.replace('.', 'root')}", subdir
                )
                candidate, evaluation = self.candidate_and_evaluation(
                    suite, subdir.replace(".", "root")
                )
                workspace = self.root / f"{subdir.replace('.', 'root')}-workspace"
                plan_path = self.root / f"{subdir.replace('.', 'root')}-plan.json"
                plan = prepare_contribution(
                    candidate,
                    suite,
                    evaluation,
                    repository,
                    workspace,
                    plan_path,
                )
                result = execute_contribution(plan_path, "local", None, None)
                self.assertTrue(result["pushed"])
                self.assertEqual(git(["rev-parse", "HEAD"], workspace), result["commit"])
                self.assertEqual(
                    git(["rev-parse", f"refs/heads/{plan['branch']}"], repository), result["commit"]
                )

    def test_provider_request_queries_bind_repository_head_base_and_commit(self) -> None:
        commit = "a" * 40
        github = json.dumps(
            [
                {
                    "number": 1,
                    "headRefName": "codex/contribution-suite",
                    "baseRefName": "main",
                    "headRefOid": commit,
                    "headRepository": {"nameWithOwner": "owner/center"},
                    "headRepositoryOwner": {"login": "owner"},
                }
            ]
        )
        self.assertEqual(
            _parse_contribution_request(
                "github",
                github,
                "github.com/owner/center",
                "codex/contribution-suite",
                "main",
                commit,
            ),
            "matching",
        )
        with self.assertRaises(SelcError) as foreign_fork:
            _parse_contribution_request(
                "github",
                github.replace("owner/center", "other/fork"),
                "github.com/owner/center",
                "codex/contribution-suite",
                "main",
                commit,
            )
        self.assertEqual(foreign_fork.exception.code, "REQUEST_CONFLICT")
        for unverified in ("", "null"):
            with self.subTest(unverified=unverified):
                with self.assertRaises(SelcError) as ambiguous:
                    _parse_contribution_request(
                        "github",
                        unverified,
                        "github.com/owner/center",
                        "codex/contribution-suite",
                        "main",
                        commit,
                    )
                self.assertEqual(ambiguous.exception.code, "REQUEST_QUERY_AMBIGUOUS")

        gitlab = json.dumps(
            [
                {
                    "state": "opened",
                    "source_branch": "codex/contribution-suite",
                    "target_branch": "main",
                    "sha": commit,
                    "source_project_id": 17,
                    "target_project_id": 17,
                }
            ]
        )
        self.assertEqual(
            _parse_contribution_request(
                "gitlab",
                gitlab,
                "gitlab.com/owner/center",
                "codex/contribution-suite",
                "main",
                commit,
            ),
            "matching",
        )
        with self.assertRaises(SelcError) as forked_merge_request:
            _parse_contribution_request(
                "gitlab",
                gitlab.replace('"source_project_id": 17', '"source_project_id": 18'),
                "gitlab.com/owner/center",
                "codex/contribution-suite",
                "main",
                commit,
            )
        self.assertEqual(forked_merge_request.exception.code, "REQUEST_CONFLICT")

        for state, expected_code in (
            ("closed", "REQUEST_INACTIVE"),
            ("merged", "REQUEST_INACTIVE"),
            (None, "REQUEST_QUERY_AMBIGUOUS"),
        ):
            with self.subTest(gitlab_state=state):
                response = json.loads(gitlab)
                if state is None:
                    del response[0]["state"]
                else:
                    response[0]["state"] = state
                with self.assertRaises(SelcError) as inactive:
                    _parse_contribution_request(
                        "gitlab",
                        json.dumps(response),
                        "gitlab.com/owner/center",
                        "codex/contribution-suite",
                        "main",
                        commit,
                    )
                self.assertEqual(inactive.exception.code, expected_code)

        github_argv = contribution_query_argv(
            "github", "gh", "github.com/owner/center", "branch", "main"
        )
        self.assertIn("headRepository", github_argv[-1])
        gitlab_argv = contribution_query_argv(
            "gitlab", "glab", "gitlab.com/owner/center", "branch", "main"
        )
        self.assertEqual(gitlab_argv[:3], ["glab", "api", "--hostname"])
        self.assertIn("projects/owner%2Fcenter/merge_requests", gitlab_argv[-1])
        self.assertIn("state=opened", gitlab_argv[-1])

    def test_nested_skill_fields_do_not_satisfy_portable_top_level_requirements(self) -> None:
        repository, suite = self.create_repository("nested-skill-suite", "suite")
        skill = suite / "skills" / "text-report" / "SKILL.md"
        skill.write_text(
            "---\nmetadata:\n  name: text-report\n  description: nested only\n---\nBody\n",
            encoding="utf-8",
        )

        with self.assertRaises(SelcError) as invalid_manifest:
            load_manifest(suite)
        self.assertEqual(invalid_manifest.exception.code, "INVALID_SKILL")
        with self.assertRaises(SelcError) as invalid_seal:
            seal_suite(suite)
        self.assertEqual(invalid_seal.exception.code, "INVALID_SKILL")
        with self.assertRaises(SelcError) as invalid_package:
            package_suite(suite, self.root / "nested-skill-suite.zip")
        self.assertEqual(invalid_package.exception.code, "INVALID_SKILL")

        skill.write_text(
            "---\nname: text-report\ndescription: valid top-level description\n---\nBody\n",
            encoding="utf-8",
        )
        seal_suite(suite)
        candidate = Path(prepare_candidate(suite, self.root / "nested-candidate")["candidate"])
        nested_candidate_skill = candidate / "skills" / "text-report" / "SKILL.md"
        nested_candidate_skill.write_text(
            "---\nmetadata:\n  name: text-report\n  description: nested only\n---\nBody\n",
            encoding="utf-8",
        )
        with self.assertRaises(SelcError) as invalid_candidate:
            evaluate_candidate(
                candidate,
                self.root / "nested-evaluation.json",
                max_runs=8,
                model_calls=0,
                candidate_number=1,
                candidate_limit=2,
                run_timeout_seconds=10,
                total_timeout_seconds=60,
            )
        self.assertEqual(invalid_candidate.exception.code, "INVALID_SKILL")

    def test_contribution_rejects_staged_or_committed_outside_changes(self) -> None:
        repository, suite = self.create_repository("contribution-tree-safety", "suite")
        outside = repository / "outside.txt"
        outside.write_text("baseline\n", encoding="utf-8")
        git(["add", "outside.txt"], repository)
        git(["commit", "-m", "outside baseline"], repository)

        candidate, evaluation = self.candidate_and_evaluation(suite, "contribution-tree-safety")
        staged_workspace = self.root / "staged-outside-workspace"
        staged_plan_path = self.root / "staged-outside-plan.json"
        staged_plan = prepare_contribution(
            candidate,
            suite,
            evaluation,
            repository,
            staged_workspace,
            staged_plan_path,
        )
        staged_outside = staged_workspace / "outside.txt"
        staged_outside.write_text("hidden staged change\n", encoding="utf-8")
        git(["add", "outside.txt"], staged_workspace)
        staged_outside.write_text("baseline\n", encoding="utf-8")
        self.assertNotIn(
            "outside.txt",
            git(
                ["diff", "--name-only", str(staged_plan["workspace_base_head"])],
                staged_workspace,
            ),
        )
        with self.assertRaises(SelcError) as staged_error:
            execute_contribution(staged_plan_path, "local", None, None)
        self.assertEqual(staged_error.exception.code, "UNSAFE_CONTRIBUTION")
        self.assertEqual(
            git(["rev-parse", "HEAD"], staged_workspace), staged_plan["workspace_base_head"]
        )

        recovery_workspace = self.root / "committed-outside-workspace"
        recovery_plan_path = self.root / "committed-outside-plan.json"
        recovery_plan = prepare_contribution(
            candidate,
            suite,
            evaluation,
            repository,
            recovery_workspace,
            recovery_plan_path,
        )

        import selc.evolve as evolve_module

        original_run_argv = evolve_module.run_argv

        def fail_push(arguments: list[str], **kwargs: object) -> str:
            if arguments[:2] == ["git", "push"]:
                raise SelcError(
                    "COMMAND_FAILED",
                    "injected push failure",
                    "retry the same contribution plan",
                )
            return original_run_argv(arguments, **kwargs)

        with (
            patch.object(evolve_module, "run_argv", side_effect=fail_push),
            self.assertRaises(SelcError) as initial_failure,
        ):
            execute_contribution(recovery_plan_path, "local", None, None)
        self.assertEqual(initial_failure.exception.code, "COMMAND_FAILED")

        recovered_outside = recovery_workspace / "outside.txt"
        recovered_outside.write_text("amended outside change\n", encoding="utf-8")
        git(["add", "outside.txt"], recovery_workspace)
        git(["commit", "--amend", "--no-edit"], recovery_workspace)
        recovered_outside.write_text("baseline\n", encoding="utf-8")
        self.assertNotIn(
            "outside.txt",
            git(
                ["diff", "--name-only", str(recovery_plan["workspace_base_head"])],
                recovery_workspace,
            ),
        )

        with self.assertRaises(SelcError) as committed_error:
            execute_contribution(recovery_plan_path, "local", None, None)
        self.assertEqual(committed_error.exception.code, "UNSAFE_CONTRIBUTION")

        git(["checkout", "--", "outside.txt"], recovery_workspace)
        self.assertEqual(git(["diff", "--name-only"], recovery_workspace), "")
        with self.assertRaises(SelcError) as clean_workspace_error:
            execute_contribution(recovery_plan_path, "local", None, None)
        self.assertEqual(clean_workspace_error.exception.code, "UNSAFE_CONTRIBUTION")
        remote_branch = subprocess.run(
            [
                "git",
                "show-ref",
                "--verify",
                "--quiet",
                f"refs/heads/{recovery_plan['branch']}",
            ],
            cwd=repository,
            check=False,
        )
        self.assertNotEqual(remote_branch.returncode, 0)

    def test_empty_candidate_skill_cannot_be_evaluated_or_contributed(self) -> None:
        _, suite = self.create_repository("invalid-candidate-suite", "suite")
        candidate_parent = self.root / "invalid-candidate"
        candidate = Path(prepare_candidate(suite, candidate_parent)["candidate"])
        (candidate / "skills" / "text-report" / "SKILL.md").write_text(
            "---\nname: text-report\ndescription: valid frontmatter\n---\n",
            encoding="utf-8",
        )
        evaluation_path = self.root / "invalid-candidate-evaluation.json"
        with self.assertRaises(SelcError) as invalid_evaluation:
            evaluate_candidate(
                candidate,
                evaluation_path,
                max_runs=8,
                model_calls=0,
                candidate_number=1,
                candidate_limit=2,
                run_timeout_seconds=10,
                total_timeout_seconds=60,
            )
        self.assertEqual(invalid_evaluation.exception.code, "INVALID_SKILL")
        self.assertFalse(evaluation_path.exists())

    def test_contribution_rejects_workspace_changes_outside_candidate_surface(self) -> None:
        repository, suite = self.create_repository("safe-suite", "suite")
        candidate, evaluation = self.candidate_and_evaluation(suite, "safe")
        workspace = self.root / "safe-workspace"
        plan_path = self.root / "safe-plan.json"
        prepare_contribution(candidate, suite, evaluation, repository, workspace, plan_path)
        (workspace / "README.md").write_text("not evaluated\n", encoding="utf-8")
        with self.assertRaises(SelcError) as captured:
            execute_contribution(plan_path, "local", None, None)
        self.assertEqual(captured.exception.code, "UNSAFE_CONTRIBUTION")

    def test_contribution_reuses_its_only_commit_after_a_temporary_push_failure(self) -> None:
        repository, suite = self.create_repository("retry-suite", "suite")
        candidate, evaluation = self.candidate_and_evaluation(suite, "retry")
        workspace = self.root / "retry-workspace"
        plan_path = self.root / "retry-plan.json"
        plan = prepare_contribution(candidate, suite, evaluation, repository, workspace, plan_path)

        import selc.evolve as evolve_module

        original_run_argv = evolve_module.run_argv
        push_attempts = 0

        def fail_first_push(arguments: list[str], **kwargs) -> str:
            nonlocal push_attempts
            if arguments[:2] == ["git", "push"]:
                push_attempts += 1
                if push_attempts == 1:
                    raise SelcError(
                        "COMMAND_FAILED",
                        "injected temporary push failure",
                        "retry the same contribution plan",
                    )
            return original_run_argv(arguments, **kwargs)

        with (
            patch.object(evolve_module, "run_argv", side_effect=fail_first_push),
            self.assertRaises(SelcError) as interrupted,
        ):
            execute_contribution(plan_path, "local", None, None)
        self.assertEqual(interrupted.exception.code, "COMMAND_FAILED")
        committed_once = git(["rev-list", "--count", "HEAD"], workspace)

        recovered = execute_contribution(plan_path, "local", None, None)
        self.assertTrue(recovered["pushed"])
        self.assertEqual(git(["rev-list", "--count", "HEAD"], workspace), committed_once)
        self.assertEqual(
            git(["rev-parse", f"refs/heads/{plan['branch']}"], repository), recovered["commit"]
        )

        skill = workspace / plan["suite_subdir"] / "skills" / "text-report" / "SKILL.md"
        skill.write_text(skill.read_text(encoding="utf-8") + "unassessed edit\n", encoding="utf-8")
        with self.assertRaises(SelcError) as stale_workspace:
            execute_contribution(plan_path, "local", None, None)
        self.assertEqual(stale_workspace.exception.code, "UNEVALUATED_WORKSPACE_CHANGE")

    def test_hosted_contribution_pushes_the_bound_center_before_provider_create(self) -> None:
        repository, suite = self.create_repository("hosted-suite", "suite")
        central = self.root / "central.git"
        git(["clone", "--bare", str(repository), str(central)], self.root)
        hosted_upstream = "https://github.com/owner/central.git"
        git(["remote", "add", "origin", hosted_upstream], repository)
        candidate, evaluation = self.candidate_and_evaluation(suite, "hosted")
        workspace = self.root / "hosted-workspace"
        plan_path = self.root / "hosted-plan.json"
        rewrite = {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": f"url.{central}.insteadOf",
            "GIT_CONFIG_VALUE_0": hosted_upstream,
        }
        with patch.dict(os.environ, rewrite | {"GH_HOST": "enterprise.invalid"}, clear=False):
            plan = prepare_contribution(
                candidate,
                suite,
                evaluation,
                repository,
                workspace,
                plan_path,
            )
            self.assertEqual(plan["push_upstream"], hosted_upstream)
            self.assertEqual(plan["upstream_identity"]["repository"], "owner/central")
            workspace_head = git(["rev-parse", "HEAD"], workspace)
            with self.assertRaises(SelcError) as mismatch:
                execute_contribution(plan_path, "github", "owner/wrong", sys.executable)
            self.assertEqual(mismatch.exception.code, "UPSTREAM_IDENTITY_MISMATCH")
            with self.assertRaises(SelcError) as wrong_provider:
                execute_contribution(plan_path, "gitlab", "owner/central", sys.executable)
            self.assertEqual(wrong_provider.exception.code, "UPSTREAM_IDENTITY_MISMATCH")
            self.assertEqual(git(["rev-parse", "HEAD"], workspace), workspace_head)

            client_log = self.root / "provider.jsonl"
            request_state = self.root / "provider-request-created"
            client = self.root / "fake-gh"
            client.write_text(
                "#!" + sys.executable + "\n"
                "import json, subprocess, sys\n"
                f"log = {str(client_log)!r}\n"
                f"central = {str(central)!r}\n"
                f"request_state = {str(request_state)!r}\n"
                "arguments = sys.argv[1:]\n"
                "repository = arguments[arguments.index('--repo') + 1]\n"
                "host, namespace = repository.split('/', maxsplit=1)\n"
                "with open(log, 'a', encoding='utf-8') as handle:\n"
                "    handle.write(json.dumps({'argv': arguments, 'host': host, 'namespace': namespace}) + '\\n')\n"
                "if host != 'github.com' or namespace != 'owner/central':\n"
                "    sys.exit(8)\n"
                "if arguments[:2] == ['pr', 'list']:\n"
                "    if not __import__('pathlib').Path(request_state).exists():\n"
                "        print('[]')\n"
                "    else:\n"
                "        branch = arguments[arguments.index('--head') + 1]\n"
                "        commit = subprocess.check_output(['git', '--git-dir', central, 'rev-parse', 'refs/heads/' + branch], text=True).strip()\n"
                "        print(json.dumps([{'number': 1, 'headRefName': branch, 'baseRefName': 'main', 'headRefOid': commit, 'headRepository': {'nameWithOwner': 'owner/central'}, 'headRepositoryOwner': {'login': 'owner'}}]))\n"
                "elif arguments[:2] == ['pr', 'create']:\n"
                "    branch = arguments[arguments.index('--head') + 1]\n"
                "    code = subprocess.run(['git', '--git-dir', central, 'show-ref', '--verify', '--quiet', 'refs/heads/' + branch]).returncode\n"
                "    if code == 0:\n"
                "        __import__('pathlib').Path(request_state).touch()\n"
                "    sys.exit(7 if code == 0 else code)\n"
                "else:\n"
                "    sys.exit(9)\n",
                encoding="utf-8",
            )
            client.chmod(0o700)
            result = execute_contribution(plan_path, "github", "owner/central", str(client))
            verified = verify_contribution(plan_path, "github", str(client))

        self.assertEqual(
            git(["rev-parse", f"refs/heads/{plan['branch']}"], central), result["commit"]
        )
        absent_from_author = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{plan['branch']}"],
            cwd=repository,
            check=False,
        )
        self.assertNotEqual(absent_from_author.returncode, 0)
        self.assertIn("github.com/owner/central", result["query"])
        self.assertEqual(result["request"], "recovered_after_ambiguous_create")
        provider_requests = [json.loads(line) for line in client_log.read_text().splitlines()]
        self.assertTrue(all(request["host"] == "github.com" for request in provider_requests))
        self.assertTrue(
            all(request["namespace"] == "owner/central" for request in provider_requests)
        )
        self.assertEqual(
            sum(request["argv"][:2] == ["pr", "create"] for request in provider_requests), 1
        )
        self.assertEqual(verified["provider_repository"], "github.com/owner/central")

    def test_remote_contribution_rejects_unclassified_host_before_commit_or_query(self) -> None:
        repository, suite = self.create_repository("unknown-host-suite", "suite")
        central = self.root / "unknown-host-central.git"
        git(["clone", "--bare", str(repository), str(central)], self.root)
        hosted_upstream = "https://gitlab.example.invalid/owner/central.git"
        git(["remote", "add", "origin", hosted_upstream], repository)
        candidate, evaluation = self.candidate_and_evaluation(suite, "unknown-host")
        workspace = self.root / "unknown-host-workspace"
        plan_path = self.root / "unknown-host-plan.json"
        rewrite = {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": f"url.{central}.insteadOf",
            "GIT_CONFIG_VALUE_0": hosted_upstream,
        }
        client_log = self.root / "unknown-host-provider.log"
        client = self.root / "unknown-host-client"
        client.write_text(
            "#!" + sys.executable + "\n"
            f"open({str(client_log)!r}, 'a', encoding='utf-8').write('called\\n')\n"
            "raise SystemExit(9)\n",
            encoding="utf-8",
        )
        client.chmod(0o700)

        with patch.dict(os.environ, rewrite, clear=False):
            plan = prepare_contribution(
                candidate,
                suite,
                evaluation,
                repository,
                workspace,
                plan_path,
            )
            workspace_head = git(["rev-parse", "HEAD"], workspace)
            with self.assertRaises(SelcError) as execution:
                execute_contribution(plan_path, "github", "owner/central", str(client))
            self.assertEqual(execution.exception.code, "UNSUPPORTED_HOSTED_PROVIDER")
            with self.assertRaises(SelcError) as verification:
                verify_contribution(plan_path, "github", str(client))
            self.assertEqual(verification.exception.code, "UNSUPPORTED_HOSTED_PROVIDER")

        self.assertEqual(git(["rev-parse", "HEAD"], workspace), workspace_head)
        self.assertFalse(client_log.exists())
        branch_ref = f"refs/heads/{plan['branch']}"
        central_branch = subprocess.run(
            ["git", "--git-dir", str(central), "show-ref", "--verify", "--quiet", branch_ref],
            check=False,
        )
        self.assertNotEqual(central_branch.returncode, 0)

    def test_gitlab_closed_request_is_not_reused_or_recreated(self) -> None:
        repository, suite = self.create_repository("closed-gitlab-suite", "suite")
        central = self.root / "closed-gitlab-central.git"
        git(["clone", "--bare", str(repository), str(central)], self.root)
        hosted_upstream = "https://gitlab.com/owner/central.git"
        git(["remote", "add", "origin", hosted_upstream], repository)
        candidate, evaluation = self.candidate_and_evaluation(suite, "closed-gitlab")
        workspace = self.root / "closed-gitlab-workspace"
        plan_path = self.root / "closed-gitlab-plan.json"
        rewrite = {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": f"url.{central}.insteadOf",
            "GIT_CONFIG_VALUE_0": hosted_upstream,
        }

        with patch.dict(os.environ, rewrite, clear=False):
            plan = prepare_contribution(
                candidate,
                suite,
                evaluation,
                repository,
                workspace,
                plan_path,
            )
            query_log = self.root / "closed-gitlab-query.log"
            create_log = self.root / "closed-gitlab-create.log"
            client = self.root / "fake-closed-glab"
            client.write_text(
                "#!" + sys.executable + "\n"
                "import json, subprocess, sys\n"
                f"central = {str(central)!r}\n"
                f"branch = {str(plan['branch'])!r}\n"
                f"base = {str(plan['base_branch'])!r}\n"
                f"query_log = {str(query_log)!r}\n"
                f"create_log = {str(create_log)!r}\n"
                "arguments = sys.argv[1:]\n"
                "if arguments[:1] == ['api']:\n"
                "    with open(query_log, 'a', encoding='utf-8') as handle:\n"
                "        handle.write(json.dumps(arguments) + '\\n')\n"
                "    commit = subprocess.check_output(\n"
                "        ['git', '--git-dir', central, 'rev-parse', 'refs/heads/' + branch], text=True\n"
                "    ).strip()\n"
                "    print(json.dumps([{'state': 'closed', 'source_branch': branch, 'target_branch': base, 'sha': commit, 'source_project_id': 17, 'target_project_id': 17}]))\n"
                "    raise SystemExit(0)\n"
                "if arguments[:2] == ['mr', 'create']:\n"
                "    open(create_log, 'w', encoding='utf-8').write('unexpected create')\n"
                "    raise SystemExit(9)\n"
                "raise SystemExit(8)\n",
                encoding="utf-8",
            )
            client.chmod(0o700)

            with self.assertRaises(SelcError) as closed_execute:
                execute_contribution(plan_path, "gitlab", "owner/central", str(client))
            self.assertEqual(closed_execute.exception.code, "REQUEST_INACTIVE")
            with self.assertRaises(SelcError) as closed_verify:
                verify_contribution(plan_path, "gitlab", str(client))
            self.assertEqual(closed_verify.exception.code, "REQUEST_INACTIVE")

        self.assertFalse(create_log.exists())
        query_argv = json.loads(query_log.read_text(encoding="utf-8").splitlines()[0])
        self.assertIn("state=opened", query_argv[-1])

    def test_release_center_boundary_rejects_an_escaping_releases_symlink(self) -> None:
        repository, suite = self.create_repository("release-suite", "suite")
        candidate, evaluation = self.candidate_and_evaluation(suite, "release")
        for section in ("skills", "resources"):
            target = suite / section
            candidate_section = candidate / section
            shutil.rmtree(target)
            shutil.copytree(candidate_section, target)
        manifest_path = suite / "suite.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["content_hash"] = suite_content_hash(suite)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        git(["add", "."], repository)
        git(["commit", "-m", "candidate"], repository)

        center = self.root / "center"
        outside = self.root / "outside"
        center.mkdir()
        outside.mkdir()
        (center / "releases").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(SelcError) as prepared:
            prepare_release(
                suite,
                candidate,
                evaluation,
                "local",
                "HEAD",
                "nested/v1.0.0",
                self.root / "escaped-plan.json",
                center,
                None,
            )
        self.assertEqual(prepared.exception.code, "UNSAFE_TAG")

        (center / "releases").unlink()
        plan_path = self.root / "release-plan.json"
        plan = prepare_release(
            suite,
            candidate,
            evaluation,
            "local",
            "HEAD",
            "nested/v1.0.0",
            plan_path,
            center,
            None,
        )
        confirmation_path = self.root / "release-confirmation.json"
        confirm_release(plan_path, plan["plan_hash"], confirmation_path)
        (center / "releases").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(SelcError) as execution:
            release_local(plan_path, confirmation_path, center)
        self.assertEqual(execution.exception.code, "UNSAFE_TAG")
        with self.assertRaises(SelcError) as verification:
            verify_local_release(center, "nested/v1.0.0")
        self.assertEqual(verification.exception.code, "UNSAFE_TAG")

    def test_local_release_stages_atomically_and_refuses_corrupt_existing_assets(self) -> None:
        repository, suite = self.create_repository("atomic-release-suite", "suite")
        candidate, evaluation = self.candidate_and_evaluation(suite, "atomic-release")
        for section in ("skills", "resources"):
            destination = suite / section
            shutil.rmtree(destination)
            shutil.copytree(candidate / section, destination)
        manifest_path = suite / "suite.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["content_hash"] = suite_content_hash(suite)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        git(["add", "."], repository)
        git(["commit", "-m", "candidate"], repository)

        center = self.root / "atomic-release-center"
        plan_path = self.root / "atomic-release-plan.json"
        plan = prepare_release(
            suite,
            candidate,
            evaluation,
            "local",
            "HEAD",
            "nested/v1.0.0",
            plan_path,
            center,
            None,
        )
        confirmation_path = self.root / "atomic-release-confirmation.json"
        confirm_release(plan_path, plan["plan_hash"], confirmation_path)

        import selc.evolve as evolve_module

        original_copy2 = evolve_module.shutil.copy2

        def fail_staging_copy(source: Path, destination: Path, *args, **kwargs):
            if destination.parent.name.startswith(".selc-release-"):
                raise OSError("injected staging copy failure")
            return original_copy2(source, destination, *args, **kwargs)

        with (
            patch.object(evolve_module.shutil, "copy2", side_effect=fail_staging_copy),
            self.assertRaises(OSError),
        ):
            release_local(plan_path, confirmation_path, center)

        release_directory = center / "releases" / "nested" / "v1.0.0"
        self.assertFalse(release_directory.exists())
        self.assertEqual(list(center.glob(".selc-release-*")), [])

        released = release_local(plan_path, confirmation_path, center)
        self.assertEqual(released["released"], "created")
        asset = Path(released["location"]) / Path(plan["package"]).name
        asset.write_bytes(b"corrupt")
        with self.assertRaises(SelcError) as verification:
            verify_local_release(center, "nested/v1.0.0")
        self.assertEqual(verification.exception.code, "RELEASE_INVALID")
        with self.assertRaises(SelcError) as retry:
            release_local(plan_path, confirmation_path, center)
        self.assertEqual(retry.exception.code, "RELEASE_INVALID")

    def test_delete_cannot_orphan_a_session_started_during_its_lease_recheck(self) -> None:
        suite = self.root / "session-suite"
        create_suite("session-suite", "1.0.0", suite, ["text-report"])
        project = self.root / "project"
        install_suite(source=suite, project_root=project, harness="codex", ref=None)
        database = records.open_database(project)
        records.start_task(
            database,
            task_id="closed-task",
            suite_id="session-suite",
            initiator="fixture",
            evidence="fixture",
            is_test=True,
        )
        records.close_task(database, "closed-task")
        database.close()

        responses: list[dict[str, object]] = []

        class InterleavingConnection(sqlite3.Connection):
            triggered = False

            def execute(self, sql: str, parameters: object = ()) -> sqlite3.Cursor:
                cursor = super().execute(sql, parameters)
                if not self.triggered and "FROM sessions WHERE status = 'active'" in sql:
                    self.triggered = True
                    completed = subprocess.run(
                        [
                            sys.executable,
                            str(REPO_ROOT / "scripts" / "selc.py"),
                            "event",
                            "--project-root",
                            str(project),
                            "--task-id",
                            "closed-task",
                            "--kind",
                            "session_start",
                            "--evidence",
                            "race attempt",
                        ],
                        cwd=REPO_ROOT,
                        capture_output=True,
                        check=False,
                        text=True,
                    )
                    responses.append(json.loads(completed.stdout))
                return cursor

        connection = sqlite3.connect(records.database_path(project), factory=InterleavingConnection)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        deleted = records.delete_records(
            connection, suite_id="session-suite", before=None, apply=True
        )
        session_count = connection.execute(
            "SELECT COUNT(*) FROM sessions WHERE status = 'active'"
        ).fetchone()[0]
        task_count = connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        connection.close()
        self.assertEqual(deleted["deleted"]["local_tasks"], 1)
        self.assertIn(responses[0]["error"]["code"], {"DATABASE_BUSY", "TASK_CLOSED"})
        self.assertEqual(session_count, 0)
        self.assertEqual(task_count, 0)
