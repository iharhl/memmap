# Releasing memmap

A release is a GitHub release with the wheel and sdist attached. Publishing the release runs
[`release.yml`](.github/workflows/release.yml), which builds both files, checks them and uploads
them. Nothing is published to PyPI.

## Strategy

Releases are cut from `main`. There is no long-lived release branch:

1. A short-lived prep branch bumps the version and brings the docs and tooling up to date.
2. It is merged into `main` through a pull request, like any other change.
3. The release is published on `main`, which creates the tag `vX.Y.Z` on the merged commit.

A release branch, `release/X.Y`, is only made when an older version needs a fix while `main`
has moved on.

## 1. Prepare the release

`main` is protected, so the preparation goes through a pull request.

```bash
git switch main && git pull
```

```bash
git switch -c release-prep/v0.3.0
```

```bash
uv version --bump minor
```

`--bump` takes `patch` (0.1.0 to 0.1.1), `minor` (0.1.0 to 0.2.0) or `major` (0.1.0 to 1.0.0). It
updates `version` in `pyproject.toml` and in `uv.lock`, but not `__version__` in
`memmap/__init__.py`: set that one by hand. A test fails when the two differ.

On the same branch, bring up to date what a user of the new version reads:
- The install URLs in `README.md` and in [Install a release](#4-install-a-release) below.
- `README.md` and `docs/` for features added since the last release.
- `AGENTS.md`, which `memmap guide` ships, for new diagnostic codes and recipes.

Commit, push, open a pull request and merge it once the checks pass.

## 2. Publish the release

Publish right after the merge, so the tag lands on the prep commit and not on a later change.
From the terminal:

```bash
git switch main && git pull
```

```bash
gh release create v0.3.0 --target main --generate-notes
```

Or on GitHub, under Releases, choose "Draft a new release":
- Tag: type `v0.3.0` and pick "Create new tag on publish".
- Target: `main`.
- "Generate release notes" lists the pull requests merged since the last release.
- "Publish release".

The tag is `v` followed by the version in `pyproject.toml`. Tag the commit on `main`, never one
on the prep branch: after a squash merge, the branch commits are not part of `main`. A draft
release does not start the workflow until it is published. A pre-release does.

## 3. What the workflow does

1. Fails if the tag without its `v` differs from the version in `pyproject.toml`.
2. Runs `pytest`.
3. Builds `memmap-0.3.0-py3-none-any.whl` and `memmap-0.3.0.tar.gz`.
4. Installs the wheel in a clean venv and runs `memmap guide` and `memmap check` from it.
5. Uploads both files to the release's Assets.

It takes about a minute. Follow it in the Actions tab.

## 4. Install a release

```bash
pipx install https://github.com/iharhl/memmap/releases/download/v0.3.0/memmap-0.3.0-py3-none-any.whl
```

`pip install` with the same URL works too. For PNG and PDF output, which also needs the system
cairo library, name the `raster` extra:

```bash
pipx install "memmap[raster] @ https://github.com/iharhl/memmap/releases/download/v0.3.0/memmap-0.3.0-py3-none-any.whl"
```

## When something fails

- **Tag and version differ**, for example because the bump was not merged: nothing was built.
  Delete the release and its tag, merge the bump, and publish again.

  ```bash
  gh release delete v0.3.0 --cleanup-tag
  ```

- **A later step fails**: the release stays published without assets. Fix the cause and choose
  "Re-run jobs" on the failed run. The upload replaces files already attached.

GitHub runs the workflow file as it is in the tagged commit, so a fix to `release.yml` itself only
applies to releases tagged after that fix is merged. For an older tag, delete the release and tag
and publish again on a newer commit.
