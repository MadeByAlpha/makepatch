"""Installed distributions: discovery and RECORD handling.

Only the standard wheel installation layout is relied upon (``*.dist-info``
with ``METADATA`` and ``RECORD``), which is what both pip and uv produce.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import sysconfig
from dataclasses import dataclass
from email.parser import HeaderParser
from pathlib import Path

from makepatch.errors import MakepatchError

MARKER = "makepatch.json"
SAVED_PATCH = "makepatch.patch"


def normalize(name: str) -> str:
    """PEP 503 normalization."""
    return re.sub(r"[-_.]+", "-", name).lower()


@dataclass(frozen=True)
class Environment:
    prefix: Path
    sites: tuple[Path, ...]

    @classmethod
    def current(cls) -> "Environment":
        paths = sysconfig.get_paths()
        return cls(Path(sys.prefix), _dedup(paths["purelib"], paths["platlib"]))

    @classmethod
    def for_python(cls, python: str) -> "Environment":
        code = (
            "import json,sys,sysconfig;p=sysconfig.get_paths();"
            "print(json.dumps([sys.prefix,p['purelib'],p['platlib']]))"
        )
        env = dict(os.environ, MAKEPATCH_DISABLE_STARTUP="1")
        try:
            proc = subprocess.run([python, "-c", code], capture_output=True, text=True, env=env)
        except OSError as exc:
            raise MakepatchError(f"cannot run {python}: {exc}") from None
        if proc.returncode != 0:
            raise MakepatchError(f"cannot inspect {python}:\n{proc.stderr.strip()}")
        prefix, purelib, platlib = json.loads(proc.stdout)
        return cls(Path(prefix), _dedup(purelib, platlib))

    @property
    def state_file(self) -> Path:
        return self.prefix / "makepatch-state.json"

    @property
    def lock_file(self) -> Path:
        return self.prefix / ".makepatch.lock"

    def distributions(self) -> list["Distribution"]:
        found = []
        for site in self.sites:
            if not site.is_dir():
                continue
            for entry in sorted(site.iterdir()):
                if entry.name.endswith(".dist-info") and (entry / "METADATA").is_file():
                    found.append(Distribution.from_dist_info(site, entry))
        return found

    def find(self, name: str) -> "Distribution":
        key = normalize(name)
        matches = [d for d in self.distributions() if d.key == key]
        if not matches:
            raise MakepatchError(f"package {name!r} is not installed in {self.prefix}")
        if len(matches) > 1:
            raise MakepatchError(f"package {name!r} is installed more than once in {self.prefix}")
        return matches[0]


def _dedup(*paths: str) -> tuple[Path, ...]:
    out: list[Path] = []
    seen: set[str] = set()
    for p in paths:
        key = os.path.realpath(p)
        if key not in seen:
            seen.add(key)
            out.append(Path(p))
    return tuple(out)


@dataclass(frozen=True)
class Distribution:
    site: Path
    dist_info: Path
    name: str
    version: str

    @property
    def key(self) -> str:
        return normalize(self.name)

    @property
    def spec(self) -> str:
        return f"{self.key}@{self.version}"

    @classmethod
    def from_dist_info(cls, site: Path, dist_info: Path) -> "Distribution":
        with (dist_info / "METADATA").open(encoding="utf-8", errors="replace") as fp:
            headers = HeaderParser().parse(fp, headersonly=True)
        name, version = headers.get("Name"), headers.get("Version")
        if not name or not version:
            stem = dist_info.name[: -len(".dist-info")]
            name, _, version = stem.partition("-")
        return cls(site, dist_info, name.strip(), version.strip())

    @property
    def is_editable(self) -> bool:
        direct = self.dist_info / "direct_url.json"
        if not direct.is_file():
            return False
        try:
            data = json.loads(direct.read_text(encoding="utf-8"))
        except ValueError:
            return False
        return bool(data.get("dir_info", {}).get("editable"))

    # -- RECORD ---------------------------------------------------------------

    @property
    def record_path(self) -> Path:
        return self.dist_info / "RECORD"

    def read_record(self) -> list[list[str]]:
        if not self.record_path.is_file():
            raise MakepatchError(f"{self.dist_info.name} has no RECORD file")
        with self.record_path.open(newline="", encoding="utf-8") as fp:
            return [row for row in csv.reader(fp) if row]

    def write_record(self, rows: list[list[str]]) -> None:
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        for row in rows:
            writer.writerow((row + ["", ""])[:3])
        atomic_write(self.record_path, buf.getvalue().encode("utf-8"))

    def files(self) -> list[str]:
        """Package files relative to the site directory, as a patch sees them."""
        dist_info_prefix = self.dist_info.name + "/"
        result = []
        for row in self.read_record():
            path = row[0].replace("\\", "/")
            if (
                path.startswith(("../", "/"))
                or path.startswith(dist_info_prefix)
                or "/__pycache__/" in f"/{path}"
                or path.endswith((".pyc", ".pyo"))
            ):
                continue
            result.append(path)
        return result

    # -- makepatch marker -------------------------------------------------------

    def marker(self) -> dict | None:
        path = self.dist_info / MARKER
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None


def record_hash(path: Path) -> tuple[str, str]:
    data = path.read_bytes()
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    return f"sha256={digest}", str(len(data))


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic_write(path: Path, data: bytes) -> None:
    """Write via rename so that a hardlinked (uv cache) inode is never modified."""
    tmp = path.with_name(f".{path.name}.makepatch-tmp")
    tmp.write_bytes(data)
    try:
        os.chmod(tmp, os.stat(path).st_mode & 0o7777)
    except FileNotFoundError:
        pass
    os.replace(tmp, path)
