"""Contract tests for the independent checker engine."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

import selc.checkers as checkers_module
from selc.checkers import (
    CheckRequest,
    CheckerIdentity,
    CheckerRegistry,
    JsonItemSummaryProvider,
    ProviderOutcome,
    RuleResult,
    ScriptCheckerProvider,
    SubjectIdentity,
    builtin_registry,
    check_artifact,
)


class CheckerEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(prefix="selc-checker-engine-")
        self.root = Path(self.temporary_directory.name)
        self.registry = builtin_registry()
        self.builtin_identity = self.registry.identity("json-item-summary", "1")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def request(
        self,
        *,
        checker: CheckerIdentity | None = None,
        input_value: object | None = None,
        artifact_value: object | None = None,
        required_rules: tuple[str, ...] = ("count", "sum"),
        configuration: dict[str, object] | None = None,
        protocol: int = 1,
        require_network_isolation: bool = False,
        run_id: str = "run-1",
    ) -> CheckRequest:
        if input_value is None:
            input_value = {
                "items": [
                    {"id": "a", "value": 2},
                    {"id": "b", "value": 3},
                    {"id": "a", "value": 5},
                ]
            }
        if artifact_value is None:
            artifact_value = {"count": 3, "sum": 10}
        return CheckRequest(
            protocol=protocol,
            run_id=run_id,
            subject=SubjectIdentity(task_id="task-1", call_id="call-1", delivery_id="delivery-1"),
            checker=checker or self.builtin_identity,
            rule_version="summary-v1",
            required_rule_ids=required_rules,
            configuration=configuration or {},
            input_bytes=json.dumps(input_value).encode("utf-8"),
            artifact_bytes=json.dumps(artifact_value).encode("utf-8"),
            resource_identity="summary-resource-v1",
            resource_bytes=b"summary-resource-bytes",
            composite_identity="suite-composite-v1",
            composite_bytes=b"suite-composite-bytes",
            require_network_isolation=require_network_isolation,
        )

    def test_builtin_checker_calculates_count_and_sum_from_input(self) -> None:
        result = check_artifact(self.request(), self.registry)

        self.assertEqual(result.status, "pass")
        self.assertEqual(
            [(entry.rule_id, entry.status) for entry in result.rule_results],
            [("count", "pass"), ("sum", "pass")],
        )
        self.assertEqual(
            result.input_digest,
            hashlib.sha256(self.request().input_bytes).hexdigest(),
        )
        self.assertEqual(
            result.artifact_digest,
            hashlib.sha256(self.request().artifact_bytes).hexdigest(),
        )

    def test_duplicate_ids_can_contribute_once_to_the_summary(self) -> None:
        result = check_artifact(
            self.request(
                artifact_value={"count": 2, "sum": 5},
                configuration={"duplicate_ids_count_once": True},
            ),
            self.registry,
        )

        self.assertEqual(result.status, "pass")

    def test_builtin_emits_only_the_requested_builtin_rules(self) -> None:
        count_only = check_artifact(
            self.request(required_rules=("count",), artifact_value={"count": 3, "sum": 999}),
            self.registry,
        )
        sum_only = check_artifact(
            self.request(required_rules=("sum",), artifact_value={"count": 999, "sum": 10}),
            self.registry,
        )

        self.assertEqual(count_only.status, "pass")
        self.assertEqual([(item.rule_id, item.status) for item in count_only.rule_results], [("count", "pass")])
        self.assertEqual(sum_only.status, "pass")
        self.assertEqual([(item.rule_id, item.status) for item in sum_only.rule_results], [("sum", "pass")])

    def test_json_sum_uses_exact_decimal_semantics_for_large_and_cancelling_values(self) -> None:
        rounded_false_pass = replace(
            self.request(),
            input_bytes=(
                b'{"items":[{"id":"a","value":100000000000000000000000000000},'
                b'{"id":"b","value":1}]}'
            ),
            artifact_bytes=b'{"count":2,"sum":100000000000000000000000000000}',
        )
        exact_large_integer = replace(
            rounded_false_pass,
            artifact_bytes=b'{"count":2,"sum":100000000000000000000000000001}',
        )
        cancellation_with_exponent = replace(
            self.request(),
            input_bytes=(
                b'{"items":[{"id":"a","value":1e30},'
                b'{"id":"b","value":-999999999999999999999999999999},'
                b'{"id":"c","value":0.25}]}'
            ),
            artifact_bytes=b'{"count":3,"sum":1.25}',
        )

        rounded = check_artifact(rounded_false_pass, self.registry)
        large = check_artifact(exact_large_integer, self.registry)
        cancellation = check_artifact(cancellation_with_exponent, self.registry)

        self.assertEqual(rounded.status, "fail")
        self.assertEqual(rounded.rule_results[1].status, "fail")
        self.assertEqual(large.status, "pass")
        self.assertEqual(cancellation.status, "pass")

    def test_exponent_span_outside_the_exact_sum_bound_is_unknown(self) -> None:
        request = replace(
            self.request(),
            input_bytes=b'{"items":[{"id":"a","value":1e9000},{"id":"b","value":1}]}',
            artifact_bytes=b'{"count":2,"sum":1e9000}',
        )

        result = check_artifact(request, self.registry)

        self.assertEqual(result.status, "unknown")
        self.assertEqual(result.rule_results[0].status, "pass")
        self.assertEqual(result.rule_results[1].reason, "input-sum-precision-unsupported")

    def test_known_business_failure_is_fail(self) -> None:
        result = check_artifact(
            self.request(artifact_value={"count": 2, "sum": 10}),
            self.registry,
        )

        self.assertEqual(result.status, "fail")
        self.assertEqual(result.rule_results[0].status, "fail")
        self.assertEqual(result.rule_results[1].status, "pass")

    def test_malformed_business_body_is_unknown_not_pass(self) -> None:
        result = check_artifact(
            self.request(input_value={"items": [{"id": "a", "value": True}]}),
            self.registry,
        )

        self.assertEqual(result.status, "unknown")
        self.assertTrue(all(entry.status == "unknown" for entry in result.rule_results))

    def test_missing_required_rule_is_unknown(self) -> None:
        result = check_artifact(
            self.request(required_rules=("count", "sum", "minimum-total")),
            self.registry,
        )

        self.assertEqual(result.status, "unknown")
        self.assertEqual(result.rule_results[-1].reason, "unsupported-rule")

    def test_known_failure_wins_over_an_unknown_required_rule(self) -> None:
        result = check_artifact(
            self.request(artifact_value={"count": 2}, required_rules=("count", "sum")),
            self.registry,
        )

        self.assertEqual(result.status, "fail")
        self.assertEqual(
            [(entry.rule_id, entry.status) for entry in result.rule_results],
            [("count", "fail"), ("sum", "unknown")],
        )

    def test_unsupported_protocol_checker_and_digest_are_unknown(self) -> None:
        protocol_result = check_artifact(self.request(protocol=2), self.registry)
        absent_result = check_artifact(
            self.request(
                checker=CheckerIdentity("absent-checker", "1", "a" * 64),
            ),
            self.registry,
        )
        wrong_digest = CheckerIdentity(
            self.builtin_identity.name,
            self.builtin_identity.version,
            "b" * 64,
        )
        digest_result = check_artifact(self.request(checker=wrong_digest), self.registry)

        self.assertEqual(protocol_result.status, "unknown")
        self.assertEqual(protocol_result.reason, "unsupported-protocol")
        self.assertEqual(absent_result.status, "unknown")
        self.assertEqual(absent_result.reason, "checker-unavailable")
        self.assertEqual(digest_result.status, "unknown")
        self.assertEqual(digest_result.reason, "checker-digest-mismatch")

    def test_invalid_engine_request_is_explicitly_rejected(self) -> None:
        invalid = self.request(required_rules=("count", "count"))

        result = check_artifact(invalid, self.registry)

        self.assertEqual(result.status, "unknown")
        self.assertEqual(result.reason, "invalid-request:duplicate-required-rule")
        self.assertEqual(result.rule_results[0].status, "unknown")

    def test_invalid_checker_configuration_is_unknown(self) -> None:
        result = check_artifact(
            self.request(configuration={"duplicate_ids_count_once": "yes"}),
            self.registry,
        )

        self.assertEqual(result.status, "unknown")
        self.assertEqual(result.reason, "duplicate-id-configuration-invalid")

    def test_custom_trusted_provider_extends_registry_without_core_dispatch_changes(self) -> None:
        implementation_digest = "c" * 64
        identity = CheckerIdentity("custom-policy", "3", implementation_digest)
        registry = CheckerRegistry()
        registry.register(identity, CustomPolicyProvider(implementation_digest))

        result = check_artifact(
            self.request(checker=identity, required_rules=("policy-approved",)),
            registry,
        )

        self.assertEqual(result.status, "pass")
        self.assertEqual(result.rule_results[0].reason, "custom-policy-approved")

    def test_script_timeout_and_crash_are_unknown(self) -> None:
        timeout_provider, timeout_identity = self.script_provider(
            "import time\ntime.sleep(10)\n",
            timeout_seconds=0.1,
        )
        crash_provider, crash_identity = self.script_provider("raise SystemExit(7)\n")
        timeout_registry = CheckerRegistry()
        timeout_registry.register(timeout_identity, timeout_provider)
        crash_registry = CheckerRegistry()
        crash_registry.register(crash_identity, crash_provider)

        timeout = check_artifact(
            self.request(checker=timeout_identity, required_rules=("external",)),
            timeout_registry,
        )
        crash = check_artifact(
            self.request(checker=crash_identity, required_rules=("external",)),
            crash_registry,
        )

        self.assertEqual(timeout.status, "unknown")
        self.assertEqual(timeout.reason, "checker-timeout")
        self.assertEqual(crash.status, "unknown")
        self.assertEqual(crash.reason, "checker-crashed:7")

    def test_digest_pinned_script_can_return_a_bound_rule_result(self) -> None:
        provider, identity = self.script_provider(
            """
