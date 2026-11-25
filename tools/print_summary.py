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

from core import i18n
from tools.print_team_colors import contrast_text

MM = 72 / 25.4
PAGE_W, PAGE_H = A4
BOXES_PER_PAGE = 5
ROW_HEIGHT_MM = 49
ROW_GAP_MM = 2
HEADER_HEIGHT_MM = 16
MARGIN_MM = 8
IMAGE_X_MM = 2.5
IMAGE_TOP_MM = 3
IMAGE_WIDTH_MM = 53
IMAGE_HEIGHT_MM = 42
INFO_X_MM = 58
TITLE_X_MM = 36
TABLE_TOP_MM = 13.5
TABLE_WIDTH_MM = 64
TABLE_GAP_MM = 6
TABLE_HEADER_MM = 5.0
TABLE_ROW_MM = 6.4
TABLE_ROWS = 4
FOOTER_TOP_MM = (
    MARGIN_MM + HEADER_HEIGHT_MM + ROW_GAP_MM
    + BOXES_PER_PAGE * ROW_HEIGHT_MM + (BOXES_PER_PAGE - 1) * ROW_GAP_MM + 8
)

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


def _text_right(c: canvas.Canvas, right: float, top: float, text: str,
                size: float, font: str | None = None, color=white) -> None:
    width = pdfmetrics.stringWidth(str(text), font or _FONT, size) / MM
    _text(c, right - width, top, text, size, font, color)


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
    foreground = HexColor(contrast_text(color))
    right = PAGE_W / MM - MARGIN_MM
    team_label = _fit_text(team, _FONT_BOLD, 19, _x(104))
    _text(c, MARGIN_MM + 2, 6, team_label, 19, _FONT_BOLD, foreground)
    revision_label = (
        i18n.t("print_summary.revision", "Revision") + f" {revision} · {date}"
        if revision else date
    )
    _text_right(c, right, 4.5, revision_label, 9, _FONT, foreground)
    _text_right(
        c, right, 9.8,
        i18n.t("print_summary.page", "Summary · Page {page} of {count}",
               page=page_no, count=page_count),
        11, _FONT_BOLD, foreground)


def _draw_placeholder(c: canvas.Canvas, x: float, top: float, width: float,
                      height: float, label: str, size: float,
                      bold: bool = False) -> None:
    font = _FONT_BOLD if bold else _FONT
    c.setFillColor(HexColor("#777777"))
    c.setFont(font, size)
    label_w = pdfmetrics.stringWidth(label, font, size)
    c.drawString(
        _x(x) + (_x(width) - label_w) / 2,
        _y(top + height / 2) - size * 0.35,
        label,
    )


def _draw_image(c: canvas.Canvas, path: str | None, x: float, top: float,
                width: float, height: float, blank: bool) -> None:
    c.setLineWidth(0.25 * MM)
    c.setStrokeColor(HexColor("#d0d0d0"))
    c.setFillColor(HexColor("#fafafa"))
    c.rect(_x(x), _y(top + height), _x(width), _x(height), stroke=1, fill=1)
    if blank:
        _draw_placeholder(c, x, top, width, height, "BLANCO", 12, bold=True)
        return
    if not path:
        _draw_placeholder(
            c, x, top, width, height,
            i18n.t("print_summary.no_image", "No image"), 8)
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
        _draw_placeholder(
            c, x, top, width, height,
            i18n.t("print_summary.no_image", "No image"), 8)


