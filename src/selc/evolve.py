"""Isolated candidate preparation, deterministic evaluation, and release planning."""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import shutil
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from .errors import SelcError
from .manifest import (
    load_framework,
    load_manifest,
    normalize_declared_skills,
    package_suite,
    portable_tree_hash,
    render_text_report,
    suite_content_hash,
    validate_declared_skills,
)
from .util import (
    exclusive_lock,
    file_hash,
    json_hash,
    read_json,
    require_relative_path,
    run_argv,
    run_argv_result,
    tree_hash,
    write_json,
)

CASES = {
    "regression": ["short plain text", "two\nlines"],
    "holdout": ["keep this independent", "another report"],
}


def prepare_candidate(suite_root: Path, output: Path) -> dict[str, Any]:
    manifest = load_manifest(suite_root)
    if output.exists():
        raise SelcError(
            "PATH_CONFLICT",
            f"candidate output exists: {output}",
            "choose an empty candidate directory",
        )
    output.mkdir(parents=True)
    candidate = output / "candidate"
    shutil.copytree(suite_root / "skills", candidate / "skills")
    shutil.copytree(suite_root / "resources", candidate / "resources")
    shutil.copytree(suite_root / "resources", output / "baseline-resources")
    baseline_hash = tree_hash(suite_root)
    declared_skills = normalize_declared_skills(manifest["skills"])
    protected = {
        "schema_version": 1,
        "suite_id": manifest["suite_id"],
        "baseline_hash": baseline_hash,
        "oracle_hash": json_hash(CASES),
        "baseline_resources_hash": tree_hash(output / "baseline-resources"),
        "declared_skills": declared_skills,
        "declared_skills_hash": json_hash(declared_skills),
        "defaults": {
            "candidate_limit": 2,
            "run_timeout_seconds": 10,
            "total_timeout_seconds": 60,
            "max_runs": 8,
            "model_calls": 0,
        },
    }
    write_json(output / "protected-oracle.json", protected)
    (output / "creator-brief.md").write_text(
        "# Candidate improvement brief\n\n"
        "This directory contains only copyable skill code/resources. It does not claim that a model edited them. "
        "Use a local creator workflow to propose changes. Do not edit `protected-oracle.json`; regression and holdout "
        "case details stay outside the candidate and holdout data must not be copied into the next modification prompt.\n",
        encoding="utf-8",
    )
    return {
        "candidate": str(candidate),
        "protected_oracle": str(output / "protected-oracle.json"),
        "baseline_hash": baseline_hash,
    }


def evaluate_candidate(
    candidate_root: Path,
    output: Path,
    *,
    max_runs: int,
    model_calls: int,
    candidate_number: int,
    candidate_limit: int,
    run_timeout_seconds: int,
    total_timeout_seconds: int,
) -> dict[str, Any]:
    if model_calls != 0:
        raise SelcError(
            "MODEL_CALLS_UNAUTHORIZED",
            "v0.1 does not start real model calls",
            "set --model-calls 0 or obtain separate authorization",
        )
    if candidate_number < 1 or candidate_limit < 1:
        raise SelcError(
            "INVALID_INPUT",
            "candidate number and candidate limit must be positive",
            "supply positive candidate budget values",
        )
    if run_timeout_seconds < 1 or total_timeout_seconds < 1:
        raise SelcError(
            "INVALID_INPUT",
            "runner timeouts must be positive seconds",
            "supply positive timeout values",
        )
    if candidate_number > candidate_limit:
        report = {
            "schema_version": 1,
            "status": "budget_exhausted",
            "budget": "candidate_limit",
            "candidate_number": candidate_number,
            "candidate_limit": candidate_limit,
            "model_calls": 0,
        }
        write_json(output, report)
        return report
    protected_path = candidate_root.parent / "protected-oracle.json"
    protected = read_json(protected_path)
    if not (candidate_root / "skills").is_dir():
        raise SelcError(
            "INVALID_CANDIDATE",
            "candidate has no editable skills directory",
            "run evolve prepare again",
        )
    declared_skills = protected.get("declared_skills")
    if not isinstance(declared_skills, list) or protected.get("declared_skills_hash") != json_hash(
        declared_skills
    ):
        raise SelcError(
            "PROTECTED_ORACLE_CHANGED",
            "candidate skill declarations are missing or changed outside the candidate",
            "prepare a fresh candidate",
        )
    validate_declared_skills(candidate_root, declared_skills)
    total_cases = sum(len(cases) for cases in CASES.values())
    required_runs = total_cases * 2
    if max_runs < required_runs:
        report = {
            "schema_version": 1,
            "status": "budget_exhausted",
            "required_runs": required_runs,
            "allowed_runs": max_runs,
            "oracle_hash": protected["oracle_hash"],
            "model_calls": 0,
        }
        write_json(output, report)
        return report
    candidate_hash = tree_hash(candidate_root)
    baseline_resources = candidate_root.parent / "baseline-resources"
    if tree_hash(baseline_resources) != protected["baseline_resources_hash"]:
        raise SelcError(
            "PROTECTED_ORACLE_CHANGED",
            "baseline resources changed outside the candidate",
            "prepare a fresh candidate",
        )
    results: list[dict[str, Any]] = []
    started_at = time.monotonic()
    for collection, cases in CASES.items():
        for text in cases:
            run_started_at = time.monotonic()
            expected = render_text_report(text, baseline_resources)
            baseline = render_text_report(text, baseline_resources)
            try:
                candidate = render_text_report(text, candidate_root / "resources")
            except SelcError:
                candidate = ""
            elapsed_run = time.monotonic() - run_started_at
            elapsed_total = time.monotonic() - started_at
            if elapsed_run > run_timeout_seconds or elapsed_total > total_timeout_seconds:
                report = {
                    "schema_version": 1,
                    "status": "timeout",
                    "candidate_hash": candidate_hash,
                    "oracle_hash": protected["oracle_hash"],
                    "run_timeout_seconds": run_timeout_seconds,
                    "total_timeout_seconds": total_timeout_seconds,
                    "elapsed_run_seconds": elapsed_run,
                    "elapsed_total_seconds": elapsed_total,
                    "completed_cases": len(results),
                    "model_calls": 0,
                }
                write_json(output, report)
                return report
            results.append(
                {
                    "collection": collection,
                    "input_hash": json_hash(text),
                    "baseline_pass": baseline == expected,
                    "candidate_pass": candidate == expected,
                }
            )
    report = {
        "schema_version": 1,
        "status": "passed" if all(item["candidate_pass"] for item in results) else "failed",
        "baseline_hash": protected["baseline_hash"],
        "candidate_hash": candidate_hash,
        "declared_skills_hash": protected["declared_skills_hash"],
        "oracle_hash": protected["oracle_hash"],
        "runs": len(results) * 2,
        "candidate_number": candidate_number,
        "candidate_limit": candidate_limit,
        "run_timeout_seconds": run_timeout_seconds,
        "total_timeout_seconds": total_timeout_seconds,
        "elapsed_seconds": time.monotonic() - started_at,
        "model_calls": 0,
        "token_cost": None,
        "money_cost": None,
        "results": results,
    }
    write_json(output, report)
    return report


