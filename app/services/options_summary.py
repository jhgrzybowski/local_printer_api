from __future__ import annotations

from typing import Any

from app.models.print_options import PrintOptions


def build_options_summary(
    queue_name: str,
    capabilities: dict[str, set[str]],
    include_debug: bool = False,
) -> dict[str, Any]:
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
    }
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
        for api_value, candidates in {
            "plain": ("Plain",),
            "photo": ("PhotoPlusGloss2", "PhotoProSemiGloss", "PhotoProPlat", "PhotopaperOther"),
            "glossy": ("GlossyPaper", "PhotoPlusGloss2"),
            "matte": ("PhotopaperMatte",),
        }.items():
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
        ("fit-to-page", "fitplot", "StpiShrinkOutput", "StpShrinkOutput"),
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
