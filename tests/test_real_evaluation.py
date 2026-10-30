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
    CODEX_FIXED_PREPARATION_INSTRUCTIONS,
    _action_request_from_prompt,
    _attempt_id,
    _codex_cli_launch_trust_failure,
    _codex_cli_prompt,
    _codex_hook_trust_changed,
    _codex_installed_hook_definitions,
    _fixed_codex_cli_command,
    _fixed_codex_cli_profile,
    _fixed_codex_environment,
    _fixed_codex_preparation_guidance,
    _match_codex_native_policy,
    _native_publication_result_failure,
    _native_session_facts,
    _native_window_failure,
    _normalise_codex_policy_template,
    _prepare_codex_hook_trust_binding,
    _prepare_policy_binding,
    _project_trust_cli_configuration,
    _qualification_support,
    _read_attempt_native_context,
    _reconstruct_attempt,
    _render_codex_explicit_routing_instructions,
    _workbuddy_session_diagnostics,
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
        """Keep pre-schema3 fixtures on their frozen schema-2 profile."""

        python_root = self.root / name
        python_root.mkdir()
        value = json.loads(specification.read_text(encoding="utf-8"))
        matrix = value["matrix"][0]
        matrix["host_version"] = "0.163.0-alpha.11"
        matrix.pop("tools_policy_digest", None)
        template = build_codex_policy_template("0.163.0-alpha.11", python_root)
        template["schema_version"] = 2
        template.pop("routing")
        template["cli"] = _fixed_codex_cli_profile(2)
        template["preparation_guidance"] = _fixed_codex_preparation_guidance()
        matrix["tools_policy_template"] = template
        specification.write_text(json.dumps(value), encoding="utf-8")

    def _schema2_codex_policy_template(self, python_root: Path) -> dict[str, object]:
        """Build the exact previous schema for frozen-plan compatibility tests."""

        template = build_codex_policy_template("0.163.0-alpha.11", python_root)
        template["schema_version"] = 2
        template.pop("routing")
        template["cli"] = _fixed_codex_cli_profile(2)
        template["preparation_guidance"] = _fixed_codex_preparation_guidance()
        return template

    def _prepared_hook_trust_attempt(
        self,
        *,
        ancestor_git_root: bool = True,
    ) -> tuple[Path, Path, dict[str, object], dict[str, object]]:
        if ancestor_git_root:
            git(["init", "-b", "main"], self.root)
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

    def _prepared_schema3_routing_attempt(
        self,
        label: str = "first",
    ) -> tuple[Path, dict[str, object], dict[str, object]]:
        """Prepare one installed schema-3 attempt with a declared fixed action."""

        suite_id = f"schema3-routing-{label}"
        suite = self.root / f"{suite_id}-source"
        create_suite(suite_id, "1.0.0", suite, ["text-report"])
        manifest_path = suite / "suite.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["managed"]["native_hosts"] = {"codex": ["0.163.0-alpha.11"]}
        manifest["managed"]["actions"]["text-report"] = {
            "skill": "text-report",
            "kind": "python-script-v1",
            "script": "bin/schema3-summary.py",
            "input": {"fields": {"input": {"type": "path"}}, "required": ["input"]},
        }
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        (suite / "bin" / "schema3-summary.py").write_text(
            "from pathlib import Path\nPath('unused.json').write_text('{}', encoding='utf-8')\n",
            encoding="utf-8",
        )
        seal_suite(suite)

        input_path = self.root / f"{suite_id}-input.json"
        input_path.write_text('{"items": []}', encoding="utf-8")
        plan_root = self.root / f"{suite_id}-plan"
        attempt_path = plan_root / "attempts" / "first"
        attempted_input = attempt_path / "project" / "evaluation-input.json"
        python_root = self.root / f"{suite_id}-python"
        python_root.mkdir()
        template = build_codex_policy_template("0.163.0-alpha.11", python_root)
        specification = self.root / f"{suite_id}-specification.json"
        specification.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "cases": [
                        {
                            "id": "schema3-case",
                            "collection": "regression",
                            "input": str(input_path),
                            "request": f'@text-report {{"input":{json.dumps(str(attempted_input))}}}',
                            "checker": {
                                "name": "json-item-summary",
                                "version": "1",
                                "rules": ["count"],
                            },
                        }
                    ],
                    "matrix": [
                        {
                            "id": "schema3-codex",
                            "adapter": "codex-desktop",
                            "host": "codex",
                            "host_version": "0.163.0-alpha.11",
                            "surface": "cli",
                            "model": "synthetic-model",
                            "tools_policy_template": template,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        prepared = prepare_real_evaluation_plan(suite, specification, plan_root)
        candidate = Path(prepare_candidate(suite, self.root / f"{suite_id}-candidate")["candidate"])
        binding_path = self.root / f"{suite_id}-binding.json"
        bind_real_evaluation_candidate(Path(prepared["plan"]), candidate, binding_path)
        state = prepare_evaluation_attempt(
            Path(prepared["plan"]),
            binding_path,
            "schema3-case",
            "schema3-codex",
            1,
            attempt_path,
        )
        plan = json.loads(Path(prepared["plan"]).read_text(encoding="utf-8"))
        return attempt_path, state, plan["matrix"][0]

    def _write_normal_hook_trust_state(
        self,
        account_home: Path,
        state: dict[str, object],
        *,
        replacement_source: Path | None = None,
        disabled: bool = False,
        extra_same_source: bool = False,
        project_trust: str | None = "trusted",
        project_key: str | None = None,
        more_specific_untrusted: bool = False,
        unrelated_sibling_untrusted: bool = False,
        trust_root: Path | None = None,
    ) -> list[dict[str, object]]:
        project_root = Path(str(state["project_root"]))
        actual_root = (trust_root or self.root).resolve()
        installation = current_installation(project_root, str(state["installation"]["suite_id"]))
        definitions, _source_hash = _codex_installed_hook_definitions(
            project_root / ".codex" / "hooks.json",
            installation["owned_hooks"],
        )
        source = replacement_source or (project_root / ".codex" / "hooks.json")
        lines: list[str] = []
        if project_trust is not None:
            trusted_project_key = project_key or str(actual_root)
            lines.extend(
                (
                    f"[projects.{json.dumps(trusted_project_key)}]",
                    f"trust_level = {json.dumps(project_trust)}",
                    "",
                )
            )
        if more_specific_untrusted:
            child_key = str(project_root.parent.resolve())
            lines.extend(
                (
                    f"[projects.{json.dumps(child_key)}]",
                    'trust_level = "untrusted"',
                    "",
                )
            )
        if unrelated_sibling_untrusted:
            sibling_key = str((actual_root / "unrelated-sibling").resolve())
            lines.extend(
                (
                    f"[projects.{json.dumps(sibling_key)}]",
                    'trust_level = "untrusted"',
                    "",
                )
            )
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

        self.assertEqual(template["schema_version"], 3)
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
        self.assertEqual(
            template["preparation_guidance"]["summary"],
            "explicit-installed-routing-read-and-native-managed-action-v2",
        )
        self.assertNotIn("developer_instructions", template["cli"]["settings"])
        self.assertEqual(template["policy"]["approval_policy"], "never")
        self.assertEqual(
            template["hook_trust"]["mode"],
            "normal-project-and-hook-state-inheritance-v2",
        )

    def test_default_codex_policy_template_uses_explicit_routing_schema3(self) -> None:
        """New plans bind an installed routing entry instead of a generic placeholder."""

        python_root = self.root / "schema3-evaluation-python"
        python_root.mkdir()

        template = build_codex_policy_template("0.163.0-alpha.11", python_root)

        self.assertEqual(template["schema_version"], 3)
        self.assertEqual(
            template["routing"],
            {
                "schema_version": 1,
                "parameter": "installed-registered-routing-entry",
                "renderer": "exact-single-cat-command-v1",
            },
        )
        self.assertEqual(
            template["preparation_guidance"]["summary"],
            "explicit-installed-routing-read-and-native-managed-action-v2",
        )

    def test_schema3_binding_derives_one_installed_routing_command(self) -> None:
        """A frozen explicit request selects one installer-owned routing entry."""

        _attempt_path, state, matrix = self._prepared_schema3_routing_attempt()

        policy_binding = state["policy_binding"]
        routing = policy_binding["routing"]
        project_root = Path(state["project_root"])
        expected_path = project_root / ".agents" / "skills" / "text-report" / "SKILL.md"
        expected_command = shlex.join(("/bin/cat", str(expected_path)))

        self.assertEqual(routing["skill"], "text-report")
        self.assertEqual(routing["action"], "text-report")
        self.assertEqual(routing["entry_path"], str(expected_path))
        self.assertEqual(routing["command"], expected_command)
        self.assertEqual(
            routing["installation"],
            {
                "suite_id": "schema3-routing-first",
                "version": state["installation"]["version"],
                "composite": state["installation"]["digest"],
            },
        )
        self.assertEqual(
            policy_binding["preparation_guidance"]["summary"],
            "explicit-installed-routing-read-and-native-managed-action-v2",
        )

        command = _fixed_codex_cli_command(Path(sys.executable), matrix, state)
        self.assertIn(
            "developer_instructions="
            + json.dumps(_render_codex_explicit_routing_instructions(expected_command)),
            command,
        )

    def test_schema3_binding_rejects_ambiguous_selection_and_unowned_routes(self) -> None:
        """Schema 3 accepts neither an automatic request nor a replaced registration."""

        _attempt_path, state, matrix = self._prepared_schema3_routing_attempt()
        project_root = Path(state["project_root"])
        expected_installation = state["installation"]
        suite_id = str(expected_installation["suite_id"])

        for request in (
            json.dumps({"input": str(project_root / "evaluation-input.json")}),
            f'@not-declared {{"input":{json.dumps(str(project_root / "evaluation-input.json"))}}}',
            f'@text-report {{"input":{json.dumps(str(project_root / "other-input.json"))}}}',
            f'@text-report @another {{"input":{json.dumps(str(project_root / "evaluation-input.json"))}}}',
        ):
            case = dict(state["case"])
            case["request"] = request
            with self.subTest(request=request), self.assertRaises(SelcError) as rejected:
                _prepare_policy_binding(
                    matrix,
                    project_root,
                    case=case,
                    suite_id=suite_id,
                    expected_installation=expected_installation,
                )
            self.assertEqual(rejected.exception.code, "ATTEMPT_TAMPERED")

        registration = project_root / ".agents" / "skills" / "text-report"
        registration.unlink()
        registration.mkdir()
        (registration / "SKILL.md").write_text("# replacement\n", encoding="utf-8")
        with self.assertRaises(SelcError) as rejected:
            _prepare_policy_binding(
                matrix,
                project_root,
                case=state["case"],
                suite_id=suite_id,
                expected_installation=expected_installation,
            )
        self.assertEqual(rejected.exception.code, "ATTEMPT_TAMPERED")

    def test_schema3_binding_rechecks_entry_and_claim_before_launch(self) -> None:
        """Entry drift and a changed routing command stop before the CLI starts."""

        _attempt_path, state, matrix = self._prepared_schema3_routing_attempt()
        routing_path = Path(state["policy_binding"]["routing"]["entry_path"])
        entry = routing_path.resolve()
        entry.write_text(entry.read_text(encoding="utf-8") + "drift\n", encoding="utf-8")

        with self.assertRaises(SelcError) as drifted:
            _fixed_codex_cli_command(Path(sys.executable), matrix, state)
        self.assertEqual(drifted.exception.code, "ATTEMPT_TAMPERED")

        _attempt_path, state, matrix = self._prepared_schema3_routing_attempt("claim")
        tampered_state = json.loads(json.dumps(state))
        tampered_state["policy_binding"]["routing"]["command"] += " && /bin/false"
        with self.assertRaises(SelcError) as tampered:
            _fixed_codex_cli_command(Path(sys.executable), matrix, tampered_state)
        self.assertEqual(tampered.exception.code, "ATTEMPT_TAMPERED")

    def test_schema3_native_fragment_requires_the_bound_rendered_command(self) -> None:
        """Native collection rejects a changed schema-3 literal before qualification."""

        _attempt_path, state, matrix = self._prepared_schema3_routing_attempt()
        project_root = Path(state["project_root"])
        template = matrix["tools_policy_template"]
        prompt = state["case"]["request"]
        routing_command = state["policy_binding"]["routing"]["command"]
        instructions = _render_codex_explicit_routing_instructions(routing_command)
        turn_id = "schema3-native-turn"
        records = [
            {
                "type": "session_meta",
                "payload": {
                    "id": "schema3-native-session",
                    "session_id": "schema3-native-session",
                    "cwd": str(project_root),
                    "cli_version": "0.163.0-alpha.11",
                    "source": "exec",
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "developer",
                    "content": [
                        {"type": "input_text", "text": "known host instructions"},
                        {"type": "input_text", "text": instructions},
                    ],
                    "internal_chat_message_metadata_passthrough": {
                        "turn_id": turn_id,
                        "content_item_kinds": [
                            "host_skills.instructions",
                            "generic.developer_instructions",
                        ],
                    },
                },
            },
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
            },
            {
                "type": "turn_context",
                "payload": {
                    "turn_id": turn_id,
                    "root_turn_id": turn_id,
                    "cwd": str(project_root),
                    "model": "synthetic-model",
                    "permission_profile": {},
                    "sandbox_policy": {},
                    "approval_policy": "never",
                    "file_system_sandbox_policy": {},
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "exec",
                    "call_id": "schema3-native-call",
                    "id": "schema3-native-item",
                    "status": "completed",
                    "input": {},
                    "internal_chat_message_metadata_passthrough": {"turn_id": turn_id},
                },
            },
        ]

        def parse(candidate_records: list[dict[str, object]]) -> object:
            raw = b"".join(
                (json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8")
                for record in candidate_records
            )
            return _native_session_facts(
                raw,
                prompt,
                self.root / "schema3-native.jsonl",
                project_root,
                template,
                state["policy_binding"],
            )

        self.assertIsNotNone(parse(records))
        tampered = json.loads(json.dumps(records))
        tampered[1]["payload"]["content"][1]["text"] = instructions + "\n/bin/false"
        self.assertIsNone(parse(tampered))

    def test_schema3_bindings_use_one_rule_for_distinct_installed_attempts(self) -> None:
        """Separate attempts bind their own route without changing schema-3 rendering rules."""

        _first_path, first_state, first_matrix = self._prepared_schema3_routing_attempt()
        _second_path, second_state, second_matrix = self._prepared_schema3_routing_attempt("second")

        self.assertEqual(
            first_matrix["tools_policy_template"]["routing"],
            second_matrix["tools_policy_template"]["routing"],
        )
        self.assertEqual(
            first_state["policy_binding"]["routing"]["schema_version"],
            second_state["policy_binding"]["routing"]["schema_version"],
        )
        self.assertNotEqual(
            first_state["policy_binding"]["routing"]["entry_path"],
            second_state["policy_binding"]["routing"]["entry_path"],
        )

    def test_schema1_codex_policy_template_preserves_the_frozen_profile_and_argv(self) -> None:
        """Schema 1 normalizes and renders without schema 2 guidance."""

        _account_home, _attempt_path, state, matrix = self._prepared_hook_trust_attempt()
        legacy_python = self.root / "legacy-python"
        legacy_python.mkdir()
        legacy = self._schema2_codex_policy_template(legacy_python)
        legacy["schema_version"] = 1
        legacy.pop("preparation_guidance")
        legacy["cli"] = _fixed_codex_cli_profile(1)
        matrix["tools_policy_template"] = legacy
        matrix["tools_policy_template_hash"] = hashlib.sha256(
            json.dumps(legacy, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        state["policy_binding"] = _prepare_policy_binding(
            matrix,
            Path(str(state["project_root"])),
        )

        self.assertEqual(
            _normalise_codex_policy_template(legacy, "0.163.0-alpha.11"),
            legacy,
        )
        command = _fixed_codex_cli_command(Path(sys.executable).resolve(), matrix, state)
        project_root = str(Path(str(state["project_root"])).resolve())
        python_root = str(legacy_python.resolve())
        expected_permissions = (
            'permissions.selc-evaluation.filesystem={":minimal"="read",'
            f'{json.dumps(python_root)}="read",'
            f'{json.dumps(project_root)}="write"}}'
        )
        self.assertEqual(
            command,
            [
                str(Path(sys.executable).resolve()),
                "-a",
                "never",
                "exec",
                "--ignore-user-config",
                "--skip-git-repo-check",
                "--strict-config",
                "--json",
                "--color",
                "never",
                "-m",
                str(matrix["model"]),
                "-c",
                'model_provider="openai"',
                "-c",
                'web_search="disabled"',
                "--enable",
                "hooks",
                *[
                    option
                    for feature in (
                        "apps",
                        "plugins",
                        "remote_plugin",
                        "skill_search",
                        "multi_agent",
                        "computer_use",
                        "browser_use",
                        "image_generation",
                    )
                    for option in ("--disable", feature)
                ],
                "-C",
                project_root,
                "-c",
                'default_permissions="selc-evaluation"',
                "-c",
                expected_permissions,
                "-c",
                "permissions.selc-evaluation.network.enabled=false",
                "-c",
                "project_doc_max_bytes=0",
                "-c",
                "project_root_markers=[]",
                "-",
            ],
        )
        self.assertNotIn("developer_instructions", " ".join(command))

    def test_schema2_native_preparation_fragment_requires_exact_initial_literal(self) -> None:
        """A schema 2 fragment is one indexed developer item before the request."""

        project_root = self.root / "native-guidance-project"
        project_root.mkdir()
        raw_path = self.root / ".codex" / "sessions" / "2026" / "native-guidance.jsonl"
        prompt = '@text-report {"input":"items.json"}'
        guidance_python = self.root / "guidance-python"
        guidance_python.mkdir()
        template = self._schema2_codex_policy_template(guidance_python)
        base_records = [
            {
                "type": "session_meta",
                "payload": {
                    "id": "native-guidance",
                    "session_id": "native-guidance",
                    "cwd": str(project_root),
                    "cli_version": "0.163.0-alpha.11",
                    "source": "exec",
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "developer",
                    "content": [
                        {"type": "input_text", "text": "known host instructions"},
                        {"type": "input_text", "text": CODEX_FIXED_PREPARATION_INSTRUCTIONS},
                    ],
                    "internal_chat_message_metadata_passthrough": {
                        "turn_id": "native-guidance-turn",
                        "content_item_kinds": [
                            "host_skills.instructions",
                            "generic.developer_instructions",
                        ],
                    },
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": prompt}],
                    "internal_chat_message_metadata_passthrough": {
                        "turn_id": "native-guidance-turn",
                        "content_item_kinds": ["user.text"],
                    },
                },
            },
            {
                "type": "turn_context",
                "payload": {
                    "turn_id": "native-guidance-turn",
                    "root_turn_id": "native-guidance-turn",
                    "cwd": str(project_root),
                    "model": "synthetic-model",
                    "permission_profile": {},
                    "sandbox_policy": {},
                    "approval_policy": "never",
                    "file_system_sandbox_policy": {},
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "exec",
                    "call_id": "native-guidance-call",
                    "id": "native-guidance-item",
                    "status": "completed",
                    "input": {},
                    "internal_chat_message_metadata_passthrough": {
                        "turn_id": "native-guidance-turn",
                    },
                },
            },
        ]

        def parse(
            records: list[dict[str, object]],
            policy_template: dict[str, object] = template,
        ) -> object:
            raw = b"".join(
                (json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8")
                for record in records
            )
            return _native_session_facts(raw, prompt, raw_path, project_root, policy_template)

        parsed = parse(base_records)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed[-1], template["preparation_guidance"])

        legacy_template = json.loads(json.dumps(template))
        legacy_template["schema_version"] = 1
        legacy_template.pop("preparation_guidance")
        legacy_template["cli"] = _fixed_codex_cli_profile(1)
        legacy_records = json.loads(json.dumps(base_records))
        legacy_records[1]["payload"].pop("content")
        legacy_records[1]["payload"]["internal_chat_message_metadata_passthrough"][
            "content_item_kinds"
        ] = ["host_skills.instructions"]
        self.assertIsNotNone(parse(legacy_records, legacy_template))
        self.assertIsNone(parse(base_records, legacy_template))

        duplicate = [*base_records]
        duplicate.insert(2, json.loads(json.dumps(base_records[1])))
        self.assertIsNone(parse(duplicate))

        wrong_turn = json.loads(json.dumps(base_records))
        wrong_turn[1]["payload"]["internal_chat_message_metadata_passthrough"]["turn_id"] = (
            "another-turn"
        )
        self.assertIsNone(parse(wrong_turn))

        late = [base_records[0], *base_records[2:], base_records[1]]
        self.assertIsNone(parse(late))

        unknown_kind = json.loads(json.dumps(base_records))
        unknown_kind[1]["payload"]["content"].append({"type": "input_text", "text": "unsupported"})
        unknown_kind[1]["payload"]["internal_chat_message_metadata_passthrough"][
            "content_item_kinds"
        ].append("unknown.developer.instructions")
        self.assertIsNone(parse(unknown_kind))

        wrong_vector = json.loads(json.dumps(base_records))
        wrong_vector[1]["payload"]["content"].pop()
        self.assertIsNone(parse(wrong_vector))

    def test_generated_maintenance_describes_schema3_and_workbuddy_diagnostics(self) -> None:
        suite = self.root / "maintenance-source"
        create_suite("maintenance-source", "1.0.0", suite, ["text-report"])

        maintenance = (suite / "MAINTENANCE.md").read_text(encoding="utf-8")
        self.assertIn(
            "Schema-3 Codex templates bind one explicit installer-owned routing entry", maintenance
        )
        self.assertIn(
            "Schema-1 and schema-2 templates retain their original profiles and argv", maintenance
        )
        self.assertIn("WorkBuddy has a bounded native-session diagnostic", maintenance)
        self.assertIn("its qualification remains unverified", maintenance)

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

        template = build_codex_policy_template("0.163.0-alpha.11", python_root)
        template["hook_trust"] = {
            "schema_version": 1,
            "mode": "normal-state-inheritance-v1",
        }
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
        project_configuration = next(
            command[index + 1]
            for index, argument in enumerate(command)
            if argument == "-c" and command[index + 1].startswith("projects=")
        )
        parsed_configuration = tomllib.loads(trust_configuration)
        parsed_project_configuration = tomllib.loads(project_configuration)
        developer_instructions = next(
            command[index + 1]
            for index, argument in enumerate(command)
            if argument == "-c" and command[index + 1].startswith("developer_instructions=")
        )
        self.assertEqual(command.count("-c"), 10)
        self.assertEqual(
            developer_instructions,
            f"developer_instructions={json.dumps(CODEX_FIXED_PREPARATION_INSTRUCTIONS)}",
        )
        self.assertEqual(
            parsed_configuration["hooks"]["state"],
            {
                definition["state_key"]: {"trusted_hash": f"sha256:{index + 1:064x}"}
                for index, definition in enumerate(definitions)
            },
        )
        self.assertEqual(
            trust_binding["project_trust"],
            {
                "raw_key": str(self.root.resolve()),
                "trust_level": "trusted",
            },
        )
        self.assertEqual(trust_binding["account_home"], str(account_home))
        self.assertEqual(
            parsed_project_configuration["projects"],
            {str(self.root.resolve()): {"trust_level": "trusted"}},
        )
        self.assertFalse((Path(str(state["project_root"])) / ".git").exists())
        self.assertNotIn(str(self.root / "other-project"), trust_configuration)
        self.assertNotIn(str(self.root / "other-project"), project_configuration)

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

        tampered_binding = json.loads(json.dumps(trust_binding))
        tampered_binding["project_trust"]["raw_key"] = "/unexpected/project"
        tampered_launch = dict(launch)
        tampered_launch["hook_trust"] = tampered_binding
        tampered_launch["hook_trust_hash"] = json_hash(tampered_binding)
        state["launch_claim_hash"] = json_hash(tampered_launch)
        (attempt_path / "launch-claim.json").write_text(
            json.dumps(tampered_launch),
            encoding="utf-8",
        )
        self.assertEqual(
            _codex_cli_launch_trust_failure(state, matrix),
            "hook-trust-binding-invalid",
        )

        state["launch_claim_hash"] = json_hash(launch)
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

    def test_hook_trust_binding_rejects_missing_untrusted_aliased_or_more_specific_project_trust(
        self,
    ) -> None:
        account_home, _attempt_path, state, matrix = self._prepared_hook_trust_attempt()

        with patch("selc.evaluation._trusted_posix_account_home", return_value=account_home):
            self._write_normal_hook_trust_state(account_home, state, project_trust=None)
            with self.assertRaises(SelcError) as missing:
                _prepare_codex_hook_trust_binding(state, matrix)
            self.assertEqual(missing.exception.code, "HOOK_TRUST_SOURCE_INVALID")

            self._write_normal_hook_trust_state(account_home, state, project_trust="untrusted")
            with self.assertRaises(SelcError) as untrusted:
                _prepare_codex_hook_trust_binding(state, matrix)
            self.assertEqual(untrusted.exception.code, "HOOK_TRUST_SOURCE_INVALID")

            alias = str(self.root / "hook-trust-plan" / "..")
            self._write_normal_hook_trust_state(account_home, state, project_key=alias)
            with self.assertRaises(SelcError) as aliased:
                _prepare_codex_hook_trust_binding(state, matrix)
            self.assertEqual(aliased.exception.code, "HOOK_TRUST_SOURCE_INVALID")

            self._write_normal_hook_trust_state(
                account_home,
                state,
                more_specific_untrusted=True,
            )
            with self.assertRaises(SelcError) as more_specific:
                _prepare_codex_hook_trust_binding(state, matrix)
            self.assertEqual(more_specific.exception.code, "HOOK_TRUST_SOURCE_INVALID")

            self._write_normal_hook_trust_state(
                account_home,
                state,
                unrelated_sibling_untrusted=True,
            )
            self.assertIsNotNone(_prepare_codex_hook_trust_binding(state, matrix))

    def test_hook_trust_binding_accepts_a_direct_trusted_repository_root(self) -> None:
        account_home, _attempt_path, state, matrix = self._prepared_hook_trust_attempt(
            ancestor_git_root=False
        )
        project_root = Path(str(state["project_root"])).resolve()
        self.assertFalse((project_root / ".git").exists())
        git(["init", "-b", "main"], project_root)
        self._write_normal_hook_trust_state(account_home, state, trust_root=project_root)

        with patch("selc.evaluation._trusted_posix_account_home", return_value=account_home):
            trust_binding = _prepare_codex_hook_trust_binding(state, matrix)

        self.assertEqual(trust_binding["git_identity"]["repository_root"], str(project_root))

    def test_hook_trust_binding_rejects_a_nested_git_repository(self) -> None:
        account_home, _attempt_path, state, matrix = self._prepared_hook_trust_attempt()
        project_root = Path(str(state["project_root"]))
        self._write_normal_hook_trust_state(account_home, state)
        git(["init", "-b", "main"], project_root)

        with (
            patch("selc.evaluation._trusted_posix_account_home", return_value=account_home),
            self.assertRaises(SelcError) as nested,
        ):
            _prepare_codex_hook_trust_binding(state, matrix)
        self.assertEqual(nested.exception.code, "HOOK_TRUST_SOURCE_INVALID")

    def test_hook_trust_binding_rejects_a_worktree_git_file(self) -> None:
        account_home, _attempt_path, state, matrix = self._prepared_hook_trust_attempt()
        project_root = Path(str(state["project_root"]))
        self._write_normal_hook_trust_state(account_home, state)
        (project_root / ".git").write_text(
            "gitdir: /unexpected/worktree\n",
            encoding="utf-8",
        )

        with (
            patch("selc.evaluation._trusted_posix_account_home", return_value=account_home),
            self.assertRaises(SelcError) as worktree,
        ):
            _prepare_codex_hook_trust_binding(state, matrix)
        self.assertEqual(worktree.exception.code, "HOOK_TRUST_SOURCE_INVALID")

    def test_hook_trust_binding_rejects_a_git_environment_identity_change(self) -> None:
        account_home, _attempt_path, state, matrix = self._prepared_hook_trust_attempt()
        self._write_normal_hook_trust_state(account_home, state)
        with (
            patch("selc.evaluation._trusted_posix_account_home", return_value=account_home),
            patch.dict(os.environ, {"GIT_DIR": str(self.root / ".git")}, clear=False),
            self.assertRaises(SelcError) as altered_environment,
        ):
            _prepare_codex_hook_trust_binding(state, matrix)
        self.assertEqual(altered_environment.exception.code, "HOOK_TRUST_SOURCE_INVALID")

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

    def test_hook_trust_binding_rechecks_project_repository_and_config_before_launch(self) -> None:
        account_home, _attempt_path, state, matrix = self._prepared_hook_trust_attempt()
        self._write_normal_hook_trust_state(account_home, state)

        with patch("selc.evaluation._trusted_posix_account_home", return_value=account_home):
            trust_binding = _prepare_codex_hook_trust_binding(state, matrix)
            config_path = account_home / ".codex" / "config.toml"
            config_path.write_text(
                config_path.read_text(encoding="utf-8") + "\n",
                encoding="utf-8",
            )
            self.assertTrue(_codex_hook_trust_changed(trust_binding, state, matrix))

            self._write_normal_hook_trust_state(account_home, state)
            project_root = Path(str(state["project_root"]))
            git_directory = project_root / ".git"
            self.assertFalse(git_directory.exists())
            git_directory.write_text(
                "gitdir: /unexpected/worktree\n",
                encoding="utf-8",
            )
            self.assertTrue(_codex_hook_trust_changed(trust_binding, state, matrix))

    def test_project_trust_cli_configuration_preserves_a_dotted_path_as_one_key(self) -> None:
        configuration = _project_trust_cli_configuration(
            {
                "raw_key": "/private/evaluation.project/with.dots",
                "trust_level": "trusted",
            }
        )

        self.assertEqual(
            tomllib.loads(configuration),
            {"projects": {"/private/evaluation.project/with.dots": {"trust_level": "trusted"}}},
        )

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

    def _workbuddy_diagnostic_attempt(
        self,
    ) -> tuple[dict[str, object], list[dict[str, object]], Path]:
        """Build the observed 5.9.2 JSONL shape without treating it as host acceptance."""

        suite = self.root / "workbuddy-diagnostic-source"
        create_suite("workbuddy-diagnostic-source", "1.0.0", suite, ["text-report"])
        input_path = self.root / "workbuddy-diagnostic-input.json"
        input_path.write_text('{"items": []}', encoding="utf-8")
        specification = self._real_plan_specification(input_path)
        value = json.loads(specification.read_text(encoding="utf-8"))
        value["matrix"][0].update(
            {
                "adapter": "workbuddy-desktop",
                "host": "workbuddy",
                "host_version": "5.9.2",
                "surface": "desktop",
                "model": "workbuddy-model",
            }
        )
        specification.write_text(json.dumps(value), encoding="utf-8")
        prepared = prepare_real_evaluation_plan(
            suite,
            specification,
            self.root / "workbuddy-diagnostic-plan",
        )
        candidate = Path(
            prepare_candidate(suite, self.root / "workbuddy-diagnostic-candidate")["candidate"]
        )
        binding_path = self.root / "workbuddy-diagnostic-binding.json"
        bind_real_evaluation_candidate(Path(prepared["plan"]), candidate, binding_path)
        attempt_path = self.root / "workbuddy-diagnostic-plan" / "attempts" / "first"
        state = prepare_evaluation_attempt(
            Path(prepared["plan"]),
            binding_path,
            "synthetic-case",
            "synthetic-matrix",
            1,
            attempt_path,
        )
        project_root = Path(str(state["project_root"])).resolve()
        session_id = "workbuddy-diagnostic-session"
        request_id = "workbuddy-diagnostic-request"
        request = json.loads(Path(str(state["request_path"])).read_text(encoding="utf-8"))[
            "request"
        ]
        self.assertIsInstance(request, str)
        raw_prompt = str(request)
        native_user_text = (
            '<system-reminder data-role="user-context">\n'
            "fixture context\n"
            "</system-reminder>\n"
            '<system-reminder data-role="additional-data">\n'
            "fixture additional data\n"
            f"</system-reminder>\n<user_query>{raw_prompt}</user_query>"
        )
        records: list[dict[str, object]] = [
            {
                "type": "session-meta",
                "sessionId": session_id,
                "meta": {"codebuddy.ai/hostKind": "unopted"},
                "hostKind": "untrusted-top-level-host-kind",
                "appVersion": "untrusted-top-level-version",
            },
            {
                "id": "workbuddy-diagnostic-user",
                "type": "message",
                "role": "user",
                "sessionId": session_id,
                "cwd": str(project_root),
                "providerData": {
                    "startsNewUserRequest": True,
                    "conversationRequestId": request_id,
                },
                "content": [{"type": "input_text", "text": native_user_text}],
            },
            {
                "id": "workbuddy-diagnostic-reasoning",
                "type": "reasoning",
                "parentId": "workbuddy-diagnostic-user",
                "sessionId": session_id,
                "cwd": str(project_root),
                "providerData": {
                    "conversationRequestId": request_id,
                    "messageId": "workbuddy-diagnostic-model-message-one",
                    "model": "workbuddy-model",
                    "stepSeq": 1,
                },
            },
            {
                "id": "workbuddy-diagnostic-first-call",
                "type": "function_call",
                "name": "Read",
                "callId": "workbuddy-diagnostic-first-call-id",
                "parentId": "workbuddy-diagnostic-reasoning",
                "sessionId": session_id,
                "cwd": str(project_root),
                "providerData": {
                    "conversationRequestId": request_id,
                    "messageId": "workbuddy-diagnostic-model-message-one",
                    "model": "workbuddy-model",
                    "stepSeq": 1,
                },
                "arguments": '{"path":"resources/items.json"}',
            },
            {
                "id": "workbuddy-diagnostic-first-result",
                "type": "function_call_result",
                "callId": "workbuddy-diagnostic-first-call-id",
                "parentId": "workbuddy-diagnostic-first-call",
                "status": "completed",
                "sessionId": session_id,
                "cwd": str(project_root),
                "providerData": {"conversationRequestId": request_id},
                "output": [],
            },
            {
                "id": "workbuddy-diagnostic-second-assistant",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "parentId": "workbuddy-diagnostic-first-result",
                "sessionId": session_id,
                "cwd": str(project_root),
                "timestamp": 1_769_000_000,
                "_meta": {
                    "baggage": "fixture-baggage",
                    "traceparent": "fixture-traceparent",
                },
                "providerData": {
                    "agent": "fixture-agent",
                    "conversationRequestId": request_id,
                    "messageId": "workbuddy-diagnostic-model-message-two",
                    "model": "workbuddy-model",
                    "requestModelId": "workbuddy-request-model",
                    "requestModelName": "WorkBuddy Fixture Model",
                    "stepSeq": 2,
                    "traceId": "fixture-trace",
                },
                "content": [
                    {
                        "type": "output_text",
                        "text": "fixture assistant response",
                        "providerData": {"annotations": []},
                    }
                ],
            },
            {
                "id": "workbuddy-diagnostic-second-call",
                "type": "function_call",
                "name": "Skill",
                "callId": "workbuddy-diagnostic-second-call-id",
                "parentId": "workbuddy-diagnostic-second-assistant",
                "sessionId": session_id,
                "cwd": str(project_root),
                "providerData": {
                    "conversationRequestId": request_id,
                    "messageId": "workbuddy-diagnostic-model-message-two",
                    "model": "workbuddy-model",
                    "stepSeq": 2,
                },
                "arguments": '{"skill":"text-report","args":"{}"}',
            },
            {
                "id": "workbuddy-diagnostic-second-result",
                "type": "function_call_result",
                "callId": "workbuddy-diagnostic-second-call-id",
                "parentId": "workbuddy-diagnostic-second-call",
                "status": "completed",
                "sessionId": session_id,
                "cwd": str(project_root),
                "providerData": {"conversationRequestId": request_id},
                "output": {"status": "ok"},
            },
            {
                "id": "workbuddy-diagnostic-third-call",
                "type": "function_call",
                "name": "Write",
                "callId": "workbuddy-diagnostic-third-call-id",
                "parentId": "workbuddy-diagnostic-second-result",
                "sessionId": session_id,
                "cwd": str(project_root),
                "providerData": {
                    "conversationRequestId": request_id,
                    "messageId": "workbuddy-diagnostic-model-message-three",
                    "model": "workbuddy-model",
                    "stepSeq": 3,
                },
                "arguments": '{"path":"reports/output.json"}',
            },
            {
                "id": "workbuddy-diagnostic-third-result",
                "type": "function_call_result",
                "callId": "workbuddy-diagnostic-third-call-id",
                "parentId": "workbuddy-diagnostic-third-call",
                "status": "completed",
                "sessionId": session_id,
                "cwd": str(project_root),
                "providerData": {"conversationRequestId": request_id},
                "output": {"status": "ok"},
            },
            {
                "id": "workbuddy-diagnostic-other-request-call",
                "type": "function_call",
                "name": "OtherTool",
                "callId": "workbuddy-diagnostic-other-request-call-id",
                "parentId": "workbuddy-diagnostic-other-request-parent",
                "sessionId": session_id,
                "cwd": str(project_root),
                "providerData": {
                    "conversationRequestId": "other-workbuddy-request",
                    "messageId": "other-workbuddy-model-message",
                    "model": "other-workbuddy-model",
                    "stepSeq": 1,
                },
                "arguments": "{}",
            },
            {
                "id": "workbuddy-diagnostic-other-request-result",
                "type": "function_call_result",
                "callId": "workbuddy-diagnostic-other-request-call-id",
                "parentId": "workbuddy-diagnostic-other-request-call",
                "status": "completed",
                "sessionId": session_id,
                "cwd": str(project_root),
                "providerData": {"conversationRequestId": "other-workbuddy-request"},
                "output": {"status": "ok"},
            },
        ]
        compressed_cwd = str(project_root).lstrip("/").replace("/", "-")
        raw_path = (
            self.root.resolve() / ".codebuddy" / "projects" / compressed_cwd / f"{session_id}.jsonl"
        )
        raw_path.parent.mkdir(parents=True)
        return state, records, raw_path

    def _write_workbuddy_diagnostic_evidence(
        self,
        state: dict[str, object],
        records: list[dict[str, object]],
        raw_path: Path,
        *,
        evidence_overrides: dict[str, object] | None = None,
    ) -> None:
        raw = b"".join(
            (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
            for record in records
        )
        raw_path.write_bytes(raw)
        raw_stat = raw_path.stat()
        evidence = {
            "attempt_id": _attempt_id(state),
            "raw_session_path": str(raw_path),
            "raw_session_sha256": hashlib.sha256(raw).hexdigest(),
            "raw_session_cutoff_bytes": len(raw),
            "raw_session_path_hash": hashlib.sha256(
                str(raw_path.resolve()).encode("utf-8")
            ).hexdigest(),
            "raw_session_device": raw_stat.st_dev,
            "raw_session_inode": raw_stat.st_ino,
        }
        if evidence_overrides:
            evidence.update(evidence_overrides)
        evidence_path = Path(str(state["session_root"])) / "native-evidence.json"
        evidence_path.write_text(json.dumps(evidence), encoding="utf-8")

    def test_workbuddy_session_diagnostics_reads_the_observed_serializer_as_unverified(
        self,
    ) -> None:
        state, records, raw_path = self._workbuddy_diagnostic_attempt()
        self._write_workbuddy_diagnostic_evidence(state, records, raw_path)

        with (
            patch("selc.evaluation.Path.home", return_value=self.root),
            patch("selc.managed_runtime.Path.home", return_value=self.root),
        ):
            diagnostics = _workbuddy_session_diagnostics(state, state["matrix"])
            reconstructed = _reconstruct_attempt(state)

        self.assertEqual(diagnostics["schema_version"], 1)
        self.assertEqual(diagnostics["parser"], "workbuddy-session-diagnostics-v1")
        self.assertEqual(diagnostics["model"], "workbuddy-model")
        self.assertEqual(diagnostics["request_model_id"], "workbuddy-request-model")
        self.assertEqual(diagnostics["request_model_name"], "WorkBuddy Fixture Model")
        self.assertEqual(diagnostics["session_id"], "workbuddy-diagnostic-session")
        self.assertEqual(diagnostics["request_id"], "workbuddy-diagnostic-request")
        self.assertEqual(diagnostics["host_kind"], "unopted")
        self.assertEqual(diagnostics["host_kind_source"], "session-meta.meta.codebuddy.ai/hostKind")
        self.assertEqual(diagnostics["app_version"], "5.9.2")
        self.assertEqual(diagnostics["app_version_source"], "matrix.host_version")
        self.assertIsNone(diagnostics["surface"])
        self.assertEqual(diagnostics["surface_source"], "unavailable")
        self.assertTrue(diagnostics["model_attribution_verified"])
        self.assertTrue(diagnostics["parent_chain_verified"])
        self.assertEqual(diagnostics["observed_tool_names"], ["Read", "Skill", "Write"])
        self.assertEqual(
            diagnostics["observed_tools"],
            [
                {"name": "Read", "scope": "current-request", "source": "function_call"},
                {"name": "Skill", "scope": "current-request", "source": "function_call"},
                {"name": "Write", "scope": "current-request", "source": "function_call"},
            ],
        )
        self.assertIsNone(diagnostics["permission_mode"])
        self.assertIsNone(diagnostics["initial_tool_names"])
        self.assertEqual(diagnostics["qualification_support"], "unverified")
        self.assertEqual(diagnostics["reason"], "workbuddy-effective-policy-unavailable")
        self.assertEqual(
            diagnostics["unavailable_reasons"], ["workbuddy-effective-policy-unavailable"]
        )
        self.assertNotIn("raw_tools_policy_hash", diagnostics)
        self.assertEqual(reconstructed["status"], "unknown")
        self.assertEqual(
            reconstructed["reason"], "qualification-native-attribution-unverified-workbuddy"
        )
        self.assertEqual(reconstructed["diagnostics"], diagnostics)
        self.assertIsNone(_read_attempt_native_context(state, state["matrix"]))

    def test_workbuddy_session_diagnostics_rejects_locator_and_unknown_serializer(self) -> None:
        state, records, raw_path = self._workbuddy_diagnostic_attempt()
        cases = {
            "wrong-locator-hash": {"raw_session_path_hash": "0" * 64},
            "wrong-device": {"raw_session_device": -1},
        }

        with (
            patch("selc.evaluation.Path.home", return_value=self.root),
            patch("selc.managed_runtime.Path.home", return_value=self.root),
        ):
            for name, evidence_overrides in cases.items():
                with self.subTest(name=name):
                    self._write_workbuddy_diagnostic_evidence(
                        state,
                        records,
                        raw_path,
                        evidence_overrides=evidence_overrides,
                    )
                    diagnostics = _workbuddy_session_diagnostics(state, state["matrix"])
                    self.assertFalse(diagnostics["model_attribution_verified"])
                    self.assertIn("native-evidence-invalid", diagnostics["unavailable_reasons"])

            self._write_workbuddy_diagnostic_evidence(state, records, raw_path)
            unknown_matrix = {**state["matrix"], "host_version": "5.9.3"}
            diagnostics = _workbuddy_session_diagnostics(state, unknown_matrix)
            self.assertFalse(diagnostics["model_attribution_verified"])
            self.assertIn("workbuddy-serializer-unavailable", diagnostics["unavailable_reasons"])

    def test_workbuddy_session_diagnostics_rejects_request_model_parent_and_duplicates(
        self,
    ) -> None:
        state, records, raw_path = self._workbuddy_diagnostic_attempt()
        mutations: dict[str, list[dict[str, object]]] = {}

        def record_by_id(
            candidate_records: list[dict[str, object]], record_id: str
        ) -> dict[str, object]:
            return next(record for record in candidate_records if record.get("id") == record_id)

        wrong_request = json.loads(json.dumps(records))
        wrong_request[1]["content"][0]["text"] = (
            '<system-reminder data-role="user-context">\nfixture\n</system-reminder>\n'
            '<system-reminder data-role="additional-data">\nfixture\n</system-reminder>\n'
            "<user_query>wrong request</user_query>"
        )
        mutations["wrong-request"] = wrong_request

        wrong_model = json.loads(json.dumps(records))
        record_by_id(wrong_model, "workbuddy-diagnostic-reasoning")["providerData"]["model"] = (
            "other-model"
        )
        mutations["wrong-model"] = wrong_model

        broken_parent = json.loads(json.dumps(records))
        record_by_id(broken_parent, "workbuddy-diagnostic-first-call")["parentId"] = (
            "missing-parent"
        )
        mutations["broken-parent"] = broken_parent

        result_transition_broken_parent = json.loads(json.dumps(records))
        record_by_id(
            result_transition_broken_parent,
            "workbuddy-diagnostic-second-assistant",
        )["parentId"] = "missing-result-parent"
        mutations["result-transition-broken-parent"] = result_transition_broken_parent

        result_transition_step_reversal = json.loads(json.dumps(records))
        record_by_id(
            result_transition_step_reversal,
            "workbuddy-diagnostic-third-call",
        )["providerData"]["stepSeq"] = 2
        mutations["result-transition-step-reversal"] = result_transition_step_reversal

        result_transition_wrong_request = json.loads(json.dumps(records))
        record_by_id(
            result_transition_wrong_request,
            "workbuddy-diagnostic-second-assistant",
        )["providerData"]["conversationRequestId"] = "other-workbuddy-request"
        mutations["result-transition-wrong-request"] = result_transition_wrong_request

        duplicate_record_id = json.loads(json.dumps(records))
        record_by_id(duplicate_record_id, "workbuddy-diagnostic-second-result")["id"] = (
            "workbuddy-diagnostic-second-call"
        )
        mutations["duplicate-record-id"] = duplicate_record_id

        duplicate_call_id = json.loads(json.dumps(records))
        duplicate_call = json.loads(
            json.dumps(record_by_id(duplicate_call_id, "workbuddy-diagnostic-first-call"))
        )
        duplicate_call["id"] = "workbuddy-diagnostic-duplicate-call"
        duplicate_call_id.append(duplicate_call)
        mutations["duplicate-call-id"] = duplicate_call_id

        duplicate_result_id = json.loads(json.dumps(records))
        duplicate_result = json.loads(
            json.dumps(record_by_id(duplicate_result_id, "workbuddy-diagnostic-first-result"))
        )
        duplicate_result["id"] = "workbuddy-diagnostic-duplicate-result"
        duplicate_result_id.append(duplicate_result)
        mutations["duplicate-result-call-id"] = duplicate_result_id

        missing_call_id = json.loads(json.dumps(records))
        record_by_id(missing_call_id, "workbuddy-diagnostic-first-call").pop("id")
        mutations["missing-call-id"] = missing_call_id

        missing_result_id = json.loads(json.dumps(records))
        record_by_id(missing_result_id, "workbuddy-diagnostic-first-result").pop("id")
        mutations["missing-result-id"] = missing_result_id

        pending_result = json.loads(json.dumps(records))
        record_by_id(pending_result, "workbuddy-diagnostic-first-result")["status"] = "pending"
        mutations["pending-result"] = pending_result

        unsupported_result = json.loads(json.dumps(records))
        record_by_id(unsupported_result, "workbuddy-diagnostic-first-result")["output"] = "text"
        mutations["unsupported-result-shape"] = unsupported_result

        invalid_metadata = json.loads(json.dumps(records))
        invalid_metadata[0]["meta"]["codebuddy.ai/hostKind"] = []
        mutations["invalid-metadata-type"] = invalid_metadata

        conflicting_metadata = json.loads(json.dumps(records))
        conflicting_metadata.insert(
            1,
            {
                "type": "session-meta",
                "sessionId": "workbuddy-diagnostic-session",
                "meta": {"codebuddy.ai/hostKind": "other"},
            },
        )
        mutations["conflicting-metadata"] = conflicting_metadata

        with (
            patch("selc.evaluation.Path.home", return_value=self.root),
            patch("selc.managed_runtime.Path.home", return_value=self.root),
        ):
            for name, candidate_records in mutations.items():
                with self.subTest(name=name):
                    self._write_workbuddy_diagnostic_evidence(state, candidate_records, raw_path)
                    diagnostics = _workbuddy_session_diagnostics(state, state["matrix"])
                    self.assertFalse(diagnostics["model_attribution_verified"])
                    self.assertIn(
                        "native-session-records-unverified", diagnostics["unavailable_reasons"]
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
        expected_routing_path = project_root / ".agents" / "skills" / "text-report" / "SKILL.md"
        routing = state["policy_binding"]["routing"]
        routing_command = routing["command"]
        self.assertEqual(
            routing_command,
            shlex.join(("/bin/cat", str(expected_routing_path))),
        )
        schema3_instructions = _render_codex_explicit_routing_instructions(routing_command)
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
                    "content": [
                        {"type": "input_text", "text": "synthetic host instructions"},
                        {
                            "type": "input_text",
                            "text": schema3_instructions,
                        },
                    ],
                    "internal_chat_message_metadata_passthrough": {
                        "turn_id": turn_id,
                        "content_item_kinds": [
                            "host_skills.instructions",
                            "generic.developer_instructions",
                        ],
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
            self.assertEqual(
                collected["evidence"]["preparation_guidance"],
                state["policy_binding"]["preparation_guidance"],
            )
            self.assertNotIn("preparation_guidance", collected["evidence"]["native_policy"])
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
