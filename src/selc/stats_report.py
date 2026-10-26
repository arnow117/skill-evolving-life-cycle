"""Read current SELC statistics into a self-contained local HTML report.

This module deliberately does not use :func:`records.open_database`: that
helper initializes and migrates databases, while report generation must only
read existing, supported record stores.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import time
import uuid
from collections import defaultdict
from contextlib import suppress
from pathlib import Path
from typing import Any, Iterable

from . import records
from .errors import SelcError
from .stats_report_template import render_report

_BUSY_TIMEOUT_MS = 250
_READ_DEADLINE_SECONDS = 2.0
_METRIC_KEYS = ("N", "S", "K", "U")
_REQUIRED_COLUMNS = {
    "meta": {"key", "value"},
    "tasks": {
        "task_id",
        "suite_id",
        "suite_version",
        "harness",
        "initiator",
        "evidence",
        "is_test",
        "first_result",
        "current_result",
        "created_at",
    },
    "calls": {
        "call_id",
        "task_id",
        "skill",
        "executor",
        "actual_harness",
        "evidence",
        "expected_version",
        "expected_hash",
        "trigger_kind",
        "first_result",
        "result",
        "created_at",
    },
    "imported_samples": {
        "subject_kind",
        "subject_id",
        "suite_id",
        "suite_version",
        "harness",
        "is_test",
        "started_at",
        "initiator",
        "skill",
        "executor",
        "evidence",
        "trigger_kind",
        "expected_hash",
        "first_result",
        "current_result",
    },
}


def generate_stats_report(
    project_roots: Iterable[Path], output: Path, *, include_test: bool
) -> dict[str, Any]:
    """Generate one atomic offline report for explicitly selected projects."""

    roots = _canonical_project_roots(project_roots)
    if not roots:
        raise SelcError(
            "INVALID_ARGUMENT",
            "at least one --project-root is required",
            "supply one or more explicit SELC project roots",
        )
    output_target = _validate_output_target(output, roots)
    project_names = _display_names(roots)
    projects = [read_project(root, display_name=project_names[root]) for root in roots]
    usable = [project for project in projects if project["status"] != "error"]
    if not usable:
        diagnostics = "; ".join(
            _source_diagnostic(project) for project in projects if project["status"] == "error"
        )
        raise SelcError(
            "ALL_PROJECTS_UNAVAILABLE",
            f"no selected project could produce a report snapshot: {diagnostics}",
            "repair each reported source and regenerate; the existing report was kept",
        )

    report_data = {
        "generated_at": records.utc_now(),
        "metric_contract": 1,
        "include_test": include_test,
        "projects": projects,
    }
    html = render_report(report_data)
    output_result = _atomic_write_html(output_target, html, roots)
    result = {
        "output": str(output_target),
        "partial": any(project["status"] == "error" for project in projects),
        "projects": [_summary_project(project) for project in projects],
    }
    if output_result["durability_warning"] is not None:
        result["durability_warning"] = output_result["durability_warning"]
    return result


def read_project(project_root: Path, *, display_name: str) -> dict[str, Any]:
    """Return a minimal count-only projection or an intentionally terse error."""

    try:
        root = _canonical_project_root(project_root)
    except OSError:
        return _error_project(
            display_name,
            "SOURCE_UNREADABLE",
            "project source could not be inspected",
            "check project directory permissions and regenerate",
        )
    if root is None:
        return _error_project(
            display_name,
            "SOURCE_NOT_FOUND",
            "project directory is unavailable",
            "verify the selected project directory and regenerate",
        )
    database_path = root / ".selc" / "records.sqlite3"
    try:
        mode = _read_installation_mode(root)
        database_metadata = _target_metadata(database_path)
    except OSError:
        return _error_project(
            display_name,
            "SOURCE_UNREADABLE",
            "project records could not be inspected",
            "check project records permissions and regenerate",
        )
    if database_metadata is None:
        if mode == "basic":
            return {
                "id": "",
                "name": display_name,
                "status": "basic_no_records",
                "read_at": records.utc_now(),
                "mode": "basic",
                "cells": [],
            }
        return _error_project(
            display_name,
            "MISSING_DATABASE",
            "records database is unavailable",
            "restore a supported records database and regenerate",
        )

    try:
        _validate_database_file(database_path)
        connection = _readonly_connection(database_path)
    except (OSError, SelcError) as error:
        return _error_project(
            display_name,
            _error_code(error),
            _error_message(error),
            _error_recovery(error),
        )

    try:
        _read_deadline = time.monotonic() + _READ_DEADLINE_SECONDS

        def stop_long_query() -> int:
            return int(time.monotonic() >= _read_deadline)

        connection.set_progress_handler(stop_long_query, 1_000)
        connection.execute("PRAGMA query_only = ON")
        connection.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        connection.execute("BEGIN")
        _validate_supported_schema(connection)
        projections = _read_projections(connection)
        connection.rollback()
    except sqlite3.OperationalError as error:
        return _error_project(display_name, "DATABASE_BUSY", _operational_message(error))
    except SelcError as error:
        return _error_project(display_name, error.code, error.message, error.recovery)
    except (sqlite3.DatabaseError, ValueError):
        return _error_project(
            display_name, "UNSUPPORTED_DATABASE", "records database is unsupported"
        )
    finally:
        connection.close()

    without_test_cells = projections["without_test"]["cells"]
    status = "empty" if not without_test_cells else "ready"
    return {
        "id": "",
        "name": display_name,
        "status": status,
        "read_at": records.utc_now(),
        "mode": mode,
        "projections": projections,
    }


def _canonical_project_roots(project_roots: Iterable[Path]) -> list[Path]:
    roots: list[Path] = []
    identities: set[tuple[object, ...]] = set()
    for candidate in project_roots:
        try:
            root = _canonical_project_root(candidate)
        except OSError:
            root = None
        if root is None:
            try:
                unavailable_root = Path(candidate).expanduser().resolve(strict=False)
            except OSError:
                unavailable_root = Path(candidate).expanduser().absolute()
            identity = ("unavailable", str(unavailable_root))
            if identity not in identities:
                identities.add(identity)
                roots.append(unavailable_root)
            continue
        identity = ("directory", *_path_identity(root))
        if identity in identities:
            continue
        identities.add(identity)
        roots.append(root)
    return roots


def _canonical_project_root(project_root: Path) -> Path | None:
    try:
        root = Path(project_root).expanduser().resolve(strict=True)
    except FileNotFoundError:
        return None
    metadata = root.stat()
    if not stat.S_ISDIR(metadata.st_mode):
        return None
    return root


def _display_names(roots: Iterable[Path]) -> dict[Path, str]:
    values: dict[str, int] = defaultdict(int)
    names: dict[Path, str] = {}
    for root in roots:
        base_name = root.name or "project"
        values[base_name] += 1
        suffix = values[base_name]
        names[root] = base_name if suffix == 1 else f"{base_name} ({suffix})"
    return names


def _readonly_connection(database_path: Path) -> sqlite3.Connection:
    try:
        connection = sqlite3.connect(database_path.as_uri() + "?mode=ro", uri=True, timeout=0.25)
    except sqlite3.OperationalError as error:
        if "unable to open database file" in str(error).lower():
            raise SelcError(
                "SOURCE_UNREADABLE",
                "records database could not be opened for reading",
                "check project records permissions and regenerate",
            ) from error
        raise SelcError(
            "DATABASE_BUSY",
            "records database could not be opened for a bounded read",
            "retry after the active record operation completes",
        ) from error
    connection.row_factory = sqlite3.Row
    return connection


def _validate_database_file(database_path: Path) -> None:
    try:
        metadata = database_path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise SelcError(
                "UNSUPPORTED_DATABASE",
                "records database is not a regular file",
                "use an intact rollback-journal SELC records database",
            )
        with database_path.open("rb") as database_file:
            header = database_file.read(100)
    except PermissionError as error:
        raise SelcError(
            "SOURCE_UNREADABLE",
            "records database could not be inspected",
            "check project records permissions and regenerate",
        ) from error
    except OSError as error:
        raise SelcError(
            "UNSUPPORTED_DATABASE",
            "records database could not be inspected",
            "verify the selected project has a readable records database",
        ) from error
    if len(header) < 100 or header[:16] != b"SQLite format 3\x00":
        raise SelcError(
            "UNSUPPORTED_DATABASE",
            "records database has an unsupported SQLite header",
            "use a current SELC records database",
        )
    if (
        header[18] == 2
        or header[19] == 2
        or database_path.with_name(database_path.name + "-wal").exists()
        or database_path.with_name(database_path.name + "-shm").exists()
    ):
        raise SelcError(
            "UNSUPPORTED_JOURNAL_MODE",
            "WAL-mode records databases are not supported for report generation",
            "close the writer and use the supported rollback-journal database",
        )


def _validate_supported_schema(connection: sqlite3.Connection) -> None:
    existing_tables = {
        str(row["name"])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    if not set(_REQUIRED_COLUMNS).issubset(existing_tables):
        raise SelcError(
            "UNSUPPORTED_DATABASE",
            "records database is missing required tables",
            "use a current SELC records database",
        )
    for table, required_columns in _REQUIRED_COLUMNS.items():
        columns = {str(row["name"]) for row in connection.execute(f"PRAGMA table_info({table})")}
        if not required_columns.issubset(columns):
            raise SelcError(
                "UNSUPPORTED_DATABASE",
                "records database is missing required columns",
                "use a current SELC records database",
            )
    row = connection.execute("SELECT value FROM meta WHERE key = 'writer_protocol'").fetchone()
    if row is None or str(row["value"]) != str(records.WRITER_PROTOCOL_VERSION):
        raise SelcError(
            "UNSUPPORTED_DATABASE",
            "records database writer protocol is unsupported",
            "use a records database created by the current SELC writer",
        )


def _read_installation_mode(project_root: Path) -> str:
    registry_path = project_root / ".selc" / "installations.json"
    try:
        registry_metadata = registry_path.lstat()
    except FileNotFoundError:
        return "unverified"
    if stat.S_ISLNK(registry_metadata.st_mode) or not stat.S_ISREG(registry_metadata.st_mode):
        return "unverified"
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        if not isinstance(registry, dict):
            return "unverified"
        installations = registry.get("installations")
        if not isinstance(installations, dict) or not installations:
            return "unverified"
        modes: set[str] = set()
        for entry in installations.values():
            if not isinstance(entry, dict):
                return "unverified"
            mode = entry.get("mode", "observed")
            if not isinstance(mode, str):
                return "unverified"
            modes.add(mode)
    except (AttributeError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        return "unverified"
    if modes == {"basic"}:
        return "basic"
    if modes == {"observed"}:
        return "observed"
    return "unverified"


def _read_projections(connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """Build each test-range projection before discarding deduplication identities."""

    projections: dict[str, dict[str, Any]] = {}
    for key, include_test in (("without_test", False), ("with_test", True)):
        task_rows = records._deduplicated_samples(connection, "task", include_test=include_test)
        call_rows = records._deduplicated_samples(connection, "call", include_test=include_test)
        cells = _project_cells(task_rows, call_rows)
        projections[key] = {
            "cells": cells,
            "metrics": _metrics_for_cells(cells, include_test=True),
        }
    return projections


def _project_cells(
    task_rows: list[dict[str, Any]], call_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    grouped: dict[tuple[object, ...], list[dict[str, Any]]] = defaultdict(list)
    keys_by_kind = {
        "task": ("suite_id", "suite_version", "harness", "initiator"),
        "call": ("suite_id", "suite_version", "harness", "initiator", "skill", "executor"),
    }
    for kind, rows in (("task", task_rows), ("call", call_rows)):
        keys = keys_by_kind[kind]
        for row in rows:
            group = tuple(str(row.get(key, "unknown")) for key in keys)
            grouped[(kind, bool(row["is_test"]), *group)].append(row)

    cells: list[dict[str, Any]] = []
    for grouped_key, rows in sorted(grouped.items(), key=lambda item: tuple(map(str, item[0]))):
        kind = str(grouped_key[0])
        is_test = bool(grouped_key[1])
        keys = keys_by_kind[kind]
        group = dict(zip(keys, grouped_key[2:], strict=True))
        final_key = "current_result" if kind == "task" else "result"
        cells.append(
            {
                "kind": kind,
                "is_test": is_test,
                "group": group,
                "one_shot": _count_metric(records._metrics(rows, "first_result")),
                "final": _count_metric(records._metrics(rows, final_key)),
            }
        )
    return cells


def _metrics_for_cells(cells: Iterable[dict[str, Any]], *, include_test: bool) -> dict[str, Any]:
    result = _empty_metric_set()
    for kind in ("task", "call"):
        relevant = [
            cell
            for cell in cells
            if cell["kind"] == kind and (include_test or not bool(cell["is_test"]))
        ]
        for axis in ("one_shot", "final"):
            result[f"{kind}s"][axis] = _sum_metrics(cell[axis] for cell in relevant)
    return result


def _count_metric(metric: dict[str, int | float | None]) -> dict[str, int]:
    return {key: int(metric[key] or 0) for key in _METRIC_KEYS}


def _sum_metrics(metrics: Iterable[dict[str, int]]) -> dict[str, int]:
    total = {key: 0 for key in _METRIC_KEYS}
    for metric in metrics:
        for key in _METRIC_KEYS:
            total[key] += int(metric[key])
    return total


def _empty_metric_set() -> dict[str, Any]:
    empty = {key: 0 for key in _METRIC_KEYS}
    return {
        "tasks": {"one_shot": dict(empty), "final": dict(empty)},
        "calls": {"one_shot": dict(empty), "final": dict(empty)},
    }


def _error_project(
    display_name: str,
    code: str,
    message: str,
    recovery: str = "repair the project and regenerate",
) -> dict[str, Any]:
    return {
        "id": "",
        "name": display_name,
        "status": "error",
        "read_at": records.utc_now(),
        "mode": "unverified",
        "error": {
            "code": code,
            "message": message,
            "recovery": recovery,
        },
    }


def _source_diagnostic(project: dict[str, Any]) -> str:
    error = project["error"]
    return f"{project['name']} [{error['code']}]: {error['recovery']}"


def _error_code(error: OSError | SelcError) -> str:
    return error.code if isinstance(error, SelcError) else "UNSUPPORTED_DATABASE"


def _error_message(error: OSError | SelcError) -> str:
    return error.message if isinstance(error, SelcError) else "records database is unavailable"


def _error_recovery(error: OSError | SelcError) -> str:
    if isinstance(error, SelcError):
        return error.recovery
    return "check project records permissions and regenerate"


def _operational_message(error: sqlite3.OperationalError) -> str:
    if "interrupted" in str(error).lower():
        return "records database read exceeded its bounded deadline"
    return "records database is busy"


def _summary_project(project: dict[str, Any]) -> dict[str, Any]:
    summary = {key: project[key] for key in ("name", "status", "read_at", "mode")}
    if "projections" in project:
        summary["projections"] = {
            key: value["metrics"] for key, value in project["projections"].items()
        }
    if "error" in project:
        summary["error"] = project["error"]
    return summary


def _validate_output_target(output: Path, roots: Iterable[Path]) -> Path:
    raw_output = Path(output).expanduser()
    if raw_output.suffix.lower() != ".html":
        raise SelcError(
            "INVALID_OUTPUT", "report output must end in .html", "choose an existing .html path"
        )
    parent = raw_output.parent
    try:
        parent_info = parent.lstat()
    except OSError as error:
        raise SelcError(
            "INVALID_OUTPUT",
            "report output parent does not exist",
            "create the output directory first",
        ) from error
    if stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(parent_info.st_mode):
        raise SelcError(
            "INVALID_OUTPUT",
            "report output parent must be an existing directory",
            "choose an existing non-symbolic-link directory",
        )
    target = parent.resolve(strict=True) / raw_output.name
    target_info = _target_metadata(target)
    if target_info is not None:
        if stat.S_ISLNK(target_info.st_mode):
            raise SelcError(
                "INVALID_OUTPUT",
                "report output cannot be a symbolic link",
                "choose a regular .html report path",
            )
        if not stat.S_ISREG(target_info.st_mode) or target_info.st_nlink != 1:
            raise SelcError(
                "INVALID_OUTPUT",
                "report output must be an unlinked regular file",
                "choose a new or single-link .html report path",
            )
    for root in roots:
        source_root = root / ".selc"
        try:
            if target.is_relative_to(source_root.resolve(strict=False)):
                raise SelcError(
                    "INVALID_OUTPUT",
                    "report output cannot be inside a source .selc directory",
                    "choose an output path outside the selected source data",
                )
            for source_file in (
                source_root / "records.sqlite3",
                source_root / "installations.json",
            ):
                if _same_file(target, source_file):
                    raise SelcError(
                        "INVALID_OUTPUT",
                        "report output aliases a protected source file",
                        "choose a different .html output path",
                    )
        except OSError:
            # A source which cannot be inspected is reported independently by
            # read_project. The output itself was already lstat-checked above,
            # including the multi-link defense that prevents alias replacement.
            continue
    return target


def _target_metadata(target: Path) -> os.stat_result | None:
    try:
        return target.lstat()
    except FileNotFoundError:
        return None


def _same_file(left: Path, right: Path) -> bool:
    try:
        right_metadata = right.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(right_metadata.st_mode):
        return False
    return os.path.samefile(left, right)


def _path_identity(path: Path) -> tuple[int, int]:
    metadata = path.stat()
    return metadata.st_dev, metadata.st_ino


def _atomic_write_html(target: Path, html: str, roots: Iterable[Path]) -> dict[str, str | None]:
    try:
        encoded = html.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise SelcError(
            "INVALID_REPORT_TEXT",
            "report content cannot be encoded as strict UTF-8",
            "remove invalid text from the selected report dimensions and regenerate",
        ) from error
    parent = target.parent
    parent_before = _path_identity(parent)
    target_before = _target_metadata(target)
    descriptor: int | None = None
    temporary = parent / f".{target.name}.{uuid.uuid4().hex}.tmp"
    try:
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = None
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        _recheck_output_target(target, roots, parent_before, target_before)
        os.replace(temporary, target)
    except OSError as error:
        raise SelcError(
            "OUTPUT_WRITE_FAILED",
            "report output could not be written safely",
            "check the output directory and retry",
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
    try:
        _fsync_parent_directory(parent)
    except OSError:
        return {
            "durability_warning": (
                "report was replaced, but parent-directory durability could not be confirmed"
            )
        }
    return {"durability_warning": None}


def _fsync_parent_directory(parent: Path) -> None:
    directory_descriptor = os.open(parent, os.O_RDONLY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)


def _recheck_output_target(
    target: Path,
    roots: Iterable[Path],
    parent_before: tuple[int, int],
    target_before: os.stat_result | None,
) -> None:
    if _path_identity(target.parent) != parent_before:
        raise SelcError(
            "INVALID_OUTPUT",
            "report output parent changed during generation",
            "retry with a stable output directory",
        )
    target_now = _target_metadata(target)
    if target_before is None and target_now is not None:
        raise SelcError(
            "INVALID_OUTPUT",
            "report output appeared during generation",
            "retry with a stable output path",
        )
    if target_before is not None and target_now is None:
        raise SelcError(
            "INVALID_OUTPUT",
            "report output disappeared during generation",
            "retry with a stable output path",
        )
    if target_before is not None and target_now is not None:
        before_identity = (target_before.st_dev, target_before.st_ino, target_before.st_nlink)
        now_identity = (target_now.st_dev, target_now.st_ino, target_now.st_nlink)
        if before_identity != now_identity:
            raise SelcError(
                "INVALID_OUTPUT",
                "report output changed during generation",
                "retry with a stable output path",
            )
    _validate_output_target(target, roots)
