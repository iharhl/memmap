# Releasing memmap

A release is a GitHub release with the wheel and sdist attached. Publishing the release runs
[`release.yml`](.github/workflows/release.yml), which builds both files, checks them and uploads
them. Nothing is published to PyPI.

## 1. Bump the version

`main` is protected, so the bump goes through a pull request like any other change.

```bash
git switch main && git pull
```

```bash
git switch -c release/v0.2.0
```

```bash
uv version --bump minor
```

`--bump` takes `patch` (0.1.0 to 0.1.1), `minor` (0.1.0 to 0.2.0) or `major` (0.1.0 to 1.0.0). It
updates `version` in `pyproject.toml` and in `uv.lock`. Commit both, push, open a pull request and
merge it once the checks pass.

## 2. Publish the release

From the terminal:

```bash
gh release create v0.2.0 --target main --generate-notes
```

Or on GitHub, under Releases, choose "Draft a new release":
- Tag: type `v0.2.0` and pick "Create new tag on publish".
- Target: `main`.
- "Generate release notes" lists the pull requests merged since the last release.
- "Publish release".

The tag is `v` followed by the version in `pyproject.toml`. A draft release does not start the
workflow until it is published. A pre-release does.

## 3. What the workflow does

1. Fails if the tag without its `v` differs from the version in `pyproject.toml`.
2. Runs `pytest`.
3. Builds `memmap-0.2.0-py3-none-any.whl` and `memmap-0.2.0.tar.gz`.
4. Installs the wheel in a clean venv and runs `memmap guide` and `memmap check` from it.
5. Uploads both files to the release's Assets.

It takes about a minute. Follow it in the Actions tab.

## 4. Install a release

```bash
pipx install https://github.com/iharhl/memmap/releases/download/v0.2.0/memmap-0.2.0-py3-none-any.whl
```

`pip install` with the same URL works too. Add `[raster]` after the file name for PNG and PDF
output, which also needs the system cairo library.

## When something fails

- **Tag and version differ**, for example because the bump was not merged: nothing was built.
  Delete the release and its tag, merge the bump, and publish again.

  ```bash
  gh release delete v0.2.0 --cleanup-tag
  ```

- **A later step fails**: the release stays published without assets. Fix the cause and choose
  "Re-run jobs" on the failed run. The upload replaces files already attached.

GitHub runs the workflow file as it is in the tagged commit, so a fix to `release.yml` itself only
applies to releases tagged after that fix is merged. For an older tag, delete the release and tag
and publish again on a newer commit.