import json
import sys

request = json.load(sys.stdin)
json.dump(
    {
        "protocol": 1,
        "checker": request["checker"],
        "results": [{"rule_id": "external", "status": "pass", "reason": "external-ok"}],
    },
    sys.stdout,
)
""",
        )
        registry = CheckerRegistry()
        registry.register(identity, provider)

        result = check_artifact(
            self.request(checker=identity, required_rules=("external",)),
            registry,
        )

        self.assertEqual(result.status, "pass")
        self.assertEqual(result.rule_results[0].reason, "external-ok")

    def test_script_output_limit_is_unknown(self) -> None:
        provider, identity = self.script_provider("print('x' * 2048)\n", max_stdout_bytes=128)
        registry = CheckerRegistry()
        registry.register(identity, provider)

        result = check_artifact(
            self.request(checker=identity, required_rules=("external",)),
            registry,
        )

        self.assertEqual(result.status, "unknown")
        self.assertEqual(result.reason, "checker-output-limit")

    def test_duplicate_and_malformed_script_results_are_unknown(self) -> None:
        duplicate_provider, duplicate_identity = self.script_provider(
            """
import json
import sys

request = json.load(sys.stdin)
json.dump(
    {
        "protocol": 1,
        "checker": request["checker"],
        "results": [
            {"rule_id": "external", "status": "pass", "reason": "first"},
            {"rule_id": "external", "status": "pass", "reason": "second"},
        ],
    },
    sys.stdout,
)
""",
        )
        malformed_provider, malformed_identity = self.script_provider(
            """
