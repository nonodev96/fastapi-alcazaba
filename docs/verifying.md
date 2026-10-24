# Verifying a release

Every release is signed with [Sigstore](https://www.sigstore.dev/) by the GitHub Actions
workflow that built it: there's no key to trust, only the identity of the workflow
(`OWNER/fastapi-alcazaba`, `.github/workflows/release.yml`, the tag). Replace `OWNER` and
`X.Y.Z` below.

## What PyPI serves

PyPI keeps the PEP 740 attestations published with each file:

```shell
uvx pypi-attestations verify pypi \
  --repository https://github.com/OWNER/fastapi-alcazaba \
  pypi:fastapi_alcazaba-X.Y.Z-py3-none-any.whl
```

## GitHub attestations

Build provenance (SLSA v1) and SBOM attestations, for any copy of the files:

```shell
gh attestation verify fastapi_alcazaba-X.Y.Z-py3-none-any.whl --repo OWNER/fastapi-alcazaba
gh attestation verify fastapi_alcazaba-X.Y.Z-py3-none-any.whl --repo OWNER/fastapi-alcazaba \
  --predicate-type https://cyclonedx.org/bom
```

## Sigstore bundles of the GitHub release

Each file of the release has a `.sigstore.json` bundle next to it:

```shell
uvx sigstore verify github fastapi_alcazaba-X.Y.Z-py3-none-any.whl \
  --bundle fastapi_alcazaba-X.Y.Z-py3-none-any.whl.sigstore.json \
  --repository OWNER/fastapi-alcazaba --ref refs/tags/vX.Y.Z --trigger push
```

## The SBOM

`fastapi-alcazaba-X.Y.Z.cdx.json` (CycloneDX 1.5) lists every runtime dependency, with every extra,
at the versions locked for the release, with their licences. Scan it for known vulnerabilities:

```shell
osv-scanner scan --sbom fastapi-alcazaba-X.Y.Z.cdx.json
```
