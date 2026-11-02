from pathlib import Path

import pytest

from app.models.print_options import PrintOptions
from app.services.lpoptions_parser import parse_lpoptions, parse_ppd_options
from app.services.options_summary import build_options_summary


GUTENPRINT_CAPABILITIES = {
    "PageSize": {"Letter", "A4", "A5"},
    "ColorModel": {"Gray", "Black", "RGB", "CMY", "CMYK", "KCMY"},
    "MediaType": {"Plain", "PhotoPlusGloss2", "GlossyPaper", "PhotopaperMatte"},
    "StpQuality": {"None", "Standard"},
    "Resolution": {"601x600dpi", "600dpi", "300dpi", "612x600dpi"},
    "Duplex": {"None", "DuplexNoTumble", "DuplexTumble"},
    "StpiShrinkOutput": {"Shrink", "Crop", "Expand"},
    "StpOrientation": {"Portrait", "Landscape", "UpsideDown", "Seascape"},
}


def test_parse_lpoptions() -> None:
    capabilities = parse_lpoptions(
        "PageSize/Media Size: *A4 Letter\n"
        "ColorModel/Color Model: *Gray RGB\n"
        "Duplex/2-Sided Printing: *None DuplexNoTumble DuplexTumble\n"
    )

    assert capabilities["PageSize"] == {"A4", "Letter"}
    assert capabilities["ColorModel"] == {"Gray", "RGB"}
    assert capabilities["Duplex"] == {"None", "DuplexNoTumble", "DuplexTumble"}


def test_parse_ppd_options() -> None:
    capabilities = parse_ppd_options(
        "*OpenUI *PageSize/Media Size: PickOne\n"
        "*DefaultPageSize: A4\n"
        "*PageSize A4/A4: \"<</PageSize[595 842]>>setpagedevice\"\n"
        "*PageSize Letter/Letter: \"<</PageSize[612 792]>>setpagedevice\"\n"
        "*CloseUI: *PageSize\n"
        "*OpenUI *ColorModel/Color Model: PickOne\n"
        "*ColorModel Gray/Grayscale: \"\"\n"
        "*ColorModel RGB/Color: \"\"\n"
        "*CloseUI: *ColorModel\n"
    )

    assert capabilities["PageSize"] == {"A4", "Letter"}
    assert capabilities["ColorModel"] == {"Gray", "RGB"}


def test_print_options_map_conservative_values() -> None:
    options = PrintOptions(
        copies=2,
        paper_size="A4",
        duplex="long-edge",
        orientation="portrait",
        color_mode="monochrome",
        quality="high",
    )

    mapped = options.to_cups_options(
        {
            "PageSize": {"A4", "Letter"},
            "Duplex": {"None", "DuplexNoTumble", "DuplexTumble"},
            "ColorModel": {"Gray", "RGB"},
            "Quality": {"Draft", "Normal", "High"},
        }
    )

    assert mapped.applied_options["copies"] == "2"
    assert mapped.applied_options["PageSize"] == "A4"
    assert mapped.applied_options["Duplex"] == "DuplexNoTumble"
    assert mapped.applied_options["ColorModel"] == "Gray"
    assert mapped.applied_options["Quality"] == "High"
    assert mapped.unsupported_options == []


def test_known_successful_gutenprint_mapping() -> None:
    options = PrintOptions(
        copies=1,
        pages="1",
        paper_size="A4",
        orientation="portrait",
        color_mode="monochrome",
        duplex="none",
        quality="normal",
        collate=True,
        media_type="plain",
        fit_to_page=True,
    )

    mapped = options.to_cups_options(GUTENPRINT_CAPABILITIES)

    assert mapped.applied_options == {
        "copies": "1",
        "Duplex": "None",
        "StpOrientation": "Portrait",
        "PageSize": "A4",
        "ColorModel": "Gray",
        "Resolution": "600dpi",
        "MediaType": "Plain",
        "StpiShrinkOutput": "Shrink",
    }
    assert mapped.unsupported_options == []
    assert "Ignored collate because copies=1" in mapped.warnings


