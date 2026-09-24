"""Loading of ``[tool.makepatch]`` from ``pyproject.toml``."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

from makepatch.errors import MakepatchError

STATE_DIR = ".makepatch"


def find_project_root(start: Path | None = None) -> Path:
    start = (start or Path.cwd()).resolve()
    for directory in (start, *start.parents):
        if (directory / "pyproject.toml").is_file():
            return directory
    raise MakepatchError(f"no pyproject.toml found in {start} or any parent directory")


def load_tool_table(root: Path) -> dict[str, Any]:
    path = root / "pyproject.toml"
    try:
        with path.open("rb") as fp:
            data = tomllib.load(fp)
    except FileNotFoundError:
        raise MakepatchError(f"{path} does not exist") from None
    except tomllib.TOMLDecodeError as exc:
        raise MakepatchError(f"{path}: {exc}") from None
    table = data.get("tool", {}).get("makepatch", {})
    if not isinstance(table, dict):
        raise MakepatchError("[tool.makepatch] must be a table")
    return table


def _safe_relpath(value: str, what: str) -> str:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts:
        raise MakepatchError(f"{what} must be a relative path without '..': {value!r}")
    return path.as_posix().rstrip("/") if path.as_posix() != "." else "."


def _patterns(table: dict[str, Any], key: str) -> list[str]:
    value = table.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise MakepatchError(f"[tool.makepatch.source] {key} must be a list of gitignore-style patterns")
    return value


@dataclass
class SourceConfig:
    root: Path
    upstream: str
    ref: str
    include: dict[str, str] = field(default_factory=dict)
    exclude: list[str] = field(default_factory=list)
    work_exclude: list[str] = field(default_factory=list)
    work_dir: Path = Path()
    patches_dir: Path = Path()

    @property
    def state_dir(self) -> Path:
        return self.root / STATE_DIR

    @property
    def source_patches(self) -> Path:
        return self.patches_dir / "sources"

    @property
    def feature_patches(self) -> Path:
        return self.patches_dir / "features"

    @classmethod
    def load(cls, root: Path, table: dict[str, Any] | None = None) -> "SourceConfig":
        if table is None:
            table = load_tool_table(root)
        src = table.get("source")
        if not isinstance(src, dict):
            raise MakepatchError("missing [tool.makepatch.source] table in pyproject.toml")
        upstream = src.get("upstream")
        ref = src.get("ref")
        if not isinstance(upstream, str) or not upstream:
            raise MakepatchError("[tool.makepatch.source] upstream must be a git URL")
        if not isinstance(ref, str) or not ref:
            raise MakepatchError("[tool.makepatch.source] ref must be a commit, tag or branch")
        include = src.get("include", {})
        if not isinstance(include, dict) or not all(isinstance(v, str) for v in include.values()):
            raise MakepatchError("[tool.makepatch.source] include must map upstream paths to wheel paths")
        include = {_safe_relpath(k, "include key"): _safe_relpath(v, "include value") for k, v in include.items()}
        exclude = _patterns(src, "exclude")
        work_exclude = _patterns(src, "work-exclude")
        # A local path upstream is resolved against the project root.
        if not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", upstream) and not re.match(r"^[^/]+@[^/]+:", upstream):
            upstream = str((root / upstream).resolve())
        return cls(
            root=root,
            upstream=upstream,
            ref=ref,
            include=include,
            exclude=exclude,
            work_exclude=work_exclude,
            work_dir=root / _safe_relpath(src.get("work-dir", "work"), "work-dir"),
            patches_dir=root / _safe_relpath(src.get("patches-dir", "patches"), "patches-dir"),
        )


@dataclass
class PackageConfig:
    root: Path
    patches_dir: Path

    @property
    def edit_dir(self) -> Path:
        return self.root / STATE_DIR / "edit"

    @classmethod
    def load(cls, root: Path) -> "PackageConfig":
        table = load_tool_table(root).get("packages", {})
        if not isinstance(table, dict):
            raise MakepatchError("[tool.makepatch.packages] must be a table")
        patches_dir = _safe_relpath(table.get("patches-dir", "patches/packages"), "patches-dir")
        return cls(root=root, patches_dir=root / patches_dir)
