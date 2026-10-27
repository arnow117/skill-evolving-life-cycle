"""Real-evaluation plans, attempts, and production qualification.

This module deliberately keeps evaluation evidence outside a candidate's
editable ``skills`` and ``resources`` tree.  A report is an index over durable
attempt state and managed-runtime records; it is never itself the authority for
production contribution or release.

The local APIs are also used by the portable runtime.  They depend only on the
standard library and existing SELC manifest, checker, and records contracts.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import sqlite3
import stat
import subprocess
import time
import tomllib
from pathlib import Path
from typing import Any, Iterable

from .checkers import (
    CheckerIdentity,
    CheckRequest,
    SubjectIdentity,
    builtin_registry,
    check_artifact,
)
from .errors import SelcError
from .install import current_installation, install_suite
from .managed_runtime import _history_contains_publication_result, _trusted_posix_account_home
from .manifest import copy_portable_suite, load_manifest, portable_tree_hash, suite_content_hash
from .util import (
    canonical_json,
    exclusive_lock,
    file_hash,
    json_hash,
    read_json,
    tree_hash,
    write_json,
)

PLAN_SCHEMA_VERSION = 1
REPORT_SCHEMA_VERSION = 2
REAL_EXECUTION_TYPE = "real-harness"
FIXTURE_EXECUTION_TYPE = "fixture"
LEGACY_EXECUTION_TYPE = "legacy-fixture"
MAX_ATTEMPT_SECONDS = 180
MAX_MATRICES = 16
MAX_CASES = 64
MAX_TOTAL_EVALUATION_SECONDS = MAX_ATTEMPT_SECONDS * MAX_CASES * MAX_MATRICES
FRAMEWORK_MIT_LICENSE_HASH = "d25592caa5a6ea2534e6d3e38f84e9786650cdb634af57a0fd039e6ac5632462"
SUPPORTED_ADAPTERS = frozenset({"codex-cli", "codex-desktop", "workbuddy-desktop"})
CODEX_POLICY_TEMPLATE_SCHEMA_VERSION = 1
CODEX_ALPHA11_VERSION = "0.163.0-alpha.11"
CODEX_HOOK_TRUST_SCHEMA_VERSION = 2
CODEX_HOOK_TRUST_MODE = "normal-project-and-hook-state-inheritance-v2"
CODEX_MANAGED_HOOK_EVENTS = (
    ("UserPromptSubmit", "user_prompt_submit"),
    ("PreToolUse", "pre_tool_use"),
    ("PostToolUse", "post_tool_use"),
    ("PermissionRequest", "permission_request"),
    ("Stop", "stop"),
)
CODEX_DISABLED_FEATURES = (
    "apps",
    "plugins",
    "remote_plugin",
    "skill_search",
    "multi_agent",
    "computer_use",
    "browser_use",
    "image_generation",
)


def build_codex_policy_template(host_version: str, python_read_root: Path) -> dict[str, object]:
    """Build the one versioned Codex policy template accepted by this evaluator.

    The template is frozen in the plan before creator work.  It permits only
    the canonical attempt project and Codex alpha.11's account-bound bootstrap
    path to vary at runtime.  Callers store this object as a Codex matrix's
    ``tools_policy_template``; they must not substitute a post-run policy hash.
    """

    if host_version != CODEX_ALPHA11_VERSION:
        raise SelcError(
            "UNSUPPORTED_NATIVE_POLICY_TEMPLATE",
            "only the observed Codex alpha.11 native policy shape is supported",
            "use the supported host version or retain this matrix as unverified",
        )
    account_home = _trusted_posix_account_home()
    if account_home is None:
        raise SelcError(
            "UNSUPPORTED_NATIVE_POLICY_TEMPLATE",
            "the current POSIX account home is unavailable",
            "prepare the plan from a supported POSIX account",
        )
    root = _template_read_root(python_read_root)
    entries = sorted(
        [
            _template_special_entry("minimal", "read"),
            _template_path_entry(str(root), "read"),
            _template_parameter_entry("attempt_project", "write"),
            _template_parameter_entry("codex_arg0", "read"),
        ],
        key=canonical_json,
    )
    policy = {
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
    return {
        "schema_version": CODEX_POLICY_TEMPLATE_SCHEMA_VERSION,
        "host": "codex",
        "host_version": host_version,
        "account_home": str(account_home),
        "parameters": {
            "attempt_project": {"kind": "canonical-attempt-project"},
            "codex_arg0": {"kind": "current-account-alpha11-bootstrap"},
        },
        "policy": policy,
        "cli": _fixed_codex_cli_profile(),
        "hook_trust": _fixed_codex_hook_trust_template(),
    }


def write_codex_policy_template(
    host_version: str, python_read_root: Path, output: Path
) -> dict[str, object]:
    """Write a no-clobber template for insertion into a maintainer plan JSON."""

    template = build_codex_policy_template(host_version, python_read_root)
    _write_no_clobber_json(output, template)
    return template


def prepare_real_evaluation_plan(
    suite_root: Path, cases_path: Path, output: Path
) -> dict[str, Any]:
    """Freeze inputs, checker rules, baseline bytes, and harness matrix.

    ``cases_path`` is a maintainer-owned JSON object.  Candidate authoring does
    not receive this path through the generated creator brief.  Its case files
    are copied into the private plan directory so later execution cannot be
    silently retargeted by changing an external path.
    """

    if output.exists():
        raise SelcError(
            "PATH_CONFLICT",
            f"evaluation plan output already exists: {output}",
            "choose an empty evaluation directory",
        )
    output.mkdir(parents=True)
    manifest = load_manifest(suite_root)
    specification = _read_plan_specification(cases_path)
    run_timeout = _positive_bounded_seconds(specification.get("run_timeout_seconds", 180), "run")
    cases = _normalise_cases(
        specification.get("cases"), cases_path.parent, output, default_timeout_seconds=run_timeout
    )
    matrices = _normalise_matrices(specification.get("matrix"))
    variants = _normalise_variants(specification.get("variants"), cases, matrices)
    total_timeout = _positive_bounded_total_seconds(
        specification.get("total_timeout_seconds", run_timeout * len(cases) * len(matrices))
    )
    maximum_attempt_timeout = max(int(case["run_timeout_seconds"]) for case in cases)
    if total_timeout < maximum_attempt_timeout:
        raise SelcError(
            "INVALID_EVALUATION_PLAN",
            "total evaluation timeout cannot be shorter than one frozen attempt timeout",
            "increase total_timeout_seconds",
        )

    baseline = output / "baseline"
    copy_portable_suite(suite_root, baseline)
    _assert_baseline_variant_versions(variants, baseline)
    baseline_hash = portable_tree_hash(baseline)
    plan = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "execution_type": REAL_EXECUTION_TYPE,
        "suite_id": manifest["suite_id"],
        "baseline_snapshot": {
            "path": str(baseline.resolve()),
            "portable_tree_hash": baseline_hash,
            "manifest_hash": file_hash(baseline / "suite.json"),
            "framework_hash": _runtime_hash(baseline),
            "license_hash": _runtime_license_hash(baseline),
        },
        "cases": cases,
        "matrix": matrices,
        "variants": variants,
        "run_timeout_seconds": run_timeout,
        "total_timeout_seconds": total_timeout,
        "candidate_budget": {
            "max_candidates": _positive_bounded_integer(
                specification.get("max_candidates", 2), "max_candidates", maximum=16
            ),
            "max_attempts": _positive_bounded_integer(
                specification.get("max_attempts", len(cases) * len(matrices)),
                "max_attempts",
                maximum=MAX_CASES * MAX_MATRICES,
            ),
        },
    }
    plan["plan_hash"] = _document_hash(plan)
    plan_path = output / "plan.json"
    _write_no_clobber_json(plan_path, plan)

    baseline_bindings = []
    for variant in variants:
        if variant["snapshot"] != "baseline":
            continue
        baseline_candidate = output / f"baseline-{variant['id']}-candidate"
        _copy_editable_candidate(baseline, baseline_candidate)
        binding_path = output / f"baseline-{variant['id']}-binding.json"
        _write_binding(
            plan_path,
            plan,
            variant,
            baseline_candidate,
            baseline,
            binding_path,
        )
        baseline_bindings.append(str(binding_path.resolve()))
    return {
        "plan": str(plan_path.resolve()),
        "plan_hash": plan["plan_hash"],
        "baseline_bindings": baseline_bindings,
    }


def bind_real_evaluation_candidate(
    plan_path: Path, candidate_root: Path, output: Path, variant_id: str | None = None
) -> dict[str, Any]:
    """Create the immutable, full portable candidate snapshot for a frozen plan."""

    if output.exists():
        raise SelcError(
            "PATH_CONFLICT",
            f"candidate binding already exists: {output}",
            "choose a new binding path",
        )
    plan = _load_real_plan(plan_path)
    _assert_baseline_snapshot(plan)
    variant = _candidate_variant(plan, variant_id)
    if not candidate_root.is_dir() or candidate_root.is_symlink():
        raise SelcError(
            "INVALID_CANDIDATE", "candidate directory is unavailable", "prepare a new candidate"
        )
    candidate_sections = {path.name for path in candidate_root.iterdir()}
    if candidate_sections.difference({"skills", "resources"}) or not {
        "skills",
        "resources",
    }.issubset(candidate_sections):
        raise SelcError(
            "INVALID_CANDIDATE",
            "candidate may contain only editable skills and resources",
            "use the directory created by evolve prepare",
        )

    if any(path.is_symlink() for path in candidate_root.rglob("*")):
        raise SelcError(
            "INVALID_CANDIDATE", "candidate cannot contain symlinks", "copy the candidate files"
        )

    snapshot = output.parent / f"{output.stem}-candidate-snapshot"
    if snapshot.exists():
        raise SelcError(
            "PATH_CONFLICT",
            f"candidate snapshot already exists: {snapshot}",
            "choose a new binding path",
        )
    baseline = Path(plan["baseline_snapshot"]["path"])
    copy_portable_suite(baseline, snapshot)
    for section in ("skills", "resources"):
        destination = snapshot / section
        shutil.rmtree(destination)
        shutil.copytree(candidate_root / section, destination)
    manifest = read_json(snapshot / "suite.json")
    manifest["version"] = variant["version"]
    manifest.pop("content_hash", None)
    write_json(snapshot / "suite.json", manifest)
    manifest["content_hash"] = suite_content_hash(snapshot)
    write_json(snapshot / "suite.json", manifest)
    load_manifest(snapshot)

    with exclusive_lock(_plan_lock_path(plan_path, "candidate-bindings")):
        _reserve_candidate_binding(plan_path, plan, candidate_root, output)
        return _write_binding(plan_path, plan, variant, candidate_root, snapshot, output)


def prepare_evaluation_attempt(
    plan_path: Path,
    binding_path: Path,
    case_id: str,
    matrix_id: str,
    attempt_number: int,
    output: Path,
) -> dict[str, Any]:
    """Durably reserve one isolated attempt before a provider can start.

    The directory is created only once.  Repeating a prepare call reads the
    same reservation rather than allocating another budget unit.
    """

    if attempt_number < 1:
        raise SelcError("INVALID_INPUT", "attempt number must be positive", "start at attempt 1")
    plan = _load_real_plan(plan_path)
    binding = _load_binding(binding_path, plan)
    _assert_baseline_snapshot(plan)
    _assert_candidate_snapshot(binding)
    if not _path_is_within(output.resolve(), plan_path.parent.resolve()):
        raise SelcError(
            "UNSAFE_ATTEMPT_PATH",
            "attempt evidence must remain below its immutable plan directory",
            "choose an output path below the plan",
        )
    case = _plan_case(plan, case_id)
    matrix = _plan_matrix(plan, matrix_id)
    if (case_id, matrix_id) not in _required_units(plan, binding["variant_id"]):
        raise SelcError(
            "UNREQUIRED_EVALUATION_UNIT",
            "attempt is outside this variant's frozen evaluation scope",
            "prepare only a required case and matrix unit",
        )
    if attempt_number > int(plan["candidate_budget"]["max_attempts"]):
        raise SelcError(
            "EVALUATION_BUDGET_EXHAUSTED",
            "attempt number exceeds the frozen evaluation budget",
            "prepare a new evaluation plan",
        )

    state_path = output / "attempt.json"
    with exclusive_lock(_plan_lock_path(plan_path, "attempt-reservations")):
        if state_path.exists():
            state = read_json(state_path)
            _assert_attempt_identity(
                state, plan, binding, case_id, matrix_id, attempt_number, attempt_path=output
            )
            return state
        if output.exists():
            raise SelcError(
                "PATH_CONFLICT",
                f"attempt directory is not a reusable reservation: {output}",
                "choose a new path",
            )
        _reserve_attempt(plan_path, plan, binding, case, matrix, attempt_number, output)

        output.mkdir(parents=True)
        attempt_root = output.resolve()
        project_root = attempt_root / "project"
        session_root = attempt_root / "session"
        attempt_suite = attempt_root / "suite"
        project_root.mkdir()
        session_root.mkdir()
        copy_portable_suite(Path(binding["candidate_snapshot"]["path"]), attempt_suite)
        input_path = project_root / "evaluation-input.json"
        source_input = Path(str(case["input_path"]))
        shutil.copy2(source_input, input_path)
        request = _frozen_request(case, input_path)
        request_path = project_root / "evaluation-request.json"
        _write_no_clobber_json(request_path, request)
        installation = install_suite(
            source=attempt_suite,
            project_root=project_root,
            harness=str(matrix["host"]),
            ref=None,
            mode="observed",
        )
        installed = current_installation(project_root, str(plan["suite_id"]))
        if (
            installation["digest"] != binding["candidate_snapshot"]["portable_tree_hash"]
            or installed.get("digest") != installation["digest"]
        ):
            raise SelcError(
                "EVALUATION_INSTALLATION_MISMATCH",
                "the prepared evaluation installation differs from its bound snapshot",
                "prepare a new isolated attempt",
            )
        policy_binding = _prepare_policy_binding(matrix, project_root)
        state = {
            "schema_version": PLAN_SCHEMA_VERSION,
            "execution_type": REAL_EXECUTION_TYPE,
            "state": "prepared",
            "plan_path": str(plan_path.resolve()),
            "plan_hash": plan["plan_hash"],
            "binding_path": str(binding_path.resolve()),
            "binding_hash": binding["binding_hash"],
            "variant_id": binding["variant_id"],
            "case_id": case_id,
            "matrix_id": matrix_id,
            "attempt_number": attempt_number,
            "attempt_path": str(attempt_root),
            "suite_path": str(attempt_suite),
            "project_root": str(project_root),
            "session_root": str(session_root),
            "input_path": str(input_path),
            "input_hash": file_hash(input_path),
            "request_path": str(request_path),
            "request_hash": file_hash(request_path),
            "installation": {
                "suite_id": installation["suite_id"],
                "version": installation["version"],
                "digest": installation["digest"],
                "hooks_hash": json_hash(installed.get("owned_hooks", {})),
            },
            "prepared_at": _utc_timestamp(),
            "budget_consumed": True,
            "case": case,
            "case_hash": json_hash(case),
            "matrix": matrix,
            "matrix_hash": json_hash(matrix),
            "policy_binding": policy_binding,
        }
        _write_no_clobber_json(state_path, state)
        return state


def launch_codex_cli_attempt(attempt_path: Path) -> dict[str, Any]:
    """Run the one supported finite Codex CLI adapter for a prepared attempt.

    There is intentionally no generic command, argv, shell, environment, or
    candidate-selected executable parameter.  Desktop adapters use
    :func:`desktop_request` and :func:`collect_evaluation_attempt` instead.
    """

    state_path = attempt_path / "attempt.json"
    with exclusive_lock(attempt_path / ".launch.lock"):
        state = read_json(state_path)
        if state.get("state") != "prepared":
            raise SelcError(
                "ATTEMPT_NOT_PREPARED",
                "only one prepared attempt may be launched",
                "collect the existing attempt instead of retrying it",
            )
        plan = _load_real_plan(Path(str(state["plan_path"])))
        binding = _load_binding(Path(str(state["binding_path"])), plan)
        _assert_attempt_identity(
            state,
            plan,
            binding,
            str(state["case_id"]),
            str(state["matrix_id"]),
            int(state["attempt_number"]),
            attempt_path=attempt_path,
        )
        matrix = _plan_matrix(plan, str(state["matrix_id"]))
        if matrix.get("adapter") != "codex-cli":
            raise SelcError(
                "UNSUPPORTED_EXECUTOR",
                "only a frozen codex-cli matrix can be launched by SELC",
                "use desktop prepare and collect for desktop matrices",
            )
        executor = matrix.get("executor")
        if not isinstance(executor, dict):
            raise SelcError(
                "EXECUTOR_TAMPERED", "Codex CLI executor is missing", "prepare a new plan"
            )
        binary = Path(str(executor.get("binary", "")))
        if not binary.is_file() or file_hash(binary) != executor.get("digest"):
            raise SelcError(
                "EXECUTOR_TAMPERED",
                "frozen Codex CLI binary changed",
                "prepare a new plan with the current CLI",
            )
        timeout_seconds = int(state["case"]["run_timeout_seconds"])
        if timeout_seconds < 1 or timeout_seconds > MAX_ATTEMPT_SECONDS:
            raise SelcError(
                "ATTEMPT_TAMPERED", "attempt timeout is invalid", "prepare a new attempt"
            )
        claim_path = attempt_path / "launch-claim.json"
        if claim_path.exists():
            _replace_attempt_state(state_path, state, "unknown", budget_consumed=True)
            raise SelcError(
                "ATTEMPT_ALREADY_CLAIMED",
                "a durable launch claim already exists for this attempt",
                "collect the existing attempt; an interrupted launch is never retried",
            )
        hook_trust_binding = _prepare_codex_hook_trust_binding(state, matrix)
        command = _fixed_codex_cli_command(binary, matrix, state, hook_trust_binding)
        prompt = _codex_cli_prompt(Path(str(state["request_path"])), state)
        environment = _fixed_codex_environment(attempt_path, state)
        launch = {
            "schema_version": 1,
            "attempt_id": _attempt_id(state),
            "command": command,
            "command_hash": json_hash(command),
            "environment": environment,
            "environment_hash": json_hash(environment),
            "timeout_seconds": timeout_seconds,
            "hook_trust": hook_trust_binding,
            "hook_trust_hash": json_hash(hook_trust_binding),
            "prepared_at": _utc_timestamp(),
        }
        try:
            _write_no_clobber_json(claim_path, launch)
        except FileExistsError as error:
            _replace_attempt_state(state_path, state, "unknown", budget_consumed=True)
            raise SelcError(
                "ATTEMPT_ALREADY_CLAIMED",
                "a durable launch claim already exists for this attempt",
                "collect the existing attempt; an interrupted launch is never retried",
            ) from error
        state = dict(state)
        state["launch_claim_hash"] = json_hash(launch)
        if _codex_hook_trust_changed(hook_trust_binding, state, matrix):
            _replace_attempt_state(state_path, state, "unknown", budget_consumed=True)
            raise SelcError(
                "HOOK_TRUST_CHANGED",
                "the normal Codex hook trust state changed before launch",
                "review and trust the installed hooks again before a new attempt",
            )
        _replace_attempt_state(state_path, state, "launched", budget_consumed=True)

        started = time.monotonic()
        process: subprocess.Popen[bytes] | None = None
        cleanup_verified = True
        try:
            process = subprocess.Popen(
                command,
                cwd=Path(str(state["project_root"])),
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            stdout, stderr = process.communicate(prompt, timeout=timeout_seconds)
            outcome = "returned"
            exit_code = process.returncode
        except subprocess.TimeoutExpired:
            outcome = "timed_out"
            stdout = b""
            stderr = b""
            exit_code = None
            if process is not None:
                cleanup_verified = _terminate_owned_process_group(process)
                if not cleanup_verified:
                    outcome = "unknown"
        except OSError as error:
            outcome = "unknown"
            stdout = b""
            stderr = str(error).encode("utf-8", errors="replace")
            exit_code = None
        _write_attempt_result(
            attempt_path,
            state,
            outcome,
            elapsed_seconds=time.monotonic() - started,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            cleanup_verified=cleanup_verified,
        )
        return read_json(state_path)


def desktop_request(attempt_path: Path) -> dict[str, Any]:
    """Produce one fixed desktop request without starting a model or host."""

    state_path = attempt_path / "attempt.json"
    state = read_json(state_path)
    matrix = state.get("matrix")
    if not isinstance(matrix, dict) or matrix.get("adapter") not in {
        "codex-desktop",
        "workbuddy-desktop",
    }:
        raise SelcError(
            "UNSUPPORTED_EXECUTOR",
            "desktop preparation requires a frozen desktop matrix",
            "prepare a desktop evaluation attempt",
        )
    request_path = attempt_path / "desktop-request.json"
    if request_path.is_file():
        return read_json(request_path)
    request = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "execution_type": REAL_EXECUTION_TYPE,
        "adapter": matrix["adapter"],
        "host": matrix["host"],
        "host_version": matrix["host_version"],
        "surface": matrix["surface"],
        "model": matrix["model"],
        "project_root": state["project_root"],
        "suite_path": state["suite_path"],
        "input_path": state["input_path"],
        "input_hash": state["input_hash"],
        "request_path": state["request_path"],
        "request_hash": state["request_hash"],
        "installation": state["installation"],
        "attempt_id": _attempt_id(state),
        "timeout_seconds": state["case"]["run_timeout_seconds"],
        "qualification_support": _qualification_support(matrix),
    }
    if request["qualification_support"] != "supported":
        request["qualification_reason"] = str(
            matrix.get("qualification_reason", "native-attribution-unverified")
        )
    _write_no_clobber_json(request_path, request)
    if state.get("state") == "prepared":
        _replace_attempt_state(state_path, state, "external-request-issued", budget_consumed=True)
    return request


def native_evidence_template(attempt_path: Path) -> dict[str, Any]:
    """Describe the only accepted raw native-session evidence for one attempt.

    This read-only helper does not start a provider or write an observation. It
    gives the maintainer the exact bounded fields that ``collect`` will re-read
    from the original session cutoff after the fixed adapter has completed.
    """

    state = read_json(attempt_path / "attempt.json")
    support = _qualification_support(state["matrix"])
    template: dict[str, Any] = {
        "schema_version": 1,
        "attempt_id": _attempt_id(state),
        "qualification_support": support,
        "expected_matrix": state["matrix"],
    }
    if support != "supported":
        template.update(
            {
                "qualification_reason": str(
                    state["matrix"].get("qualification_reason", "native-attribution-unverified")
                ),
                "collect_result": "unknown",
            }
        )
        return template
    template.update(
        {
            "evidence_path": str((Path(state["session_root"]) / "native-evidence.json").resolve()),
            "raw_session_path_requirement": "the original regular JSONL file in the supported native host store",
            "required_fields": [
                "attempt_id",
                "raw_session_path",
                "raw_session_sha256",
                "raw_session_cutoff_bytes",
                "raw_session_path_hash",
                "raw_session_device",
                "raw_session_inode",
            ],
            "required_native_context": [
                "model",
                "permission_profile",
                "sandbox_policy",
                "approval_policy",
                "file_system_sandbox_policy",
            ],
            "policy_template": {
                "template_hash": state["policy_binding"]["template_hash"],
                "mapping": state["policy_binding"]["mapping"],
                "raw_policy_hash": "recomputed from the matched native turn",
            },
        }
    )
    return template


def collect_evaluation_attempt(attempt_path: Path) -> dict[str, Any]:
    """Read existing attempt evidence without invoking a provider or host."""

    state_path = attempt_path / "attempt.json"
    state = read_json(state_path)
    if state.get("state") in {"prepared", "external-request-issued", "launched"}:
        _replace_attempt_state(state_path, state, "unknown", budget_consumed=True)
        state = read_json(state_path)
    evidence = _reconstruct_attempt(state)
    result_path = attempt_path / "collected.json"
    if result_path.exists():
        existing = read_json(result_path)
        if existing.get("attempt_id") != _attempt_id(state):
            raise SelcError(
                "ATTEMPT_TAMPERED", "collected evidence has another attempt identity", "inspect it"
            )
        return existing
    collected = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "execution_type": REAL_EXECUTION_TYPE,
        "attempt_id": _attempt_id(state),
        "state": state["state"],
        "evidence": evidence,
    }
    _write_no_clobber_json(result_path, collected)
    return collected


def finalize_real_evaluation(
    plan_path: Path, binding_paths: Iterable[Path] | Path, attempts: Iterable[Path], output: Path
) -> dict[str, Any]:
    """Create an index over collected attempts after re-reading every record."""

    if output.exists():
        raise SelcError(
            "PATH_CONFLICT", f"evaluation report exists: {output}", "choose a new report path"
        )
    plan = _load_real_plan(plan_path)
    _assert_baseline_snapshot(plan)
    paths = [binding_paths] if isinstance(binding_paths, Path) else list(binding_paths)
    if not paths:
        raise SelcError(
            "INVALID_EVALUATION_BINDING", "at least one binding is required", "bind a candidate"
        )
    bindings = [_load_binding(path, plan) for path in paths]
    if len({binding["variant_id"] for binding in bindings}) != len(bindings):
        raise SelcError(
            "EVALUATION_EVIDENCE_CONFLICT",
            "a report cannot contain two bindings for the same variant",
            "choose one binding for each variant",
        )
    for binding in bindings:
        _assert_candidate_snapshot(binding)
    by_hash = {binding["binding_hash"]: binding for binding in bindings}
    collected: dict[str, list[dict[str, Any]]] = {
        binding["binding_hash"]: [] for binding in bindings
    }
    actual: dict[str, set[tuple[str, str]]] = {
        binding["binding_hash"]: set() for binding in bindings
    }
    for attempt in attempts:
        result = collect_evaluation_attempt(attempt)
        state = read_json(attempt / "attempt.json")
        binding = by_hash.get(state.get("binding_hash"))
        if binding is None:
            raise SelcError(
                "EVALUATION_EVIDENCE_CONFLICT",
                "attempt belongs to a binding that is not in this report",
                "finalize it with its matching binding",
            )
        identity = (str(state.get("case_id")), str(state.get("matrix_id")))
        binding_hash = binding["binding_hash"]
        if identity in actual[binding_hash]:
            raise SelcError(
                "EVALUATION_EVIDENCE_CONFLICT",
                "more than one collected attempt claims one required matrix unit",
                "prepare a new plan instead of combining duplicate attempts",
            )
        actual[binding_hash].add(identity)
        collected[binding_hash].append(
            {
                "attempt_path": str(attempt.resolve()),
                "attempt_id": result["attempt_id"],
                "case_id": identity[0],
                "matrix_id": identity[1],
                "status": result["evidence"]["status"],
                "evidence_digest": json_hash(result["evidence"]),
            }
        )
    qualifications: list[dict[str, Any]] = []
    for binding in bindings:
        binding_hash = binding["binding_hash"]
        units = collected[binding_hash]
        variant = _plan_variant(plan, binding["variant_id"])
        expected = _required_units(plan, binding["variant_id"])
        status = (
            "passed"
            if actual[binding_hash] == expected and all(item["status"] == "pass" for item in units)
            else "unknown"
        )
        qualifications.append(
            {
                "variant_id": binding["variant_id"],
                "version": variant["version"],
                "scope": variant["scope"],
                "binding_path": binding["binding_path"],
                "binding_hash": binding_hash,
                "candidate_path": binding["candidate_path"],
                "candidate_business_hash": binding["candidate_business_hash"],
                "candidate_snapshot_hash": binding["candidate_snapshot"]["portable_tree_hash"],
                "status": status,
                "matrix": units,
            }
        )
    statuses = {qualification["status"] for qualification in qualifications}
    included_variants = {qualification["variant_id"] for qualification in qualifications}
    unverified_variants = [
        {"variant_id": variant["id"], "version": variant["version"], "scope": variant["scope"]}
        for variant in plan["variants"]
        if variant["id"] not in included_variants
    ]
    status = (
        "passed"
        if statuses == {"passed"} and not unverified_variants
        else "partial"
        if "passed" in statuses
        else "unknown"
    )
    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "execution_type": REAL_EXECUTION_TYPE,
        "status": status,
        "plan_path": str(plan_path.resolve()),
        "plan_hash": plan["plan_hash"],
        "qualifications": qualifications,
        "unverified_variants": unverified_variants,
        "usage": {"model_calls": "unknown", "token_cost": "unknown", "money_cost": "unknown"},
    }
    report["report_hash"] = _document_hash(report)
    write_json(output, report)
    return report


def validate_real_evaluation(evaluation_path: Path, candidate_root: Path) -> dict[str, Any]:
    """Reconstruct production qualification without trusting report assertions.

    This function deliberately returns the same error for legacy fixtures,
    missing evidence, and self-asserted real JSON.  Consumers must not expose a
    more permissive path based on an ``eligible`` or ``status`` field.
    """

    try:
        report = read_json(evaluation_path)
    except (OSError, json.JSONDecodeError) as error:
        raise _real_evaluation_required("evaluation report is unreadable") from error
    if (
        report.get("schema_version") != REPORT_SCHEMA_VERSION
        or report.get("execution_type") != REAL_EXECUTION_TYPE
    ):
        raise _real_evaluation_required("a real-harness report is required")
    plan_path = _resolved_file(report.get("plan_path"))
    if plan_path is None:
        raise _real_evaluation_required("real report has no readable immutable plan")
    try:
        plan = _load_real_plan(plan_path)
        _assert_baseline_snapshot(plan)
    except (OSError, json.JSONDecodeError, SelcError) as error:
        raise _real_evaluation_required("real report plan cannot be verified") from error
    if report.get("plan_hash") != plan["plan_hash"]:
        raise _real_evaluation_required("real report plan hash does not match")

    report_hash = report.get("report_hash")
    unsigned = dict(report)
    unsigned.pop("report_hash", None)
    if not isinstance(report_hash, str) or report_hash != _document_hash(unsigned):
        raise _real_evaluation_required("report integrity hash does not match")
    qualifications = report.get("qualifications")
    if not isinstance(qualifications, list) or not qualifications:
        raise _real_evaluation_required("report has no versioned qualification evidence")
    candidates: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    for qualification in qualifications:
        if not isinstance(qualification, dict):
            raise _real_evaluation_required("report qualification is malformed")
        binding_path = _resolved_file(qualification.get("binding_path"))
        if binding_path is None:
            raise _real_evaluation_required("report has no readable immutable binding")
        try:
            binding = _load_binding(binding_path, plan)
            _assert_candidate_snapshot(binding)
        except (OSError, json.JSONDecodeError, SelcError) as error:
            raise _real_evaluation_required("real report binding cannot be verified") from error
        variant = _plan_variant(plan, binding["variant_id"])
        if (
            qualification.get("variant_id") != binding["variant_id"]
            or qualification.get("version") != variant["version"]
            or qualification.get("scope") != variant["scope"]
            or qualification.get("binding_hash") != binding["binding_hash"]
            or qualification.get("candidate_path") != binding["candidate_path"]
            or qualification.get("candidate_business_hash") != binding["candidate_business_hash"]
            or qualification.get("candidate_snapshot_hash")
            != binding["candidate_snapshot"]["portable_tree_hash"]
        ):
            raise _real_evaluation_required("candidate or evaluation binding changed")
        bound_candidate = _resolved_directory(binding.get("candidate_path"))
        if (
            bound_candidate is not None
            and bound_candidate == candidate_root.resolve()
            and tree_hash(candidate_root) == binding["candidate_business_hash"]
        ):
            candidates.append((qualification, binding, variant))
    if len(candidates) != 1:
        raise _real_evaluation_required(
            "report does not have one exact qualification for this candidate"
        )
    qualification, binding, variant = candidates[0]
    if qualification.get("status") != "passed":
        raise _real_evaluation_required(
            "the candidate's required real matrix units are not passing"
        )
    matrix = qualification.get("matrix")
    if not isinstance(matrix, list):
        raise _real_evaluation_required("qualification has no matrix evidence")
    expected = _required_units(plan, variant["id"])
    observed: set[tuple[str, str]] = set()
    for item in matrix:
        if not isinstance(item, dict):
            raise _real_evaluation_required("report matrix entry is malformed")
        unit = (item.get("case_id"), item.get("matrix_id"))
        if (
            not all(isinstance(value, str) for value in unit)
            or unit in observed
            or unit not in expected
        ):
            raise _real_evaluation_required("report matrix does not match the frozen plan")
        attempt_path = _resolved_directory(item.get("attempt_path"))
        if attempt_path is None or not _path_is_within(attempt_path, plan_path.parent):
            raise _real_evaluation_required("attempt evidence is outside the plan directory")
        try:
            state = read_json(attempt_path / "attempt.json")
            _assert_attempt_identity(
                state, plan, binding, unit[0], unit[1], int(state["attempt_number"])
            )
            evidence = _reconstruct_attempt(state)
        except (OSError, ValueError, KeyError, json.JSONDecodeError, SelcError) as error:
            raise _real_evaluation_required("attempt evidence cannot be reconstructed") from error
        if evidence["status"] != "pass" or item.get("status") != "pass":
            raise _real_evaluation_required(
                "one required real attempt is incomplete or not passing"
            )
        if item.get("evidence_digest") != json_hash(evidence):
            raise _real_evaluation_required("attempt evidence digest changed")
        observed.add(unit)
    if observed != expected:
        raise _real_evaluation_required("real report omits a required matrix unit")
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "execution_type": REAL_EXECUTION_TYPE,
        "evaluation_hash": file_hash(evaluation_path),
        "plan_hash": plan["plan_hash"],
        "binding_hash": binding["binding_hash"],
        "variant_id": variant["id"],
        "version": variant["version"],
        "scope": variant["scope"],
        "candidate_business_hash": binding["candidate_business_hash"],
        "candidate_snapshot_hash": binding["candidate_snapshot"]["portable_tree_hash"],
    }


def public_evaluation_summary(report_path: Path) -> dict[str, Any]:
    """Return a pathless, identifier-free summary suitable for publication."""

    report = read_json(report_path)
    qualifications = report.get("qualifications", [])
    if not isinstance(qualifications, list):
        qualifications = []
    return {
        "schema_version": report.get("schema_version"),
        "execution_type": report.get("execution_type", LEGACY_EXECUTION_TYPE),
        "status": report.get("status", "unknown"),
        "plan_hash": report.get("plan_hash"),
        "qualifications": [
            {
                "variant_id": qualification.get("variant_id"),
                "version": qualification.get("version"),
                "scope": qualification.get("scope"),
                "status": qualification.get("status"),
                "candidate_snapshot_hash": qualification.get("candidate_snapshot_hash"),
                "units": [
                    {
                        "case_id": item.get("case_id"),
                        "matrix_id": item.get("matrix_id"),
                        "status": item.get("status"),
                    }
                    for item in qualification.get("matrix", [])
                    if isinstance(item, dict)
                ],
            }
            for qualification in qualifications
            if isinstance(qualification, dict)
        ],
        "unverified_variants": report.get("unverified_variants", []),
        "usage": report.get(
            "usage", {"model_calls": "unknown", "token_cost": "unknown", "money_cost": "unknown"}
        ),
    }


def _read_plan_specification(path: Path) -> dict[str, Any]:
    try:
        value = read_json(path)
    except (OSError, json.JSONDecodeError) as error:
        raise SelcError(
            "INVALID_EVALUATION_PLAN",
            "evaluation case specification is not valid JSON",
            "repair the plan input",
        ) from error
    if not isinstance(value, dict) or value.get("schema_version") != PLAN_SCHEMA_VERSION:
        raise SelcError(
            "INVALID_EVALUATION_PLAN",
            "evaluation case specification has an unsupported schema",
            "use schema version 1",
        )
    return value


def _normalise_cases(
    value: object, base: Path, output: Path, *, default_timeout_seconds: int
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value or len(value) > MAX_CASES:
        raise SelcError(
            "INVALID_EVALUATION_PLAN", "cases must be a non-empty bounded list", "supply cases"
        )
    inputs_root = output / "case-inputs"
    inputs_root.mkdir(parents=True)
    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict):
            raise SelcError(
                "INVALID_EVALUATION_PLAN", "each case must be an object", "repair cases"
            )
        case_id = raw.get("id")
        collection = raw.get("collection")
        input_value = raw.get("input")
        checker = raw.get("checker")
        request = raw.get(
            "request",
            json.dumps({"input": "evaluation-input.json"}, separators=(",", ":")),
        )
        if (
            not isinstance(case_id, str)
            or not case_id
            or case_id in seen
            or collection not in {"regression", "holdout"}
            or not isinstance(input_value, str)
            or not isinstance(checker, dict)
            or not isinstance(request, str)
            or not request
        ):
            raise SelcError(
                "INVALID_EVALUATION_PLAN", "case identity or checker is invalid", "repair cases"
            )
        source = (
            (base / input_value).resolve()
            if not Path(input_value).is_absolute()
            else Path(input_value)
        )
        if not source.is_file() or source.is_symlink():
            raise SelcError(
                "INVALID_EVALUATION_PLAN",
                "case input must be a regular readable file",
                "supply a safe input file",
            )
        contents = source.read_bytes()
        destination = inputs_root / f"{len(cases):03d}-{_safe_case_name(case_id)}.json"
        destination.write_bytes(contents)
        normalised_checker = _normalise_checker(checker)
        cases.append(
            {
                "id": case_id,
                "collection": collection,
                "input_path": str(destination.resolve()),
                "input_hash": hashlib.sha256(contents).hexdigest(),
                "request": request,
                "request_hash": hashlib.sha256(request.encode("utf-8")).hexdigest(),
                "oracle": normalised_checker,
                "oracle_hash": json_hash(normalised_checker),
                "run_timeout_seconds": _positive_bounded_seconds(
                    raw.get("run_timeout_seconds", default_timeout_seconds), "case"
                ),
            }
        )
        seen.add(case_id)
    return cases


def _normalise_checker(value: dict[str, Any]) -> dict[str, Any]:
    name = value.get("name")
    version = value.get("version")
    rules = value.get("rules")
    configuration = value.get("configuration", {})
    if (
        not isinstance(name, str)
        or not isinstance(version, str)
        or not isinstance(rules, list)
        or not rules
        or any(not isinstance(rule, str) or not rule for rule in rules)
        or len(rules) != len(set(rules))
        or not isinstance(configuration, dict)
    ):
        raise SelcError(
            "INVALID_EVALUATION_PLAN", "case checker declaration is invalid", "repair the checker"
        )
    registry = builtin_registry()
    try:
        identity = registry.identity(name, version)
    except KeyError as error:
        raise SelcError(
            "UNKNOWN_EVALUATION_CHECKER",
            "case names no trusted built-in checker",
            "use a registered checker",
        ) from error
    return {
        "name": identity.name,
        "version": identity.version,
        "digest": identity.digest,
        "rules": list(rules),
        "configuration": configuration,
    }


def _normalise_matrices(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value or len(value) > MAX_MATRICES:
        raise SelcError(
            "INVALID_EVALUATION_PLAN",
            "matrix must be a non-empty bounded list",
            "supply matrix entries",
        )
    matrices: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict):
            raise SelcError(
                "INVALID_EVALUATION_PLAN", "matrix entries must be objects", "repair the matrix"
            )
        matrix_id = raw.get("id")
        adapter = raw.get("adapter")
        host = raw.get("host")
        host_version = raw.get("host_version")
        surface = raw.get("surface")
        model = raw.get("model")
        policy_digest = raw.get("tools_policy_digest")
        policy_template = raw.get("tools_policy_template")
        if (
            not isinstance(matrix_id, str)
            or not matrix_id
            or matrix_id in seen
            or adapter not in SUPPORTED_ADAPTERS
            or host not in {"codex", "workbuddy"}
            or not all(isinstance(item, str) and item for item in (host_version, surface, model))
        ):
            raise SelcError(
                "INVALID_EVALUATION_PLAN", "matrix identity is invalid", "repair the matrix"
            )
        expected_host = "workbuddy" if adapter == "workbuddy-desktop" else "codex"
        if host != expected_host:
            raise SelcError(
                "INVALID_EVALUATION_PLAN", "adapter and host disagree", "repair the matrix"
            )
        matrix: dict[str, Any] = {
            "id": matrix_id,
            "adapter": adapter,
            "host": host,
            "host_version": host_version,
            "surface": surface,
            "model": model,
        }
        if host == "codex" and isinstance(policy_template, dict):
            template = _normalise_codex_policy_template(policy_template, host_version)
            matrix["tools_policy_template"] = template
            matrix["tools_policy_template_hash"] = json_hash(template)
            matrix["qualification_support"] = "supported"
        elif isinstance(policy_digest, str) and _is_digest(policy_digest):
            # The old raw digest has no safe parameterization for an isolated
            # project.  Preserve it for diagnostic plans, but never promote it
            # to a production qualification template.
            matrix["legacy_tools_policy_digest"] = policy_digest
            matrix["qualification_support"] = "unverified"
            matrix["qualification_reason"] = (
                "raw-policy-digest-plan"
                if host == "codex"
                else "native-attribution-unverified-for-workbuddy"
            )
        elif host == "workbuddy":
            matrix["qualification_support"] = "unverified"
            matrix["qualification_reason"] = "native-attribution-unverified-for-workbuddy"
        else:
            raise SelcError(
                "INVALID_EVALUATION_PLAN",
                "a supported Codex matrix needs a versioned tools_policy_template",
                "generate the template before preparing the plan",
            )
        if adapter == "codex-cli":
            executor = raw.get("executor")
            if not isinstance(executor, dict):
                raise SelcError(
                    "INVALID_EVALUATION_PLAN",
                    "codex-cli matrix needs a fixed executor",
                    "repair the matrix",
                )
            binary = Path(str(executor.get("binary", "")))
            version = executor.get("version")
            if (
                not binary.is_file()
                or binary.is_symlink()
                or not isinstance(version, str)
                or not version
            ):
                raise SelcError(
                    "INVALID_EVALUATION_PLAN",
                    "codex-cli executor must be a regular versioned binary",
                    "repair the matrix",
                )
            matrix["executor"] = {
                "binary": str(binary.resolve()),
                "digest": file_hash(binary),
                "version": version,
            }
        elif "executor" in raw:
            raise SelcError(
                "INVALID_EVALUATION_PLAN",
                "desktop matrix cannot accept an executor command",
                "remove executor",
            )
        matrices.append(matrix)
        seen.add(matrix_id)
    return matrices


def _normalise_codex_policy_template(
    value: dict[str, object], host_version: str
) -> dict[str, object]:
    """Validate a complete alpha.11 policy template without widening it."""

    required = {
        "schema_version",
        "host",
        "host_version",
        "account_home",
        "parameters",
        "policy",
        "cli",
        "hook_trust",
    }
    if set(value) != required:
        raise SelcError(
            "INVALID_EVALUATION_PLAN",
            "Codex policy template has missing or unsupported fields",
            "generate a new template with evolve policy-template",
        )
    account_home = _trusted_posix_account_home()
    if (
        value.get("schema_version") != CODEX_POLICY_TEMPLATE_SCHEMA_VERSION
        or value.get("host") != "codex"
        or value.get("host_version") != host_version
        or host_version != CODEX_ALPHA11_VERSION
        or account_home is None
        or value.get("account_home") != str(account_home)
        or value.get("parameters")
        != {
            "attempt_project": {"kind": "canonical-attempt-project"},
            "codex_arg0": {"kind": "current-account-alpha11-bootstrap"},
        }
        or value.get("cli") != _fixed_codex_cli_profile()
        or value.get("hook_trust") != _fixed_codex_hook_trust_template()
    ):
        raise SelcError(
            "INVALID_EVALUATION_PLAN",
            "Codex policy template is not the supported frozen alpha.11 profile",
            "regenerate the template on this supported account",
        )
    policy = value.get("policy")
    if not isinstance(policy, dict):
        raise SelcError(
            "INVALID_EVALUATION_PLAN",
            "Codex policy template has no structured policy",
            "regenerate the template",
        )
    normalized_policy = _normalise_template_policy(policy)
    return {
        "schema_version": CODEX_POLICY_TEMPLATE_SCHEMA_VERSION,
        "host": "codex",
        "host_version": host_version,
        "account_home": str(account_home),
        "parameters": {
            "attempt_project": {"kind": "canonical-attempt-project"},
            "codex_arg0": {"kind": "current-account-alpha11-bootstrap"},
        },
        "policy": normalized_policy,
        "cli": _fixed_codex_cli_profile(),
        "hook_trust": _fixed_codex_hook_trust_template(),
    }


def _normalise_template_policy(value: dict[str, object]) -> dict[str, object]:
    expected_keys = {
        "permission_profile",
        "sandbox_policy",
        "approval_policy",
        "file_system_sandbox_policy",
        "tool_names",
    }
    if set(value) != expected_keys:
        raise SelcError(
            "INVALID_EVALUATION_PLAN",
            "Codex policy template has unsupported policy fields",
            "regenerate the template",
        )
    profile = value.get("permission_profile")
    sandbox = value.get("sandbox_policy")
    filesystem = value.get("file_system_sandbox_policy")
    if (
        not isinstance(profile, dict)
        or not isinstance(sandbox, dict)
        or not isinstance(filesystem, dict)
        or profile.get("type") != "managed"
        or profile.get("network") != "restricted"
        or set(profile) != {"type", "file_system", "network"}
        or sandbox
        != {
            "type": "workspace-write",
            "network_access": False,
            "exclude_tmpdir_env_var": True,
            "exclude_slash_tmp": True,
        }
        or value.get("approval_policy") != "never"
        or value.get("tool_names") != ["exec"]
        or filesystem.get("kind") != "restricted"
        or set(filesystem) != {"kind", "entries"}
    ):
        raise SelcError(
            "INVALID_EVALUATION_PLAN",
            "Codex policy template widens its fixed policy fields",
            "regenerate the template",
        )
    profile_filesystem = profile.get("file_system")
    if (
        not isinstance(profile_filesystem, dict)
        or set(profile_filesystem) != {"type", "entries"}
        or profile_filesystem.get("type") != "restricted"
    ):
        raise SelcError(
            "INVALID_EVALUATION_PLAN",
            "Codex permission_profile filesystem declaration is invalid",
            "regenerate the template",
        )
    profile_entries = _normalise_template_entries(profile_filesystem.get("entries"))
    filesystem_entries = _normalise_template_entries(filesystem.get("entries"))
    if profile_entries != filesystem_entries:
        raise SelcError(
            "INVALID_EVALUATION_PLAN",
            "Codex filesystem declarations disagree",
            "make permission_profile and file_system_sandbox_policy identical",
        )
    required_parameterized_entries = {
        canonical_json(_template_special_entry("minimal", "read")),
        canonical_json(_template_parameter_entry("attempt_project", "write")),
        canonical_json(_template_parameter_entry("codex_arg0", "read")),
    }
    non_path_entries = {
        canonical_json(entry) for entry in profile_entries if entry["path"].get("type") != "path"
    }
    readonly_paths = [
        entry
        for entry in profile_entries
        if entry["access"] == "read"
        and isinstance(entry["path"], dict)
        and entry["path"].get("type") == "path"
    ]
    if (
        non_path_entries != required_parameterized_entries
        or len(readonly_paths) != 1
        or len(profile_entries) != 4
    ):
        raise SelcError(
            "INVALID_EVALUATION_PLAN",
            "Codex policy template must contain only minimal, Python, project, and arg0 roots",
            "generate the supported template",
        )
    _template_read_root(Path(str(readonly_paths[0]["path"]["path"])))
    return {
        "permission_profile": {
            "type": "managed",
            "file_system": {"type": "restricted", "entries": profile_entries},
            "network": "restricted",
        },
        "sandbox_policy": {
            "type": "workspace-write",
            "network_access": False,
            "exclude_tmpdir_env_var": True,
            "exclude_slash_tmp": True,
        },
        "approval_policy": "never",
        "file_system_sandbox_policy": {"kind": "restricted", "entries": profile_entries},
        "tool_names": ["exec"],
    }


def _normalise_template_entries(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list) or not value:
        raise SelcError(
            "INVALID_EVALUATION_PLAN",
            "Codex policy entries must be a non-empty list",
            "repair the template",
        )
    entries: list[dict[str, object]] = []
    seen: set[str] = set()
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != {"path", "access"}:
            raise SelcError(
                "INVALID_EVALUATION_PLAN", "Codex policy entry is invalid", "repair the template"
            )
        path = entry.get("path")
        access = entry.get("access")
        if access not in {"read", "write", "deny"} or not isinstance(path, dict):
            raise SelcError(
                "INVALID_EVALUATION_PLAN",
                "Codex policy entry has invalid access",
                "repair the template",
            )
        normalized_path: dict[str, object]
        if path.get("type") == "special" and path == {
            "type": "special",
            "value": {"kind": "minimal"},
        }:
            normalized_path = {"type": "special", "value": {"kind": "minimal"}}
        elif (
            path.get("type") == "parameter"
            and path.get("name")
            in {
                "attempt_project",
                "codex_arg0",
            }
            and set(path) == {"type", "name"}
        ):
            normalized_path = {"type": "parameter", "name": str(path["name"])}
        elif (
            path.get("type") == "path"
            and set(path) == {"type", "path"}
            and isinstance(path.get("path"), str)
        ):
            normalized_path = {"type": "path", "path": str(_template_read_root(Path(path["path"])))}
        else:
            raise SelcError(
                "INVALID_EVALUATION_PLAN",
                "Codex policy path entry is unsupported",
                "repair the template",
            )
        normalized = {"path": normalized_path, "access": access}
        identity = canonical_json(normalized)
        if identity in seen:
            raise SelcError(
                "INVALID_EVALUATION_PLAN",
                "Codex policy contains duplicate entries",
                "remove duplicates",
            )
        seen.add(identity)
        entries.append(normalized)
    return sorted(entries, key=canonical_json)


def _template_read_root(value: Path) -> Path:
    if value.is_symlink() or not value.is_dir():
        raise SelcError(
            "INVALID_EVALUATION_PLAN",
            "the selected Python dependency root must be a regular directory",
            "supply the exact trusted Python dependency root",
        )
    root = value.resolve()
    account_home = _trusted_posix_account_home()
    if account_home is not None:
        codex_root = account_home / ".codex"
        try:
            codex_root.relative_to(root)
        except ValueError:
            pass
        else:
            raise SelcError(
                "INVALID_EVALUATION_PLAN",
                "the Python dependency root may not enclose the Codex account directory",
                "supply the interpreter dependency directory instead",
            )
    return root


def _template_special_entry(kind: str, access: str) -> dict[str, object]:
    return {"path": {"type": "special", "value": {"kind": kind}}, "access": access}


def _template_path_entry(path: str, access: str) -> dict[str, object]:
    return {"path": {"type": "path", "path": path}, "access": access}


def _template_parameter_entry(name: str, access: str) -> dict[str, object]:
    return {"path": {"type": "parameter", "name": name}, "access": access}


def _fixed_codex_cli_profile() -> dict[str, object]:
    return {
        "approval_policy": "never",
        "ignore_user_config": True,
        "subcommand": "exec",
        "flags": ["--skip-git-repo-check", "--strict-config", "--json", "--color", "never"],
        "enable_features": ["hooks"],
        "disable_features": list(CODEX_DISABLED_FEATURES),
        "settings": {
            "model_provider": "openai",
            "web_search": "disabled",
            "default_permissions": "selc-evaluation",
            "network_enabled": False,
            "project_doc_max_bytes": 0,
            "project_root_markers": [],
        },
    }


def _fixed_codex_hook_trust_template() -> dict[str, object]:
    """Declare the normal project and hook trust state the runner can inherit.

    Codex's public ``--ignore-user-config`` flag otherwise omits normal
    UI-reviewed project and hook state. The template contains no trust values:
    the exact project decision and five hook hashes can only be copied from the
    current account's normal review immediately before launch.
    """

    return {
        "schema_version": CODEX_HOOK_TRUST_SCHEMA_VERSION,
        "mode": CODEX_HOOK_TRUST_MODE,
        "state_source": "os-account-config-hooks-state",
        "events": [
            {"event": event, "state_event": state_event}
            for event, state_event in CODEX_MANAGED_HOOK_EVENTS
        ],
    }


def _prepare_policy_binding(matrix: dict[str, Any], project_root: Path) -> dict[str, object]:
    """Bind the only runtime policy parameters before a host can start."""

    if _qualification_support(matrix) != "supported":
        return {
            "status": "unverified",
            "reason": str(matrix.get("qualification_reason", "native-attribution-unverified")),
        }
    template = matrix.get("tools_policy_template")
    template_hash = matrix.get("tools_policy_template_hash")
    if not isinstance(template, dict) or not isinstance(template_hash, str):
        raise SelcError(
            "ATTEMPT_TAMPERED",
            "a supported Codex matrix lacks its frozen policy template",
            "prepare a new evaluation plan",
        )
    normalized = _normalise_codex_policy_template(template, str(matrix["host_version"]))
    if normalized != template or json_hash(template) != template_hash:
        raise SelcError(
            "ATTEMPT_TAMPERED",
            "the frozen Codex policy template changed",
            "prepare a new evaluation plan",
        )
    account_home = str(template["account_home"])
    project = str(project_root.resolve())
    arg0_parent = str(Path(account_home) / ".codex" / "tmp" / "arg0-macos")
    mapping = {
        "attempt_project": project,
        "account_home": account_home,
        "codex_arg0_parent": arg0_parent,
        "codex_arg0_pattern": "codex-arg0[A-Za-z0-9]+",
    }
    configuration = {
        "template_hash": template_hash,
        "mapping": mapping,
        "cli": template["cli"],
        "hook_trust": template["hook_trust"],
    }
    return {
        "status": "bound",
        "template_hash": template_hash,
        "mapping": mapping,
        "cli_configuration": configuration,
        "cli_configuration_hash": json_hash(configuration),
    }


def _qualification_support(matrix: dict[str, Any]) -> str:
    """Return the evaluator's derived native-attribution support state.

    WorkBuddy has a prepared external runner and existing business compatibility
    coverage, but this evaluator has no bounded WorkBuddy native reconstruction
    parser.  Its plan entries therefore remain executable preparation records
    whose collection is intentionally unverified rather than routed through the
    Codex-only parser.
    """

    if matrix.get("host") == "codex" and not _has_supported_codex_hook_trust_template(matrix):
        return "unverified"
    declared = matrix.get("qualification_support")
    if declared in {"supported", "unverified"}:
        return declared
    return "supported" if matrix.get("host") == "codex" else "unverified"


def _has_supported_codex_hook_trust_template(matrix: dict[str, Any]) -> bool:
    template = matrix.get("tools_policy_template")
    return (
        isinstance(template, dict)
        and template.get("hook_trust") == _fixed_codex_hook_trust_template()
    )


def _normalise_variants(
    value: object, cases: list[dict[str, Any]], matrices: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Freeze the scope and required units of every releasable version.

    A plan that predates this field gets one explicit candidate variant covering
    its complete matrix.  New plans can distinguish an initial stable snapshot
    from an improved candidate without pretending that either supports cases
    outside its own declared scope.
    """

    case_ids = {case["id"] for case in cases}
    matrix_ids = {matrix["id"] for matrix in matrices}
    if value is None:
        value = [
            {
                "id": "candidate",
                "version": "candidate",
                "snapshot": "candidate",
                "scope": {"kind": "full-plan"},
                "required_cases": sorted(case_ids),
                "required_matrices": sorted(matrix_ids),
            }
        ]
    if not isinstance(value, list) or not value:
        raise SelcError(
            "INVALID_EVALUATION_PLAN", "variants must be a non-empty list", "declare version scopes"
        )
    variants: list[dict[str, Any]] = []
    seen: set[str] = set()
    versions: set[str] = set()
    baseline_count = 0
    for raw in value:
        if not isinstance(raw, dict):
            raise SelcError(
                "INVALID_EVALUATION_PLAN", "each variant must be an object", "repair variants"
            )
        variant_id = raw.get("id")
        version = raw.get("version")
        snapshot = raw.get("snapshot")
        scope = raw.get("scope")
        required_cases = raw.get("required_cases", sorted(case_ids))
        required_matrices = raw.get("required_matrices", sorted(matrix_ids))
        if (
            not isinstance(variant_id, str)
            or not variant_id
            or variant_id in seen
            or not isinstance(version, str)
            or not version
            or version in versions
            or snapshot not in {"baseline", "candidate"}
            or not isinstance(scope, dict)
            or not scope
            or not isinstance(required_cases, list)
            or not isinstance(required_matrices, list)
            or not required_cases
            or not required_matrices
            or any(not isinstance(item, str) for item in required_cases + required_matrices)
            or len(required_cases) != len(set(required_cases))
            or len(required_matrices) != len(set(required_matrices))
            or not set(required_cases).issubset(case_ids)
            or not set(required_matrices).issubset(matrix_ids)
        ):
            raise SelcError(
                "INVALID_EVALUATION_PLAN",
                "variant scope or required units are invalid",
                "repair variants",
            )
        if snapshot == "baseline":
            baseline_count += 1
        variants.append(
            {
                "id": variant_id,
                "version": version,
                "snapshot": snapshot,
                "scope": scope,
                "required_cases": sorted(required_cases),
                "required_matrices": sorted(required_matrices),
            }
        )
        seen.add(variant_id)
        versions.add(version)
    if baseline_count > 1:
        raise SelcError(
            "INVALID_EVALUATION_PLAN",
            "one immutable baseline can have only one baseline variant",
            "use one baseline variant and candidate variants for later versions",
        )
    return variants