def test_print_options_drop_unsupported_values() -> None:
    mapped = PrintOptions(paper_size="Letter", color_mode="color").to_cups_options(
        {"PageSize": {"A4"}, "ColorModel": {"Gray"}}
    )

    assert "paper_size" in mapped.unsupported_options
    assert "color_mode" in mapped.unsupported_options
    assert mapped.applied_options == {"copies": "1"}


def test_collate_warning_for_multiple_copies_when_unsupported() -> None:
    mapped = PrintOptions(copies=2, collate=True).to_cups_options(GUTENPRINT_CAPABILITIES)

    assert "collate" in mapped.unsupported_options
    assert "Dropped collate because no compatible CUPS option was detected" in mapped.warnings


def test_color_mapping_color_and_monochrome() -> None:
    mono = PrintOptions(color_mode="monochrome").to_cups_options(GUTENPRINT_CAPABILITIES)
    color = PrintOptions(color_mode="color").to_cups_options(GUTENPRINT_CAPABILITIES)

    assert mono.applied_options["ColorModel"] == "Gray"
    assert color.applied_options["ColorModel"] == "RGB"


def test_duplex_mapping_all_modes() -> None:
    assert PrintOptions(duplex="none").to_cups_options(GUTENPRINT_CAPABILITIES).applied_options["Duplex"] == "None"
    assert PrintOptions(duplex="long-edge").to_cups_options(GUTENPRINT_CAPABILITIES).applied_options["Duplex"] == "DuplexNoTumble"
    assert PrintOptions(duplex="short-edge").to_cups_options(GUTENPRINT_CAPABILITIES).applied_options["Duplex"] == "DuplexTumble"


def test_quality_maps_to_detected_resolution() -> None:
    assert PrintOptions(quality="draft").to_cups_options(GUTENPRINT_CAPABILITIES).applied_options["Resolution"] == "300dpi"
    assert PrintOptions(quality="normal").to_cups_options(GUTENPRINT_CAPABILITIES).applied_options["Resolution"] == "600dpi"
    # Gutenprint's 612x600dpi is its T-shirt transfer mode, not a higher
    # resolution. Without a real one, high quality is not offered.
    high = PrintOptions(quality="high").to_cups_options(GUTENPRINT_CAPABILITIES)
    assert "Resolution" not in high.applied_options
    assert "quality" in high.unsupported_options


def test_high_quality_uses_a_real_higher_resolution() -> None:
    capabilities = {"Resolution": {"600dpi", "612x600dpi", "1200x600dpi"}}

    mapped = PrintOptions(quality="high").to_cups_options(capabilities)

    assert mapped.applied_options["Resolution"] == "1200x600dpi"


def test_queue_quality_presets_win_over_gutenprint_resolution_modes() -> None:
    capabilities = {**GUTENPRINT_CAPABILITIES, "print-quality": {"4", "5"}}

    assert PrintOptions(quality="normal").to_cups_options(capabilities).applied_options["print-quality"] == "4"
    assert PrintOptions(quality="high").to_cups_options(capabilities).applied_options["print-quality"] == "5"
    assert PrintOptions(quality="draft").to_cups_options(capabilities).applied_options["Resolution"] == "300dpi"


def test_normal_resolution_is_not_reused_for_high_quality() -> None:
    for resolutions in ({"601x600dpi"}, {"600dpi", "601x600dpi"}):
        capabilities = {"Resolution": resolutions}
        normal = PrintOptions(quality="normal").to_cups_options(capabilities)
        high = PrintOptions(quality="high").to_cups_options(capabilities)
        summary = build_options_summary("Canon_MG5350", capabilities)["quality"]

        assert "Resolution" in normal.applied_options
        assert "quality" in high.unsupported_options
        assert summary["recommended_mapping"]["high"] is None