def _draw_capture_table(c: canvas.Canvas, x: float, top: float) -> None:
    widths = (36, 14, 14)
    total_w = sum(widths)
    total_h = TABLE_HEADER_MM + TABLE_ROW_MM * TABLE_ROWS
    c.setFillColor(HexColor("#f3f3f3"))
    c.rect(_x(x), _y(top + TABLE_HEADER_MM), _x(total_w), _x(TABLE_HEADER_MM),
           stroke=0, fill=1)
    c.setStrokeColor(HexColor("#cfcfcf"))
    c.setLineWidth(0.2 * MM)
    c.rect(_x(x), _y(top + total_h), _x(total_w), _x(total_h), stroke=1, fill=0)
    cursor = x
    for width in widths[:-1]:
        cursor += width
        c.line(_x(cursor), _y(top), _x(cursor), _y(top + total_h))
    for index in range(TABLE_ROWS + 1):
        line_top = top + TABLE_HEADER_MM + index * TABLE_ROW_MM
        c.line(_x(x), _y(line_top), _x(x + total_w), _y(line_top))
    labels = (
        i18n.t("print_summary.technician", "Technician"),
        i18n.t("print_summary.start", "Start"),
        i18n.t("print_summary.stop", "Stop"),
    )
    cursor = x
    for label, width in zip(labels, widths):
        _text(c, cursor + 1.2, top + 1.0, label, 7, _FONT_BOLD,
              HexColor("#555555"))
        cursor += width


def _draw_box(c: canvas.Canvas, box: dict, row_index: int) -> None:
    x = MARGIN_MM
    top = (MARGIN_MM + HEADER_HEIGHT_MM + ROW_GAP_MM
           + row_index * (ROW_HEIGHT_MM + ROW_GAP_MM))
    width = PAGE_W / MM - 2 * MARGIN_MM
    c.setStrokeColor(HexColor("#b8b8b8"))
    c.setLineWidth(0.3 * MM)
    c.setFillColor(white)
    c.rect(_x(x), _y(top + ROW_HEIGHT_MM), _x(width), _x(ROW_HEIGHT_MM),
           stroke=1, fill=1)
    blank = bool(box.get("blank"))
    _draw_image(c, box.get("cover_image"), x + IMAGE_X_MM, top + IMAGE_TOP_MM,
                IMAGE_WIDTH_MM, IMAGE_HEIGHT_MM, blank)

    info_x = x + INFO_X_MM
    if blank:
        _text(c, info_x, top + 2, i18n.t("print_summary.order_no", "ORDER NO."),
              7.5, _FONT_BOLD, HexColor("#555555"))
        _text(c, info_x + 40, top + 2,
              i18n.t("print_summary.title", "TITLE / DESCRIPTION"),
              7.5, _FONT_BOLD, HexColor("#555555"))
        c.setStrokeColor(HexColor("#888888"))
        c.setLineWidth(0.2 * MM)
        c.line(_x(info_x + 20), _y(top + 9.5), _x(info_x + 36), _y(top + 9.5))
        c.line(_x(info_x + 76), _y(top + 9.5), _x(info_x + 134),
               _y(top + 9.5))
    else:
        _text(c, info_x, top + 1, str(box.get("order", "")), 14, _FONT_BOLD,
              HexColor("#1a1a1a"))
        title = _fit_text(str(box.get("title", "")), _FONT_BOLD, 9.5, _x(98))
        _text(c, info_x + TITLE_X_MM, top + 2.5, title, 9.5, _FONT_BOLD,
              HexColor("#1a1a1a"))

    _draw_capture_table(c, info_x, top + TABLE_TOP_MM)
    _draw_capture_table(
        c, info_x + TABLE_WIDTH_MM + TABLE_GAP_MM, top + TABLE_TOP_MM)


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
        _draw_header(c, team, revision, date, page_index + 1, page_count,
                     team_color)
        page_boxes = boxes[page_index * BOXES_PER_PAGE:(page_index + 1) * BOXES_PER_PAGE]
        for row_index, box in enumerate(page_boxes):
            _draw_box(c, box, row_index)
        footer = f"{team} · {revision}" if revision else team
        _text(c, MARGIN_MM, FOOTER_TOP_MM, footer, 7, _FONT,
              HexColor("#777777"))
        _text_right(
            c, PAGE_W / MM - MARGIN_MM, FOOTER_TOP_MM,
            i18n.t("print_summary.footer_page", "Summary {page} / {count}",
                   page=page_index + 1, count=page_count),
            7, _FONT, HexColor("#777777"))
        c.showPage()
    c.save()
    return page_count
