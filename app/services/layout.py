"""Plan how selected pages land on physical sheets, like a print dialog preview.

The plan mirrors what CUPS (pdftopdf) does with number-up, print-scaling, and
orientation, so a client can draw the preview from cached page images and
redraw it instantly when a setting changes. It is an approximation: the
printer driver can still shift content by its hardware margins.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


# Grid (long side, short side) per pages-per-sheet value.
NUP_GRID = {1: (1, 1), 2: (2, 1), 4: (2, 2), 6: (3, 2), 9: (3, 3), 16: (4, 4)}
# CUPS images without a resolution are placed at this density.
DEFAULT_IMAGE_PPI = 128


@dataclass(frozen=True)
class PageBox:
    number: int
    width: float
    height: float


@dataclass(frozen=True)
class Paper:
    keyword: str
    label: str
    width: float
    height: float
    # Unprintable margins in portrait orientation: left, bottom, right, top.
    margins: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)


def plan_layout(
    pages: list[PageBox],
    paper: Paper,
    *,
    orientation: str | None = None,
    pages_per_sheet: int = 1,
    pages_per_sheet_layout: str | None = None,
    page_border: str | None = None,
    scaling: str | None = None,
    scale_percent: int | None = None,
    two_sided: bool = False,
    copies: int = 1,
    borderless: bool = False,
    reverse_order: bool = False,
) -> dict[str, object]:
    pages_per_sheet = pages_per_sheet if pages_per_sheet in NUP_GRID else 1
    mode = "custom" if scale_percent is not None else (scaling or "auto")
    impressions = math.ceil(len(pages) / pages_per_sheet) if pages else 0
    sheets_per_copy = math.ceil(impressions / 2) if two_sided else impressions

    sides = []
    for index in range(impressions):
        group = pages[index * pages_per_sheet:(index + 1) * pages_per_sheet]
        if pages_per_sheet == 1:
            side = _single_page_side(group[0], paper, orientation, mode, scale_percent, borderless)
        else:
            side = _nup_side(group, paper, orientation, pages_per_sheet, pages_per_sheet_layout, borderless)
        side["sheet"] = index // 2 + 1 if two_sided else index + 1
        side["face"] = "back" if two_sided and index % 2 else "front"
        sides.append(side)

    return {
        "paper": {"keyword": paper.keyword, "label": paper.label,
                  "width_pt": round(paper.width, 2), "height_pt": round(paper.height, 2)},
        "pages_per_sheet": pages_per_sheet,
        "pages_per_sheet_layout": (pages_per_sheet_layout or "lrtb") if pages_per_sheet > 1 else None,
        "page_border": page_border if pages_per_sheet > 1 and page_border not in (None, "none") else None,
        "scaling": mode,
        "scale_percent": scale_percent,
        "selected_page_count": len(pages),
        "impressions_per_copy": impressions,
        "sheets_per_copy": sheets_per_copy,
        "copies": copies,
        # Each two-sided copy starts on a fresh sheet, as CUPS prints it.
        "total_sheets": sheets_per_copy * copies,
        # Sides stay in reading order for the preview; with reverse delivery
        # the printer outputs the last sheet first.
        "reverse_order": reverse_order,
        "sides": sides,
    }


def _frame(paper: Paper, landscape: bool, borderless: bool) -> tuple[float, float, tuple[float, float, float, float]]:
    """Return width, height and (left, top, right, bottom) margins as viewed."""
    left, bottom, right, top = (0.0, 0.0, 0.0, 0.0) if borderless else paper.margins
    if landscape:
        # Turning a portrait sheet a quarter turn moves its right edge to the top.
        return paper.height, paper.width, (top, right, bottom, left)
    return paper.width, paper.height, (left, top, right, bottom)


def _single_page_side(
    page: PageBox, paper: Paper, orientation: str | None, mode: str,
    scale_percent: int | None, borderless: bool,
) -> dict[str, object]:
    if orientation in {"landscape", "reverse-landscape"}:
        landscape = True
    elif orientation in {"portrait", "reverse-portrait"}:
        landscape = False
    else:
        # CUPS turns a landscape page to match the paper automatically.
        landscape = page.width > page.height
    width, height, (left, top, right, bottom) = _frame(paper, landscape, borderless)
    area_width, area_height = width - left - right, height - top - bottom
    fit = min(area_width / page.width, area_height / page.height)
    if mode == "custom":
        scale = (scale_percent or 100) / 100
    elif mode == "fit":
        scale = fit
    elif mode == "auto-fit":
        scale = min(1.0, fit)
    elif mode == "fill":
        scale = max(width / page.width, height / page.height)
    elif mode == "none":
        scale = 1.0
    else:
        # auto: keep a page that fits the sheet at its size, otherwise fit it.
        fits = page.width <= width + 1 and page.height <= height + 1
        scale = 1.0 if fits else fit
    placed_width, placed_height = page.width * scale, page.height * scale
    if mode in {"fit", "auto-fit"} or (mode == "auto" and scale != 1.0):
        x = left + (area_width - placed_width) / 2
        y = top + (area_height - placed_height) / 2
    else:
        x = (width - placed_width) / 2
        y = (height - placed_height) / 2
    return _side(width, height, (left, top, right, bottom), [
        _cell(page.number, x, y, placed_width, placed_height, 0, width, height),
    ])


def _nup_side(
    pages: list[PageBox], paper: Paper, orientation: str | None, count: int,
    layout: str | None, borderless: bool,
) -> dict[str, object]:
    long_count, short_count = NUP_GRID[count]
    landscape = orientation in {"landscape", "reverse-landscape"}
    if long_count != short_count:
        # 2-up and 6-up turn the sheet so each cell keeps the page's shape.
        landscape = not landscape
    width, height, (left, top, right, bottom) = _frame(paper, landscape, borderless)
    columns, rows = (long_count, short_count) if width >= height else (short_count, long_count)
    area_width, area_height = width - left - right, height - top - bottom
    cell_width, cell_height = area_width / columns, area_height / rows
    cells = []
    for index, page in enumerate(pages):
        column, row = _grid_position(index, columns, rows, layout or "lrtb")
        # CUPS turns a page to suit its cell only when no orientation is set.
        turn = orientation is None and page.width != page.height
        rotate = 90 if turn and (page.width > page.height) != (cell_width > cell_height) else 0
        page_width, page_height = (page.height, page.width) if rotate else (page.width, page.height)
        scale = min(cell_width / page_width, cell_height / page_height)
        placed_width, placed_height = page_width * scale, page_height * scale
        x = left + column * cell_width + (cell_width - placed_width) / 2
        y = top + row * cell_height + (cell_height - placed_height) / 2
        cells.append(_cell(page.number, x, y, placed_width, placed_height, rotate, width, height))
    return _side(width, height, (left, top, right, bottom), cells)


def _grid_position(index: int, columns: int, rows: int, layout: str) -> tuple[int, int]:
    primary, secondary = layout[:2], layout[2:]
    if primary in {"lr", "rl"}:
        column, row = index % columns, index // columns
    else:
        row, column = index % rows, index // rows
    if "rl" in (primary, secondary):
        column = columns - 1 - column
    if "bt" in (primary, secondary):
        row = rows - 1 - row
    return column, row


def _cell(number: int, x: float, y: float, width: float, height: float, rotate: int,
          frame_width: float, frame_height: float) -> dict[str, object]:
    # Fractions of the sheet, origin at its top-left corner. Values outside
    # 0..1 mean the page is cropped at the sheet edge (fill or actual size).
    return {
        "page": number,
        "x": round(x / frame_width, 4),
        "y": round(y / frame_height, 4),
        "width": round(width / frame_width, 4),
        "height": round(height / frame_height, 4),
        "rotate": rotate,
    }


def _side(width: float, height: float, margins: tuple[float, float, float, float],
          cells: list[dict[str, object]]) -> dict[str, object]:
    left, top, right, bottom = margins
    return {
        "width_pt": round(width, 2),
        "height_pt": round(height, 2),
        "printable": {
            "x": round(left / width, 4), "y": round(top / height, 4),
            "width": round((width - left - right) / width, 4),
            "height": round((height - top - bottom) / height, 4),
        },
        "cells": cells,
    }
