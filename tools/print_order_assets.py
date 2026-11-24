"""Retained Print Orders cover images for summary rendering."""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path


def _safe_order(order: object) -> str:
    value = re.sub(r"[^A-Za-z0-9_-]+", "_", str(order or "")).strip("_")
    return value or "order"


def retain_cover_image(
    order: object,
    source_paths: Iterable[str | Path],
    destination_dir: str | Path,
) -> str | None:
    """Save the first decodable image as a stable PNG and return its path."""
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return None
    destination = Path(destination_dir)
    for source in source_paths:
        try:
            with Image.open(source) as opened:
                image = ImageOps.exif_transpose(opened).convert("RGB")
        except (OSError, ValueError):
            continue
        destination.mkdir(parents=True, exist_ok=True)
        output = destination / f"{_safe_order(order)}_cover.png"
        image.save(output, "PNG")
        return str(output)
    return None
