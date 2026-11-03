"""Checking uploads: the type of a file from its bytes, its size and pixels, a malware scanner,
safe file names; and storing them in S3 (or what speaks its API) by signed URLs and upload
forms. `check_uploaded` (in `fastapi_alcazaba.uploads.direct`, the `uploads` extra) checks what
was uploaded straight to the bucket."""

from fastapi_alcazaba.uploads.checks import (
    IMAGES,
    CheckedFile,
    FastAPIUploads,
    image_size,
    safe_filename,
    sniff,
)
from fastapi_alcazaba.uploads.s3 import PresignedUpload, S3Presigner, UploadForm
from fastapi_alcazaba.uploads.scanning import (
    ClamAVScanner,
    Scanner,
    ScannerUnavailable,
    ScanResult,
)

__all__ = [
    "CheckedFile",
    "ClamAVScanner",
    "PresignedUpload",
    "ScanResult",
    "Scanner",
    "ScannerUnavailable",
    "FastAPIUploads",
    "image_size",
    "IMAGES",
    "S3Presigner",
    "safe_filename",
    "sniff",
    "UploadForm",
]
