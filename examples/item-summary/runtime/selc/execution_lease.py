"""Durable process identities for managed side effects.

An advisory suite lock belongs to its parent process and vanishes if that
parent dies.  Managed actions therefore retain a kernel-observed process
identity which callers must check before they start, recover, delete, or
replace a suite.
"""

from __future__ import annotations

import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .errors import SelcError
from .records import utc_now


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    process_group: int
    started: str


def inspect_process(pid: int, started: str = "") -> ProcessIdentity | None:
    """Return the process-group identity, or ``None`` after termination."""

    if pid <= 0:
        return None
    try:
        os.kill(pid, 0)
        process_group = os.getpgid(pid)
    except ProcessLookupError:
        return None
    except OSError as error:
        raise SelcError(
            "EXECUTION_IDENTITY_UNAVAILABLE",
            "cannot inspect the managed process identity",
            "pause managed work until process identity inspection is available",
        ) from error
    return ProcessIdentity(pid, process_group, started)


def process_group_is_live(process_group: int) -> bool:
    """Return whether any member of a recorded process group still exists.

    The session leader can exit before a child it launched.  A successful
    ``killpg(..., 0)`` therefore carries the safety boundary for a managed
    execution; the leader PID is only an initial identity check.
    """

    if process_group <= 0:
        raise SelcError(
            "EXECUTION_IDENTITY_UNAVAILABLE",
            "managed execution has no valid process group identity",
            "pause managed work until the durable execution record is repaired",
        )
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError as error:
        raise SelcError(
            "EXECUTION_IDENTITY_UNAVAILABLE",
            "cannot inspect the managed process group",
            "pause managed work until process group inspection is available",
        ) from error
    except OSError as error:
        raise SelcError(
            "EXECUTION_IDENTITY_UNAVAILABLE",
            "cannot inspect the managed process group",
            "pause managed work until process group inspection is available",
        ) from error
    return True


def assert_no_live_execution(
    database: Any,
    suite_id: str,
    *,
    task_ids: Iterable[str] | None = None,
    action_run_ids: Iterable[str] | None = None,
) -> None:
    """Fence live executions for a suite, optionally limited to selected tasks.

    Deletion needs the optional task filter because a time-bounded operation
    may remove only part of a suite.  The execution record belongs to an
    action, so the filter follows the durable managed-action relationship
    instead of trying to infer a task from a process identifier.
    """

    was_in_transaction = bool(getattr(database, "in_transaction", False))
    marked_terminated = False
    if task_ids is not None and action_run_ids is not None:
        raise ValueError("select executions by task or action, not both")
    selected_task_ids = tuple(sorted(set(task_ids))) if task_ids is not None else None
    selected_action_ids = tuple(sorted(set(action_run_ids))) if action_run_ids is not None else None
    if selected_task_ids == () or selected_action_ids == ():
        return

    filters = ["execution.suite_id = ?", "execution.state = 'running'"]
    arguments: list[object] = [suite_id]
    if selected_action_ids is not None:
        placeholders = ", ".join("?" for _ in selected_action_ids)
        filters.append("execution.action_run_id IN (" + placeholders + ")")
        arguments.extend(selected_action_ids)
    elif selected_task_ids is not None:
        placeholders = ", ".join("?" for _ in selected_task_ids)
        filters.append(
            """execution.action_run_id IN (
            SELECT action_run_id FROM managed_actions
            WHERE task_id IN ("""
            + placeholders
            + "))"
        )
        arguments.extend(selected_task_ids)
    where = " AND ".join(filters)
    rows = database.execute(
        """SELECT execution.action_run_id, execution.pid, execution.process_group,
        execution.started_identity, execution.heartbeat_path
        FROM managed_executions AS execution WHERE """
        + where,
        arguments,
    ).fetchall()
    for row in rows:
        process_group = int(row["process_group"])
        if not process_group_is_live(process_group):
            database.execute(
                "UPDATE managed_executions SET state = 'terminated', updated_at = ? WHERE action_run_id = ?",
                (utc_now(), row["action_run_id"]),
            )
            marked_terminated = True
            continue

        identity = inspect_process(int(row["pid"]), str(row["started_identity"]))
        if identity is not None and identity.process_group != process_group:
            raise SelcError(
                "MANAGED_EXECUTION_UNCERTAIN",
                "a managed process identifier no longer matches its durable process group",
                "pause managed work and inspect the retained execution record",
            )
        heartbeat_state = _heartbeat_state(str(row["heartbeat_path"]), str(row["started_identity"]))
        if heartbeat_state == "running":
            raise SelcError(
                "MANAGED_EXECUTION_ACTIVE",
                "a managed execution tree is still alive",
                "wait for the managed process to terminate before starting, recovering, deleting, or updating",
            )
        raise SelcError(
            "MANAGED_EXECUTION_UNCERTAIN",
            "a live managed process lacks its expected handshake heartbeat",
            "pause managed work and inspect the retained execution record",
        )
    if marked_terminated and not was_in_transaction:
        database.commit()


def _heartbeat_state(path_value: str, token: str) -> str | None:
    if not path_value or not token:
        return None
    try:
        raw = Path(path_value).read_text(encoding="utf-8").strip()
        heartbeat_token, state, timestamp = raw.split(" ", 2)
        observed_at = float(timestamp)
    except (OSError, ValueError):
        return None
    if heartbeat_token != token or state not in {"running", "terminated"}:
        return None
    if not 0 <= time.time() - observed_at <= 2:
        return None
    return state


def terminate_process_group(identity: ProcessIdentity) -> None:
    """Boundedly terminate every remaining member of a local execution group."""

    try:
        os.killpg(identity.process_group, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 1
    while process_group_is_live(identity.process_group) and time.monotonic() < deadline:
        time.sleep(0.02)
    if not process_group_is_live(identity.process_group):
        return
    try:
        os.killpg(identity.process_group, signal.SIGKILL)
    except ProcessLookupError:
        return
