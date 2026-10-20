#!/opt/homebrew/bin/python3.11
"""Private probe-v1 for observing one explicitly prepared host workspace.

This experiment intentionally does not import SELC runtime code.  It records only
the hook payload sent for a prepared fixture and allows its one small business
command only after a fresh, native PreToolUse receipt is committed locally.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shlex
import sqlite3
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable


PROTOCOL = "probe-v1"
FIXTURE_DIRECTORY = ".host_observation_probe"
FIXTURE_FILE = "fixture.json"
DATABASE_FILE = "observations.sqlite3"
SKILL_NAME = "host-observation-probe"
RESULT_FILE = "result.txt"
RECEIPT_MAX_AGE_SECONDS = 60
SUPPORTED_HOSTS = {"codex", "workbuddy"}
SUPPORTED_NATIVE_TOOL_NAMES = {"Bash", "exec_command"}


class ProbeError(Exception):
    """A checked probe precondition failure with a stable diagnostic code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class Fixture:
    host: str
    nonce: str
    workspace: Path
    execute_argv: tuple[str, ...]


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def resolve_path(value: str | Path) -> Path:
    return Path(value).expanduser().resolve()


def state_directory(workspace: Path) -> Path:
    return workspace / FIXTURE_DIRECTORY


def fixture_path(workspace: Path) -> Path:
    return state_directory(workspace) / FIXTURE_FILE


def database_path(workspace: Path) -> Path:
    return state_directory(workspace) / DATABASE_FILE


def is_within(path: Path, directory: Path) -> bool:
    try:
        path.resolve().relative_to(directory.resolve())
    except ValueError:
        return False
    return True


def read_fixture(workspace_value: str | Path) -> Fixture:
    workspace = resolve_path(workspace_value)
    path = fixture_path(workspace)
    try:
        raw_fixture = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ProbeError("UNPREPARED_WORKSPACE", f"no {PROTOCOL} fixture at {workspace}") from error
    except json.JSONDecodeError as error:
        raise ProbeError("INVALID_FIXTURE", f"fixture is not JSON: {path}") from error

    if raw_fixture.get("protocol") != PROTOCOL:
        raise ProbeError("INVALID_FIXTURE", f"fixture does not declare {PROTOCOL}")
    if raw_fixture.get("workspace") != str(workspace):
        raise ProbeError("INVALID_FIXTURE", "fixture workspace does not match its directory")
    host = raw_fixture.get("host")
    nonce = raw_fixture.get("nonce")
    execute_argv = raw_fixture.get("execute_argv")
    if (
        host not in SUPPORTED_HOSTS
        or not isinstance(nonce, str)
        or not isinstance(execute_argv, list)
        or not all(isinstance(argument, str) for argument in execute_argv)
    ):
        raise ProbeError("INVALID_FIXTURE", "fixture has unsupported host, nonce, or execute command")
    return Fixture(host=host, nonce=nonce, workspace=workspace, execute_argv=tuple(execute_argv))


