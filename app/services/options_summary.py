from __future__ import annotations

from typing import Any, Mapping, get_args

from app.models.print_options import (
    ADVANCED_RESERVED_OPTIONS,
    PageBorder,
    PagesPerSheet,
    PagesPerSheetLayout,
    PrintOptions,
    Scaling,
)
from app.services.ppd import PpdInfo, paper_size

COPIES_MAX = 99
SCALE_PERCENT_RANGE = (10, 400)
MEDIA_TYPE_ALIASES = {
    "plain": ("Plain",),
    "photo": ("PhotoPlusGloss2", "PhotoProSemiGloss", "PhotoProPlat", "PhotopaperOther"),
    "glossy": ("GlossyPaper", "PhotoPlusGloss2"),
    "matte": ("PhotopaperMatte",),
}


def build_options_summary(
    queue_name: str,
    capabilities: dict[str, set[str]],
    include_debug: bool = False,
    ppd: PpdInfo | None = None,
    ipp: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    ipp = ipp or {}
    response: dict[str, Any] = {
        "queue": queue_name,
        "paper_sizes": _choice_block("paper_size", _first_values(capabilities, ("PageSize", "media"))),
        "duplex_modes": _duplex_modes(capabilities),
        "color_modes": _color_modes(capabilities),
        "quality": _quality_modes(capabilities),
        "media_types": _media_types(capabilities),
        "collate": {
            "supported": all(_mapped(capabilities, "collate", value) is not None for value in (True, False)),
            "raw_options": _present_names(capabilities, ("Collate", "collate")),
        },
        "fit_to_page": _fit_to_page(capabilities),
        "orientation": _orientation_modes(capabilities),
        "copies": {"supported": True, "min": 1, "max": COPIES_MAX},
        "page_set": {"api_name": "page_set", "supported": True, "choices": ["all", "odd", "even"]},
        "pages_per_sheet": _pages_per_sheet(capabilities),
        "scaling": _scaling(capabilities),
        "reverse_order": {
            "api_name": "reverse_order",
            "supported": _mapped(capabilities, "reverse_order", True) is not None,
        },
        "media_sources": _media_sources(capabilities, ppd),
        "borderless": {
            "api_name": "borderless",
            "raw_option": "StpFullBleed" if "StpFullBleed" in capabilities else None,
            "supported": all(_mapped(capabilities, "borderless", value) is not None for value in (True, False)),
        },
        "defaults": _defaults(capabilities, ppd, ipp),
        "advanced": _advanced(capabilities, ppd),
        "driver": ppd.nickname if ppd is not None else None,
    }
    _describe_paper_sizes(response["paper_sizes"], ppd)
    _describe_media_types(response["media_types"], ppd)
    if include_debug:
        response["debug"] = {
            name: sorted(values)
            for name, values in sorted(capabilities.items())
        }
    return response


def _choice_block(api_name: str, values: tuple[str | None, list[str]]) -> dict[str, Any]:
    raw_option, choices = values
    return {
        "api_name": api_name,
        "raw_option": raw_option,
        "supported": bool(raw_option and choices),
        "choices": choices,
    }


def _duplex_modes(capabilities: dict[str, set[str]]) -> dict[str, Any]:
    selected = {
        value: _mapped(capabilities, "duplex", value)
        for value in ("none", "long-edge", "short-edge")
    }
    supported = {value: result[1] for value, result in selected.items() if result is not None}
    option_mapping = {value: result[0] for value, result in selected.items() if result is not None}
    option_names = set(option_mapping.values())
    return {
        "api_name": "duplex",
        "raw_option": next(iter(option_names)) if len(option_names) == 1 else None,
        "supported": bool(supported),
        "choices": list(supported),
        "mapping": supported,
        "option_mapping": option_mapping,
    }


def _color_modes(capabilities: dict[str, set[str]]) -> dict[str, Any]:
    selected = {value: _mapped(capabilities, "color_mode", value) for value in ("monochrome", "color", "auto")}
    mapping = {value: result[1] for value, result in selected.items() if result is not None}
    option_mapping = {value: result[0] for value, result in selected.items() if result is not None}
    option_names = set(option_mapping.values())
    return {
        "api_name": "color_mode",
        "raw_option": next(iter(option_names)) if len(option_names) == 1 else None,
        "supported": bool(mapping),
        "choices": list(mapping),
        "mapping": mapping,
        "option_mapping": option_mapping,
    }


def _quality_modes(capabilities: dict[str, set[str]]) -> dict[str, Any]:
    selected = {value: _mapped(capabilities, "quality", value) for value in ("draft", "normal", "high")}
    option_names = {result[0] for result in selected.values() if result is not None}
    raw_option = next(iter(option_names)) if len(option_names) == 1 else None
    choices = (
        sorted(capabilities[raw_option])
        if raw_option
        else [value for value, result in selected.items() if result is not None]
    )
    return {
        "api_name": "quality",
        "raw_option": raw_option,
        "supported": bool(option_names),
        "choices": choices,
        "recommended_mapping": {value: result[1] if result else None for value, result in selected.items()},
        "option_mapping": {value: result[0] for value, result in selected.items() if result},
    }


def _media_types(capabilities: dict[str, set[str]]) -> dict[str, Any]:
    raw_option, choices = _first_values(capabilities, ("MediaType", "media-type"))
    mapping: dict[str, str] = {}
    if choices:
        for api_value, candidates in MEDIA_TYPE_ALIASES.items():
            selected = _first_present(set(choices), candidates)
            if selected:
                mapping[api_value] = selected
    return {
        "api_name": "media_type",
        "raw_option": raw_option,
        "supported": bool(mapping),
        "choices": choices,
        "mapping": mapping,
    }


def _fit_to_page(capabilities: dict[str, set[str]]) -> dict[str, Any]:
    raw_option, choices = _first_values(
        capabilities,
        ("print-scaling", "fit-to-page", "fitplot", "StpiShrinkOutput", "StpShrinkOutput"),
    )
    return {
        "api_name": "fit_to_page",
        "raw_option": raw_option,
        "supported": raw_option is not None,
        "choices": choices,
        "notes": "Gutenprint exposes shrink/crop/expand behavior, not a direct IPP fit-to-page option"
        if raw_option == "StpiShrinkOutput"
        else "",
    }


def _orientation_modes(capabilities: dict[str, set[str]]) -> dict[str, Any]:
    mapping = {
        "portrait": "3",
        "landscape": "4",
        "reverse-landscape": "5",
        "reverse-portrait": "6",
    }
    if "orientation-requested" in capabilities:
        return {
            "api_name": "orientation",
            "raw_option": "orientation-requested",
            "supported": True,
            "choices": [
                api_value for api_value, raw_value in mapping.items()
                if _supports(capabilities, "orientation-requested", raw_value)
            ],
            "mapping": mapping,
        }
    if "StpOrientation" in capabilities:
        raw_mapping = {
            "portrait": "Portrait",
            "landscape": "Landscape",
            "reverse-landscape": "Seascape",
            "reverse-portrait": "UpsideDown",
        }
        return {
            "api_name": "orientation",
            "raw_option": "StpOrientation",
            "supported": bool(capabilities["StpOrientation"]),
            "choices": [
                api_value
                for api_value, raw_value in raw_mapping.items()
                if _supports(capabilities, "StpOrientation", raw_value)
            ],
            "mapping": raw_mapping,
        }
    return {
        "api_name": "orientation",
        "raw_option": "orientation-requested",
        "supported": bool(capabilities),
        "choices": list(mapping) if capabilities else [],
        "mapping": mapping,
    }


def _first_values(
    capabilities: dict[str, set[str]],
    names: tuple[str, ...],
) -> tuple[str | None, list[str]]:
    for name in names:
        if name in capabilities:
            return name, sorted(capabilities[name])
    return None, []


def _mapped(capabilities: dict[str, set[str]], field: str, value: str | bool) -> tuple[str, str] | None:
    if not capabilities:
        return None
    result = PrintOptions(copies=2, **{field: value}).to_cups_options(capabilities)
    if field in result.unsupported_options:
        return None
    for name, option_value in result.applied_options.items():
        if name != "copies":
            return name, option_value
    return None


def _present_names(capabilities: dict[str, set[str]], names: tuple[str, ...]) -> list[str]:
    return [name for name in names if name in capabilities]


def _supports(capabilities: dict[str, set[str]], option_name: str, value: str) -> bool:
    return option_name in capabilities and value in capabilities[option_name]


def _first_present(choices: set[str], candidates: tuple[str, ...]) -> str | None:
    for candidate in candidates:
        if candidate in choices:
            return candidate
    return None


def _pages_per_sheet(capabilities: dict[str, set[str]]) -> dict[str, Any]:
    choices = [value for value in get_args(PagesPerSheet) if value == 1 or _supports(capabilities, "number-up", str(value))]
    layouts = [value for value in get_args(PagesPerSheetLayout) if _supports(capabilities, "number-up-layout", value)]
    supported = len(choices) > 1
    return {
        "api_name": "pages_per_sheet",
        "raw_option": "number-up" if supported else None,
        "supported": supported,
        "choices": choices,
        "layouts": layouts,
        "default_layout": "lrtb" if "lrtb" in layouts else None,
        "borders": list(get_args(PageBorder)) if supported else [],
    }


def _scaling(capabilities: dict[str, set[str]]) -> dict[str, Any]:
    choices = [value for value in get_args(Scaling) if _supports(capabilities, "print-scaling", value)]
    return {
        "api_name": "scaling",
        "raw_option": "print-scaling" if choices else None,
        "supported": bool(choices),
        "choices": choices,
        "custom_percent": {
            "api_name": "scale_percent",
            "supported": "none" in choices,
            "min": SCALE_PERCENT_RANGE[0],
            "max": SCALE_PERCENT_RANGE[1],
            "documents": ["application/pdf"],
        },
    }


def _media_sources(capabilities: dict[str, set[str]], ppd: PpdInfo | None) -> dict[str, Any]:
    raw_option, choices = _first_values(capabilities, ("InputSlot", "media-source"))
    option = ppd.options.get(raw_option) if ppd is not None and raw_option else None
    return {
        "api_name": "media_source",
        "raw_option": raw_option,
        "supported": bool(choices),
        "choices": [choice.value for choice in option.choices] if option else choices,
        "labels": {choice.value: choice.label for choice in option.choices} if option else {},
        "default": option.default if option else None,
    }


def _describe_paper_sizes(block: dict[str, Any], ppd: PpdInfo | None) -> None:
    """Add labels and geometry so a client can draw the sheet."""
    page_size = ppd.options.get("PageSize") if ppd is not None else None
    block["default"] = page_size.default if page_size else None
    block["labels"] = {choice.value: choice.label for choice in page_size.choices} if page_size else {}
    details = []
    for keyword in block["choices"]:
        size = paper_size(keyword, ppd)
        if size is None:
            continue
        margins = None
        if size.imageable is not None:
            left, bottom, right, top = size.imageable
            margins = {"left": round(left, 2), "bottom": round(bottom, 2),
                       "right": round(size.width - right, 2), "top": round(size.height - top, 2)}
        details.append({
            "value": keyword,
            "label": block["labels"].get(keyword, size.label),
            "width_pt": round(size.width, 2),
            "height_pt": round(size.height, 2),
            "margins_pt": margins,
        })
    block["details"] = details


def _describe_media_types(block: dict[str, Any], ppd: PpdInfo | None) -> None:
    option = ppd.options.get(block["raw_option"]) if ppd is not None and block["raw_option"] else None
    block["labels"] = {choice.value: choice.label for choice in option.choices} if option else {}
    block["default"] = option.default if option else None


def _defaults(capabilities: dict[str, set[str]], ppd: PpdInfo | None, ipp: Mapping[str, Any]) -> dict[str, Any]:
    """The queue's own defaults, in API values, to initialise a dialog."""
    options = ppd.options if ppd is not None else {}
    sides = ipp.get("sides-default")
    duplex = {"one-sided": "none", "two-sided-long-edge": "long-edge",
              "two-sided-short-edge": "short-edge"}.get(str(sides)) if sides else None
    if duplex is None and "Duplex" in options:
        duplex = {"None": "none", "DuplexNoTumble": "long-edge", "DuplexTumble": "short-edge"}.get(
            options["Duplex"].default or "")
    color = ipp.get("print-color-mode-default")
    quality = {3: "draft", 4: "normal", 5: "high"}.get(ipp.get("print-quality-default"))  # type: ignore[call-overload]
    media_default = options["MediaType"].default if "MediaType" in options else None
    media_type = next(
        (alias for alias, candidates in MEDIA_TYPE_ALIASES.items() if media_default in candidates),
        None,
    )
    return {
        "paper_size": options["PageSize"].default if "PageSize" in options else None,
        "duplex": duplex,
        "color_mode": color if color in {"monochrome", "color"} else None,
        "quality": quality,
        "media_type": media_type,
        "media_source": options["InputSlot"].default if "InputSlot" in options else None,
        "copies": 1,
        "pages_per_sheet": 1,
        "scaling": "auto" if _supports(capabilities, "print-scaling", "auto") else None,
    }


def _advanced(capabilities: dict[str, set[str]], ppd: PpdInfo | None) -> list[dict[str, Any]]:
    """Driver options the main settings do not cover, like a dialog's Advanced tab."""
    if ppd is None:
        return []
    # Driver options that a main setting represents, even when the queue maps
    # that setting through an IPP attribute (sides instead of Duplex). Quality
    # options stay listed: they hold modes such as photo printing that the
    # three quality levels cannot select.
    covered = set(ADVANCED_RESERVED_OPTIONS) | {
        "Duplex", "ColorModel", "MediaType", "InputSlot", "StpFullBleed", "StpOrientation", "Collate",
    }
    probes: dict[str, tuple[Any, ...]] = {
        "duplex": ("none", "long-edge", "short-edge"),
        "color_mode": ("monochrome", "color", "auto"),
        "orientation": ("portrait", "landscape", "reverse-landscape", "reverse-portrait"),
        "media_type": tuple(MEDIA_TYPE_ALIASES),
        "fit_to_page": (True,),
        "borderless": (True, False),
        "collate": (True, False),
        "media_source": ("auto", *sorted(capabilities.get("InputSlot", ()))),
    }
    for field, values in probes.items():
        for value in values:
            result = PrintOptions(copies=2, **{field: value}).to_cups_options(capabilities)
            covered.update(name for name in result.applied_options if name != "copies")
    return [
        {
            "keyword": option.keyword,
            "label": option.label,
            "group": option.group,
            "ui": option.ui,
            "hint": option.hint,
            "default": option.default,
            "choices": [{"value": choice.value, "label": choice.label} for choice in option.choices],
        }
        for option in ppd.options.values()
        if option.keyword not in covered
    ]
