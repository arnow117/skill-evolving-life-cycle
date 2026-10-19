"""Authoring-seal behavior for portable suites."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from selc.errors import SelcError
from selc.install import install_suite
from selc.manifest import create_suite, load_manifest, package_suite, seal_suite


class SealContractsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def run_cli(self, *arguments: str, expected: int = 0) -> dict[str, object]:
        completed = subprocess.run(
            [sys.executable, str(REPOSITORY_ROOT / "scripts" / "selc.py"), *arguments],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            check=False,
            text=True,
        )
        self.assertEqual(completed.returncode, expected, completed.stdout + completed.stderr)
        return json.loads(completed.stdout)

    def run_portable(self, suite: Path, *arguments: str, expected: int = 0) -> dict[str, object]:
        completed = subprocess.run(
            [sys.executable, str(suite / "bin" / "selc-suite.py"), *arguments],
            capture_output=True,
            check=False,
            text=True,
        )
        self.assertEqual(completed.returncode, expected, completed.stdout + completed.stderr)
        return json.loads(completed.stdout)

    def test_seal_refreshes_author_edits_without_cleaning_an_installed_copy(self) -> None:
        suite = self.root / "authoring-suite"
        create_suite("authoring-suite", "1.0.0", suite, ["text-report"])
        skill_path = suite / "skills" / "text-report" / "SKILL.md"
        skill_path.write_text(
            skill_path.read_text(encoding="utf-8") + "\nCreator-authored usage detail.\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(SelcError, "content hash"):
            load_manifest(suite)
        with self.assertRaisesRegex(SelcError, "content hash"):
            package_suite(suite, self.root / "before-seal.zip")

        sealed = self.run_cli("seal", "--suite", str(suite))
        self.assertEqual(sealed["suite"]["suite_id"], "authoring-suite")
        self.assertEqual(load_manifest(suite)["suite_id"], "authoring-suite")
        package_suite(suite, self.root / "after-seal.zip")

        project = self.root / "project"
        install_suite(source=suite, project_root=project, harness="codex", ref=None)
        current = project / ".selc" / "installs" / "authoring-suite" / "current"
        current_skill = current / "skills" / "text-report" / "SKILL.md"
        current_skill.write_text(
            current_skill.read_text(encoding="utf-8") + "\nLocal installed edit.\n",
            encoding="utf-8",
        )
        self.run_portable(current, "seal", "--suite", str(current))
        with self.assertRaisesRegex(SelcError, "local edits") as captured:
            install_suite(source=suite, project_root=project, harness="codex", ref=None)
        self.assertEqual(captured.exception.code, "LOCAL_EDIT_CONFLICT")

    def test_seal_rejects_framework_protocol_or_runtime_tampering_without_rewriting_it(
        self,
    ) -> None:
        suite = self.root / "sealed-framework"
        create_suite("sealed-framework", "1.0.0", suite, ["text-report"])
        descriptor_path = suite / "framework.json"
        original_descriptor = descriptor_path.read_bytes()
        original_manifest = (suite / "suite.json").read_bytes()

        descriptor = json.loads(original_descriptor)
        descriptor["runtime_protocol"] = 999
        descriptor_path.write_text(json.dumps(descriptor), encoding="utf-8")
        with self.assertRaisesRegex(SelcError, "runtime protocol") as captured:
            seal_suite(suite)
        self.assertEqual(captured.exception.code, "UNSUPPORTED_RUNTIME_PROTOCOL")
        self.assertEqual(descriptor_path.read_text(encoding="utf-8"), json.dumps(descriptor))
        self.assertEqual((suite / "suite.json").read_bytes(), original_manifest)

        descriptor_path.write_bytes(original_descriptor)
        runtime_file = suite / "runtime" / "selc" / "manifest.py"
        runtime_file.write_text(
            runtime_file.read_text(encoding="utf-8") + "\n# tampered\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(SelcError, "does not match") as captured:
            seal_suite(suite)
        self.assertEqual(captured.exception.code, "FRAMEWORK_HASH_MISMATCH")
        self.assertEqual(descriptor_path.read_bytes(), original_descriptor)
        self.assertEqual((suite / "suite.json").read_bytes(), original_manifest)


if __name__ == "__main__":
    unittest.main()
