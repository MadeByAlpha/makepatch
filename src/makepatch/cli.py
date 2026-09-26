"""Command line interface: ``makepatch src ...`` and ``makepatch pkg ...``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from makepatch import __version__
from makepatch.config import PackageConfig, SourceConfig, find_project_root
from makepatch.errors import MakepatchError
from makepatch.term import paint

# Colors for the actions reported by ``pkg`` commands.
ACTION_STYLES = {
    "applied": ("green",),
    "reapplied": ("green",),
    "would apply": ("cyan",),
    "unchanged": ("dim",),
    "reverted": ("yellow",),
    "would revert": ("yellow",),
    "not patched": ("dim",),
}


def _root(args: argparse.Namespace) -> Path:
    return find_project_root(Path(args.project) if args.project else None)


def _action(action: str) -> str:
    return paint(action, *ACTION_STYLES.get(action, ()))


def _cmd(command: str) -> str:
    return paint(f"`{command}`", "cyan")


def _err(text: str, *styles: str) -> str:
    return paint(text, *styles, stream=sys.stderr)


def _note(message: str) -> None:
    print(f"{_err('note:', 'yellow', 'bold')} {message}", file=sys.stderr)


def _env(args: argparse.Namespace):
    from makepatch.package.dist import Environment

    return Environment.for_python(args.python) if args.python else Environment.current()


# -- src -------------------------------------------------------------------------------


def cmd_src_apply(args: argparse.Namespace) -> int:
    from makepatch.source import workspace

    cfg = SourceConfig.load(_root(args))
    work = workspace.setup(cfg, offline=args.offline, force=args.force)
    print(f"{paint('work repository ready:', 'green')} {paint(str(work), 'bold')}")
    return 0


def cmd_src_rebuild(args: argparse.Namespace) -> int:
    from makepatch.source import workspace

    cfg = SourceConfig.load(_root(args))
    sources, features = workspace.rebuild(cfg)
    counts = f"{paint(str(sources), 'bold')} source patch(es) and {paint(str(features), 'bold')} feature patch(es)"
    print(f"{paint('wrote', 'green')} {counts}")
    return 0


def cmd_src_fixup(args: argparse.Namespace) -> int:
    from makepatch.source import workspace

    cfg = SourceConfig.load(_root(args))
    workspace.fixup(cfg)
    print(f"{paint('folded changes into the source patch commit', 'green')}; run {_cmd('makepatch src rebuild')} to update patches")
    return 0


def cmd_src_status(args: argparse.Namespace) -> int:
    from makepatch.source import workspace

    cfg = SourceConfig.load(_root(args))
    st = workspace.status(cfg)
    print(f"{paint('upstream:', 'bold')}        {cfg.upstream} @ {paint(cfg.ref, 'cyan')} {paint(f'({st.base[:12]})', 'dim')}")
    print(f"{paint('source files:', 'bold')}    {st.source_files}")
    print(f"{paint('feature commits:', 'bold')} {st.features}")
    print(f"{paint('dirty:', 'bold')}           {paint('yes', 'yellow') if st.dirty else paint('no', 'green')}")
    if st.in_progress:
        print(f"{paint('in progress:', 'bold')}     {paint(st.in_progress, 'yellow', 'bold')}")
    return 0


# -- pkg -------------------------------------------------------------------------------


def cmd_pkg_edit(args: argparse.Namespace) -> int:
    from makepatch.package import edit

    cfg = PackageConfig.load(_root(args))
    path = edit.edit(cfg, _env(args), args.package, force=args.force)
    print(f"edit the files in {paint(str(path), 'bold')}, then run {_cmd(f'makepatch pkg commit {args.package}')}")
    return 0


def cmd_pkg_commit(args: argparse.Namespace) -> int:
    from makepatch.package import edit

    cfg = PackageConfig.load(_root(args))
    patch, action = edit.commit(cfg, _env(args), args.package, keep=args.keep)
    if patch is None:
        print(f"no changes; {paint(args.package, 'bold')}: {_action(action)}")
    else:
        wrote = paint(str(patch.relative_to(cfg.root)), "bold")
        print(f"{paint('wrote', 'green')} {wrote}; {paint(args.package, 'bold')}: {_action(action)}")
    return 0


def _print_results(results) -> int:
    failed = 0
    for r in results:
        if r.error:
            failed += 1
            print(f"{_err(r.key, 'bold')}: {_err('failed:', 'red', 'bold')} {r.error}", file=sys.stderr)
        else:
            print(f"{paint(r.key, 'bold')}: {_action(r.action)}")
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
    print(f"{paint(dist.key, 'bold')}: {_action('reverted' if done else 'not patched')}")
    if done:
        _note("the start-up hook re-applies it unless the patch file is removed")
    return 0


def cmd_pkg_status(args: argparse.Namespace) -> int:
    from makepatch.package.apply import discover

    cfg = PackageConfig.load(_root(args))
    env = _env(args)
    patches = discover(cfg)
    dists = {d.key: d for d in env.distributions()}
    keys = sorted(set(patches) | {k for k, d in dists.items() if d.marker()})
    if not keys:
        print(paint("no package patches", "dim"))
    for key in keys:
        patch, dist = patches.get(key), dists.get(key)
        if dist is None:
            state = paint("not installed", "red")
        elif patch is None:
            state = paint("patched, but the patch file is gone", "red")
        elif dist.version != patch.version:
            state = paint(f"version mismatch (installed {dist.version})", "red")
        else:
            marker = dist.marker()
            if marker is None:
                state = paint("not applied", "yellow")
            elif marker.get("sha256") != patch.sha256:
                state = paint("outdated", "yellow")
            else:
                state = paint("applied", "green")
        print(f"{paint(patch.path.name if patch else key, 'bold')}: {state}")
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
        print(f"{_err('makepatch: error:', 'red', 'bold')} {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
