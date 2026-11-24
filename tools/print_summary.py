"""Render the signed-off Print Orders workcenter summary pages."""

from __future__ import annotations

import math
import os
from pathlib import Path

from reportlab.lib.colors import HexColor, white
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

MM = 72 / 25.4
PAGE_W, PAGE_H = A4
BOXES_PER_PAGE = 5
ROW_HEIGHT_MM = 47
ROW_GAP_MM = 2
HEADER_HEIGHT_MM = 16
MARGIN_MM = 10

_FONT = "Helvetica"
_FONT_BOLD = "Helvetica-Bold"
_font_dir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
if (_font_dir / "segoeui.ttf").exists() and (_font_dir / "segoeib.ttf").exists():
    pdfmetrics.registerFont(TTFont("PrintSummary", str(_font_dir / "segoeui.ttf")))
    pdfmetrics.registerFont(TTFont("PrintSummaryBold", str(_font_dir / "segoeib.ttf")))
    _FONT = "PrintSummary"
    _FONT_BOLD = "PrintSummaryBold"


def _x(value: float) -> float:
    return value * MM


def _y(top: float) -> float:
    return PAGE_H - top * MM


def _text(c: canvas.Canvas, x: float, top: float, text: str, size: float,
          font: str | None = None, color=white) -> None:
    c.setFont(font or _FONT, size)
    c.setFillColor(color)
    c.drawString(_x(x), _y(top) - size, str(text))


def _fit_text(text: str, font: str, size: float, max_width: float) -> str:
    value = str(text or "")
    if pdfmetrics.stringWidth(value, font, size) <= max_width:
        return value
    suffix = "…"
    while value and pdfmetrics.stringWidth(value + suffix, font, size) > max_width:
        value = value[:-1]
    return value.rstrip() + suffix


def _draw_header(c: canvas.Canvas, team: str, revision: str, date: str,
                 page_no: int, page_count: int, color) -> None:
    c.setFillColor(color)
    c.rect(0, PAGE_H - _x(HEADER_HEIGHT_MM), PAGE_W, _x(HEADER_HEIGHT_MM),
           stroke=0, fill=1)
    _text(c, 14, 6, team, 19, _FONT_BOLD)
    _text(c, 128, 5, f"Revision {revision} · {date}", 8, _FONT)
    _text(c, 150, 10, f"Summary · Page {page_no} of {page_count}", 10, _FONT_BOLD)


def _draw_image(c: canvas.Canvas, path: str | None, x: float, top: float,
                width: float, height: float, blank: bool) -> None:
    c.setLineWidth(0.25 * MM)
    c.setStrokeColor(HexColor("#d0d0d0"))
    c.setFillColor(HexColor("#fafafa"))
    c.rect(_x(x), _y(top + height), _x(width), _x(height), stroke=1, fill=1)
    if blank:
        c.setFillColor(HexColor("#777777"))
        c.setFont(_FONT_BOLD, 11)
        label = "BLANCO"
        label_w = pdfmetrics.stringWidth(label, _FONT_BOLD, 11)
        c.drawString(_x(x) + (_x(width) - label_w) / 2,
                     _y(top + height / 2) - 4, label)
        return
    if not path:
        c.setFillColor(HexColor("#999999"))
        c.setFont(_FONT, 7)
        label = "No image"
        label_w = pdfmetrics.stringWidth(label, _FONT, 7)
        c.drawString(_x(x) + (_x(width) - label_w) / 2,
                     _y(top + height / 2) - 3, label)
        return
    try:
        reader = ImageReader(str(path))
        image_w, image_h = reader.getSize()
        scale = min(_x(width) / image_w, _x(height) / image_h)
        draw_w, draw_h = image_w * scale, image_h * scale
        draw_x = _x(x) + (_x(width) - draw_w) / 2
        draw_y = _y(top + height) + (_x(height) - draw_h) / 2
        c.drawImage(reader, draw_x, draw_y, draw_w, draw_h, mask="auto")
    except Exception:
        pass


