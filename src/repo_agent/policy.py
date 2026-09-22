"""Filesystem checks are an application policy, not an OS sandbox."""

from pathlib import Path
import os


class Denied(ValueError):
    pass


BLOCKED = {
    ".git",
    ".ssh",
    ".aws",
    ".azure",
    ".runtime",
    ".venv",
    "node_modules",
    "__pycache__",
}
SECRET_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}
TEXT_SUFFIXES = {
    ".py",
    ".md",
    ".txt",
    ".toml",
    ".json",
    ".yaml",
    ".yml",
    ".ini",
    ".cfg",
    ".html",
    ".css",
    ".js",
    ".ts",
    ".csv",
    ".rst",
}


class Policy:
    def __init__(self, root):
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir():
            raise Denied("Workspace must be a directory")

    def path(self, relative, *, writing=False):
        p = Path(relative)
        if (
            p.is_absolute()
            or p.drive
            or ".." in p.parts
            or ":" in relative
            or "\\" in relative
        ):
            raise Denied("Only relative workspace paths without traversal are allowed")
        for part in p.parts:
            lower = part.lower()
            if (
                lower in BLOCKED
                or lower.startswith(".env")
                or lower in {"credentials", "id_rsa", "id_ed25519"}
            ):
                raise Denied("Protected path")
        candidate = self.root / p
        cursor = self.root
        for part in p.parts:
            cursor = cursor / part
            if cursor.is_symlink() or (
                hasattr(cursor, "is_junction") and cursor.is_junction()
            ):
                raise Denied("Symbolic links and junctions are not allowed")
        resolved = candidate.resolve()
        if not resolved.is_relative_to(self.root):
            raise Denied("Path escapes workspace")
        if resolved.suffix.lower() in SECRET_SUFFIXES:
            raise Denied("Credential file blocked")
        if writing and (
            resolved.suffix.lower() not in TEXT_SUFFIXES or resolved == self.root
        ):
            raise Denied("Only supported text files can be edited")
        if resolved.is_file() and os.stat(resolved).st_nlink > 1:
            raise Denied("Hard-linked files are not supported")
        return resolved

    def files(self):
        result = []
        for base, dirs, names in os.walk(self.root, followlinks=False):
            allowed = []
            for name in sorted(dirs):
                try:
                    self.path((Path(base) / name).relative_to(self.root).as_posix())
                    allowed.append(name)
                except Denied:
                    pass
            dirs[:] = allowed
            for name in sorted(names):
                relative = (Path(base) / name).relative_to(self.root).as_posix()
                try:
                    self.path(relative)
                    result.append(relative)
                except Denied:
                    continue
                if len(result) >= 1500:
                    return result
        return result
