"""Source patch mode: the paperweight-style work repository.

History of the work repository::

    <upstream ref>                 tag makepatch/base
    makepatch: source patches      tag makepatch/sources  (all ``git diff`` patches)
    <feature commit>...            one commit per ``git format-patch`` patch
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from makepatch import git
from makepatch.config import SourceConfig
from makepatch.errors import MakepatchError
from makepatch.source import upstream

BRANCH = "patched"
BASE_TAG = "makepatch/base"
SOURCES_TAG = "makepatch/sources"
SOURCES_SUBJECT = "makepatch: source patches"

FORMAT_PATCH_ARGS = ("-p", "--minimal", "--zero-commit", "--no-numbered")
DIFF_ARGS = ("--no-color", "--no-ext-diff", "--binary", "--no-renames")


def _list_patches(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.rglob("*.patch") if p.is_file())


def _in_progress(work: Path) -> str | None:
    gitdir = Path(git.out(["rev-parse", "--absolute-git-dir"], work))
    if (gitdir / "rebase-apply").exists():
        return "git am" if (gitdir / "rebase-apply" / "applying").exists() else "git rebase"
    if (gitdir / "rebase-merge").exists():
        return "git rebase"
    return None


def setup(cfg: SourceConfig, work: Path | None = None, *, offline: bool = False, force: bool = False) -> Path:
    """(Re)create the work repository and apply every patch.

    Returns the work repository path. If a feature patch does not apply, the
    ``git am`` session is left in place for the user to resolve.
    """
    work = work or cfg.work_dir
    cache, commit = upstream.resolve(cfg, offline=offline)

    if work.exists() and any(work.iterdir()):
        if not (work / ".git").exists():
            raise MakepatchError(f"{work} exists and is not a makepatch work repository")
        if not force:
            dirty = git.out(["status", "--porcelain", "--untracked-files=no"], work)
            if dirty or _in_progress(work):
                raise MakepatchError(
                    f"{work} has uncommitted changes or an unfinished git operation; "
                    "run `makepatch src rebuild` first or pass --force to discard them"
                )
            ahead = git.out(["rev-list", "--count", f"{SOURCES_TAG}..HEAD"], work) if _has_ref(work, SOURCES_TAG) else "0"
            ahead_patches = len(_list_patches(cfg.feature_patches))
            if int(ahead) > ahead_patches:
                raise MakepatchError(
                    f"{work} has {ahead} feature commit(s) but only {ahead_patches} feature patch(es) exist; "
                    "run `makepatch src rebuild` first or pass --force to discard them"
                )
        _abort_in_progress(work)
    else:
        work.mkdir(parents=True, exist_ok=True)
        git.run(["init", "--quiet"], work)

    git.run(["fetch", "--no-tags", "--quiet", str(cache), commit], work)
    _configure_sparse(cfg, work)
    git.run(["checkout", "--quiet", "--force", "-B", BRANCH, commit], work)
    git.run(["clean", "-fdxq"], work)
    git.run(["tag", "-f", BASE_TAG, commit], work)

    source_patches = _list_patches(cfg.source_patches)
    feature_patches = _list_patches(cfg.feature_patches)
    _check_not_excluded(cfg, work, source_patches + feature_patches)
    if source_patches:
        git.run(["apply", "--index", "--whitespace=nowarn", *map(str, source_patches)], work)
    git.run(["commit", "--quiet", "--allow-empty", "--no-verify", "-m", SOURCES_SUBJECT], work, fixed_date=True)
    git.run(["tag", "-f", SOURCES_TAG, "HEAD"], work)

    if feature_patches:
        proc = git.run(
            ["am", "--quiet", "--3way", "--committer-date-is-author-date", *map(str, feature_patches)],
            work,
            check=False,
        )
        if proc.returncode != 0:
            raise MakepatchError(
                f"a feature patch failed to apply in {work}:\n{(proc.stdout + proc.stderr).strip()}\n"
                "Resolve the conflict there, run `git am --continue`, then `makepatch src rebuild`."
            )
    return work


def sparse_patterns(cfg: SourceConfig) -> str:
    """Turn ``work-exclude`` (gitignore syntax, upstream root) into non-cone
    sparse-checkout patterns, which share the syntax with inverted meaning."""
    lines = ["/*"]
    for pattern in cfg.work_exclude:
        pattern = pattern.strip()
        if not pattern or pattern.startswith("#"):
            continue
        lines.append(pattern[1:] if pattern.startswith("!") else f"!{pattern}")
    return "\n".join(lines) + "\n"


def _configure_sparse(cfg: SourceConfig, work: Path) -> None:
    # Configured by hand instead of `git sparse-checkout set --no-cone`,
    # which needs a newer git.
    gitdir = Path(git.out(["rev-parse", "--absolute-git-dir"], work))
    (gitdir / "info").mkdir(exist_ok=True)
    (gitdir / "info" / "sparse-checkout").write_text(sparse_patterns(cfg))
    git.run(["config", "core.sparseCheckout", "true"], work)
    git.run(["config", "core.sparseCheckoutCone", "false"], work)


def _check_not_excluded(cfg: SourceConfig, work: Path, patches: list[Path]) -> None:
    if not cfg.work_exclude or not patches:
        return
    touched: dict[str, Path] = {}
    for patch in patches:
        for path in git.patch_paths(patch, work):
            touched.setdefault(path, patch)
    listing = git.run(["ls-files", "-t", "-z", "--", *touched], work, env={"GIT_LITERAL_PATHSPECS": "1"}).stdout
    excluded = [entry[2:] for entry in listing.split("\0") if entry.startswith("S ")]
    if excluded:
        details = "\n".join(f"  {path} (patched by {touched[path].name})" for path in excluded)
        raise MakepatchError(f"patches touch files excluded by work-exclude:\n{details}")


def _has_ref(work: Path, ref: str) -> bool:
    return git.run(["rev-parse", "--verify", "--quiet", ref], work, check=False).returncode == 0


def _abort_in_progress(work: Path) -> None:
    state = _in_progress(work)
    if state == "git am":
        git.run(["am", "--abort"], work, check=False)
    elif state == "git rebase":
        git.run(["rebase", "--abort"], work, check=False)


def _sources_commit(work: Path) -> str:
    """Locate the source-patch commit (first commit after base) and re-tag it."""
    if not _has_ref(work, BASE_TAG):
        raise MakepatchError(f"{work} is not set up; run `makepatch src apply`")
    first = git.out(["rev-list", "--reverse", "--first-parent", f"{BASE_TAG}..HEAD"], work).splitlines()
    if not first:
        raise MakepatchError(f"the '{SOURCES_SUBJECT}' commit is missing from {work}")
    commit = first[0]
    subject = git.out(["log", "-1", "--format=%s", commit], work)
    if subject != SOURCES_SUBJECT:
        raise MakepatchError(
            f"the first commit after {BASE_TAG} must be '{SOURCES_SUBJECT}', found '{subject}'"
        )
    git.run(["tag", "-f", SOURCES_TAG, commit], work)
    return commit


def _require_clean(work: Path) -> None:
    if not (work / ".git").exists():
        raise MakepatchError(f"{work} is not set up; run `makepatch src apply`")
    state = _in_progress(work)
    if state:
        raise MakepatchError(f"finish the pending {state} in {work} first")
    if git.out(["status", "--porcelain", "--untracked-files=no"], work):
        raise MakepatchError(
            f"{work} has uncommitted changes; commit them as a feature, or run `makepatch src fixup`"
        )


def _clear_patches(directory: Path, keep: set[Path]) -> None:
    for patch in _list_patches(directory):
        if patch not in keep:
            patch.unlink()
    if directory.is_dir():
        for sub in sorted((d for d in directory.rglob("*") if d.is_dir()), key=lambda d: len(d.parts), reverse=True):
            if not any(sub.iterdir()):
                sub.rmdir()


def rebuild(cfg: SourceConfig, work: Path | None = None) -> tuple[int, int]:
    """Regenerate patches from the work repository. Returns (sources, features)."""
    work = work or cfg.work_dir
    _require_clean(work)
    sources = _sources_commit(work)

    changed = [
        p
        for p in git.run(["diff", "--name-only", "-z", "--no-renames", BASE_TAG, sources], work).stdout.split("\0")
        if p
    ]
    written: set[Path] = set()
    for path in changed:
        diff = git.run(["diff", *DIFF_ARGS, BASE_TAG, sources, "--", path], work, binary=True).stdout
        target = cfg.source_patches / f"{path}.patch"
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.is_file() or target.read_bytes() != diff:
            target.write_bytes(diff)
        written.add(target)
    _clear_patches(cfg.source_patches, written)

    _clear_patches(cfg.feature_patches, set())
    count = int(git.out(["rev-list", "--count", f"{sources}..HEAD"], work))
    if count:
        cfg.feature_patches.mkdir(parents=True, exist_ok=True)
        git.run(
            ["format-patch", *FORMAT_PATCH_ARGS, "--no-signature", "--quiet", "-o", str(cfg.feature_patches), f"{sources}..HEAD"],
            work,
        )
    return len(written), count


def fixup(cfg: SourceConfig, work: Path | None = None) -> None:
    """Fold the current working tree changes into the source-patch commit."""
    work = work or cfg.work_dir
    if not (work / ".git").exists():
        raise MakepatchError(f"{work} is not set up; run `makepatch src apply`")
    state = _in_progress(work)
    if state:
        raise MakepatchError(f"finish the pending {state} in {work} first")
    sources = _sources_commit(work)
    git.run(["add", "--all"], work)
    if not git.out(["diff", "--cached", "--name-only"], work):
        raise MakepatchError("nothing to fix up: the working tree is clean")
    git.run(["commit", "--quiet", "--no-verify", f"--fixup={sources}"], work)
    proc = git.run(
        ["rebase", "--quiet", "--interactive", "--autosquash", "--keep-empty", BASE_TAG],
        work,
        check=False,
        env={"GIT_SEQUENCE_EDITOR": "true", "GIT_EDITOR": "true"},
    )
    if proc.returncode != 0:
        raise MakepatchError(
            f"rebasing feature commits onto the updated source patches stopped in {work}:\n"
            f"{(proc.stdout + proc.stderr).strip()}\n"
            "Resolve it, run `git rebase --continue`, then `makepatch src rebuild`."
        )
    _sources_commit(work)


@dataclass
class Status:
    base: str
    source_files: int
    features: int
    dirty: bool
    in_progress: str | None


def status(cfg: SourceConfig, work: Path | None = None) -> Status:
    work = work or cfg.work_dir
    if not (work / ".git").exists() or not _has_ref(work, BASE_TAG):
        raise MakepatchError(f"{work} is not set up; run `makepatch src apply`")
    in_progress = _in_progress(work)
    sources = _sources_commit(work) if not in_progress else git.out(["rev-parse", SOURCES_TAG], work)
    files = git.out(["diff", "--name-only", "--no-renames", BASE_TAG, sources], work).splitlines()
    return Status(
        base=git.out(["rev-parse", BASE_TAG], work),
        source_files=len(files),
        features=int(git.out(["rev-list", "--count", f"{sources}..HEAD"], work)),
        dirty=bool(git.out(["status", "--porcelain", "--untracked-files=no"], work)),
        in_progress=in_progress,
    )


def materialize(cfg: SourceConfig, dest: Path, *, offline: bool = False) -> Path:
    """Build a pristine patched tree in ``dest`` (used by the build hook)."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    return setup(cfg, dest, offline=offline, force=True)
