#!/bin/bash -eu
# Builds each fuzz/*_fuzzer.py into a fuzzer (a PyInstaller bundle that atheris drives). The
# package's requirements and its zstd extra come pinned by hash from requirements.txt, resolved for
# the image's Python by scripts/fuzz_requirements.py; the package is the checkout, which PyInstaller
# finds through --paths.
python3 -m pip install --require-hashes --only-binary :all: -r .clusterfuzzlite/requirements.txt

for fuzzer in fuzz/*_fuzzer.py; do
  # backports is a namespace package, and zstd is imported where it's first needed.
  compile_python_fuzzer "$fuzzer" --paths "$PWD" --hidden-import backports.zstd
done