def _draw_capture_table(c: canvas.Canvas, x: float, top: float) -> None:
    widths = [38, 14, 14, 38, 14, 14]
    header_h = 4
    row_h = 5.2
    total_w = sum(widths)
    total_h = header_h + row_h * 4
    c.setFillColor(HexColor("#f3f3f3"))
    c.rect(_x(x), _y(top + header_h), _x(total_w), _x(header_h),
           stroke=0, fill=1)
    c.setStrokeColor(HexColor("#cfcfcf"))
    c.setLineWidth(0.2 * MM)
    c.rect(_x(x), _y(top + total_h), _x(total_w), _x(total_h), stroke=1, fill=0)
    cursor = x
    for width in widths[:-1]:
        cursor += width
        c.line(_x(cursor), _y(top), _x(cursor), _y(top + total_h))
    for index in range(5):
        line_top = top + header_h + index * row_h
        c.line(_x(x), _y(line_top), _x(x + total_w), _y(line_top))
    labels = ["Technician", "Start", "Stop", "Technician", "Start", "Stop"]
    cursor = x
    for label, width in zip(labels, widths):
        _text(c, cursor + 1, top + 0.8, label, 5.8, _FONT_BOLD, HexColor("#555555"))
        cursor += width


def _draw_box(c: canvas.Canvas, box: dict, row_index: int, color) -> None:
    x = MARGIN_MM
    top = MARGIN_MM + HEADER_HEIGHT_MM + ROW_GAP_MM + row_index * (ROW_HEIGHT_MM + ROW_GAP_MM)
    width = PAGE_W / MM - 2 * MARGIN_MM
    c.setStrokeColor(HexColor("#b8b8b8"))
    c.setLineWidth(0.3 * MM)
    c.setFillColor(white)
    c.rect(_x(x), _y(top + ROW_HEIGHT_MM), _x(width), _x(ROW_HEIGHT_MM),
           stroke=1, fill=1)
    blank = bool(box.get("blank"))
    _draw_image(c, box.get("cover_image"), x + 2, top + 2, 46, 36, blank)

    info_x = x + 56
    if blank:
        _text(c, info_x, top + 2, "ORDER NO.", 6.5, _FONT_BOLD, HexColor("#555555"))
        _text(c, info_x + 38, top + 2, "TITLE / DESCRIPTION", 6.5,
              _FONT_BOLD, HexColor("#555555"))
        c.setStrokeColor(HexColor("#888888"))
        c.setLineWidth(0.2 * MM)
        c.line(_x(info_x + 20), _y(top + 8), _x(info_x + 34), _y(top + 8))
        c.line(_x(info_x + 66), _y(top + 8), _x(info_x + 130), _y(top + 8))
    else:
        _text(c, info_x, top + 1, str(box.get("order", "")), 13, _FONT_BOLD,
              HexColor("#1a1a1a"))
        title = _fit_text(str(box.get("title", "")), _FONT_BOLD, 8.7, _x(98))
        _text(c, info_x + 30, top + 2.5, title, 8.7, _FONT_BOLD,
              HexColor("#1a1a1a"))

    _draw_capture_table(c, info_x, top + 12)
    _text(c, info_x, top + 39.5, "Notes:", 5.8, _FONT, HexColor("#777777"))
    c.setStrokeColor(HexColor("#d8d8d8"))
    c.setLineWidth(0.2 * MM)
    c.line(_x(info_x + 10), _y(top + 43), _x(info_x + 132), _y(top + 43))


def render_summary(
    orders: list[dict],
    output_path: str | Path,
    *,
    team: str,
    revision: str,
    date: str,
    color: str,
    blank_count: int = 4,
) -> int:
    """Write the summary PDF and return its page count."""
    blank_count = max(0, int(blank_count))
    boxes = list(orders) + [{"blank": True} for _ in range(blank_count)]
    page_count = max(1, math.ceil(len(boxes) / BOXES_PER_PAGE))
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        team_color = HexColor(color)
    except Exception:
        team_color = HexColor("#546e7a")

    c = canvas.Canvas(str(output), pagesize=A4, pageCompression=1)
    for page_index in range(page_count):
        _draw_header(c, team, revision, date, page_index + 1, page_count, team_color)
        page_boxes = boxes[page_index * BOXES_PER_PAGE:(page_index + 1) * BOXES_PER_PAGE]
        for row_index, box in enumerate(page_boxes):
            _draw_box(c, box, row_index, team_color)
        _text(c, 10, 289, f"{team} · {revision}", 6, _FONT, HexColor("#777777"))
        page_label = f"Summary {page_index + 1} / {page_count}"
        label_w = pdfmetrics.stringWidth(page_label, _FONT, 6)
        _text(c, PAGE_W / MM - 10 - label_w / MM, 289, page_label, 6,
              _FONT, HexColor("#777777"))
        c.showPage()
    c.save()
    return page_count
