"""Renders synthetic invoices as PDFs or as degraded scans.

One layout is drawn through a small canvas interface, either into a PDF (reportlab) or onto an
image (Pillow) that is then rotated, blurred, noised and JPEG-compressed like a phone photo or a
cheap scanner. Hebrew text is reordered for display with python-bidi; DejaVu Sans has the glyphs.
"""

from __future__ import annotations

import io
import random
import zlib
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from bidi import get_display
from PIL import Image, ImageDraw, ImageFilter, ImageFont
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as pdf_canvas

from payables.schemas import DocumentType, ExtractedInvoice
from payables.synth.dataset import BANKS, BANKS_EN, Render, Sample

FONTS = Path(__file__).parent / "fonts"
PAGE_W, PAGE_H = A4  # points
SCAN_DPI = 150


def has_hebrew(text: str) -> bool:
    return any("֐" <= ch <= "׿" for ch in text)


def visual(text: str) -> str:
    """Logical to visual order for right-to-left text."""
    return get_display(text) if has_hebrew(text) else text


class Canvas(Protocol):
    def text(
        self, x: float, y: float, s: str, size: float = 10, bold: bool = False, align: str = "left"
    ) -> None: ...

    def line(self, x1: float, y1: float, x2: float, y2: float) -> None: ...


class PdfCanvas:
    def __init__(self) -> None:
        if "DejaVu" not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont("DejaVu", str(FONTS / "DejaVuSans.ttf")))
            pdfmetrics.registerFont(TTFont("DejaVu-Bold", str(FONTS / "DejaVuSans-Bold.ttf")))
        self.buffer = io.BytesIO()
        # invariant=1 drops timestamps and random IDs, so the same invoice gives the same bytes.
        self.c = pdf_canvas.Canvas(self.buffer, pagesize=A4, invariant=1)

    def text(
        self, x: float, y: float, s: str, size: float = 10, bold: bool = False, align: str = "left"
    ) -> None:
        self.c.setFont("DejaVu-Bold" if bold else "DejaVu", size)
        yy = PAGE_H - y
        if align == "right":
            self.c.drawRightString(x, yy, visual(s))
        elif align == "center":
            self.c.drawCentredString(x, yy, visual(s))
        else:
            self.c.drawString(x, yy, visual(s))

    def line(self, x1: float, y1: float, x2: float, y2: float) -> None:
        self.c.line(x1, PAGE_H - y1, x2, PAGE_H - y2)

    def finish(self) -> bytes:
        self.c.showPage()
        self.c.save()
        return self.buffer.getvalue()


class ImageCanvas:
    def __init__(self) -> None:
        self.scale = SCAN_DPI / 72
        self.img = Image.new("RGB", (int(PAGE_W * self.scale), int(PAGE_H * self.scale)), (250, 249, 245))
        self.draw = ImageDraw.Draw(self.img)
        self._fonts: dict[tuple[float, bool], ImageFont.FreeTypeFont] = {}

    def _font(self, size: float, bold: bool) -> ImageFont.FreeTypeFont:
        key = (size, bold)
        if key not in self._fonts:
            name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
            self._fonts[key] = ImageFont.truetype(str(FONTS / name), int(size * self.scale))
        return self._fonts[key]

    def text(
        self, x: float, y: float, s: str, size: float = 10, bold: bool = False, align: str = "left"
    ) -> None:
        font = self._font(size, bold)
        shown = visual(s)
        width = self.draw.textlength(shown, font=font) / self.scale
        if align == "right":
            x -= width
        elif align == "center":
            x -= width / 2
        self.draw.text((x * self.scale, (y - size) * self.scale), shown, font=font, fill=(25, 25, 30))

    def line(self, x1: float, y1: float, x2: float, y2: float) -> None:
        self.draw.line(
            [(x1 * self.scale, y1 * self.scale), (x2 * self.scale, y2 * self.scale)], fill=(60, 60, 60)
        )

    def finish(self, r: Render, seed: int) -> bytes:
        rng = random.Random(seed)
        img = self.img
        if r.blur:
            img = img.filter(ImageFilter.GaussianBlur(r.blur))
        if r.noise:
            noise = Image.effect_noise(img.size, r.noise).convert("RGB")
            img = Image.blend(img, noise, 0.08 + rng.random() * 0.04)
        if r.angle:
            img = img.rotate(
                r.angle, expand=True, fillcolor=(205, 205, 200), resample=Image.Resampling.BICUBIC
            )
        if r.quarter_turns:
            img = img.rotate(90 * r.quarter_turns, expand=True)
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=r.jpeg_quality)
        return out.getvalue()


