"""Checking uploads: the type of a file from its bytes, its size and pixels, safe file names;
and storing them in S3 (or what speaks its API) by signed URLs and upload forms."""

from fastapi_alcazaba.uploads.checks import (
    IMAGES,
    CheckedFile,
    FastAPIUploads,
    image_size,
    safe_filename,
    sniff,
)
from fastapi_alcazaba.uploads.s3 import S3Presigner, UploadForm

__all__ = [
    "CheckedFile",
    "FastAPIUploads",
    "image_size",
    "IMAGES",
    "S3Presigner",
    "safe_filename",
    "sniff",
    "UploadForm",
]