import json
import sys

request = json.load(sys.stdin)
json.dump(
    {
        "protocol": 1,
        "checker": request["checker"],
        "results": [{"rule_id": "external", "status": "pass"}],
    },
    sys.stdout,
)
""",
        )
        duplicate_registry = CheckerRegistry()
        duplicate_registry.register(duplicate_identity, duplicate_provider)
        malformed_registry = CheckerRegistry()
        malformed_registry.register(malformed_identity, malformed_provider)

        duplicate = check_artifact(
            self.request(checker=duplicate_identity, required_rules=("external",)),
            duplicate_registry,
        )
        malformed = check_artifact(
            self.request(checker=malformed_identity, required_rules=("external",)),
            malformed_registry,
        )

        self.assertEqual(duplicate.status, "unknown")
        self.assertEqual(duplicate.rule_results[0].reason, "duplicate-rule-result")
        self.assertEqual(malformed.status, "unknown")
        self.assertEqual(malformed.rule_results[0].reason, "malformed-rule-result")

    def test_non_string_script_status_is_malformed_not_an_engine_crash(self) -> None:
        for invalid_status in ("[]", "{}"):
            with self.subTest(invalid_status=invalid_status):
                provider, identity = self.script_provider(
                    "\n".join(
                        (
                            "import json",
                            "import sys",
                            "request = json.load(sys.stdin)",
                            "json.dump({'protocol': 1, 'checker': request['checker'], 'results': [",
                            "    {'rule_id': 'external', 'status': " + invalid_status + ", 'reason': 'bad'}",
                            "]}, sys.stdout)",
                        )
                    )
                )
                registry = CheckerRegistry()
                registry.register(identity, provider)

                result = check_artifact(
                    self.request(checker=identity, required_rules=("external",)), registry
                )

                self.assertEqual(result.status, "unknown")
                self.assertEqual(result.rule_results[0].reason, "malformed-rule-result")

    def test_changed_registered_script_digest_prevents_side_effects(self) -> None:
        sentinel = self.root / "digest-mismatch-ran"
        provider, identity = self.script_provider(
            f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('original')\n",
        )
        provider.script_path.write_text(
            f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('changed')\n",
            encoding="utf-8",
        )
        registry = CheckerRegistry()
        registry.register(identity, provider)

        result = check_artifact(
            self.request(checker=identity, required_rules=("external",)),
            registry,
        )

        self.assertEqual(result.status, "unknown")
        self.assertEqual(result.reason, "checker-digest-mismatch")
        self.assertFalse(sentinel.exists())

    def test_script_snapshot_tampering_is_detected_without_writing_source(self) -> None:
        source = self.root / "business-input.json"
        source.write_text('{"items":[{"id":"a","value":1}]}', encoding="utf-8")
        original_source = source.read_bytes()
        provider, identity = self.script_provider(
            """
