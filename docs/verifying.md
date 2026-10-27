# Verifying a release

Every release is signed with [Sigstore](https://www.sigstore.dev/) by the GitHub Actions
workflow that built it: there's no key to trust, only the identity of the workflow
(`nonodev96/fastapi-alcazaba`, `.github/workflows/release.yml`, the tag). Replace `X.Y.Z`
below with the version. The tools, in a virtual environment of their own:

```shell
python3 -m venv verify && verify/bin/pip install pypi-attestations sigstore
```

## What PyPI serves

PyPI keeps the PEP 740 attestations published with each file:

```shell
verify/bin/pypi-attestations verify pypi \
  --repository https://github.com/nonodev96/fastapi-alcazaba \
  pypi:fastapi_alcazaba-X.Y.Z-py3-none-any.whl
```

## GitHub attestations

Build provenance (SLSA v1) and SBOM attestations, for any copy of the files:

```shell
gh attestation verify fastapi_alcazaba-X.Y.Z-py3-none-any.whl --repo nonodev96/fastapi-alcazaba
gh attestation verify fastapi_alcazaba-X.Y.Z-py3-none-any.whl --repo nonodev96/fastapi-alcazaba \
  --predicate-type https://cyclonedx.org/bom
```

The release carries the provenance too, `fastapi-alcazaba-X.Y.Z.intoto.jsonl` (the Sigstore
bundle of the attestation, one JSON line): with it, `gh attestation verify` doesn't ask GitHub
for it, and checks the repository's name as the certificate recorded it when the release was
built.

```shell
gh attestation verify fastapi_alcazaba-X.Y.Z-py3-none-any.whl --repo nonodev96/fastapi-alcazaba \
  --bundle fastapi-alcazaba-X.Y.Z.intoto.jsonl
```

## Sigstore bundles of the GitHub release

Each file of the release but the provenance has a `.sigstore.json` bundle next to it:

```shell
verify/bin/sigstore verify github fastapi_alcazaba-X.Y.Z-py3-none-any.whl \
  --bundle fastapi_alcazaba-X.Y.Z-py3-none-any.whl.sigstore.json \
  --repository nonodev96/fastapi-alcazaba --ref refs/tags/vX.Y.Z --trigger push
```

## The SBOM

`fastapi-alcazaba-X.Y.Z.cdx.json` (CycloneDX 1.5) lists every runtime dependency, with every extra,
at the versions installing it resolved to when it was released, with their licences and the
hashes of their files. Scan it for known vulnerabilities:

```shell
osv-scanner scan --sbom fastapi-alcazaba-X.Y.Z.cdx.json
```
