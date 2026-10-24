"""CycloneDX SBOM of fastapi-alcazaba and its runtime dependencies (every extra, no dev tools).

    uv sync --frozen --no-dev --all-extras
    uv run --no-sync python scripts/sbom.py --version X.Y.Z -o dist/fastapi-alcazaba.cdx.json

The component list and versions come from uv.lock (`uv export --format cyclonedx1.5`). This adds
what uv leaves out: the version and purl of the package itself and the licence of every component,
read from the metadata of the installed environment.
"""

import argparse
import json
import subprocess
from importlib import metadata
from pathlib import Path
from typing import Any

LICENSE_CLASSIFIER = "License :: OSI Approved :: "


def uv_sbom() -> dict[str, Any]:
    output = subprocess.run(  # noqa: S603
        ["uv", "export", "--frozen", "--format", "cyclonedx1.5", "--no-dev", "--all-extras"],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return dict(json.loads(output))


def license_of(name: str) -> list[dict[str, Any]]:
    try:
        meta = metadata.metadata(name)
    except metadata.PackageNotFoundError:
        return []
    expression = (meta.get_all("License-Expression") or [""])[0]
    if expression:
        return [{"expression": expression}]
    classifiers = [
        c.removeprefix(LICENSE_CLASSIFIER)
        for c in meta.get_all("Classifier") or []
        if c.startswith(LICENSE_CLASSIFIER)
    ]
    if classifiers:
        return [{"license": {"name": name}} for name in classifiers]
    text = (meta.get_all("License") or [""])[0].strip()
    if text and "\n" not in text and len(text) < 100:
        return [{"license": {"name": text}}]
    return []


def enrich(sbom: dict[str, Any], version: str) -> dict[str, Any]:
    root = sbom["metadata"]["component"]
    root.update(
        {
            "version": version,
            "purl": f"pkg:pypi/fastapi-alcazaba@{version}",
            "licenses": [{"expression": "Apache-2.0"}],
            "supplier": {"name": "Antonio Mudarra Machuca"},
        }
    )
    for component in sbom.get("components", []):
        licenses = license_of(component["name"])
        if licenses:
            component["licenses"] = licenses
    return sbom


def validate(sbom: dict[str, Any]) -> None:
    from cyclonedx.schema import SchemaVersion
    from cyclonedx.validation.json import JsonStrictValidator

    error = JsonStrictValidator(SchemaVersion.V1_5).validate_str(json.dumps(sbom))
    if error is not None:
        raise SystemExit(f"invalid SBOM: {error}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("-o", "--output", required=True)
    parser.add_argument("--no-validate", action="store_true", help="skip the schema check")
    args = parser.parse_args()
    sbom = enrich(uv_sbom(), args.version)
    if not args.no_validate:
        validate(sbom)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(sbom, indent=2) + "\n")
    licensed = sum(1 for c in sbom["components"] if c.get("licenses"))
    print(f"{args.output}: {len(sbom['components'])} components, {licensed} with a licence")


if __name__ == "__main__":
    main()
