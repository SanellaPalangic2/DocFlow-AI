"""Step 1 - Upload validation.

Defense in depth: we check the extension, the size, AND the actual bytes.
A file named invoice.pdf that doesn't start with %PDF- is rejected.
Uploaded bytes are only ever *read* as data - never saved to disk, never executed.
"""
import hashlib
from dataclasses import dataclass

from werkzeug.utils import secure_filename

ALLOWED_EXTENSIONS = {"pdf", "txt"}


class ValidationError(Exception):
    """A user-facing, safe-to-display reason the upload was refused."""


@dataclass
class ValidatedFile:
    safe_name: str
    file_type: str   # 'pdf' | 'txt'
    data: bytes
    size_bytes: int
    sha256: str


def validate_upload(filename: str | None, data: bytes, max_bytes: int) -> ValidatedFile:
    if not filename:
        raise ValidationError("Please choose a file to upload.")

    safe_name = secure_filename(filename)[:120] or "document"
    if "." not in safe_name:
        raise ValidationError("The file has no extension. Supported types: PDF, TXT.")
    ext = safe_name.rsplit(".", 1)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise ValidationError(f"'.{ext}' files are not supported. Supported types: PDF, TXT.")

    size = len(data)
    if size == 0:
        raise ValidationError("The file is empty.")
    if size > max_bytes:
        raise ValidationError(f"The file is too large (limit {max_bytes // (1024 * 1024)} MB).")

    # Content sniffing: the bytes must match the claimed type.
    if ext == "pdf" and not data.lstrip()[:5] == b"%PDF-":
        raise ValidationError("This file does not look like a real PDF.")
    if ext == "txt" and b"\x00" in data[:8192]:
        raise ValidationError("This file looks binary, not plain text.")

    return ValidatedFile(
        safe_name=safe_name,
        file_type=ext,
        data=data,
        size_bytes=size,
        sha256=hashlib.sha256(data).hexdigest(),
    )
