"""Thin wrapper around the ``git`` executable.

Every git invocation in makepatch goes through :func:`run` so that the
environment is pinned: stable locale, fixed identity, no signing, no
line-ending conversion and no discovery of an enclosing repository.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Sequence

from makepatch.errors import MakepatchError

IDENTITY_NAME = "makepatch"
IDENTITY_EMAIL = "makepatch@localhost"
# Fixed date for commits makepatch creates itself (base / source patches),
# so that re-running setup yields identical commit ids.
FIXED_DATE = "1970-01-01T00:00:00+0000"

_CONFIG = (
    "commit.gpgsign=false",
    "tag.gpgsign=false",
    "core.autocrlf=false",
    "core.safecrlf=false",
    "core.quotepath=false",
    "advice.detachedHead=false",
    "init.defaultBranch=main",
)


class GitError(MakepatchError):
    def __init__(self, args: Sequence[str], returncode: int, stdout: str, stderr: str):
        self.args_ = list(args)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        detail = (stderr or stdout).strip()
        super().__init__(f"git {' '.join(args)} failed ({returncode})" + (f":\n{detail}" if detail else ""))


def _env(cwd: Path, extra: dict[str, str] | None, fixed_date: bool, network: bool) -> dict[str, str]:
    env = dict(os.environ)
    for key in list(env):
        # Never let an outer repository leak in (e.g. when run from a git hook).
        if key.startswith("GIT_") and key not in ("GIT_SSH", "GIT_SSH_COMMAND", "GIT_ASKPASS", "GIT_TERMINAL_PROMPT"):
            del env[key]
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    if not network:
        # Local operations (diff, format-patch, apply, am, commit, rebase) must
        # not be influenced by user configuration such as diff.noprefix,
        # format.* or core.hooksPath. Network operations keep it for
        # proxies, credential helpers and url.*.insteadOf.
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = IDENTITY_NAME
    env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = IDENTITY_EMAIL
    if fixed_date:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = FIXED_DATE
    # Stop repository discovery at the parent of cwd: ``cwd`` itself may be a
    # repository, but nothing above it is ever used.
    env["GIT_CEILING_DIRECTORIES"] = str(Path(cwd).resolve().parent)
    if extra:
        env.update(extra)
    return env


def run(
    args: Sequence[str],
    cwd: Path | str,
    *,
    check: bool = True,
    input: bytes | str | None = None,
    env: dict[str, str] | None = None,
    fixed_date: bool = False,
    binary: bool = False,
    network: bool = False,
) -> subprocess.CompletedProcess:
    cwd = Path(cwd)
    cmd = ["git"]
    for item in _CONFIG:
        cmd += ["-c", item]
    cmd += list(args)
    if isinstance(input, str):
        input = input.encode()
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            input=input,
            capture_output=True,
            env=_env(cwd, env, fixed_date, network),
        )
    except FileNotFoundError as exc:  # pragma: no cover - depends on system
        raise MakepatchError("git executable not found in PATH") from exc
    if not binary:
        proc.stdout = proc.stdout.decode("utf-8", "surrogateescape")
        proc.stderr = proc.stderr.decode("utf-8", "surrogateescape")
    if check and proc.returncode != 0:
        out = proc.stdout if isinstance(proc.stdout, str) else proc.stdout.decode("utf-8", "replace")
        err = proc.stderr if isinstance(proc.stderr, str) else proc.stderr.decode("utf-8", "replace")
        raise GitError(args, proc.returncode, out, err)
    return proc


def out(args: Sequence[str], cwd: Path | str, **kwargs) -> str:
    return run(args, cwd, **kwargs).stdout.strip()


def patch_paths(patch: Path, cwd: Path) -> list[str]:
    """Return every path a patch touches (old and new names)."""
    proc = run(["apply", "--numstat", "-z", str(patch)], cwd)
    paths: list[str] = []
    fields = proc.stdout.split("\0")
    # ``--numstat -z`` emits "added\tdeleted\tpath\0", or for renames
    # "added\tdeleted\t\0old\0new\0".
    i = 0
    while i < len(fields):
        field = fields[i]
        parts = field.split("\t")
        if len(parts) == 3:
            if parts[2]:
                paths.append(parts[2])
                i += 1
            else:
                paths += fields[i + 1 : i + 3]
                i += 3
        else:
            i += 1
    return list(dict.fromkeys(p for p in paths if p))
