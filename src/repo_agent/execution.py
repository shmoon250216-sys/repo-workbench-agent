"""Build reviewable execution specifications; this is not a filesystem sandbox."""

import hashlib
import json
import os
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .policy import Denied

IGNORED_DIRS = {
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".runtime",
}
ENV_NAMES = {"SystemRoot", "WINDIR", "PATH", "TEMP", "TMP", "LANG"}


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode()
    ).hexdigest()


def file_digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        for block in iter(lambda: stream.read(65536), b""):
            value.update(block)
        after = os.fstat(stream.fileno())
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ino,
    ):
        raise Denied("File changed while computing execution identity; retry")
    return value.hexdigest()


def workspace_snapshot(root, state_dir):
    """Fingerprint bounded workspace contents, including untracked source/config files.

    Runtime state, caches and dependency directories are excluded, explicitly listed
    in the spec. Oversize or linked workspaces fail closed instead of being truncated.
    """
    root, state_dir = Path(root).resolve(), Path(state_dir).resolve()
    if root == state_dir:
        raise Denied("Session state directory must not equal execution workspace")
    entries, total = [], 0

    def walk_error(error):
        raise Denied(
            "Workspace snapshot could not enumerate every included directory"
        ) from error

    for base, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
        kept = []
        for name in sorted(dirs):
            p = Path(base) / name
            if name.lower() in IGNORED_DIRS or p.resolve() == state_dir:
                continue
            if p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction()):
                raise Denied(
                    "Linked directories are not supported in execution snapshots"
                )
            kept.append(name)
        dirs[:] = kept
        for name in sorted(files):
            p = Path(base) / name
            info = p.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
                raise Denied(
                    "Linked or special files are not supported in execution snapshots"
                )
            total += info.st_size
            if len(entries) >= 5000 or info.st_size > 8_000_000 or total > 50_000_000:
                raise Denied(
                    "Workspace snapshot exceeds limits (5000 files / 8 MB per file / 50 MB total)"
                )
            entries.append(
                [p.relative_to(root).as_posix(), info.st_mode, file_digest(p)]
            )
    excluded_state = (
        state_dir.relative_to(root).as_posix()
        if state_dir.is_relative_to(root)
        else None
    )
    return {
        "sha256": digest(entries),
        "file_count": len(entries),
        "bytes": total,
        "excluded_directories": sorted(IGNORED_DIRS),
        "excluded_state_directory": excluded_state,
    }


def execution_environment():
    env = {k: v for k, v in os.environ.items() if k in ENV_NAMES}
    env.update(PYTHONIOENCODING="utf-8", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    return env


def executable_identity(value, env):
    resolved = shutil.which(str(value), path=env.get("PATH", ""))
    if not resolved:
        raise Denied("Execution executable unavailable")
    selected = Path(resolved).absolute()
    path = selected.resolve(strict=True)
    identity = {
        "path": str(selected),
        "real_path": str(path),
        "sha256": file_digest(path),
    }
    config = selected.parent.parent / "pyvenv.cfg"
    if config.is_file():
        identity["venv_config_sha256"] = file_digest(config)
    return identity


def docker_metadata(executable, env):
    try:

        def query(args):
            result = subprocess.run(
                [executable, *args],
                env=env,
                capture_output=True,
                timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode != 0 or len(result.stdout) > 4096:
                raise Denied(
                    "Docker metadata unavailable; prepare a local test image first"
                )
            return result.stdout.decode().strip()

        image = query(["image", "inspect", "python:3.12-slim", "--format", "{{.Id}}"])
        daemon = query(["info", "--format", "{{.ID}}"])
        if (
            not image.startswith("sha256:")
            or len(image) != 71
            or any(c not in "0123456789abcdef" for c in image[7:])
            or not daemon
        ):
            raise Denied("Invalid Docker execution identity")
        return {"image_id": image, "daemon_id": daemon}
    except (OSError, subprocess.SubprocessError, UnicodeError):
        raise Denied("Docker metadata lookup failed; no host fallback") from None


@dataclass
class ExecutionPlan:
    spec: dict
    env: dict


def build_plan(root, state_dir, sid, call_id, runner, backend, python):
    if backend not in {"trusted-local", "docker"}:
        raise Denied("Test execution disabled or unknown backend")
    if runner not in {"unittest", "pytest"}:
        raise Denied("Unsupported test runner")
    env = execution_environment()
    container = "repo-agent-" + digest([sid, call_id])[:24]
    if backend == "docker":
        if runner != "unittest":
            raise Denied("Default Docker image supports unittest only")
        executable = executable_identity("docker", env)
        runtime = {"executable": executable, **docker_metadata(executable["path"], env)}
        command = [
            executable["path"],
            "run",
            "--rm",
            "--pull=never",
            "--name",
            container,
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--memory=256m",
            "--cpus=1",
            "--pids-limit=64",
            "--user=65534:65534",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=32m",
            "-v",
            str(Path(root).resolve()) + ":/workspace:ro",
            "-w",
            "/workspace",
            runtime["image_id"],
            "python",
            "-I",
            "-B",
            "-m",
            "unittest",
            "discover",
            "-s",
            "tests",
            "-v",
        ]
    else:
        executable = executable_identity(python, env)
        runtime = {"executable": executable}
        command = [executable["path"], "-I", "-m"] + (
            ["unittest", "discover", "-s", "tests", "-v"]
            if runner == "unittest"
            else ["pytest", "-q", "-p", "no:cacheprovider"]
        )
    spec = {
        "schema_version": 1,
        "session_id": sid,
        "call_id": call_id,
        "runner": runner,
        "backend": backend,
        "command": command,
        "workspace": str(Path(root).resolve()),
        "snapshot": workspace_snapshot(root, state_dir),
        "runtime": runtime,
        "environment_sha256": digest(env),
        "timeout_seconds": 30,
        "container_name": container if backend == "docker" else None,
    }
    return ExecutionPlan(spec, env)
