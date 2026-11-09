"""Uploaded documents are untrusted: identify them by content, bound their size, normalise images,
and render a preview once, here, with a pixel cap.

Any failure to parse a file, whatever the library raises, is a RejectedDocumentError: a malformed
upload is refused with a reason, never a server error.
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass

import pypdfium2 as pdfium
from PIL import Image, ImageOps
from pypdf import PdfReader

MAX_BYTES = 10 * 1024 * 1024
MAX_PDF_PAGES = 10
MAX_PIXELS = 40_000_000
#: Longest side of an image sent to the model, and of every preview (keeps tokens, memory and cost
#: bounded whatever page size a PDF declares).
MAX_SIDE = 2000
PREVIEW_SCALE = 1.5  # 108 dpi for an A4 page


class RejectedDocumentError(ValueError):
    """The upload is not a PDF or image we accept."""


@dataclass(frozen=True)
class SourceDocument:
    #: What the model reads: the PDF as uploaded, or the normalised PNG of an image.
    content: bytes
    media_type: str
    filename: str
    #: The upload byte for byte, and its sniffed type (what "download" returns).
    original: bytes = b""
    original_media_type: str = ""
    #: First page as PNG, at most MAX_SIDE pixels on its longest side.
    preview_png: bytes = b""

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()

    @property
    def original_sha256(self) -> str:
        return hashlib.sha256(self.original or self.content).hexdigest()


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


def safe_filename(filename: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._- " else "_" for ch in filename)[:120] or "upload"


def load(content: bytes, filename: str) -> SourceDocument:
    if not content:
        raise RejectedDocumentError("empty file")
    if len(content) > MAX_BYTES:
        raise RejectedDocumentError("file is larger than 10 MB")
    media_type = sniff(content)
    if media_type is None:
        raise RejectedDocumentError("only PDF, PNG, JPEG and WebP files are accepted")
    name = safe_filename(filename)
    if media_type == "application/pdf":
        _check_pdf(content)
        return SourceDocument(content, media_type, name, content, media_type, _pdf_preview(content))
    png = _normalise_image(content)
    return SourceDocument(png, "image/png", name, content, media_type, png)


def _check_pdf(content: bytes) -> None:
    try:
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted:
            raise RejectedDocumentError("encrypted PDFs are not accepted")
        pages = len(reader.pages)
    except RejectedDocumentError:
        raise
    except Exception as e:  # pypdf raises many types on hostile input
        raise RejectedDocumentError("not a readable PDF") from e
    if pages == 0 or pages > MAX_PDF_PAGES:
        raise RejectedDocumentError(f"PDFs must have 1 to {MAX_PDF_PAGES} pages")


def _pdf_preview(content: bytes) -> bytes:
    """First page rasterised with pdfium, scaled so neither side exceeds MAX_SIDE pixels."""
    try:
        pdf = pdfium.PdfDocument(content)
    except Exception as e:  # a PDF pypdf accepts but pdfium cannot open
        raise RejectedDocumentError("not a readable PDF") from e
    try:
        page = pdf[0]
        width, height = page.get_size()  # points
        if width <= 0 or height <= 0:
            raise RejectedDocumentError("the PDF page has no size")
        scale = min(PREVIEW_SCALE, MAX_SIDE / max(width, height))
        image = page.render(scale=scale).to_pil()
        out = io.BytesIO()
        image.save(out, format="PNG", optimize=True)
        return out.getvalue()
    except RejectedDocumentError:
        raise
    except Exception as e:
        raise RejectedDocumentError("the PDF could not be rendered") from e
    finally:
        pdf.close()


def _normalise_image(content: bytes) -> bytes:
    """Apply EXIF rotation, cap the size, strip metadata, re-encode as PNG."""
    try:
        with Image.open(io.BytesIO(content)) as probe:
            # The header gives the size before any pixel is decoded: refuse decompression bombs here
            # (Pillow's own guard only warns between 1x and 2x its limit).
            width, height = probe.size
            if width * height > MAX_PIXELS:
                raise RejectedDocumentError(f"images are limited to {MAX_PIXELS // 1_000_000} megapixels")
            probe.verify()
        with Image.open(io.BytesIO(content)) as opened:
            upright: Image.Image = ImageOps.exif_transpose(opened)
            rgb = upright.convert("RGB")
            rgb.thumbnail((MAX_SIDE, MAX_SIDE))
            out = io.BytesIO()
            rgb.save(out, format="PNG", optimize=True)
            return out.getvalue()
    except RejectedDocumentError:
        raise
    except Exception as e:  # PIL raises OSError, ValueError, SyntaxError, DecompressionBombError, ...
        raise RejectedDocumentError("not a readable image") from e
