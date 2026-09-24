from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def sh(*args: str, cwd: Path, env: dict | None = None, check: bool = True) -> subprocess.CompletedProcess:
    full_env = dict(os.environ)
    full_env.update(env or {})
    proc = subprocess.run(list(args), cwd=cwd, env=full_env, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise AssertionError(f"{args} failed ({proc.returncode}):\n{proc.stdout}\n{proc.stderr}")
    return proc


GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


def git(*args: str, cwd: Path, check: bool = True) -> str:
    return sh("git", "-c", "init.defaultBranch=main", "-c", "commit.gpgsign=false", *args, cwd=cwd, env=GIT_ENV, check=check).stdout


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.fixture
def upstream(tmp_path: Path) -> tuple[Path, str]:
    """A small upstream repository; returns (path, commit)."""
    repo = tmp_path / "upstream"
    repo.mkdir()
    git("init", "-q", cwd=repo)
    write(repo / "src" / "demo" / "__init__.py", 'GREETING = "hello"\n\n\ndef greet(name):\n    return f"{GREETING}, {name}"\n')
    write(repo / "src" / "demo" / "util.py", "def add(a, b):\n    return a + b\n")
    write(repo / "README", "upstream readme\n")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "initial", cwd=repo)
    git("tag", "v1.0", cwd=repo)
    return repo, git("rev-parse", "HEAD", cwd=repo).strip()


def fork_project(tmp_path: Path, upstream_repo: Path, ref: str, name: str = "fork") -> Path:
    project = tmp_path / name
    project.mkdir()
    write(
        project / "pyproject.toml",
        f"""\
[build-system]
requires = ["hatchling", "makepatch"]
build-backend = "hatchling.build"

[project]
name = "demo-fork"
version = "1.0.post1"

[tool.makepatch.source]
upstream = "{upstream_repo.as_uri()}"
ref = "{ref}"
include = {{ "src/demo" = "demo" }}

[tool.hatch.build.hooks.makepatch]

[tool.hatch.build.targets.wheel]
bypass-selection = true
""",
    )
    write(project / ".gitignore", "work/\n.makepatch/\ndist/\n")
    return project
