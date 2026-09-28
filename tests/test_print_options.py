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
    assert PrintOptions(quality="high").to_cups_options(GUTENPRINT_CAPABILITIES).applied_options["Resolution"] == "612x600dpi"


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
    assert summary["quality"]["recommended_mapping"]["high"] == "612x600dpi"
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
        "Resolution": {"600dpi", "612x600dpi"},
    }
    quality = build_options_summary("Canon_MG5350", capabilities)["quality"]

    assert quality["supported"] is True
    assert quality["raw_option"] is None
    assert quality["choices"] == ["draft", "normal", "high"]
    assert quality["recommended_mapping"] == {
        "draft": "3",
        "normal": "600dpi",
        "high": "612x600dpi",
    }
    assert quality["option_mapping"] == {
        "draft": "print-quality",
        "normal": "Resolution",
        "high": "Resolution",
    }


def test_empty_capabilities_do_not_advertise_orientation() -> None:
    assert build_options_summary("Canon_MG5350", {})["orientation"]["supported"] is False
