"""Independent business-checker engine.

``check_artifact(request, registry)`` is an internal protocol-1 API.  The
trusted coordinator builds :class:`CheckRequest`: it supplies stable run and
subject identifiers, fixed input and artifact bytes, fixed resource and
composite identities plus their bytes, requested checker identity, and the
required rule set.  The engine calculates all byte digests itself.

The returned :class:`CheckResult` repeats the run, subject, checker, rule,
resource, and composite bindings.  It records SHA-256 digests for input,
artifact, resource, and composite bytes, one three-state result per required
rule, and an aggregate ``pass`` / ``fail`` / ``unknown`` status.  A valid
required-rule failure wins over unknown rules. Invalid engine requests and
malformed business bodies are ``unknown`` because neither is an established
business-rule failure.

Only a maintainer-owned :class:`CheckerRegistry` selects providers.  Requests
cannot name an executable, command line, environment, or path.  The supplied
``ScriptCheckerProvider`` is for explicitly registered local Python files whose
digest is pinned in the registry; it uses bounded snapshots and process-group
cleanup, but deliberately does not claim to be an operating-system sandbox.
"""

from __future__ import annotations

import hashlib
import json
import os
import selectors
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

PROTOCOL_VERSION = 1
MAX_BOUND_BYTES = 1_000_000
MAX_PROVIDER_CONFIGURATION_BYTES = 16_384
MAX_RUN_ID_BYTES = 262_144
MAX_SUBJECT_ID_BYTES = 4_096
MAX_CHECKER_IDENTIFIER_BYTES = 256
MAX_RULE_VERSION_BYTES = 256
MAX_RULE_ID_BYTES = 256
MAX_REQUIRED_RULES = 64
MAX_RESOURCE_IDENTIFIER_BYTES = 4_096
MAX_SCRIPT_PAYLOAD_BYTES = 300_000
MAX_EXACT_SUM_PRECISION = 8_192
MAX_REASON_LENGTH = 512
PROCESS_GRACE_SECONDS = 0.1
PROCESS_CLEANUP_SECONDS = 0.25

STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_UNKNOWN = "unknown"
VALID_STATUSES = frozenset({STATUS_PASS, STATUS_FAIL, STATUS_UNKNOWN})


@dataclass(frozen=True)
class CheckerIdentity:
    """Maintainer-registered checker name, version, and implementation digest."""

    name: str
    version: str
    digest: str

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "version": self.version, "digest": self.digest}


@dataclass(frozen=True)
class SubjectIdentity:
    """Trusted task/call/delivery identifiers for the checked business artifact."""

    task_id: str
    call_id: str
    delivery_id: str

    def as_dict(self) -> dict[str, str]:
        return {
            "task_id": self.task_id,
            "call_id": self.call_id,
            "delivery_id": self.delivery_id,
        }


@dataclass(frozen=True)
class CheckRequest:
    """Trusted protocol-1 input for one fixed business-artifact check.

    ``resource_bytes`` and ``composite_bytes`` are the coordinator's fixed
    resource and assembled-suite representations.  They let the engine bind
    their computed digests alongside the human-meaningful identity strings.
    ``configuration`` contains checker-specific JSON data.  Setting
    ``require_network_isolation`` asks for OS-enforced network isolation; a
    provider that cannot provide it is never started.
    """

    protocol: int
    run_id: str
    subject: SubjectIdentity
    checker: CheckerIdentity
    rule_version: str
    required_rule_ids: tuple[str, ...]
    configuration: Mapping[str, object]
    input_bytes: bytes
    artifact_bytes: bytes
    resource_identity: str
    resource_bytes: bytes
    composite_identity: str
    composite_bytes: bytes
    require_network_isolation: bool = False


@dataclass(frozen=True)
class RuleResult:
    """A concise explanation for one required business rule."""

    rule_id: str
    status: str
    reason: str


@dataclass(frozen=True)
class CheckResult:
    """Protocol-1 result handed to records/manifest integration by T009."""

    protocol: int
    run_id: str
    subject: SubjectIdentity
    checker: CheckerIdentity
    rule_version: str
    resource_identity: str
    composite_identity: str
    input_digest: str
    artifact_digest: str
    resource_digest: str
    composite_digest: str
    rule_results: tuple[RuleResult, ...]
    status: str
    reason: str


@dataclass(frozen=True)
class ProviderOutcome:
    """Provider output before the engine validates required rule coverage."""

    rule_results: tuple[RuleResult, ...] = ()
    reason: str | None = None
    raw_results: object | None = None


