"""Create and verify a clean source archive for the workflow engine."""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import zipfile
import fnmatch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT / "self-healing-workflow-engine-aws-ready.zip"
PREFIX = "Self-Healing-Workflow-Engine"
EXCLUDED_DIRS = {
    ".git", ".pytest_cache", ".ruff_cache", ".venv", ".local-python",
    "__pycache__", "node_modules", "dist", "build", "artifacts", "logs",
    ".next", ".vite", "*.egg-info",
    "self-healing-workflow-engine-aws-ready",
}
EXCLUDED_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".log", ".pyc", ".pyo", ".zip"}
SECRET_PATTERNS = [
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(rb"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(rb"\bxox[baprs]-[A-Za-z0-9-]{30,}\b"),
    re.compile(rb"\bsk-[A-Za-z0-9]{32,}\b"),
]


def excluded(path: Path) -> bool:
    relative = path.relative_to(ROOT)
    if any(part.casefold() in EXCLUDED_DIRS or fnmatch.fnmatch(part.casefold(), "*.egg-info")
           for part in relative.parts):
        return True
    if path.suffix.casefold() in EXCLUDED_SUFFIXES:
        return True
    if path.name.casefold().startswith(".env") and path.name.casefold() != ".env.example":
        return True
    return False


def source_files() -> list[Path]:
    files: list[Path] = []
    for directory, subdirs, filenames in os.walk(ROOT, topdown=True, followlinks=False):
        folder = Path(directory)
        subdirs[:] = [name for name in subdirs
                      if name.casefold() not in EXCLUDED_DIRS
                      and not fnmatch.fnmatch(name.casefold(), "*.egg-info")
                      and not (folder / name).is_symlink()]
        files.extend(folder / name for name in filenames
                     if not (folder / name).is_symlink() and not excluded(folder / name))
    return sorted(files)


def verify_archive(archive: Path) -> Path:
    with zipfile.ZipFile(archive) as zipped:
        bad = zipped.testzip()
        if bad:
            raise RuntimeError(f"ZIP CRC check failed for {bad}")
        names = zipped.namelist()
        required = {
            f"{PREFIX}/README.md", f"{PREFIX}/pyproject.toml", f"{PREFIX}/alembic.ini",
            f"{PREFIX}/backend/app.py", f"{PREFIX}/frontend/package.json",
            f"{PREFIX}/frontend/pnpm-lock.yaml", f"{PREFIX}/migrations/versions/0002_retry_policy.py",
            f"{PREFIX}/.env.example", f"{PREFIX}/scripts/reliability.py",
            f"{PREFIX}/deploy/aws/workflow-engine.yml", f"{PREFIX}/deploy/aws/deploy.ps1",
            f"{PREFIX}/deploy/aws/README.md", f"{PREFIX}/frontend/nginx.conf.template",
            f"{PREFIX}/docs/vscode-local-setup.md",
            f"{PREFIX}/frontend/Dockerfile", f"{PREFIX}/frontend/.dockerignore",
            f"{PREFIX}/.dockerignore", f"{PREFIX}/docker-compose.yml",
        }
        missing = required - set(names)
        if missing:
            raise RuntimeError(f"Required archive files are missing: {sorted(missing)}")
        for name in names:
            parts = Path(name).parts
            leaf = parts[-1].casefold()
            if any(part.casefold() in {"node_modules", "__pycache__", ".git", ".venv", ".local-python"}
                   for part in parts):
                raise RuntimeError(f"Local dependency/cache directory leaked into archive: {name}")
            if leaf.startswith(".env") and leaf != ".env.example":
                raise RuntimeError(f"Environment secret file leaked into archive: {name}")
            if Path(leaf).suffix in EXCLUDED_SUFFIXES:
                raise RuntimeError(f"Generated/local file leaked into archive: {name}")
            data = zipped.read(name)
            if any(pattern.search(data) for pattern in SECRET_PATTERNS):
                raise RuntimeError(f"Secret-like value detected in {name}")
        extraction = Path(tempfile.mkdtemp(prefix="workflow-archive-verify-"))
        zipped.extractall(extraction)
    return extraction / PREFIX


def run(command: list[str], cwd: Path, env: dict[str, str]) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=cwd, env=env, check=True)


def main() -> None:
    files = source_files()
    with zipfile.ZipFile(ARCHIVE, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=7) as zipped:
        for path in files:
            zipped.write(path, (Path(PREFIX) / path.relative_to(ROOT)).as_posix())
    extracted = verify_archive(ARCHIVE)
    print(f"Archive integrity and contents verified: {ARCHIVE}", flush=True)

    python = sys.executable
    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(extracted) + (os.pathsep + existing_pythonpath if existing_pythonpath else "")
    env["DATABASE_URL"] = f"sqlite+aiosqlite:///{(extracted / 'clean-migration-check.db').as_posix()}"
    run([python, "-m", "alembic", "upgrade", "head"], extracted, env)
    run([python, "-m", "pytest", "-q"], extracted, env)
    print(f"Verified {len(files)} source files in {ARCHIVE.stat().st_size:,} bytes.", flush=True)


if __name__ == "__main__":
    main()