def prepare_contribution(
    candidate_root: Path,
    suite_root: Path,
    evaluation_path: Path,
    source_repository: Path | None,
    workspace: Path,
    output: Path,
    project_root: Path | None = None,
    suite_id: str | None = None,
) -> dict[str, Any]:
    if workspace.exists():
        raise SelcError(
            "PATH_CONFLICT",
            f"contribution workspace already exists: {workspace}",
            "choose an empty workspace path",
        )
    manifest = load_manifest(suite_root)
    (
        source_location,
        push_upstream,
        upstream_identity,
        source_commit,
        suite_subdir,
        base_branch,
    ) = _contribution_baseline(
        suite_root,
        manifest,
        source_repository,
        project_root,
        suite_id,
    )
    evaluation = read_json(evaluation_path)
    if evaluation.get("status") != "passed":
        raise SelcError(
            "EVALUATION_REQUIRED",
            "a passed evaluation is required before contribution preparation",
            "evaluate the candidate before preparing a contribution",
        )
    _validate_candidate_structure(candidate_root, evaluation)
    candidate_hash = tree_hash(candidate_root)
    if evaluation.get("candidate_hash") != candidate_hash:
        raise SelcError(
            "EVALUATION_STALE",
            "candidate changed after evaluation",
            "evaluate the current candidate again",
        )
    branch = f"codex/contribution-{candidate_hash[:12]}"
    run_argv(["git", "clone", "--no-checkout", source_location, str(workspace)])
    run_argv(["git", "checkout", "--detach", source_commit], cwd=workspace)
    run_argv(["git", "checkout", "-b", branch], cwd=workspace)
    workspace_suite = workspace / suite_subdir
    load_manifest(workspace_suite)
    diff = _business_diff(workspace_suite, candidate_root)
    if not diff:
        raise SelcError(
            "NO_CONTRIBUTION_CHANGES",
            "candidate does not change business code or resources",
            "edit the candidate before preparing a contribution",
        )
    _apply_candidate_business(candidate_root, workspace_suite)
    _refresh_manifest_hash(workspace_suite)
    prepared_suite_hash = portable_tree_hash(workspace_suite)
    prepared_business_hash = _business_content_hash(workspace_suite)
    prepared_manifest_hash = file_hash(workspace_suite / "suite.json")
    prepared_runtime_hash = tree_hash(workspace_suite / "runtime")
    prepared_wrapper_hash = file_hash(workspace_suite / "bin" / "selc-suite.py")
    changed_files = _changed_business_files(candidate_root)
    diff_path = output.with_suffix(".diff")
    diff_path.write_text(diff, encoding="utf-8")
    package_path = output.with_suffix(".zip")
    package = package_suite(workspace_suite, package_path)
    plan = {
        "schema_version": 1,
        "source_repository": source_location,
        "push_upstream": push_upstream,
        "upstream_identity": upstream_identity,
        "source_commit": source_commit,
        "suite_subdir": suite_subdir,
        "suite_id": manifest["suite_id"],
        "base_branch": base_branch,
        "branch": branch,
        "candidate": str(candidate_root.resolve()),
        "candidate_hash": candidate_hash,
        "evaluation": str(evaluation_path.resolve()),
        "evaluation_hash": file_hash(evaluation_path),
        "workspace": str(workspace.resolve()),
        "workspace_base_head": source_commit,
        "workspace_suite_hash": prepared_suite_hash,
        "workspace_business_hash": prepared_business_hash,
        "workspace_manifest_hash": prepared_manifest_hash,
        "workspace_runtime_hash": prepared_runtime_hash,
        "workspace_wrapper_hash": prepared_wrapper_hash,
        "changed_files": changed_files,
        "diff": str(diff_path.resolve()),
        "diff_hash": file_hash(diff_path),
        "package": str(package_path.resolve()),
        "package_hash": file_hash(package_path),
        "package_summary": package,
        "summary": "Prepared an isolated contribution workspace and non-default branch; execute is still required to commit, push, and open a PR or MR.",
    }
    plan["plan_hash"] = contribution_plan_hash(plan)
    write_json(output, plan)
    return plan


def _validate_candidate_structure(candidate_root: Path, binding: dict[str, Any]) -> None:
    """Recheck candidate skill entries against the protected evaluated mapping."""

    protected = read_json(candidate_root.parent / "protected-oracle.json")
    declared_skills = protected.get("declared_skills")
    declared_hash = protected.get("declared_skills_hash")
    if not isinstance(declared_skills, list) or not isinstance(declared_hash, str):
        raise SelcError(
            "INVALID_CANDIDATE",
            "candidate has no protected declared skill mapping",
            "prepare a fresh candidate",
        )
    if (
        declared_hash != json_hash(declared_skills)
        or binding.get("declared_skills_hash") != declared_hash
    ):
        raise SelcError(
            "EVALUATION_STALE",
            "candidate skill declaration binding changed after evaluation",
            "prepare and evaluate a fresh candidate",
        )
    validate_declared_skills(candidate_root, declared_skills)


def _contribution_baseline(
    suite_root: Path,
    manifest: dict[str, Any],
    source_repository: Path | None,
    project_root: Path | None,
    suite_id: str | None,
) -> tuple[str, str, dict[str, str] | None, str, str, str]:
    """Resolve an exact Git baseline from a checkout or an installed suite's provenance."""

    if source_repository is not None:
        repository_root = _find_git_root(suite_root)
        if repository_root is None or repository_root.resolve() != source_repository.resolve():
            raise SelcError(
                "INVALID_SOURCE",
                "contribution suite must be inside the specified source repository",
                "omit --source-repo and use saved installed provenance, or select the owning checkout",
            )
        commit = run_argv(["git", "rev-parse", "HEAD"], cwd=source_repository)
        base_branch = run_argv(["git", "branch", "--show-current"], cwd=source_repository)
        if not base_branch:
            raise SelcError(
                "INVALID_SOURCE",
                "source repository must have a named base branch",
                "check out the source baseline branch before preparing a contribution",
            )
        suite_subdir = suite_root.resolve().relative_to(source_repository.resolve()).as_posix()
        clone_source = str(source_repository.resolve())
        push_upstream = _configured_upstream(source_repository)
        return (
            clone_source,
            push_upstream,
            _hosted_repository_identity(push_upstream),
            commit,
            suite_subdir,
            base_branch,
        )

    if project_root is None or not suite_id:
        raise SelcError(
            "MISSING_SOURCE_BASELINE",
            "installed-suite contribution needs --project-root and --suite-id when --source-repo is absent",
            "supply the installed suite identity or an explicit source checkout",
        )
    from .install import current_installation

    installation = current_installation(project_root, suite_id)
    source = installation.get("source", {})
    if source.get("kind") != "git" or not source.get("repo") or not source.get("commit"):
        raise SelcError(
            "MISSING_SOURCE_BASELINE",
            "the installed package has no reusable Git source provenance",
            "install from a provenance-carrying package or provide --source-repo",
        )
    if installation.get("suite_id") != manifest["suite_id"]:
        raise SelcError(
            "INVALID_SOURCE",
            "installed suite identity does not match the contribution suite",
            "select the matching --suite-id",
        )
    baseline = str(source.get("contribution_base") or source["base"] or "")
    if not baseline or baseline.startswith("v"):
        raise SelcError(
            "MISSING_SOURCE_BASELINE",
            "installed provenance has no named contribution base branch",
            "provide --source-repo checked out on the intended base branch",
        )
    clone_source = str(source["repo"])
    return (
        clone_source,
        clone_source,
        _hosted_repository_identity(clone_source),
        str(source["commit"]),
        str(source.get("subdir") or "."),
        baseline,
    )


def _configured_upstream(repository_root: Path) -> str:
    """Return the configured push center without replacing the local clone baseline."""

    code, output, _ = run_argv_result(
        ["git", "config", "--get", "remote.origin.url"], cwd=repository_root
    )
    if code == 0 and output:
        return output
    return str(repository_root.resolve())


def _hosted_repository_identity(upstream: str) -> dict[str, str] | None:
    """Extract the host and namespace from a hosted Git URL.

    Local paths intentionally have no provider identity.  They remain usable for
    the local transport, while a GitHub or GitLab request must name a source with
    a hosted upstream before it can commit or push.
    """

    host = ""
    path = ""
    if upstream.startswith(("http://", "https://", "ssh://")):
        parsed = urlparse(upstream)
        host = parsed.hostname or ""
        path = parsed.path
    elif upstream.startswith("git@") and ":" in upstream:
        host, path = upstream.removeprefix("git@").split(":", maxsplit=1)
    if not host:
        return None
    namespace = path.strip("/")
    if namespace.endswith(".git"):
        namespace = namespace.removesuffix(".git")
    parts = [part for part in namespace.split("/") if part]
    if len(parts) < 2:
        return None
    identity = {"host": host, "repository": "/".join(parts), "url": upstream}
    if host == "github.com":
        identity["provider"] = "github"
    elif host == "gitlab.com":
        identity["provider"] = "gitlab"
    return identity