class CheckerProvider(Protocol):
    """Trusted registry extension point; implementations do not come from requests."""

    implementation_digest: str
    supported_protocols: frozenset[int]
    supported_rules: frozenset[str]
    capabilities: frozenset[str]

    def evaluate(self, request: CheckRequest) -> ProviderOutcome:
        """Evaluate a fixed request without changing its binding metadata."""


@dataclass(frozen=True)
class _RegisteredChecker:
    identity: CheckerIdentity
    provider: CheckerProvider


class CheckerRegistry:
    """Maintainer-owned registry of fixed checker implementations."""

    def __init__(self) -> None:
        self._by_identity: dict[CheckerIdentity, _RegisteredChecker] = {}
        self._by_name_version: dict[tuple[str, str], _RegisteredChecker] = {}

    def register(self, identity: CheckerIdentity, provider: CheckerProvider) -> None:
        """Register a provider only when its fixed implementation digest matches."""

        if not _is_digest(identity.digest):
            raise ValueError("checker identity digest must be a SHA-256 hex string")
        if provider.implementation_digest != identity.digest:
            raise ValueError("provider digest must match its registered checker identity")
        name_and_version = (identity.name, identity.version)
        if identity in self._by_identity or name_and_version in self._by_name_version:
            raise ValueError("checker name and version are already registered")

        registered = _RegisteredChecker(identity, provider)
        self._by_identity[identity] = registered
        self._by_name_version[(identity.name, identity.version)] = registered

    def identity(self, name: str, version: str) -> CheckerIdentity:
        """Return a registered identity for trusted callers and tests."""

        return self._by_name_version[(name, version)].identity

    def resolve(self, identity: CheckerIdentity) -> tuple[_RegisteredChecker | None, str | None]:
        """Resolve an exact identity without accepting a digest substitution."""

        exact = self._by_identity.get(identity)
        if exact is not None:
            return exact, None
        if (identity.name, identity.version) in self._by_name_version:
            return None, "checker-digest-mismatch"
        return None, "checker-unavailable"


def check_artifact(request: CheckRequest, registry: CheckerRegistry) -> CheckResult:
    """Run a trusted checker and return bound three-state rule results.

    The engine owns aggregation and all byte hashing.  A checker can never
    promote an arbitrary provider-declared ``pass`` to an accepted result.
    """

    digests = _request_digests(request)
    validation_error = _validate_request(request)
    if validation_error is not None:
        return _rejected_request(request, digests, validation_error)

    if request.protocol != PROTOCOL_VERSION:
        return _unknown_result(request, digests, "unsupported-protocol")

    registered, registry_error = registry.resolve(request.checker)
    if registered is None:
        return _unknown_result(request, digests, registry_error or "checker-unavailable")

    provider = registered.provider
    if request.protocol not in provider.supported_protocols:
        return _unknown_result(request, digests, "provider-protocol-unsupported")
    if request.require_network_isolation and "network-isolation" not in provider.capabilities:
        return _unknown_result(request, digests, "network-isolation-unavailable")

    try:
        outcome = provider.evaluate(request)
    except Exception as error:
        return _unknown_result(request, digests, f"checker-error:{type(error).__name__}")

    unsupported_rules = frozenset(
        rule_id
        for rule_id in request.required_rule_ids
        if not _provider_supports_rule(provider, rule_id)
    )
    rule_results = _normalise_rule_results(request, outcome, unsupported_rules)
    return _aggregate_result(request, digests, rule_results)