def _load_real_plan(path: Path) -> dict[str, Any]:
    value = read_json(path)
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != PLAN_SCHEMA_VERSION
        or value.get("execution_type") != REAL_EXECUTION_TYPE
        or not isinstance(value.get("plan_hash"), str)
    ):
        raise SelcError(
            "INVALID_EVALUATION_PLAN", "real evaluation plan is malformed", "prepare a new plan"
        )
    unsigned = dict(value)
    plan_hash = unsigned.pop("plan_hash")
    if plan_hash != _document_hash(unsigned):
        raise SelcError(
            "EVALUATION_PLAN_TAMPERED", "evaluation plan hash changed", "prepare a new plan"
        )
    if not isinstance(value.get("cases"), list) or not isinstance(value.get("matrix"), list):
        raise SelcError(
            "INVALID_EVALUATION_PLAN", "evaluation plan is incomplete", "prepare a new plan"
        )
    variants = value.get("variants")
    if not isinstance(variants, list) or not variants:
        raise SelcError(
            "INVALID_EVALUATION_PLAN",
            "evaluation plan has no versioned scope",
            "prepare a new plan",
        )
    _assert_case_inputs(value)
    return value


def _load_binding(path: Path, plan: dict[str, Any]) -> dict[str, Any]:
    value = read_json(path)
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != PLAN_SCHEMA_VERSION
        or value.get("execution_type") != REAL_EXECUTION_TYPE
        or value.get("plan_hash") != plan["plan_hash"]
        or not isinstance(value.get("binding_hash"), str)
    ):
        raise SelcError(
            "INVALID_EVALUATION_BINDING", "candidate binding is malformed", "bind a new candidate"
        )
    unsigned = dict(value)
    binding_hash = unsigned.pop("binding_hash")
    if binding_hash != _document_hash(unsigned):
        raise SelcError(
            "EVALUATION_BINDING_TAMPERED", "candidate binding hash changed", "bind a new candidate"
        )
    if value.get("binding_path") != str(path.resolve()):
        raise SelcError(
            "EVALUATION_BINDING_TAMPERED", "binding was moved after it was frozen", "bind again"
        )
    variant = _plan_variant(plan, value.get("variant_id"))
    if value.get("variant_version") != variant["version"]:
        raise SelcError(
            "EVALUATION_BINDING_TAMPERED",
            "binding version differs from its plan",
            "bind a new candidate",
        )
    snapshot_value = value.get("candidate_snapshot")
    snapshot = (
        _resolved_directory(snapshot_value.get("path"))
        if isinstance(snapshot_value, dict)
        else None
    )
    if snapshot is None or load_manifest(snapshot).get("version") != variant["version"]:
        raise SelcError(
            "EVALUATION_BINDING_TAMPERED",
            "the bound snapshot manifest version differs from its frozen variant",
            "bind a new candidate snapshot",
        )
    return value


