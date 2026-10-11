"""Small helpers shared by project maintenance commands."""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
SLUG = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")


def validate_slug(value: str) -> str:
    if len(value) > 32 or not SLUG.fullmatch(value):
        raise ValueError("Slug must start with a lowercase letter, contain lowercase letters, numbers or hyphens, and be at most 32 characters.")
    return value


def prompt(label: str, default: str) -> str:
    if not sys.stdin.isatty():
        raise ValueError("Interactive setup needs a terminal; supply --non-interactive options instead.")
    return input(f"{label} [{default}]: ").strip() or default


def pnpm_command() -> list[str]:
    if not shutil.which("pnpm"):
        raise ValueError("Global pnpm is missing. Run npm install --global pnpm@latest, then retry.")
    return ["pnpm"]


def write_atomic(path: Path, content: str, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    permissions = mode if mode is not None else path.stat().st_mode & 0o777 if path.exists() else 0o644
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(content)
        os.chmod(temporary, permissions)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
