"""Offline contracts for ordinary and observed suite installation modes."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from selc import records
from selc.errors import SelcError
from selc.install import current_installation, install_suite, recover_installation
from selc.manifest import create_suite, package_suite, seal_suite


class InstallModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def create_suite(self, suite_id: str = "mode-suite") -> Path:
        suite = self.root / suite_id
        create_suite(suite_id, "1.0.0", suite, ["text-report"])
        return suite

    def run_generated_basic_body(
        self,
        body: str,
        *,
        project: Path,
        python_executable: Path,
        replacements: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        code_blocks = re.findall(r"```sh\n(.*?)\n```", body, flags=re.DOTALL)
        self.assertEqual(len(code_blocks), 2)
        self.assertIn(
            'SELC_PYTHON="<absolute path to a Python 3.11+ executable>"',
            code_blocks[0],
        )
        self.assertIn('"$SELC_PYTHON"', code_blocks[1])

        command = "\n".join(code_blocks).replace(
            "<absolute path to a Python 3.11+ executable>", str(python_executable)
        )
        for placeholder, replacement in replacements.items():
            self.assertIn(placeholder, command)
            command = command.replace(placeholder, replacement)

        environment = os.environ.copy()
        environment.pop("SELC_PYTHON", None)
        self.assertNotIn("SELC_PYTHON", environment)
        return subprocess.run(
            ["/bin/sh", "-c", command],
            cwd=project,
            env=environment,
            capture_output=True,
            check=False,
            text=True,
        )

    def test_fresh_basic_workbuddy_install_ignores_malformed_hooks_and_runs(self) -> None:
        suite = self.create_suite()
        package = self.root / "mode-suite.zip"
        package_suite(suite, package)
        project = self.root / "project"
        settings_path = project / ".codebuddy" / "settings.json"
        settings_path.parent.mkdir(parents=True)
        malformed_settings = b"{ not valid JSON"
        settings_path.write_bytes(malformed_settings)

        installation = install_suite(
            source=package,
            project_root=project,
            harness="workbuddy",
            ref=None,
        )

        self.assertEqual(installation["mode"], "basic")
        self.assertEqual(installation["observation"], {"enabled": False})
        self.assertEqual(settings_path.read_bytes(), malformed_settings)
        self.assertEqual(installation["owned_hooks"], {})
        skill_link = project / ".codebuddy" / "skills" / "text-report"
        self.assertEqual(
            skill_link.resolve(),
            (
                project
                / ".selc"
                / "installs"
                / "mode-suite"
                / "current"
                / "skills"
                / "text-report-basic"
            ).resolve(),
        )

        input_path = self.root / "input.txt"
        output_path = self.root / "report.md"
        input_path.write_text("first item\nsecond item\n", encoding="utf-8")
        completed = subprocess.run(
            [
                sys.executable,
                str(
                    project
                    / ".selc"
                    / "installs"
                    / "mode-suite"
                    / "current"
                    / "bin"
                    / "selc-suite.py"
                ),
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
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("first item", output_path.read_text(encoding="utf-8"))

        database = records.open_database(project)
        try:
            for table in ("tasks", "calls", "managed_executions"):
                self.assertEqual(database.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
        finally:
            database.close()

    def test_generated_basic_command_resolves_the_physical_workbuddy_skill_directory(self) -> None:
        suite = self.create_suite()
        project = self.root / "project"
        install_suite(
            source=suite,
            project_root=project,
            harness="workbuddy",
            ref=None,
        )
        discovery_skill = project / ".codebuddy" / "skills" / "text-report" / "SKILL.md"
        body = discovery_skill.read_text(encoding="utf-8")
        self.assertIn('cd -P "$(dirname "$SELC_SKILL_FILE")"', body)

        (project / "input.txt").write_text("relative input\n", encoding="utf-8")
        python_executable = self.root / "Python 3.11 executable"
        python_executable.symlink_to(sys.executable)
        completed = self.run_generated_basic_body(
            body,
            project=project,
            python_executable=python_executable,
            replacements={
                "<absolute path to this loaded SKILL.md>": str(discovery_skill),
                "<input.txt>": "input.txt",
                "<report.md>": "output.md",
            },
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("relative input", (project / "output.md").read_text(encoding="utf-8"))

    def test_generated_python_script_body_uses_its_declared_python(self) -> None:
        suite = self.root / "python-suite"
        create_suite(
            "python-suite",
            "1.0.0",
            suite,
            ["script-report"],
            managed={
                "protocol": 1,
                "actions": {
                    "script-report": {
                        "skill": "script-report",
                        "kind": "python-script-v1",
                        "script": "resources/run.py",
                        "input": {
                            "fields": {"request": {"type": "path"}},
                            "required": ["request"],
                        },
                    }
                },
            },
        )
        declared_script = suite / "resources" / "run.py"
        declared_script.write_text(
            "from pathlib import Path\n"
            "import sys\n\n"
            "output = Path(sys.argv[sys.argv.index('--output') + 1])\n"
            "output.write_text('script branch\\n', encoding='utf-8')\n",
            encoding="utf-8",
        )
        seal_suite(suite)

        project = self.root / "project"
        install_suite(
            source=suite,
            project_root=project,
            harness="workbuddy",
            ref=None,
        )
        discovery_skill = project / ".codebuddy" / "skills" / "script-report" / "SKILL.md"
        body = discovery_skill.read_text(encoding="utf-8")
        python_executable = self.root / "Python 3.11 executable"
        python_executable.symlink_to(sys.executable)
        (project / "request.json").write_text("{}\n", encoding="utf-8")

        completed = self.run_generated_basic_body(
            body,
            project=project,
            python_executable=python_executable,
            replacements={
                "<absolute path to this loaded SKILL.md>": str(discovery_skill),
                "<request.json>": "request.json",
                "<artifact>": "artifact.json",
            },
        )

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(
            (project / "artifact.json").read_text(encoding="utf-8"),
            "script branch\n",
        )

    def test_legacy_omitted_mode_stays_observed_and_requires_a_receipt(self) -> None:
        suite = self.create_suite()
        project = self.root / "project"
        observed = install_suite(
            source=suite,
            project_root=project,
            harness="codex",
            ref=None,
            mode="observed",
        )
        registry_path = project / ".selc" / "installations.json"
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        registry["installations"]["mode-suite"].pop("mode")
        registry_path.write_text(json.dumps(registry), encoding="utf-8")

        preserved = install_suite(
            source=suite,
            project_root=project,
            harness="codex",
            ref=None,
        )

        self.assertEqual(preserved["mode"], "observed")
        self.assertTrue(preserved["owned_hooks"])
        self.assertTrue(
            (project / ".agents" / "skills" / "text-report")
            .resolve()
            .is_relative_to((project / ".selc" / "installs" / "mode-suite" / "entries").resolve())
        )
        from selc.managed_runtime import activate

        with self.assertRaises(SelcError) as captured:
            activate(
                project_root=project,
                suite_id="mode-suite",
                skill="text-report",
                receipt="not-a-issued-receipt",
            )
        self.assertEqual(captured.exception.code, "RECEIPT_UNAVAILABLE")
        self.assertEqual(observed["mode"], "observed")

    def test_basic_rejects_missing_ordinary_entry_before_installation(self) -> None:
        suite = self.create_suite()
        manifest_path = suite / "suite.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["skills"][0].pop("basic_path")
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        seal_suite(suite)
        project = self.root / "project"

        with self.assertRaises(SelcError) as captured:
            install_suite(
                source=suite,
                project_root=project,
                harness="workbuddy",
                ref=None,
            )

        self.assertEqual(captured.exception.code, "BASIC_ENTRY_UNAVAILABLE")
        self.assertFalse((project / ".selc" / "installs" / "mode-suite" / "current").exists())
        self.assertFalse((project / ".codebuddy" / "skills" / "text-report").exists())

    def test_basic_path_must_stay_on_the_portable_skills_surface(self) -> None:
        suite = self.create_suite()
        manifest_path = suite / "suite.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        ordinary_body = suite / "ordinary" / "text-report"
        ordinary_body.mkdir(parents=True)
        (ordinary_body / "SKILL.md").write_text(
            (suite / "skills" / "text-report-basic" / "SKILL.md").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        manifest["skills"][0]["basic_path"] = "ordinary/text-report"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with self.assertRaises(SelcError) as captured:
            seal_suite(suite)

        self.assertEqual(captured.exception.code, "INVALID_MANIFEST")

    def test_mode_switch_updates_every_harness_and_preserves_unrelated_hook_commands(self) -> None:
        suite = self.create_suite()
        project = self.root / "project"
        codex_hooks = project / ".codex" / "hooks.json"
        codex_hooks.parent.mkdir(parents=True)
        codex_hooks.write_text(
            json.dumps({"hooks": {"Stop": [{"hooks": [{"command": "echo keep"}]}]}}),
            encoding="utf-8",
        )
        install_suite(
            source=suite,
            project_root=project,
            harness="codex",
            ref=None,
        )
        install_suite(
            source=suite,
            project_root=project,
            harness="workbuddy",
            ref=None,
        )

        observed = install_suite(
            source=suite,
            project_root=project,
            harness="codex",
            ref=None,
            mode="observed",
        )
        self.assertEqual(observed["mode"], "observed")
        self.assertTrue((project / ".codebuddy" / "settings.json").is_file())
        for skill_link in (
            project / ".agents" / "skills" / "text-report",
            project / ".codebuddy" / "skills" / "text-report",
        ):
            self.assertTrue(
                skill_link.resolve().is_relative_to(
                    (project / ".selc" / "installs" / "mode-suite" / "entries").resolve()
                )
            )

        basic = install_suite(
            source=suite,
            project_root=project,
            harness="workbuddy",
            ref=None,
            mode="basic",
        )
        self.assertEqual(basic["mode"], "basic")
        self.assertEqual(basic["owned_hooks"], {})
        retained = json.loads(codex_hooks.read_text(encoding="utf-8"))
        self.assertEqual(retained["hooks"]["Stop"], [{"hooks": [{"command": "echo keep"}]}])
        for skill_link in (
            project / ".agents" / "skills" / "text-report",
            project / ".codebuddy" / "skills" / "text-report",
        ):
            self.assertEqual(
                skill_link.resolve(),
                (
                    project
                    / ".selc"
                    / "installs"
                    / "mode-suite"
                    / "current"
                    / "skills"
                    / "text-report-basic"
                ).resolve(),
            )

    def test_observed_switch_preflights_every_harness_before_writing_hooks(self) -> None:
        suite = self.create_suite()
        project = self.root / "project"
        install_suite(source=suite, project_root=project, harness="codex", ref=None)
        install_suite(source=suite, project_root=project, harness="workbuddy", ref=None)
        workbuddy_settings = project / ".codebuddy" / "settings.json"
        malformed_settings = b'{"hooks": []}'
        workbuddy_settings.write_bytes(malformed_settings)

        with self.assertRaises(SelcError) as captured:
            install_suite(
                source=suite,
                project_root=project,
                harness="codex",
                ref=None,
                mode="observed",
            )

        self.assertEqual(captured.exception.code, "HOOK_CONFIG_CONFLICT")
        recovered = recover_installation(project, "mode-suite")
        self.assertEqual(recovered, {"recovered": False, "state": "no_journal"})
        self.assertEqual(current_installation(project, "mode-suite")["mode"], "basic")
        self.assertEqual(workbuddy_settings.read_bytes(), malformed_settings)
        codex_hooks = project / ".codex" / "hooks.json"
        if codex_hooks.exists():
            self.assertNotIn("managed hook", codex_hooks.read_text(encoding="utf-8"))

    def test_recovery_removes_planned_hooks_after_a_partial_observed_write(self) -> None:
        suite = self.create_suite()
        project = self.root / "project"
        install_suite(source=suite, project_root=project, harness="codex", ref=None)
        install_suite(source=suite, project_root=project, harness="workbuddy", ref=None)
        workbuddy_settings = project / ".codebuddy" / "settings.json"

        import selc.install as install_module

        original_write_json = install_module.write_json

        def fail_after_workbuddy_write(path: Path, value: object) -> None:
            original_write_json(path, value)
            if path == workbuddy_settings and "managed hook" in json.dumps(value):
                raise SelcError(
                    "HOOK_CONFIG_CONFLICT",
                    "injected workbuddy hook write interruption",
                    "recover the interrupted installation",
                )

        with patch.object(install_module, "write_json", side_effect=fail_after_workbuddy_write):
            with self.assertRaisesRegex(SelcError, "workbuddy hook write interruption"):
                install_suite(
                    source=suite,
                    project_root=project,
                    harness="codex",
                    ref=None,
                    mode="observed",
                )

            journal = project / ".selc" / "installs" / "mode-suite" / "journal.json"
            self.assertTrue(journal.is_file())
            self.assertTrue(
                json.loads(journal.read_text(encoding="utf-8"))["new_installation"]["owned_hooks"]
            )
            recovered = recover_installation(project, "mode-suite")
        self.assertEqual(recovered["state"], "rolled_back")
        self.assertEqual(current_installation(project, "mode-suite")["mode"], "basic")
        for hook_file in (project / ".codex" / "hooks.json", workbuddy_settings):
            self.assertNotIn("managed hook", hook_file.read_text(encoding="utf-8"))

    def test_recovery_replaces_interrupted_hooks_from_an_earlier_interpreter(self) -> None:
        suite = self.create_suite()
        project = self.root / "project"
        install_suite(source=suite, project_root=project, harness="codex", ref=None)
        install_suite(source=suite, project_root=project, harness="workbuddy", ref=None)

        third_party_command = "echo preserve-third-party-hook"
        codex_hooks = project / ".codex" / "hooks.json"
        workbuddy_settings = project / ".codebuddy" / "settings.json"
        for hook_file in (codex_hooks, workbuddy_settings):
            hook_file.parent.mkdir(parents=True, exist_ok=True)
            hook_file.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "Stop": [
                                {"hooks": [{"type": "command", "command": third_party_command}]}
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )

        import selc.install as install_module

        interpreter_a = self.root / "interpreter A"
        interpreter_b = self.root / "interpreter B"
        with (
            patch.object(install_module.sys, "executable", str(interpreter_a)),
            patch.object(
                install_module,
                "_write_registry",
                side_effect=SelcError(
                    "WRITE_FAILED",
                    "injected registry write interruption",
                    "recover the interrupted installation",
                ),
            ),
            self.assertRaisesRegex(SelcError, "registry write interruption"),
        ):
            install_suite(
                source=suite,
                project_root=project,
                harness="codex",
                ref=None,
                mode="observed",
            )

        journal = project / ".selc" / "installs" / "mode-suite" / "journal.json"
        journal_record = json.loads(journal.read_text(encoding="utf-8"))
        self.assertIn(
            str(interpreter_a),
            json.dumps(journal_record["new_installation"]["owned_hooks"]),
        )

        with patch.object(install_module.sys, "executable", str(interpreter_b)):
            recovered = recover_installation(project, "mode-suite")
        self.assertEqual(recovered["state"], "completed")
        recovered_installation = current_installation(project, "mode-suite")
        self.assertEqual(recovered_installation["mode"], "observed")
        self.assertIn(str(interpreter_a), json.dumps(recovered_installation["owned_hooks"]))
        self.assertNotIn(str(interpreter_b), json.dumps(recovered_installation["owned_hooks"]))
        for hook_file in (codex_hooks, workbuddy_settings):
            contents = hook_file.read_text(encoding="utf-8")
            self.assertIn(str(interpreter_a), contents)
            self.assertNotIn(str(interpreter_b), contents)
            self.assertIn(third_party_command, contents)

        with patch.object(install_module.sys, "executable", str(interpreter_b)):
            basic = install_suite(
                source=suite,
                project_root=project,
                harness="codex",
                ref=None,
                mode="basic",
            )
        self.assertEqual(basic["mode"], "basic")
        for hook_file in (codex_hooks, workbuddy_settings):
            contents = hook_file.read_text(encoding="utf-8")
            self.assertNotIn(str(interpreter_a), contents)
            self.assertNotIn(str(interpreter_b), contents)
            self.assertIn(third_party_command, contents)

    def test_recovery_rollback_reuses_old_frozen_hook_commands(self) -> None:
        suite = self.create_suite()
        project = self.root / "project"

        import selc.install as install_module

        interpreter_a = self.root / "interpreter A"
        interpreter_b = self.root / "interpreter B"
        with patch.object(install_module.sys, "executable", str(interpreter_a)):
            install_suite(
                source=suite,
                project_root=project,
                harness="codex",
                ref=None,
                mode="observed",
            )

        resource = suite / "resources" / "text-report.json"
        resource_data = json.loads(resource.read_text(encoding="utf-8"))
        resource_data["heading"] = "Updated text report"
        resource.write_text(json.dumps(resource_data), encoding="utf-8")
        seal_suite(suite)

        codex_hooks = project / ".codex" / "hooks.json"
        original_write_json = install_module.write_json

        def fail_after_new_hook_write(path: Path, value: object) -> None:
            original_write_json(path, value)
            if path == codex_hooks and str(interpreter_b) in json.dumps(value):
                raise SelcError(
                    "HOOK_CONFIG_CONFLICT",
                    "injected new hook write interruption",
                    "recover the interrupted installation",
                )

        with (
            patch.object(install_module.sys, "executable", str(interpreter_b)),
            patch.object(install_module, "write_json", side_effect=fail_after_new_hook_write),
            self.assertRaisesRegex(SelcError, "new hook write interruption"),
        ):
            install_suite(
                source=suite,
                project_root=project,
                harness="codex",
                ref=None,
                mode="observed",
            )

        def fail_new_recovery_hook_write(path: Path, value: object) -> None:
            if path == codex_hooks and str(interpreter_b) in json.dumps(value):
                raise SelcError(
                    "HOOK_CONFIG_CONFLICT",
                    "injected completion hook write interruption",
                    "roll back the interrupted installation",
                )
            original_write_json(path, value)

        with (
            patch.object(install_module.sys, "executable", str(interpreter_b)),
            patch.object(
                install_module,
                "write_json",
                side_effect=fail_new_recovery_hook_write,
            ),
        ):
            recovered = recover_installation(project, "mode-suite")

        self.assertEqual(recovered["state"], "rolled_back")
        installation = current_installation(project, "mode-suite")
        self.assertEqual(installation["mode"], "observed")
        self.assertIn(str(interpreter_a), json.dumps(installation["owned_hooks"]))
        self.assertNotIn(str(interpreter_b), json.dumps(installation["owned_hooks"]))
        contents = codex_hooks.read_text(encoding="utf-8")
        self.assertIn(str(interpreter_a), contents)
        self.assertNotIn(str(interpreter_b), contents)

    def test_recovery_requires_complete_journal_hook_ownership(self) -> None:
        suite = self.create_suite()
        project = self.root / "project"
        install_suite(source=suite, project_root=project, harness="codex", ref=None)

        import selc.install as install_module

        with (
            patch.object(
                install_module,
                "_write_registry",
                side_effect=SelcError(
                    "WRITE_FAILED",
                    "injected registry write interruption",
                    "recover the interrupted installation",
                ),
            ),
            self.assertRaisesRegex(SelcError, "registry write interruption"),
        ):
            install_suite(
                source=suite,
                project_root=project,
                harness="codex",
                ref=None,
                mode="observed",
            )

        journal = project / ".selc" / "installs" / "mode-suite" / "journal.json"
        record = json.loads(journal.read_text(encoding="utf-8"))
        journal_owned_hooks = record["new_installation"]["owned_hooks"]
        record["new_installation"]["owned_hooks"] = {}
        journal.write_text(json.dumps(record), encoding="utf-8")

        recovered = recover_installation(project, "mode-suite")

        self.assertEqual(recovered["state"], "needs_recovery")
        self.assertTrue(journal.is_file())

        record = json.loads(journal.read_text(encoding="utf-8"))
        record["new_installation"]["owned_hooks"] = journal_owned_hooks
        record["new_installation"]["harnesses"] = ["codex", {"invalid": "harness"}]
        journal.write_text(json.dumps(record), encoding="utf-8")

        malformed_harnesses = recover_installation(project, "mode-suite")

        self.assertEqual(malformed_harnesses["state"], "needs_recovery")
        self.assertTrue(journal.is_file())

    def test_recovery_removes_a_new_harness_after_a_partial_observed_switch(self) -> None:
        suite = self.create_suite()
        project = self.root / "project"
        install_suite(source=suite, project_root=project, harness="codex", ref=None)
        workbuddy_settings = project / ".codebuddy" / "settings.json"

        import selc.install as install_module

        original_write_json = install_module.write_json

        def fail_after_workbuddy_write(path: Path, value: object) -> None:
            original_write_json(path, value)
            if path == workbuddy_settings and "managed hook" in json.dumps(value):
                raise SelcError(
                    "HOOK_CONFIG_CONFLICT",
                    "injected workbuddy hook write interruption",
                    "recover the interrupted installation",
                )

        with patch.object(install_module, "write_json", side_effect=fail_after_workbuddy_write):
            with self.assertRaisesRegex(SelcError, "workbuddy hook write interruption"):
                install_suite(
                    source=suite,
                    project_root=project,
                    harness="workbuddy",
                    ref=None,
                    mode="observed",
                )

            recovered = recover_installation(project, "mode-suite")

        self.assertEqual(recovered["state"], "rolled_back")
        installation = current_installation(project, "mode-suite")
        self.assertEqual(installation["mode"], "basic")
        self.assertEqual(installation["harnesses"], ["codex"])
        workbuddy_skill = project / ".codebuddy" / "skills" / "text-report"
        self.assertFalse(workbuddy_skill.exists())
        self.assertFalse(workbuddy_skill.is_symlink())
        for hook_file in (project / ".codex" / "hooks.json", workbuddy_settings):
            self.assertNotIn("managed hook", hook_file.read_text(encoding="utf-8"))

    def test_switch_to_basic_preserves_an_observed_hook_file_without_hooks_key(self) -> None:
        suite = self.create_suite()
        project = self.root / "project"
        install_suite(
            source=suite,
            project_root=project,
            harness="codex",
            ref=None,
            mode="observed",
        )
        hooks_path = project / ".codex" / "hooks.json"
        hooks_path.write_text("{}", encoding="utf-8")

        installation = install_suite(
            source=suite,
            project_root=project,
            harness="codex",
            ref=None,
            mode="basic",
        )

        self.assertEqual(installation["mode"], "basic")
        self.assertEqual(installation["owned_hooks"], {})
        self.assertEqual(hooks_path.read_text(encoding="utf-8"), "{}")

    def test_created_basic_path_avoids_a_declared_skill_name_collision(self) -> None:
        suite = self.root / "collision-suite"
        create_suite(
            "collision-suite",
            "1.0.0",
            suite,
            ["text-report", "text-report-basic"],
        )
        manifest = json.loads((suite / "suite.json").read_text(encoding="utf-8"))
        text_report = next(skill for skill in manifest["skills"] if skill["name"] == "text-report")
        self.assertEqual(text_report["basic_path"], "skills/text-report-basic-2")
        self.assertTrue((suite / text_report["basic_path"] / "SKILL.md").is_file())
        self.assertTrue((suite / "skills" / "text-report-basic" / "SKILL.md").is_file())

        project = self.root / "project"
        install_suite(
            source=suite,
            project_root=project,
            harness="workbuddy",
            ref=None,
        )
        self.assertEqual(
            (project / ".codebuddy" / "skills" / "text-report").resolve(),
            (
                project
                / ".selc"
                / "installs"
                / "collision-suite"
                / "current"
                / text_report["basic_path"]
            ).resolve(),
        )

    def test_cli_install_and_install_local_accept_modes(self) -> None:
        suite = self.create_suite()
        package = self.root / "mode-suite.zip"
        package_suite(suite, package)
        project = self.root / "project"

        installed = subprocess.run(
            [
                sys.executable,
                str(REPOSITORY_ROOT / "scripts" / "selc.py"),
                "install",
                "--source",
                str(package),
                "--project-root",
                str(project),
                "--harness",
                "codex",
                "--mode",
                "observed",
            ],
            capture_output=True,
            check=False,
            text=True,
        )
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        self.assertEqual(json.loads(installed.stdout)["installation"]["mode"], "observed")

        local_project = self.root / "local-project"
        local_install = subprocess.run(
            [
                sys.executable,
                str(suite / "install.py"),
                "--project-root",
                str(local_project),
                "--harness",
                "workbuddy",
                "--mode",
                "basic",
            ],
            capture_output=True,
            check=False,
            text=True,
        )
        self.assertEqual(local_install.returncode, 0, local_install.stdout + local_install.stderr)
        self.assertEqual(json.loads(local_install.stdout)["installation"]["mode"], "basic")

    def test_mode_switch_respects_active_leases_and_recovery_preserves_basic(self) -> None:
        suite = self.create_suite()
        project = self.root / "project"
        install_suite(
            source=suite,
            project_root=project,
            harness="codex",
            ref=None,
        )
        database = records.open_database(project)
        records.start_task(
            database,
            task_id="task",
            suite_id="mode-suite",
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
            executor="fixture",
            evidence="fixture",
            expected_version="1.0.0",
            expected_hash=None,
            trigger_kind="explicit",
        )
        database.close()
        with self.assertRaises(SelcError) as captured:
            install_suite(
                source=suite,
                project_root=project,
                harness="codex",
                ref=None,
                mode="observed",
            )
        self.assertEqual(captured.exception.code, "ACTIVE_LEASE")

        database = records.open_database(project)
        records.complete_call(database, "call", "unknown")
        records.close_task(database, "task")
        database.close()
        install_suite(
            source=suite,
            project_root=project,
            harness="codex",
            ref=None,
            mode="observed",
        )

        import selc.install as install_module

        original_write_registry = install_module._write_registry
        with (
            patch.object(install_module, "_write_registry", side_effect=OSError("interrupted")),
            self.assertRaises(OSError),
        ):
            install_suite(
                source=suite,
                project_root=project,
                harness="codex",
                ref=None,
                mode="basic",
            )

        self.assertTrue((project / ".selc" / "installs" / "mode-suite" / "journal.json").is_file())
        self.assertIs(install_module._write_registry, original_write_registry)
        recovered = recover_installation(project, "mode-suite")
        self.assertTrue(recovered["recovered"])
        self.assertEqual(current_installation(project, "mode-suite")["mode"], "basic")


if __name__ == "__main__":
    unittest.main()