def _assert_case_inputs(plan: dict[str, Any]) -> None:
    snapshot = plan.get("baseline_snapshot")
    if not isinstance(snapshot, dict):
        raise SelcError(
            "EVALUATION_PLAN_TAMPERED", "baseline snapshot is missing", "prepare a new plan"
        )
    baseline = _resolved_directory(snapshot.get("path"))
    if baseline is None:
        raise SelcError(
            "EVALUATION_PLAN_TAMPERED", "baseline snapshot is unavailable", "prepare a new plan"
        )
    inputs_root = baseline.parent / "case-inputs"
    for case in plan["cases"]:
        if not isinstance(case, dict):
            raise SelcError(
                "EVALUATION_PLAN_TAMPERED", "frozen case is malformed", "prepare a new plan"
            )
        input_path = _resolved_file(case.get("input_path"))
        oracle = case.get("oracle")
        try:
            input_path.relative_to(inputs_root)
        except (AttributeError, ValueError):
            raise SelcError(
                "EVALUATION_PLAN_TAMPERED",
                "frozen input is outside the plan directory",
                "prepare a new plan",
            ) from None
        if (
            file_hash(input_path) != case.get("input_hash")
            or not isinstance(case.get("request"), str)
            or hashlib.sha256(str(case.get("request")).encode("utf-8")).hexdigest()
            != case.get("request_hash")
            or not isinstance(oracle, dict)
            or json_hash(oracle) != case.get("oracle_hash")
        ):
            raise SelcError(
                "EVALUATION_PLAN_TAMPERED", "frozen input or oracle changed", "prepare a new plan"
            )


