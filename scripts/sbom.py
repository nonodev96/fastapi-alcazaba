"""CycloneDX SBOM of fastapi-alcazaba and its runtime dependencies (every extra, no dev tools).

    python scripts/sbom.py dist/fastapi_alcazaba-X.Y.Z-py3-none-any.whl \
        -o sbom/fastapi-alcazaba.cdx.json

pip resolves what installing the wheel with every extra would install, without installing it
(`pip install --dry-run --report`): the components, their versions, licences, files and hashes,
for this Python and platform. Set PIP_UPLOADED_PRIOR_TO as the rest of the build does.
"""

import argparse
import datetime
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

LICENSE_CLASSIFIER = "License :: OSI Approved :: "
SUPPLIER = "Antonio Mudarra Machuca"


def resolve(wheel: str) -> dict[str, Any]:
    """pip's installation report for the wheel with every extra."""
    command = [sys.executable, "-m", "pip", "install", "--dry-run", "--ignore-installed"]
    command += ["--quiet", "--report", "-", f"{wheel}[all]"]
    output = subprocess.run(command, check=True, capture_output=True, text=True).stdout  # noqa: S603
    return dict(json.loads(output))


def licenses_of(meta: dict[str, Any]) -> list[dict[str, Any]]:
    if expression := meta.get("license_expression"):
        return [{"expression": expression}]
    classifiers = [
        c.removeprefix(LICENSE_CLASSIFIER)
        for c in meta.get("classifier") or []
        if c.startswith(LICENSE_CLASSIFIER)
    ]
    if classifiers:
        return [{"license": {"name": name}} for name in classifiers]
    text = (meta.get("license") or "").strip()
    if text and "\n" not in text and len(text) < 100:
        return [{"license": {"name": text}}]
    return []


def normalized(name: str) -> str:
    return str(canonicalize_name(name))


def purl(meta: dict[str, Any]) -> str:
    return f"pkg:pypi/{normalized(meta['name'])}@{meta['version']}"


def component(item: dict[str, Any]) -> dict[str, Any]:
    meta = item["metadata"]
    found: dict[str, Any] = {
        "type": "library",
        "bom-ref": purl(meta),
        "name": meta["name"],
        "version": meta["version"],
        "purl": purl(meta),
    }
    if licenses := licenses_of(meta):
        found["licenses"] = licenses
    download = item.get("download_info", {})
    hashes = download.get("archive_info", {}).get("hashes", {})
    if "sha256" in hashes:
        found["hashes"] = [{"alg": "SHA-256", "content": hashes["sha256"]}]
    if download.get("url", "").startswith("https://"):
        found["externalReferences"] = [{"type": "distribution", "url": download["url"]}]
    return found


def dependencies(
    items: dict[str, dict[str, Any]],
    environment: dict[str, str],
) -> dict[str, set[str]]:
    """Which components each one requires, with its markers and the extras asked of it (by the
    command, or by the requirements of the others) taken into account."""
    extras = {name: set(item.get("requested_extras") or []) for name, item in items.items()}
    edges: dict[str, set[str]] = {name: set() for name in items}
    changed = True
    while changed:  # an extra asked for may bring in requirements that ask for more
        changed = False
        for name, item in items.items():
            for spec in item["metadata"].get("requires_dist") or []:
                requirement = Requirement(spec)
                target = normalized(requirement.name)
                if target not in items:
                    continue
                marker = requirement.marker
                if marker is not None and not any(
                    marker.evaluate({**environment, "extra": extra})
                    for extra in extras[name] | {""}
                ):
                    continue
                if target != name and target not in edges[name]:
                    edges[name].add(target)
                    changed = True
                if wanted := set(requirement.extras) - extras[target]:
                    extras[target] |= wanted
                    changed = True
    return edges


def timestamp() -> str:
    """From the commit, as the build, when SOURCE_DATE_EPOCH is set: the same tag, the same SBOM."""
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    moment = (
        datetime.datetime.fromtimestamp(int(epoch), datetime.UTC)
        if epoch
        else datetime.datetime.now(datetime.UTC)
    )
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def sbom(report: dict[str, Any]) -> dict[str, Any]:
    items = {normalized(i["metadata"]["name"]): i for i in report["install"]}
    [root] = [name for name, item in items.items() if item.get("is_direct")]
    components = {name: component(item) for name, item in items.items()}
    package = components.pop(root)
    package.update({"licenses": [{"expression": "Apache-2.0"}], "supplier": {"name": SUPPLIER}})
    edges = dependencies(items, report["environment"])
    refs = {name: purl(item["metadata"]) for name, item in items.items()}
    digest = package.get("hashes", [{}])[0].get("content", "")
    # The same wheel, the same serial number.
    serial = uuid.uuid5(uuid.NAMESPACE_URL, f"{package['purl']}#{digest}")
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{serial}",
        "version": 1,
        "metadata": {
            "timestamp": timestamp(),
            "tools": {
                "components": [
                    {"type": "application", "name": "pip", "version": report["pip_version"]}
                ]
            },
            "component": package,
        },
        "components": sorted(components.values(), key=lambda c: str(c["name"]).lower()),
        "dependencies": [
            {"ref": refs[name], "dependsOn": sorted(refs[target] for target in edges[name])}
            for name in sorted(items)
        ],
    }


def validate(document: dict[str, Any]) -> None:
    from cyclonedx.schema import SchemaVersion
    from cyclonedx.validation.json import JsonStrictValidator

    error = JsonStrictValidator(SchemaVersion.V1_5).validate_str(json.dumps(document))
    if error is not None:
        raise SystemExit(f"invalid SBOM: {error}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("wheel", help="the wheel the SBOM is about")
    parser.add_argument("-o", "--output", required=True)
    parser.add_argument("--no-validate", action="store_true", help="skip the schema check")
    args = parser.parse_args()
    document = sbom(resolve(args.wheel))
    if not args.no_validate:
        validate(document)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    components = document["components"]
    licensed = sum(1 for c in components if c.get("licenses"))
    print(f"{args.output}: {len(components)} components, {licensed} with a licence")


if __name__ == "__main__":
    main()
