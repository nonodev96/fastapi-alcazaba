"""The requirements of the fuzzing build, pinned by hash, in .clusterfuzzlite/requirements.txt.

    python3.11 scripts/fuzz_requirements.py

ClusterFuzzLite builds the fuzzers in OSS-Fuzz's image, whose Python is 3.11: .clusterfuzzlite/
build.sh installs the package's requirements and its zstd extra there from this file, with
--require-hashes. pip resolves them without installing them (`pip install --dry-run --report`) in a
virtual environment of the Python that runs this script, which must be 3.11: pip evaluates
markers (`python_version < "3.13"`) for the Python it runs on, whatever --python-version says. As
in the rest of the build, no release younger than three days. PyPI gives the hash of every wheel
of each release, so that the image's pip can pick the one for its platform.
"""

import datetime
import json
import re
import subprocess
import sys
import tempfile
import tomllib
import urllib.request
import venv
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parents[1]
OUTPUT = ROOT / ".clusterfuzzlite" / "requirements.txt"
#: The Python of OSS-Fuzz's image.
PYTHON = (3, 11)
EXTRAS = ("zstd",)
#: No release younger than this, as everywhere in the project.
AGE = datetime.timedelta(days=3)

HEADER = """\
# The requirements of the fuzzing build (build.sh), for Python 3.11, the one in OSS-Fuzz's image,
# pinned by hash: written by scripts/fuzz_requirements.py (make fuzz-requirements).
"""


def wanted(pyproject: Path) -> list[str]:
    """The package's requirements, with those of EXTRAS."""
    project = tomllib.loads(pyproject.read_text())["project"]
    found = list(project["dependencies"])
    for extra in EXTRAS:
        found += project["optional-dependencies"][extra]
    return found


def normalized(name: str) -> str:
    """The name as PyPI and pip compare it (PEP 503)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def resolve(requirements: list[str], cutoff: datetime.datetime) -> list[tuple[str, str]]:
    """The name and version of everything pip would install for `requirements`, in a new virtual
    environment of this Python with pip 26.2 or later (for --uploaded-prior-to)."""
    with tempfile.TemporaryDirectory() as directory:
        venv.EnvBuilder(with_pip=True).create(directory)
        python = str(Path(directory) / "bin" / "python")
        pip = [python, "-m", "pip", "--disable-pip-version-check"]
        subprocess.run([*pip, "install", "--quiet", "--upgrade", "pip>=26.2"], check=True)  # noqa: S603
        command = [*pip, "install", "--dry-run", "--ignore-installed", "--quiet"]
        command += ["--only-binary", ":all:", "--uploaded-prior-to", cutoff.isoformat()]
        command += ["--report", "-", *requirements]
        output = subprocess.run(command, check=True, capture_output=True, text=True).stdout  # noqa: S603
    report: dict[str, Any] = json.loads(output)
    return sorted(
        (normalized(item["metadata"]["name"]), item["metadata"]["version"])
        for item in report["install"]
    )


def hashes(name: str, version: str, cutoff: datetime.datetime) -> list[str]:
    """The SHA-256 of every wheel of the release uploaded before `cutoff`, from PyPI."""
    url = f"https://pypi.org/pypi/{name}/{version}/json"
    with urllib.request.urlopen(url, timeout=30) as response:
        release: dict[str, Any] = json.load(response)
    found = sorted(
        file["digests"]["sha256"]
        for file in release["urls"]
        if file["packagetype"] == "bdist_wheel"
        and datetime.datetime.fromisoformat(file["upload_time_iso_8601"]) <= cutoff
    )
    if not found:
        raise SystemExit(f"PyPI has no wheel of {name} {version}")
    return found


def render(pins: list[tuple[str, str, list[str]]]) -> str:
    lines = [HEADER]
    for name, version, digests in pins:
        lines.append(f"{name}=={version} \\\n")
        lines.append(" \\\n".join(f"    --hash=sha256:{digest}" for digest in digests) + "\n")
    return "".join(lines)


def main() -> None:
    if sys.version_info[:2] != PYTHON:
        raise SystemExit("Run it with Python 3.11, the one in OSS-Fuzz's image")
    cutoff = datetime.datetime.now(datetime.UTC) - AGE
    pins = [
        (name, version, hashes(name, version, cutoff))
        for name, version in resolve(wanted(ROOT / "pyproject.toml"), cutoff)
    ]
    OUTPUT.write_text(render(pins))
    print(f"{OUTPUT.relative_to(ROOT)}: {', '.join(f'{n}=={v}' for n, v, _ in pins)}")


if __name__ == "__main__":
    main()