def _plan_variant(plan: dict[str, Any], variant_id: object) -> dict[str, Any]:
    for variant in plan["variants"]:
        if isinstance(variant, dict) and variant.get("id") == variant_id:
            return variant
    raise SelcError(
        "UNKNOWN_EVALUATION_VARIANT", "variant is not in the frozen plan", "use a planned variant"
    )


def _candidate_variant(plan: dict[str, Any], variant_id: str | None) -> dict[str, Any]:
    candidates = [variant for variant in plan["variants"] if variant.get("snapshot") == "candidate"]
    if variant_id is None and len(candidates) == 1:
        return candidates[0]
    if variant_id is not None:
        variant = _plan_variant(plan, variant_id)
        if variant.get("snapshot") == "candidate":
            return variant
    raise SelcError(
        "UNKNOWN_EVALUATION_VARIANT",
        "bind needs one declared candidate variant",
        "pass --variant for the candidate version",
    )


def _required_units(plan: dict[str, Any], variant_id: str) -> set[tuple[str, str]]:
    variant = _plan_variant(plan, variant_id)
    return {
        (case_id, matrix_id)
        for case_id in variant["required_cases"]
        for matrix_id in variant["required_matrices"]
    }


def _assert_baseline_variant_versions(variants: list[dict[str, Any]], baseline: Path) -> None:
    """Require a baseline variant to name the immutable baseline manifest version."""

    baseline_version = load_manifest(baseline)["version"]
    for variant in variants:
        if variant["snapshot"] == "baseline" and variant["version"] != baseline_version:
            raise SelcError(
                "INVALID_EVALUATION_PLAN",
                "a baseline variant version must equal the immutable baseline manifest version",
                "declare the baseline's actual stable version",
            )


def _plan_lock_path(plan_path: Path, name: str) -> Path:
    return plan_path.parent / ".locks" / f"{name}.lock"


def _candidate_reservations_path(plan_path: Path) -> Path:
    return plan_path.parent / "candidate-reservations.json"


def _attempt_reservations_path(plan_path: Path) -> Path:
    return plan_path.parent / "attempt-reservations.json"


def _reservation_document(path: Path, plan: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": 1, "plan_hash": plan["plan_hash"], "reservations": []}
    document = read_json(path)
    if (
        document.get("schema_version") != 1
        or document.get("plan_hash") != plan["plan_hash"]
        or not isinstance(document.get("reservations"), list)
    ):
        raise SelcError(
            "EVALUATION_RESERVATION_TAMPERED",
            "the plan-local reservation ledger changed",
            "prepare a new evaluation plan",
        )
    return document


def _write_reservation_document(path: Path, document: dict[str, Any]) -> None:
    payload = dict(document)
    payload["reservation_hash"] = _document_hash(document)
    _write_atomic_json(path, payload)


def _load_reservation_document(path: Path, plan: dict[str, Any]) -> dict[str, Any]:
    document = _reservation_document(path, plan)
    if not path.exists():
        return document
    saved_hash = document.pop("reservation_hash", None)
    if saved_hash != _document_hash(document):
        raise SelcError(
            "EVALUATION_RESERVATION_TAMPERED",
            "the plan-local reservation ledger integrity hash changed",
            "prepare a new evaluation plan",
        )
    return document


def _reserve_candidate_binding(
    plan_path: Path,
    plan: dict[str, Any],
    candidate_root: Path,
    binding_path: Path,
) -> None:
    """Count distinct editable candidates once across their version bindings."""

    path = _candidate_reservations_path(plan_path)
    document = _load_reservation_document(path, plan)
    candidate_hash = tree_hash(candidate_root)
    candidates = {
        (entry.get("candidate_path"), entry.get("candidate_business_hash"))
        for entry in document["reservations"]
        if isinstance(entry, dict)
    }
    identity = (str(candidate_root.resolve()), candidate_hash)
    if identity not in candidates and len(candidates) >= plan["candidate_budget"]["max_candidates"]:
        raise SelcError(
            "EVALUATION_BUDGET_EXHAUSTED",
            "the frozen maximum candidate budget is exhausted",
            "prepare a new evaluation plan",
        )
    document["reservations"].append(
        {
            "candidate_path": identity[0],
            "candidate_business_hash": candidate_hash,
            "binding_path": str(binding_path.resolve()),
            "reserved_at": _utc_timestamp(),
        }
    )
    _write_reservation_document(path, document)


def _reserve_attempt(
    plan_path: Path,
    plan: dict[str, Any],
    binding: dict[str, Any],
    case: dict[str, Any],
    matrix: dict[str, Any],
    attempt_number: int,
    output: Path,
) -> None:
    """Persist a plan-local attempt budget claim before any install or launch."""

    path = _attempt_reservations_path(plan_path)
    document = _load_reservation_document(path, plan)
    reservations = document["reservations"]
    identity = {
        "binding_hash": binding["binding_hash"],
        "case_id": case["id"],
        "matrix_id": matrix["id"],
        "attempt_number": attempt_number,
    }
    for existing in reservations:
        if not isinstance(existing, dict):
            raise SelcError(
                "EVALUATION_RESERVATION_TAMPERED",
                "the plan-local attempt reservation is malformed",
                "prepare a new evaluation plan",
            )
        if all(existing.get(key) == value for key, value in identity.items()):
            raise SelcError(
                "EVALUATION_BUDGET_EXHAUSTED",
                "this frozen attempt identity is already reserved",
                "resume the existing attempt instead of preparing another",
            )
    if len(reservations) >= int(plan["candidate_budget"]["max_attempts"]):
        raise SelcError(
            "EVALUATION_BUDGET_EXHAUSTED",
            "the frozen maximum attempt budget is exhausted",
            "prepare a new evaluation plan",
        )
    reserved_seconds = sum(
        int(entry.get("timeout_seconds", 0))
        for entry in reservations
        if isinstance(entry, dict) and type(entry.get("timeout_seconds")) is int
    )
    timeout_seconds = int(case["run_timeout_seconds"])
    if reserved_seconds + timeout_seconds > int(plan["total_timeout_seconds"]):
        raise SelcError(
            "EVALUATION_BUDGET_EXHAUSTED",
            "the frozen total evaluation time budget is exhausted",
            "prepare a new evaluation plan",
        )
    reservations.append(
        identity
        | {
            "attempt_path": str(output.resolve()),
            "timeout_seconds": timeout_seconds,
            "reserved_at": _utc_timestamp(),
        }
    )
    _write_reservation_document(path, document)


def _frozen_request(case: dict[str, Any], input_path: Path) -> dict[str, Any]:
    request = case.get("request")
    if not isinstance(request, str) or not request:
        raise SelcError(
            "EVALUATION_PLAN_TAMPERED", "frozen business request is missing", "prepare a new plan"
        )
    return {
        "schema_version": 1,
        "request": request,
        "request_hash": hashlib.sha256(request.encode("utf-8")).hexdigest(),
        "input_path": input_path.name,
        "input_hash": file_hash(input_path),
    }


def _copy_editable_candidate(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True)
    for section in ("skills", "resources"):
        shutil.copytree(source / section, destination / section)


def _write_binding(
    plan_path: Path,
    plan: dict[str, Any],
    variant: dict[str, Any],
    candidate_root: Path,
    snapshot: Path,
    output: Path,
) -> dict[str, Any]:
    binding = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "execution_type": REAL_EXECUTION_TYPE,
        "plan_path": str(plan_path.resolve()),
        "plan_hash": plan["plan_hash"],
        "binding_path": str(output.resolve()),
        "variant_id": variant["id"],
        "variant_version": variant["version"],
        "candidate_path": str(candidate_root.resolve()),
        "candidate_business_hash": tree_hash(candidate_root),
        "candidate_snapshot": {
            "path": str(snapshot.resolve()),
            "portable_tree_hash": portable_tree_hash(snapshot),
            "manifest_hash": file_hash(snapshot / "suite.json"),
            "framework_hash": _runtime_hash(snapshot),
            "license_hash": _runtime_license_hash(snapshot),
        },
    }
    binding["binding_hash"] = _document_hash(binding)
    _write_no_clobber_json(output, binding)
    return binding


def _assert_baseline_snapshot(plan: dict[str, Any]) -> None:
    snapshot = plan.get("baseline_snapshot")
    if not isinstance(snapshot, dict):
        raise SelcError(
            "EVALUATION_PLAN_TAMPERED", "baseline snapshot is missing", "prepare a new plan"
        )
    root = _resolved_directory(snapshot.get("path"))
    if root is None or portable_tree_hash(root) != snapshot.get("portable_tree_hash"):
        raise SelcError(
            "EVALUATION_PLAN_TAMPERED", "baseline snapshot changed", "prepare a new plan"
        )
    _assert_runtime_license(root, snapshot)


def _assert_candidate_snapshot(binding: dict[str, Any]) -> None:
    snapshot = binding.get("candidate_snapshot")
    if not isinstance(snapshot, dict):
        raise _real_evaluation_required("candidate snapshot is missing")
    root = _resolved_directory(snapshot.get("path"))
    if root is None or portable_tree_hash(root) != snapshot.get("portable_tree_hash"):
        raise _real_evaluation_required("candidate snapshot changed")
    _assert_runtime_license(root, snapshot)


def _assert_runtime_license(root: Path, snapshot: dict[str, Any]) -> None:
    if (
        file_hash(root / "suite.json") != snapshot.get("manifest_hash")
        or _runtime_hash(root) != snapshot.get("framework_hash")
        or _runtime_license_hash(root) != snapshot.get("license_hash")
    ):
        raise SelcError(
            "EVALUATION_PLAN_TAMPERED",
            "portable framework or runtime license changed",
            "prepare a new plan",
        )