class JsonItemSummaryProvider:
    """Built-in checker for JSON item counts and decimal sums.

    Input is ``{"items": [{"id": <non-empty string>, "value": <finite JSON number>}]}``.
    Output is ``{"count": <JSON integer>, "sum": <finite JSON number>}``.
    With ``duplicate_ids_count_once: true``, only an ID's first item contributes
    to both count and sum; the default counts every item. JSON decimal literals
    are parsed as :class:`~decimal.Decimal`, so comparisons do not depend on
    binary floating-point rounding. Inputs whose exact aligned sum needs more
    than ``MAX_EXACT_SUM_PRECISION`` digits produce an ``unknown`` sum result.
    """

    supported_protocols = frozenset({PROTOCOL_VERSION})
    supported_rules = frozenset({"count", "sum"})
    capabilities = frozenset()

    def __init__(self) -> None:
        self.implementation_digest = _module_digest()

    def evaluate(self, request: CheckRequest) -> ProviderOutcome:
        configuration_error = _duplicate_id_configuration_error(request.configuration)
        if configuration_error is not None:
            return ProviderOutcome(reason=configuration_error)

        input_value, input_error = _load_json_object(request.input_bytes, "input")
        if input_error is not None:
            return ProviderOutcome(reason=input_error)
        artifact_value, artifact_error = _load_json_object(request.artifact_bytes, "artifact")
        if artifact_error is not None:
            return ProviderOutcome(reason=artifact_error)

        assert input_value is not None
        assert artifact_value is not None
        items, items_error = _read_summary_items(input_value)
        if items_error is not None:
            return ProviderOutcome(reason=items_error)

        assert items is not None
        deduplicate_ids = bool(request.configuration.get("duplicate_ids_count_once", False))
        rule_results: list[RuleResult] = []
        if "count" in request.required_rule_ids:
            rule_results.append(_check_count_rule(items, artifact_value, deduplicate_ids))
        if "sum" in request.required_rule_ids:
            rule_results.append(_check_sum_rule(items, artifact_value, deduplicate_ids))
        return ProviderOutcome(rule_results=tuple(rule_results))


