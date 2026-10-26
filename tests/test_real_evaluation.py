"""Contracts for T011 real-evaluation qualification and portable packaging.

The cases in this module are deliberately synthetic.  They verify that local
fixture output can exercise diagnostics without being accepted as production
evidence, and that each public lifecycle consumer uses the same qualification
function before its side effect.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import tomllib
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from selc.creator import prepare_creator_request, verify_creator_result
from selc.errors import SelcError
from selc.evaluation import (
    CODEX_ALPHA11_VERSION,
    _action_request_from_prompt,
    _codex_cli_launch_trust_failure,
    _codex_cli_prompt,
    _codex_hook_trust_changed,
    _codex_installed_hook_definitions,
    _fixed_codex_cli_command,
    _fixed_codex_environment,
    _match_codex_native_policy,
    _native_publication_result_failure,
    _native_window_failure,
    _prepare_codex_hook_trust_binding,
    _qualification_support,
    _read_attempt_native_context,
    bind_real_evaluation_candidate,
    build_codex_policy_template,
    collect_evaluation_attempt,
    desktop_request,
    finalize_real_evaluation,
    launch_codex_cli_attempt,
    native_evidence_template,
    prepare_evaluation_attempt,
    prepare_real_evaluation_plan,
    public_evaluation_summary,
    validate_real_evaluation,
)
from selc.evolve import (
    confirm_release,
    evaluate_candidate,
    execute_contribution,
    prepare_candidate,
    prepare_contribution,
    prepare_release,
    release_local,
)
from selc.install import current_installation
from selc.managed_runtime import _trusted_posix_account_home
from selc.manifest import create_suite, load_manifest, package_suite, seal_suite
from selc.native_adapter import receive_native_hook
from selc.util import json_hash


def git(arguments: list[str], cwd: Path) -> str:
    completed = subprocess.run(
        ["git", *arguments], cwd=cwd, capture_output=True, check=True, text=True
    )
    return completed.stdout.strip()


class RealEvaluationContractsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _repository_and_candidate(self) -> tuple[Path, Path, Path, Path]:
        repository = self.root / "repository"
        suite = repository / "suite"
        create_suite("evaluation-suite", "1.0.0", suite, ["text-report"])
        git(["init", "-b", "main"], repository)
        git(["config", "user.name", "SELC Fixture"], repository)
        git(["config", "user.email", "fixture@example.test"], repository)
        git(["add", "."], repository)
        git(["commit", "-m", "baseline"], repository)

        candidate_parent = self.root / "candidate-parent"
        candidate = Path(prepare_candidate(suite, candidate_parent)["candidate"])
        skill = candidate / "skills" / "text-report" / "SKILL.md"
        skill.write_text(
            skill.read_text(encoding="utf-8") + "Synthetic improvement.\n", encoding="utf-8"
        )
        evaluation = self.root / "fixture-evaluation.json"
        evaluate_candidate(
            candidate,
            evaluation,
            max_runs=8,
            model_calls=0,
            candidate_number=1,
            candidate_limit=2,
            run_timeout_seconds=10,
            total_timeout_seconds=60,
        )
        return repository, suite, candidate, evaluation

    def _real_plan_specification(
        self,
        input_path: Path,
        *,
        adapter: str = "codex-desktop",
        max_attempts: int = 4,
    ) -> Path:
        specification = self.root / f"{adapter}-cases.json"
        matrix: dict[str, object] = {
            "id": "synthetic-matrix",
            "adapter": adapter,
            "host": "codex",
            "host_version": "synthetic",
            "surface": "cli" if adapter == "codex-cli" else "desktop",
            "model": "synthetic-model",
            "tools_policy_digest": "0" * 64,
        }
        if adapter == "codex-cli":
            matrix["executor"] = {
                "binary": str(Path(sys.executable).resolve()),
                "version": "synthetic-python",
            }
        specification.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "max_attempts": max_attempts,
                    "cases": [
                        {
                            "id": "synthetic-case",
                            "collection": "holdout",
                            "input": str(input_path),
                            "checker": {
                                "name": "json-item-summary",
                                "version": "1",
                                "rules": ["count"],
                            },
                        }
                    ],
                    "matrix": [matrix],
                }
            ),
            encoding="utf-8",
        )
        return specification

    def _creator_tooling_roots(self) -> tuple[Path, Path]:
        skill_creator = self.root / "skill-creator"
        validator = skill_creator / "scripts" / "quick_validate.py"
        validator.parent.mkdir(parents=True)
        (skill_creator / "SKILL.md").write_text("# Synthetic creator\n", encoding="utf-8")
        validator.write_text("print('synthetic validator')\n", encoding="utf-8")
        python_runtime = self.root / "creator-python-runtime"
        python_runtime.mkdir()
        return skill_creator, python_runtime

    def _add_supported_codex_policy_template(self, specification: Path, name: str) -> None:
        python_root = self.root / name
        python_root.mkdir()
        value = json.loads(specification.read_text(encoding="utf-8"))
        matrix = value["matrix"][0]
        matrix["host_version"] = "0.163.0-alpha.11"
        matrix.pop("tools_policy_digest", None)
        matrix["tools_policy_template"] = build_codex_policy_template(
            "0.163.0-alpha.11", python_root
        )
        specification.write_text(json.dumps(value), encoding="utf-8")

    def _prepared_hook_trust_attempt(
        self,
    ) -> tuple[Path, Path, dict[str, object], dict[str, object]]:
        suite = self.root / "hook-trust-source"
        create_suite("hook-trust-source", "1.0.0", suite, ["text-report"])
        input_path = self.root / "hook-trust-input.json"
        input_path.write_text('{"items": []}', encoding="utf-8")
        specification = self._real_plan_specification(input_path, adapter="codex-cli")
        account_home = (self.root / "synthetic-account").resolve()
        (account_home / ".codex").mkdir(parents=True)
        with patch("selc.evaluation._trusted_posix_account_home", return_value=account_home):
            self._add_supported_codex_policy_template(specification, "hook-trust-python")
            prepared = prepare_real_evaluation_plan(
                suite,
                specification,
                self.root / "hook-trust-plan",
            )
            candidate = Path(
                prepare_candidate(suite, self.root / "hook-trust-candidate")["candidate"]
            )
            binding_path = self.root / "hook-trust-binding.json"
            bind_real_evaluation_candidate(Path(prepared["plan"]), candidate, binding_path)
            attempt_path = self.root / "hook-trust-plan" / "attempts" / "first"
            prepare_evaluation_attempt(
                Path(prepared["plan"]),
                binding_path,
                "synthetic-case",
                "synthetic-matrix",
                1,
                attempt_path,
            )
        state = json.loads((attempt_path / "attempt.json").read_text(encoding="utf-8"))
        plan = json.loads(Path(prepared["plan"]).read_text(encoding="utf-8"))
        return account_home, attempt_path, state, plan["matrix"][0]

    def _write_normal_hook_trust_state(
        self,
        account_home: Path,
        state: dict[str, object],
        *,
        replacement_source: Path | None = None,
        disabled: bool = False,
        extra_same_source: bool = False,
    ) -> list[dict[str, object]]:
        project_root = Path(str(state["project_root"]))
        installation = current_installation(project_root, str(state["installation"]["suite_id"]))
        definitions, _source_hash = _codex_installed_hook_definitions(
            project_root / ".codex" / "hooks.json",
            installation["owned_hooks"],
        )
        source = replacement_source or (project_root / ".codex" / "hooks.json")
        lines: list[str] = []
        for index, definition in enumerate(definitions):
            state_key = str(definition["state_key"])
            if replacement_source is not None:
                state_key = state_key.replace(
                    str(project_root / ".codex" / "hooks.json"),
                    str(replacement_source.resolve()),
                    1,
                )
            lines.extend(
                (
                    f"[hooks.state.{json.dumps(state_key)}]",
                    f'trusted_hash = "sha256:{index + 1:064x}"',
                )
            )
            if disabled and index == 0:
                lines.append("disabled = true")
            lines.append("")
        if extra_same_source:
            extra_key = f"{source.resolve()}:stop:99:0"
            lines.extend(
                (
                    f"[hooks.state.{json.dumps(extra_key)}]",
                    'trusted_hash = "sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"',
                    "",
                )
            )
        unrelated_key = (
            f"{(self.root / 'other-project' / '.codex' / 'hooks.json').resolve()}:stop:0:0"
        )
        lines.extend(
            (
                f"[hooks.state.{json.dumps(unrelated_key)}]",
                'trusted_hash = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"',
                "",
            )
        )
        config_path = account_home / ".codex" / "config.toml"
        config_path.write_text("\n".join(lines), encoding="utf-8")
        return definitions

    def test_codex_policy_template_freezes_only_supported_path_parameters(self) -> None:
        python_root = self.root / "evaluation-python"
        python_root.mkdir()

        template = build_codex_policy_template("0.163.0-alpha.11", python_root)

        self.assertEqual(template["schema_version"], 1)
        self.assertEqual(template["host"], "codex")
        self.assertEqual(template["host_version"], "0.163.0-alpha.11")
        self.assertEqual(
            template["parameters"],
            {
                "attempt_project": {"kind": "canonical-attempt-project"},
                "codex_arg0": {"kind": "current-account-alpha11-bootstrap"},
            },
        )
        self.assertTrue(template["cli"]["ignore_user_config"])
        self.assertEqual(template["policy"]["approval_policy"], "never")
        self.assertEqual(
            template["hook_trust"]["mode"],
            "normal-state-inheritance-v1",
        )

    def test_codex_template_without_normal_hook_trust_remains_unverified(self) -> None:
        python_root = self.root / "legacy-template-python"
        python_root.mkdir()
        template = build_codex_policy_template("0.163.0-alpha.11", python_root)
        template.pop("hook_trust")

        self.assertEqual(
            _qualification_support(
                {
                    "host": "codex",
                    "qualification_support": "supported",
                    "tools_policy_template": template,
                }
            ),
            "unverified",
        )

    def test_hook_trust_binding_copies_only_exact_reviewed_values_to_quoted_cli_keys(self) -> None:
        account_home, attempt_path, state, matrix = self._prepared_hook_trust_attempt()
        definitions = self._write_normal_hook_trust_state(account_home, state)
        with patch("selc.evaluation._trusted_posix_account_home", return_value=account_home):
            trust_binding = _prepare_codex_hook_trust_binding(state, matrix)
        command = _fixed_codex_cli_command(
            Path(sys.executable).resolve(),
            matrix,
            state,
            trust_binding,
        )

        self.assertEqual(
            trust_binding["states"][0]["trusted_hash"],
            "sha256:0000000000000000000000000000000000000000000000000000000000000001",
        )
        trust_configuration = next(
            command[index + 1]
            for index, argument in enumerate(command)
            if argument == "-c" and command[index + 1].startswith("hooks.state=")
        )
        parsed_configuration = tomllib.loads(trust_configuration)
        self.assertEqual(command.count("-c"), 8)
        self.assertEqual(
            parsed_configuration["hooks"]["state"],
            {
                definition["state_key"]: {"trusted_hash": f"sha256:{index + 1:064x}"}
                for index, definition in enumerate(definitions)
            },
        )
        self.assertNotIn(str(self.root / "other-project"), trust_configuration)

        launch = {
            "schema_version": 1,
            "attempt_id": (
                f"{state['plan_hash']}:{state['case_id']}:"
                f"{state['matrix_id']}:{state['attempt_number']}"
            ),
            "command": command,
            "command_hash": json_hash(command),
            "environment": {"PATH": "/synthetic"},
            "environment_hash": json_hash({"PATH": "/synthetic"}),
            "timeout_seconds": state["case"]["run_timeout_seconds"],
            "hook_trust": trust_binding,
            "hook_trust_hash": json_hash(trust_binding),
            "prepared_at": "synthetic",
        }
        state["launch_claim_hash"] = json_hash(launch)
        (attempt_path / "launch-claim.json").write_text(json.dumps(launch), encoding="utf-8")
        self.assertIsNone(_codex_cli_launch_trust_failure(state, matrix))

        launch["command"] = [*command, "--unexpected"]
        (attempt_path / "launch-claim.json").write_text(json.dumps(launch), encoding="utf-8")
        self.assertEqual(_codex_cli_launch_trust_failure(state, matrix), "launch-claim-tampered")

    def test_hook_trust_binding_rejects_missing_disabled_wrong_or_extra_state(self) -> None:
        account_home, _attempt_path, state, matrix = self._prepared_hook_trust_attempt()
        config_path = account_home / ".codex" / "config.toml"
        project_root = Path(str(state["project_root"]))

        with patch("selc.evaluation._trusted_posix_account_home", return_value=account_home):
            with self.assertRaises(SelcError) as missing:
                _prepare_codex_hook_trust_binding(state, matrix)
            self.assertEqual(missing.exception.code, "HOOK_TRUST_SOURCE_UNAVAILABLE")

            self._write_normal_hook_trust_state(account_home, state, disabled=True)
            with self.assertRaises(SelcError) as disabled:
                _prepare_codex_hook_trust_binding(state, matrix)
            self.assertEqual(disabled.exception.code, "HOOK_TRUST_SOURCE_INVALID")

            wrong_source = self.root / "wrong-source" / "hooks.json"
            self._write_normal_hook_trust_state(
                account_home,
                state,
                replacement_source=wrong_source,
            )
            with self.assertRaises(SelcError) as wrong:
                _prepare_codex_hook_trust_binding(state, matrix)
            self.assertEqual(wrong.exception.code, "HOOK_TRUST_SOURCE_INVALID")

            self._write_normal_hook_trust_state(account_home, state, extra_same_source=True)
            with self.assertRaises(SelcError) as extra:
                _prepare_codex_hook_trust_binding(state, matrix)
            self.assertEqual(extra.exception.code, "HOOK_TRUST_SOURCE_INVALID")

        self.assertTrue(config_path.is_file())
        self.assertTrue((project_root / ".codex" / "hooks.json").is_file())

    def test_hook_trust_binding_rechecks_definition_and_selected_state_before_launch(self) -> None:
        account_home, _attempt_path, state, matrix = self._prepared_hook_trust_attempt()
        self._write_normal_hook_trust_state(account_home, state)
        with patch("selc.evaluation._trusted_posix_account_home", return_value=account_home):
            trust_binding = _prepare_codex_hook_trust_binding(state, matrix)

            config_path = account_home / ".codex" / "config.toml"
            config_path.write_text(
                config_path.read_text(encoding="utf-8").replace("0001", "0009", 1),
                encoding="utf-8",
            )
            self.assertTrue(_codex_hook_trust_changed(trust_binding, state, matrix))

            self._write_normal_hook_trust_state(account_home, state)
            hook_path = Path(str(state["project_root"])) / ".codex" / "hooks.json"
            hooks = json.loads(hook_path.read_text(encoding="utf-8"))
            hooks["hooks"]["Stop"][0]["unexpected"] = True
            hook_path.write_text(json.dumps(hooks), encoding="utf-8")
            self.assertTrue(_codex_hook_trust_changed(trust_binding, state, matrix))

    def test_codex_policy_template_matches_only_its_two_runtime_parameters(self) -> None:
        python_root = self.root / "template-python"
        python_root.mkdir()
        template = build_codex_policy_template("0.163.0-alpha.11", python_root)
        account_home = Path(str(template["account_home"]))

        def policy_for(project: Path, suffix: str) -> dict[str, object]:
            entries = [
                {
                    "path": {"type": "special", "value": {"kind": "minimal"}},
                    "access": "read",
                },
                {
                    "path": {"type": "path", "path": str(python_root.resolve())},
                    "access": "read",
                },
                {
                    "path": {"type": "path", "path": str(project.resolve())},
                    "access": "write",
                },
                {
                    "path": {
                        "type": "path",
                        "path": str(
                            account_home / ".codex" / "tmp" / "arg0-macos" / f"codex-arg0{suffix}"
                        ),
                    },
                    "access": "read",
                },
            ]
            return {
                "permission_profile": {
                    "type": "managed",
                    "file_system": {"type": "restricted", "entries": entries},
                    "network": "restricted",
                },
                "sandbox_policy": {
                    "type": "workspace-write",
                    "network_access": False,
                    "exclude_tmpdir_env_var": True,
                    "exclude_slash_tmp": True,
                },
                "approval_policy": "never",
                "file_system_sandbox_policy": {"kind": "restricted", "entries": entries},
                "tool_names": ["exec"],
            }

        first_project = self.root / "isolated-first"
        second_project = self.root / "isolated-second"
        first_project.mkdir()
        second_project.mkdir()
        first = policy_for(first_project, "SyntheticOne")
        second = policy_for(second_project, "SyntheticTwo")

        def binding_for(project: Path) -> dict[str, object]:
            return {
                "template_hash": json_hash(template),
                "mapping": {
                    "attempt_project": str(project.resolve()),
                    "account_home": str(account_home),
                    "codex_arg0_parent": str(account_home / ".codex" / "tmp" / "arg0-macos"),
                    "codex_arg0_pattern": "codex-arg0[A-Za-z0-9]+",
                },
            }

        first_match = _match_codex_native_policy(first, template, binding_for(first_project))
        second_match = _match_codex_native_policy(second, template, binding_for(second_project))
        self.assertIsNotNone(first_match)
        self.assertIsNotNone(second_match)
        self.assertNotEqual(json_hash(first), json_hash(second))
        assert first_match is not None
        assert second_match is not None
        self.assertEqual(first_match["attempt_project"], str(first_project.resolve()))
        self.assertEqual(second_match["attempt_project"], str(second_project.resolve()))

        for mutation in (
            lambda value: value["permission_profile"]["file_system"]["entries"].append(
                {"path": {"type": "path", "path": "/synthetic/extra"}, "access": "read"}
            ),
            lambda value: value.__setitem__("approval_policy", "on-request"),
            lambda value: value["sandbox_policy"].__setitem__("network_access", True),
            lambda value: value.__setitem__("tool_names", ["exec", "shell"]),
        ):
            invalid = json.loads(json.dumps(first))
            mutation(invalid)
            if len(invalid["permission_profile"]["file_system"]["entries"]) == 5:
                invalid["file_system_sandbox_policy"]["entries"].append(
                    {"path": {"type": "path", "path": "/synthetic/extra"}, "access": "read"}
                )
            self.assertIsNone(
                _match_codex_native_policy(invalid, template, binding_for(first_project))
            )

        inconsistent = json.loads(json.dumps(first))
        inconsistent["file_system_sandbox_policy"]["entries"].pop()
        self.assertIsNone(
            _match_codex_native_policy(inconsistent, template, binding_for(first_project))
        )

        duplicate = json.loads(json.dumps(first))
        duplicate_entry = duplicate["permission_profile"]["file_system"]["entries"][0]
        duplicate["permission_profile"]["file_system"]["entries"].append(duplicate_entry)
        duplicate["file_system_sandbox_policy"]["entries"].append(duplicate_entry)
        self.assertIsNone(
            _match_codex_native_policy(duplicate, template, binding_for(first_project))
        )

        wrong_account = json.loads(json.dumps(first))
        wrong_account["permission_profile"]["file_system"]["entries"][-1]["path"]["path"] = (
            "/synthetic-account/.codex/tmp/arg0-macos/codex-arg0Wrong"
        )
        wrong_account["file_system_sandbox_policy"]["entries"][-1]["path"]["path"] = (
            "/synthetic-account/.codex/tmp/arg0-macos/codex-arg0Wrong"
        )
        self.assertIsNone(
            _match_codex_native_policy(wrong_account, template, binding_for(first_project))
        )

        wrong_shape = json.loads(json.dumps(first))
        wrong_shape["permission_profile"]["file_system"]["entries"][-1]["path"]["path"] = str(
            account_home / ".codex" / "tmp" / "arg0-macos" / "codex-arg0-not-valid"
        )
        wrong_shape["file_system_sandbox_policy"]["entries"][-1]["path"]["path"] = str(
            account_home / ".codex" / "tmp" / "arg0-macos" / "codex-arg0-not-valid"
        )
        self.assertIsNone(
            _match_codex_native_policy(wrong_shape, template, binding_for(first_project))
        )

    def test_fixture_and_self_asserted_reports_are_not_real_qualification(self) -> None:
        repository, suite, candidate, fixture_report = self._repository_and_candidate()

        for report in (
            fixture_report,
            self.root / "self-asserted-real.json",
        ):
            if report.name == "self-asserted-real.json":
                report.write_text(
                    json.dumps(
                        {
                            "schema_version": 2,
                            "execution_type": "real-harness",
                            "status": "passed",
                            "candidate_hash": "0" * 64,
                            "eligible": True,
                        }
                    ),
                    encoding="utf-8",
                )
            with self.subTest(report=report.name):
                with self.assertRaises(SelcError) as captured:
                    validate_real_evaluation(report, candidate)
                self.assertEqual(captured.exception.code, "REAL_EVALUATION_REQUIRED")

        with self.assertRaises(SelcError) as contribution_error:
            prepare_contribution(
                candidate,
                suite,
                fixture_report,
                repository,
                self.root / "fixture-contribution-workspace",
                self.root / "fixture-contribution.json",
            )
        self.assertEqual(contribution_error.exception.code, "REAL_EVALUATION_REQUIRED")
        with self.assertRaises(SelcError) as release_error:
            prepare_release(
                suite,
                candidate,
                fixture_report,
                "local",
                "HEAD",
                "v1.0.1",
                self.root / "fixture-release.json",
                self.root / "fixture-release-center",
                None,
            )
        self.assertEqual(release_error.exception.code, "REAL_EVALUATION_REQUIRED")

    def test_every_contribution_and_release_consumer_uses_shared_qualification(self) -> None:
        repository, suite, candidate, evaluation = self._repository_and_candidate()
        qualified = {
            "schema_version": 2,
            "execution_type": "real-harness",
            "candidate_hash": "synthetic-qualified-by-test",
            "snapshot_hash": "synthetic-qualified-by-test",
        }
        contribution_plan_path = self.root / "contribution.json"

        with patch("selc.evolve.validate_real_evaluation", return_value=qualified) as validate:
            contribution = prepare_contribution(
                candidate,
                suite,
                evaluation,
                repository,
                self.root / "contribution-workspace",
                contribution_plan_path,
            )
            contribution_result = execute_contribution(contribution_plan_path, "local", None, None)
            release_plan_path = self.root / "release.json"
            release = prepare_release(
                suite,
                candidate,
                evaluation,
                "local",
                contribution_result["commit"],
                "v1.0.1",
                release_plan_path,
                self.root / "release-center",
                None,
            )
            confirmation = self.root / "confirmation.json"
            confirm_release(release_plan_path, str(release["plan_hash"]), confirmation)
            release_local(release_plan_path, confirmation, self.root / "release-center")

        self.assertEqual(validate.call_count, 4)
        self.assertTrue(contribution["qualification"]["execution_type"] == "real-harness")
        with self.assertRaises(SelcError) as contribution_error:
            execute_contribution(contribution_plan_path, "local", None, None)
        self.assertEqual(contribution_error.exception.code, "REAL_EVALUATION_REQUIRED")
        with self.assertRaises(SelcError) as release_error:
            release_local(release_plan_path, confirmation, self.root / "release-center")
        self.assertEqual(release_error.exception.code, "REAL_EVALUATION_REQUIRED")

    def test_generated_runtime_carries_the_framework_license(self) -> None:
        suite = self.root / "licensed-suite"
        self.assertFalse((self.root / "LICENSE").exists())
        create_suite("licensed-suite", "1.0.0", suite, ["text-report"])
        runtime_license = suite / "runtime" / "SELC-LICENSE"
        framework_license = REPOSITORY_ROOT / "src" / "selc" / "SELC-LICENSE"
        self.assertEqual(runtime_license.read_bytes(), framework_license.read_bytes())
        (self.root / "LICENSE").write_text("distinct business license\n", encoding="utf-8")
        recreated_suite = self.root / "recreated-suite"
        create_suite("recreated-suite", "1.0.0", recreated_suite, ["text-report"])
        self.assertEqual(
            (recreated_suite / "runtime" / "SELC-LICENSE").read_bytes(),
            framework_license.read_bytes(),
        )
        package = self.root / "licensed-suite.zip"
        package_suite(suite, package)
        self.assertTrue(package.is_file())

        portable_child = self.root / "portable-child"
        completed = subprocess.run(
            [
                sys.executable,
                str(suite / "bin" / "selc-suite.py"),
                "create",
                "--suite-id",
                "portable-child",
                "--version",
                "1.0.0",
                "--output",
                str(portable_child),
                "--skill",
                "portable-skill",
            ],
            cwd=self.root,
            capture_output=True,
            check=False,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        self.assertEqual(
            (portable_child / "runtime" / "SELC-LICENSE").read_bytes(),
            framework_license.read_bytes(),
        )
        self.assertEqual(
            (portable_child / "runtime" / "selc" / "SELC-LICENSE").read_bytes(),
            framework_license.read_bytes(),
        )

    def test_codex_cli_uses_exact_frozen_prompt_and_native_store_environment(self) -> None:
        request_path = self.root / "frozen-request.json"
        prompt = '@text-report {"input":"/isolated/project/evaluation-input.json"}'
        request_path.write_text(json.dumps({"request": prompt}), encoding="utf-8")
        state = {
            "plan_hash": "plan",
            "binding_hash": "binding",
            "case_id": "case",
            "matrix_id": "matrix",
            "attempt_number": 1,
        }

        self.assertEqual(_codex_cli_prompt(request_path, state), prompt.encode("utf-8"))
        with patch.dict(
            "os.environ",
            {
                "PATH": "/synthetic/bin",
                "CODEX_HOME": "/unexpected/home",
                "CODEX_SESSION_ID": "stale-session",
                "CODEX_THREAD_ID": "stale-thread",
            },
            clear=True,
        ):
            environment = _fixed_codex_environment(self.root / "attempt", state)

        self.assertEqual(environment["PATH"], "/synthetic/bin")
        self.assertNotIn("CODEX_HOME", environment)
        self.assertNotIn("CODEX_SESSION_ID", environment)
        self.assertNotIn("CODEX_THREAD_ID", environment)
        self.assertEqual(environment["HOME"], str(Path.home()))

    def test_untracked_authoring_suite_does_not_claim_a_git_baseline(self) -> None:
        repository = self.root / "repository"
        repository.mkdir()
        git(["init", "-b", "main"], repository)
        git(["config", "user.name", "SELC Fixture"], repository)
        git(["config", "user.email", "fixture@example.test"], repository)
        (repository / "README.md").write_text("baseline\n", encoding="utf-8")
        git(["add", "README.md"], repository)
        git(["commit", "-m", "baseline"], repository)
        suite = repository / "untracked-suite"
        create_suite("untracked-suite", "1.0.0", suite, ["text-report"])

        package = self.root / "untracked-suite.zip"
        package_suite(suite, package)
        with zipfile.ZipFile(package) as archive:
            self.assertNotIn("source-baseline.json", archive.namelist())

    def test_ignored_authoring_suite_does_not_claim_a_git_baseline(self) -> None:
        repository = self.root / "repository"
        repository.mkdir()
        git(["init", "-b", "main"], repository)
        git(["config", "user.name", "SELC Fixture"], repository)
        git(["config", "user.email", "fixture@example.test"], repository)
        (repository / ".gitignore").write_text("ignored-suite/\n", encoding="utf-8")
        git(["add", ".gitignore"], repository)
        git(["commit", "-m", "baseline"], repository)
        suite = repository / "ignored-suite"
        create_suite("ignored-suite", "1.0.0", suite, ["text-report"])

        package = self.root / "ignored-suite.zip"
        package_suite(suite, package)
        with zipfile.ZipFile(package) as archive:
            self.assertNotIn("source-baseline.json", archive.namelist())

    def test_real_plan_binds_full_snapshot_and_desktop_collection_never_launches(self) -> None:
        suite = self.root / "plan-suite"
        create_suite("plan-suite", "1.0.0", suite, ["text-report"])
        input_path = self.root / "items.json"
        input_path.write_text('{"items": [{"id": "one", "value": 1}]}', encoding="utf-8")
        specification = self.root / "cases.json"
        specification.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "cases": [
                        {
                            "id": "synthetic-holdout",
                            "collection": "holdout",
                            "input": str(input_path),
                            "checker": {
                                "name": "json-item-summary",
                                "version": "1",
                                "rules": ["count", "sum"],
                                "configuration": {"duplicate_ids_count_once": True},
                            },
                        }
                    ],
                    "matrix": [
                        {
                            "id": "codex-desktop-synthetic",
                            "adapter": "codex-desktop",
                            "host": "codex",
                            "host_version": "synthetic",
                            "surface": "desktop",
                            "model": "synthetic-model",
                            "tools_policy_digest": "0" * 64,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        plan_root = self.root / "real-plan"
        prepared = prepare_real_evaluation_plan(suite, specification, plan_root)
        candidate = Path(prepare_candidate(suite, self.root / "candidate")["candidate"])
        creator_request = self.root / "creator-request.json"
        prepared_creator = prepare_creator_request(
            Path(prepared["plan"]), candidate, creator_request
        )
        self.assertNotIn("synthetic-holdout", prepared_creator["prompt"])
        skill = candidate / "skills" / "text-report" / "SKILL.md"
        skill.write_text(
            skill.read_text(encoding="utf-8") + "Creator changed only this body.\n",
            encoding="utf-8",
        )
        with self.assertRaises(SelcError) as creator_unverified:
            verify_creator_result(creator_request, candidate)
        self.assertEqual(creator_unverified.exception.code, "CREATOR_ISOLATION_UNVERIFIED")
        binding_path = self.root / "binding.json"
        binding = bind_real_evaluation_candidate(Path(prepared["plan"]), candidate, binding_path)
        self.assertTrue(Path(binding["candidate_snapshot"]["path"]).is_dir())
        attempt = plan_root / "attempts" / "desktop-synthetic"
        prepare_evaluation_attempt(
            Path(prepared["plan"]),
            binding_path,
            "synthetic-holdout",
            "codex-desktop-synthetic",
            1,
            attempt,
        )
        request = desktop_request(attempt)
        self.assertEqual(request["adapter"], "codex-desktop")
        self.assertEqual(request["qualification_support"], "unverified")
        self.assertEqual(request["qualification_reason"], "raw-policy-digest-plan")
        collected = collect_evaluation_attempt(attempt)
        self.assertEqual(collected["evidence"]["status"], "unknown")
        self.assertEqual(collected["evidence"]["reason"], "qualification-raw-policy-digest-plan")
        (plan_root / "baseline" / "runtime" / "SELC-LICENSE").write_text(
            "not the framework license\n", encoding="utf-8"
        )
        with self.assertRaises(SelcError) as changed_license:
            bind_real_evaluation_candidate(
                Path(prepared["plan"]), candidate, self.root / "license-binding.json"
            )
        self.assertEqual(changed_license.exception.code, "EVALUATION_PLAN_TAMPERED")

    def test_plan_freezes_version_scope_and_refuses_changed_case_inputs(self) -> None:
        suite = self.root / "versioned-suite"
        create_suite("versioned-suite", "1.0.0", suite, ["text-report"])
        input_path = self.root / "items.json"
        input_path.write_text('{"items": [{"id": "one", "value": 1}]}', encoding="utf-8")
        specification = self.root / "versions.json"
        specification.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "cases": [
                        {
                            "id": "integers-empty",
                            "collection": "regression",
                            "input": str(input_path),
                            "checker": {
                                "name": "json-item-summary",
                                "version": "1",
                                "rules": ["count"],
                            },
                        },
                        {
                            "id": "decimal-cancellation",
                            "collection": "holdout",
                            "input": str(input_path),
                            "checker": {
                                "name": "json-item-summary",
                                "version": "1",
                                "rules": ["sum"],
                            },
                        },
                    ],
                    "matrix": [
                        {
                            "id": "desktop",
                            "adapter": "codex-desktop",
                            "host": "codex",
                            "host_version": "synthetic",
                            "surface": "desktop",
                            "model": "synthetic-model",
                            "tools_policy_digest": "0" * 64,
                        }
                    ],
                    "variants": [
                        {
                            "id": "initial",
                            "version": "1.0.0",
                            "snapshot": "baseline",
                            "scope": {"capabilities": ["integers", "empty"]},
                            "required_cases": ["integers-empty"],
                        },
                        {
                            "id": "improved",
                            "version": "1.1.0",
                            "snapshot": "candidate",
                            "scope": {"capabilities": ["decimal", "cancellation"]},
                            "required_cases": ["decimal-cancellation"],
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        plan_root = self.root / "versioned-plan"
        prepared = prepare_real_evaluation_plan(suite, specification, plan_root)
        self.assertEqual(len(prepared["baseline_bindings"]), 1)

        candidate = Path(prepare_candidate(suite, self.root / "candidate")["candidate"])
        candidate_binding = self.root / "improved-binding.json"
        bind_real_evaluation_candidate(
            Path(prepared["plan"]), candidate, candidate_binding, "improved"
        )
        with self.assertRaises(SelcError) as out_of_scope:
            prepare_evaluation_attempt(
                Path(prepared["plan"]),
                candidate_binding,
                "integers-empty",
                "desktop",
                1,
                plan_root / "attempts" / "out-of-scope",
            )
        self.assertEqual(out_of_scope.exception.code, "UNREQUIRED_EVALUATION_UNIT")

        candidate_attempt = plan_root / "attempts" / "candidate-improved"
        prepare_evaluation_attempt(
            Path(prepared["plan"]),
            candidate_binding,
            "decimal-cancellation",
            "desktop",
            1,
            candidate_attempt,
        )
        report = finalize_real_evaluation(
            Path(prepared["plan"]),
            [candidate_binding],
            [candidate_attempt],
            self.root / "versioned-report.json",
        )
        self.assertEqual(report["status"], "unknown")
        self.assertEqual(report["qualifications"][0]["variant_id"], "improved")
        summary = public_evaluation_summary(self.root / "versioned-report.json")
        self.assertEqual(summary["unverified_variants"][0]["variant_id"], "initial")

        frozen_input = plan_root / "case-inputs" / "000-integers-empty.json"
        frozen_input.write_text('{"items": []}', encoding="utf-8")
        with self.assertRaises(SelcError) as tampered:
            bind_real_evaluation_candidate(
                Path(prepared["plan"]), candidate, self.root / "tampered-binding.json", "improved"
            )
        self.assertEqual(tampered.exception.code, "EVALUATION_PLAN_TAMPERED")

    def test_binding_snapshot_manifest_uses_the_frozen_candidate_version(self) -> None:
        suite = self.root / "version-source"
        create_suite("version-source", "1.0.0", suite, ["text-report"])
        input_path = self.root / "version-input.json"
        input_path.write_text('{"items": []}', encoding="utf-8")
        specification = self._real_plan_specification(input_path)
        specification_value = json.loads(specification.read_text(encoding="utf-8"))
        specification_value["variants"] = [
            {
                "id": "initial",
                "version": "1.0.0",
                "snapshot": "baseline",
                "scope": {"capabilities": ["integer"]},
                "required_cases": ["synthetic-case"],
            },
            {
                "id": "improved",
                "version": "1.1.0",
                "snapshot": "candidate",
                "scope": {"capabilities": ["decimal"]},
                "required_cases": ["synthetic-case"],
            },
        ]
        specification.write_text(json.dumps(specification_value), encoding="utf-8")
        prepared = prepare_real_evaluation_plan(suite, specification, self.root / "version-plan")
        candidate = Path(prepare_candidate(suite, self.root / "version-candidate")["candidate"])

        binding = bind_real_evaluation_candidate(
            Path(prepared["plan"]), candidate, self.root / "improved-binding.json", "improved"
        )

        snapshot = Path(binding["candidate_snapshot"]["path"])
        self.assertEqual(load_manifest(snapshot)["version"], "1.1.0")

    def test_attempt_reservation_installs_the_snapshot_and_freezes_input(self) -> None:
        suite = self.root / "attempt-source"
        create_suite("attempt-source", "1.0.0", suite, ["text-report"])
        input_path = self.root / "attempt-input.json"
        input_path.write_text('{"items": []}', encoding="utf-8")
        prepared = prepare_real_evaluation_plan(
            suite, self._real_plan_specification(input_path), self.root / "attempt-plan"
        )
        candidate = Path(prepare_candidate(suite, self.root / "attempt-candidate")["candidate"])
        binding_path = self.root / "attempt-binding.json"
        bind_real_evaluation_candidate(Path(prepared["plan"]), candidate, binding_path)

        state = prepare_evaluation_attempt(
            Path(prepared["plan"]),
            binding_path,
            "synthetic-case",
            "synthetic-matrix",
            1,
            self.root / "attempt-plan" / "attempts" / "first",
        )

        project_root = Path(state["project_root"])
        self.assertTrue(
            (project_root / ".agents" / "skills" / "text-report" / "SKILL.md").is_file()
        )
        self.assertEqual(
            (project_root / "evaluation-input.json").read_bytes(),
            input_path.read_bytes(),
        )

    def test_workbuddy_attempt_installs_the_workbuddy_collector(self) -> None:
        suite = self.root / "workbuddy-source"
        create_suite("workbuddy-source", "1.0.0", suite, ["text-report"])
        input_path = self.root / "workbuddy-input.json"
        input_path.write_text('{"items": []}', encoding="utf-8")
        specification = self._real_plan_specification(input_path)
        value = json.loads(specification.read_text(encoding="utf-8"))
        value["matrix"][0].update(
            {
                "adapter": "workbuddy-desktop",
                "host": "workbuddy",
                "host_version": "synthetic-workbuddy",
                "surface": "desktop",
            }
        )
        specification.write_text(json.dumps(value), encoding="utf-8")
        prepared = prepare_real_evaluation_plan(suite, specification, self.root / "workbuddy-plan")
        candidate = Path(prepare_candidate(suite, self.root / "workbuddy-candidate")["candidate"])
        binding_path = self.root / "workbuddy-binding.json"
        bind_real_evaluation_candidate(Path(prepared["plan"]), candidate, binding_path)

        state = prepare_evaluation_attempt(
            Path(prepared["plan"]),
            binding_path,
            "synthetic-case",
            "synthetic-matrix",
            1,
            self.root / "workbuddy-plan" / "attempts" / "first",
        )

        project_root = Path(state["project_root"])
        self.assertTrue((project_root / ".codebuddy" / "settings.json").is_file())
        self.assertEqual(state["matrix"]["qualification_support"], "unverified")

        request = desktop_request(self.root / "workbuddy-plan" / "attempts" / "first")
        self.assertEqual(request["qualification_support"], "unverified")
        self.assertEqual(
            request["qualification_reason"], "native-attribution-unverified-for-workbuddy"
        )
        template = native_evidence_template(self.root / "workbuddy-plan" / "attempts" / "first")
        self.assertEqual(template["qualification_support"], "unverified")
        self.assertEqual(template["collect_result"], "unknown")
        self.assertNotIn("required_fields", template)

        collected = collect_evaluation_attempt(self.root / "workbuddy-plan" / "attempts" / "first")
        self.assertEqual(collected["evidence"]["status"], "unknown")
        self.assertEqual(
            collected["evidence"]["reason"],
            "qualification-native-attribution-unverified-workbuddy",
        )

    def test_synthetic_native_lifecycle_reconstructs_a_real_shaped_qualification(self) -> None:
        """Exercise installed Codex hooks and publication; this is not host acceptance.

        The test uses the real managed invoke/action/publish commands and the
        accepted native serializer shape.  It writes no records directly: the
        hooks create the receipt, call, publication, window, and checker rows.
        """

        suite = self.root / "positive-suite"
        create_suite("positive-suite", "1.0.0", suite, ["text-report"])
        manifest_path = suite / "suite.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["managed"]["native_hosts"] = {"codex": ["0.163.0-alpha.11"]}
        manifest["managed"]["actions"]["text-report"] = {
            "skill": "text-report",
            "kind": "python-script-v1",
            "script": "bin/json-summary.py",
            "input": {"fields": {"input": {"type": "path"}}, "required": ["input"]},
        }
        manifest["artifact_checks"] = {
            "summary": {
                "checker": {"name": "json-item-summary", "version": "1"},
                "rule_version": "1",
                "required_rule_ids": ["count", "sum"],
                "configuration": {"duplicate_ids_count_once": False},
            }
        }
        manifest["skills"][0]["artifact_check"] = "summary"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        (suite / "bin" / "json-summary.py").write_text(
            "import argparse\n"
            "import json\n"
            "from pathlib import Path\n"
            "parser = argparse.ArgumentParser()\n"
            "parser.add_argument('command')\n"
            "parser.add_argument('--request', required=True)\n"
            "parser.add_argument('--output', required=True)\n"
            "arguments = parser.parse_args()\n"
            "request = json.loads(Path(arguments.request).read_text(encoding='utf-8'))\n"
            "items = json.loads(Path(request['input']).read_text(encoding='utf-8'))['items']\n"
            "artifact = {'count': len(items), 'sum': sum(item['value'] for item in items)}\n"
            "Path(arguments.output).write_text(json.dumps(artifact), encoding='utf-8')\n",
            encoding="utf-8",
        )
        seal_suite(suite)
        input_path = self.root / "positive-input.json"
        input_path.write_text(
            json.dumps({"items": [{"id": "first", "value": 2}, {"id": "second", "value": 3}]}),
            encoding="utf-8",
        )
        plan_root = self.root / "positive-plan"
        attempt_path = plan_root / "attempts" / "native-positive"
        attempted_input = attempt_path / "project" / "evaluation-input.json"
        evaluation_python = self.root / "evaluation-python"
        evaluation_python.mkdir()
        policy_template = build_codex_policy_template("0.163.0-alpha.11", evaluation_python)
        prompt = f'@text-report {{"input":{json.dumps(str(attempted_input))}}}'
        specification = self.root / "positive-specification.json"
        specification.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "cases": [
                        {
                            "id": "positive-case",
                            "collection": "regression",
                            "input": str(input_path),
                            "request": prompt,
                            "checker": {
                                "name": "json-item-summary",
                                "version": "1",
                                "rules": ["count", "sum"],
                            },
                        }
                    ],
                    "matrix": [
                        {
                            "id": "codex-native",
                            "adapter": "codex-desktop",
                            "host": "codex",
                            "host_version": "0.163.0-alpha.11",
                            "surface": "cli",
                            "model": "synthetic-model",
                            "tools_policy_template": policy_template,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        prepared = prepare_real_evaluation_plan(suite, specification, plan_root)
        candidate = Path(prepare_candidate(suite, self.root / "positive-candidate")["candidate"])
        candidate_skill = candidate / "skills" / "text-report" / "SKILL.md"
        candidate_skill.write_text(
            candidate_skill.read_text(encoding="utf-8") + "Synthetic checked improvement.\n",
            encoding="utf-8",
        )
        binding_path = self.root / "positive-binding.json"
        bind_real_evaluation_candidate(Path(prepared["plan"]), candidate, binding_path)
        state = prepare_evaluation_attempt(
            Path(prepared["plan"]),
            binding_path,
            "positive-case",
            "codex-native",
            1,
            attempt_path,
        )
        self.assertEqual(
            _action_request_from_prompt(state, "text-report"),
            {"input": str(attempted_input)},
        )
        project_root = Path(state["project_root"])
        account_home = Path(str(policy_template["account_home"]))
        arg0_path = account_home / ".codex" / "tmp" / "arg0-macos" / "codex-arg0SyntheticA1"
        native_entries = [
            {
                "path": {"type": "special", "value": {"kind": "minimal"}},
                "access": "read",
            },
            {
                "path": {"type": "path", "path": str(evaluation_python.resolve())},
                "access": "read",
            },
            {
                "path": {"type": "path", "path": str(project_root.resolve())},
                "access": "write",
            },
            {
                "path": {"type": "path", "path": str(arg0_path)},
                "access": "read",
            },
        ]
        turn_policy = {
            "permission_profile": {
                "type": "managed",
                "file_system": {"type": "restricted", "entries": native_entries},
                "network": "restricted",
            },
            "sandbox_policy": {
                "type": "workspace-write",
                "network_access": False,
                "exclude_tmpdir_env_var": True,
                "exclude_slash_tmp": True,
            },
            "approval_policy": "never",
            "file_system_sandbox_policy": {"kind": "restricted", "entries": native_entries},
            "tool_names": ["exec"],
        }
        session_id = "synthetic-evaluation-session"
        turn_id = "synthetic-evaluation-turn"
        history = self.root / ".codex" / "sessions" / "2026" / f"{session_id}.jsonl"
        history.parent.mkdir(parents=True)
        initial_records = [
            {
                "type": "session_meta",
                "payload": {
                    "id": session_id,
                    "session_id": session_id,
                    "cwd": str(project_root.resolve()),
                    "cli_version": "0.163.0-alpha.11",
                    "source": "exec",
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "task_started",
                    "turn_id": turn_id,
                    "root_turn_id": turn_id,
                    "turn_attribution": {
                        "turn_id": turn_id,
                        "root_turn_id": turn_id,
                        "parent_turn_id": None,
                    },
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "developer",
                    "internal_chat_message_metadata_passthrough": {
                        "turn_id": turn_id,
                        "content_item_kinds": ["host_skills.instructions"],
                    },
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "internal_chat_message_metadata_passthrough": {
                        "turn_id": turn_id,
                        "content_item_kinds": ["agents_md.instructions"],
                    },
                },
            },
            {"type": "world_state", "payload": {"full": True, "state": {"environments": {}}}},
            {
                "type": "turn_context",
                "payload": {
                    "turn_id": turn_id,
                    "root_turn_id": turn_id,
                    "cwd": str(project_root.resolve()),
                    "model": "synthetic-model",
                    **{key: value for key, value in turn_policy.items() if key != "tool_names"},
                },
            },
            {
                "type": "event_msg",
                "payload": {"type": "thread_settings_applied", "thread_id": session_id},
            },
        ]
        history.write_bytes(
            b"".join((json.dumps(record) + "\n").encode("utf-8") for record in initial_records)
        )
        environment = {
            **os.environ,
            "CODEX_SESSION_ID": session_id,
            "CODEX_THREAD_ID": session_id,
            "CODEX_VERSION": "0.163.0-alpha.11",
        }
        environment.pop("CODEX_TOOL_CALL_ID", None)

        def receive(event: dict[str, object]) -> dict[str, object]:
            with (
                patch("selc.managed_runtime.Path.home", return_value=self.root),
                patch("selc.native_adapter.Path.home", return_value=self.root),
            ):
                return receive_native_hook(
                    project_root=project_root,
                    suite_id="positive-suite",
                    host="codex",
                    payload=event,
                    environment=environment,
                )

        def run(command: list[str], tool_call_id: str) -> dict[str, object]:
            completed = subprocess.run(
                command,
                env={**environment, "CODEX_TOOL_CALL_ID": tool_call_id},
                capture_output=True,
                check=False,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
            return json.loads(completed.stdout)["managed"]

        receive(
            {
                "hook_event_name": "UserPromptSubmit",
                "cwd": str(project_root),
                "session_id": session_id,
                "turn_id": turn_id,
                "transcript_path": str(history),
                "prefix_length": history.stat().st_size,
                "prompt": prompt,
            }
        )
        with history.open("ab") as stream:
            stream.write(
                (
                    json.dumps(
                        {
                            "type": "response_item",
                            "payload": {
                                "type": "message",
                                "role": "user",
                                "content": [{"type": "input_text", "text": prompt}],
                                "internal_chat_message_metadata_passthrough": {
                                    "turn_id": turn_id,
                                    "content_item_kinds": ["user.text"],
                                },
                            },
                        }
                    )
                    + "\n"
                ).encode("utf-8")
            )
        wrapper = (
            project_root
            / ".selc"
            / "installs"
            / "positive-suite"
            / "current"
            / "bin"
            / "selc-suite.py"
        )
        base_command = [
            sys.executable,
            str(wrapper),
            "managed",
        ]
        installation = state["installation"]
        invoke_command = base_command + [
            "invoke",
            "--project-root",
            str(project_root),
            "--suite",
            "positive-suite",
            "--skill",
            "text-report",
            "--host",
            "codex",
            "--expected-version",
            str(installation["version"]),
            "--expected-composite",
            str(installation["digest"]),
        ]
        receive(
            {
                "hook_event_name": "PreToolUse",
                "cwd": str(project_root),
                "session_id": session_id,
                "turn_id": turn_id,
                "tool_call_id": "positive-invoke",
                "tool_name": "Bash",
                "tool_input": {"command": shlex.join(invoke_command)},
                "transcript_path": str(history),
                "prefix_length": history.stat().st_size,
            }
        )
        activated = run(invoke_command, "positive-invoke")
        action_request = self.root / "positive-action-request.json"
        action_request.write_text(json.dumps({"input": str(attempted_input)}), encoding="utf-8")
        action_command = base_command + [
            "action",
            "--project-root",
            str(project_root),
            "--suite",
            "positive-suite",
            "--skill",
            "text-report",
            "--task-id",
            str(activated["task_id"]),
            "--call-id",
            str(activated["call_id"]),
            "--action",
            "text-report",
            "--request",
            str(action_request),
            "--host",
            "codex",
            "--expected-version",
            str(installation["version"]),
            "--expected-composite",
            str(installation["digest"]),
        ]
        receive(
            {
                "hook_event_name": "PreToolUse",
                "cwd": str(project_root),
                "session_id": session_id,
                "turn_id": turn_id,
                "tool_call_id": "positive-action",
                "tool_name": "Bash",
                "tool_input": {"command": shlex.join(action_command)},
                "transcript_path": str(history),
                "prefix_length": history.stat().st_size,
            }
        )
        action = run(action_command, "positive-action")
        publish_command = base_command + [
            "publish",
            "--project-root",
            str(project_root),
            "--suite",
            "positive-suite",
            "--skill",
            "text-report",
            "--task-id",
            str(activated["task_id"]),
            "--call-id",
            str(activated["call_id"]),
            "--artifact",
            str(action["artifact_path"]),
            "--target",
            "reports/positive.json",
            "--host",
            "codex",
            "--expected-version",
            str(installation["version"]),
            "--expected-composite",
            str(installation["digest"]),
        ]
        receive(
            {
                "hook_event_name": "PreToolUse",
                "cwd": str(project_root),
                "session_id": session_id,
                "turn_id": turn_id,
                "tool_call_id": "positive-publish",
                "tool_name": "Bash",
                "tool_input": {"command": shlex.join(publish_command)},
                "transcript_path": str(history),
                "prefix_length": history.stat().st_size,
            }
        )
        with history.open("ab") as stream:
            stream.write(
                (
                    json.dumps(
                        {
                            "type": "response_item",
                            "payload": {
                                "type": "custom_tool_call",
                                "name": "exec",
                                "call_id": "outer-exec-publish",
                                "input": {"cmd": shlex.join(publish_command)},
                                "id": "outer-exec-publish-item",
                                "status": "completed",
                                "internal_chat_message_metadata_passthrough": {"turn_id": turn_id},
                            },
                        }
                    )
                    + "\n"
                ).encode("utf-8")
            )
        published = run(publish_command, "positive-publish")
        self.assertEqual(published["status"], "published")
        receive(
            {
                "hook_event_name": "PostToolUse",
                "cwd": str(project_root),
                "session_id": session_id,
                "turn_id": turn_id,
                "tool_call_id": "positive-publish",
                "tool_name": "Bash",
                "tool_input": {"command": shlex.join(publish_command)},
                "transcript_path": str(history),
                "prefix_length": history.stat().st_size,
            }
        )
        with history.open("ab") as stream:
            stream.write(
                (
                    json.dumps(
                        {
                            "type": "event_msg",
                            "payload": {
                                "type": "item_completed",
                                "thread_id": session_id,
                                "turn_id": turn_id,
                                "item": {
                                    "type": "CommandExecution",
                                    "id": "positive-publish",
                                    "command": ["/bin/zsh", "-lc", shlex.join(publish_command)],
                                    "cwd": str(project_root.resolve()),
                                    "status": "completed",
                                    "exit_code": 0,
                                },
                            },
                        }
                    )
                    + "\n"
                ).encode("utf-8")
            )
        stopped = receive(
            {
                "hook_event_name": "Stop",
                "cwd": str(project_root),
                "session_id": session_id,
                "turn_id": turn_id,
                "transcript_path": str(history),
                "prefix_length": history.stat().st_size,
            }
        )
        self.assertEqual(stopped["status"], "stop_recorded")

        database_path = project_root / ".selc" / "records.sqlite3"
        database = sqlite3.connect(database_path)
        try:
            payload = database.execute(
                """SELECT native.payload_json
                FROM managed_windows AS windows
                JOIN managed_native_events AS native ON native.event_id = windows.post_event_id
                WHERE windows.status = 'closed'"""
            ).fetchone()[0]
        finally:
            database.close()
        prefix = json.loads(payload)["prefix"]
        raw_path = history.resolve()
        raw_stat = raw_path.stat()
        self.assertEqual(
            hashlib.sha256(raw_path.read_bytes()[: prefix["length"]]).hexdigest(),
            prefix["sha256"],
        )
        (Path(state["session_root"]) / "native-evidence.json").write_text(
            json.dumps(
                {
                    "attempt_id": ":".join(
                        [
                            state["plan_hash"],
                            state["case_id"],
                            state["matrix_id"],
                            str(state["attempt_number"]),
                        ]
                    ),
                    "raw_session_path": str(raw_path),
                    "raw_session_sha256": prefix["sha256"],
                    "raw_session_cutoff_bytes": prefix["length"],
                    "raw_session_path_hash": prefix["path_hash"],
                    "raw_session_device": raw_stat.st_dev,
                    "raw_session_inode": raw_stat.st_ino,
                }
            ),
            encoding="utf-8",
        )
        with patch("selc.evaluation.Path.home", return_value=self.root):
            collected = collect_evaluation_attempt(attempt_path)
            self.assertEqual(collected["evidence"]["status"], "pass", collected["evidence"])
            report_path = self.root / "positive-report.json"
            report = finalize_real_evaluation(
                Path(prepared["plan"]),
                [binding_path],
                [attempt_path],
                report_path,
            )
            qualification = validate_real_evaluation(report_path, candidate)

        self.assertEqual(collected["evidence"]["status"], "pass")
        self.assertEqual(report["status"], "passed")
        self.assertEqual(qualification["execution_type"], "real-harness")

        database = sqlite3.connect(database_path)
        database.row_factory = sqlite3.Row
        try:
            proof_row = dict(
                database.execute(
                    """SELECT native.host, native.session_id, native.generation_id, native.request_id,
                              native.tool_use_id, native.payload_json,
                              root_native.payload_json AS root_payload_json,
                              pre_native.payload_json AS pre_payload_json,
                              post_native.payload_json AS cutoff_payload_json
                       FROM managed_publications AS publications
                       JOIN managed_receipts AS receipts
                         ON receipts.receipt_digest = publications.receipt_digest
                       JOIN managed_native_events AS native
                         ON native.event_id = receipts.native_event_id
                       JOIN managed_contexts AS contexts
                         ON contexts.suite_id = receipts.suite_id
                         AND contexts.host = receipts.host
                         AND contexts.session_id = receipts.session_id
                         AND contexts.generation_id = receipts.generation_id
                         AND contexts.request_id = receipts.request_id
                       JOIN managed_native_events AS root_native
                         ON root_native.event_id = contexts.root_event_id
                       JOIN managed_windows AS windows
                         ON windows.delivery_id = publications.delivery_id
                       JOIN managed_native_events AS pre_native
                         ON pre_native.event_id = windows.pre_event_id
                       JOIN managed_native_events AS post_native
                         ON post_native.event_id = windows.post_event_id
                       WHERE publications.status = 'recorded'"""
                ).fetchone()
            )
        finally:
            database.close()
        with patch("selc.evaluation.Path.home", return_value=self.root):
            native_context = _read_attempt_native_context(state, state["matrix"])
        self.assertIsNotNone(native_context)
        assert native_context is not None

        root_tampered = dict(proof_row)
        root_payload = json.loads(root_tampered["root_payload_json"])
        root_payload["prefix"]["sha256"] = "0" * 64
        root_tampered["root_payload_json"] = json.dumps(root_payload)
        self.assertEqual(
            _native_window_failure(root_tampered, native_context), "root-prefix-digest"
        )

        wrong_turn = dict(proof_row)
        wrong_turn["generation_id"] = "wrong-native-turn"
        self.assertEqual(
            _native_publication_result_failure(wrong_turn, native_context),
            "command-execution-unavailable",
        )

        wrong_inner = dict(native_context)
        raw = wrong_inner["raw"]
        self.assertIsInstance(raw, bytes)
        assert isinstance(raw, bytes)
        wrong_inner["raw"] = raw.replace(
            b'"id": "positive-publish"', b'"id": "negative-publish"', 1
        )
        self.assertEqual(
            _native_publication_result_failure(proof_row, wrong_inner),
            "command-execution-unavailable",
        )

    def test_attempt_budget_rejects_a_second_reservation_for_the_same_unit(self) -> None:
        suite = self.root / "budget-source"
        create_suite("budget-source", "1.0.0", suite, ["text-report"])
        input_path = self.root / "budget-input.json"
        input_path.write_text('{"items": []}', encoding="utf-8")
        prepared = prepare_real_evaluation_plan(
            suite,
            self._real_plan_specification(input_path, max_attempts=1),
            self.root / "budget-plan",
        )
        candidate = Path(prepare_candidate(suite, self.root / "budget-candidate")["candidate"])
        binding_path = self.root / "budget-binding.json"
        bind_real_evaluation_candidate(Path(prepared["plan"]), candidate, binding_path)
        prepare_evaluation_attempt(
            Path(prepared["plan"]),
            binding_path,
            "synthetic-case",
            "synthetic-matrix",
            1,
            self.root / "budget-plan" / "attempts" / "first",
        )

        with self.assertRaises(SelcError) as exhausted:
            prepare_evaluation_attempt(
                Path(prepared["plan"]),
                binding_path,
                "synthetic-case",
                "synthetic-matrix",
                1,
                self.root / "budget-plan" / "attempts" / "second",
            )
        self.assertEqual(exhausted.exception.code, "EVALUATION_BUDGET_EXHAUSTED")

    def test_attempt_total_time_budget_rejects_another_numbered_attempt(self) -> None:
        suite = self.root / "time-budget-source"
        create_suite("time-budget-source", "1.0.0", suite, ["text-report"])
        input_path = self.root / "time-budget-input.json"
        input_path.write_text('{"items": []}', encoding="utf-8")
        specification = self._real_plan_specification(input_path, max_attempts=2)
        specification_value = json.loads(specification.read_text(encoding="utf-8"))
        specification_value["total_timeout_seconds"] = 180
        specification.write_text(json.dumps(specification_value), encoding="utf-8")
        prepared = prepare_real_evaluation_plan(
            suite, specification, self.root / "time-budget-plan"
        )
        candidate = Path(prepare_candidate(suite, self.root / "time-budget-candidate")["candidate"])
        binding_path = self.root / "time-budget-binding.json"
        bind_real_evaluation_candidate(Path(prepared["plan"]), candidate, binding_path)
        prepare_evaluation_attempt(
            Path(prepared["plan"]),
            binding_path,
            "synthetic-case",
            "synthetic-matrix",
            1,
            self.root / "time-budget-plan" / "attempts" / "first",
        )

        with self.assertRaises(SelcError) as exhausted:
            prepare_evaluation_attempt(
                Path(prepared["plan"]),
                binding_path,
                "synthetic-case",
                "synthetic-matrix",
                2,
                self.root / "time-budget-plan" / "attempts" / "second",
            )

        self.assertEqual(exhausted.exception.code, "EVALUATION_BUDGET_EXHAUSTED")

    def test_case_timeout_defaults_to_the_frozen_plan_timeout(self) -> None:
        suite = self.root / "short-timeout-source"
        create_suite("short-timeout-source", "1.0.0", suite, ["text-report"])
        input_path = self.root / "short-timeout-input.json"
        input_path.write_text('{"items": []}', encoding="utf-8")
        specification = self._real_plan_specification(input_path)
        specification_value = json.loads(specification.read_text(encoding="utf-8"))
        specification_value["run_timeout_seconds"] = 9
        specification_value["total_timeout_seconds"] = 9
        specification.write_text(json.dumps(specification_value), encoding="utf-8")

        prepared = prepare_real_evaluation_plan(
            suite, specification, self.root / "short-timeout-plan"
        )
        plan = json.loads(Path(prepared["plan"]).read_text(encoding="utf-8"))

        self.assertEqual(plan["cases"][0]["run_timeout_seconds"], 9)

    def test_concurrent_preparation_keeps_one_durable_reservation(self) -> None:
        """Two synthetic subprocesses cannot reserve or install the same unit twice."""

        suite = self.root / "concurrent-source"
        create_suite("concurrent-source", "1.0.0", suite, ["text-report"])
        input_path = self.root / "concurrent-input.json"
        input_path.write_text('{"items": []}', encoding="utf-8")
        prepared = prepare_real_evaluation_plan(
            suite, self._real_plan_specification(input_path), self.root / "concurrent-plan"
        )
        candidate = Path(prepare_candidate(suite, self.root / "concurrent-candidate")["candidate"])
        binding_path = self.root / "concurrent-binding.json"
        bind_real_evaluation_candidate(Path(prepared["plan"]), candidate, binding_path)
        attempt_path = self.root / "concurrent-plan" / "attempts" / "shared"
        runner = self.root / "concurrent-prepare.py"
        runner.write_text(
            "import sys\n"
            f"sys.path.insert(0, {str(REPOSITORY_ROOT / 'src')!r})\n"
            "from pathlib import Path\n"
            "from selc.errors import SelcError\n"
            "from selc.evaluation import prepare_evaluation_attempt\n"
            "try:\n"
            "    prepare_evaluation_attempt(Path(sys.argv[1]), Path(sys.argv[2]), 'synthetic-case', "
            "'synthetic-matrix', 1, Path(sys.argv[3]))\n"
            "except SelcError as error:\n"
            "    print(error.code)\n"
            "else:\n"
            "    print('prepared')\n",
            encoding="utf-8",
        )
        command = [
            sys.executable,
            str(runner),
            str(Path(prepared["plan"])),
            str(binding_path),
            str(attempt_path),
        ]
        first = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        second = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        first_stdout, first_stderr = first.communicate(timeout=20)
        second_stdout, second_stderr = second.communicate(timeout=20)

        self.assertEqual(first.returncode, 0, first_stderr)
        self.assertEqual(second.returncode, 0, second_stderr)
        self.assertTrue(
            {first_stdout.strip(), second_stdout.strip()}.issubset({"prepared", "LOCKED"})
        )
        reservations = json.loads(
            (self.root / "concurrent-plan" / "attempt-reservations.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(len(reservations["reservations"]), 1)

    def test_attempt_input_tampering_stays_unknown_during_reconstruction(self) -> None:
        suite = self.root / "tampered-attempt-source"
        create_suite("tampered-attempt-source", "1.0.0", suite, ["text-report"])
        input_path = self.root / "tampered-attempt-input.json"
        input_path.write_text('{"items": []}', encoding="utf-8")
        prepared = prepare_real_evaluation_plan(
            suite, self._real_plan_specification(input_path), self.root / "tampered-attempt-plan"
        )
        candidate = Path(
            prepare_candidate(suite, self.root / "tampered-attempt-candidate")["candidate"]
        )
        binding_path = self.root / "tampered-attempt-binding.json"
        bind_real_evaluation_candidate(Path(prepared["plan"]), candidate, binding_path)
        attempt_path = self.root / "tampered-attempt-plan" / "attempts" / "first"
        state = prepare_evaluation_attempt(
            Path(prepared["plan"]),
            binding_path,
            "synthetic-case",
            "synthetic-matrix",
            1,
            attempt_path,
        )
        Path(state["input_path"]).write_text('{"items": [{"id": "changed"}]}', encoding="utf-8")

        collected = collect_evaluation_attempt(attempt_path)

        self.assertEqual(collected["evidence"]["status"], "unknown")
        self.assertEqual(
            collected["evidence"]["reason"], "attempt-identity-or-installation-invalid"
        )

    def test_creator_verification_requires_native_policy_evidence(self) -> None:
        suite = self.root / "creator-source"
        create_suite("creator-source", "1.0.0", suite, ["text-report"])
        input_path = self.root / "creator-input.json"
        input_path.write_text('{"items": []}', encoding="utf-8")
        prepared = prepare_real_evaluation_plan(
            suite, self._real_plan_specification(input_path), self.root / "creator-plan"
        )
        candidate = Path(prepare_candidate(suite, self.root / "creator-candidate")["candidate"])
        request = self.root / "creator-request.json"
        diagnostic = prepare_creator_request(Path(prepared["plan"]), candidate, request)
        self.assertEqual(diagnostic["brief"]["status"], "diagnostic-missing")
        self.assertIn("diagnostic-only", diagnostic["prompt"])
        skill = candidate / "skills" / "text-report" / "SKILL.md"
        skill.write_text(
            skill.read_text(encoding="utf-8") + "Synthetic change.\n", encoding="utf-8"
        )

        with self.assertRaises(SelcError) as missing_evidence:
            verify_creator_result(request, candidate)
        self.assertEqual(missing_evidence.exception.code, "CREATOR_ISOLATION_UNVERIFIED")

    def test_creator_prepare_binds_brief_and_trusted_readonly_dependencies(self) -> None:
        suite = self.root / "creator-brief-source"
        create_suite("creator-brief-source", "1.0.0", suite, ["text-report"])
        input_path = self.root / "creator-brief-input.json"
        input_path.write_text('{"items": []}', encoding="utf-8")
        prepared = prepare_real_evaluation_plan(
            suite, self._real_plan_specification(input_path), self.root / "creator-brief-plan"
        )
        candidate = Path(
            prepare_candidate(suite, self.root / "creator-brief-candidate")["candidate"]
        )
        brief = self.root / "creator-brief.txt"
        brief.write_text("Correct the declared summary formatting regression.\n", encoding="utf-8")
        skill_creator, python_runtime = self._creator_tooling_roots()

        request = prepare_creator_request(
            Path(prepared["plan"]),
            candidate,
            self.root / "creator-brief-request.json",
            brief_path=brief,
            skill_creator_root=skill_creator,
            creator_python_root=python_runtime,
        )

        self.assertEqual(request["brief"]["status"], "bound")
        self.assertEqual(request["brief"]["sha256"], hashlib.sha256(brief.read_bytes()).hexdigest())
        self.assertIn(brief.read_text(encoding="utf-8"), request["prompt"])
        self.assertEqual(request["profile"]["readonly_dependencies"]["status"], "bound")
        self.assertEqual(
            request["profile"]["filesystem"]["read"][:2],
            [":minimal", str(candidate.resolve())],
        )
        self.assertEqual(
            request["profile"]["project_context"],
            {"project_doc_max_bytes": 0, "project_root_markers": []},
        )
        fixed_arguments = request["profile"]["fixed_arguments"]
        exec_index = fixed_arguments.index("exec")
        self.assertEqual(fixed_arguments[exec_index + 1], "--ignore-user-config")
        fixed_configuration = " ".join(request["profile"]["fixed_arguments"])
        self.assertIn('":minimal"="read"', fixed_configuration)
        self.assertIn(f'{json.dumps(str(candidate.resolve()))}="read"', fixed_configuration)
        self.assertIn("project_doc_max_bytes=0", fixed_configuration)
        self.assertIn("project_root_markers=[]", fixed_configuration)
        for read_root in request["profile"]["readonly_dependencies"]["read_roots"]:
            self.assertIn(f'{json.dumps(read_root)}="read"', fixed_configuration)

        revised_brief = self.root / "creator-brief-revised.txt"
        revised_brief.write_text("Correct a different formatting regression.\n", encoding="utf-8")
        revised = prepare_creator_request(
            Path(prepared["plan"]),
            candidate,
            self.root / "creator-brief-revised-request.json",
            brief_path=revised_brief,
            skill_creator_root=skill_creator,
            creator_python_root=python_runtime,
        )
        self.assertNotEqual(request["run_id"], revised["run_id"])

    def test_synthetic_creator_native_evidence_exercises_the_strict_verifier_only(self) -> None:
        """A local JSONL fixture checks parsing; it is never a real qualification."""

        suite = self.root / "creator-evidence-source"
        create_suite("creator-evidence-source", "1.0.0", suite, ["text-report"])
        input_path = self.root / "creator-evidence-input.json"
        input_path.write_text('{"items": []}', encoding="utf-8")
        prepared = prepare_real_evaluation_plan(
            suite, self._real_plan_specification(input_path), self.root / "creator-evidence-plan"
        )
        candidate = Path(
            prepare_candidate(suite, self.root / "creator-evidence-candidate")["candidate"]
        )
        brief = self.root / "creator-evidence-brief.txt"
        brief.write_text("Repair a synthetic regression.\n", encoding="utf-8")
        skill_creator, python_runtime = self._creator_tooling_roots()
        request_path = self.root / "creator-evidence-request.json"
        request = prepare_creator_request(
            Path(prepared["plan"]),
            candidate,
            request_path,
            executor_binary=Path(sys.executable).resolve(),
            executor_version=CODEX_ALPHA11_VERSION,
            brief_path=brief,
            skill_creator_root=skill_creator,
            creator_python_root=python_runtime,
        )
        fixed_configuration = " ".join(request["profile"]["fixed_arguments"])
        for protected_path in request["profile"]["filesystem"]["deny"]:
            self.assertIn(f'{json.dumps(protected_path)}="deny"', fixed_configuration)
        skill = candidate / "skills" / "text-report" / "SKILL.md"
        skill.write_text(
            skill.read_text(encoding="utf-8") + "Synthetic change.\n", encoding="utf-8"
        )

        writable_entries = [
            {
                "path": {"type": "path", "path": str((candidate / section).resolve())},
                "access": "write",
            }
            for section in ("skills", "resources")
        ]
        denied_entries = [
            {
                "path": {"type": "path", "path": path},
                "access": "deny",
            }
            for path in request["profile"]["filesystem"]["deny"]
        ]
        readonly_entries = [
            {
                "path": {"type": "special", "value": {"kind": "minimal"}},
                "access": "read",
            },
            {
                "path": {"type": "path", "path": str(candidate.resolve())},
                "access": "read",
            },
        ] + [
            {
                "path": {"type": "path", "path": path},
                "access": "read",
            }
            for path in request["profile"]["readonly_dependencies"]["read_roots"]
        ]
        account_home = _trusted_posix_account_home()
        self.assertIsNotNone(account_home)
        assert account_home is not None
        readonly_entries.append(
            {
                "path": {
                    "type": "path",
                    "path": str(
                        account_home / ".codex" / "tmp" / "arg0-macos" / "codex-arg0Synthetic"
                    ),
                },
                "access": "read",
            }
        )
        native_entries = writable_entries + readonly_entries + denied_entries
        session_id = "creator-synthetic-session"
        turn_id = "creator-synthetic-turn"
        raw_records = [
            {
                "type": "session_meta",
                "payload": {
                    "id": session_id,
                    "session_id": session_id,
                    "cwd": str(candidate.resolve()),
                    "cli_version": CODEX_ALPHA11_VERSION,
                    "source": "exec",
                },
            },
            {
                "type": "turn_context",
                "payload": {
                    "turn_id": turn_id,
                    "root_turn_id": turn_id,
                    "cwd": str(candidate.resolve()),
                    "model": "gpt-5.6-terra",
                    "approval_policy": "never",
                    "permission_profile": {
                        "type": "managed",
                        "file_system": {"type": "restricted", "entries": native_entries},
                        "network": "restricted",
                    },
                    "sandbox_policy": {
                        "type": "workspace-write",
                        "network_access": False,
                        "exclude_tmpdir_env_var": True,
                        "exclude_slash_tmp": True,
                    },
                    "file_system_sandbox_policy": {
                        "kind": "restricted",
                        "entries": native_entries,
                    },
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": request["prompt"]}],
                    "internal_chat_message_metadata_passthrough": {"turn_id": turn_id},
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "exec",
                    "call_id": "creator-tool",
                    "input": {"cmd": "skill-creator validate"},
                    "id": "creator-tool-item",
                    "status": "completed",
                    "internal_chat_message_metadata_passthrough": {"turn_id": turn_id},
                },
            },
        ]
        raw = b"".join((json.dumps(record) + "\n").encode("utf-8") for record in raw_records)
        raw_path = self.root / ".codex" / "sessions" / "2026" / f"{session_id}.jsonl"
        raw_path.parent.mkdir(parents=True)
        raw_path.write_bytes(raw)
        evidence_path = self.root / "creator-evidence.json"

        def write_evidence(raw_bytes: bytes) -> None:
            evidence_path.write_text(
                json.dumps(
                    {
                        "raw_session_path": str(raw_path),
                        "raw_session_sha256": hashlib.sha256(raw_bytes).hexdigest(),
                        "raw_session_cutoff_bytes": len(raw_bytes),
                        "request_hash": request["request_hash"],
                        "protected_before_hashes": request["protected_before_hashes"],
                        "protected_after_hashes": request["protected_before_hashes"],
                    }
                ),
                encoding="utf-8",
            )

        forged = (
            json.dumps(
                {
                    "type": "turn_context",
                    "payload": {
                        "model": "gpt-5.6-terra",
                        "approval_policy": "never",
                        "permission_profile": {"network": "restricted"},
                        "sandbox_policy": {"network_access": False},
                        "file_system_sandbox_policy": {
                            "entries": writable_entries + denied_entries
                        },
                        "tool_name": "exec",
                        "creator_run": request["run_id"],
                    },
                }
            ).encode("utf-8")
            + b"\n"
        )
        raw_path.write_bytes(forged)
        write_evidence(forged)
        with (
            patch("selc.creator.Path.home", return_value=self.root),
            self.assertRaises(SelcError) as rejected,
        ):
            verify_creator_result(request_path, candidate, evidence_path)
        self.assertEqual(rejected.exception.code, "CREATOR_ISOLATION_UNVERIFIED")

        raw_path.write_bytes(raw)
        write_evidence(raw)

        with patch("selc.creator.Path.home", return_value=self.root):
            verified = verify_creator_result(request_path, candidate, evidence_path)

        self.assertTrue(verified["isolation_verified"])

        def assert_policy_rejected(records: list[dict[str, object]]) -> None:
            altered = b"".join((json.dumps(record) + "\n").encode("utf-8") for record in records)
            raw_path.write_bytes(altered)
            write_evidence(altered)
            with (
                patch("selc.creator.Path.home", return_value=self.root),
                self.assertRaises(SelcError) as rejected,
            ):
                verify_creator_result(request_path, candidate, evidence_path)
            self.assertEqual(rejected.exception.code, "CREATOR_ISOLATION_UNVERIFIED")

        extra_read = json.loads(json.dumps(raw_records))
        extra_entry = {
            "path": {"type": "path", "path": str((self.root / "unexpected-read").resolve())},
            "access": "read",
        }
        extra_read[1]["payload"]["permission_profile"]["file_system"]["entries"].append(extra_entry)
        extra_read[1]["payload"]["file_system_sandbox_policy"]["entries"].append(extra_entry)
        assert_policy_rejected(extra_read)

        extra_write = json.loads(json.dumps(raw_records))
        extra_entry["access"] = "write"
        extra_write[1]["payload"]["permission_profile"]["file_system"]["entries"].append(
            extra_entry
        )
        extra_write[1]["payload"]["file_system_sandbox_policy"]["entries"].append(extra_entry)
        assert_policy_rejected(extra_write)

        inconsistent = json.loads(json.dumps(raw_records))
        inconsistent[1]["payload"]["file_system_sandbox_policy"]["entries"].pop()
        assert_policy_rejected(inconsistent)

        wrong_bootstrap = json.loads(json.dumps(raw_records))
        for entries in (
            wrong_bootstrap[1]["payload"]["permission_profile"]["file_system"]["entries"],
            wrong_bootstrap[1]["payload"]["file_system_sandbox_policy"]["entries"],
        ):
            for entry in entries:
                path = entry["path"]
                if path.get("type") == "path" and path.get("path", "").endswith("arg0Synthetic"):
                    path["path"] = "/wrong-account/.codex/tmp/arg0-macos/codex-arg0Synthetic"
        assert_policy_rejected(wrong_bootstrap)

    def test_native_evidence_template_describes_only_reconstruction_inputs(self) -> None:
        suite = self.root / "template-source"
        create_suite("template-source", "1.0.0", suite, ["text-report"])
        input_path = self.root / "template-input.json"
        input_path.write_text('{"items": []}', encoding="utf-8")
        specification = self._real_plan_specification(input_path)
        self._add_supported_codex_policy_template(specification, "template-python")
        prepared = prepare_real_evaluation_plan(
            suite,
            specification,
            self.root / "template-plan",
        )
        candidate = Path(prepare_candidate(suite, self.root / "template-candidate")["candidate"])
        binding_path = self.root / "template-binding.json"
        bind_real_evaluation_candidate(Path(prepared["plan"]), candidate, binding_path)
        attempt_path = self.root / "template-plan" / "attempts" / "first"
        prepare_evaluation_attempt(
            Path(prepared["plan"]),
            binding_path,
            "synthetic-case",
            "synthetic-matrix",
            1,
            attempt_path,
        )

        template = native_evidence_template(attempt_path)

        self.assertEqual(template["schema_version"], 1)
        self.assertIn("raw_session_sha256", template["required_fields"])
        self.assertFalse(Path(template["evidence_path"]).exists())

    def test_resumed_launch_claim_becomes_unknown_without_starting_a_process(self) -> None:
        suite = self.root / "claim-source"
        create_suite("claim-source", "1.0.0", suite, ["text-report"])
        input_path = self.root / "claim-input.json"
        input_path.write_text('{"items": []}', encoding="utf-8")
        specification = self._real_plan_specification(input_path, adapter="codex-cli")
        self._add_supported_codex_policy_template(specification, "claim-python")
        prepared = prepare_real_evaluation_plan(suite, specification, self.root / "claim-plan")
        candidate = Path(prepare_candidate(suite, self.root / "claim-candidate")["candidate"])
        binding_path = self.root / "claim-binding.json"
        bind_real_evaluation_candidate(Path(prepared["plan"]), candidate, binding_path)
        attempt_path = self.root / "claim-plan" / "attempts" / "first"
        prepare_evaluation_attempt(
            Path(prepared["plan"]),
            binding_path,
            "synthetic-case",
            "synthetic-matrix",
            1,
            attempt_path,
        )
        (attempt_path / "launch-claim.json").write_text('{"interrupted": true}\n', encoding="utf-8")

        with self.assertRaises(SelcError) as claimed:
            launch_codex_cli_attempt(attempt_path)

        self.assertEqual(claimed.exception.code, "ATTEMPT_ALREADY_CLAIMED")
        state = json.loads((attempt_path / "attempt.json").read_text(encoding="utf-8"))
        self.assertEqual(state["state"], "unknown")

    def test_policy_binding_tampering_refuses_cli_before_launch(self) -> None:
        suite = self.root / "policy-binding-source"
        create_suite("policy-binding-source", "1.0.0", suite, ["text-report"])
        input_path = self.root / "policy-binding-input.json"
        input_path.write_text('{"items": []}', encoding="utf-8")
        specification = self._real_plan_specification(input_path, adapter="codex-cli")
        self._add_supported_codex_policy_template(specification, "policy-binding-python")
        prepared = prepare_real_evaluation_plan(
            suite, specification, self.root / "policy-binding-plan"
        )
        candidate = Path(
            prepare_candidate(suite, self.root / "policy-binding-candidate")["candidate"]
        )
        binding_path = self.root / "policy-binding.json"
        bind_real_evaluation_candidate(Path(prepared["plan"]), candidate, binding_path)
        attempt_path = self.root / "policy-binding-plan" / "attempts" / "first"
        prepare_evaluation_attempt(
            Path(prepared["plan"]),
            binding_path,
            "synthetic-case",
            "synthetic-matrix",
            1,
            attempt_path,
        )
        state_path = attempt_path / "attempt.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        plan = json.loads(Path(prepared["plan"]).read_text(encoding="utf-8"))
        command = _fixed_codex_cli_command(Path(sys.executable), plan["matrix"][0], state)
        fixed_configuration = " ".join(command)
        exec_index = command.index("exec")
        self.assertEqual(command[exec_index + 1], "--ignore-user-config")
        python_root = str((self.root / "policy-binding-python").resolve())
        self.assertIn(f'{json.dumps(python_root)}="read"', fixed_configuration)
        state["policy_binding"]["cli_configuration_hash"] = "0" * 64
        state_path.write_text(json.dumps(state), encoding="utf-8")

        with self.assertRaises(SelcError) as rejected:
            launch_codex_cli_attempt(attempt_path)

        self.assertEqual(rejected.exception.code, "ATTEMPT_TAMPERED")
        self.assertFalse((attempt_path / "launch-claim.json").exists())

    def test_generic_explicit_managed_declaration_supports_arbitrary_skill_names(self) -> None:
        suite = self.root / "generic-suite"
        declaration = {
            "protocol": 1,
            "native_hosts": {"codex": ["synthetic"]},
            "actions": {
                "invoice-stage": {
                    "skill": "invoice-writer",
                    "kind": "instruction-staging-v1",
                    "input": {"fields": {}, "required": []},
                }
            },
        }
        create_suite(
            "generic-suite",
            "1.0.0",
            suite,
            ["invoice-writer"],
            managed=declaration,
        )
        self.assertEqual(load_manifest(suite)["managed"], declaration)

    def test_generic_authoring_declaration_binds_skill_artifact_checks(self) -> None:
        suite = self.root / "checked-generic-suite"
        declaration = {
            "managed": {
                "protocol": 1,
                "native_hosts": {"codex": ["synthetic"]},
                "actions": {
                    "invoice-stage": {
                        "skill": "invoice-writer",
                        "kind": "instruction-staging-v1",
                        "input": {"fields": {}, "required": []},
                    }
                },
            },
            "artifact_checks": {
                "invoice-json": {
                    "checker": {"name": "json-item-summary", "version": "1"},
                    "rule_version": "1",
                    "required_rule_ids": ["count", "sum"],
                    "configuration": {"duplicate_ids_count_once": True},
                }
            },
            "skill_artifact_checks": {"invoice-writer": "invoice-json"},
        }
        create_suite(
            "checked-generic-suite",
            "1.0.0",
            suite,
            ["invoice-writer"],
            managed=declaration,
        )

        manifest = load_manifest(suite)
        self.assertEqual(manifest["skills"][0]["artifact_check"], "invoice-json")
        self.assertIn("invoice-json", manifest["artifact_checks"])


if __name__ == "__main__":
    unittest.main()