def _plan_case(plan: dict[str, Any], case_id: str) -> dict[str, Any]:
    for case in plan["cases"]:
        if isinstance(case, dict) and case.get("id") == case_id:
            return case
    raise SelcError(
        "UNKNOWN_EVALUATION_CASE", "case is not in the frozen plan", "use a planned case id"
    )


def _plan_matrix(plan: dict[str, Any], matrix_id: str) -> dict[str, Any]:
    for matrix in plan["matrix"]:
        if isinstance(matrix, dict) and matrix.get("id") == matrix_id:
            return matrix
    raise SelcError(
        "UNKNOWN_EVALUATION_MATRIX", "matrix is not in the frozen plan", "use a planned matrix id"
    )


def _assert_attempt_identity(
    state: dict[str, Any],
    plan: dict[str, Any],
    binding: dict[str, Any],
    case_id: str,
    matrix_id: str,
    attempt: int,
    *,
    attempt_path: Path | None = None,
) -> None:
    case = _plan_case(plan, case_id)
    matrix = _plan_matrix(plan, matrix_id)
    root = (
        attempt_path.resolve()
        if attempt_path is not None
        else _resolved_directory(state.get("attempt_path"))
    )
    if root is None:
        raise SelcError("ATTEMPT_TAMPERED", "attempt root is unavailable", "prepare a new attempt")
    expected_paths = {
        "attempt_path": str(root),
        "suite_path": str(root / "suite"),
        "project_root": str(root / "project"),
        "session_root": str(root / "session"),
        "input_path": str(root / "project" / "evaluation-input.json"),
        "request_path": str(root / "project" / "evaluation-request.json"),
    }
    expected = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "execution_type": REAL_EXECUTION_TYPE,
        "plan_hash": plan["plan_hash"],
        "binding_hash": binding["binding_hash"],
        "variant_id": binding["variant_id"],
        "case_id": case_id,
        "matrix_id": matrix_id,
        "attempt_number": attempt,
        "case_hash": json_hash(case),
        "matrix_hash": json_hash(matrix),
    }
    if (
        not all(state.get(key) == value for key, value in expected.items())
        or not all(state.get(key) == value for key, value in expected_paths.items())
        or state.get("case") != case
        or state.get("matrix") != matrix
    ):
        raise SelcError(
            "ATTEMPT_TAMPERED",
            "attempt identity differs from its frozen plan",
            "prepare a new attempt",
        )
    input_hash = file_hash(Path(expected_paths["input_path"]))
    if input_hash != case["input_hash"] or state.get("input_hash") != input_hash:
        raise SelcError(
            "ATTEMPT_TAMPERED",
            "attempt input differs from its frozen case",
            "prepare a new attempt",
        )
    request = _frozen_request(case, Path(expected_paths["input_path"]))
    if file_hash(Path(expected_paths["request_path"])) != state.get("request_hash"):
        raise SelcError(
            "ATTEMPT_TAMPERED", "attempt request bytes changed", "prepare a new attempt"
        )
    if read_json(Path(expected_paths["request_path"])) != request:
        raise SelcError(
            "ATTEMPT_TAMPERED",
            "attempt request differs from the frozen case",
            "prepare a new attempt",
        )
    installation = state.get("installation")
    if not isinstance(installation, dict):
        raise SelcError(
            "ATTEMPT_TAMPERED", "attempt installation identity is missing", "prepare a new attempt"
        )
    if (
        installation.get("suite_id") != plan["suite_id"]
        or installation.get("version") != binding["variant_version"]
        or installation.get("digest") != binding["candidate_snapshot"]["portable_tree_hash"]
    ):
        raise SelcError(
            "ATTEMPT_TAMPERED",
            "attempt installation does not match the exact bound portable snapshot",
            "prepare a new attempt",
        )
    expected_policy_binding = _prepare_policy_binding(matrix, Path(expected_paths["project_root"]))
    if state.get("policy_binding") != expected_policy_binding:
        raise SelcError(
            "ATTEMPT_TAMPERED",
            "attempt policy binding differs from its frozen template",
            "prepare a new evaluation attempt",
        )


def _reconstruct_attempt(state: dict[str, Any]) -> dict[str, Any]:
    """Rebuild proof from production managed records and rerun the checker."""

    project_root = _resolved_directory(state.get("project_root"))
    suite_root = _resolved_directory(state.get("suite_path"))
    if project_root is None or suite_root is None:
        return {"status": "unknown", "reason": "attempt-path-unavailable"}
    database_path = project_root / ".selc" / "records.sqlite3"
    if not database_path.is_file():
        return {"status": "unknown", "reason": "production-records-unavailable"}
    case = state.get("case")
    matrix = state.get("matrix")
    if not isinstance(case, dict) or not isinstance(matrix, dict):
        return {"status": "unknown", "reason": "attempt-plan-missing"}
    try:
        plan = _load_real_plan(Path(str(state["plan_path"])))
        binding = _load_binding(Path(str(state["binding_path"])), plan)
        _assert_attempt_identity(
            state,
            plan,
            binding,
            str(state["case_id"]),
            str(state["matrix_id"]),
            int(state["attempt_number"]),
        )
        installed = current_installation(project_root, str(plan["suite_id"]))
    except (KeyError, OSError, SelcError, ValueError):
        return {"status": "unknown", "reason": "attempt-identity-or-installation-invalid"}
    if (
        installed.get("digest") != state["installation"]["digest"]
        or installed.get("version") != state["installation"]["version"]
        or json_hash(installed.get("owned_hooks", {})) != state["installation"]["hooks_hash"]
    ):
        return {"status": "unknown", "reason": "installed-suite-or-hooks-changed"}
    if _qualification_support(matrix) != "supported":
        qualification_reason = str(
            matrix.get("qualification_reason", "native-attribution-unverified")
        )
        if qualification_reason == "native-attribution-unverified-for-workbuddy":
            qualification_reason = "native-attribution-unverified-workbuddy"
        return {
            "status": "unknown",
            "reason": f"qualification-{qualification_reason}",
        }
    if matrix.get("adapter") == "codex-cli":
        launch_failure = _codex_cli_launch_trust_failure(state, matrix)
        if launch_failure is not None:
            return {"status": "unknown", "reason": launch_failure}
    native_context = _read_attempt_native_context(state, matrix)
    if native_context is None:
        return {"status": "unknown", "reason": "native-attribution-or-policy-unavailable"}
    try:
        database = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
        database.row_factory = sqlite3.Row
    except sqlite3.Error:
        return {"status": "unknown", "reason": "production-records-unreadable"}
    try:
        rows = database.execute(
            """
            SELECT publications.delivery_id, publications.task_id, publications.call_id,
                   publications.artifact_digest, publications.staging_path,
                   publications.status AS publication_status, publications.action_run_id,
                   intents.state AS intent_state, intents.staged_digest,
                   actions.request_digest AS action_request_digest,
                   actions.action_id AS action_id, actions.artifact_path AS action_artifact_path,
                   actions.status AS action_status,
                   receipts.native_event_id, receipts.host, receipts.host_version,
                   receipts.status AS receipt_status, receipts.consumed_at,
                   native.session_id, native.generation_id, native.request_id,
                   native.tool_use_id, native.status AS native_status,
                   native.payload_json,
                   contexts.root_valid, contexts.unknown_change,
                   root_native.payload_json AS root_payload_json,
                   windows.status AS window_status, windows.pre_event_id, windows.post_event_id,
                   pre_native.payload_json AS pre_payload_json,
                   post_native.phase AS cutoff_phase, post_native.payload_json AS cutoff_payload_json,
                   assessments.first_result, assessments.final_result,
                   checks.result AS checker_result, checks.input_digest AS checker_input_digest,
                   checks.artifact_digest AS checker_artifact_digest,
                   checks.checker_name, checks.checker_version, checks.checker_digest,
                   checks.installation_digest, checks.installation_version,
                   tasks.is_test, tasks.status AS task_status, calls.status AS call_status
            FROM managed_publications AS publications
            JOIN managed_receipts AS receipts ON receipts.receipt_digest = publications.receipt_digest
            JOIN managed_native_events AS native ON native.event_id = receipts.native_event_id
            JOIN managed_contexts AS contexts
              ON contexts.suite_id = receipts.suite_id
              AND contexts.host = receipts.host
              AND contexts.session_id = receipts.session_id
              AND contexts.generation_id = receipts.generation_id
              AND contexts.request_id = receipts.request_id
            JOIN managed_native_events AS root_native ON root_native.event_id = contexts.root_event_id
            JOIN tasks ON tasks.task_id = publications.task_id
            JOIN calls ON calls.call_id = publications.call_id
            JOIN managed_actions AS actions ON actions.action_run_id = publications.action_run_id
            JOIN managed_publication_intents AS intents ON intents.delivery_id = publications.delivery_id
            JOIN checker_runs AS checks ON checks.delivery_id = publications.delivery_id
            JOIN managed_windows AS windows
              ON windows.delivery_id = publications.delivery_id
              AND windows.task_id = publications.task_id
              AND windows.call_id = publications.call_id
              AND windows.host = receipts.host
              AND windows.session_id = receipts.session_id
              AND windows.generation_id = receipts.generation_id
              AND windows.request_id = receipts.request_id
              AND windows.tool_use_id = receipts.tool_use_id
            JOIN managed_native_events AS pre_native ON pre_native.event_id = windows.pre_event_id
            JOIN managed_native_events AS post_native ON post_native.event_id = windows.post_event_id
            JOIN managed_assessments AS assessments ON assessments.task_id = publications.task_id
            WHERE publications.suite_id = ? AND publications.status = 'recorded'
            """,
            (load_manifest(suite_root)["suite_id"],),
        ).fetchall()
    except (sqlite3.Error, SelcError):
        return {"status": "unknown", "reason": "production-records-incomplete"}
    finally:
        database.close()
    failures = [_production_row_match_failure(row, matrix, native_context) for row in rows]
    qualifying = [row for row, failure in zip(rows, failures, strict=True) if failure is None]
    if len(qualifying) != 1:
        unique_failures = {failure for failure in failures if failure is not None}
        if len(rows) == 1 and len(unique_failures) == 1:
            return {"status": "unknown", "reason": f"production-proof-{unique_failures.pop()}"}
        return {"status": "unknown", "reason": "production-proof-not-unique"}
    row = qualifying[0]
    artifact_path = Path(str(row["action_artifact_path"]))
    input_path = Path(str(case.get("input_path", "")))
    if not artifact_path.is_file() or not input_path.is_file():
        return {"status": "unknown", "reason": "checked-artifact-or-input-unavailable"}
    try:
        artifact_bytes = artifact_path.read_bytes()
        input_bytes = input_path.read_bytes()
    except OSError:
        return {"status": "unknown", "reason": "checked-artifact-or-input-unreadable"}
    if hashlib.sha256(artifact_bytes).hexdigest() != row["artifact_digest"]:
        return {"status": "unknown", "reason": "recorded-artifact-digest-mismatch"}
    checker = case.get("oracle")
    if not isinstance(checker, dict):
        return {"status": "unknown", "reason": "frozen-oracle-missing"}
    proof_checks = (
        ("checker-result", row["checker_result"] == "pass"),
        ("checker-input", row["checker_input_digest"] == state["input_hash"]),
        ("checker-artifact", row["checker_artifact_digest"] == row["artifact_digest"]),
        (
            "checker-identity",
            row["checker_name"] == checker["name"]
            and row["checker_version"] == checker["version"]
            and row["checker_digest"] == checker["digest"],
        ),
        (
            "installation",
            row["installation_digest"] == state["installation"]["digest"]
            and row["installation_version"] == state["installation"]["version"],
        ),
        (
            "action-publication",
            row["action_status"] == "published" and row["staged_digest"] == row["artifact_digest"],
        ),
    )
    for reason, matched in proof_checks:
        if not matched:
            return {"status": "unknown", "reason": f"publication-checker-{reason}-mismatch"}
    action_failure = _action_request_match_failure(state, suite_root, row, artifact_bytes)
    if action_failure is not None:
        return {"status": "unknown", "reason": f"action-request-{action_failure}-mismatch"}
    try:
        identity = CheckerIdentity(checker["name"], checker["version"], checker["digest"])
        request = CheckRequest(
            protocol=1,
            run_id=_attempt_id(state),
            subject=SubjectIdentity(row["task_id"], row["call_id"], row["delivery_id"]),
            checker=identity,
            rule_version=checker["version"],
            required_rule_ids=tuple(checker["rules"]),
            configuration=checker["configuration"],
            input_bytes=input_bytes,
            artifact_bytes=artifact_bytes,
            resource_identity="evaluation-input",
            resource_bytes=input_bytes,
            composite_identity=str(state.get("binding_hash", "")),
            composite_bytes=str(state.get("binding_hash", "")).encode("ascii"),
        )
        checked = check_artifact(request, builtin_registry())
    except (KeyError, TypeError, ValueError):
        return {"status": "unknown", "reason": "checker-reconstruction-failed"}
    return {
        "status": checked.status,
        "checker_status": checked.status,
        "one_shot_result": row["first_result"],
        "business_result": row["final_result"],
        "task_id": row["task_id"],
        "call_id": row["call_id"],
        "delivery_id": row["delivery_id"],
        "artifact_digest": row["artifact_digest"],
        "input_digest": hashlib.sha256(input_bytes).hexdigest(),
        "matrix": {
            "host": matrix["host"],
            "host_version": matrix["host_version"],
            "surface": matrix["surface"],
            "model": matrix["model"],
        },
        "native_session_digest": native_context["cutoff_digest"],
        "native_policy": {
            "raw_hash": native_context["raw_tools_policy_hash"],
            "template_hash": native_context["tools_policy_template_hash"],
            "validated_mapping": native_context["validated_policy_mapping"],
        },
    }


def _production_row_matches(
    row: sqlite3.Row,
    matrix: dict[str, Any],
    native_context: dict[str, object],
) -> bool:
    return _production_row_match_failure(row, matrix, native_context) is None


def _production_row_match_failure(
    row: sqlite3.Row,
    matrix: dict[str, Any],
    native_context: dict[str, object],
) -> str | None:
    try:
        native_payload = json.loads(row["payload_json"])
    except (TypeError, json.JSONDecodeError):
        return "native-payload-invalid"
    if not isinstance(native_payload, dict):
        return "native-payload-invalid"
    window_failure = _native_window_failure(row, native_context)
    checks = (
        (
            "test-or-open-subject",
            row["is_test"] == 0
            and row["task_status"] == "closed"
            and row["call_status"] == "closed",
        ),
        (
            "receipt-not-consumed",
            row["receipt_status"] == "consumed" and row["consumed_at"] is not None,
        ),
        (
            "native-or-publication-not-recorded",
            row["native_status"] == "recorded"
            and row["publication_status"] == "recorded"
            and row["intent_state"] == "recorded",
        ),
        (
            "window-not-closed",
            row["window_status"] == "closed"
            and bool(row["post_event_id"])
            and row["cutoff_phase"] in {"PostToolUse", "Stop"},
        ),
        (
            "root-or-business-result-invalid",
            row["root_valid"] == 1 and row["unknown_change"] == 0 and row["final_result"] == "pass",
        ),
        (
            "host-version-mismatch",
            row["host"] == matrix["host"] and row["host_version"] == matrix["host_version"],
        ),
        ("native-producer-invalid", native_payload.get("source") == "native-collector"),
        (
            "native-matrix-attribution-mismatch",
            native_context.get("model") == matrix["model"]
            and native_context.get("host_version") == matrix["host_version"]
            and native_context.get("surface") == matrix["surface"]
            and native_context.get("tools_policy_template_hash")
            == matrix["tools_policy_template_hash"]
            and isinstance(native_context.get("raw_tools_policy_hash"), str)
            and isinstance(native_context.get("validated_policy_mapping"), dict)
            and native_context.get("session_id") == row["session_id"]
            and native_context.get("turn_id") == row["generation_id"]
            and native_context.get("request_id") == row["request_id"],
        ),
        (f"native-window-{window_failure}", window_failure is None),
        (
            "native-publication-result-mismatch",
            _native_publication_result_failure(row, native_context) is None,
        ),
        (
            "native-identity-missing",
            all(row[key] for key in ("session_id", "generation_id", "request_id", "tool_use_id")),
        ),
    )
    for reason, matched in checks:
        if not matched:
            return reason
    return None


def _read_attempt_native_context(
    state: dict[str, Any], matrix: dict[str, Any]
) -> dict[str, object] | None:
    """Read one preserved native session cutoff; never trust ledger payload declarations."""

    session_root = _resolved_directory(state.get("session_root"))
    if session_root is None:
        return None
    evidence_path = session_root / "native-evidence.json"
    if not evidence_path.is_file() or evidence_path.is_symlink():
        return None
    try:
        evidence = read_json(evidence_path)
    except SelcError:
        return None
    raw_path_value = evidence.get("raw_session_path")
    raw_hash = evidence.get("raw_session_sha256")
    cutoff = evidence.get("raw_session_cutoff_bytes")
    if (
        evidence.get("attempt_id") != _attempt_id(state)
        or not isinstance(raw_path_value, str)
        or not isinstance(raw_hash, str)
        or type(cutoff) is not int
        or cutoff < 1
        or cutoff > 8 * 1024 * 1024
    ):
        return None
    raw_path = Path(raw_path_value)
    expected_path_hash = evidence.get("raw_session_path_hash")
    expected_device = evidence.get("raw_session_device")
    expected_inode = evidence.get("raw_session_inode")
    try:
        resolved_path = raw_path.resolve(strict=True)
        resolved_path.relative_to(Path.home().resolve() / ".codex" / "sessions")
    except (OSError, ValueError):
        return None
    if (
        raw_path.is_symlink()
        or not resolved_path.is_file()
        or resolved_path.stat().st_size < cutoff
    ):
        return None
    stat = resolved_path.stat()
    path_hash = hashlib.sha256(str(resolved_path).encode("utf-8")).hexdigest()
    if (
        expected_path_hash != path_hash
        or type(expected_device) is not int
        or expected_device != stat.st_dev
        or type(expected_inode) is not int
        or expected_inode != stat.st_ino
    ):
        return None
    raw = resolved_path.read_bytes()[:cutoff]
    if hashlib.sha256(raw).hexdigest() != raw_hash:
        return None
    request_path = Path(str(state.get("request_path", "")))
    try:
        expected_prompt = str(read_json(request_path)["request"])
    except (KeyError, OSError, SelcError):
        return None
    project_root = _resolved_directory(state.get("project_root"))
    if project_root is None:
        return None
    try:
        expected_binding = _prepare_policy_binding(matrix, project_root)
    except SelcError:
        return None
    policy_binding = state.get("policy_binding")
    if policy_binding != expected_binding or expected_binding.get("status") != "bound":
        return None
    parsed = _native_session_facts(raw, expected_prompt, resolved_path, project_root)
    if parsed is None:
        return None
    model, surface, session_id, turn_id, host_version, policy, tool_call_ids = parsed
    template = matrix.get("tools_policy_template")
    if not isinstance(template, dict):
        return None
    validated_mapping = _match_codex_native_policy(policy, template, expected_binding)
    if validated_mapping is None:
        return None
    raw_policy_hash = json_hash(policy)
    if (
        model != matrix["model"]
        or surface != matrix["surface"]
        or host_version != matrix["host_version"]
    ):
        return None
    return {
        "model": model,
        "host_version": host_version,
        "surface": surface,
        "session_id": session_id,
        "turn_id": turn_id,
        "request_id": turn_id,
        "tools_policy_template_hash": matrix["tools_policy_template_hash"],
        "raw_tools_policy_hash": raw_policy_hash,
        "validated_policy_mapping": validated_mapping,
        "cutoff_digest": raw_hash,
        "cutoff_bytes": cutoff,
        "path_hash": path_hash,
        "device": stat.st_dev,
        "inode": stat.st_ino,
        "raw_path": str(resolved_path),
        "project_root": str(project_root),
        "tool_call_ids": json.dumps(sorted(tool_call_ids)),
        "raw": raw,
    }


def _native_session_facts(
    raw: bytes, expected_prompt: str, raw_path: Path, project_root: Path
) -> tuple[str, str, str, str, str, dict[str, object], set[str]] | None:
    """Parse the supported Codex JSONL serializer without recursive key searches.

    Evaluation accepts only a raw file from the native Codex session store.  A
    matching first ``session_meta``, one user message, one ``turn_context``,
    and at least one native ``exec`` tool record must all name the same turn.
    This avoids treating a hand-written nested JSON object as a session.
    """

    try:
        records = [json.loads(line) for line in raw.splitlines() if line]
    except json.JSONDecodeError:
        return None
    if not records or not all(isinstance(record, dict) for record in records):
        return None
    metadata = _codex_session_metadata(records[0], raw_path, project_root)
    if metadata is None:
        return None
    session_id, source, host_version = metadata
    matching_turns: set[str] = set()
    contexts: dict[str, dict[str, object]] = {}
    tool_records: dict[str, list[tuple[str, str]]] = {}
    for record in records:
        record_type = record.get("type")
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        if (
            record_type == "response_item"
            and payload.get("type") == "message"
            and payload.get("role") == "user"
        ):
            turn_id = _record_turn_id(payload)
            content = payload.get("content")
            if turn_id and _user_content_matches(content, expected_prompt):
                matching_turns.add(turn_id)
        elif record_type == "turn_context":
            turn_id = payload.get("turn_id")
            if isinstance(turn_id, str) and turn_id:
                contexts[turn_id] = payload
        elif record_type == "response_item" and payload.get("type") == "custom_tool_call":
            turn_id = _record_turn_id(payload)
            name = payload.get("name")
            call_id = payload.get("call_id")
            if (
                isinstance(turn_id, str)
                and isinstance(name, str)
                and name
                and isinstance(call_id, str)
                and call_id
                and "input" in payload
                and isinstance(payload.get("id"), str)
                and isinstance(payload.get("status"), str)
            ):
                tool_records.setdefault(turn_id, []).append((name, call_id))
    if len(matching_turns) != 1:
        return None
    turn_id = next(iter(matching_turns))
    context = contexts.get(turn_id)
    if (
        context is None
        or context.get("root_turn_id") != turn_id
        or context.get("cwd") != str(project_root)
    ):
        return None
    required = {
        "model",
        "permission_profile",
        "sandbox_policy",
        "approval_policy",
        "file_system_sandbox_policy",
    }
    if not required.issubset(context) or not isinstance(context["model"], str):
        return None
    matching_tools = tool_records.get(turn_id, [])
    names = {name for name, _call_id in matching_tools}
    if names != {"exec"}:
        return None
    policy = {
        "permission_profile": context["permission_profile"],
        "sandbox_policy": context["sandbox_policy"],
        "approval_policy": context["approval_policy"],
        "file_system_sandbox_policy": context["file_system_sandbox_policy"],
        "tool_names": sorted(names),
    }
    surface = "cli" if source == "exec" else "desktop" if source == "desktop" else "unknown"
    return (
        context["model"],
        surface,
        session_id,
        turn_id,
        host_version,
        policy,
        {call_id for _name, call_id in matching_tools},
    )


def _match_codex_native_policy(
    policy: dict[str, object], template: dict[str, object], binding: dict[str, object]
) -> dict[str, str] | None:
    """Validate every alpha.11 policy field and the two allowed parameters.

    Native serializers repeat filesystem entries in two policy fields.  Both
    representations must be individually unique and canonically identical
    before parameter substitution, so a duplicate or extra root cannot hide in
    one copy.
    """

    try:
        normalized_template = _normalise_codex_policy_template(
            template, str(template["host_version"])
        )
    except (KeyError, SelcError):
        return None
    if normalized_template != template or set(policy) != {
        "permission_profile",
        "sandbox_policy",
        "approval_policy",
        "file_system_sandbox_policy",
        "tool_names",
    }:
        return None
    profile = policy.get("permission_profile")
    filesystem = policy.get("file_system_sandbox_policy")
    if (
        not isinstance(profile, dict)
        or not isinstance(filesystem, dict)
        or set(profile) != {"type", "file_system", "network"}
        or profile.get("type") != "managed"
        or profile.get("network") != "restricted"
        or policy.get("sandbox_policy")
        != {
            "type": "workspace-write",
            "network_access": False,
            "exclude_tmpdir_env_var": True,
            "exclude_slash_tmp": True,
        }
        or policy.get("approval_policy") != "never"
        or policy.get("tool_names") != ["exec"]
        or set(filesystem) != {"kind", "entries"}
        or filesystem.get("kind") != "restricted"
    ):
        return None
    profile_filesystem = profile.get("file_system")
    if (
        not isinstance(profile_filesystem, dict)
        or set(profile_filesystem) != {"type", "entries"}
        or profile_filesystem.get("type") != "restricted"
    ):
        return None
    profile_entries = _normalise_native_policy_entries(profile_filesystem.get("entries"))
    filesystem_entries = _normalise_native_policy_entries(filesystem.get("entries"))
    if (
        profile_entries is None
        or filesystem_entries is None
        or profile_entries != filesystem_entries
    ):
        return None
    mapping = binding.get("mapping")
    if not isinstance(mapping, dict):
        return None
    project_root = mapping.get("attempt_project")
    account_home = mapping.get("account_home")
    arg0_parent = mapping.get("codex_arg0_parent")
    arg0_pattern = mapping.get("codex_arg0_pattern")
    if not all(
        isinstance(value, str) and value
        for value in (project_root, account_home, arg0_parent, arg0_pattern)
    ):
        return None
    if account_home != template.get("account_home"):
        return None
    entries = normalized_template["policy"]["permission_profile"]["file_system"]["entries"]
    if not isinstance(entries, list):
        return None
    remaining = list(profile_entries)
    resolved: dict[str, str] = {}
    for template_entry in entries:
        if not isinstance(template_entry, dict):
            return None
        match = _take_matching_native_policy_entry(
            remaining,
            template_entry,
            project_root,
            account_home,
            arg0_parent,
        )
        if match is None:
            return None
        parameter = template_entry["path"].get("name")
        if isinstance(parameter, str):
            previous = resolved.setdefault(parameter, match)
            if previous != match:
                return None
    if remaining or set(resolved) != {"attempt_project", "codex_arg0"}:
        return None
    return {
        "attempt_project": resolved["attempt_project"],
        "codex_arg0": resolved["codex_arg0"],
        "account_home": account_home,
        "template_hash": str(binding.get("template_hash", "")),
    }


def _normalise_native_policy_entries(value: object) -> list[dict[str, object]] | None:
    if not isinstance(value, list) or not value:
        return None
    entries: list[dict[str, object]] = []
    seen: set[str] = set()
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != {"path", "access"}:
            return None
        path = entry.get("path")
        access = entry.get("access")
        if access not in {"read", "write", "deny"} or not isinstance(path, dict):
            return None
        if path == {"type": "special", "value": {"kind": "minimal"}}:
            normalized_path: dict[str, object] = {"type": "special", "value": {"kind": "minimal"}}
        elif (
            set(path) == {"type", "path"}
            and path.get("type") == "path"
            and isinstance(path.get("path"), str)
            and _canonical_absolute_path(path["path"]) is not None
        ):
            normalized_path = {"type": "path", "path": path["path"]}
        else:
            return None
        normalized = {"path": normalized_path, "access": access}
        identity = canonical_json(normalized)
        if identity in seen:
            return None
        seen.add(identity)
        entries.append(normalized)
    return sorted(entries, key=canonical_json)


def _take_matching_native_policy_entry(
    entries: list[dict[str, object]],
    template_entry: dict[str, object],
    project_root: str,
    account_home: str,
    arg0_parent: str,
) -> str | None:
    template_path = template_entry.get("path")
    access = template_entry.get("access")
    if not isinstance(template_path, dict) or not isinstance(access, str):
        return None
    for index, entry in enumerate(entries):
        if entry.get("access") != access:
            continue
        path = entry.get("path")
        if not isinstance(path, dict):
            continue
        if template_path.get("type") == "special" and path == template_path:
            entries.pop(index)
            return "minimal"
        if template_path.get("type") == "path" and path == template_path:
            entries.pop(index)
            return str(template_path["path"])
        if template_path.get("type") != "parameter" or path.get("type") != "path":
            continue
        name = template_path.get("name")
        actual = path.get("path")
        if name == "attempt_project" and actual == project_root:
            entries.pop(index)
            return actual if isinstance(actual, str) else None
        if (
            name == "codex_arg0"
            and isinstance(actual, str)
            and _is_alpha11_arg0_path(actual, account_home, arg0_parent)
        ):
            entries.pop(index)
            return actual
    return None


