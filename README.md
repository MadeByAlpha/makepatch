# makepatch

A Git-based patch tool for Python packages that integrates with Hatchling. Inspired by [paperweight](https://github.com/PaperMC/paperweight) and `pnpm patch`.

| Mode | Approach | Output | Patch types |
| --- | --- | --- | --- |
| Source patch (`makepatch src`) | paperweight style: fetch an upstream Git repository at a pinned ref and patch it | sdist/wheel of a new package (fork) | source patches, feature patches |
| Package patch (`makepatch pkg`) | pnpm style: patch installed packages in site-packages | patched virtual environment | source patches |

There are two kinds of patches:

- **Source patch**: created with `git diff` and applied with `git apply`. One patch per file.
- **Feature patch**: created with `git format-patch -p --minimal --zero-commit --no-numbered` and applied with `git am --3way`. One patch per commit; available in source patch mode only.

All patches are applied statically; there is no runtime monkey-patching. Only `git` is used, without patchutils.

## Requirements

- Python ≥ 3.10
- git ≥ 2.32 (uses `GIT_CONFIG_GLOBAL`)
- uv or pip

## Source patch mode

A fork repository needs only `pyproject.toml` and `patches/`.

```toml
[build-system]
requires = ["hatchling", "makepatch"]
build-backend = "hatchling.build"

[project]            # Define the fork's metadata yourself.
name = "requests-fork"
version = "2.32.3.post1"
dependencies = ["urllib3>=1.21.1,<3", "idna>=2.5,<4", "charset_normalizer>=2,<4", "certifi>=2017.4.17"]

[tool.makepatch.source]
upstream = "https://github.com/psf/requests.git"
ref = "v2.32.3"                           # a 40-digit commit SHA is recommended; tags and branches work too
include = { "src/requests" = "requests" } # upstream path → wheel path
exclude = ["**/*.pyi"]                    # excluded from sdist/wheel (relative to include paths)
work-exclude = ["docs/", "tests/"]        # excluded from work/ and the build tree (relative to the upstream root)
# work-dir = "work"                       # default
# patches-dir = "patches"                 # default

[tool.hatch.build.hooks.makepatch]        # enables the build hook (settings live in the table above)

[tool.hatch.build.targets.wheel]
bypass-selection = true
```

Add `work/` and `.makepatch/` to `.gitignore` so that they are excluded from the sdist.

### Excluding files

Both options use gitignore syntax (including `!` negation), but differ in base path and effect.

| Option | Base path | Effect |
| --- | --- | --- |
| `exclude` | each `include` path (e.g. `src/requests`) | Excluded from the sdist and wheel. Still present in `work/`. |
| `work-exclude` | upstream root | Not checked out into `work/` or the build tree, via `git sparse-checkout`. Meant to save time and disk space. |

- If a patch touches a file excluded by `work-exclude`, `makepatch src apply` and the build stop with an error.
- After changing `work-exclude`, run `makepatch src apply` again.

### Workflow

```sh
makepatch src apply     # fetch upstream → create work/ → apply source patches → git am feature patches
# edit files in work/
makepatch src fixup     # fold working tree changes into the source patch commit
# or make regular commits in work/ → feature patches
makepatch src rebuild   # regenerate patches/sources/** and patches/features/*.patch
makepatch src status
```

The `work/` repository is laid out as follows:

```
<upstream ref>                 tag makepatch/base
makepatch: source patches      tag makepatch/sources   ← patches/sources/<path>.patch
<feature commits> ...                                  ← patches/features/NNNN-*.patch
```

If a feature patch conflicts, the `git am` session is left in place. Resolve the conflict in `work/`, run `git am --continue`, then run `makepatch src rebuild`.

### Building

```sh
uv build
```

- The build hook does not use `work/`; it applies the patches afresh in `.makepatch/build/tree`. Uncommitted work therefore never leaks into the output.
- The sdist contains the patched sources (`_makepatch/tree/`). Building a wheel from the sdist needs neither git nor network access.
- Set `MAKEPATCH_OFFLINE=1` to use only the `.makepatch/upstream.git` cache.
- Upstream's own build steps (C extensions, etc.) are not run. Pure Python sources are the target.

## Package patch mode

Install makepatch as a development dependency of the project.

```sh
uv add --dev makepatch
```

```sh
uv run makepatch pkg edit requests     # create an editable copy in .makepatch/edit/requests@2.32.3/
# edit files in the copy
uv run makepatch pkg commit requests   # save patches/packages/requests@2.32.3.patch and apply it
uv run makepatch pkg apply             # apply every patch (--check: only check that they apply)
uv run makepatch pkg status
uv run makepatch pkg revert requests   # restore the original files
```

- Patch files are named `<normalized name>@<version>.patch`, with paths relative to site-packages. An error is raised if the installed version differs.
- Deleting a patch file and running `pkg apply` restores that package to its original state.
- The patch directory can be changed with `[tool.makepatch.packages] patches-dir`.
- `--python <interpreter>` targets a different environment.
- Editable installs are not supported; edit their sources directly.

### Compatibility with uv

- **Cache protection**: on Linux, uv installs files from its cache as hardlinks by default (`--link-mode` selects clone, copy or symlink instead). makepatch never modifies files in place: it runs `git apply` on the affected files in a temporary directory and swaps them in with `os.replace`. Only the replaced paths get new inodes, so the uv cache stays untouched. The same holds in symlink mode.
- **Records**: the hashes in `RECORD` are updated, and the applied state (`makepatch.json`, `makepatch.patch`) is written into the dist-info and registered in `RECORD`, so it is removed together with the package.
- **Recovery after reinstall**: `uv sync` does not inspect the contents of installed files, so patches persist. If a reinstall or version change drops a patch, the `makepatch-startup.pth` installed by makepatch detects this at interpreter start-up and re-applies it.
  - Normally it only compares the hashes of the patch files and markers.
  - Only on a mismatch does it take the environment lock and do the same work as `pkg apply`.
  - Disable it with `MAKEPATCH_DISABLE_STARTUP=1`.
  - It is skipped when the interpreter runs with `-I` (isolated), as when uv queries an interpreter.

## Using in Vercel builds

The Vercel build image is based on Amazon Linux 2023. `git` is on the list of pre-installed packages, but `uv` is not, so it may need to be installed separately. Rather than relying on the start-up hook during builds, run `pkg apply` explicitly right after installation. If applying fails, the build stops with exit code 1.

```json
{
  "installCommand": "uv sync --frozen && uv run makepatch pkg apply"
}
```

## Colored output

The CLI colors its output only when writing to a terminal. Set `NO_COLOR=1` to turn colors off, or `FORCE_COLOR=1` to keep them when the output is redirected (e.g. in CI logs). `NO_COLOR` takes precedence.

## Development

```sh
uv sync
uv run pytest
```