def _validate_remote_contribution_destination(
    plan: dict[str, Any], provider: str, repository: str | None
) -> tuple[str, dict[str, str], str]:
    """Bind a hosted provider request to the exact upstream fixed during prepare."""

    if provider not in {"github", "gitlab"} or not repository:
        raise SelcError(
            "INVALID_INPUT",
            "remote contribution execution needs provider and repository",
            "supply github or gitlab with the repository fixed by the source upstream",
        )
    push_upstream = plan.get("push_upstream")
    identity = plan.get("upstream_identity")
    if not isinstance(push_upstream, str) or not isinstance(identity, dict):
        raise SelcError(
            "MISSING_HOSTED_UPSTREAM",
            "the prepared source has no hosted upstream for a PR or MR",
            "configure origin to the intended hosted center and prepare a new contribution",
        )
    expected_repository = identity.get("repository")
    expected_url = identity.get("url")
    expected_provider = identity.get("provider")
    expected_host = identity.get("host")
    if not isinstance(expected_repository, str) or expected_url != push_upstream:
        raise SelcError(
            "PLAN_TAMPERED",
            "contribution upstream identity no longer matches its push target",
            "prepare a new contribution workspace from the intended source",
        )
    if repository != expected_repository:
        raise SelcError(
            "UPSTREAM_IDENTITY_MISMATCH",
            "provider repository differs from the prepared hosted upstream",
            "use the repository recorded in the contribution plan or prepare a new plan",
        )
    if expected_provider not in {"github", "gitlab"} or not isinstance(expected_host, str):
        raise SelcError(
            "UNSUPPORTED_HOSTED_PROVIDER",
            "remote contribution supports only upstreams hosted at github.com or gitlab.com",
            "use local transport or prepare from a public GitHub or GitLab upstream",
        )
    if provider != expected_provider:
        raise SelcError(
            "UPSTREAM_IDENTITY_MISMATCH",
            "provider does not match the prepared hosted upstream",
            "use the provider for the recorded upstream or prepare a new plan",
        )
    provider_repository = f"{expected_host}/{expected_repository}"
    normalized_identity = {str(key): str(value) for key, value in identity.items()}
    return push_upstream, normalized_identity, provider_repository


def contribution_plan_hash(plan: dict[str, Any]) -> str:
    value = dict(plan)
    value.pop("plan_hash", None)
    return json_hash(value)


def execute_contribution(
    plan_path: Path,
    provider: str,
    repository: str | None,
    client: str | None,
) -> dict[str, Any]:
    plan = read_json(plan_path)
    if plan.get("plan_hash") != contribution_plan_hash(plan):
        raise SelcError(
            "PLAN_TAMPERED",
            "contribution plan fields changed after preparation",
            "prepare a new contribution workspace",
        )
    candidate_root = Path(plan["candidate"])
    evaluation_path = Path(plan["evaluation"])
    diff_path = Path(plan["diff"])
    package_path = Path(plan["package"])
    if tree_hash(candidate_root) != plan["candidate_hash"]:
        raise SelcError(
            "CONTRIBUTION_STALE",
            "candidate changed after contribution preparation",
            "prepare a new contribution workspace",
        )
    if not evaluation_path.is_file() or file_hash(evaluation_path) != plan["evaluation_hash"]:
        raise SelcError(
            "CONTRIBUTION_STALE",
            "evaluation changed after contribution preparation",
            "prepare a new contribution workspace",
        )
    evaluation = read_json(evaluation_path)
    if (
        evaluation.get("status") != "passed"
        or evaluation.get("candidate_hash") != plan["candidate_hash"]
    ):
        raise SelcError(
            "CONTRIBUTION_STALE",
            "contribution no longer has a passed evaluation for its candidate",
            "evaluate the candidate and prepare a new contribution workspace",
        )
    _validate_candidate_structure(candidate_root, evaluation)
    if not diff_path.is_file() or file_hash(diff_path) != plan["diff_hash"]:
        raise SelcError(
            "CONTRIBUTION_STALE",
            "contribution diff changed after preparation",
            "prepare a new contribution workspace",
        )
    if not package_path.is_file() or file_hash(package_path) != plan["package_hash"]:
        raise SelcError(
            "CONTRIBUTION_STALE",
            "contribution package changed after preparation",
            "prepare a new contribution workspace",
        )
    workspace = Path(plan["workspace"])
    branch = run_argv(["git", "branch", "--show-current"], cwd=workspace)
    if branch != plan["branch"]:
        raise SelcError(
            "CONTRIBUTION_STALE",
            "contribution workspace is not on the prepared branch",
            "restore the prepared workspace or prepare a new contribution",
        )
    push_upstream = plan.get("push_upstream")
    if not isinstance(push_upstream, str) or not push_upstream:
        raise SelcError(
            "PLAN_TAMPERED",
            "contribution plan has no fixed push target",
            "prepare a new contribution workspace",
        )
    upstream_identity: dict[str, str] | None = None
    provider_repository: str | None = None
    if provider != "local":
        if not client:
            raise SelcError(
                "INVALID_INPUT",
                "remote contribution execution needs provider, repository, and client",
                "supply a controlled GitHub or GitLab client, or choose local",
            )
        push_upstream, upstream_identity, provider_repository = (
            _validate_remote_contribution_destination(plan, provider, repository)
        )
    contribution_commit = _prepare_or_reuse_contribution_commit(workspace, plan)
    run_argv(
        ["git", "push", push_upstream, f"{contribution_commit}:refs/heads/{plan['branch']}"],
        cwd=workspace,
    )
    _assert_remote_branch_points_to_commit(push_upstream, str(plan["branch"]), contribution_commit)
    result: dict[str, Any] = {
        "workspace": str(workspace),
        "branch": plan["branch"],
        "commit": contribution_commit,
        "pushed": True,
        "push_upstream": push_upstream,
    }
    if provider == "local":
        result["provider"] = "local"
        return result
    assert repository is not None
    assert client is not None
    assert upstream_identity is not None
    assert provider_repository is not None
    remote_query = contribution_query_argv(
        provider,
        client,
        provider_repository,
        str(plan["branch"]),
        str(plan["base_branch"]),
    )
    remote_query_output = run_argv(remote_query)
    request_state = _parse_contribution_request(
        provider,
        remote_query_output,
        provider_repository,
        str(plan["branch"]),
        str(plan["base_branch"]),
        contribution_commit,
    )
    if request_state == "matching":
        result.update(
            {
                "provider": provider,
                "repository": repository,
                "provider_repository": provider_repository,
                "upstream_identity": upstream_identity,
                "request": "reused",
                "query": remote_query,
                "query_output": remote_query_output,
            }
        )
        return result
    note_path = workspace / "SELC_CONTRIBUTION.md"
    note_path.write_text(_contribution_note(plan), encoding="utf-8")
    create = contribution_create_argv(
        provider,
        client,
        provider_repository,
        plan["branch"],
        plan["base_branch"],
        note_path,
    )
    try:
        create_output = run_argv(create)
    except SelcError as error:
        follow_up = run_argv(remote_query)
        if (
            _parse_contribution_request(
                provider,
                follow_up,
                provider_repository,
                str(plan["branch"]),
                str(plan["base_branch"]),
                contribution_commit,
            )
            == "matching"
        ):
            result.update(
                {
                    "provider": provider,
                    "repository": repository,
                    "provider_repository": provider_repository,
                    "upstream_identity": upstream_identity,
                    "request": "recovered_after_ambiguous_create",
                    "query": remote_query,
                    "query_output": follow_up,
                }
            )
            return result
        raise error
    verification_output = run_argv(remote_query)
    verification_state = _parse_contribution_request(
        provider,
        verification_output,
        provider_repository,
        str(plan["branch"]),
        str(plan["base_branch"]),
        contribution_commit,
    )
    if verification_state != "matching":
        raise SelcError(
            "REQUEST_CREATE_UNVERIFIED",
            "provider did not return the created contribution request",
            "query the fixed repository before retrying",
        )
    result.update(
        {
            "provider": provider,
            "repository": repository,
            "provider_repository": provider_repository,
            "upstream_identity": upstream_identity,
            "query": remote_query,
            "query_output": verification_output,
            "create": create,
            "create_output": create_output,
        }
    )
    return result


