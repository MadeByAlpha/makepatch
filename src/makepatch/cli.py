"""Command line interface: ``makepatch src ...`` and ``makepatch pkg ...``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from makepatch import __version__
from makepatch.config import PackageConfig, SourceConfig, find_project_root
from makepatch.errors import MakepatchError


def _root(args: argparse.Namespace) -> Path:
    return find_project_root(Path(args.project) if args.project else None)


def _env(args: argparse.Namespace):
    from makepatch.package.dist import Environment

    return Environment.for_python(args.python) if args.python else Environment.current()


# -- src -------------------------------------------------------------------------------


def cmd_src_apply(args: argparse.Namespace) -> int:
    from makepatch.source import workspace

    cfg = SourceConfig.load(_root(args))
    work = workspace.setup(cfg, offline=args.offline, force=args.force)
    print(f"work repository ready: {work}")
    return 0


def cmd_src_rebuild(args: argparse.Namespace) -> int:
    from makepatch.source import workspace

    cfg = SourceConfig.load(_root(args))
    sources, features = workspace.rebuild(cfg)
    print(f"wrote {sources} source patch(es) and {features} feature patch(es)")
    return 0


def cmd_src_fixup(args: argparse.Namespace) -> int:
    from makepatch.source import workspace

    cfg = SourceConfig.load(_root(args))
    workspace.fixup(cfg)
    print("folded changes into the source patch commit; run `makepatch src rebuild` to update patches")
    return 0


def cmd_src_status(args: argparse.Namespace) -> int:
    from makepatch.source import workspace

    cfg = SourceConfig.load(_root(args))
    st = workspace.status(cfg)
    print(f"upstream:      {cfg.upstream} @ {cfg.ref} ({st.base[:12]})")
    print(f"source files:  {st.source_files}")
    print(f"feature commits: {st.features}")
    print(f"dirty:         {'yes' if st.dirty else 'no'}")
    if st.in_progress:
        print(f"in progress:   {st.in_progress}")
    return 0


# -- pkg -------------------------------------------------------------------------------


def cmd_pkg_edit(args: argparse.Namespace) -> int:
    from makepatch.package import edit

    cfg = PackageConfig.load(_root(args))
    path = edit.edit(cfg, _env(args), args.package, force=args.force)
    print(f"edit the files in {path}, then run `makepatch pkg commit {args.package}`")
    return 0


def cmd_pkg_commit(args: argparse.Namespace) -> int:
    from makepatch.package import edit

    cfg = PackageConfig.load(_root(args))
    patch, action = edit.commit(cfg, _env(args), args.package, keep=args.keep)
    if patch is None:
        print(f"no changes; {args.package}: {action}")
    else:
        print(f"wrote {patch.relative_to(cfg.root)}; {args.package}: {action}")
    return 0


def _print_results(results) -> int:
    failed = 0
    for r in results:
        if r.error:
            failed += 1
            print(f"{r.key}: failed: {r.error}", file=sys.stderr)
        else:
            print(f"{r.key}: {r.action}")
    return 1 if failed else 0


def cmd_pkg_apply(args: argparse.Namespace) -> int:
    from makepatch.package.apply import apply_all, locked

    cfg = PackageConfig.load(_root(args))
    env = _env(args)
    with locked(env):
        return _print_results(apply_all(cfg, env, dry_run=args.check))


def cmd_pkg_revert(args: argparse.Namespace) -> int:
    from makepatch.package.apply import locked, revert, write_state

    cfg = PackageConfig.load(_root(args))
    env = _env(args)
    with locked(env):
        dist = env.find(args.package)
        done = revert(dist)
        write_state(cfg, env)
    print(f"{dist.key}: {'reverted' if done else 'not patched'}")
    if done:
        print("note: the start-up hook re-applies it unless the patch file is removed", file=sys.stderr)
    return 0


def cmd_pkg_status(args: argparse.Namespace) -> int:
    from makepatch.package.apply import discover

    cfg = PackageConfig.load(_root(args))
    env = _env(args)
    patches = discover(cfg)
    dists = {d.key: d for d in env.distributions()}
    keys = sorted(set(patches) | {k for k, d in dists.items() if d.marker()})
    if not keys:
        print("no package patches")
    for key in keys:
        patch, dist = patches.get(key), dists.get(key)
        if dist is None:
            state = "not installed"
        elif patch is None:
            state = "patched, but the patch file is gone"
        elif dist.version != patch.version:
            state = f"version mismatch (installed {dist.version})"
        else:
            marker = dist.marker()
            if marker is None:
                state = "not applied"
            elif marker.get("sha256") != patch.sha256:
                state = "outdated"
            else:
                state = "applied"
        print(f"{patch.path.name if patch else key}: {state}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="makepatch", description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-C", "--project", help="project directory (default: nearest pyproject.toml)")
    modes = parser.add_subparsers(dest="mode", required=True)

    src = modes.add_parser("src", help="source patch mode (fork an upstream git repository)")
    src_cmds = src.add_subparsers(dest="command", required=True)
    p = src_cmds.add_parser("apply", help="create the work repository and apply all patches")
    p.add_argument("--offline", action="store_true", help="use the cached upstream only")
    p.add_argument("--force", action="store_true", help="discard unsaved work in the work repository")
    p.set_defaults(func=cmd_src_apply)
    p = src_cmds.add_parser("rebuild", help="regenerate patches from the work repository")
    p.set_defaults(func=cmd_src_rebuild)
    p = src_cmds.add_parser("fixup", help="fold working tree changes into the source patches")
    p.set_defaults(func=cmd_src_fixup)
    p = src_cmds.add_parser("status", help="show the work repository state")
    p.set_defaults(func=cmd_src_status)

    pkg = modes.add_parser("pkg", help="package patch mode (patch installed distributions)")
    pkg.add_argument("--python", help="interpreter of the target environment (default: the current one)")
    pkg_cmds = pkg.add_subparsers(dest="command", required=True)
    p = pkg_cmds.add_parser("edit", help="prepare an editable copy of an installed package")
    p.add_argument("package")
    p.add_argument("--force", action="store_true", help="discard an existing edit")
    p.set_defaults(func=cmd_pkg_edit)
    p = pkg_cmds.add_parser("commit", help="write the patch from an edit and apply it")
    p.add_argument("package")
    p.add_argument("--keep", action="store_true", help="keep the edit directory")
    p.set_defaults(func=cmd_pkg_commit)
    p = pkg_cmds.add_parser("apply", help="apply every package patch to the environment")
    p.add_argument("--check", action="store_true", help="only check that the patches apply")
    p.set_defaults(func=cmd_pkg_apply)
    p = pkg_cmds.add_parser("revert", help="restore the original files of a package")
    p.add_argument("package")
    p.set_defaults(func=cmd_pkg_revert)
    p = pkg_cmds.add_parser("status", help="show the state of every package patch")
    p.set_defaults(func=cmd_pkg_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except MakepatchError as exc:
        print(f"makepatch: error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
