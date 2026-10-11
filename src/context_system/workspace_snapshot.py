from __future__ import annotations

import os
from pathlib import Path

from .models import WorkspaceSnapshot

_IGNORED_NAMES = {
    ".git",
    ".venv",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "__pycache__",
    "node_modules",
}

# Hard budget for the file scan. A workspace such as a home folder holds
# hundreds of thousands of files, and the snapshot is rebuilt on every model
# turn, so traversal must stop on its own instead of walking the whole tree.
_MAX_SCANNED_DIRS = 2_000
_MAX_SCANNED_FILES = 20_000

_KEY_FILE_CANDIDATES = (
    "README.md",
    "CLAUDE.md",
    "pyproject.toml",
    "requirements.txt",
    "uv.lock",
    "package.json",
    "Makefile",
)


def build_workspace_snapshot(
    workspace_root: str | Path,
    *,
    cwd: str | Path | None = None,
    top_level_limit: int = 12,
) -> WorkspaceSnapshot:
    root = Path(workspace_root).expanduser().resolve()
    current = Path(cwd).expanduser().resolve() if cwd is not None else root
    if not _is_within(current, root):
        current = root

    entries: list[str] = []
    try:
        children = sorted(root.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except Exception:
        children = []
    for child in children:
        if child.name in _IGNORED_NAMES:
            continue
        marker = "/" if child.is_dir() else ""
        entries.append(f"{child.name}{marker}")
        if len(entries) >= top_level_limit:
            break

    key_files = tuple(name for name in _KEY_FILE_CANDIDATES if (root / name).exists())
    python_file_count, test_file_count = _count_source_files(root)

    return WorkspaceSnapshot(
        workspace_root=root,
        current_directory=current,
        top_level_entries=tuple(entries),
        key_files=key_files,
        python_file_count=python_file_count,
        test_file_count=test_file_count,
    )


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _count_source_files(root: Path) -> tuple[int, int]:
    """Count Python and test files under ``root``.

    ``Path.rglob`` cannot prune subtrees: it still descends into every ignored
    directory before the caller can drop the results, which took minutes when
    the workspace was a home folder. A pruned ``os.walk`` with a hard budget
    keeps the snapshot cheap no matter how large the workspace is.
    """
    python_files = 0
    test_files = 0
    visited_dirs = 0
    visited_files = 0

    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        visited_dirs += 1
        dirnames[:] = [name for name in dirnames if name not in _IGNORED_NAMES]

        for filename in filenames:
            visited_files += 1
            if filename.endswith(".py"):
                python_files += 1
                if filename.startswith("test_"):
                    test_files += 1

        if visited_dirs >= _MAX_SCANNED_DIRS or visited_files >= _MAX_SCANNED_FILES:
            break

    return python_files, test_files
