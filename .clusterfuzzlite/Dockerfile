# Where ClusterFuzzLite builds the fuzz targets of fuzz/: OSS-Fuzz's image, with Python 3.11,
# atheris and libFuzzer. Pinned by digest; Dependabot proposes newer ones.
FROM gcr.io/oss-fuzz-base/base-builder-python:v1@sha256:21f6fab17ace4dc76e1c56de91ee95d3d03da021df94b41056ba036f6c6f55e9
COPY . $SRC/fastapi-alcazaba
WORKDIR $SRC/fastapi-alcazaba
COPY .clusterfuzzlite/build.sh $SRC/
