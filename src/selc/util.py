"""Filesystem, JSON, hashing, and subprocess helpers shared by SELC."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .errors import SelcError

try:  # SELC's supported local hosts are POSIX; retain an explicit fallback for portability.
    import fcntl
except ImportError:  # pragma: no cover - exercised only on unsupported non-POSIX hosts.
    fcntl = None  # type: ignore[assignment]


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def json_hash(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_hash(root: Path) -> str:
    if not root.is_dir():
        raise SelcError(
            "INVALID_PATH", f"directory does not exist: {root}", "choose an existing directory"
        )
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if _is_runtime_cache(path.relative_to(root)):
            continue
        if path.is_symlink():
            digest.update(f"link:{relative}:{os.readlink(path)}".encode("utf-8"))
            continue
        if path.is_file():
            digest.update(f"file:{relative}:".encode("utf-8"))
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _is_runtime_cache(relative: Path) -> bool:
    """Ignore interpreter bytecode caches, which do not change suite content."""

    return "__pycache__" in relative.parts or relative.suffix == ".pyc"


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise SelcError("MISSING_FILE", f"missing file: {path}", "provide a valid path") from error
    except json.JSONDecodeError as error:
        raise SelcError(
            "INVALID_JSON", f"invalid JSON in {path}: {error.msg}", "repair or recreate the file"
        ) from error
    if not isinstance(value, dict):
        raise SelcError(
            "INVALID_JSON", f"expected a JSON object in {path}", "repair or recreate the file"
        )
    return value


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(f"{json.dumps(value, ensure_ascii=False, indent=2)}\n", encoding="utf-8")
    os.replace(temporary, path)


def require_relative_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or value in {"", "."}:
        raise SelcError(
            "UNSAFE_PATH",
            f"path must stay below its root: {value}",
            "use a non-empty relative path",
        )
    return path


def copy_tree(source: Path, destination: Path) -> None:
    if destination.exists():
        raise SelcError(
            "PATH_CONFLICT",
            f"destination already exists: {destination}",
            "choose a new output path",
        )
    shutil.copytree(source, destination, symlinks=True)


def run_argv(arguments: list[str], *, cwd: Path | None = None, timeout: int = 30) -> str:
    completed = _run_process(arguments, cwd=cwd, timeout=timeout)
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip() or "command failed"
        raise SelcError("COMMAND_FAILED", message, "inspect the command input and retry")
    return completed.stdout.strip()


def run_argv_result(
    arguments: list[str], *, cwd: Path | None = None, timeout: int = 30
) -> tuple[int, str, str]:
    """Run an argv command while retaining a provider query's explicit status."""

    completed = _run_process(arguments, cwd=cwd, timeout=timeout)
    return completed.returncode, completed.stdout.strip(), completed.stderr.strip()


def _run_process(
    arguments: list[str], *, cwd: Path | None = None, timeout: int = 30
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            arguments,
            cwd=cwd,
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as error:
        raise SelcError(
            "MISSING_DEPENDENCY",
            f"required executable is unavailable: {arguments[0]}",
            "install it or select another provider",
        ) from error
    except subprocess.TimeoutExpired as error:
        raise SelcError(
            "COMMAND_TIMEOUT",
            f"command timed out: {arguments[0]}",
            "inspect the local source and retry",
        ) from error


@contextmanager
def exclusive_lock(path: Path) -> Iterator[None]:
    """Serialize an operation with a process-owned advisory lock.

    The file is intentionally durable for diagnostics, while the operating-system
    lock is released when a process exits abruptly.  A leftover pathname therefore
    never masquerades as a live transaction; the installation journal remains the
    source of truth for recovery.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if fcntl is None:
            raise SelcError(
                "LOCK_UNSUPPORTED",
                "this host cannot provide process-owned suite locks",
                "run SELC on a POSIX host with advisory file locks",
            )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise SelcError(
                "LOCKED",
                f"operation is already active: {path}",
                "wait for the lock owner and retry; do not force-close active tasks or sessions",
            ) from error
        os.ftruncate(descriptor, 0)
        os.write(descriptor, str(os.getpid()).encode("ascii"))
        yield
    finally:
        if fcntl is not None:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


@contextmanager
def temporary_directory(prefix: str) -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix=prefix) as temporary:
        yield Path(temporary)
