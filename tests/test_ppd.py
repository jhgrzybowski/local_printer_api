from __future__ import annotations

from pathlib import Path

from app.services.lpoptions_parser import parse_ppd_options
from app.services.ppd import decode_label, paper_size, parse_ppd


FIXTURE = Path(__file__).parent / "fixtures" / "gutenprint_mg5350_excerpt.ppd"


def test_real_gutenprint_ppd_keeps_labels_defaults_and_groups() -> None:
    ppd = parse_ppd(FIXTURE.read_text(encoding="utf-8"))

    assert ppd.nickname == "Canon PIXMA MG5350 - CUPS+Gutenprint v5.3.4"
    input_slot = ppd.options["InputSlot"]
    assert input_slot.label == "Media Source"
    assert input_slot.default == "AutoSwitch"
    assert input_slot.choice_label("Rear") == "Rear tray"
    assert ppd.options["StpFullBleed"].hint == "checkbox"
    assert ppd.options["StpFullBleed"].group == "Printer Features Common"
    assert ppd.options["Resolution"].choice_label("612x600dpi") == "600x600 DPI T-SHIRT"
    assert ppd.options["MediaType"].choice_label("PhotoPlusGloss2") == "Photo Paper Plus Glossy II"


def test_real_gutenprint_ppd_paper_geometry() -> None:
    ppd = parse_ppd(FIXTURE.read_text(encoding="utf-8"))

    four_by_six = ppd.paper_sizes["w288h432"]
    assert (four_by_six.label, four_by_six.width, four_by_six.height) == ("4x6", 288.0, 432.0)
    assert ppd.paper_sizes["A4"].imageable == (0.0, 0.0, 595.0, 842.0)
    assert ppd.options["PageSize"].default == "Letter"


def test_capabilities_match_option_choices() -> None:
    capabilities = parse_ppd(FIXTURE.read_text(encoding="utf-8")).capabilities()

    assert capabilities["Duplex"] == {"None", "DuplexNoTumble", "DuplexTumble"}
    assert capabilities["PageSize"] == {"Letter", "A4", "w288h432"}
    assert parse_ppd_options(FIXTURE.read_text(encoding="utf-8")) == capabilities


def test_multiline_values_and_constraints() -> None:
    ppd = parse_ppd(
        "*OpenUI *Duplex/Two-Sided: PickOne\n"
        "*DefaultDuplex: None\n"
        "*Duplex None/Off: \"<</Duplex false>>\n"
        "*Duplex Fake/Fake: this continuation line is not a choice\"\n"
        "*Duplex DuplexNoTumble/Long Edge: \"\"\n"
        "*CloseUI: *Duplex\n"
        "*OpenUI *MediaType/Paper: PickOne\n"
        "*MediaType Plain/Plain <E9>t<E9>: \"\"\n"
        "*MediaType Transparency/Film: \"\"\n"
        "*CloseUI: *MediaType\n"
        "*UIConstraints: *Duplex *MediaType Transparency\n"
        "*UIConstraints: *MediaType Transparency *Duplex\n"
    )

    assert [choice.value for choice in ppd.options["Duplex"].choices] == ["None", "DuplexNoTumble"]
    assert ppd.options["MediaType"].choice_label("Plain") == "Plain été"
    assert ppd.conflicts({"Duplex": "DuplexNoTumble", "MediaType": "Transparency"}) == [
        "Duplex=DuplexNoTumble cannot be combined with MediaType=Transparency",
        "MediaType=Transparency cannot be combined with Duplex=DuplexNoTumble",
    ]
    # An option named without a choice matches anything except its off value.
    assert ppd.conflicts({"Duplex": "None", "MediaType": "Transparency"}) == []
    assert ppd.conflicts({"Duplex": "DuplexNoTumble", "MediaType": "Plain"}) == []


def test_paper_size_fallbacks() -> None:
    assert paper_size("A4").width == 595  # type: ignore[union-attr]
    assert paper_size("A4.Borderless").height == 842  # type: ignore[union-attr]
    size = paper_size("w360h504")
    assert size is not None and (size.width, size.height) == (360.0, 504.0)
    assert paper_size("Mystery") is None


def test_decode_label_keeps_unpaired_hex() -> None:
    assert decode_label("A<4>B") == "A<4>B"
