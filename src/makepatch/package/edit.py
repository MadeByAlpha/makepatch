"""``pkg edit`` / ``pkg commit``: the pnpm-patch style editing workflow."""

from __future__ import annotations

import shutil
from pathlib import Path

from makepatch import git
from makepatch.config import PackageConfig
from makepatch.errors import MakepatchError
from makepatch.package import apply
from makepatch.package.dist import SAVED_PATCH, Distribution, Environment, normalize

DIFF_ARGS = ("--no-color", "--no-ext-diff", "--binary", "--no-renames")
BASE_SUBJECT = "makepatch: pristine"


def edit_dir(cfg: PackageConfig, dist: Distribution) -> Path:
    return cfg.edit_dir / dist.spec


def edit(cfg: PackageConfig, env: Environment, name: str, *, force: bool = False) -> Path:
    dist = env.find(name)
    if dist.is_editable:
        raise MakepatchError(f"{dist.spec} is an editable install; edit its sources directly")
    target = edit_dir(cfg, dist)
    if target.exists():
        if not force:
            raise MakepatchError(f"{target} already exists; commit it or pass --force to start over")
        shutil.rmtree(target)
    for stale in cfg.edit_dir.glob(f"{dist.key}@*") if cfg.edit_dir.is_dir() else ():
        if force:
            shutil.rmtree(stale)
        else:
            raise MakepatchError(f"{stale} is an edit of another version; remove it or pass --force")
    target.mkdir(parents=True)

    for rel in dist.files():
        src = dist.site / rel
        if src.is_file():
            dst = target / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    git.run(["init", "--quiet"], target)
    (target / ".git" / "info").mkdir(parents=True, exist_ok=True)
    (target / ".git" / "info" / "exclude").write_text("__pycache__/\n*.py[co]\n")

    saved = dist.dist_info / SAVED_PATCH
    patched = dist.marker() is not None and saved.is_file()
    if patched:
        # The installed files carry the current patch: rebuild the pristine
        # tree first, then put the patch back as uncommitted changes.
        git.run(["apply", "--reverse", "--whitespace=nowarn", str(saved)], target)
    git.run(["add", "--all"], target)
    git.run(["commit", "--quiet", "--allow-empty", "--no-verify", "-m", f"{BASE_SUBJECT} {dist.spec}"], target, fixed_date=True)
    if patched:
        git.run(["apply", "--whitespace=nowarn", str(saved)], target)
    return target


def _find_edit(cfg: PackageConfig, name: str) -> Path:
    key = normalize(name)
    matches = sorted(cfg.edit_dir.glob(f"{key}@*")) if cfg.edit_dir.is_dir() else []
    if not matches:
        raise MakepatchError(f"no edit in progress for {name}; run `makepatch pkg edit {name}` first")
    if len(matches) > 1:
        raise MakepatchError(f"several edits in progress for {name}: {', '.join(m.name for m in matches)}")
    return matches[0]


def commit(cfg: PackageConfig, env: Environment, name: str, *, keep: bool = False) -> tuple[Path | None, str]:
    """Turn the edit directory into a patch file and apply it.

    Returns (patch path or None if the edit was empty, apply action).
    """
    dist = env.find(name)
    work = _find_edit(cfg, name)
    if work.name != dist.spec:
        raise MakepatchError(f"{work.name} was edited, but {dist.spec} is installed; run `makepatch pkg edit --force`")
    git.run(["add", "--all"], work)
    diff = git.run(["diff", "--cached", *DIFF_ARGS, "HEAD"], work, binary=True).stdout
    patch_path = cfg.patches_dir / apply.patch_file_name(dist)
    for other in cfg.patches_dir.glob(f"{dist.key}@*.patch") if cfg.patches_dir.is_dir() else ():
        if other != patch_path:
            other.unlink()
    if diff:
        cfg.patches_dir.mkdir(parents=True, exist_ok=True)
        patch_path.write_bytes(diff)
        action = apply.apply_patch(dist, apply.PatchFile(patch_path, dist.key, dist.version))
    else:
        if patch_path.exists():
            patch_path.unlink()
        action = "reverted" if apply.revert(dist) else "unchanged"
    apply.write_state(cfg, env)
    if not keep:
        shutil.rmtree(work)
    return (patch_path if diff else None), action