def verify_contribution(plan_path: Path, provider: str, client: str) -> dict[str, Any]:
    """Query the exact hosted contribution center fixed by a prepared plan."""

    plan = read_json(plan_path)
    if plan.get("plan_hash") != contribution_plan_hash(plan):
        raise SelcError(
            "PLAN_TAMPERED",
            "contribution plan fields changed after preparation",
            "prepare a new contribution workspace",
        )
    identity = plan.get("upstream_identity")
    repository = identity.get("repository") if isinstance(identity, dict) else None
    push_upstream, _identity, provider_repository = _validate_remote_contribution_destination(
        plan, provider, repository
    )
    branch = plan.get("branch")
    if not isinstance(branch, str) or not branch:
        raise SelcError(
            "PLAN_TAMPERED",
            "contribution plan has no fixed branch",
            "prepare a new contribution workspace",
        )
    workspace = Path(str(plan["workspace"]))
    commit = run_argv(["git", "rev-parse", "HEAD"], cwd=workspace)
    baseline = str(plan["workspace_base_head"])
    parents = run_argv(["git", "rev-list", "--parents", "-n", "1", commit], cwd=workspace).split()
    if commit == baseline or len(parents) != 2 or parents[1] != baseline:
        raise SelcError(
            "CONTRIBUTION_UNVERIFIED",
            "contribution workspace has no verified single contribution commit",
            "execute the prepared contribution before verification",
        )
    _assert_committed_workspace_matches_plan(workspace, commit, plan)
    _assert_workspace_matches_plan(workspace, plan)
    _assert_remote_branch_points_to_commit(push_upstream, branch, commit)
    argv = contribution_query_argv(
        provider, client, provider_repository, branch, str(plan["base_branch"])
    )
    output = run_argv(argv)
    request_state = _parse_contribution_request(
        provider,
        output,
        provider_repository,
        branch,
        str(plan["base_branch"]),
        commit,
    )
    if request_state != "matching":
        raise SelcError(
            "CONTRIBUTION_UNVERIFIED",
            "no matching contribution request exists at the fixed upstream",
            "create or recover the request before verification",
        )
    return {
        "provider": provider,
        "repository": repository,
        "provider_repository": provider_repository,
        "branch": branch,
        "commit": commit,
        "output": output,
    }


def contribution_query_argv(
    provider: str, client: str, repository: str, branch: str, base_branch: str
) -> list[str]:
    if provider == "github":
        return [
            client,
            "pr",
            "list",
            "--repo",
            repository,
            "--head",
            branch,
            "--base",
            base_branch,
            "--state",
            "all",
            "--json",
            "number,state,headRefName,baseRefName,headRepository,headRepositoryOwner,headRefOid",
        ]
    if provider == "gitlab":
        host, namespace = repository.split("/", maxsplit=1)
        encoded_namespace = quote(namespace, safe="")
        return [
            client,
            "api",
            "--hostname",
            host,
            f"projects/{encoded_namespace}/merge_requests"
            f"?source_branch={quote(branch, safe='')}&target_branch={quote(base_branch, safe='')}"
            "&state=all",
        ]
    raise SelcError(
        "UNSUPPORTED_PROVIDER", f"unsupported provider: {provider}", "choose github or gitlab"
    )


def contribution_create_argv(
    provider: str,
    client: str,
    repository: str,
    branch: str,
    base_branch: str,
    note_path: Path,
) -> list[str]:
    title = "SELC contribution"
    if provider == "github":
        return [
            client,
            "pr",
            "create",
            "--repo",
            repository,
            "--head",
            branch,
            "--base",
            base_branch,
            "--title",
            title,
            "--body-file",
            str(note_path),
        ]
    if provider == "gitlab":
        return [
            client,
            "mr",
            "create",
            "--repo",
            repository,
            "--source-branch",
            branch,
            "--target-branch",
            base_branch,
            "--title",
            title,
            "--description",
            note_path.read_text(encoding="utf-8"),
        ]
    raise SelcError(
        "UNSUPPORTED_PROVIDER", f"unsupported provider: {provider}", "choose github or gitlab"
    )


def _apply_candidate_business(candidate_root: Path, suite_root: Path) -> None:
    for name in ("skills", "resources"):
        source = candidate_root / name
        if not source.is_dir():
            raise SelcError(
                "INVALID_CANDIDATE",
                f"candidate has no {name} directory",
                "prepare a complete candidate before contribution",
            )
        for path in source.rglob("*"):
            if path.is_symlink():
                raise SelcError(
                    "UNSAFE_SYMLINK",
                    f"candidate business file cannot be a symlink: {path.relative_to(candidate_root)}",
                    "copy the business file into the candidate",
                )
        destination = suite_root / name
        shutil.rmtree(destination, ignore_errors=True)
        shutil.copytree(source, destination)


def _refresh_manifest_hash(suite_root: Path) -> None:
    manifest = read_json(suite_root / "suite.json")
    manifest["content_hash"] = suite_content_hash(suite_root)
    write_json(suite_root / "suite.json", manifest)


def _changed_business_files(candidate_root: Path) -> list[str]:
    return sorted(
        path.relative_to(candidate_root).as_posix()
        for path in candidate_root.rglob("*")
        if path.is_file()
    )


def _business_diff(baseline_root: Path, candidate_root: Path) -> str:
    files = set(_changed_business_files(candidate_root))
    for section in ("skills", "resources"):
        baseline_section = baseline_root / section
        if baseline_section.is_dir():
            files.update(
                path.relative_to(baseline_root).as_posix()
                for path in baseline_section.rglob("*")
                if path.is_file()
            )
    lines: list[str] = []
    for relative in sorted(files):
        before = baseline_root / relative
        after = candidate_root / relative
        before_text = (
            before.read_text(encoding="utf-8").splitlines(keepends=True) if before.exists() else []
        )
        after_text = (
            after.read_text(encoding="utf-8").splitlines(keepends=True) if after.exists() else []
        )
        lines.extend(
            difflib.unified_diff(
                before_text, after_text, fromfile=f"a/{relative}", tofile=f"b/{relative}"
            )
        )
    return "".join(lines)


def _assert_workspace_changes_are_safe(workspace: Path, plan: dict[str, Any]) -> None:
    """Reject uncommitted index and worktree changes outside the prepared surface."""

    staged = run_argv(
        ["git", "diff", "--cached", "--name-only", "--no-renames"], cwd=workspace
    ).splitlines()
    unstaged = run_argv(["git", "diff", "--name-only", "--no-renames"], cwd=workspace).splitlines()
    untracked = run_argv(
        ["git", "ls-files", "--others", "--exclude-standard"], cwd=workspace
    ).splitlines()
    suite_subdir = str(plan["suite_subdir"])
    for name in sorted({*staged, *unstaged}):
        _assert_controlled_contribution_path(Path(name), suite_subdir)
    for name in sorted(untracked):
        if name == "SELC_CONTRIBUTION.md" and _is_expected_contribution_note(workspace, plan):
            continue
        _assert_controlled_contribution_path(Path(name), suite_subdir)


def _assert_controlled_contribution_path(path: Path, suite_subdir: str) -> None:
    if _is_controlled_contribution_path(path, suite_subdir):
        return
    raise SelcError(
        "UNSAFE_CONTRIBUTION",
        f"prepared workspace contains an unexpected change: {path.as_posix()}",
        "recreate the contribution workspace",
    )


def _assert_contribution_commit_paths_are_safe(
    workspace: Path, contribution_commit: str, plan: dict[str, Any]
) -> None:
    """Check the actual baseline-to-commit tree, not only the mutable worktree."""

    baseline = str(plan["workspace_base_head"])
    changed_paths = run_argv(
        [
            "git",
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "--no-renames",
            "-r",
            baseline,
            contribution_commit,
        ],
        cwd=workspace,
    ).splitlines()
    suite_subdir = str(plan["suite_subdir"])
    for name in sorted(changed_paths):
        _assert_controlled_contribution_path(Path(name), suite_subdir)


def _contribution_note(plan: dict[str, Any]) -> str:
    """Return the sole tool-owned untracked note allowed during request recovery."""

    return (
        f"# SELC contribution\n\n"
        f"Plan: {plan['plan_hash']}\n"
        f"Suite: {plan['suite_id']}\n"
        f"Candidate: {plan['candidate_hash']}\n"
    )


def _is_expected_contribution_note(workspace: Path, plan: dict[str, Any]) -> bool:
    note = workspace / "SELC_CONTRIBUTION.md"
    try:
        return note.is_file() and note.read_text(encoding="utf-8") == _contribution_note(plan)
    except OSError:
        return False


