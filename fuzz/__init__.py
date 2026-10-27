"""Fuzz targets: what reads data that comes from outside (uploads, cursors, webhooks, stored
entries, audit details), with the properties it must keep whatever it's given.

ClusterFuzzLite runs them with atheris (`.clusterfuzzlite/`, `.github/workflows/fuzzing.yml`);
`tests/unit/test_fuzz_targets.py` runs them as plain tests, on random inputs and on the cases
fuzzing found.
"""
