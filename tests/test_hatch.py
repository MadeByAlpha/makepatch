from __future__ import annotations

import shutil
import sys
import tarfile
import zipfile

from makepatch.config import SourceConfig
from makepatch.source import workspace

from conftest import ROOT, fork_project, git, sh, write


def test_uv_build_sdist_and_wheel(tmp_path, upstream):
    repo, commit = upstream
    project = fork_project(tmp_path, repo, commit, source_extra='exclude = ["tests/", "*.txt"]\nwork-exclude = ["docs/"]')
    with (project / "pyproject.toml").open("a") as fp:
        fp.write(f'\n[tool.uv.sources]\nmakepatch = {{ path = "{ROOT.as_posix()}" }}\n')
    cfg = SourceConfig.load(project)
    work = workspace.setup(cfg)
    write(work / "src/demo/util.py", "def add(a, b):\n    return a + b + 0\n")
    workspace.fixup(cfg)
    write(work / "src/demo/extra.py", "EXTRA = 1\n")
    write(work / "src/demo/sub/notes.txt", "excluded\n")
    git("add", "-A", cwd=work)
    git("commit", "-q", "-m", "Add extra", cwd=work)
    workspace.rebuild(cfg)
    # Uncommitted work must not leak into the build.
    write(work / "src/demo/extra.py", "EXTRA = 2\n")
    git("init", "-q", cwd=project)  # so that .gitignore excludes work/ from the sdist

    sh("uv", "build", "--sdist", "--out-dir", "dist", cwd=project)
    (sdist,) = (project / "dist").glob("*.tar.gz")
    with tarfile.open(sdist) as tar:
        names = {n.split("/", 1)[1] for n in tar.getnames() if "/" in n}
    assert "_makepatch/tree/src/demo/extra.py" in names
    assert not any("/tests/" in n or n.endswith(".txt") for n in names), sorted(names)
    assert not any(n.startswith("docs/") for n in names)
    assert not any(n.startswith("work/") for n in names)

    # Build the wheel from the sdist with the upstream gone: no fetch needed.
    shutil.rmtree(project / ".makepatch")
    repo.rename(tmp_path / "gone")
    # uv does not apply [tool.uv.sources] to build requirements of an sdist
    # archive, so build against this checkout's environment instead.
    sh("uv", "build", "--wheel", "--no-build-isolation", "--python", sys.executable, "--out-dir", "dist", str(sdist), cwd=project)
    (wheel,) = (project / "dist").glob("*.whl")
    with zipfile.ZipFile(wheel) as zf:
        assert zf.read("demo/util.py").decode() == "def add(a, b):\n    return a + b + 0\n"
        assert zf.read("demo/extra.py").decode() == "EXTRA = 1\n"
        names = zf.namelist()
        assert not any(n.startswith(("src/", "_makepatch/", "patches/", "demo/tests/")) or n.endswith(".txt") for n in names)
        assert "demo/__init__.py" in names
