from __future__ import annotations

import base64
import hashlib
import json
import os
import zipfile
from pathlib import Path

import pytest

from conftest import ROOT, sh, write

TINY_V1 = {
    "tiny/__init__.py": 'VALUE = "original"\n\n\ndef value():\n    return VALUE\n',
    "tiny/data.txt": "keep me\n",
    "tiny/gone.py": "GONE = True\n",
}


def build_wheel(dest: Path, name: str, version: str, files: dict[str, str]) -> Path:
    dist_info = f"{name}-{version}.dist-info"
    content = dict(files)
    content[f"{dist_info}/METADATA"] = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
    content[f"{dist_info}/WHEEL"] = "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    record = []
    for path, text in content.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(text.encode()).digest()).rstrip(b"=").decode()
        record.append(f"{path},sha256={digest},{len(text.encode())}")
    record.append(f"{dist_info}/RECORD,,")
    content[f"{dist_info}/RECORD"] = "\n".join(record) + "\n"
    wheel = dest / f"{name}-{version}-py3-none-any.whl"
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(wheel, "w") as zf:
        for path, text in content.items():
            zf.writestr(path, text)
    return wheel


class Env:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.cache = tmp / "uv-cache"
        self.venv = tmp / "venv"
        self.python = self.venv / "bin" / "python"
        self.project = tmp / "project"
        write(self.project / "pyproject.toml", '[project]\nname = "app"\nversion = "0"\n')
        sh("uv", "venv", "-q", "--python", os.environ.get("MAKEPATCH_TEST_PYTHON", "3.11"), str(self.venv), cwd=tmp)
        self.uv_pip("install", "-e", str(ROOT))

    def uv_pip(self, *args: str):
        return sh(
            "uv", "pip", *args[:1], "-q", "--python", str(self.python), "--cache-dir", str(self.cache),
            "--link-mode", "hardlink", *args[1:], cwd=self.tmp,
        )

    def makepatch(self, *args: str, check: bool = True):
        # The start-up hook is tested through ``py``; keep CLI runs deterministic.
        return sh(
            str(self.python), "-m", "makepatch", "-C", str(self.project), *args,
            cwd=self.project, check=check, env={"MAKEPATCH_DISABLE_STARTUP": "1"},
        )

    def py(self, code: str, **env: str):
        return sh(str(self.python), "-c", code, cwd=self.project, env=env)

    @property
    def site(self) -> Path:
        (site,) = self.venv.glob("lib/python*/site-packages")
        return site


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def _cache_copies(cache: Path, name: str) -> list[Path]:
    return [p for p in cache.rglob(name) if p.is_file() and "tiny" in p.parts]


def test_edit_commit_apply_and_startup_repair(env: Env):
    wheel = build_wheel(env.tmp / "wheels", "tiny", "1.0", TINY_V1)
    env.uv_pip("install", str(wheel))
    init = env.site / "tiny" / "__init__.py"
    assert init.stat().st_nlink > 1, "uv should have hardlinked the file from its cache"
    cached = _cache_copies(env.cache, "__init__.py")
    assert cached

    edit_dir = Path(env.makepatch("pkg", "edit", "tiny").stdout.split("files in ")[1].split(",")[0])
    write(edit_dir / "tiny" / "__init__.py", 'VALUE = "patched"\n\n\ndef value():\n    return VALUE\n')
    write(edit_dir / "tiny" / "added.py", "ADDED = 1\n")
    (edit_dir / "tiny" / "gone.py").unlink()
    out = env.makepatch("pkg", "commit", "tiny").stdout
    assert "patches/packages/tiny@1.0.patch" in out and "applied" in out
    assert not edit_dir.exists()

    # Installed files are patched, uv's cache is untouched.
    assert 'VALUE = "patched"' in init.read_text()
    assert init.stat().st_nlink == 1
    assert (env.site / "tiny" / "added.py").is_file()
    assert not (env.site / "tiny" / "gone.py").exists()
    for copy in cached:
        assert 'VALUE = "original"' in copy.read_text()

    # RECORD matches the new content, and lists makepatch's own files.
    dist_info = env.site / "tiny-1.0.dist-info"
    record = (dist_info / "RECORD").read_text()
    assert "tiny/added.py,sha256=" in record
    assert "tiny/gone.py" not in record
    assert "tiny-1.0.dist-info/makepatch.json" in record
    digest = base64.urlsafe_b64encode(hashlib.sha256(init.read_bytes()).digest()).rstrip(b"=").decode()
    assert f"tiny/__init__.py,sha256={digest}," in record

    assert env.makepatch("pkg", "status").stdout.strip() == "tiny@1.0.patch: applied"
    assert env.makepatch("pkg", "apply").stdout.strip() == "tiny: unchanged"

    # Reinstalling restores the pristine files; the start-up hook repairs them.
    env.uv_pip("install", "--reinstall", str(wheel))
    assert 'VALUE = "original"' in init.read_text()
    proc = env.py("import tiny; print(tiny.value())")
    assert proc.stdout.strip() == "patched"
    assert "makepatch: tiny: applied" in proc.stderr
    # Second start: fast path, silent.
    proc = env.py("import tiny; print(tiny.value())")
    assert proc.stdout.strip() == "patched" and proc.stderr == ""
    # The hook can be disabled, and is skipped in isolated mode (uv probes).
    env.uv_pip("install", "--reinstall", str(wheel))
    assert env.py("import tiny; print(tiny.value())", MAKEPATCH_DISABLE_STARTUP="1").stdout.strip() == "original"
    sh(str(env.python), "-I", "-c", "pass", cwd=env.project)
    assert 'VALUE = "original"' in init.read_text()
    env.makepatch("pkg", "apply")
    for copy in _cache_copies(env.cache, "__init__.py"):
        assert 'VALUE = "original"' in copy.read_text()