def _prepare_or_reuse_contribution_commit(workspace: Path, plan: dict[str, Any]) -> str:
    """Commit once, or verify the one prior commit after an interrupted retry."""

    head = run_argv(["git", "rev-parse", "HEAD"], cwd=workspace)
    baseline = str(plan["workspace_base_head"])
    if head == baseline:
        _assert_workspace_matches_plan(workspace, plan)
        _assert_workspace_changes_are_safe(workspace, plan)
        author_name = run_argv(["git", "config", "user.name"], cwd=workspace)
        author_email = run_argv(["git", "config", "user.email"], cwd=workspace)
        run_argv(["git", "config", "user.name", author_name], cwd=workspace)
        run_argv(["git", "config", "user.email", author_email], cwd=workspace)
        suite_subdir = str(plan["suite_subdir"])
        suite_path = Path(suite_subdir)
        staged_paths = [
            str(suite_path / "skills"),
            str(suite_path / "resources"),
            str(suite_path / "suite.json"),
        ]
        if suite_subdir == ".":
            staged_paths = ["skills", "resources", "suite.json"]
        run_argv(["git", "add", "--", *staged_paths], cwd=workspace)
        run_argv(
            ["git", "commit", "-m", f"SELC contribution for {plan['suite_id']}"], cwd=workspace
        )
        contribution_commit = run_argv(["git", "rev-parse", "HEAD"], cwd=workspace)
        _assert_committed_workspace_matches_plan(workspace, contribution_commit, plan)
        return contribution_commit

    parents = run_argv(["git", "rev-list", "--parents", "-n", "1", head], cwd=workspace).split()
    if len(parents) != 2 or parents[1] != baseline:
        raise SelcError(
            "CONTRIBUTION_STALE",
            "contribution workspace has commits outside the prepared baseline and contribution",
            "recreate the contribution workspace from the saved source baseline",
        )
    _assert_committed_workspace_matches_plan(workspace, head, plan)
    _assert_workspace_matches_plan(workspace, plan)
    _assert_workspace_changes_are_safe(workspace, plan)
    return head


def _assert_remote_branch_points_to_commit(
    push_upstream: str, branch: str, contribution_commit: str
) -> None:
    output = run_argv(["git", "ls-remote", push_upstream, f"refs/heads/{branch}"])
    fields = output.split()
    if len(fields) < 2 or fields[0] != contribution_commit:
        raise SelcError(
            "CONTRIBUTION_PUSH_UNVERIFIED",
            "contribution branch does not point to the verified contribution commit",
            "inspect the fixed upstream branch before retrying",
        )


def _parse_contribution_request(
    provider: str,
    output: str,
    repository: str,
    branch: str,
    base_branch: str,
    commit: str,
) -> str:
    """Accept only a structured request bound to the prepared head and base."""

    if output.strip() == "[]":
        return "absent"
    try:
        parsed = json.loads(output)
    except json.JSONDecodeError as error:
        raise SelcError(
            "REQUEST_QUERY_AMBIGUOUS",
            "provider request query did not return structured JSON",
            "inspect the fixed repository before retrying",
        ) from error
    if not isinstance(parsed, list):
        raise SelcError(
            "REQUEST_QUERY_AMBIGUOUS",
            "provider request query has an unsupported JSON shape",
            "inspect the fixed repository before retrying",
        )
    if not parsed:
        return "absent"
    if not all(isinstance(item, dict) for item in parsed):
        raise SelcError(
            "REQUEST_QUERY_AMBIGUOUS",
            "provider request query has an unsupported JSON shape",
            "inspect the fixed repository before retrying",
        )
    entries: list[dict[str, Any]] = parsed
    expected_repository = repository.split("/", maxsplit=1)[1]
    if provider == "github":
        for item in entries:
            head_repository = item.get("headRepository")
            head_owner = item.get("headRepositoryOwner")
            if not isinstance(head_repository, dict) or not isinstance(head_owner, dict):
                raise SelcError(
                    "REQUEST_QUERY_AMBIGUOUS",
                    "GitHub request query lacks head repository identity",
                    "inspect the fixed repository before retrying",
                )
            matches_identity = (
                item.get("headRefName") == branch
                and item.get("baseRefName") == base_branch
                and item.get("headRefOid") == commit
                and head_repository.get("nameWithOwner") == expected_repository
                and head_owner.get("login") == expected_repository.split("/", maxsplit=1)[0]
            )
            if not matches_identity:
                raise SelcError(
                    "REQUEST_CONFLICT",
                    "provider returned a request that does not match the prepared repository, head, and base",
                    "inspect the fixed repository before retrying",
                )
            state = item.get("state")
            if not isinstance(state, str):
                raise SelcError(
                    "REQUEST_QUERY_AMBIGUOUS",
                    "GitHub request query lacks an open-state value",
                    "inspect the fixed repository before retrying",
                )
            if state != "OPEN":
                raise SelcError(
                    "REQUEST_INACTIVE",
                    "GitHub returned a matching contribution request that is not open",
                    "inspect the inactive request and prepare a new contribution if needed",
                )
        return "matching"
    elif provider == "gitlab":
        for item in entries:
            source_project = item.get("source_project_id")
            target_project = item.get("target_project_id")
            if not isinstance(source_project, int) or not isinstance(target_project, int):
                raise SelcError(
                    "REQUEST_QUERY_AMBIGUOUS",
                    "GitLab request query lacks source or target project identity",
                    "inspect the fixed repository before retrying",
                )
            matches_identity = (
                item.get("source_branch") == branch
                and item.get("target_branch") == base_branch
                and item.get("sha") == commit
                and source_project == target_project
            )
            if not matches_identity:
                raise SelcError(
                    "REQUEST_CONFLICT",
                    "provider returned a request that does not match the prepared repository, head, and base",
                    "inspect the fixed repository before retrying",
                )
            state = item.get("state")
            if not isinstance(state, str):
                raise SelcError(
                    "REQUEST_QUERY_AMBIGUOUS",
                    "GitLab request query lacks an opened-state value",
                    "inspect the fixed repository before retrying",
                )
            if state != "opened":
                raise SelcError(
                    "REQUEST_INACTIVE",
                    "GitLab returned a matching contribution request that is not opened",
                    "inspect the inactive request and prepare a new contribution if needed",
                )
        return "matching"
    else:
        raise SelcError(
            "UNSUPPORTED_PROVIDER",
            f"unsupported provider: {provider}",
            "choose github or gitlab",
        )
    raise SelcError(
        "REQUEST_CONFLICT",
        "provider returned a request that does not match the prepared repository, head, and base",
        "inspect the fixed repository before retrying",
    )


def _is_controlled_contribution_path(path: Path, suite_subdir: str) -> bool:
    suite_path = Path() if suite_subdir == "." else Path(suite_subdir)
    try:
        relative = path.relative_to(suite_path)
    except ValueError:
        return False
    if relative == Path("suite.json"):
        return True
    return bool(relative.parts) and relative.parts[0] in {"skills", "resources"}


def _assert_workspace_matches_plan(workspace: Path, plan: dict[str, Any]) -> None:
    """Reject any un-evaluated workspace alteration before it can be committed."""

    suite_root = workspace / str(plan["suite_subdir"])
    expected = {
        "workspace_suite_hash": portable_tree_hash(suite_root),
        "workspace_business_hash": _business_content_hash(suite_root),
        "workspace_manifest_hash": file_hash(suite_root / "suite.json"),
        "workspace_runtime_hash": tree_hash(suite_root / "runtime"),
        "workspace_wrapper_hash": file_hash(suite_root / "bin" / "selc-suite.py"),
    }
    if any(plan.get(key) != value for key, value in expected.items()):
        raise SelcError(
            "UNEVALUATED_WORKSPACE_CHANGE",
            "contribution workspace differs from the evaluated candidate package",
            "discard the workspace edit and prepare/evaluate a new candidate",
        )


def _assert_committed_workspace_matches_plan(
    workspace: Path, contribution_commit: str, plan: dict[str, Any]
) -> None:
    """Re-read the actual Git commit before push, rather than trusting the index."""

    _assert_contribution_commit_paths_are_safe(workspace, contribution_commit, plan)
    with tempfile.TemporaryDirectory(prefix="selc-contribution-verify-") as temporary:
        checkout = Path(temporary) / "checkout"
        run_argv(["git", "clone", "--no-checkout", str(workspace), str(checkout)])
        run_argv(["git", "checkout", "--detach", contribution_commit], cwd=checkout)
        verified_plan = dict(plan)
        verified_plan["workspace"] = str(checkout)
        _assert_workspace_matches_plan(checkout, verified_plan)


def _business_content_hash(root: Path) -> str:
    entries: list[dict[str, str]] = []
    for section in ("skills", "resources"):
        section_root = root / section
        if not section_root.is_dir():
            raise SelcError(
                "INVALID_CANDIDATE",
                f"business content has no {section} directory",
                "restore the complete candidate or target suite",
            )
        for path in sorted(section_root.rglob("*")):
            if path.is_symlink():
                raise SelcError(
                    "UNSAFE_SYMLINK",
                    f"business content cannot contain a symlink: {path.relative_to(root)}",
                    "copy the business file into the suite",
                )
            if path.is_file():
                entries.append({"path": path.relative_to(root).as_posix(), "hash": file_hash(path)})
    return json_hash(entries)


