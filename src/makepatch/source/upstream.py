"""Bare cache of the upstream repository."""

from __future__ import annotations

import re
from pathlib import Path

from makepatch import git
from makepatch.config import SourceConfig
from makepatch.errors import MakepatchError

_FULL_SHA = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")


def cache_dir(cfg: SourceConfig) -> Path:
    return cfg.state_dir / "upstream.git"


def _has_commit(repo: Path, rev: str) -> bool:
    return git.run(["cat-file", "-e", f"{rev}^{{commit}}"], repo, check=False).returncode == 0


def resolve(cfg: SourceConfig, *, offline: bool = False) -> tuple[Path, str]:
    """Make sure the configured ref is in the cache and return (cache, commit)."""
    repo = cache_dir(cfg)
    if not (repo / "HEAD").is_file():
        repo.mkdir(parents=True, exist_ok=True)
        git.run(["init", "--bare", "--quiet"], repo)

    ref = cfg.ref
    pinned = f"refs/makepatch/pinned/{ref}" if not _FULL_SHA.match(ref) else None
    if _FULL_SHA.match(ref) and _has_commit(repo, ref):
        return repo, ref
    if offline:
        if pinned and _has_commit(repo, pinned):
            return repo, git.out(["rev-parse", f"{pinned}^{{commit}}"], repo)
        raise MakepatchError(f"upstream ref {ref!r} is not cached and fetching is disabled")

    proc = git.run(["fetch", "--no-tags", "--quiet", cfg.upstream, ref], repo, check=False, network=True)
    if proc.returncode != 0:
        if pinned and _has_commit(repo, pinned):
            # Fall back to what was fetched before (e.g. offline rebuilds).
            return repo, git.out(["rev-parse", f"{pinned}^{{commit}}"], repo)
        raise git.GitError(["fetch", cfg.upstream, ref], proc.returncode, proc.stdout, proc.stderr)
    commit = git.out(["rev-parse", "FETCH_HEAD^{commit}"], repo)
    if _FULL_SHA.match(ref) and commit != ref:
        raise MakepatchError(f"fetched {commit} but {ref} was requested")
    # Keep the object reachable so that gc never drops it.
    git.run(["update-ref", pinned or f"refs/makepatch/commits/{commit}", commit], repo)
    return repo, commit