import json
import os
import sys

request = json.load(sys.stdin)
input_path = request["input_snapshot"]["path"]
os.chmod(input_path, 0o600)
with open(input_path, "wb") as handle:
    handle.write(b"tampered")
json.dump(
    {
        "protocol": 1,
        "checker": request["checker"],
        "results": [{"rule_id": "external", "status": "pass", "reason": "claimed-pass"}],
    },
    sys.stdout,
)
""",
        )
        registry = CheckerRegistry()
        registry.register(identity, provider)
        request = self.request(
            checker=identity,
            input_value=json.loads(original_source),
            required_rules=("external",),
        )

        result = check_artifact(request, registry)

        self.assertEqual(result.status, "unknown")
        self.assertEqual(result.reason, "snapshot-tampered")
        self.assertEqual(source.read_bytes(), original_source)

    def test_network_isolation_demand_refuses_to_start_script(self) -> None:
        sentinel = self.root / "script-ran"
        provider, identity = self.script_provider(
            f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('ran')\n",
        )
        registry = CheckerRegistry()
        registry.register(identity, provider)

        result = check_artifact(
            self.request(
                checker=identity,
                required_rules=("external",),
                require_network_isolation=True,
            ),
            registry,
        )

        self.assertEqual(result.status, "unknown")
        self.assertEqual(result.reason, "network-isolation-unavailable")
        self.assertFalse(sentinel.exists())

    def test_builtin_checker_does_not_claim_os_network_isolation(self) -> None:
        with patch.object(
            JsonItemSummaryProvider,
            "evaluate",
            side_effect=AssertionError("builtin checker must not run"),
        ):
            result = check_artifact(
                self.request(require_network_isolation=True),
                self.registry,
            )

        self.assertEqual(result.status, "unknown")
        self.assertEqual(result.reason, "network-isolation-unavailable")

    def test_timeout_covers_large_non_reader_stdin_payload_and_cleans_threads(self) -> None:
        provider, identity = self.script_provider(
            "import time\ntime.sleep(0.6)\n",
            timeout_seconds=0.05,
        )
        registry = CheckerRegistry()
        registry.register(identity, provider)
        request = self.request(
            checker=identity,
            required_rules=("external",),
            run_id="r" * 200_000,
        )

        started = time.monotonic()
        result = check_artifact(request, registry)
        elapsed = time.monotonic() - started

        self.assertEqual(result.status, "unknown")
        self.assertEqual(result.reason, "checker-timeout")
        self.assertLess(elapsed, 0.4)
        self.assertFalse(
            any(thread.name.startswith("selc-checker-") for thread in threading.enumerate())
        )

    def test_oversized_metadata_is_refused_before_script_execution(self) -> None:
        sentinel = self.root / "oversized-metadata-ran"
        provider, identity = self.script_provider(
            f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('ran')\n",
        )
        registry = CheckerRegistry()
        registry.register(identity, provider)

        result = check_artifact(
            self.request(
                checker=identity,
                required_rules=("external",),
                run_id="r" * 300_000,
            ),
            registry,
        )

        self.assertEqual(result.status, "unknown")
        self.assertEqual(result.reason, "invalid-request:run-id")
        self.assertFalse(sentinel.exists())

    def test_builtin_digest_pins_the_module_implementation(self) -> None:
        self.assertEqual(
            self.builtin_identity.digest,
            hashlib.sha256(Path(checkers_module.__file__).read_bytes()).hexdigest(),
        )

    def test_script_executes_validated_private_snapshot_after_source_replacement(self) -> None:
        sentinel = self.root / "snapshot-provider-ran"
        script = self.root / "source-provider.py"
        original_source = f"""
