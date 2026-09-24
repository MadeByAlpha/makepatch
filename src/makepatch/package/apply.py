"""Package patch mode: applying ``git diff`` patches to installed packages.

Files are never modified in place. Every touched file is copied into a
staging directory next to the site directory, patched there with
``git apply`` and moved back with :func:`os.replace`. The replaced path gets
a new inode, so files hardlinked (or symlinked) into uv's cache stay intact.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from makepatch import git
from makepatch.config import PackageConfig
from makepatch.errors import MakepatchError
from makepatch.package.dist import (
    MARKER,
    SAVED_PATCH,
    Distribution,
    Environment,
    atomic_write,
    normalize,
    record_hash,
    sha256_file,
)


@dataclass(frozen=True)
class PatchFile:
    path: Path
    key: str
    version: str

    @property
    def sha256(self) -> str:
        return sha256_file(self.path)


def patch_file_name(dist: Distribution) -> str:
    return f"{dist.spec}.patch"


def discover(cfg: PackageConfig) -> dict[str, PatchFile]:
    found: dict[str, PatchFile] = {}
    if not cfg.patches_dir.is_dir():
        return found
    for path in sorted(cfg.patches_dir.glob("*.patch")):
        stem = path.name[: -len(".patch")]
        name, sep, version = stem.partition("@")
        if not sep or not name or not version:
            raise MakepatchError(f"{path.name}: patch files must be named <package>@<version>.patch")
        key = normalize(name)
        if key in found:
            raise MakepatchError(f"more than one patch for {key}: {found[key].path.name}, {path.name}")
        found[key] = PatchFile(path, key, version)
    return found


# -- low level -------------------------------------------------------------------


def _check_paths(paths: list[str], patch: Path) -> None:
    for p in paths:
        pure = PurePosixPath(p)
        if pure.is_absolute() or ".." in pure.parts or pure.parts[:1] == (".git",):
            raise MakepatchError(f"{patch.name}: refusing to touch {p!r}")


def _stage(dist: Distribution, steps: list[tuple[Path, bool]], *, dry_run: bool = False) -> list[str]:
    """Apply ``steps`` ((patch, reverse), ...) to ``dist`` and return touched paths."""
    site = dist.site
    staging = Path(tempfile.mkdtemp(prefix=".makepatch-", dir=site))
    try:
        touched: list[str] = []
        for patch, _ in steps:
            paths = git.patch_paths(patch, staging)
            _check_paths(paths, patch)
            touched += paths
        touched = list(dict.fromkeys(touched))
        for rel in touched:
            src = site / rel
            if src.is_file():
                dst = staging / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
        for patch, reverse in steps:
            args = ["apply", "--whitespace=nowarn"] + (["--reverse"] if reverse else []) + [str(patch)]
            proc = git.run(args, staging, check=False)
            if proc.returncode != 0:
                direction = "revert" if reverse else "apply"
                raise MakepatchError(
                    f"cannot {direction} {patch.name} to {dist.spec}:\n{(proc.stderr or proc.stdout).strip()}"
                )
        if dry_run:
            return touched
        for rel in touched:
            staged, target = staging / rel, site / rel
            if staged.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staged, target)
            elif target.is_file() or target.is_symlink():
                target.unlink()
            if rel.endswith(".py"):
                _drop_bytecode(target)
        return touched
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _drop_bytecode(source: Path) -> None:
    cache = source.parent / "__pycache__"
    if cache.is_dir():
        for pyc in cache.glob(f"{source.stem}.*.pyc"):
            with contextlib.suppress(OSError):
                pyc.unlink()


def _update_record(dist: Distribution, touched: list[str], marker: bool) -> None:
    rows = dist.read_record()
    info = dist.dist_info.name
    own = {f"{info}/{MARKER}", f"{info}/{SAVED_PATCH}"}
    touched_set = set(touched)
    out: list[list[str]] = []
    for row in rows:
        path = row[0].replace("\\", "/")
        if path in touched_set or path in own:
            continue
        if path.endswith(".pyc") and not (dist.site / path).exists():
            continue
        out.append(row)
    for rel in touched:
        target = dist.site / rel
        if target.is_file():
            out.append([rel, *record_hash(target)])
    if marker:
        out += [[f"{info}/{MARKER}", "", ""], [f"{info}/{SAVED_PATCH}", "", ""]]
    record = f"{info}/RECORD"
    out.sort(key=lambda r: (r[0] == record, r[0]))
    dist.write_record(out)


# -- operations --------------------------------------------------------------------


def apply_patch(dist: Distribution, patch: PatchFile, *, dry_run: bool = False) -> str:
    """Bring ``dist`` to the state described by ``patch``. Returns what was done."""
    if dist.is_editable:
        raise MakepatchError(f"{dist.spec} is an editable install; patch its sources directly")
    if dist.version != patch.version:
        raise MakepatchError(
            f"{patch.path.name} targets {patch.key} {patch.version}, but {dist.version} is installed"
        )
    sha = patch.sha256
    marker = dist.marker()
    if marker and marker.get("sha256") == sha:
        return "unchanged"
    saved = dist.dist_info / SAVED_PATCH
    steps: list[tuple[Path, bool]] = []
    if marker and saved.is_file():
        steps.append((saved, True))
    steps.append((patch.path, False))
    touched = _stage(dist, steps, dry_run=dry_run)
    if dry_run:
        return "would apply"
    atomic_write(saved, patch.path.read_bytes())
    atomic_write(
        dist.dist_info / MARKER,
        json.dumps({"patch": patch.path.name, "sha256": sha, "files": touched}, indent=2).encode() + b"\n",
    )
    _update_record(dist, touched, marker=True)
    return "reapplied" if marker else "applied"


def revert(dist: Distribution, *, dry_run: bool = False) -> bool:
    saved = dist.dist_info / SAVED_PATCH
    if dist.marker() is None or not saved.is_file():
        return False
    touched = _stage(dist, [(saved, True)], dry_run=dry_run)
    if dry_run:
        return True
    (dist.dist_info / MARKER).unlink()
    saved.unlink()
    _update_record(dist, touched, marker=False)
    return True


@dataclass
class Result:
    key: str
    action: str
    error: str | None = None


def apply_all(cfg: PackageConfig, env: Environment, *, dry_run: bool = False) -> list[Result]:
    patches = discover(cfg)
    dists: dict[str, Distribution] = {}
    for dist in env.distributions():
        dists.setdefault(dist.key, dist)
    results: list[Result] = []
    for key, patch in patches.items():
        dist = dists.get(key)
        try:
            if dist is None:
                raise MakepatchError(f"{patch.path.name}: {key} is not installed in {env.prefix}")
            results.append(Result(key, apply_patch(dist, patch, dry_run=dry_run)))
        except MakepatchError as exc:
            results.append(Result(key, "failed", str(exc)))
    for key, dist in dists.items():
        if key not in patches and dist.marker() is not None:
            try:
                revert(dist, dry_run=dry_run)
                results.append(Result(key, "would revert" if dry_run else "reverted"))
            except MakepatchError as exc:
                results.append(Result(key, "failed", str(exc)))
    if not dry_run:
        write_state(cfg, env)
    return results


def write_state(cfg: PackageConfig, env: Environment) -> None:
    """Record the project in the environment so that the startup hook can find it."""
    applied = {}
    for dist in env.distributions():
        marker = dist.marker()
        if marker:
            applied[dist.key] = {"sha256": marker.get("sha256"), "dist_info": str(dist.dist_info)}
    state = {
        "version": 1,
        "project": str(cfg.root),
        "patches_dir": str(cfg.patches_dir),
        "applied": applied,
    }
    data = json.dumps(state, indent=2, sort_keys=True).encode() + b"\n"
    try:
        if env.state_file.read_bytes() == data:
            return
    except OSError:
        pass
    atomic_write(env.state_file, data)


@contextlib.contextmanager
def locked(env: Environment, *, blocking: bool = True):
    """Serialize makepatch processes working on the same environment."""
    env.lock_file.parent.mkdir(parents=True, exist_ok=True)
    with open(env.lock_file, "a+b") as fp:
        if os.name == "nt":  # pragma: no cover
            import msvcrt

            mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
            fp.seek(0)
            msvcrt.locking(fp.fileno(), mode, 1)
        else:
            import fcntl

            fcntl.flock(fp.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield
