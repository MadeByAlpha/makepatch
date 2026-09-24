"""Hatchling build hook for source patch mode.

``[tool.hatch.build.hooks.makepatch]`` enables it; the settings themselves
live in ``[tool.makepatch.source]``.

* sdist: the patched upstream tree is stored under ``_makepatch/tree`` so
  that building a wheel from the sdist needs neither git history nor network.
* wheel: the paths of ``include`` are mapped from the patched tree into the
  wheel.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

from makepatch.config import SourceConfig
from makepatch.errors import MakepatchError
from makepatch.source import workspace

SDIST_TREE = "_makepatch/tree"


class MakepatchBuildHook(BuildHookInterface):
    PLUGIN_NAME = "makepatch"

    def _tree(self, cfg: SourceConfig) -> Path:
        root = Path(self.root)
        prebuilt = root / SDIST_TREE
        if (root / "PKG-INFO").is_file() and prebuilt.is_dir():
            return prebuilt
        offline = os.environ.get("MAKEPATCH_OFFLINE", "") not in ("", "0")
        dest = cfg.state_dir / "build" / "tree"
        self.app.display_info(f"makepatch: applying patches onto {cfg.upstream} @ {cfg.ref}")
        return workspace.materialize(cfg, dest, offline=offline)

    def initialize(self, version: str, build_data: dict) -> None:
        cfg = SourceConfig.load(Path(self.root))
        if not cfg.include:
            raise MakepatchError("[tool.makepatch.source] include is empty: nothing would be built")
        tree = self._tree(cfg)
        force_include = build_data.setdefault("force_include", {})
        for src, dst in cfg.include.items():
            path = tree / src
            if not path.exists():
                raise MakepatchError(f"include path {src!r} does not exist in the patched upstream tree")
            if self.target_name == "sdist":
                force_include[str(path)] = f"{SDIST_TREE}/{src}"
            else:
                force_include[str(path)] = dst

    def clean(self, versions: list[str]) -> None:
        build = Path(self.root) / ".makepatch" / "build"
        if build.is_dir():
            shutil.rmtree(build)