def test_edit_of_patched_package_and_removal(env: Env):
    wheel = build_wheel(env.tmp / "wheels", "tiny", "1.0", TINY_V1)
    env.uv_pip("install", str(wheel))
    patch = env.project / "patches/packages/tiny@1.0.patch"

    edit_dir = env.project / ".makepatch/edit/tiny@1.0"
    env.makepatch("pkg", "edit", "tiny")
    write(edit_dir / "tiny" / "data.txt", "changed\n")
    env.makepatch("pkg", "commit", "tiny")
    first = patch.read_text()

    # Editing again starts from the pristine files with the patch pending.
    env.makepatch("pkg", "edit", "tiny")
    status = sh("git", "status", "--porcelain", cwd=edit_dir).stdout
    assert status.strip() == "M tiny/data.txt"
    write(edit_dir / "tiny" / "data.txt", "changed twice\n")
    env.makepatch("pkg", "commit", "tiny")
    assert patch.read_text() != first
    assert (env.site / "tiny" / "data.txt").read_text() == "changed twice\n"

    # Deleting the patch file and applying restores the original package.
    patch.unlink()
    assert env.makepatch("pkg", "apply").stdout.strip() == "tiny: reverted"
    assert (env.site / "tiny" / "data.txt").read_text() == "keep me\n"
    assert not (env.site / "tiny-1.0.dist-info" / "makepatch.json").exists()
    assert "makepatch.patch" not in (env.site / "tiny-1.0.dist-info" / "RECORD").read_text()


def test_startup_hook_reverts_removed_patch(env: Env):
    env.uv_pip("install", str(build_wheel(env.tmp / "wheels", "tiny", "1.0", TINY_V1)))
    env.makepatch("pkg", "edit", "tiny")
    write(env.project / ".makepatch/edit/tiny@1.0/tiny/data.txt", "changed\n")
    env.makepatch("pkg", "commit", "tiny")
    (env.project / "patches/packages/tiny@1.0.patch").unlink()
    proc = env.py("import pathlib, tiny; print(pathlib.Path(tiny.__file__).with_name('data.txt').read_text(), end='')")
    assert proc.stdout == "keep me\n"
    assert "makepatch: tiny: reverted" in proc.stderr


def test_version_mismatch_is_an_error(env: Env):
    env.uv_pip("install", str(build_wheel(env.tmp / "wheels", "tiny", "1.0", TINY_V1)))
    env.makepatch("pkg", "edit", "tiny")
    write(env.project / ".makepatch/edit/tiny@1.0/tiny/data.txt", "changed\n")
    env.makepatch("pkg", "commit", "tiny")

    env.uv_pip("install", str(build_wheel(env.tmp / "wheels", "tiny", "2.0", TINY_V1)))
    proc = env.makepatch("pkg", "apply", check=False)
    assert proc.returncode == 1
    assert "targets tiny 1.0, but 2.0 is installed" in proc.stderr
    proc = env.py("import tiny")
    assert "makepatch: warning:" in proc.stderr


def test_state_file(env: Env):
    env.uv_pip("install", str(build_wheel(env.tmp / "wheels", "tiny", "1.0", TINY_V1)))
    env.makepatch("pkg", "apply")
    state = json.loads((env.venv / "makepatch-state.json").read_text())
    assert state["project"] == str(env.project)
    assert state["applied"] == {}
