"""Uploaded documents are untrusted: identify them by content, bound their size, and normalise images."""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass

from PIL import Image, ImageOps
from pypdf import PdfReader
from pypdf.errors import PdfReadError

MAX_BYTES = 10 * 1024 * 1024
MAX_PDF_PAGES = 10
MAX_PIXELS = 40_000_000
#: Longest image side sent to the model; larger scans are downscaled (keeps tokens and cost bounded).
MAX_SIDE = 2000


class RejectedDocumentError(ValueError):
    """The upload is not a PDF or image we accept."""


@dataclass(frozen=True)
class SourceDocument:
    content: bytes
    media_type: str
    filename: str

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


def sniff(content: bytes) -> str | None:
    """Media type from magic bytes; the filename and declared content type are not trusted."""
    if content.startswith(b"%PDF-"):
        return "application/pdf"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    return None


def load(content: bytes, filename: str) -> SourceDocument:
    if not content:
        raise RejectedDocumentError("empty file")
    if len(content) > MAX_BYTES:
        raise RejectedDocumentError("file is larger than 10 MB")
    media_type = sniff(content)
    if media_type is None:
        raise RejectedDocumentError("only PDF, PNG, JPEG and WebP files are accepted")
    safe_name = "".join(ch if ch.isalnum() or ch in "._- " else "_" for ch in filename)[:120] or "upload"
    if media_type == "application/pdf":
        _check_pdf(content)
        return SourceDocument(content, media_type, safe_name)
    return SourceDocument(_normalise_image(content), "image/png", safe_name)


def _check_pdf(content: bytes) -> None:
    try:
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted:
            raise RejectedDocumentError("encrypted PDFs are not accepted")
        pages = len(reader.pages)
    except PdfReadError as e:
        raise RejectedDocumentError("not a readable PDF") from e
    if pages == 0 or pages > MAX_PDF_PAGES:
        raise RejectedDocumentError(f"PDFs must have 1 to {MAX_PDF_PAGES} pages")


def _normalise_image(content: bytes) -> bytes:
    """Apply EXIF rotation, cap the size, strip metadata, re-encode as PNG."""
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    try:
        with Image.open(io.BytesIO(content)) as probe:
            probe.verify()
        with Image.open(io.BytesIO(content)) as opened:
            upright: Image.Image = ImageOps.exif_transpose(opened)
            rgb = upright.convert("RGB")
            rgb.thumbnail((MAX_SIDE, MAX_SIDE))
            out = io.BytesIO()
            rgb.save(out, format="PNG", optimize=True)
            return out.getvalue()
    except (OSError, SyntaxError, Image.DecompressionBombError) as e:
        raise RejectedDocumentError("not a readable image") from e