def open_database(workspace: Path) -> sqlite3.Connection:
    path = database_path(workspace)
    database = sqlite3.connect(path)
    database.row_factory = sqlite3.Row
    database.execute("PRAGMA foreign_keys = ON")
    database.execute("PRAGMA busy_timeout = 250")
    database.executescript(
        """
        CREATE TABLE IF NOT EXISTS events (
          arrival_id TEXT PRIMARY KEY,
          source_type TEXT NOT NULL CHECK(source_type IN ('native', 'synthetic')),
          host TEXT NOT NULL,
          event_type TEXT NOT NULL,
          cwd TEXT NOT NULL,
          session_id TEXT,
          native_tool_use_id TEXT,
          native_tool_name TEXT,
          tool_input_json TEXT,
          raw_payload_json TEXT NOT NULL,
          received_at TEXT NOT NULL,
          receipt_status TEXT NOT NULL,
          receipt_reason TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS receipts (
          receipt_id INTEGER PRIMARY KEY AUTOINCREMENT,
          arrival_id TEXT NOT NULL UNIQUE REFERENCES events(arrival_id),
          host TEXT NOT NULL,
          nonce TEXT NOT NULL,
          native_tool_use_id TEXT NOT NULL,
          received_at TEXT NOT NULL,
          consumed_at TEXT,
          consumed_by_invocation_id TEXT,
          UNIQUE(host, native_tool_use_id)
        );
        CREATE TABLE IF NOT EXISTS invocations (
          invocation_id TEXT PRIMARY KEY,
          host TEXT NOT NULL,
          nonce TEXT NOT NULL,
          receipt_id INTEGER NOT NULL UNIQUE REFERENCES receipts(receipt_id),
          started_at TEXT NOT NULL,
          finished_at TEXT,
          status TEXT NOT NULL CHECK(status IN ('started', 'finished', 'failed')),
          artifact_digest TEXT,
          artifact_path TEXT,
          failure_code TEXT
        );
        """
    )
    database.commit()
    return database


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def native_identifier(payload: dict[str, Any]) -> str | None:
    for field_name in ("tool_use_id", "tool_call_id", "call_id"):
        value = payload.get(field_name)
        if isinstance(value, str) and value:
            return value
    return None


def command_from_payload(payload: dict[str, Any]) -> str | None:
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    command = tool_input.get("command")
    return command if isinstance(command, str) else None


def receipt_reason(fixture: Fixture, source_type: str, payload: dict[str, Any]) -> str:
    if source_type != "native":
        return "synthetic_event"
    if payload.get("hook_event_name") != "PreToolUse":
        return "not_pre_tool_use"
    if payload.get("tool_name") not in SUPPORTED_NATIVE_TOOL_NAMES:
        return "unsupported_tool_name"
    payload_cwd = payload.get("cwd")
    if not isinstance(payload_cwd, str) or resolve_path(payload_cwd) != fixture.workspace:
        return "cwd_does_not_match_fixture_root"
    if native_identifier(payload) is None:
        return "missing_stable_native_id"
    command = command_from_payload(payload)
    if command is None:
        return "missing_tool_command"
    try:
        command_argv = tuple(shlex.split(command))
    except ValueError:
        return "invalid_tool_command"
    if command_argv != fixture.execute_argv:
        return "command_does_not_match_fixture"
    return "ready"


