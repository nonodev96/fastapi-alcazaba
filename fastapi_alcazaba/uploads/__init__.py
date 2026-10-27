"""Checking uploads: the type of a file from its bytes, its size and pixels, safe file names."""

from fastapi_alcazaba.uploads.checks import (
    IMAGES,
    CheckedFile,
    FastAPIUploads,
    image_size,
    safe_filename,
    sniff,
)

__all__ = [
    "CheckedFile",
    "FastAPIUploads",
    "image_size",
    "IMAGES",
    "safe_filename",
    "sniff",
]