def test_media_type_aliases_and_drop_behavior() -> None:
    plain = PrintOptions(media_type="plain").to_cups_options(GUTENPRINT_CAPABILITIES)
    glossy = PrintOptions(media_type="glossy").to_cups_options(GUTENPRINT_CAPABILITIES)
    unsupported = PrintOptions(media_type="canvas").to_cups_options(GUTENPRINT_CAPABILITIES)

    assert plain.applied_options["MediaType"] == "Plain"
    assert glossy.applied_options["MediaType"] == "GlossyPaper"
    assert "media_type" in unsupported.unsupported_options


def test_landscape_orientation_prefers_detected_gutenprint_option() -> None:
    mapped = PrintOptions(orientation="landscape").to_cups_options(GUTENPRINT_CAPABILITIES)

    assert mapped.applied_options["StpOrientation"] == "Landscape"


def test_standard_orientation_fallback() -> None:
    mapped = PrintOptions(orientation="landscape").to_cups_options({"PageSize": {"A4"}})

    assert mapped.applied_options["orientation-requested"] == "4"


def test_options_summary_frontend_shape() -> None:
    summary = build_options_summary("Canon_MG5350", GUTENPRINT_CAPABILITIES)

    assert summary["queue"] == "Canon_MG5350"
    assert summary["paper_sizes"]["raw_option"] == "PageSize"
    assert summary["duplex_modes"]["mapping"]["long-edge"] == "DuplexNoTumble"
    assert summary["color_modes"]["mapping"]["monochrome"] == "Gray"
    assert summary["quality"]["raw_option"] == "Resolution"
    assert summary["media_types"]["mapping"]["plain"] == "Plain"
    assert summary["fit_to_page"]["raw_option"] == "StpiShrinkOutput"
    assert summary["collate"]["supported"] is False
    assert summary["quality"]["recommended_mapping"]["high"] is None
    assert summary["color_modes"]["choices"] == ["monochrome", "color", "auto"]


def test_summary_uses_the_option_selected_for_submission() -> None:
    capabilities = {
        "sides": {"one-sided", "two-sided-long-edge"},
        "Duplex": {"None", "DuplexNoTumble", "DuplexTumble"},
        "print-color-mode": {"monochrome", "color", "auto"},
        "ColorModel": {"Gray", "RGB"},
        "print-quality": {"3", "4", "5"},
        "Resolution": {"300dpi", "600dpi", "612x600dpi"},
        "Collate": {"True", "False"},
    }
    summary = build_options_summary("Canon_MG5350", capabilities)
    assert summary["duplex_modes"]["option_mapping"]["long-edge"] == "sides"
    assert summary["duplex_modes"]["mapping"]["long-edge"] == "two-sided-long-edge"
    assert summary["duplex_modes"]["option_mapping"]["short-edge"] == "Duplex"
    assert summary["color_modes"]["raw_option"] == "print-color-mode"
    assert summary["quality"]["raw_option"] == "print-quality"
    assert summary["collate"]["supported"] is True
    assert PrintOptions(copies=2, collate=True).to_cups_options(capabilities).applied_options["Collate"] == "True"
    assert PrintOptions(copies=2, collate=False).to_cups_options(capabilities).applied_options["Collate"] == "False"


def test_quality_summary_lists_api_choices_when_raw_options_are_mixed() -> None:
    capabilities = {
        "print-quality": {"3"},
        "Resolution": {"600dpi", "612x600dpi", "1200x600dpi"},
    }
    quality = build_options_summary("Canon_MG5350", capabilities)["quality"]

    assert quality["supported"] is True
    assert quality["raw_option"] is None
    assert quality["choices"] == ["draft", "normal", "high"]
    assert quality["recommended_mapping"] == {
        "draft": "3",
        "normal": "600dpi",
        "high": "1200x600dpi",
    }
    assert quality["option_mapping"] == {
        "draft": "print-quality",
        "normal": "Resolution",
        "high": "Resolution",
    }


def test_empty_capabilities_do_not_advertise_orientation() -> None:
    assert build_options_summary("Canon_MG5350", {})["orientation"]["supported"] is False


