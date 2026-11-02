"""Private, local-only storage for intake uploads.

Nothing in this module is reachable through Django's static or media file
serving: uploads live outside ``BASE_DIR/staticfiles`` and are only handed out
by an authenticated, household-scoped view. Files are stored with ``0600``
permissions inside a ``0700`` household directory and are never addressed by a
user-supplied path or filename.
"""

import hashlib
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from django.conf import settings
from django.core.exceptions import ValidationError

ALLOWED_IMAGE_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
# A declared type may use these aliases; only the canonical form is stored.
_TYPE_ALIASES = {"image/jpg": "image/jpeg", "image/pjpeg": "image/jpeg"}

HARD_MAX_IMAGE_BYTES = 15 * 1024 * 1024
MAX_IMAGE_DIMENSION = 12000
MAX_IMAGE_PIXELS = 40_000_000

_DEFAULT_MAX_BYTES = 5 * 1024 * 1024
MIN_IMAGE_BYTES = 32
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"
_KEY_RE = re.compile(r"^[0-9a-f-]{36}/[A-Za-z0-9._-]{1,128}$")


@dataclass(frozen=True)
class UploadInfo:
    mime: str
    size: int
    sha256: str
    width: int | None
    height: int | None


def upload_root() -> Path:
    configured = getattr(settings, "INTAKE_UPLOAD_ROOT", None)
    root = Path(configured) if configured else Path(settings.BASE_DIR) / "private" / "intake"
    return root.resolve()


def max_image_bytes() -> int:
    configured = getattr(settings, "INTAKE_MAX_IMAGE_BYTES", _DEFAULT_MAX_BYTES)
    try:
        configured = int(configured)
    except (TypeError, ValueError):
        configured = _DEFAULT_MAX_BYTES
    return max(1, min(configured, HARD_MAX_IMAGE_BYTES))


def extension_for(mime: str) -> str:
    return ALLOWED_IMAGE_TYPES.get(mime, ".bin")


def sniff_image_type(data: bytes) -> str | None:
    if data.startswith(_PNG_MAGIC):
        return "image/png"
    if data.startswith(_JPEG_MAGIC):
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _png_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) >= 24 and data[12:16] == b"IHDR":
        width = int.from_bytes(data[16:20], "big")
        height = int.from_bytes(data[20:24], "big")
        return width, height
    return None


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    if not data.startswith(_JPEG_MAGIC):
        return None
    index = 2
    length = len(data)
    frame_markers = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
    while index + 4 <= length:
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker == 0xFF:
            index += 1
            continue
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7 or marker == 0x01:
            index += 2
            continue
        segment_length = int.from_bytes(data[index + 2:index + 4], "big")
        if segment_length < 2:
            return None
        if marker in frame_markers:
            if index + 9 > length:
                return None
            height = int.from_bytes(data[index + 5:index + 7], "big")
            width = int.from_bytes(data[index + 7:index + 9], "big")
            return width, height
        index += 2 + segment_length
    return None


def _webp_dimensions(data: bytes) -> tuple[int, int] | None:
    chunk = data[12:16]
    if chunk == b"VP8X" and len(data) >= 30:
        width = 1 + int.from_bytes(data[24:27], "little")
        height = 1 + int.from_bytes(data[27:30], "little")
        return width, height
    if chunk == b"VP8 " and len(data) >= 30 and data[23:26] == b"\x9d\x01\x2a":
        width = int.from_bytes(data[26:28], "little") & 0x3FFF
        height = int.from_bytes(data[28:30], "little") & 0x3FFF
        return width, height
    if chunk == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
        bits = int.from_bytes(data[21:25], "little")
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
        return width, height
    return None


def image_dimensions(data: bytes, mime: str) -> tuple[int, int] | None:
    if mime == "image/png":
        return _png_dimensions(data)
    if mime == "image/jpeg":
        return _jpeg_dimensions(data)
    if mime == "image/webp":
        return _webp_dimensions(data)
    return None


def validate_upload(data: bytes | None, declared_content_type: str = "") -> UploadInfo:
    """Validate upload bytes and return bounded, trusted metadata.

    The declared content type is advisory: the actual type is sniffed from the
    bytes and a mismatch is rejected rather than trusting the client.
    """
    if not data:
        raise ValidationError({"image": "Choose a JPEG, PNG, or WebP photo to upload."})
    if len(data) < MIN_IMAGE_BYTES:
        raise ValidationError({"image": "The uploaded image is too small to be a photo."})
    limit = max_image_bytes()
    if len(data) > limit:
        raise ValidationError({"image": f"Photos must be {limit // (1024 * 1024)} MB or smaller."})
    mime = sniff_image_type(data)
    if mime is None:
        raise ValidationError({"image": "Only JPEG, PNG, or WebP photos are accepted."})
    declared = (declared_content_type or "").split(";")[0].strip().lower()
    declared = _TYPE_ALIASES.get(declared, declared)
    if declared and declared != mime:
        raise ValidationError({"image": "The uploaded file does not match its declared image type."})
    dimensions = image_dimensions(data, mime)
    if dimensions is not None:
        width, height = dimensions
        if width <= 0 or height <= 0 or width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION:
            raise ValidationError({"image": "Photo dimensions are outside the accepted range."})
        if width * height > MAX_IMAGE_PIXELS:
            raise ValidationError({"image": "Photo resolution is too large."})
    return UploadInfo(
        mime=mime,
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        width=dimensions[0] if dimensions else None,
        height=dimensions[1] if dimensions else None,
    )


def save_private_image(household_id, data: bytes, mime: str) -> str:
    """Write bytes to the private intake root and return a relative storage key."""
    if mime not in ALLOWED_IMAGE_TYPES:
        raise ValidationError({"image": "Unsupported image type."})
    root = upload_root()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(root, 0o700)
    except OSError:  # pragma: no cover - platform/filesystem dependent
        pass
    directory = root / str(household_id)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:  # pragma: no cover - platform/filesystem dependent
        pass
    filename = f"{uuid.uuid4().hex}{extension_for(mime)}"
    path = directory / filename
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return f"{household_id}/{filename}"


def image_path(key: str) -> Path:
    """Resolve a storage key inside the private root, refusing traversal."""
    if not key or not _KEY_RE.match(key):
        raise ValueError("Invalid private image key.")
    pure = PurePosixPath(key)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise ValueError("Invalid private image key.")
    root = upload_root()
    candidate = (root / pure).resolve()
    if root not in candidate.parents:
        raise ValueError("Invalid private image key.")
    return candidate


def read_private_image(key: str) -> bytes:
    path = image_path(key)
    if not path.is_file():
        raise FileNotFoundError("Private image is missing.")
    return path.read_bytes()


def delete_private_image(key: str) -> None:
    path = image_path(key)
    path.unlink(missing_ok=True)
