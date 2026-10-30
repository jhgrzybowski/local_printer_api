"""Layout plans were checked against CUPS pdftopdf output for the same options."""
from __future__ import annotations

import pytest

from app.services.layout import PageBox, Paper, plan_layout


A4 = Paper("A4", "A4", 595, 842)
PORTRAIT = PageBox(1, 595, 842)
LANDSCAPE = PageBox(2, 842, 595)
LETTER = PageBox(4, 612, 792)


def cells(plan: dict, side: int = 0) -> list[dict]:
    return plan["sides"][side]["cells"]  # type: ignore[index]


def test_single_page_fills_matching_paper() -> None:
    plan = plan_layout([PORTRAIT], A4)

    side = plan["sides"][0]  # type: ignore[index]
    assert (side["width_pt"], side["height_pt"]) == (595, 842)
    assert cells(plan) == [{"page": 1, "x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0, "rotate": 0}]
    assert plan["total_sheets"] == 1


def test_landscape_page_turns_the_sheet_when_orientation_is_automatic() -> None:
    side = plan_layout([LANDSCAPE], A4)["sides"][0]  # type: ignore[index]

    assert (side["width_pt"], side["height_pt"]) == (842, 595)
    assert side["cells"][0]["width"] == 1.0


def test_auto_scaling_fits_a_larger_page_and_keeps_its_shape() -> None:
    cell = cells(plan_layout([LETTER], A4))[0]

    assert cell["width"] == 1.0
    assert cell["height"] == pytest.approx(792 * 595 / 612 / 842, abs=1e-3)
    assert cell["y"] == pytest.approx((1 - cell["height"]) / 2, abs=1e-3)


@pytest.mark.parametrize(
    ("scaling", "width"),
    [("fill", 842 / 792 * 612 / 595), ("none", 612 / 595), ("fit", 1.0)],
)
def test_scaling_modes(scaling: str, width: float) -> None:
    cell = cells(plan_layout([LETTER], A4, scaling=scaling))[0]

    assert cell["width"] == pytest.approx(width, abs=1e-3)


def test_custom_scale_is_centered() -> None:
    cell = cells(plan_layout([PORTRAIT], A4, scale_percent=50))[0]

    assert (cell["x"], cell["y"], cell["width"], cell["height"]) == (0.25, 0.25, 0.5, 0.5)
    assert plan_layout([PORTRAIT], A4, scale_percent=50)["scaling"] == "custom"


def test_two_up_uses_a_landscape_sheet_with_pages_side_by_side() -> None:
    plan = plan_layout([PORTRAIT, LANDSCAPE, PageBox(3, 595, 842)], A4, pages_per_sheet=2)

    side = plan["sides"][0]  # type: ignore[index]
    assert (side["width_pt"], side["height_pt"]) == (842, 595)
    first, second = side["cells"]
    assert first["page"] == 1 and first["x"] < second["x"]
    # CUPS turns a landscape page to suit a portrait cell.
    assert second["rotate"] == 90
    assert plan["impressions_per_copy"] == 2


def test_explicit_orientation_stops_cell_rotation() -> None:
    plan = plan_layout([PORTRAIT, LANDSCAPE], A4, pages_per_sheet=2, orientation="landscape")

    side = plan["sides"][0]  # type: ignore[index]
    assert (side["width_pt"], side["height_pt"]) == (595, 842)
    first, second = side["cells"]
    assert first["y"] < second["y"]
    assert first["rotate"] == second["rotate"] == 0
    assert second["width"] == pytest.approx(1.0, abs=1e-3)
    # The portrait page stays upright, so it fills only half the cell width.
    assert first["width"] == pytest.approx(0.5, abs=1e-3)


@pytest.mark.parametrize(
    ("layout", "order"),
    [("lrtb", [(0, 0), (1, 0), (0, 1), (1, 1)]), ("tblr", [(0, 0), (0, 1), (1, 0), (1, 1)]),
     ("rltb", [(1, 0), (0, 0), (1, 1), (0, 1)]), ("btlr", [(0, 1), (0, 0), (1, 1), (1, 0)])],
)
def test_four_up_layout_order(layout: str, order: list[tuple[int, int]]) -> None:
    pages = [PageBox(number, 595, 842) for number in range(1, 5)]
    plan = plan_layout(pages, A4, pages_per_sheet=4, pages_per_sheet_layout=layout)

    positions = [(round(cell["x"] * 2), round(cell["y"] * 2)) for cell in cells(plan)]
    assert positions == order


def test_six_up_grid_is_three_by_two_on_a_landscape_sheet() -> None:
    pages = [PageBox(number, 595, 842) for number in range(1, 7)]
    side = plan_layout(pages, A4, pages_per_sheet=6)["sides"][0]  # type: ignore[index]

    assert side["width_pt"] > side["height_pt"]
    assert sorted({round(cell["y"], 2) for cell in side["cells"]}) == [0.0, 0.5]
    assert len({round(cell["x"], 2) for cell in side["cells"]}) == 3


def test_duplex_pairs_sides_and_counts_sheets_per_copy() -> None:
    pages = [PageBox(number, 595, 842) for number in range(1, 6)]
    plan = plan_layout(pages, A4, duplex="long-edge", copies=3)

    assert [(side["sheet"], side["face"]) for side in plan["sides"]] == [  # type: ignore[union-attr]
        (1, "front"), (1, "back"), (2, "front"), (2, "back"), (3, "front"),
    ]
    assert plan["sheets_per_copy"] == 3
    assert plan["total_sheets"] == 9


def test_margins_shrink_the_fit_area_unless_borderless() -> None:
    paper = Paper("A4", "A4", 595, 842, margins=(18, 36, 18, 36))
    framed = cells(plan_layout([PORTRAIT], paper, scaling="fit"))[0]
    borderless = cells(plan_layout([PORTRAIT], paper, scaling="fit", borderless=True))[0]

    assert framed["height"] == pytest.approx(770 / 842, abs=1e-3)
    assert borderless["height"] == 1.0