# Capabilities of the real Canon_MG5350 queue: the PPD plus the IPP job
# attributes CUPS advertises for it (checked against CUPS 2.4 + Gutenprint).
MG5350_QUEUE = {
    **GUTENPRINT_CAPABILITIES,
    "InputSlot": {"AutoSwitch", "Rear", "Cassette", "Continuous", "CD"},
    "StpFullBleed": {"False", "True"},
    "StpImageType": {"None", "Text", "Graphics", "TextGraphics", "Photo", "LineArt"},
    "sides": {"one-sided", "two-sided-long-edge", "two-sided-short-edge"},
    "print-color-mode": {"monochrome", "color"},
    "print-quality": {"4", "5"},
    "number-up": {"1", "2", "4", "6", "9", "16"},
    "number-up-layout": {"lrtb", "lrbt", "rltb", "rlbt", "tblr", "tbrl", "btlr", "btrl"},
    "page-delivery": {"reverse-order", "same-order"},
    "print-scaling": {"auto", "auto-fit", "fill", "fit", "none"},
    "media-source": {"auto", "rear", "main", "continuous", "cd"},
    "orientation-requested": {"3", "4", "5", "6"},
    "collate": {"true", "false"},
}


def test_orientation_uses_cups_layout_attribute_when_advertised() -> None:
    # pdftopdf ignores StpOrientation, so pages per sheet would be laid out
    # as if no orientation were set.
    mapped = PrintOptions(orientation="landscape", pages_per_sheet=2).to_cups_options(MG5350_QUEUE)

    assert mapped.applied_options["orientation-requested"] == "4"
    assert "StpOrientation" not in mapped.applied_options
    summary = build_options_summary("Canon_MG5350", MG5350_QUEUE)["orientation"]
    assert summary["raw_option"] == "orientation-requested"
    assert summary["choices"] == ["portrait", "landscape", "reverse-landscape", "reverse-portrait"]


def test_print_dialog_options_map_to_standard_cups_attributes() -> None:
    mapped = PrintOptions(
        copies=2, collate=True, pages_per_sheet=4, pages_per_sheet_layout="tblr", page_border="single",
        scaling="fill", reverse_order=True, media_source="rear", borderless=True,
    ).to_cups_options(MG5350_QUEUE)

    assert mapped.unsupported_options == []
    assert mapped.applied_options == {
        "copies": "2",
        "collate": "true",
        "number-up": "4",
        "number-up-layout": "tblr",
        "page-border": "single",
        "print-scaling": "fill",
        "page-delivery": "reverse-order",
        "InputSlot": "Rear",
        "StpFullBleed": "True",
    }


def test_fit_to_page_now_scales_through_print_scaling() -> None:
    # The Gutenprint StpiShrinkOutput fallback only restates the driver default.
    assert PrintOptions(fit_to_page=True).to_cups_options(MG5350_QUEUE).applied_options == {
        "copies": "1", "print-scaling": "fit",
    }
    assert PrintOptions(fit_to_page=True).to_cups_options(GUTENPRINT_CAPABILITIES).applied_options[
        "StpiShrinkOutput"] == "Shrink"


def test_scale_percent_asks_cups_for_actual_size() -> None:
    mapped = PrintOptions(scale_percent=80).to_cups_options(MG5350_QUEUE)

    assert mapped.applied_options["print-scaling"] == "none"
    assert "scale_percent" in PrintOptions(scale_percent=80).to_cups_options({"PageSize": {"A4"}}).unsupported_options