import json
import sys
from pathlib import Path

request = json.load(sys.stdin)
Path({str(sentinel)!r}).write_text("original", encoding="utf-8")
json.dump(
    {{
        "protocol": 1,
        "checker": request["checker"],
        "results": [{{"rule_id": "external", "status": "pass", "reason": "original-ran"}}],
    }},
    sys.stdout,
)
"""
        replacement_source = (
            "from pathlib import Path\n"
            f"Path({str(sentinel)!r}).write_text('replacement')\n"
        )
        script.write_text(original_source, encoding="utf-8")
        digest = hashlib.sha256(script.read_bytes()).hexdigest()
        identity = CheckerIdentity("snapshot-provider", "1", digest)
        provider = ReplacingScriptProvider(script, digest, replacement_source)
        registry = CheckerRegistry()
        registry.register(identity, provider)

        result = check_artifact(
            self.request(checker=identity, required_rules=("external",)),
            registry,
        )

        self.assertEqual(result.status, "pass")
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "original")

    def test_timeout_terminates_the_owned_process_group(self) -> None:
        child_pid_file = self.root / "child.pid"
        provider, identity = self.script_provider(
            """
import json
import subprocess
import sys
import time
from pathlib import Path

request = json.load(sys.stdin)
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
Path(request["configuration"]["child_pid_file"]).write_text(str(child.pid), encoding="utf-8")
time.sleep(30)
""",
            timeout_seconds=0.1,
        )
        registry = CheckerRegistry()
        registry.register(identity, provider)

        result = check_artifact(
            self.request(
                checker=identity,
                required_rules=("external",),
                configuration={"child_pid_file": str(child_pid_file)},
            ),
            registry,
        )

        self.assertEqual(result.status, "unknown")
        self.assertEqual(result.reason, "checker-timeout")
        child_pid = int(child_pid_file.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 2
        while self.process_exists(child_pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(self.process_exists(child_pid))

    def test_escaped_pipe_holder_returns_by_deadline_without_reader_threads(self) -> None:
        child_pid_file = self.root / "escaped-helper.pid"
        provider, identity = self.script_provider(
            """