def receive_event(
    workspace_value: str | Path,
    host: str,
    source_type: str,
    payload: dict[str, Any],
    *,
    now: Callable[[], str] = utc_now,
) -> dict[str, Any]:
    """Record one validated hook payload and optionally create a native receipt."""

    fixture = read_fixture(workspace_value)
    if host != fixture.host:
        raise ProbeError("HOST_MISMATCH", f"fixture host is {fixture.host}, not {host}")
    if source_type not in {"native", "synthetic"}:
        raise ProbeError("INVALID_SOURCE", "source must be native or synthetic")
    if not isinstance(payload, dict):
        raise ProbeError("INVALID_PAYLOAD", "hook input must be a JSON object")

    payload_host = payload.get("host")
    if payload_host is not None and payload_host != host:
        raise ProbeError("EVENT_HOST_MISMATCH", f"event host is not {host}")
    if source_type == "synthetic" and payload_host != host:
        raise ProbeError("SYNTHETIC_HOST_REQUIRED", "synthetic events must explicitly name their host")

    payload_cwd = payload.get("cwd")
    if not isinstance(payload_cwd, str):
        raise ProbeError("MISSING_CWD", "hook event did not provide cwd")
    event_cwd = resolve_path(payload_cwd)
    if not is_within(event_cwd, fixture.workspace):
        raise ProbeError("CWD_OUTSIDE_WORKSPACE", "hook cwd is outside this prepared fixture")

    event_type = payload.get("hook_event_name")
    if not isinstance(event_type, str):
        raise ProbeError("MISSING_EVENT_TYPE", "hook event did not provide hook_event_name")

    arrival_id = secrets.token_urlsafe(18)
    received_at = now()
    receipt_state = receipt_reason(fixture, source_type, payload)
    tool_input = payload.get("tool_input")
    stable_id = native_identifier(payload)
    database = open_database(fixture.workspace)
    try:
        with database:
            database.execute(
                """INSERT INTO events(
                arrival_id, source_type, host, event_type, cwd, session_id,
                native_tool_use_id, native_tool_name, tool_input_json, raw_payload_json,
                received_at, receipt_status, receipt_reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    arrival_id,
                    source_type,
                    host,
                    event_type,
                    str(event_cwd),
                    payload.get("session_id") if isinstance(payload.get("session_id"), str) else None,
                    stable_id,
                    payload.get("tool_name") if isinstance(payload.get("tool_name"), str) else None,
                    json_text(tool_input) if tool_input is not None else None,
                    json_text(payload),
                    received_at,
                    "recorded" if receipt_state == "ready" else "ignored",
                    receipt_state,
                ),
            )
            if receipt_state == "ready":
                try:
                    database.execute(
                        """INSERT INTO receipts(
                        arrival_id, host, nonce, native_tool_use_id, received_at)
                        VALUES (?, ?, ?, ?, ?)""",
                        (arrival_id, host, fixture.nonce, stable_id, received_at),
                    )
                except sqlite3.IntegrityError:
                    database.execute(
                        """UPDATE events
                        SET receipt_status = 'duplicate_native_id',
                            receipt_reason = 'stable_native_id_already_received'
                        WHERE arrival_id = ?""",
                        (arrival_id,),
                    )
                    receipt_state = "duplicate_native_id"
    finally:
        database.close()

    return {
        "protocol": PROTOCOL,
        "arrival_id": arrival_id,
        "source_type": source_type,
        "receipt_status": receipt_state,
    }


def write_result(workspace: Path) -> tuple[Path, str]:
    """Create the experiment's only business artifact without following an existing path."""

    result_path = workspace / RESULT_FILE
    temporary_path = state_directory(workspace) / "result.txt.tmp"
    if result_path.exists() or result_path.is_symlink():
        raise ProbeError("RESULT_EXISTS", f"business artifact already exists: {result_path}")
    if temporary_path.exists() or temporary_path.is_symlink():
        raise ProbeError("TEMP_RESULT_EXISTS", f"temporary artifact already exists: {temporary_path}")

    content = b"host-observation-probe-v1\n"
    descriptor = os.open(temporary_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(content)
    os.replace(temporary_path, result_path)
    return result_path, hashlib.sha256(content).hexdigest()


def consume_receipt_and_start(
    fixture: Fixture,
    current_time: str,
) -> tuple[str, int]:
    database = open_database(fixture.workspace)
    try:
        database.execute("BEGIN IMMEDIATE")
        cutoff = parse_timestamp(current_time) - timedelta(seconds=RECEIPT_MAX_AGE_SECONDS)
        receipt = database.execute(
            """SELECT receipt_id, received_at FROM receipts
            WHERE host = ? AND nonce = ? AND consumed_at IS NULL
            ORDER BY receipt_id DESC LIMIT 1""",
            (fixture.host, fixture.nonce),
        ).fetchone()
        if receipt is None:
            database.rollback()
            raise ProbeError("MISSING_NATIVE_RECEIPT", "no unconsumed native PreToolUse receipt")
        if parse_timestamp(str(receipt["received_at"])) < cutoff:
            database.rollback()
            raise ProbeError("STALE_NATIVE_RECEIPT", "native PreToolUse receipt is too old")

        invocation_id = secrets.token_urlsafe(18)
        updated = database.execute(
            """UPDATE receipts
            SET consumed_at = ?, consumed_by_invocation_id = ?
            WHERE receipt_id = ? AND consumed_at IS NULL""",
            (current_time, invocation_id, receipt["receipt_id"]),
        )
        if updated.rowcount != 1:
            database.rollback()
            raise ProbeError("RECEIPT_ALREADY_CONSUMED", "native receipt was consumed concurrently")
        database.execute(
            """INSERT INTO invocations(
            invocation_id, host, nonce, receipt_id, started_at, status)
            VALUES (?, ?, ?, ?, ?, 'started')""",
            (invocation_id, fixture.host, fixture.nonce, receipt["receipt_id"], current_time),
        )
        database.commit()
        return invocation_id, int(receipt["receipt_id"])
    except Exception:
        if database.in_transaction:
            database.rollback()
        raise
    finally:
        database.close()


def finish_invocation(
    fixture: Fixture,
    invocation_id: str,
    *,
    status: str,
    finished_at: str,
    artifact_path: Path | None = None,
    artifact_digest: str | None = None,
    failure_code: str | None = None,
) -> None:
    database = open_database(fixture.workspace)
    try:
        with database:
            updated = database.execute(
                """UPDATE invocations
                SET finished_at = ?, status = ?, artifact_path = ?, artifact_digest = ?, failure_code = ?
                WHERE invocation_id = ? AND status = 'started'""",
                (
                    finished_at,
                    status,
                    str(artifact_path) if artifact_path is not None else None,
                    artifact_digest,
                    failure_code,
                    invocation_id,
                ),
            )
            if updated.rowcount != 1:
                raise ProbeError("MISSING_STARTED_INVOCATION", "could not finish the started invocation")
    finally:
        database.close()


def discard_result(artifact_path: Path, expected_digest: str) -> None:
    """Remove only the probe result we just wrote if its record cannot be committed."""

    if not artifact_path.is_file() or artifact_path.is_symlink():
        return
    actual_digest = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    if actual_digest == expected_digest:
        artifact_path.unlink()


def execute(
    workspace_value: str | Path,
    nonce: str,
    *,
    now: Callable[[], str] = utc_now,
) -> dict[str, Any]:
    """Run the one business action after atomically consuming a fresh receipt."""

    fixture = read_fixture(workspace_value)
    if nonce != fixture.nonce:
        raise ProbeError("NONCE_MISMATCH", "execute nonce does not match this fixture")
    if Path.cwd().resolve() != fixture.workspace:
        raise ProbeError("EXECUTE_CWD_MISMATCH", "execute must run from the fixture workspace root")

    started_at = now()
    invocation_id, receipt_id = consume_receipt_and_start(fixture, started_at)
    try:
        artifact_path, artifact_digest = write_result(fixture.workspace)
    except ProbeError as error:
        finish_invocation(
            fixture,
            invocation_id,
            status="failed",
            finished_at=now(),
            failure_code=error.code,
        )
        raise
    except OSError as error:
        finish_invocation(
            fixture,
            invocation_id,
            status="failed",
            finished_at=now(),
            failure_code="RESULT_WRITE_FAILED",
        )
        raise ProbeError("RESULT_WRITE_FAILED", str(error)) from error

    try:
        finish_invocation(
            fixture,
            invocation_id,
            status="finished",
            finished_at=now(),
            artifact_path=artifact_path,
            artifact_digest=artifact_digest,
        )
    except (OSError, ProbeError, sqlite3.Error) as error:
        discard_result(artifact_path, artifact_digest)
        raise ProbeError("FINISH_RECORD_FAILED", str(error)) from error
    return {
        "protocol": PROTOCOL,
        "invocation_id": invocation_id,
        "receipt_id": receipt_id,
        "artifact": str(artifact_path),
        "artifact_digest": artifact_digest,
    }


def command_text(argv: tuple[str, ...] | list[str]) -> str:
    return shlex.join(argv)


def hook_configuration_path(fixture: Fixture) -> Path:
    if fixture.host == "codex":
        return fixture.workspace / ".codex" / "hooks.json"
    return fixture.workspace / ".codebuddy" / "settings.json"


def hook_configuration(fixture: Fixture) -> tuple[Path, dict[str, Any]]:
    receive_command = command_text(
        (
            sys.executable,
            str(Path(__file__).resolve()),
            "receive",
            "--workspace",
            str(fixture.workspace),
            "--host",
            fixture.host,
        )
    )
    tool_matcher = "^Bash$" if fixture.host == "codex" else "^(Bash|exec_command)$"
    configuration = {
        "description": f"{PROTOCOL} isolated {fixture.host} PreToolUse observation fixture.",
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": tool_matcher,
                    "hooks": [
                        {
                            "type": "command",
                            "command": receive_command,
                            "timeout": 5,
                        }
                    ],
                }
            ]
        },
    }
    return hook_configuration_path(fixture), configuration