def prepare_release(
    suite_root: Path,
    candidate_root: Path,
    evaluation_path: Path,
    provider: str,
    target: str,
    tag: str,
    output: Path,
    center: Path | None,
    repository: str | None,
) -> dict[str, Any]:
    load_manifest(suite_root)
    safe_tag = _release_tag_path(tag)
    repository_root = _find_git_root(suite_root)
    if repository_root is None:
        raise SelcError(
            "MISSING_SOURCE_BASELINE",
            "release preparation needs a suite checked out from its saved Git source",
            "prepare the release from a local source repository",
        )
    source_repository = _source_repository_location(repository_root)
    contribution_base = _contribution_base_branch(repository_root, target)
    try:
        suite_subdir = suite_root.resolve().relative_to(repository_root.resolve()).as_posix()
    except ValueError as error:
        raise SelcError(
            "INVALID_SOURCE",
            "release suite must be inside its source repository",
            "prepare the release from the saved suite baseline",
        ) from error
    if provider == "local":
        if center is None:
            raise SelcError(
                "INVALID_INPUT",
                "local release preparation requires --center",
                "choose the local release center before confirmation",
            )
        center_root, _, _ = _local_release_paths(center, safe_tag)
        destination = {"kind": "local", "center": str(center_root)}
    else:
        if not repository:
            raise SelcError(
                "INVALID_INPUT",
                "remote release preparation requires --repository",
                "provide the explicit GitHub or GitLab repository identifier",
            )
        destination = {"kind": provider, "repository": repository}
    evaluation = read_json(evaluation_path)
    if evaluation.get("status") != "passed":
        raise SelcError(
            "EVALUATION_REQUIRED",
            "a passed evaluation is required before release preparation",
            "fix the candidate and evaluate it",
        )
    if evaluation.get("candidate_hash") != tree_hash(candidate_root):
        raise SelcError(
            "EVALUATION_STALE",
            "candidate changed after evaluation",
            "evaluate the current candidate again",
        )
    _validate_candidate_structure(candidate_root, evaluation)
    package_path = output.with_suffix(".zip")
    with tempfile.TemporaryDirectory(prefix="selc-release-suite-") as temporary:
        checkout = Path(temporary) / "checkout"
        run_argv(["git", "clone", "--no-checkout", source_repository, str(checkout)])
        target_commit = run_argv(["git", "rev-parse", target], cwd=checkout)
        run_argv(["git", "checkout", "--detach", target_commit], cwd=checkout)
        source_baseline = {
            "schema_version": 1,
            "kind": "git",
            "repo": source_repository,
            "subdir": suite_subdir,
            "commit": target_commit,
            "base": target,
            "contribution_base": contribution_base,
        }
        target_suite = checkout / suite_subdir
        target_manifest = load_manifest(target_suite)
        target_suite_hash = portable_tree_hash(target_suite)
        if _business_content_hash(target_suite) != _business_content_hash(candidate_root):
            raise SelcError(
                "CANDIDATE_NOT_IN_TARGET",
                "release target does not contain the evaluated candidate business files",
                "execute and integrate the prepared contribution, then prepare release from that commit",
            )
        release_suite = Path(temporary) / "suite"
        shutil.copytree(target_suite, release_suite)
        release_manifest = read_json(release_suite / "suite.json")
        release_manifest["content_hash"] = suite_content_hash(release_suite)
        write_json(release_suite / "suite.json", release_manifest)
        package = package_suite(release_suite, package_path, source_baseline=source_baseline)
        released_suite_hash = portable_tree_hash(release_suite)
        framework = load_framework(release_suite)
    plan = {
        "schema_version": 1,
        "suite_id": target_manifest["suite_id"],
        "version": target_manifest["version"],
        "provider": provider,
        "target_ref": target,
        "target_commit": target_commit,
        "tag": safe_tag.as_posix(),
        "destination": destination,
        "source_repository": source_repository,
        "source_upstream": source_repository,
        "suite_subdir": suite_subdir,
        "contribution_base": contribution_base,
        "source_baseline": source_baseline,
        "framework": {
            "framework_version": framework["framework_version"],
            "runtime_protocol": framework["runtime_protocol"],
            "runtime_hash": framework["runtime_hash"],
        },
        "suite_hash": released_suite_hash,
        "target_suite_hash": target_suite_hash,
        "candidate_hash": tree_hash(candidate_root),
        "candidate_path": str(candidate_root.resolve()),
        "evaluation_hash": file_hash(evaluation_path),
        "evaluation_path": str(evaluation_path.resolve()),
        "package": str(package_path),
        "package_hash": file_hash(package_path),
        "package_summary": package,
    }
    plan["plan_hash"] = release_plan_hash(plan)
    write_json(output, plan)
    return plan


def release_plan_hash(plan: dict[str, Any]) -> str:
    """Hash every confirmation-bound plan field, excluding only the stored digest itself."""

    content = dict(plan)
    content.pop("plan_hash", None)
    return json_hash(content)


def _find_git_root(path: Path) -> Path | None:
    for candidate in (path, *path.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _release_tag_path(tag: str) -> Path:
    try:
        path = require_relative_path(tag)
    except SelcError as error:
        raise SelcError(
            "UNSAFE_TAG",
            "release tag must be a safe relative path below the release center",
            "use a non-empty tag without '..' or an absolute path",
        ) from error
    code, _, _ = run_argv_result(
        ["git", "check-ref-format", "--allow-onelevel", f"refs/tags/{path.as_posix()}"]
    )
    if code != 0:
        raise SelcError(
            "INVALID_TAG",
            "release tag is not a valid Git tag reference",
            "use a Git-valid relative tag name",
        )
    return path


def _local_release_paths(center: Path, tag: str | Path) -> tuple[Path, Path, Path]:
    """Resolve a local release location without allowing a symlink to reset its boundary."""

    tag_path = _release_tag_path(str(tag)) if isinstance(tag, str) else tag
    center_root = center.resolve()
    releases_root = (center / "releases").resolve()
    release_directory = (releases_root / tag_path).resolve()
    try:
        releases_root.relative_to(center_root)
        release_directory.relative_to(releases_root)
        release_directory.relative_to(center_root)
    except ValueError as error:
        raise SelcError(
            "UNSAFE_TAG",
            "release destination escapes the confirmed release center",
            "repair the center path or choose a safe tag below its releases directory",
        ) from error
    return center_root, releases_root, release_directory


def _safe_suite_subdir(value: str) -> str:
    path = Path(value)
    if not value or path.is_absolute() or ".." in path.parts:
        raise SelcError(
            "CONFIRMATION_STALE",
            "release plan has an unsafe suite location",
            "prepare and confirm a new release plan",
        )
    return path.as_posix()


def _source_repository_location(repository_root: Path) -> str:
    code, output, _ = run_argv_result(
        ["git", "config", "--get", "remote.origin.url"], cwd=repository_root
    )
    return output if code == 0 and output else str(repository_root.resolve())


def _contribution_base_branch(repository_root: Path, fallback_commit: str) -> str:
    current = run_argv(["git", "branch", "--show-current"], cwd=repository_root)
    if current:
        return current
    code, output, _ = run_argv_result(
        ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], cwd=repository_root
    )
    if code == 0 and output.startswith("origin/"):
        return output.removeprefix("origin/")
    branches = run_argv(
        ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads"], cwd=repository_root
    ).splitlines()
    for preferred in ("main", "master"):
        if preferred in branches:
            return preferred
    return branches[0] if len(branches) == 1 else fallback_commit


def confirm_release(plan_path: Path, summary_hash: str, output: Path) -> dict[str, Any]:
    plan = read_json(plan_path)
    declared_hash = plan.get("plan_hash")
    computed_hash = release_plan_hash(plan)
    if declared_hash != computed_hash:
        raise SelcError(
            "PLAN_TAMPERED",
            "release plan fields changed after preparation",
            "prepare a new release plan and obtain a new confirmation",
        )
    if summary_hash != computed_hash:
        raise SelcError(
            "CONFIRMATION_STALE",
            "confirmation does not match the release plan",
            "review the latest plan and confirm its exact hash",
        )
    confirmation = {"schema_version": 1, "plan_hash": summary_hash, "confirmed": True}
    write_json(output, confirmation)
    return confirmation


