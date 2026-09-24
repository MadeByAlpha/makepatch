from __future__ import annotations

from pathlib import Path

import pytest

from makepatch.config import SourceConfig
from makepatch.errors import MakepatchError
from makepatch.source import workspace

from conftest import fork_project, git, write


def _tree(work: Path) -> dict[str, str]:
    return {
        p.relative_to(work).as_posix(): p.read_text()
        for p in sorted(work.rglob("*"))
        if p.is_file() and ".git" not in p.relative_to(work).parts
    }


def test_setup_without_patches(tmp_path, upstream):
    repo, commit = upstream
    cfg = SourceConfig.load(fork_project(tmp_path, repo, commit))
    work = workspace.setup(cfg)
    assert git("rev-parse", "makepatch/base", cwd=work).strip() == commit
    assert git("log", "-1", "--format=%s", cwd=work).strip() == workspace.SOURCES_SUBJECT
    assert (work / "src/demo/util.py").read_text() == "def add(a, b):\n    return a + b\n"


def test_roundtrip_source_and_feature_patches(tmp_path, upstream):
    repo, commit = upstream
    project = fork_project(tmp_path, repo, "v1.0")
    cfg = SourceConfig.load(project)
    work = workspace.setup(cfg)

    # Source (file) patch: edit, then fold into the source patch commit.
    write(work / "src/demo/util.py", "def add(a, b):\n    return b + a\n")
    write(work / "src/demo/new.py", "NEW = True\n")
    workspace.fixup(cfg)

    # Feature patch: a regular commit on top.
    write(work / "src/demo/__init__.py", 'GREETING = "hi"\n\n\ndef greet(name):\n    return f"{GREETING}, {name}!"\n')
    git("commit", "-q", "-am", "Friendlier greeting", cwd=work)

    assert workspace.rebuild(cfg) == (2, 1)
    assert sorted(p.relative_to(cfg.patches_dir).as_posix() for p in cfg.patches_dir.rglob("*.patch")) == [
        "features/0001-Friendlier-greeting.patch",
        "sources/src/demo/new.py.patch",
        "sources/src/demo/util.py.patch",
    ]
    feature = (cfg.feature_patches / "0001-Friendlier-greeting.patch").read_text()
    assert feature.startswith("From 0000000000000000000000000000000000000000 ")
    assert " 1 file changed" not in feature  # -p: no diffstat
    assert "Subject: [PATCH] Friendlier greeting" in feature
    source = (cfg.source_patches / "src/demo/util.py.patch").read_text()
    assert source.startswith("diff --git a/src/demo/util.py b/src/demo/util.py")

    expected = _tree(work)
    # A fresh work repository built from the patches only must be identical.
    fresh = workspace.materialize(cfg, tmp_path / "fresh")
    assert _tree(fresh) == expected
    assert git("log", "-1", "--format=%s", cwd=fresh).strip() == "Friendlier greeting"

    # Rebuilding again is a no-op.
    before = {p: p.read_bytes() for p in cfg.patches_dir.rglob("*.patch")}
    workspace.rebuild(cfg)
    assert {p: p.read_bytes() for p in cfg.patches_dir.rglob("*.patch")} == before


def test_removed_patches_are_deleted(tmp_path, upstream):
    repo, commit = upstream
    cfg = SourceConfig.load(fork_project(tmp_path, repo, commit))
    work = workspace.setup(cfg)
    write(work / "src/demo/util.py", "X = 1\n")
    workspace.fixup(cfg)
    git("commit", "-q", "--allow-empty", "-m", "Empty feature", cwd=work)
    workspace.rebuild(cfg)
    assert (cfg.source_patches / "src/demo/util.py.patch").is_file()

    git("reset", "-q", "--hard", "makepatch/base", cwd=work)
    git("commit", "-q", "--allow-empty", "-m", workspace.SOURCES_SUBJECT, cwd=work)
    workspace.rebuild(cfg)
    assert list(cfg.patches_dir.rglob("*.patch")) == []
    assert not (cfg.source_patches / "src").exists()


def test_rebuild_refuses_dirty_tree(tmp_path, upstream):
    repo, commit = upstream
    cfg = SourceConfig.load(fork_project(tmp_path, repo, commit))
    work = workspace.setup(cfg)
    write(work / "README", "changed\n")
    with pytest.raises(MakepatchError, match="uncommitted"):
        workspace.rebuild(cfg)


def test_setup_protects_unsaved_feature_commits(tmp_path, upstream):
    repo, commit = upstream
    cfg = SourceConfig.load(fork_project(tmp_path, repo, commit))
    work = workspace.setup(cfg)
    write(work / "README", "changed\n")
    git("commit", "-q", "-am", "Unsaved", cwd=work)
    with pytest.raises(MakepatchError, match="feature commit"):
        workspace.setup(cfg)
    workspace.setup(cfg, force=True)
    assert (work / "README").read_text() == "upstream readme\n"


def test_feature_conflict_leaves_am_session(tmp_path, upstream):
    repo, commit = upstream
    cfg = SourceConfig.load(fork_project(tmp_path, repo, commit))
    write(
        cfg.feature_patches / "0001-Broken.patch",
        "From 0000000000000000000000000000000000000000 Mon Sep 17 00:00:00 2001\n"
        "From: Test <test@example.com>\n"
        "Date: Thu, 1 Jan 2026 00:00:00 +0000\n"
        "Subject: [PATCH] Broken\n\n---\n"
        "diff --git a/README b/README\n"
        "--- a/README\n+++ b/README\n@@ -1 +1 @@\n-something else\n+x\n",
    )
    with pytest.raises(MakepatchError, match="git am --continue"):
        workspace.setup(cfg)
    assert workspace.status(cfg).in_progress == "git am"


def test_offline_uses_cache(tmp_path, upstream):
    repo, commit = upstream
    cfg = SourceConfig.load(fork_project(tmp_path, repo, "v1.0"))
    workspace.setup(cfg)
    repo.rename(tmp_path / "gone")
    workspace.materialize(cfg, tmp_path / "offline", offline=True)
    # A failed fetch falls back to the pinned ref as well.
    workspace.materialize(cfg, tmp_path / "fallback")