def current_hook_configuration(fixture: Fixture) -> tuple[Path, dict[str, Any]]:
    """Read the fixture configuration as it exists now, including a disabled hooks object."""

    configuration_path = hook_configuration_path(fixture)
    try:
        configuration_text = configuration_path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise ProbeError(
            "MISSING_HOOK_CONFIGURATION",
            f"hook configuration does not exist: {configuration_path}",
        ) from error
    except OSError as error:
        raise ProbeError(
            "HOOK_CONFIGURATION_UNAVAILABLE",
            f"could not read hook configuration: {configuration_path}",
        ) from error

    try:
        configuration = json.loads(configuration_text)
    except json.JSONDecodeError as error:
        raise ProbeError(
            "INVALID_HOOK_CONFIGURATION",
            f"hook configuration is not JSON: {configuration_path}",
        ) from error
    if not isinstance(configuration, dict):
        raise ProbeError(
            "INVALID_HOOK_CONFIGURATION",
            "hook configuration must be a JSON object",
        )
    return configuration_path, configuration


def skill_text(fixture: Fixture) -> str:
    execute_command = command_text(fixture.execute_argv)
    return f"""---
name: {SKILL_NAME}
description: Run the isolated {PROTOCOL} business-command observation fixture.
---

# Host observation probe

Run this exact business command through the host shell tool while its working directory is this
fixture workspace:

```sh
{execute_command}
```

This writes only `result.txt` after the configured native `PreToolUse` hook has synchronously
recorded a matching receipt. Do not invoke the receiver directly from this skill.
"""