import json
import subprocess
import sys
import time
from pathlib import Path

request = json.load(sys.stdin)
helper = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(4)"],
    stdout=sys.stdout,
    stderr=sys.stderr,
    start_new_session=True,
)
Path(request["configuration"]["child_pid_file"]).write_text(str(helper.pid), encoding="utf-8")
""",
            timeout_seconds=0.1,
        )
        registry = CheckerRegistry()
        registry.register(identity, provider)

        started = time.monotonic()
        result = check_artifact(
            self.request(
                checker=identity,
                required_rules=("external",),
                configuration={"child_pid_file": str(child_pid_file)},
            ),
            registry,
        )
        elapsed = time.monotonic() - started

        self.assertEqual(result.status, "unknown")
        self.assertEqual(result.reason, "process-cleanup-incomplete")
        self.assertLess(elapsed, 0.6)
        self.assertFalse(
            any(thread.name.startswith("selc-checker-") for thread in threading.enumerate())
        )
        helper_pid = int(child_pid_file.read_text(encoding="utf-8"))
        try:
            os.kill(helper_pid, 0)
        except ProcessLookupError:
            pass
        else:
            os.kill(helper_pid, 9)

    def script_provider(
        self,
        source: str,
        *,
        timeout_seconds: float = 1,
        max_stdout_bytes: int = 4096,
    ) -> tuple[ScriptCheckerProvider, CheckerIdentity]:
        script = self.root / f"provider-{len(list(self.root.glob('provider-*.py')))}.py"
        script.write_text(source, encoding="utf-8")
        digest = hashlib.sha256(script.read_bytes()).hexdigest()
        identity = CheckerIdentity(f"script-{script.stem}", "1", digest)
        return (
            ScriptCheckerProvider(
                script,
                implementation_digest=digest,
                timeout_seconds=timeout_seconds,
                max_stdout_bytes=max_stdout_bytes,
            ),
            identity,
        )

    @staticmethod
    def process_exists(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True


class CustomPolicyProvider:
    supported_protocols = frozenset({1})
    supported_rules = frozenset({"policy-approved"})
    capabilities = frozenset()

    def __init__(self, implementation_digest: str) -> None:
        self.implementation_digest = implementation_digest

    def evaluate(self, request: CheckRequest) -> ProviderOutcome:
        return ProviderOutcome(
            rule_results=(
                RuleResult("policy-approved", "pass", "custom-policy-approved"),
            )
        )


class ReplacingScriptProvider(ScriptCheckerProvider):
    def __init__(self, script_path: Path, digest: str, replacement_source: str) -> None:
        super().__init__(script_path, implementation_digest=digest)
        self.replacement_source = replacement_source

    def _run_script(self, request: CheckRequest, script_bytes: bytes) -> ProviderOutcome:
        self.script_path.write_text(self.replacement_source, encoding="utf-8")
        return super()._run_script(request, script_bytes)


if __name__ == "__main__":
    unittest.main()
