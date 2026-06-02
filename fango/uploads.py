"""User-uploaded image storage.

Agents POST base64-encoded image bytes via ``fango_upload_image``; we
write them to ``data/uploads/<sha256>.<ext>`` (sha256 of the bytes →
identical uploads dedup automatically) and hand back a stable absolute
URL that the same agent can immediately pass to ``fango_attach_image``.

Validation is deliberately defensive:

* The image must base64-decode to non-empty bytes.
* Size capped at ``MAX_SIZE_BYTES`` (5 MB).
* Magic-byte sniff identifies one of JPEG / PNG / WebP / GIF — we reject
  everything else (SVG in particular is unsafe: it can carry script).
"""
from __future__ import annotations

import base64
import hashlib
from pathlib import Path

from .config import REPO_ROOT, load_settings


UPLOADS_DIR = REPO_ROOT / "data" / "uploads"

MAX_SIZE_BYTES = 5 * 1024 * 1024   # 5 MB

# (sha256-extension, content-type) for the formats we accept.
_FORMAT_BY_EXT = {
    "jpg":  "image/jpeg",
    "png":  "image/png",
    "webp": "image/webp",
    "gif":  "image/gif",
}


class UploadError(Exception):
    """Raised when an upload fails validation."""


def _detect_mime(data: bytes) -> str | None:
    """Magic-byte sniff. Returns the IANA mime type or None."""
    if len(data) < 12:
        return None
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return None


def _ext_for(mime: str) -> str:
    for ext, m in _FORMAT_BY_EXT.items():
        if m == mime:
            return ext
    raise UploadError(f"no extension for mime {mime}")


def save_image(image_base64: str) -> dict:
    """Decode a base64 (optionally data-URL) payload, then validate + dedup +
    write via :func:`save_image_bytes`. Returns a dict the MCP tool can ship
    straight back to the caller."""
    if not image_base64 or not isinstance(image_base64, str):
        raise UploadError("image_base64 must be a non-empty string")
    # Strip a possible data-URL prefix.
    payload = image_base64
    if payload.startswith("data:"):
        _, _, payload = payload.partition(",")
    try:
        data = base64.b64decode(payload, validate=False)
    except (ValueError, TypeError) as exc:
        raise UploadError(f"invalid base64: {exc}") from None
    return save_image_bytes(data)


def save_image_bytes(data: bytes) -> dict:
    """Validate + dedup + write raw image bytes. Returns the same shape as
    :func:`save_image` (``url`` / ``sha256`` / ``size_bytes`` / ``mime`` /
    ``reused``). Used by the base64 upload path and by server-side image
    self-hosting (e.g. caching an unfurled og:image)."""
    if not data:
        raise UploadError("decoded image is empty")
    if len(data) > MAX_SIZE_BYTES:
        raise UploadError(
            f"image is {len(data)} bytes; max is {MAX_SIZE_BYTES} bytes ({MAX_SIZE_BYTES // (1024*1024)} MB)"
        )
    mime = _detect_mime(data)
    if mime is None:
        raise UploadError(
            "unrecognised image format — only JPEG, PNG, WebP and GIF are accepted"
        )
    sha = hashlib.sha256(data).hexdigest()
    ext = _ext_for(mime)
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    path = UPLOADS_DIR / f"{sha}.{ext}"
    reused = path.exists()
    if not reused:
        # Atomic-ish write so half-written files don't pollute the dedup
        # cache if the process dies mid-write.
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.rename(path)
    base = (load_settings().public_base_url or "").rstrip("/")
    url_path = f"/uploads/{sha}.{ext}"
    return {
        "url": f"{base}{url_path}" if base else url_path,
        "sha256": sha,
        "size_bytes": len(data),
        "mime": mime,
        "reused": reused,
    }