def prepare(workspace_value: str | Path, host: str) -> dict[str, Any]:
    if host not in SUPPORTED_HOSTS:
        raise ProbeError("UNSUPPORTED_HOST", "host must be codex or workbuddy")
    workspace = resolve_path(workspace_value)
    if workspace.exists() and any(workspace.iterdir()):
        raise ProbeError(
            "WORKSPACE_NOT_EMPTY",
            f"refusing to alter non-empty workspace: {workspace}",
        )
    workspace.mkdir(parents=True, exist_ok=True)
    state_directory(workspace).mkdir()
    nonce = secrets.token_urlsafe(24)
    execute_argv = (
        sys.executable,
        str(Path(__file__).resolve()),
        "execute",
        "--workspace",
        str(workspace),
        "--nonce",
        nonce,
    )
    fixture = Fixture(host=host, nonce=nonce, workspace=workspace, execute_argv=execute_argv)
    fixture_path(workspace).write_text(
        json.dumps(
            {
                "protocol": PROTOCOL,
                "host": host,
                "nonce": nonce,
                "workspace": str(workspace),
                "execute_argv": list(execute_argv),
                "receipt_max_age_seconds": RECEIPT_MAX_AGE_SECONDS,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    database = open_database(workspace)
    database.close()

    skill_directory = workspace / ".claude" / "skills" / SKILL_NAME
    skill_directory.mkdir(parents=True)
    (skill_directory / "SKILL.md").write_text(skill_text(fixture), encoding="utf-8")
    link_directory = (
        workspace / ".agents" / "skills"
        if host == "codex"
        else workspace / ".codebuddy" / "skills"
    )
    link_directory.mkdir(parents=True)
    (link_directory / SKILL_NAME).symlink_to(
        f"../../.claude/skills/{SKILL_NAME}", target_is_directory=True
    )

    configuration_path, configuration = hook_configuration(fixture)
    configuration_path.parent.mkdir(parents=True, exist_ok=True)
    configuration_path.write_text(
        json.dumps(configuration, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (workspace / "RUNNING.md").write_text(
        "\n".join(
            [
                "# Host observation probe fixture",
                "",
                f"Protocol: `{PROTOCOL}`.",
                "",
                "Open this directory as the host workspace, review and trust its generated hook config,",
                "then invoke the local `host-observation-probe` skill. The skill contains exactly one",
                "business command; do not run its receiver directly.",
                "",
                "`inspect` prints the configured command and the recorded evidence.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return {
        "protocol": PROTOCOL,
        "workspace": str(workspace),
        "host": host,
        "config": str(configuration_path),
        "execute_command": command_text(execute_argv),
    }


def inspect(workspace_value: str | Path) -> dict[str, Any]:
    fixture = read_fixture(workspace_value)
    configuration_path, configuration = current_hook_configuration(fixture)
    database = open_database(fixture.workspace)
    try:
        events = [dict(row) for row in database.execute("SELECT * FROM events ORDER BY rowid")]
        receipts = [dict(row) for row in database.execute("SELECT * FROM receipts ORDER BY receipt_id")]
        invocations = [
            dict(row) for row in database.execute("SELECT * FROM invocations ORDER BY started_at")
        ]
    finally:
        database.close()
    return {
        "protocol": PROTOCOL,
        "workspace": str(fixture.workspace),
        "host": fixture.host,
        "nonce": fixture.nonce,
        "execute_command": command_text(fixture.execute_argv),
        "config_path": str(configuration_path),
        "config": configuration,
        "events": events,
        "receipts": receipts,
        "invocations": invocations,
    }


def load_stdin_json() -> dict[str, Any]:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as error:
        raise ProbeError("INVALID_PAYLOAD", "hook stdin is not valid JSON") from error
    if not isinstance(payload, dict):
        raise ProbeError("INVALID_PAYLOAD", "hook stdin must be a JSON object")
    return payload


def parser() -> argparse.ArgumentParser:
    argument_parser = argparse.ArgumentParser(
        description=f"{PROTOCOL} isolated host observation probe"
    )
    subcommands = argument_parser.add_subparsers(dest="command", required=True)

    preparing = subcommands.add_parser("prepare", help="create an empty isolated host fixture")
    preparing.add_argument("--workspace", required=True)
    preparing.add_argument("--host", required=True, choices=sorted(SUPPORTED_HOSTS))

    receiving = subcommands.add_parser("receive", help="record one hook JSON object from stdin")
    receiving.add_argument("--workspace", required=True)
    receiving.add_argument("--host", required=True, choices=sorted(SUPPORTED_HOSTS))
    receiving.add_argument("--source", default="native", choices=("native", "synthetic"))

    executing = subcommands.add_parser("execute", help="run the one receipt-bound business command")
    executing.add_argument("--workspace", required=True)
    executing.add_argument("--nonce", required=True)

    inspecting = subcommands.add_parser("inspect", help="show fixture configuration and evidence")
    inspecting.add_argument("--workspace", required=True)
    return argument_parser


def main(arguments: list[str] | None = None) -> int:
    parsed = parser().parse_args(arguments)
    try:
        if parsed.command == "prepare":
            response = prepare(parsed.workspace, parsed.host)
        elif parsed.command == "receive":
            response = receive_event(parsed.workspace, parsed.host, parsed.source, load_stdin_json())
        elif parsed.command == "execute":
            response = execute(parsed.workspace, parsed.nonce)
        else:
            response = inspect(parsed.workspace)
    except (OSError, sqlite3.Error) as error:
        response = {
            "protocol": PROTOCOL,
            "ok": False,
            "code": "DATABASE_UNAVAILABLE",
            "message": str(error),
        }
        print(json.dumps(response), file=sys.stderr)
        return 1
    except ProbeError as error:
        response = {
            "protocol": PROTOCOL,
            "ok": False,
            "code": error.code,
            "message": str(error),
        }
        print(json.dumps(response), file=sys.stderr)
        return 1
    success_response = {"protocol": PROTOCOL, "ok": True, **response}
    print(json.dumps(success_response, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
