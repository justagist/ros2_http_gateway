# Releasing

Publishing is a manual act. The workflow exists; pulling its trigger is yours.

```bash
git tag v0.1.0 && git push origin v0.1.0
```

That is the whole release. `.github/workflows/release.yml` fires on `v*` tags only -- no
`workflow_dispatch`, no branch trigger -- and then builds the sdist and wheel, runs
`twine check --strict`, refuses to continue if the tag does not match
`ros2_http_gateway/_version.py`, publishes to PyPI, and creates the GitHub release with this
version's CHANGELOG section as its body.

## Before the first tag: PyPI trusted publishing

One-time setup, in the PyPI web UI, by a maintainer. It cannot be scripted and no automation
here attempts it.

1. Sign in to <https://pypi.org> and go to **Your projects** → **Publishing**, or for a project
   that does not exist yet, <https://pypi.org/manage/account/publishing/> and add a *pending*
   publisher.
2. Add a **GitHub** publisher with:
   - Owner: `justagist`
   - Repository: `ros2_http_gateway`
   - Workflow name: `release.yml`
   - Environment: `pypi`
3. In the GitHub repository, **Settings → Environments → New environment**, named `pypi`. Add
   required reviewers if you want a human gate between the tag and the upload.

Trusted publishing means there is no API token anywhere: PyPI verifies the workflow's OIDC
identity. If you would rather use a token, replace the `pypa/gh-action-pypi-publish` step's
implicit OIDC with `password: ${{ secrets.PYPI_API_TOKEN }}` and store the token as a repository
secret -- but the OIDC path has nothing to leak, so prefer it.

## Release checklist

1. `python3 -m pytest` on a ROS distro, and once without ROS.
2. `python3 bench/run.py --out bench/RESULTS.md` and commit if the numbers moved.
3. Move the `[Unreleased]` heading in `CHANGELOG.md` to `[<version>] - <date>` and add a fresh
   empty `[Unreleased]`.
4. Bump `ros2_http_gateway/_version.py`. Nothing else: `pyproject.toml` reads it,
   `setup.py` executes it, and `package.xml` is checked against it by
   `test/test_packaging.py`, which fails on drift.
5. `python3 -m build && python3 -m twine check --strict dist/*` locally.
6. Commit, push, and let CI go green.
7. Tag and push the tag.

## Verifying afterwards

```bash
python3 -m venv /tmp/verify && /tmp/verify/bin/pip install ros2-http-gateway
/tmp/verify/bin/ros2-http-gateway --version
```

Without a sourced ROS environment that should print the version, and any command that needs the
graph should fail with the `rclpy not found` hint rather than a traceback.