class ScriptCheckerProvider:
    """Digest-pinned trusted Python script provider with bounded local execution.

    The script reads one JSON object from stdin and writes a JSON object to
    stdout.  It receives read-only snapshots of input and artifact bytes, plus
    their engine-computed digests.  It must echo the exact protocol and checker
    identity and return ``results`` entries with ``rule_id``, ``status``, and
    ``reason``.  The script is trusted local code, not an OS sandboxed plugin.
    """

    supported_protocols = frozenset({PROTOCOL_VERSION})
    capabilities = frozenset()

    def __init__(
        self,
        script_path: Path,
        *,
        implementation_digest: str,
        timeout_seconds: float = 1.0,
        max_snapshot_bytes: int = MAX_BOUND_BYTES,
        max_stdout_bytes: int = 32_768,
        max_stderr_bytes: int = 8_192,
        supported_rules: frozenset[str] | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_snapshot_bytes <= 0 or max_stdout_bytes <= 0 or max_stderr_bytes <= 0:
            raise ValueError("provider byte limits must be positive")
        if not _is_digest(implementation_digest):
            raise ValueError("implementation_digest must be a SHA-256 hex string")

        self.script_path = script_path
        self.implementation_digest = implementation_digest
        self.timeout_seconds = timeout_seconds
        self.max_snapshot_bytes = max_snapshot_bytes
        self.max_stdout_bytes = max_stdout_bytes
        self.max_stderr_bytes = max_stderr_bytes
        self.supported_rules = supported_rules or frozenset({"*"})

    def evaluate(self, request: CheckRequest) -> ProviderOutcome:
        script_bytes, script_error = self._load_validated_script()
        if script_error is not None:
            return ProviderOutcome(reason=script_error)
        if len(request.input_bytes) > self.max_snapshot_bytes:
            return ProviderOutcome(reason="input-snapshot-limit")
        if len(request.artifact_bytes) > self.max_snapshot_bytes:
            return ProviderOutcome(reason="artifact-snapshot-limit")

        assert script_bytes is not None
        return self._run_script(request, script_bytes)

    def _load_validated_script(self) -> tuple[bytes | None, str | None]:
        if self.script_path.is_symlink() or not self.script_path.is_file():
            return None, "checker-script-unavailable"
        try:
            script_bytes = self.script_path.read_bytes()
        except OSError:
            return None, "checker-script-unavailable"
        digest = _sha256_bytes(script_bytes)
        if digest != self.implementation_digest:
            return None, "checker-digest-mismatch"
        return script_bytes, None

    def _run_script(self, request: CheckRequest, script_bytes: bytes) -> ProviderOutcome:
        if os.name != "posix":
            return ProviderOutcome(reason="process-group-cleanup-unsupported")

        with tempfile.TemporaryDirectory(prefix="selc-checker-engine-") as temporary_name:
            temporary_directory = Path(temporary_name)
            script_snapshot = temporary_directory / "provider.py"
            input_snapshot = temporary_directory / "input.snapshot"
            artifact_snapshot = temporary_directory / "artifact.snapshot"
            _write_private_script_snapshot(script_snapshot, script_bytes)
            _write_read_only_snapshot(input_snapshot, request.input_bytes)
            _write_read_only_snapshot(artifact_snapshot, request.artifact_bytes)

            payload = _script_payload(request, input_snapshot, artifact_snapshot)
            if len(payload) > MAX_SCRIPT_PAYLOAD_BYTES:
                return ProviderOutcome(reason="checker-request-limit")
            completed = _run_owned_script(
                script_path=script_snapshot,
                cwd=temporary_directory,
                payload=payload,
                timeout_seconds=self.timeout_seconds,
                max_stdout_bytes=self.max_stdout_bytes,
                max_stderr_bytes=self.max_stderr_bytes,
            )
            if completed.reason is not None:
                return ProviderOutcome(reason=completed.reason)

            if not _snapshot_is_unchanged(input_snapshot, request.input_bytes):
                return ProviderOutcome(reason="snapshot-tampered")
            if not _snapshot_is_unchanged(artifact_snapshot, request.artifact_bytes):
                return ProviderOutcome(reason="snapshot-tampered")

            return _parse_script_response(completed.stdout, request)


@dataclass(frozen=True)
class _ScriptCompleted:
    stdout: bytes
    reason: str | None = None


class _BoundedCapture:
    """Retain bounded nonblocking pipe output without a reader thread."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._data = bytearray()
        self.exceeded = False

    def add(self, chunk: bytes) -> None:
        available = self.limit + 1 - len(self._data)
        if available > 0:
            self._data.extend(chunk[:available])
        if len(chunk) > available or len(self._data) > self.limit:
            self.exceeded = True

    def bytes(self) -> bytes:
        return bytes(self._data[: self.limit])


def _run_owned_script(
    *,
    script_path: Path,
    cwd: Path,
    payload: bytes,
    timeout_seconds: float,
    max_stdout_bytes: int,
    max_stderr_bytes: int,
) -> _ScriptCompleted:
    environment = {"PATH": os.defpath, "PYTHONIOENCODING": "utf-8"}
    started_at = time.monotonic()
    try:
        process = subprocess.Popen(
            [sys.executable, str(script_path)],
            cwd=cwd,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            bufsize=0,
        )
    except OSError:
        return _ScriptCompleted(b"", "checker-start-failed")

    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    deadline = started_at + timeout_seconds
    completed, reason = _communicate_until_deadline(
        process=process,
        payload=payload,
        deadline=deadline,
        max_stdout_bytes=max_stdout_bytes,
        max_stderr_bytes=max_stderr_bytes,
    )
    if reason is not None and not _terminate_owned_process_group(process):
        reason = "process-cleanup-incomplete"
    if reason is None and not _ensure_process_group_is_empty(process.pid, process):
        reason = "process-cleanup-incomplete"
    _close_process_stdin(process.stdin)
    _close_process_stream(process.stdout)
    _close_process_stream(process.stderr)
    return _ScriptCompleted(completed, reason)


def _communicate_until_deadline(
    *,
    process: subprocess.Popen[bytes],
    payload: bytes,
    deadline: float,
    max_stdout_bytes: int,
    max_stderr_bytes: int,
) -> tuple[bytes, str | None]:
    """Exchange bounded bytes without waiting for inherited pipe descriptors.

    A trusted script can still start a child that escapes its process group and
    inherits stdout or stderr.  That child is outside the process group we own,
    so the only safe response is a bounded ``process-cleanup-incomplete``
    result.  In particular, this loop never calls a buffered ``close`` while a
    reader could be blocked behind that unrelated child.
    """

    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    stdout_capture = _BoundedCapture(max_stdout_bytes)
    stderr_capture = _BoundedCapture(max_stderr_bytes)
    streams = {
        process.stdin.fileno(): (process.stdin, selectors.EVENT_WRITE, None),
        process.stdout.fileno(): (process.stdout, selectors.EVENT_READ, stdout_capture),
        process.stderr.fileno(): (process.stderr, selectors.EVENT_READ, stderr_capture),
    }
    for descriptor in streams:
        os.set_blocking(descriptor, False)
    payload_offset = 0
    open_descriptors = set(streams)
    selector = selectors.DefaultSelector()
    for descriptor, (stream, events, _) in streams.items():
        selector.register(descriptor, events, stream)

    reason: str | None = None
    try:
        while open_descriptors:
            now = time.monotonic()
            if now >= deadline:
                reason = "checker-timeout" if process.poll() is None else "process-cleanup-incomplete"
                break
            timeout = min(0.01, deadline - now)
            for selected, mask in selector.select(timeout):
                descriptor = selected.fd
                stream, _, capture = streams[descriptor]
                if capture is None and mask & selectors.EVENT_WRITE:
                    try:
                        written = os.write(descriptor, payload[payload_offset:])
                    except (BlockingIOError, BrokenPipeError, OSError):
                        written = 0
                        _unregister_descriptor(selector, descriptor, open_descriptors)
                    else:
                        payload_offset += written
                        if payload_offset == len(payload):
                            _unregister_descriptor(selector, descriptor, open_descriptors)
                            _close_process_stdin(stream)
                elif capture is not None and mask & selectors.EVENT_READ:
                    try:
                        chunk = os.read(descriptor, 8_192)
                    except BlockingIOError:
                        continue
                    except OSError:
                        chunk = b""
                    if not chunk:
                        _unregister_descriptor(selector, descriptor, open_descriptors)
                    else:
                        capture.add(chunk)
                        if capture.exceeded:
                            reason = "checker-output-limit"
                            break
            if reason is not None:
                break
            if process.poll() is not None and not {
                process.stdout.fileno(), process.stderr.fileno()
            }.intersection(open_descriptors):
                break
    finally:
        selector.close()

    if reason is None:
        if process.poll() is None:
            reason = "checker-timeout"
        elif process.returncode != 0:
            if process.returncode < 0:
                reason = f"checker-signaled:{-process.returncode}"
            else:
                reason = f"checker-crashed:{process.returncode}"
    return stdout_capture.bytes(), reason


def _unregister_descriptor(
    selector: selectors.BaseSelector, descriptor: int, open_descriptors: set[int]
) -> None:
    if descriptor not in open_descriptors:
        return
    try:
        selector.unregister(descriptor)
    except KeyError:
        pass
    open_descriptors.discard(descriptor)


def _close_process_stdin(stream: Any) -> None:
    try:
        stream.close()
    except (BrokenPipeError, OSError, ValueError):
        return


def _close_process_stream(stream: Any) -> None:
    try:
        stream.close()
    except OSError:
        return


def _terminate_owned_process_group(process: subprocess.Popen[bytes]) -> bool:
    """Stop only this invocation's session/process group and verify its absence."""

    process_group_id = process.pid
    _signal_process_group(process_group_id, signal.SIGTERM)
    try:
        process.wait(timeout=PROCESS_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    _signal_process_group(process_group_id, signal.SIGKILL)
    try:
        process.wait(timeout=PROCESS_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        return False
    return _wait_for_process_group_exit(process_group_id, PROCESS_CLEANUP_SECONDS)


def _ensure_process_group_is_empty(
    process_group_id: int, process: subprocess.Popen[bytes]
) -> bool:
    if not _process_group_exists(process_group_id):
        return True
    return _terminate_owned_process_group(process)


def _signal_process_group(process_group_id: int, signal_number: int) -> None:
    try:
        os.killpg(process_group_id, signal_number)
    except ProcessLookupError:
        return


def _wait_for_process_group_exit(process_group_id: int, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while _process_group_exists(process_group_id):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)
    return True


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _script_payload(request: CheckRequest, input_snapshot: Path, artifact_snapshot: Path) -> bytes:
    payload = {
        "protocol": request.protocol,
        "run_id": request.run_id,
        "subject": request.subject.as_dict(),
        "checker": request.checker.as_dict(),
        "rule_version": request.rule_version,
        "required_rule_ids": list(request.required_rule_ids),
        "configuration": dict(request.configuration),
        "input_snapshot": {
            "path": str(input_snapshot),
            "sha256": _sha256_bytes(request.input_bytes),
        },
        "artifact_snapshot": {
            "path": str(artifact_snapshot),
            "sha256": _sha256_bytes(request.artifact_bytes),
        },
        "resource": {
            "identity": request.resource_identity,
            "sha256": _sha256_bytes(request.resource_bytes),
        },
        "composite": {
            "identity": request.composite_identity,
            "sha256": _sha256_bytes(request.composite_bytes),
        },
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _write_read_only_snapshot(path: Path, contents: bytes) -> None:
    path.write_bytes(contents)
    os.chmod(path, 0o400)


def _write_private_script_snapshot(path: Path, contents: bytes) -> None:
    path.write_bytes(contents)
    os.chmod(path, 0o500)


def _snapshot_is_unchanged(path: Path, expected: bytes) -> bool:
    try:
        return _sha256_bytes(path.read_bytes()) == _sha256_bytes(expected)
    except OSError:
        return False


def _parse_script_response(stdout: bytes, request: CheckRequest) -> ProviderOutcome:
    try:
        value = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ProviderOutcome(reason="checker-output-invalid")
    if not isinstance(value, dict):
        return ProviderOutcome(reason="checker-output-invalid")
    if value.get("protocol") != request.protocol:
        return ProviderOutcome(reason="checker-output-invalid")
    if value.get("checker") != request.checker.as_dict():
        return ProviderOutcome(reason="checker-identity-mismatch")
    if not isinstance(value.get("results"), list):
        return ProviderOutcome(reason="checker-output-invalid")
    return ProviderOutcome(raw_results=value["results"])


def builtin_registry() -> CheckerRegistry:
    """Create a registry containing the independent JSON item summary checker."""

    provider = JsonItemSummaryProvider()
    identity = CheckerIdentity("json-item-summary", "1", provider.implementation_digest)
    registry = CheckerRegistry()
    registry.register(identity, provider)
    return registry


def _validate_request(request: CheckRequest) -> str | None:
    if not isinstance(request.protocol, int) or isinstance(request.protocol, bool):
        return "protocol"
    if not _bounded_string(request.run_id, MAX_RUN_ID_BYTES):
        return "run-id"
    if not isinstance(request.subject, SubjectIdentity) or not all(
        _bounded_string(value, MAX_SUBJECT_ID_BYTES)
        for value in (request.subject.task_id, request.subject.call_id, request.subject.delivery_id)
    ):
        return "subject"
    if not isinstance(request.checker, CheckerIdentity) or not all(
        _bounded_string(value, MAX_CHECKER_IDENTIFIER_BYTES)
        for value in (request.checker.name, request.checker.version)
    ):
        return "checker"
    if not _is_digest(request.checker.digest):
        return "checker-digest"
    if not _bounded_string(request.rule_version, MAX_RULE_VERSION_BYTES):
        return "rule-version"
    if not request.required_rule_ids:
        return "missing-required-rules"
    if len(request.required_rule_ids) > MAX_REQUIRED_RULES:
        return "required-rule-limit"
    if any(
        not _bounded_string(rule_id, MAX_RULE_ID_BYTES) for rule_id in request.required_rule_ids
    ):
        return "required-rule"
    if len(set(request.required_rule_ids)) != len(request.required_rule_ids):
        return "duplicate-required-rule"
    if not isinstance(request.configuration, Mapping):
        return "configuration"
    try:
        configuration = json.dumps(request.configuration, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError):
        return "configuration"
    if len(configuration) > MAX_PROVIDER_CONFIGURATION_BYTES:
        return "configuration-limit"
    for field_name, contents in (
        ("input-bytes", request.input_bytes),
        ("artifact-bytes", request.artifact_bytes),
        ("resource-bytes", request.resource_bytes),
        ("composite-bytes", request.composite_bytes),
    ):
        if not isinstance(contents, bytes):
            return field_name
        if len(contents) > MAX_BOUND_BYTES:
            return f"{field_name}-limit"
    if not _bounded_string(request.resource_identity, MAX_RESOURCE_IDENTIFIER_BYTES):
        return "resource-identity"
    if not _bounded_string(request.composite_identity, MAX_RESOURCE_IDENTIFIER_BYTES):
        return "composite-identity"
    if not isinstance(request.require_network_isolation, bool):
        return "require-network-isolation"
    return None


def _normalise_rule_results(
    request: CheckRequest,
    outcome: ProviderOutcome,
    unsupported_rules: frozenset[str],
) -> tuple[RuleResult, ...]:
    if outcome.reason is not None:
        return tuple(
            RuleResult(rule_id, STATUS_UNKNOWN, outcome.reason)
            for rule_id in request.required_rule_ids
        )

    raw_results: object = outcome.raw_results
    if raw_results is None:
        raw_results = outcome.rule_results
    if not isinstance(raw_results, (list, tuple)):
        return tuple(
            RuleResult(rule_id, STATUS_UNKNOWN, "checker-output-invalid")
            for rule_id in request.required_rule_ids
        )

    by_rule: dict[str, list[object]] = {}
    unexpected = False
    for raw_result in raw_results:
        rule_id = _raw_rule_id(raw_result)
        if rule_id is None:
            unexpected = True
            continue
        if rule_id not in request.required_rule_ids:
            unexpected = True
            continue
        by_rule.setdefault(rule_id, []).append(raw_result)

    normalised: list[RuleResult] = []
    for rule_id in request.required_rule_ids:
        if rule_id in unsupported_rules:
            normalised.append(RuleResult(rule_id, STATUS_UNKNOWN, "unsupported-rule"))
            continue
        candidates = by_rule.get(rule_id, [])
        if not candidates:
            normalised.append(RuleResult(rule_id, STATUS_UNKNOWN, "missing-required-result"))
            continue
        if len(candidates) != 1:
            normalised.append(RuleResult(rule_id, STATUS_UNKNOWN, "duplicate-rule-result"))
            continue
        normalised.append(_normalise_rule_result(rule_id, candidates[0]))

    if unexpected:
        normalised.append(RuleResult("__provider__", STATUS_UNKNOWN, "unexpected-rule-result"))
    return tuple(normalised)


def _raw_rule_id(value: object) -> str | None:
    if isinstance(value, RuleResult):
        return value.rule_id
    if isinstance(value, dict) and isinstance(value.get("rule_id"), str):
        return value["rule_id"]
    return None


def _normalise_rule_result(expected_rule_id: str, value: object) -> RuleResult:
    if isinstance(value, RuleResult):
        rule_id = value.rule_id
        status = value.status
        reason = value.reason
    elif isinstance(value, dict):
        rule_id = value.get("rule_id")
        status = value.get("status")
        reason = value.get("reason")
    else:
        return RuleResult(expected_rule_id, STATUS_UNKNOWN, "malformed-rule-result")
    if (
        rule_id != expected_rule_id
        or not isinstance(status, str)
        or status not in VALID_STATUSES
    ):
        return RuleResult(expected_rule_id, STATUS_UNKNOWN, "malformed-rule-result")
    if not isinstance(reason, str) or not reason or len(reason) > MAX_REASON_LENGTH:
        return RuleResult(expected_rule_id, STATUS_UNKNOWN, "malformed-rule-result")
    return RuleResult(expected_rule_id, status, reason)


def _aggregate_result(
    request: CheckRequest,
    digests: tuple[str, str, str, str],
    rule_results: tuple[RuleResult, ...],
) -> CheckResult:
    required_results = [
        result for result in rule_results if result.rule_id in request.required_rule_ids
    ]
    if any(result.status == STATUS_FAIL for result in required_results):
        status = STATUS_FAIL
        reason = "required-rule-failed"
    elif (
        len(required_results) == len(request.required_rule_ids)
        and all(result.status == STATUS_PASS for result in required_results)
        and all(result.status != STATUS_UNKNOWN for result in rule_results)
    ):
        status = STATUS_PASS
        reason = "all-required-rules-passed"
    else:
        status = STATUS_UNKNOWN
        unknown_reasons = [
            result.reason for result in rule_results if result.status == STATUS_UNKNOWN
        ]
        reason = (
            unknown_reasons[0]
            if unknown_reasons and all(item == unknown_reasons[0] for item in unknown_reasons)
            else "required-rule-unknown"
        )
    return _result(request, digests, rule_results, status, reason)


def _unknown_result(
    request: CheckRequest,
    digests: tuple[str, str, str, str],
    reason: str,
) -> CheckResult:
    results = tuple(
        RuleResult(rule_id, STATUS_UNKNOWN, reason) for rule_id in request.required_rule_ids
    )
    return _result(request, digests, results, STATUS_UNKNOWN, reason)


def _rejected_request(
    request: CheckRequest,
    digests: tuple[str, str, str, str],
    validation_error: str,
) -> CheckResult:
    reason = f"invalid-request:{validation_error}"
    return _unknown_result(request, digests, reason)


def _result(
    request: CheckRequest,
    digests: tuple[str, str, str, str],
    rule_results: tuple[RuleResult, ...],
    status: str,
    reason: str,
) -> CheckResult:
    input_digest, artifact_digest, resource_digest, composite_digest = digests
    return CheckResult(
        protocol=request.protocol,
        run_id=request.run_id,
        subject=request.subject,
        checker=request.checker,
        rule_version=request.rule_version,
        resource_identity=request.resource_identity,
        composite_identity=request.composite_identity,
        input_digest=input_digest,
        artifact_digest=artifact_digest,
        resource_digest=resource_digest,
        composite_digest=composite_digest,
        rule_results=rule_results,
        status=status,
        reason=reason,
    )


def _request_digests(request: CheckRequest) -> tuple[str, str, str, str]:
    return (
        _sha256_bytes(request.input_bytes),
        _sha256_bytes(request.artifact_bytes),
        _sha256_bytes(request.resource_bytes),
        _sha256_bytes(request.composite_bytes),
    )


def _sha256_bytes(contents: bytes) -> str:
    if not isinstance(contents, bytes):
        return ""
    return hashlib.sha256(contents).hexdigest()


def _module_digest() -> str:
    return _sha256_bytes(Path(__file__).read_bytes())


def _is_digest(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _provider_supports_rule(provider: CheckerProvider, rule_id: str) -> bool:
    return "*" in provider.supported_rules or rule_id in provider.supported_rules


def _non_empty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _bounded_string(value: object, maximum_bytes: int) -> bool:
    return _non_empty_string(value) and len(value.encode("utf-8")) <= maximum_bytes


def _load_json_object(contents: bytes, label: str) -> tuple[dict[str, object] | None, str | None]:
    try:
        value = json.loads(contents.decode("utf-8"), parse_float=Decimal)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, f"{label}-json-invalid"
    if not isinstance(value, dict):
        return None, f"{label}-object-required"
    return value, None


def _read_summary_items(
    input_value: dict[str, object],
) -> tuple[list[tuple[str, Decimal]] | None, str | None]:
    raw_items = input_value.get("items")
    if not isinstance(raw_items, list):
        return None, "input-items-invalid"
    items: list[tuple[str, Decimal]] = []
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            return None, "input-items-invalid"
        item_id = raw_item.get("id")
        if not _non_empty_string(item_id):
            return None, "input-items-invalid"
        value = _decimal_number(raw_item.get("value"))
        if value is None:
            return None, "input-items-invalid"
        items.append((item_id, value))
    return items, None


def _duplicate_id_configuration_error(configuration: Mapping[str, object]) -> str | None:
    value = configuration.get("duplicate_ids_count_once", False)
    if not isinstance(value, bool):
        return "duplicate-id-configuration-invalid"
    return None


def _check_count_rule(
    items: Sequence[tuple[str, Decimal]], artifact: dict[str, object], deduplicate_ids: bool
) -> RuleResult:
    actual = artifact.get("count")
    if not isinstance(actual, int) or isinstance(actual, bool):
        return RuleResult("count", STATUS_UNKNOWN, "artifact-count-invalid")
    expected = len(_contributing_items(items, deduplicate_ids))
    if actual == expected:
        return RuleResult("count", STATUS_PASS, "count-matches-input")
    return RuleResult("count", STATUS_FAIL, "count-does-not-match-input")


def _check_sum_rule(
    items: Sequence[tuple[str, Decimal]], artifact: dict[str, object], deduplicate_ids: bool
) -> RuleResult:
    actual = _decimal_number(artifact.get("sum"))
    if actual is None:
        return RuleResult("sum", STATUS_UNKNOWN, "artifact-sum-invalid")
    expected, sum_error = _exact_decimal_sum(
        [value for _, value in _contributing_items(items, deduplicate_ids)]
    )
    if sum_error is not None:
        return RuleResult("sum", STATUS_UNKNOWN, sum_error)
    assert expected is not None
    if actual == expected:
        return RuleResult("sum", STATUS_PASS, "sum-matches-input")
    return RuleResult("sum", STATUS_FAIL, "sum-does-not-match-input")


def _contributing_items(
    items: Sequence[tuple[str, Decimal]], deduplicate_ids: bool
) -> list[tuple[str, Decimal]]:
    if not deduplicate_ids:
        return list(items)
    seen_ids: set[str] = set()
    contributing: list[tuple[str, Decimal]] = []
    for item_id, value in items:
        if item_id in seen_ids:
            continue
        seen_ids.add(item_id)
        contributing.append((item_id, value))
    return contributing


def _exact_decimal_sum(values: Sequence[Decimal]) -> tuple[Decimal | None, str | None]:
    """Sum finite JSON decimals without relying on the ambient Decimal precision.

    The required precision aligns every nonzero coefficient at the smallest
    exponent, plus enough carry digits for the number of summands. Extremely
    sparse exponents are reported as unknown instead of silently rounded.
    """

    nonzero_values = [value for value in values if value != 0]
    if not nonzero_values:
        return Decimal(0), None

    minimum_exponent = min(value.as_tuple().exponent for value in nonzero_values)
    maximum_adjusted = max(value.adjusted() for value in nonzero_values)
    required_precision = maximum_adjusted - minimum_exponent + 1
    required_precision += len(str(len(nonzero_values)))
    if required_precision > MAX_EXACT_SUM_PRECISION:
        return None, "input-sum-precision-unsupported"

    with localcontext() as context:
        context.prec = max(required_precision, 1)
        return sum(nonzero_values, Decimal(0)), None


def _decimal_number(value: object) -> Decimal | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, Decimal) and value.is_finite():
        return value
    return None
