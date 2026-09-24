"""Interpreter start-up check, imported from ``makepatch-startup.pth``.

It only does work in an environment where ``makepatch pkg apply`` (or
``pkg commit``) has run before, i.e. where ``<sys.prefix>/makepatch-state.json``
exists. The fast path reads the patch files and the markers in the
dist-info directories; only when they disagree (a package was reinstalled
by uv/pip, or a patch was added, changed or removed) the patches are
applied again, under the environment lock. Nothing is ever patched in
memory: the result is the same as running ``makepatch pkg apply``.
"""

import hashlib
import json
import os
import re
import sys


def _up_to_date(state):
    applied = state.get("applied") or {}
    patches_dir = state["patches_dir"]
    try:
        names = [n for n in os.listdir(patches_dir) if n.endswith(".patch")]
    except FileNotFoundError:
        names = []
    if len(names) != len(applied):
        return False
    for name in names:
        key = re.sub(r"[-_.]+", "-", name.partition("@")[0]).lower()
        entry = applied.get(key)
        if entry is None:
            return False
        with open(os.path.join(patches_dir, name), "rb") as fp:
            sha = hashlib.sha256(fp.read()).hexdigest()
        if entry.get("sha256") != sha:
            return False
        try:
            with open(os.path.join(entry["dist_info"], "makepatch.json"), "rb") as fp:
                marker = json.load(fp)
        except (OSError, ValueError):
            return False
        if marker.get("sha256") != sha:
            return False
    return True


def _repair(state):
    from pathlib import Path

    from makepatch.config import PackageConfig
    from makepatch.package.apply import apply_all, locked
    from makepatch.package.dist import Environment

    env = Environment.current()
    cfg = PackageConfig(root=Path(state["project"]), patches_dir=Path(state["patches_dir"]))
    with locked(env):
        # Another process may have repaired the environment meanwhile.
        try:
            with open(env.state_file, "rb") as fp:
                if _up_to_date(json.load(fp)):
                    return
        except (OSError, ValueError, KeyError):
            pass
        results = apply_all(cfg, env)
    for result in results:
        if result.error:
            sys.stderr.write(f"makepatch: warning: {result.error}\n")
        elif result.action != "unchanged":
            sys.stderr.write(f"makepatch: {result.key}: {result.action}\n")


def _run():
    if os.environ.get("MAKEPATCH_DISABLE_STARTUP") or sys.flags.isolated:
        return
    try:
        with open(os.path.join(sys.prefix, "makepatch-state.json"), "rb") as fp:
            state = json.load(fp)
    except (OSError, ValueError):
        return
    if not os.path.isdir(state.get("project", "")):
        return
    if _up_to_date(state):
        return
    _repair(state)


try:
    _run()
except Exception as exc:  # never break interpreter start-up
    sys.stderr.write(f"makepatch: warning: start-up check failed: {exc}\n")
