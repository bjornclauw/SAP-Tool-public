"""Assemble Print Orders workcenter summary packets."""

from __future__ import annotations

import tempfile
from io import BytesIO
from pathlib import Path

from pypdf import PdfReader, PdfWriter

from tools.print_team_colors import team_color

MM = 72 / 25.4


def _group_orders(orders: list[dict]) -> list[tuple[str, list[dict]]]:
    groups: dict[str, list[dict]] = {}
    for order in orders:
        workcenter = str(order.get("workcenter", "")).strip()
        if not workcenter:
            raise ValueError(f"Order {order.get('order', '')} has no workcenter")
        groups.setdefault(workcenter, []).append(order)
    return list(groups.items())


def _tag_page(page, team: str, revision: str, color: str) -> None:
    from reportlab.lib.colors import HexColor, white
    from reportlab.pdfbase.pdfmetrics import stringWidth
    from reportlab.pdfgen import canvas

    width = float(page.mediabox.width)
    height = float(page.mediabox.height)
    margin = 4 * MM
    tag_w = 50 * MM
    tag_h = 8 * MM
    x = width - margin - tag_w
    y = height - margin - tag_h
    buffer = BytesIO()
    overlay = canvas.Canvas(buffer, pagesize=(width, height))
    overlay.setFillColor(HexColor(color))
    overlay.rect(x, y, tag_w, tag_h, stroke=0, fill=1)
    label = f"{team} · {revision}"
    size = 8
    while size > 5 and stringWidth(label, "Helvetica-Bold", size) > tag_w - 6 * MM:
        size -= 0.5
    overlay.setFillColor(white)
    overlay.setFont("Helvetica-Bold", size)
    overlay.drawString(x + 3 * MM, y + 2.6 * MM, label)
    overlay.showPage()
    overlay.save()
    buffer.seek(0)
    page.merge_page(PdfReader(buffer).pages[0])


def build_packet(
    orders: list[dict],
    output_path: str | Path,
    *,
    revision: str,
    date: str,
    color_overrides: dict | None = None,
    grouped: bool = True,
) -> dict:
    """Write the merged packet and return page count and team order."""
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    team_order: list[str] = []

    if grouped:
        try:
            __import__("reportlab")
            __import__("PIL")
        except ImportError as exc:
            raise RuntimeError(
                "Team summaries require ReportLab and Pillow. "
                "Install them with: python -m pip install -r requirements.txt"
            ) from exc
        from tools.print_summary import render_summary

        with tempfile.TemporaryDirectory() as tmp:
            for team_index, (team, team_orders) in enumerate(_group_orders(orders)):
                team_order.append(team)
                summary = Path(tmp) / f"summary-{team_index}.pdf"
                render_summary(
                    team_orders, summary, team=team, revision=revision, date=date,
                    color=team_color(team, color_overrides),
                )
                for page in PdfReader(summary).pages:
                    writer.add_page(page)
                if len(writer.pages) % 2:
                    writer.add_blank_page(width=595, height=842)
                color = team_color(team, color_overrides)
                for order in team_orders:
                    for page in PdfReader(order["pdf_path"]).pages:
                        _tag_page(page, team, revision, color)
                        writer.add_page(page)
                    if len(writer.pages) % 2:
                        writer.add_blank_page(width=595, height=842)
    else:
        for order in orders:
            for page in PdfReader(order["pdf_path"]).pages:
                writer.add_page(page)
            if len(writer.pages) % 2:
                writer.add_blank_page(width=595, height=842)

    with open(output, "wb") as handle:
        writer.write(handle)
    return {"page_count": len(writer.pages), "team_order": team_order}