@pytest.mark.parametrize("options", [
    {"scale_percent": 80, "scaling": "fit"},
    {"scale_percent": 80, "fit_to_page": True},
    {"scale_percent": 80, "pages_per_sheet": 2},
    {"scale_percent": 5},
    {"pages_per_sheet": 3},
])
def test_invalid_scaling_combinations_are_rejected(options: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        PrintOptions(**options)


def test_media_source_accepts_ppd_keywords_aliases_and_ipp_names() -> None:
    assert PrintOptions(media_source="Cassette").to_cups_options(MG5350_QUEUE).applied_options["InputSlot"] == "Cassette"
    assert PrintOptions(media_source="auto").to_cups_options(MG5350_QUEUE).applied_options["InputSlot"] == "AutoSwitch"
    ipp_only = PrintOptions(media_source="main").to_cups_options({"media-source": {"auto", "main"}})
    assert ipp_only.applied_options["media-source"] == "main"
    assert "media_source" in PrintOptions(media_source="manual").to_cups_options(MG5350_QUEUE).unsupported_options


def test_advanced_options_pass_through_only_detected_choices() -> None:
    mapped = PrintOptions(
        duplex="long-edge",
        advanced={"StpImageType": "Photo", "Resolution": "605x600dpi", "StpBogus": "x",
                  "PageSize": "A4", "sides": "one-sided", "MediaType": "Vinyl"},
    ).to_cups_options(MG5350_QUEUE)

    assert mapped.applied_options["StpImageType"] == "Photo"
    assert mapped.applied_options["sides"] == "two-sided-long-edge"
    assert "Ignored advanced sides because a print setting already sets it" in mapped.warnings
    # 605x600dpi is not among the trimmed test Resolution choices.
    assert sorted(mapped.unsupported_options) == [
        "advanced.MediaType", "advanced.PageSize", "advanced.Resolution", "advanced.StpBogus",
    ]
    assert "Resolution" not in mapped.applied_options


def test_number_up_needs_an_advertised_value() -> None:
    mapped = PrintOptions(pages_per_sheet=16).to_cups_options({"number-up": {"1", "2"}})

    assert "pages_per_sheet" in mapped.unsupported_options
    assert "number-up" not in mapped.applied_options


def test_options_summary_describes_new_dialog_settings() -> None:
    from app.services.ppd import parse_ppd

    ppd = parse_ppd(
        (Path(__file__).parent / "fixtures" / "gutenprint_mg5350_excerpt.ppd").read_text(encoding="utf-8")
    )
    capabilities = {**ppd.capabilities(), **{k: v for k, v in MG5350_QUEUE.items() if k[0].islower()}}
    summary = build_options_summary(
        "Canon_MG5350", capabilities, ppd=ppd,
        ipp={"sides-default": "one-sided", "print-color-mode-default": "color", "print-quality-default": 4},
    )

    assert summary["pages_per_sheet"]["choices"] == [1, 2, 4, 6, 9, 16]
    assert summary["scaling"]["custom_percent"]["supported"] is True
    assert summary["reverse_order"]["supported"] is True
    assert summary["media_sources"]["labels"]["Rear"] == "Rear tray"
    assert summary["borderless"]["supported"] is True
    assert summary["paper_sizes"]["labels"]["w288h432"] == "4x6"
    assert {"value": "A4", "label": "A4", "width_pt": 595.0, "height_pt": 842.0,
            "margins_pt": {"left": 0.0, "bottom": 0.0, "right": 0.0, "top": 0.0}} in summary["paper_sizes"]["details"]
    assert summary["media_types"]["labels"]["GlossyPaper"] == "Glossy Photo Paper"
    assert summary["defaults"] == {
        "paper_size": "Letter", "duplex": "none", "color_mode": "color", "quality": "normal",
        "media_type": "plain", "media_source": "AutoSwitch", "copies": 1, "pages_per_sheet": 1, "scaling": "auto",
    }
    advanced = {option["keyword"]: option for option in summary["advanced"]}
    # Settings with a dedicated control stay out of the advanced list...
    assert not {"PageSize", "Duplex", "ColorModel", "MediaType", "InputSlot", "StpFullBleed"} & set(advanced)
    # ...but photo and other print modes stay reachable.
    assert {"value": "605x600dpi", "label": "600x600 DPI PHOTO HIGH"} in advanced["Resolution"]["choices"]
    assert advanced["StpImageType"]["default"] == "TextGraphics"
    assert summary["driver"] == "Canon PIXMA MG5350 - CUPS+Gutenprint v5.3.4"