def _validate_release_execution(plan_path: Path, confirmation_path: Path) -> dict[str, Any]:
    plan = read_json(plan_path)
    confirmation = read_json(confirmation_path)
    _release_tag_path(str(plan.get("tag", "")))
    computed_hash = release_plan_hash(plan)
    if plan.get("plan_hash") != computed_hash:
        raise SelcError(
            "PLAN_TAMPERED",
            "release plan fields changed after preparation",
            "prepare a new release plan and obtain a new confirmation",
        )
    if confirmation.get("plan_hash") != computed_hash or not confirmation.get("confirmed"):
        raise SelcError(
            "CONFIRMATION_REQUIRED",
            "release needs a matching final maintainer confirmation",
            "confirm the current plan",
        )
    package = Path(plan["package"])
    if not package.is_file() or file_hash(package) != plan["package_hash"]:
        raise SelcError(
            "CONFIRMATION_STALE",
            "package changed after release preparation",
            "prepare and confirm a new release plan",
        )
    candidate_path = Path(plan.get("candidate_path", ""))
    if not candidate_path.is_dir() or tree_hash(candidate_path) != plan["candidate_hash"]:
        raise SelcError(
            "CONFIRMATION_STALE",
            "candidate changed after release preparation",
            "prepare and confirm a new release plan",
        )
    evaluation_path = Path(plan.get("evaluation_path", ""))
    if not evaluation_path.is_file() or file_hash(evaluation_path) != plan["evaluation_hash"]:
        raise SelcError(
            "CONFIRMATION_STALE",
            "evaluation changed after release preparation",
            "prepare and confirm a new release plan",
        )
    source_repository = str(plan.get("source_repository", ""))
    if not source_repository:
        raise SelcError(
            "CONFIRMATION_STALE",
            "source repository is unavailable after release preparation",
            "restore the source baseline and prepare a new release plan",
        )
    suite_subdir = _safe_suite_subdir(str(plan.get("suite_subdir", "")))
    with tempfile.TemporaryDirectory(prefix="selc-release-verify-") as temporary:
        checkout = Path(temporary) / "checkout"
        run_argv(["git", "clone", "--no-checkout", source_repository, str(checkout)])
        run_argv(["git", "cat-file", "-e", f"{plan['target_commit']}^{{commit}}"], cwd=checkout)
        run_argv(["git", "checkout", "--detach", plan["target_commit"]], cwd=checkout)
        target_suite = checkout / suite_subdir
        target_manifest = load_manifest(target_suite)
        if _business_content_hash(target_suite) != _business_content_hash(candidate_path):
            raise SelcError(
                "CONFIRMATION_STALE",
                "release target no longer matches the evaluated candidate business content",
                "execute the matching contribution and prepare a new release plan",
            )
        if portable_tree_hash(target_suite) != plan.get("target_suite_hash"):
            raise SelcError(
                "CONFIRMATION_STALE",
                "release target suite content differs from the prepared target",
                "prepare and confirm a new release plan",
            )
        framework = load_framework(target_suite)
        if {
            "framework_version": framework["framework_version"],
            "runtime_protocol": framework["runtime_protocol"],
            "runtime_hash": framework["runtime_hash"],
        } != plan.get("framework"):
            raise SelcError(
                "CONFIRMATION_STALE",
                "release target framework differs from the prepared target",
                "prepare and confirm a new release plan",
            )
        if target_manifest["suite_id"] != plan["suite_id"]:
            raise SelcError(
                "CONFIRMATION_STALE",
                "release target suite identity differs from the prepared plan",
                "prepare and confirm a new release plan",
            )
    _assert_package_business_matches_candidate(package, candidate_path)
    return plan


def release_local(plan_path: Path, confirmation_path: Path, center: Path) -> dict[str, Any]:
    plan = _validate_release_execution(plan_path, confirmation_path)
    if plan.get("provider") != "local":
        raise SelcError(
            "UNSUPPORTED_PROVIDER",
            "local execution requires a plan prepared for the local provider",
            "prepare a local release plan or use the explicit remote adapter",
        )
    destination = plan.get("destination")
    if not isinstance(destination, dict) or destination.get("center") != str(center.resolve()):
        raise SelcError(
            "CONFIRMATION_STALE",
            "release center differs from the center bound by the confirmation",
            "prepare and confirm a plan for the requested release center",
        )
    package = Path(plan["package"])
    center_root, releases_root, release_directory = _local_release_paths(center, str(plan["tag"]))
    center_root.mkdir(parents=True, exist_ok=True)
    with exclusive_lock(center_root / ".selc-release.lock"):
        if release_directory.exists() or release_directory.is_symlink():
            _assert_existing_local_release_matches_plan(release_directory, center_root, plan)
            return {"released": "reused", "tag": plan["tag"], "location": str(release_directory)}

        releases_root.mkdir(parents=True, exist_ok=True)
        release_directory.parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=".selc-release-", dir=center_root))
        try:
            staged_package = stage / package.name
            shutil.copy2(package, staged_package)
            metadata_value = _local_release_metadata(plan, staged_package.name)
            write_json(stage / "release.json", metadata_value)
            _assert_local_release_directory(stage, center_root, expected=metadata_value)
            os.replace(stage, release_directory)
        except Exception:
            shutil.rmtree(stage, ignore_errors=True)
            raise
    return {"released": "created", "tag": plan["tag"], "location": str(release_directory)}


def _local_release_metadata(plan: dict[str, Any], package_name: str) -> dict[str, Any]:
    """Persist the confirmation-bound local target identity beside its asset."""

    return {
        "schema_version": 1,
        "tag": plan["tag"],
        "suite_id": plan["suite_id"],
        "package": package_name,
        "package_hash": plan["package_hash"],
        "target_commit": plan["target_commit"],
        "target_suite_hash": plan["target_suite_hash"],
        "suite_hash": plan["suite_hash"],
        "framework": plan["framework"],
        "source_upstream": plan["source_upstream"],
        "suite_subdir": plan["suite_subdir"],
        "contribution_base": plan["contribution_base"],
        "plan_hash": plan["plan_hash"],
    }


def _assert_existing_local_release_matches_plan(
    release_directory: Path, center_root: Path, plan: dict[str, Any]
) -> None:
    metadata_path = release_directory / "release.json"
    if not release_directory.is_dir() or not metadata_path.is_file():
        raise SelcError(
            "RELEASE_RECOVERY_REQUIRED",
            "release tag exists without a complete SELC release",
            "inspect or remove the incomplete tag directory before retrying",
        )
    expected = _local_release_metadata(plan, Path(str(plan["package"])).name)
    _assert_local_release_directory(release_directory, center_root, expected=expected)