LABELS = {
    "he": {
        DocumentType.TAX_INVOICE: "חשבונית מס",
        DocumentType.QUOTE: "הצעת מחיר",
        DocumentType.RECEIPT: "קבלה",
        "number": "מספר",
        "date": "תאריך",
        "due": "לתשלום עד",
        "tax_id": "ח.פ.",
        "to": "לכבוד",
        "item": "תיאור",
        "qty": "כמות",
        "price": "מחיר יחידה",
        "amount": 'סה"כ',
        "subtotal": 'סה"כ לפני מע"מ',
        "vat": 'מע"מ',
        "total": 'סה"כ לתשלום',
        "bank": "פרטי בנק לתשלום",
        "branch": "סניף",
        "account": "חשבון",
    },
    "en": {
        DocumentType.TAX_INVOICE: "TAX INVOICE",
        DocumentType.QUOTE: "PRICE QUOTE",
        DocumentType.RECEIPT: "RECEIPT",
        "number": "No.",
        "date": "Date",
        "due": "Due",
        "tax_id": "Company No.",
        "to": "Bill to",
        "item": "Description",
        "qty": "Qty",
        "price": "Unit price",
        "amount": "Amount",
        "subtotal": "Subtotal",
        "vat": "VAT",
        "total": "Total due",
        "bank": "Pay to",
        "branch": "Branch",
        "account": "Account",
    },
}


def _amount(d: Decimal | None) -> str:
    return "" if d is None else f"₪{d:,.2f}"


def _date(d: date | None) -> str:
    return "" if d is None else d.strftime("%d/%m/%Y")


def draw(c: Canvas, s: Sample) -> None:
    inv: ExtractedInvoice = s.truth
    lang = "en" if s.render.language == "en" else "he"
    rtl = lang == "he"
    labels = LABELS[lang]
    left, right = 50.0, PAGE_W - 50
    start, end, align_start = (right, left, "right") if rtl else (left, right, "left")
    align_end = "left" if rtl else "right"

    def row(y: float, label: str, value: str, size: float = 10, bold: bool = False) -> None:
        c.text(start, y, label, size, bold, align_start)
        c.text(end, y, value, size, bold, align_end)

    c.text(start, 70, inv.vendor_name or "", 16, True, align_start)
    c.text(start, 90, f"{labels['tax_id']} {inv.vendor_tax_id or ''}", 10, False, align_start)
    title = labels.get(inv.document_type, labels[DocumentType.TAX_INVOICE])
    assert isinstance(title, str)
    if s.render.language == "mixed":
        title = f"{title} / {LABELS['en'].get(inv.document_type)}"
    c.text(PAGE_W / 2, 130, title, 18, True, "center")
    row(160, f"{labels['number']}: {inv.invoice_number}", f"{labels['date']}: {_date(inv.issue_date)}")
    row(178, f"{labels['to']}: {s.our_company}", f"{labels['due']}: {_date(inv.due_date)}")

    y = 220.0
    cols = [start, PAGE_W * (0.45 if rtl else 0.55), PAGE_W * (0.30 if rtl else 0.70), end]
    aligns = [align_start, "center", "center", align_end]
    for x, text, a in zip(
        cols, (labels["item"], labels["qty"], labels["price"], labels["amount"]), aligns, strict=True
    ):
        c.text(x, y, str(text), 10, True, a)
    c.line(left, y + 6, right, y + 6)
    y += 24
    for li in inv.line_items:
        qty = "" if li.quantity is None else f"{li.quantity:g}"
        for x, text, a in zip(
            cols, (li.description, qty, _amount(li.unit_price), _amount(li.amount)), aligns, strict=True
        ):
            c.text(x, y, text, 10, False, a)
        y += 20
    c.line(left, y - 8, right, y - 8)
    y += 10
    rate = "" if inv.vat_rate is None else f" {(inv.vat_rate * 100).normalize():f}%"
    row(y, str(labels["subtotal"]), _amount(inv.subtotal))
    row(y + 18, f"{labels['vat']}{rate}", _amount(inv.vat_amount))
    row(y + 40, str(labels["total"]), _amount(inv.total), 13, True)
    y += 90
    if inv.bank is not None:
        names = BANKS if rtl else BANKS_EN
        bank_name = names.get(inv.bank.bank_code or "", inv.bank.bank_name or "")
        c.text(start, y, str(labels["bank"]), 10, True, align_start)
        c.text(
            start,
            y + 18,
            f"{bank_name} {inv.bank.bank_code}, {labels['branch']} {inv.bank.branch}, "
            f"{labels['account']} {inv.bank.account}",
            10,
            False,
            align_start,
        )
        y += 50
    if inv.notes:
        for i, chunk in enumerate(_wrap(inv.notes, 90)):
            c.text(start, y + i * 15, chunk, 9, False, align_start)


def _wrap(text: str, width: int) -> list[str]:
    words, lines, current = text.split(), [], ""
    for w in words:
        if len(current) + len(w) + 1 > width:
            lines.append(current)
            current = w
        else:
            current = f"{current} {w}".strip()
    return [*lines, current] if current else lines


def render(s: Sample) -> bytes:
    """PDF bytes, or JPEG bytes for scans (the pipeline normalises scans to PNG before upload)."""
    if s.render.format == "pdf":
        pc = PdfCanvas()
        draw(pc, s)
        return pc.finish()
    ic = ImageCanvas()
    draw(ic, s)
    return ic.finish(s.render, seed=zlib.crc32(s.id.encode()))
