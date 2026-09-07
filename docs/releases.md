# CI and releases

GitHub Actions runs the checks workflow for pushes to `main`, pull requests, and manual runs. It builds one source distribution and wheel, checks their metadata, and tests the same wheel on Python 3.11 and 3.14. Each interpreter runs the Python suite, Ruff, frontend syntax/format checks, reproducible terminal bundle checks, and an installed-package Chromium test. Test reports, coverage XML, browser results, and distributions are retained as workflow artifacts for seven days.

Local `.scratch` coordination data and review output are ignored whether `.scratch` is a directory or a symlink. They must remain untracked and are not release artifacts.

## One-time PyPI setup

The repository uses [PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/using-a-publisher/), with the upload isolated in the `pypi` GitHub environment. No long-lived PyPI token is needed. The workflow accepts version tags and requires the tagged commit to be on `main`. The named GitHub environment is created when the upload job first uses it.

Before the first release, sign in to the owning PyPI account and add a [pending publisher](https://pypi.org/manage/account/publishing/) with these exact values:

| Field | Value |
| --- | --- |
| PyPI project name | `ctlrm` |
| Owner | `d-chambers` |
| Repository | `ctlrm` |
| Workflow filename | `release.yml` |
| Environment | `pypi` |

The filename is `release.yml`, without the `.github/workflows/` prefix. Pending publisher registration creates the project on the first successful upload; it does not reserve the package name. If the project already exists under your account, add the same publisher in its Publishing settings instead. See the [PyPI instructions](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/).

## Publish a version

Merge the intended changes into `main`, then create a GitHub release from that commit with a new version tag such as `v0.1.0`. Prereleases use canonical tags such as `v0.2.0rc1` and should be marked as prereleases in GitHub. Draft releases do not publish; publishing a release starts the release workflow. A tag push alone does not publish.

Versions are derived from Git tags through hatch-vcs. The workflow accepts `vMAJOR.MINOR.PATCH`, optionally followed by `aN`, `bN`, or `rcN`; it refreshes `main` and the release tag from the remote, then rejects tags outside that format, commits outside `main`, moved or deleted tags, and package metadata that differs from the tag. No source version constant needs updating.

Every published release reruns the full checks workflow on its tagged commit. Only after both Python test jobs pass does the upload job download those exact distributions and publish them with a short-lived PyPI identity and package attestations. Build and test jobs have read-only repository permissions and cannot request the publishing identity.

Follow the release workflow in GitHub Actions, then verify the version on [PyPI](https://pypi.org/project/ctlrm/). Failed validation or tests prevent upload. After correcting a failed upload, use Re-run failed jobs while its distribution artifact is still available. If the seven-day artifact retention has elapsed, use Re-run all jobs to rebuild and retest the packages before uploading. Duplicate files fail explicitly. Published package files are immutable, so a code correction needs a new version.
