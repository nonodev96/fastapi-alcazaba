# Deployment and releases

Alcazaba is released from GitHub Actions: a `vX.Y.Z` tag builds the distributions, generates the
SBOM, signs everything with Sigstore (no keys to keep), publishes to TestPyPI, checks that
version installs, and publishes to PyPI after a manual approval. `OWNER` below is your GitHub
user.

## One-time setup

### 1. Repository

```shell
git remote add origin git@github.com:OWNER/fastapi-alcazaba.git
git push -u origin main
```

The repository has a history of its own (the original one stays upstream), so no upstream tag
can trigger a release. Then add the URLs to `pyproject.toml`:

```toml
[project.urls]
Homepage = "https://github.com/OWNER/fastapi-alcazaba"
Documentation = "https://OWNER.github.io/fastapi-alcazaba/"
Changelog = "https://github.com/OWNER/fastapi-alcazaba/blob/main/CHANGELOG.md"
```

### 2. Repository settings

- **Actions → General → Workflow permissions:** read repository contents (the workflows ask for
  more per job).
- **Rules → Rulesets:**
    - `main`: require a pull request, the status checks of CI (`Lint and type check`, `Unit and
      integration tests (3.11)` … `(3.14)`, `End-to-end`, `Package`, `Audit dependencies`,
      `Docs`), linear history; block force pushes and deletion.
    - Tags `v*`: only you can create them; block updates and deletion (a published version must
      never move).
- **Environments:**
    - `testpypi`: deployment tags `v*`.
    - `pypi`: deployment tags `v*`, **required reviewer: you** (the approval before PyPI).
    - `github-pages`: created when Pages is enabled.
- **Pages:** source "GitHub Actions".
- **Security:** Dependabot alerts and security updates, secret scanning with push protection,
  private vulnerability reporting (SECURITY.md points there), code scanning (CodeQL and
  Scorecard upload their results).

### 3. Trusted publishers

Before the first release the project doesn't exist on the indexes: add a **pending publisher**
on [TestPyPI](https://test.pypi.org/manage/account/publishing/) and on
[PyPI](https://pypi.org/manage/account/publishing/):

| Field | TestPyPI | PyPI |
| --- | --- | --- |
| PyPI project name | `fastapi-alcazaba` | `fastapi-alcazaba` |
| Owner | `OWNER` | `OWNER` |
| Repository | `fastapi-alcazaba` | `fastapi-alcazaba` |
| Workflow | `release.yml` | `release.yml` |
| Environment | `testpypi` | `pypi` |

No API token is created or stored anywhere: the workflow proves its identity with OIDC.

## Releasing

1. Move the `Unreleased` notes of `CHANGELOG.md` under `## [X.Y.Z] - YYYY-MM-DD` and merge to
   `main`; wait for CI.
2. Tag the merge commit on `main` and push the tag:

    ```shell
    git switch main && git pull
    git tag vX.Y.Z
    git push origin vX.Y.Z
    ```

3. The *Release* workflow:
    1. checks that the tag is a version on `main` and runs every suite;
    2. builds the sdist and the wheel (the version comes from the tag, `SOURCE_DATE_EPOCH` from
       the commit);
    3. generates the CycloneDX SBOM and validates it;
    4. signs: GitHub attestations of build provenance (SLSA) and of the SBOM, plus a Sigstore
       bundle per file;
    5. publishes to TestPyPI and installs that version in a clean environment (smoke test);
    6. waits for your approval in the `pypi` environment, then publishes to PyPI with PEP 740
       attestations;
    7. creates the GitHub release with the distributions, the SBOM, the Sigstore bundles and the
       notes of that version from the changelog;
    8. the *Docs* workflow publishes the site.

Pre-releases (`vX.Y.ZrcN`, `aN`, `bN`) stop after TestPyPI and become GitHub pre-releases.

## If something goes wrong

- Nothing reaches PyPI until you approve it: reject the deployment and fix.
- A version can't be uploaded twice. Published something broken? *Yank* it on PyPI (it stays
  installable only when pinned exactly) and release `X.Y.Z+1`.
- Don't move or reuse a tag that was released.