def _canonical_absolute_path(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    if not path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    return value if str(path) == value else None


def _is_alpha11_arg0_path(value: str, account_home: str, expected_parent: str) -> bool:
    if _canonical_absolute_path(value) is None:
        return False
    expected = Path(account_home) / ".codex" / "tmp" / "arg0-macos"
    candidate = Path(value)
    if str(expected) != expected_parent or str(candidate.parent) != expected_parent:
        return False
    return re.fullmatch(r"codex-arg0[A-Za-z0-9]+", candidate.name) is not None


def _codex_session_metadata(
    record: dict[str, object], raw_path: Path, project_root: Path
) -> tuple[str, str, str] | None:
    if record.get("type") != "session_meta":
        return None
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    session_id = payload.get("session_id")
    record_id = payload.get("id")
    source = payload.get("source")
    version = payload.get("cli_version")
    cwd = payload.get("cwd")
    if (
        not isinstance(session_id, str)
        or not session_id
        or record_id != session_id
        or not isinstance(source, str)
        or not source
        or not isinstance(version, str)
        or not version
        or cwd != str(project_root)
        or raw_path.suffix != ".jsonl"
    ):
        return None
    return session_id, source, version


def _record_turn_id(payload: dict[str, object]) -> str | None:
    metadata = payload.get("internal_chat_message_metadata_passthrough")
    if not isinstance(metadata, dict):
        return None
    turn_id = metadata.get("turn_id")
    return turn_id if isinstance(turn_id, str) and turn_id else None


def _user_content_matches(content: object, expected_prompt: str) -> bool:
    if not isinstance(content, list) or len(content) != 1:
        return False
    entry = content[0]
    return (
        isinstance(entry, dict)
        and entry.get("type") == "input_text"
        and entry.get("text") == expected_prompt
    )


def _native_window_matches(row: sqlite3.Row, context: dict[str, object]) -> bool:
    """Recheck the native pre/cutoff prefixes against the preserved raw cutoff."""

    return _native_window_failure(row, context) is None


def _native_window_failure(row: sqlite3.Row, context: dict[str, object]) -> str | None:
    """Return the first immutable-prefix relation that is unavailable."""

    try:
        pre_payload = json.loads(row["pre_payload_json"])
        cutoff_payload = json.loads(row["cutoff_payload_json"])
        root_payload = json.loads(row["root_payload_json"])
    except (TypeError, json.JSONDecodeError):
        return "payload-invalid"
    if not all(isinstance(value, dict) for value in (pre_payload, cutoff_payload, root_payload)):
        return "payload-invalid"
    raw = context.get("raw")
    if not isinstance(raw, bytes):
        return "raw-unavailable"
    for name, payload in (
        ("pre", pre_payload),
        ("cutoff", cutoff_payload),
        ("root", root_payload),
    ):
        if payload.get("source") != "native-collector":
            return "producer"
        prefix = payload.get("prefix")
        if (
            not isinstance(prefix, dict)
            or not isinstance(prefix.get("sha256"), str)
            or type(prefix.get("length")) is not int
            or prefix.get("path_hash") != context.get("path_hash")
            or prefix.get("device") != context.get("device")
            or prefix.get("inode") != context.get("inode")
        ):
            return "prefix-identity"
        length = prefix["length"]
        if length < 0 or length > len(raw):
            return f"{name}-prefix-length"
        if hashlib.sha256(raw[:length]).hexdigest() != prefix["sha256"]:
            return f"{name}-prefix-digest"
    cutoff_prefix = cutoff_payload["prefix"]
    if cutoff_prefix.get("sha256") != context.get("cutoff_digest"):
        return "cutoff-digest"
    if cutoff_prefix.get("length") != context.get("cutoff_bytes"):
        return "cutoff-length"
    if (
        not root_payload["prefix"]["length"]
        <= pre_payload["prefix"]["length"]
        <= cutoff_prefix["length"]
    ):
        return "prefix-order"
    return None


def _native_publication_result_failure(row: sqlite3.Row, context: dict[str, object]) -> str | None:
    """Bind a hook tool ID through its native CommandExecution completion.

    Codex has independent outer ``exec`` call IDs and hook tool IDs.  The
    managed runtime already verifies the supported bridge: one current-window
    ``CommandExecution`` must carry the hook tool ID, exact command, session,
    and turn.  Qualification reuses that proof over the preserved cutoff bytes.
    """

    raw = context.get("raw")
    if not isinstance(raw, bytes):
        return "raw-unavailable"
    try:
        pre_payload = json.loads(row["pre_payload_json"])
        cutoff_payload = json.loads(row["cutoff_payload_json"])
    except (TypeError, json.JSONDecodeError):
        return "payload-invalid"
    if not isinstance(pre_payload, dict) or not isinstance(cutoff_payload, dict):
        return "payload-invalid"
    pre_prefix = pre_payload.get("prefix")
    cutoff_prefix = cutoff_payload.get("prefix")
    if not isinstance(pre_prefix, dict) or not isinstance(cutoff_prefix, dict):
        return "prefix-invalid"
    pre_length = pre_prefix.get("length")
    if type(pre_length) is not int:
        return "prefix-invalid"
    event = dict(pre_payload)
    event.update(
        {
            "host": row["host"],
            "session_id": row["session_id"],
            "generation_id": row["generation_id"],
            "request_id": row["request_id"],
            "cwd": context.get("project_root"),
            "prefix": cutoff_prefix,
            "pre_prefix_length": pre_length,
        }
    )
    if _history_contains_publication_result(event, str(row["tool_use_id"]), raw=raw):
        return None
    return "command-execution-unavailable"


def _action_request_matches(
    state: dict[str, Any], suite_root: Path, row: sqlite3.Row, artifact_bytes: bytes
) -> bool:
    """Recompute the native managed-action digest from immutable business input."""

    return _action_request_match_failure(state, suite_root, row, artifact_bytes) is None


def _action_request_match_failure(
    state: dict[str, Any], suite_root: Path, row: sqlite3.Row, artifact_bytes: bytes
) -> str | None:
    """Name the missing immutable action relation without trusting a report."""

    try:
        manifest = load_manifest(suite_root)
        actions = manifest["managed"]["actions"]
        action = actions[row["action_id"]]
        skill = action["skill"]
        request = _action_request_from_prompt(state, str(skill))
        kind = action["kind"]
    except (KeyError, TypeError, SelcError):
        return "manifest"
    if kind == "instruction-staging-v1":
        expected = hashlib.sha256(
            canonical_json(
                {
                    "instruction": hashlib.sha256(artifact_bytes).hexdigest(),
                    "checker_input": state["input_hash"],
                }
            ).encode("utf-8")
        ).hexdigest()
    elif kind == "python-script-v1":
        if not _request_uses_attempt_input(request, Path(str(state["input_path"]))):
            return "input"
        expected = hashlib.sha256(
            canonical_json({"skill": skill, "input": request}).encode("utf-8")
        ).hexdigest()
    else:
        return "kind"
    return None if expected == row["action_request_digest"] else "digest"


def _action_request_from_prompt(state: dict[str, Any], skill: str) -> dict[str, object] | None:
    try:
        prompt = read_json(Path(str(state["request_path"])))["request"]
    except (KeyError, OSError, SelcError):
        return None
    if not isinstance(prompt, str):
        return None
    encoded = prompt
    if prompt.startswith("@"):
        prefix, separator, encoded = prompt.partition(" ")
        if prefix != f"@{skill}" or not separator:
            return None
    try:
        value = json.loads(encoded)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _request_uses_attempt_input(request: dict[str, object] | None, input_path: Path) -> bool:
    if request is None or not isinstance(request.get("input"), str):
        return False
    try:
        requested_path = Path(request["input"])
        if not requested_path.is_absolute():
            requested_path = input_path.parent / requested_path
        requested_path = requested_path.resolve(strict=True)
        expected_path = input_path.resolve(strict=True)
    except OSError:
        return False
    return requested_path == expected_path


def _write_attempt_result(
    attempt_path: Path,
    state: dict[str, Any],
    outcome: str,
    *,
    elapsed_seconds: float,
    exit_code: int | None,
    stdout: bytes,
    stderr: bytes,
    cleanup_verified: bool,
) -> None:
    result = {
        "state": outcome,
        "returned_at": _utc_timestamp(),
        "elapsed_seconds": elapsed_seconds,
        "exit_code": exit_code,
        "stdout_hash": hashlib.sha256(stdout).hexdigest(),
        "stderr_hash": hashlib.sha256(stderr).hexdigest(),
        "cleanup_verified": cleanup_verified,
    }
    _write_no_clobber_json(attempt_path / "adapter-result.json", result)
    _replace_attempt_state(attempt_path / "attempt.json", state, outcome, budget_consumed=True)


def _replace_attempt_state(
    path: Path, state: dict[str, Any], next_state: str, *, budget_consumed: bool
) -> None:
    next_value = dict(state)
    next_value["state"] = next_state
    next_value["budget_consumed"] = budget_consumed
    next_value["updated_at"] = _utc_timestamp()
    _write_atomic_json(path, next_value)


def _write_no_clobber_json(path: Path, value: dict[str, Any]) -> None:
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_atomic_json(path: Path, value: dict[str, Any]) -> None:
    """Replace one local state file only after both bytes and parent directory sync."""

    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    temporary = path.with_name(f".{path.name}.tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _terminate_owned_process_group(process: subprocess.Popen[bytes]) -> bool:
    """Stop only a verified child session group and prove its absence before return."""

    if process.poll() is not None:
        return _process_group_absent(process.pid)
    try:
        if os.getpgid(process.pid) != process.pid:
            return False
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=0.5)
    except (OSError, subprocess.TimeoutExpired):
        try:
            if os.getpgid(process.pid) != process.pid:
                return False
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            return _process_group_absent(process.pid)
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        if _process_group_absent(process.pid):
            return True
        time.sleep(0.05)
    return _process_group_absent(process.pid)


def _process_group_absent(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False


def _codex_cli_prompt(request_path: Path, state: dict[str, Any]) -> bytes:
    request = read_json(request_path)
    prompt = request.get("request")
    if not isinstance(prompt, str):
        raise SelcError(
            "ATTEMPT_TAMPERED",
            "frozen evaluation request is not text",
            "prepare a new evaluation attempt",
        )
    return prompt.encode("utf-8")


def _prepare_codex_hook_trust_binding(
    state: dict[str, Any], matrix: dict[str, Any]
) -> dict[str, object]:
    """Copy normal UI-reviewed project and hook state for one isolated attempt.

    ``--ignore-user-config`` keeps arbitrary account configuration out of the
    runner. Codex stores normal project and hook review decisions in that
    configuration, so this helper extracts the one matching Git-root decision
    and five installer-owned entries after normal UI review. It never creates a
    trust value.
    """

    template = matrix.get("tools_policy_template")
    policy_binding = state.get("policy_binding")
    if (
        not isinstance(template, dict)
        or template.get("hook_trust") != _fixed_codex_hook_trust_template()
        or not isinstance(policy_binding, dict)
        or policy_binding.get("status") != "bound"
    ):
        raise SelcError(
            "HOOK_TRUST_TEMPLATE_UNAVAILABLE",
            "the frozen Codex template does not declare normal hook trust inheritance",
            "prepare a new plan with the current policy template",
        )
    account_home = _trusted_posix_account_home()
    expected_home = template.get("account_home")
    if account_home is None or expected_home != str(account_home):
        raise SelcError(
            "HOOK_TRUST_SOURCE_UNAVAILABLE",
            "the current OS account does not match the frozen policy template",
            "prepare the attempt on the account used for its policy template",
        )
    project_root = _resolved_directory(state.get("project_root"))
    if project_root is None:
        raise SelcError(
            "HOOK_TRUST_SOURCE_UNAVAILABLE",
            "the isolated evaluation project is unavailable",
            "prepare a new isolated attempt",
        )
    installation = state.get("installation")
    if not isinstance(installation, dict) or not isinstance(installation.get("suite_id"), str):
        raise SelcError(
            "HOOK_TRUST_INSTALLATION_INVALID",
            "the prepared installation identity is missing",
            "prepare a new isolated attempt",
        )
    try:
        installed = current_installation(project_root, installation["suite_id"])
    except (OSError, SelcError, ValueError) as error:
        raise SelcError(
            "HOOK_TRUST_INSTALLATION_INVALID",
            "the installed Codex hooks cannot be verified",
            "repair the isolated installation before normal hook review",
        ) from error
    if (
        installed.get("digest") != installation.get("digest")
        or installed.get("version") != installation.get("version")
        or json_hash(installed.get("owned_hooks", {})) != installation.get("hooks_hash")
    ):
        raise SelcError(
            "HOOK_TRUST_INSTALLATION_INVALID",
            "the installed suite differs from the frozen attempt snapshot",
            "prepare a new isolated attempt",
        )
    git_identity = _codex_attempt_git_identity(project_root)
    hook_source = project_root / ".codex" / "hooks.json"
    definitions, hook_source_sha256 = _codex_installed_hook_definitions(
        hook_source,
        installed.get("owned_hooks"),
    )
    config_path = account_home / ".codex" / "config.toml"
    config_bytes = _read_regular_bytes(config_path, "normal Codex hook trust configuration")
    try:
        configuration = tomllib.loads(config_bytes.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise SelcError(
            "HOOK_TRUST_SOURCE_INVALID",
            "normal Codex hook trust configuration is not valid UTF-8 TOML",
            "review the normal Codex hook trust state before launching",
        ) from error
    project_trust = _selected_codex_project_trust(
        configuration,
        git_identity,
        project_root,
    )
    states = _selected_codex_hook_trust_states(configuration, hook_source, definitions)
    return {
        "schema_version": CODEX_HOOK_TRUST_SCHEMA_VERSION,
        "mode": CODEX_HOOK_TRUST_MODE,
        "template_hash": matrix.get("tools_policy_template_hash"),
        "account_home": str(account_home),
        "config_source_path": str(config_path),
        "config_source_sha256": hashlib.sha256(config_bytes).hexdigest(),
        "canonical_cwd": str(project_root),
        "git_identity": git_identity,
        "project_trust": project_trust,
        "hook_source_path": str(hook_source.resolve()),
        "hook_source_sha256": hook_source_sha256,
        "installation_hooks_hash": installation.get("hooks_hash"),
        "definitions": definitions,
        "states": states,
    }


def _read_regular_bytes(path: Path, description: str) -> bytes:
    try:
        resolved = path.resolve(strict=True)
        if path.is_symlink() or not resolved.is_file():
            raise OSError("not a regular file")
        return resolved.read_bytes()
    except OSError as error:
        raise SelcError(
            "HOOK_TRUST_SOURCE_UNAVAILABLE",
            f"{description} is unavailable",
            "complete normal hook review before launching",
        ) from error


def _codex_attempt_git_identity(project_root: Path) -> dict[str, str]:
    """Read the one direct Git identity enclosing a Codex attempt.

    An attempt can be a directory below a normally trusted Git root.  It cannot
    introduce another repository, a worktree ``.git`` file, or alternate Git
    metadata between that root and the attempt.
    """

    canonical_cwd = project_root.resolve()
    direct_root, git_directory = _nearest_direct_git_root(canonical_cwd)
    repository_root = _git_repository_path(canonical_cwd, "--show-toplevel")
    git_dir = _git_repository_path(canonical_cwd, "--git-dir")
    git_common_dir = _git_repository_path(canonical_cwd, "--git-common-dir")
    if repository_root != direct_root or git_dir != git_directory or git_common_dir != git_dir:
        raise SelcError(
            "HOOK_TRUST_SOURCE_INVALID",
            "the isolated evaluation project has an ambiguous enclosing Git identity",
            "use normal Codex trust for the existing Git repository",
        )
    return {
        "repository_root": str(repository_root),
        "git_dir": str(git_dir),
        "git_common_dir": str(git_common_dir),
    }


def _nearest_direct_git_root(canonical_cwd: Path) -> tuple[Path, Path]:
    """Find the only direct ``.git`` directory that contains an attempt."""

    current = canonical_cwd
    direct_root: Path | None = None
    direct_git_directory: Path | None = None
    while True:
        git_entry = current / ".git"
        entry_type = _git_metadata_entry_type(git_entry)
        if entry_type is not None:
            if entry_type != "directory":
                raise SelcError(
                    "HOOK_TRUST_SOURCE_INVALID",
                    "the isolated evaluation project has non-directory Git metadata",
                    "use normal Codex trust for the existing Git repository",
                )
            if direct_root is not None:
                raise SelcError(
                    "HOOK_TRUST_SOURCE_INVALID",
                    "the isolated evaluation project is inside nested Git repositories",
                    "use one normally trusted Git repository",
                )
            direct_root = current
            direct_git_directory = git_entry.resolve(strict=True)
        if current == current.parent:
            break
        current = current.parent
    if direct_root is None or direct_git_directory is None:
        raise SelcError(
            "HOOK_TRUST_SOURCE_INVALID",
            "the isolated evaluation project is not inside a direct Git repository",
            "trust the existing Git repository through Codex's normal UI",
        )
    return direct_root, direct_git_directory


def _git_metadata_entry_type(git_entry: Path) -> str | None:
    """Classify a possible ``.git`` entry without following symlinks."""

    try:
        metadata = git_entry.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise SelcError(
            "HOOK_TRUST_SOURCE_INVALID",
            "the isolated evaluation project's Git metadata cannot be inspected",
            "use normal Codex trust for the existing Git repository",
        ) from error
    if stat.S_ISLNK(metadata.st_mode):
        return "symlink"
    if stat.S_ISDIR(metadata.st_mode):
        return "directory"
    return "other"


def _git_repository_path(project_root: Path, argument: str) -> Path:
    try:
        completed = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", argument],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SelcError(
            "HOOK_TRUST_SOURCE_INVALID",
            "the isolated evaluation project's Git identity is unavailable",
            "use normal Codex trust for the existing Git repository",
        ) from error
    value = completed.stdout.strip()
    if completed.returncode != 0 or not value or "\n" in value:
        raise SelcError(
            "HOOK_TRUST_SOURCE_INVALID",
            "the isolated evaluation project's Git identity is unavailable",
            "use normal Codex trust for the existing Git repository",
        )
    path = Path(value)
    if not path.is_absolute():
        path = project_root / path
    try:
        return path.resolve(strict=True)
    except OSError as error:
        raise SelcError(
            "HOOK_TRUST_SOURCE_INVALID",
            "the isolated evaluation project's Git identity is unavailable",
            "use normal Codex trust for the existing Git repository",
        ) from error


def _selected_codex_project_trust(
    configuration: object,
    git_identity: dict[str, str],
    canonical_cwd: Path,
) -> dict[str, str]:
    """Select one exact normal-UI project trust decision for this Git root."""

    if not isinstance(configuration, dict):
        raise SelcError(
            "HOOK_TRUST_SOURCE_INVALID",
            "normal Codex project trust configuration is not an object",
            "trust the isolated project through Codex's normal UI",
        )
    projects = configuration.get("projects")
    if not isinstance(projects, dict):
        raise SelcError(
            "HOOK_TRUST_SOURCE_INVALID",
            "normal Codex project trust is missing",
            "trust the isolated project through Codex's normal UI",
        )
    repository_root = Path(git_identity["repository_root"])
    expected_key = str(repository_root)
    selected: tuple[str, object] | None = None
    for raw_key, value in projects.items():
        if not isinstance(raw_key, str):
            continue
        configured_path = Path(raw_key)
        if not configured_path.is_absolute():
            continue
        resolved_path = configured_path.resolve()
        if resolved_path == repository_root:
            if raw_key != expected_key:
                raise SelcError(
                    "HOOK_TRUST_SOURCE_INVALID",
                    "normal Codex project trust uses an ambiguous path alias",
                    "trust the isolated project through Codex's normal UI",
                )
            if selected is not None:
                raise SelcError(
                    "HOOK_TRUST_SOURCE_INVALID",
                    "normal Codex project trust has more than one matching identity",
                    "trust the isolated project through Codex's normal UI",
                )
            selected = (raw_key, value)
            continue
        is_under_repository = _path_is_within(resolved_path, repository_root)
        is_cwd_within_entry = _path_is_within(canonical_cwd, resolved_path)
        if (
            is_under_repository
            and is_cwd_within_entry
            and isinstance(value, dict)
            and value.get("trust_level") == "untrusted"
        ):
            raise SelcError(
                "HOOK_TRUST_SOURCE_INVALID",
                "a more specific normal Codex project trust decision is untrusted",
                "resolve the project trust through Codex's normal UI",
            )
    if selected is None:
        raise SelcError(
            "HOOK_TRUST_SOURCE_INVALID",
            "normal Codex project trust is missing for this isolated Git root",
            "trust the isolated project through Codex's normal UI",
        )
    raw_key, value = selected
    if (
        not isinstance(value, dict)
        or set(value) != {"trust_level"}
        or value.get("trust_level") != "trusted"
    ):
        raise SelcError(
            "HOOK_TRUST_SOURCE_INVALID",
            "normal Codex project trust is not an unchanged trusted decision",
            "trust the isolated project through Codex's normal UI",
        )
    return {"raw_key": raw_key, "trust_level": "trusted"}


def _codex_installed_hook_definitions(
    hook_source: Path, owned_hooks: object
) -> tuple[list[dict[str, object]], str]:
    """Return the exact five installer entries and their normal state keys."""

    source_bytes = _read_regular_bytes(hook_source, "installed Codex hook definitions")
    try:
        configuration = json.loads(source_bytes)
    except json.JSONDecodeError as error:
        raise SelcError(
            "HOOK_TRUST_INSTALLATION_INVALID",
            "installed Codex hook definitions are not valid JSON",
            "repair the isolated installation before normal hook review",
        ) from error
    if not isinstance(configuration, dict) or not isinstance(configuration.get("hooks"), dict):
        raise SelcError(
            "HOOK_TRUST_INSTALLATION_INVALID",
            "installed Codex hook definitions have no hooks object",
            "repair the isolated installation before normal hook review",
        )
    if not isinstance(owned_hooks, dict):
        raise SelcError(
            "HOOK_TRUST_INSTALLATION_INVALID",
            "installed Codex hook ownership metadata is missing",
            "repair the isolated installation before normal hook review",
        )
    codex_owned = owned_hooks.get("codex")
    expected_events = {event for event, _state_event in CODEX_MANAGED_HOOK_EVENTS}
    if not isinstance(codex_owned, dict) or set(codex_owned) != expected_events:
        raise SelcError(
            "HOOK_TRUST_INSTALLATION_INVALID",
            "installed Codex hooks are not the exact managed hook set",
            "repair the isolated installation before normal hook review",
        )
    source_path = str(hook_source.resolve())
    hooks = configuration["hooks"]
    definitions: list[dict[str, object]] = []
    for event, state_event in CODEX_MANAGED_HOOK_EVENTS:
        command = codex_owned.get(event)
        entries = hooks.get(event)
        if not isinstance(command, str) or not isinstance(entries, list):
            raise SelcError(
                "HOOK_TRUST_INSTALLATION_INVALID",
                "an installed managed Codex hook is missing",
                "repair the isolated installation before normal hook review",
            )
        expected_entry: dict[str, object] = {
            "hooks": [{"type": "command", "command": command, "timeout": 5}]
        }
        if event in {"PreToolUse", "PostToolUse"}:
            expected_entry["matcher"] = "^(Bash|exec_command|shell)$"
        matches = [
            (entry_index, entry)
            for entry_index, entry in enumerate(entries)
            if entry == expected_entry
        ]
        if len(matches) != 1:
            raise SelcError(
                "HOOK_TRUST_INSTALLATION_INVALID",
                "an installed managed Codex hook definition changed or is ambiguous",
                "repair the isolated installation before normal hook review",
            )
        entry_index, entry = matches[0]
        state_key = f"{source_path}:{state_event}:{entry_index}:0"
        definitions.append(
            {
                "event": event,
                "state_event": state_event,
                "entry_index": entry_index,
                "hook_index": 0,
                "state_key": state_key,
                "definition_hash": json_hash(entry),
                "command_hash": hashlib.sha256(command.encode("utf-8")).hexdigest(),
            }
        )
    return definitions, hashlib.sha256(source_bytes).hexdigest()


def _selected_codex_hook_trust_states(
    configuration: object,
    hook_source: Path,
    definitions: list[dict[str, object]],
) -> list[dict[str, str]]:
    if not isinstance(configuration, dict):
        raise SelcError(
            "HOOK_TRUST_SOURCE_INVALID",
            "normal Codex hook trust configuration is not an object",
            "complete normal hook review before launching",
        )
    hooks = configuration.get("hooks")
    states = hooks.get("state") if isinstance(hooks, dict) else None
    if not isinstance(states, dict):
        raise SelcError(
            "HOOK_TRUST_SOURCE_INVALID",
            "normal Codex hook trust state is missing",
            "review all installed Codex hooks through the normal UI",
        )
    expected_keys = {definition["state_key"] for definition in definitions}
    if not all(isinstance(key, str) for key in expected_keys):
        raise SelcError(
            "HOOK_TRUST_SOURCE_INVALID",
            "installed Codex hook state keys are invalid",
            "repair the isolated installation before normal hook review",
        )
    source_prefix = f"{hook_source.resolve()}:"
    matching_keys = {
        key for key in states if isinstance(key, str) and key.startswith(source_prefix)
    }
    if matching_keys != expected_keys:
        raise SelcError(
            "HOOK_TRUST_SOURCE_INVALID",
            "normal Codex hook trust state is missing or has an extra entry for this attempt",
            "review exactly the installed Codex hooks through the normal UI",
        )
    selected: list[dict[str, str]] = []
    for definition in definitions:
        key = definition["state_key"]
        assert isinstance(key, str)
        value = states.get(key)
        if (
            not isinstance(value, dict)
            or set(value) != {"trusted_hash"}
            or not isinstance(value.get("trusted_hash"), str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", value["trusted_hash"]) is None
        ):
            raise SelcError(
                "HOOK_TRUST_SOURCE_INVALID",
                "normal Codex hook trust state is not an unchanged reviewed hash",
                "review the installed Codex hook through the normal UI",
            )
        selected.append({"state_key": key, "trusted_hash": value["trusted_hash"]})
    return selected


def _hook_trust_cli_entries(
    hook_trust_binding: dict[str, object], state: dict[str, Any], matrix: dict[str, Any]
) -> list[str]:
    _validate_saved_codex_hook_trust_binding(hook_trust_binding, state, matrix)
    project_trust = hook_trust_binding["project_trust"]
    states = hook_trust_binding["states"]
    assert isinstance(project_trust, dict)
    assert isinstance(states, list)
    state_values: list[str] = []
    for item in states:
        assert isinstance(item, dict)
        key = item["state_key"]
        trusted_hash = item["trusted_hash"]
        assert isinstance(key, str) and isinstance(trusted_hash, str)
        state_values.append(f"{json.dumps(key)}={{trusted_hash={json.dumps(trusted_hash)}}}")
    return [
        "-c",
        _project_trust_cli_configuration(project_trust),
        "-c",
        f"hooks.state={{{','.join(state_values)}}}",
    ]


def _project_trust_cli_configuration(project_trust: dict[str, object]) -> str:
    """Encode one selected raw project key as a complete TOML inline table."""

    project_key = project_trust["raw_key"]
    trust_level = project_trust["trust_level"]
    assert isinstance(project_key, str) and isinstance(trust_level, str)
    return f"projects={{{json.dumps(project_key)}={{trust_level={json.dumps(trust_level)}}}}}"


def _validate_saved_codex_hook_trust_binding(
    binding: object, state: dict[str, Any], matrix: dict[str, Any]
) -> None:
    """Validate a frozen trust selection without reading current user config."""

    if not isinstance(binding, dict):
        raise SelcError(
            "HOOK_TRUST_BINDING_INVALID",
            "the launch claim has no normal hook trust binding",
            "collect the attempt as unknown",
        )
    template = matrix.get("tools_policy_template")
    project_root = _resolved_directory(state.get("project_root"))
    installation = state.get("installation")
    if (
        not isinstance(template, dict)
        or project_root is None
        or not isinstance(installation, dict)
        or not isinstance(template.get("account_home"), str)
    ):
        raise SelcError(
            "HOOK_TRUST_BINDING_INVALID",
            "the frozen normal hook trust context is incomplete",
            "collect the attempt as unknown",
        )
    expected_keys = {
        "schema_version",
        "mode",
        "template_hash",
        "account_home",
        "config_source_path",
        "config_source_sha256",
        "canonical_cwd",
        "git_identity",
        "project_trust",
        "hook_source_path",
        "hook_source_sha256",
        "installation_hooks_hash",
        "definitions",
        "states",
    }
    if set(binding) != expected_keys:
        raise SelcError(
            "HOOK_TRUST_BINDING_INVALID",
            "the frozen normal hook trust binding has unsupported fields",
            "collect the attempt as unknown",
        )
    expected_config_path = str(Path(template["account_home"]) / ".codex" / "config.toml")
    expected_hook_source = str(project_root / ".codex" / "hooks.json")
    try:
        current_git_identity = _codex_attempt_git_identity(project_root)
    except SelcError as error:
        raise SelcError(
            "HOOK_TRUST_BINDING_INVALID",
            "the isolated Git identity is unavailable for qualification",
            "collect the attempt as unknown",
        ) from error
    if (
        binding.get("schema_version") != CODEX_HOOK_TRUST_SCHEMA_VERSION
        or binding.get("mode") != CODEX_HOOK_TRUST_MODE
        or binding.get("template_hash") != matrix.get("tools_policy_template_hash")
        or binding.get("account_home") != template.get("account_home")
        or binding.get("config_source_path") != expected_config_path
        or binding.get("canonical_cwd") != str(project_root)
        or binding.get("git_identity") != current_git_identity
        or binding.get("hook_source_path") != expected_hook_source
        or binding.get("installation_hooks_hash") != installation.get("hooks_hash")
        or not _is_digest(binding.get("config_source_sha256"))
        or not _is_digest(binding.get("hook_source_sha256"))
    ):
        raise SelcError(
            "HOOK_TRUST_BINDING_INVALID",
            "the frozen normal hook trust binding does not match this attempt",
            "collect the attempt as unknown",
        )
    project_trust = binding.get("project_trust")
    expected_project_key = current_git_identity["repository_root"]
    if (
        not isinstance(project_trust, dict)
        or set(project_trust) != {"raw_key", "trust_level"}
        or project_trust.get("raw_key") != expected_project_key
        or project_trust.get("trust_level") != "trusted"
    ):
        raise SelcError(
            "HOOK_TRUST_BINDING_INVALID",
            "the frozen normal Codex project trust is invalid",
            "collect the attempt as unknown",
        )
    definitions = binding.get("definitions")
    states = binding.get("states")
    if not isinstance(definitions, list) or not isinstance(states, list):
        raise SelcError(
            "HOOK_TRUST_BINDING_INVALID",
            "the frozen normal hook trust entries are invalid",
            "collect the attempt as unknown",
        )
    try:
        current_definitions, source_hash = _codex_installed_hook_definitions(
            Path(expected_hook_source),
            current_installation(project_root, str(installation["suite_id"])).get("owned_hooks"),
        )
    except (OSError, SelcError, ValueError) as error:
        raise SelcError(
            "HOOK_TRUST_BINDING_INVALID",
            "the installed hook definitions are unavailable for qualification",
            "collect the attempt as unknown",
        ) from error
    if definitions != current_definitions or binding.get("hook_source_sha256") != source_hash:
        raise SelcError(
            "HOOK_TRUST_BINDING_INVALID",
            "the installed hook definitions differ from the frozen launch binding",
            "collect the attempt as unknown",
        )
    expected_state_keys = [definition["state_key"] for definition in current_definitions]
    if len(states) != len(expected_state_keys):
        raise SelcError(
            "HOOK_TRUST_BINDING_INVALID",
            "the frozen normal hook trust state count is invalid",
            "collect the attempt as unknown",
        )
    for item, expected_key in zip(states, expected_state_keys, strict=True):
        if (
            not isinstance(item, dict)
            or set(item) != {"state_key", "trusted_hash"}
            or item.get("state_key") != expected_key
            or not isinstance(item.get("trusted_hash"), str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", item["trusted_hash"]) is None
        ):
            raise SelcError(
                "HOOK_TRUST_BINDING_INVALID",
                "the frozen normal hook trust state is invalid",
                "collect the attempt as unknown",
            )


def _codex_hook_trust_changed(
    binding: dict[str, object], state: dict[str, Any], matrix: dict[str, Any]
) -> bool:
    try:
        current = _prepare_codex_hook_trust_binding(state, matrix)
    except SelcError:
        return True
    return current != binding


def _codex_cli_launch_trust_failure(state: dict[str, Any], matrix: dict[str, Any]) -> str | None:
    """Check the frozen launch binding without rereading later account config."""

    attempt_root = _resolved_directory(state.get("attempt_path"))
    if attempt_root is None:
        return "launch-claim-unavailable"
    claim_path = attempt_root / "launch-claim.json"
    try:
        launch = read_json(claim_path)
    except (OSError, SelcError, ValueError, json.JSONDecodeError):
        return "launch-claim-unavailable"
    required = {
        "schema_version",
        "attempt_id",
        "command",
        "command_hash",
        "environment",
        "environment_hash",
        "timeout_seconds",
        "hook_trust",
        "hook_trust_hash",
        "prepared_at",
    }
    if not isinstance(launch, dict) or set(launch) != required:
        return "launch-claim-invalid"
    binding = launch.get("hook_trust")
    try:
        _validate_saved_codex_hook_trust_binding(binding, state, matrix)
    except SelcError:
        return "hook-trust-binding-invalid"
    if (
        launch.get("attempt_id") != _attempt_id(state)
        or state.get("launch_claim_hash") != json_hash(launch)
        or launch.get("hook_trust_hash") != json_hash(binding)
        or launch.get("command_hash") != json_hash(launch.get("command"))
    ):
        return "launch-claim-tampered"
    executor = matrix.get("executor")
    if not isinstance(executor, dict) or not isinstance(executor.get("binary"), str):
        return "launch-executor-invalid"
    try:
        expected_command = _fixed_codex_cli_command(
            Path(executor["binary"]),
            matrix,
            state,
            binding,
        )
    except (OSError, SelcError, ValueError):
        return "hook-trust-command-invalid"
    if launch.get("command") != expected_command:
        return "hook-trust-command-tampered"
    return None


def _fixed_codex_cli_command(
    binary: Path,
    matrix: dict[str, Any],
    state: dict[str, Any],
    hook_trust_binding: dict[str, object] | None = None,
) -> list[str]:
    """Return the only CLI argument vector accepted by the real adapter."""

    project_root = str(Path(str(state["project_root"])).resolve())
    policy_binding = state.get("policy_binding")
    template = matrix.get("tools_policy_template")
    if (
        not isinstance(policy_binding, dict)
        or policy_binding.get("status") != "bound"
        or not isinstance(template, dict)
        or policy_binding.get("template_hash") != matrix.get("tools_policy_template_hash")
    ):
        raise SelcError(
            "ATTEMPT_TAMPERED",
            "the Codex CLI policy template is not bound to this attempt",
            "prepare a new evaluation attempt",
        )
    profile = template.get("cli")
    if profile != _fixed_codex_cli_profile():
        raise SelcError(
            "ATTEMPT_TAMPERED",
            "the Codex CLI profile differs from the frozen policy template",
            "prepare a new evaluation plan",
        )
    mapping = policy_binding.get("mapping")
    configuration = policy_binding.get("cli_configuration")
    if (
        not isinstance(mapping, dict)
        or mapping.get("attempt_project") != project_root
        or not isinstance(configuration, dict)
        or policy_binding.get("cli_configuration_hash") != json_hash(configuration)
    ):
        raise SelcError(
            "ATTEMPT_TAMPERED",
            "the pre-launch Codex CLI configuration changed",
            "prepare a new evaluation attempt",
        )
    settings = profile["settings"]
    if not isinstance(settings, dict):
        raise SelcError(
            "ATTEMPT_TAMPERED", "the frozen Codex settings are invalid", "prepare a new plan"
        )
    python_read_root = _template_python_read_root(template)
    permissions = (
        'permissions.selc-evaluation.filesystem={":minimal"="read",'
        f'{json.dumps(python_read_root)}="read",'
        f'{json.dumps(project_root)}="write"}}'
    )
    command = [
        str(binary),
        "-a",
        str(profile["approval_policy"]),
        str(profile["subcommand"]),
        "--ignore-user-config",
        "--skip-git-repo-check",
        "--strict-config",
        "--json",
        "--color",
        "never",
        "-m",
        str(matrix["model"]),
        "-c",
        f"model_provider={json.dumps(settings['model_provider'])}",
        "-c",
        f"web_search={json.dumps(settings['web_search'])}",
        "--enable",
        "hooks",
    ]
    disabled_features = profile.get("disable_features")
    if disabled_features != list(CODEX_DISABLED_FEATURES):
        raise SelcError(
            "ATTEMPT_TAMPERED", "the frozen Codex feature list changed", "prepare a new plan"
        )
    for feature in disabled_features:
        command.extend(("--disable", feature))
    command.extend(
        (
            "-C",
            project_root,
            "-c",
            f"default_permissions={json.dumps(settings['default_permissions'])}",
            "-c",
            permissions,
            "-c",
            "permissions.selc-evaluation.network.enabled=false",
            "-c",
            f"project_doc_max_bytes={settings['project_doc_max_bytes']}",
            "-c",
            f"project_root_markers={json.dumps(settings['project_root_markers'])}",
            "-",
        )
    )
    if hook_trust_binding is not None:
        hook_trust_entries = _hook_trust_cli_entries(
            hook_trust_binding,
            state,
            matrix,
        )
        command[-1:-1] = hook_trust_entries
    return command


def _template_python_read_root(template: dict[str, object]) -> str:
    policy = template.get("policy")
    if not isinstance(policy, dict):
        raise SelcError(
            "ATTEMPT_TAMPERED", "the frozen Codex policy is invalid", "prepare a new plan"
        )
    profile = policy.get("permission_profile")
    if not isinstance(profile, dict):
        raise SelcError(
            "ATTEMPT_TAMPERED", "the frozen Codex profile is invalid", "prepare a new plan"
        )
    filesystem = profile.get("file_system")
    if not isinstance(filesystem, dict):
        raise SelcError(
            "ATTEMPT_TAMPERED", "the frozen Codex filesystem is invalid", "prepare a new plan"
        )
    entries = filesystem.get("entries")
    if not isinstance(entries, list):
        raise SelcError(
            "ATTEMPT_TAMPERED", "the frozen Codex roots are invalid", "prepare a new plan"
        )
    roots: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("access") != "read":
            continue
        path = entry.get("path")
        if (
            isinstance(path, dict)
            and path.get("type") == "path"
            and isinstance(path.get("path"), str)
        ):
            roots.append(path["path"])
    if len(roots) != 1 or not isinstance(roots[0], str):
        raise SelcError(
            "ATTEMPT_TAMPERED", "the frozen Python root is invalid", "prepare a new plan"
        )
    return roots[0]


def _fixed_codex_environment(attempt_path: Path, state: dict[str, Any]) -> dict[str, str]:
    """Keep Codex's native store while removing inherited run identities.

    The collector resolves its transcript under ``Path.home()/.codex/sessions``.
    A new ``HOME`` or ``CODEX_HOME`` would split the CLI from that collector and
    can also discard the user's already-authorized CLI credentials.  Attempts
    remain isolated through their project, suite, SQLite database, and durable
    launch claim; this limited environment deliberately leaves no prior native
    session identity in scope.
    """

    path_value = os.environ.get("PATH")
    if not path_value:
        raise SelcError(
            "EXECUTOR_TAMPERED", "the fixed CLI environment has no PATH", "repair the host"
        )
    environment = {
        "PATH": path_value,
        "HOME": str(Path.home()),
        "LANG": "C.UTF-8",
        "SELC_EVALUATION_ATTEMPT": _attempt_id(state),
    }
    for name in (
        "CODEX_HOME",
        "CODEX_SESSION_ID",
        "CODEX_THREAD_ID",
        "CODEX_TURN_ID",
        "CODEX_RUN_ID",
        "CODEX_TOOL_CALL_ID",
    ):
        environment.pop(name, None)
    return environment


def _runtime_hash(root: Path) -> str:
    runtime = root / "runtime"
    if not runtime.is_dir():
        raise SelcError(
            "EVALUATION_PLAN_TAMPERED", "runtime directory is missing", "prepare a new plan"
        )
    return tree_hash(runtime)


def _runtime_license_hash(root: Path) -> str:
    license_path = root / "runtime" / "SELC-LICENSE"
    if not license_path.is_file() or license_path.is_symlink():
        raise SelcError(
            "EVALUATION_PLAN_TAMPERED", "runtime MIT license is missing", "recreate the suite"
        )
    digest = file_hash(license_path)
    if digest != FRAMEWORK_MIT_LICENSE_HASH:
        raise SelcError(
            "EVALUATION_PLAN_TAMPERED",
            "runtime MIT license differs from the framework license",
            "recreate the suite from the current framework",
        )
    return digest


def _document_hash(value: dict[str, Any]) -> str:
    return json_hash(value)


def _attempt_id(state: dict[str, Any]) -> str:
    return ":".join(
        [
            str(state.get("plan_hash")),
            str(state.get("case_id")),
            str(state.get("matrix_id")),
            str(state.get("attempt_number")),
        ]
    )


def _positive_bounded_seconds(value: object, label: str) -> int:
    if type(value) is not int or value < 1 or value > MAX_ATTEMPT_SECONDS:
        raise SelcError(
            "INVALID_EVALUATION_PLAN",
            f"{label} timeout must be between 1 and {MAX_ATTEMPT_SECONDS}",
            "repair timeouts",
        )
    return value


def _positive_bounded_total_seconds(value: object) -> int:
    if type(value) is not int or value < 1 or value > MAX_TOTAL_EVALUATION_SECONDS:
        raise SelcError(
            "INVALID_EVALUATION_PLAN",
            f"total timeout must be between 1 and {MAX_TOTAL_EVALUATION_SECONDS}",
            "repair total_timeout_seconds",
        )
    return value


def _positive_bounded_integer(value: object, label: str, *, maximum: int) -> int:
    if type(value) is not int or value < 1 or value > maximum:
        raise SelcError("INVALID_EVALUATION_PLAN", f"{label} is out of range", "repair the plan")
    return value


def _safe_case_name(value: str) -> str:
    normalized = "".join(
        character if character.isalnum() or character in "-_" else "-" for character in value
    )
    return normalized[:80] or "case"


def _resolved_file(value: object) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    return path.resolve() if path.is_file() and not path.is_symlink() else None


def _resolved_directory(value: object) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    return path.resolve() if path.is_dir() and not path.is_symlink() else None


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _is_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _real_evaluation_required(message: str) -> SelcError:
    return SelcError(
        "REAL_EVALUATION_REQUIRED",
        message,
        "run and collect a complete real-harness evaluation before contribution or release",
    )
