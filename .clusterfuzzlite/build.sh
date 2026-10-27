#!/bin/bash -eu
# Builds each fuzz/*_fuzzer.py into a fuzzer (a PyInstaller bundle that atheris drives), with the
# package and its zstd extra installed. As everywhere in the project, pip takes no release younger
# than three days; the image's pip is older than the option, hence the upgrade first.
python3 -m pip install --quiet "pip==26.2.2"
# The version comes from git (hatch-vcs), which the build may not see: any will do here.
SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0 PIP_UPLOADED_PRIOR_TO=P3D python3 -m pip install ".[zstd]"

for fuzzer in fuzz/*_fuzzer.py; do
  # backports is a namespace package, and zstd is imported where it's first needed.
  compile_python_fuzzer "$fuzzer" --hidden-import backports.zstd
done