def _assert_local_release_directory(
    release_directory: Path,
    center_root: Path,
    *,
    expected: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metadata = read_json(release_directory / "release.json")
    required = {
        "schema_version",
        "tag",
        "suite_id",
        "package",
        "package_hash",
        "target_commit",
        "target_suite_hash",
        "suite_hash",
        "framework",
        "source_upstream",
        "suite_subdir",
        "contribution_base",
        "plan_hash",
    }
    if required.difference(metadata):
        raise SelcError(
            "RELEASE_INVALID",
            "local release metadata is incomplete",
            "repair or recreate the release from its confirmed plan",
        )
    package_name = require_relative_path(str(metadata["package"]))
    package = (release_directory / package_name).resolve()
    try:
        package.relative_to(release_directory.resolve())
        package.relative_to(center_root.resolve())
    except ValueError as error:
        raise SelcError(
            "RELEASE_INVALID",
            "local release metadata points outside its release directory",
            "repair or recreate the release",
        ) from error
    if not package.is_file() or file_hash(package) != metadata["package_hash"]:
        raise SelcError(
            "RELEASE_INVALID",
            "local release package does not match its metadata",
            "repair or recreate the release",
        )
    if expected is not None and metadata != expected:
        raise SelcError(
            "RELEASE_CONFLICT",
            "existing release tag does not match the confirmed release target",
            "choose a new tag or repair the existing release",
        )
    return metadata


def _assert_package_business_matches_candidate(package: Path, candidate_root: Path) -> None:
    """Check that the bound package contains the evaluated business files, without extraction."""

    expected: dict[str, str] = {}
    for section in ("skills", "resources"):
        for path in sorted((candidate_root / section).rglob("*")):
            if path.is_file():
                expected[path.relative_to(candidate_root).as_posix()] = file_hash(path)
    with zipfile.ZipFile(package) as archive:
        actual = {
            name: hashlib.sha256(archive.read(name)).hexdigest()
            for name in archive.namelist()
            if name.startswith(("skills/", "resources/")) and not name.endswith("/")
        }
    if actual != expected:
        raise SelcError(
            "CONFIRMATION_STALE",
            "release package business content differs from the evaluated candidate",
            "prepare and confirm a new release plan",
        )


def verify_local_release(center: Path, tag: str) -> dict[str, Any]:
    center_root, _, release_directory = _local_release_paths(center, tag)
    metadata = _assert_local_release_directory(release_directory, center_root)
    package = release_directory / str(metadata["package"])
    return {"verified": True, "tag": tag, "package": str(package)}


def remote_release_argv(
    provider: str,
    client: str,
    tag: str,
    target: str,
    notes_file: Path,
    repository: str,
    package: Path | None = None,
) -> list[str]:
    if provider == "github":
        arguments = [
            client,
            "release",
            "create",
            tag,
            "--target",
            target,
            "--notes-file",
            str(notes_file),
            "--repo",
            repository,
        ]
        if package is not None:
            arguments.insert(4, str(package))
        return arguments
    if provider == "gitlab":
        arguments = [
            client,
            "release",
            "create",
            tag,
            "--ref",
            target,
            "--notes-file",
            str(notes_file),
            "--no-update",
            "--repo",
            repository,
        ]
        if package is not None:
            arguments.insert(4, str(package))
        return arguments
    raise SelcError(
        "UNSUPPORTED_PROVIDER",
        f"unsupported provider: {provider}",
        "choose local, github, or gitlab",
    )


def query_remote_release(provider: str, client: str, tag: str, repository: str) -> dict[str, Any]:
    argv = remote_release_query_argv(provider, client, tag, repository)
    output = run_argv(argv)
    return {"provider": provider, "query": argv, "output": output}


def release_remote(plan_path: Path, confirmation_path: Path, client: str) -> dict[str, Any]:
    """Create a remote release only after an explicit, target-bound absence query."""

    plan = _validate_release_execution(plan_path, confirmation_path)
    provider = str(plan["provider"])
    if provider not in {"github", "gitlab"}:
        raise SelcError(
            "UNSUPPORTED_PROVIDER",
            "remote execution requires a GitHub or GitLab plan",
            "prepare a remote provider plan or use local execution",
        )
    destination = plan.get("destination")
    if not isinstance(destination, dict) or destination.get("kind") != provider:
        raise SelcError(
            "CONFIRMATION_STALE",
            "remote destination differs from the bound release plan",
            "prepare and confirm a plan for the intended repository",
        )
    repository = destination.get("repository")
    if not isinstance(repository, str) or not repository:
        raise SelcError(
            "CONFIRMATION_STALE",
            "remote release plan has no explicit repository",
            "prepare and confirm a plan with --repository",
        )
    initial_state = _remote_release_state(plan, client, repository)
    query = initial_state["query"]
    if initial_state["state"] == "matching":
        return {
            "released": "reused",
            "provider": provider,
            "tag": plan["tag"],
            "repository": repository,
            "query": query,
            "verify": initial_state["verify"],
        }
    if initial_state["state"] == "conflict":
        raise SelcError(
            "RELEASE_CONFLICT",
            str(initial_state["message"]),
            "choose a new tag or repair the explicitly identified release asset",
        )
    if initial_state["state"] != "absent":
        raise SelcError(
            "REMOTE_QUERY_UNKNOWN",
            str(initial_state["message"]),
            "resolve provider access or query the tag before retrying",
        )
    with tempfile.TemporaryDirectory(prefix="selc-release-notes-") as temporary:
        notes = Path(temporary) / "notes.md"
        notes.write_text(
            f"SELC package hash: {plan['package_hash']}\n"
            f"SELC target commit: {plan['target_commit']}\n",
            encoding="utf-8",
        )
        create = remote_release_argv(
            provider,
            client,
            plan["tag"],
            plan["target_commit"],
            notes,
            repository,
            Path(plan["package"]),
        )
        exit_code, stdout, stderr = run_argv_result(create)
    verified_state = _remote_release_state(plan, client, repository)
    if verified_state["state"] != "matching":
        if verified_state["state"] == "conflict":
            raise SelcError(
                "RELEASE_CONFLICT",
                str(verified_state["message"]),
                "repair the remote release or prepare a new tag",
            )
        if exit_code != 0:
            raise SelcError(
                "REMOTE_CREATE_UNVERIFIED",
                "release creation returned an error and a follow-up query could not verify the asset",
                "inspect the remote tag before retrying; SELC did not retry creation",
            )
        raise SelcError(
            "REMOTE_VERIFY_FAILED",
            "release creation returned success but target and package asset were not verified",
            "inspect the remote release before retrying",
        )
    return {
        "released": "created" if exit_code == 0 else "recovered_after_ambiguous_create",
        "provider": provider,
        "tag": plan["tag"],
        "repository": repository,
        "query": query,
        "create": create,
        "output": stdout,
        "stderr": stderr,
        "verify": verified_state["verify"],
    }


def remote_release_query_argv(provider: str, client: str, tag: str, repository: str) -> list[str]:
    if provider == "github":
        return [
            client,
            "release",
            "view",
            tag,
            "--repo",
            repository,
            "--json",
            "tagName,targetCommitish,assets",
        ]
    if provider == "gitlab":
        return [client, "release", "view", tag, "--repo", repository, "--output", "json"]
    raise SelcError(
        "UNSUPPORTED_PROVIDER", f"unsupported provider: {provider}", "choose github or gitlab"
    )


def _remote_release_download_argv(
    provider: str,
    client: str,
    tag: str,
    repository: str,
    asset_name: str,
    destination: Path,
) -> list[str]:
    if provider == "github":
        return [
            client,
            "release",
            "download",
            tag,
            "--repo",
            repository,
            "--pattern",
            asset_name,
            "--dir",
            str(destination),
        ]
    if provider == "gitlab":
        return [
            client,
            "release",
            "download",
            tag,
            "--repo",
            repository,
            "--asset-name",
            asset_name,
            "--dir",
            str(destination),
        ]
    raise SelcError(
        "UNSUPPORTED_PROVIDER", f"unsupported provider: {provider}", "choose github or gitlab"
    )


def _remote_release_state(plan: dict[str, Any], client: str, repository: str) -> dict[str, Any]:
    provider = str(plan["provider"])
    query = remote_release_query_argv(provider, client, str(plan["tag"]), repository)
    exit_code, stdout, stderr = run_argv_result(query)
    if exit_code != 0:
        response = "\n".join(value for value in (stdout, stderr) if value).lower()
        if any(marker in response for marker in ("not found", "notfound", "404", "no release")):
            return {"state": "absent", "query": query}
        return {
            "state": "unknown",
            "query": query,
            "message": "remote release lookup did not establish whether the tag exists",
        }
    try:
        value = json.loads(stdout)
    except json.JSONDecodeError:
        return {
            "state": "unknown",
            "query": query,
            "message": "remote release query did not return the required JSON metadata",
        }
    if not isinstance(value, dict):
        return {
            "state": "unknown",
            "query": query,
            "message": "remote release query returned an invalid metadata shape",
        }
    tag = value.get("tagName", value.get("tag_name", value.get("tag")))
    target = value.get("targetCommitish", value.get("target_commitish"))
    if target is None and isinstance(value.get("commit"), dict):
        target = value["commit"].get("id")
    if tag != plan["tag"] or target != plan["target_commit"]:
        return {
            "state": "conflict",
            "query": query,
            "message": "an existing remote release has a different tag identity or target commit",
        }
    asset_name = Path(str(plan["package"])).name
    assets = value.get("assets")
    if provider == "github" and isinstance(assets, list):
        names = {asset.get("name") for asset in assets if isinstance(asset, dict)}
        if asset_name not in names:
            return {
                "state": "conflict",
                "query": query,
                "message": "an existing remote release is missing the required package asset",
            }
    with tempfile.TemporaryDirectory(prefix="selc-release-download-") as temporary:
        destination = Path(temporary)
        download = _remote_release_download_argv(
            provider, client, str(plan["tag"]), repository, asset_name, destination
        )
        download_code, _download_stdout, _download_stderr = run_argv_result(download)
        downloaded = destination / asset_name
        if download_code != 0 or not downloaded.is_file():
            return {
                "state": "conflict",
                "query": query,
                "message": "an existing remote release is missing a downloadable required package asset",
            }
        if file_hash(downloaded) != plan["package_hash"]:
            return {
                "state": "conflict",
                "query": query,
                "message": "an existing remote release package asset has a different content hash",
            }
    return {"state": "matching", "query": query, "verify": {"download": download}}
