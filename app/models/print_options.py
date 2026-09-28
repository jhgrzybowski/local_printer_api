from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


Orientation = Literal["portrait", "landscape", "reverse-landscape", "reverse-portrait"]
ColorMode = Literal["monochrome", "color", "auto"]
DuplexMode = Literal["none", "long-edge", "short-edge"]
Quality = Literal["draft", "normal", "high"]


@dataclass(frozen=True)
class CupsOptionMapping:
    applied_options: dict[str, str]
    unsupported_options: list[str]
    warnings: list[str]


class PrintOptions(BaseModel):
    model_config = ConfigDict(extra="allow")

    copies: int = Field(default=1, ge=1, le=99)
    pages: str | None = None
    paper_size: str | None = None
    orientation: Orientation | None = None
    color_mode: ColorMode | None = None
    duplex: DuplexMode | None = None
    quality: Quality | None = None
    collate: bool | None = None
    media_type: str | None = None
    fit_to_page: bool | None = None

    def to_cups_options(
        self,
        capabilities: dict[str, set[str]] | None = None,
    ) -> CupsOptionMapping:
        mapper = _OptionMapper(capabilities or {})
        applied: dict[str, str] = {"copies": str(self.copies)}
        unsupported: list[str] = []
        warnings: list[str] = []

        if self.duplex is not None:
            mapper.map_duplex(self.duplex, applied, unsupported, warnings)
        if self.orientation is not None:
            mapper.map_orientation(self.orientation, applied, warnings)
        if self.paper_size is not None:
            mapper.map_choice("paper_size", self.paper_size, ("PageSize", "media"), applied, unsupported, warnings)
        if self.color_mode is not None:
            mapper.map_color_mode(self.color_mode, applied, unsupported, warnings)
        if self.quality is not None:
            mapper.map_quality(self.quality, applied, unsupported, warnings)
        if self.collate is not None:
            mapper.map_collate(self.collate, self.copies, applied, unsupported, warnings)
        if self.media_type is not None:
            mapper.map_media_type(self.media_type, applied, unsupported, warnings)
        if self.fit_to_page is not None:
            mapper.map_fit_to_page(self.fit_to_page, applied, unsupported, warnings)
        if self.model_extra:
            for name in sorted(self.model_extra):
                unsupported.append(name)
            warnings.append("Dropped unsupported option keys from request")

        return CupsOptionMapping(applied, unsupported, warnings)


class PrintRequest(BaseModel):
    file_id: str
    options: PrintOptions = Field(default_factory=PrintOptions)


class _OptionMapper:
    def __init__(self, capabilities: dict[str, set[str]]) -> None:
        self.capabilities = capabilities

    def map_duplex(
        self,
        value: DuplexMode,
        applied: dict[str, str],
        unsupported: list[str],
        warnings: list[str],
    ) -> None:
        standard_values = {
            "none": "one-sided",
            "long-edge": "two-sided-long-edge",
            "short-edge": "two-sided-short-edge",
        }
        if self._supports("sides", standard_values[value]):
            applied["sides"] = standard_values[value]
            return

        ppd_values = {
            "none": "None",
            "long-edge": "DuplexNoTumble",
            "short-edge": "DuplexTumble",
        }
        if self._supports("Duplex", ppd_values[value]):
            applied["Duplex"] = ppd_values[value]
            warnings.append("Mapped duplex through detected PPD Duplex option")
            return

        if not self.capabilities:
            applied["sides"] = standard_values[value]
            warnings.append("Applied standard CUPS sides option without detected queue capabilities")
            return

        unsupported.append("duplex")
        warnings.append("Dropped duplex because no compatible CUPS option was detected")

    def map_orientation(
        self,
        value: Orientation,
        applied: dict[str, str],
        warnings: list[str],
    ) -> None:
        ppd_values = {
            "portrait": "Portrait",
            "landscape": "Landscape",
            "reverse-landscape": "Seascape",
            "reverse-portrait": "UpsideDown",
        }
        if self._supports("StpOrientation", ppd_values[value]):
            applied["StpOrientation"] = ppd_values[value]
            warnings.append("Mapped orientation through detected PPD StpOrientation option")
            return

        standard_values = {
            "portrait": "3",
            "landscape": "4",
            "reverse-landscape": "5",
            "reverse-portrait": "6",
        }
        applied["orientation-requested"] = standard_values[value]
        if self.capabilities and "orientation-requested" not in self.capabilities:
            warnings.append("Applied standard orientation-requested option without detected queue support")

    def map_color_mode(
        self,
        value: ColorMode,
        applied: dict[str, str],
        unsupported: list[str],
        warnings: list[str],
    ) -> None:
        standard = {
            "monochrome": "monochrome",
            "color": "color",
            "auto": "auto",
        }
        if self._supports("print-color-mode", standard[value]):
            applied["print-color-mode"] = standard[value]
            return

        if "ColorModel" in self.capabilities:
            selected = self._choose_color_model(value)
            if selected:
                applied["ColorModel"] = selected
                warnings.append("Mapped color mode through detected PPD ColorModel option")
                return

        if not self.capabilities:
            applied["print-color-mode"] = standard[value]
            warnings.append("Applied standard print-color-mode option without detected queue capabilities")
            return

        unsupported.append("color_mode")
        warnings.append("Dropped color_mode because no compatible CUPS option was detected")

    def map_quality(
        self,
        value: Quality,
        applied: dict[str, str],
        unsupported: list[str],
        warnings: list[str],
    ) -> None:
        standard = {
            "draft": "3",
            "normal": "4",
            "high": "5",
        }
        if self._supports("print-quality", standard[value]):
            applied["print-quality"] = standard[value]
            return

        for option_name in ("Resolution", "StpQuality", "PrintQuality", "Quality"):
            if option_name in self.capabilities:
                selected = self._choose_quality_value(option_name, value)
                if selected:
                    applied[option_name] = selected
                    warnings.append(f"Mapped quality through detected PPD {option_name} option")
                    return

        if not self.capabilities:
            applied["print-quality"] = standard[value]
            warnings.append("Applied standard print-quality option without detected queue capabilities")
            return

        unsupported.append("quality")
        warnings.append("Dropped quality because no compatible CUPS option was detected")

    def map_collate(
        self,
        value: bool,
        copies: int,
        applied: dict[str, str],
        unsupported: list[str],
        warnings: list[str],
    ) -> None:
        if copies == 1:
            warnings.append("Ignored collate because copies=1")
            return
        self.map_bool("collate", value, ("Collate", "collate"), applied, unsupported, warnings)

    def map_media_type(
        self,
        value: str,
        applied: dict[str, str],
        unsupported: list[str],
        warnings: list[str],
    ) -> None:
        normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
        aliases = {
            "plain": ("Plain",),
            "photo": ("PhotoPlusGloss2", "PhotoProSemiGloss", "PhotoProPlat", "PhotopaperOther"),
            "glossy": ("GlossyPaper", "PhotoPlusGloss2"),
            "matte": ("PhotopaperMatte",),
        }
        candidates = aliases.get(normalized, (value,))
        for option_name in ("MediaType", "media-type"):
            for candidate in candidates:
                if self._supports(option_name, candidate):
                    applied[option_name] = candidate
                    if candidate != value:
                        warnings.append(f"Mapped media_type={value} to detected {option_name}={candidate}")
                    return

        if not self.capabilities:
            applied["MediaType"] = candidates[0]
            warnings.append("Applied MediaType without detected queue capabilities")
            return

        unsupported.append("media_type")
        warnings.append("Dropped media_type because value is not in detected queue capabilities")

    def map_fit_to_page(
        self,
        value: bool,
        applied: dict[str, str],
        unsupported: list[str],
        warnings: list[str],
    ) -> None:
        if not value:
            warnings.append("Ignored fit_to_page=false")
            return

        for option_name, fit_value in (
            ("fit-to-page", "true"),
            ("fitplot", "true"),
            ("StpiShrinkOutput", "Shrink"),
            ("StpShrinkOutput", "Shrink"),
        ):
            if self._supports(option_name, fit_value):
                applied[option_name] = fit_value
                warnings.append(f"Mapped fit_to_page through detected {option_name} option")
                return

        unsupported.append("fit_to_page")
        warnings.append("Dropped fit_to_page because no compatible CUPS/Gutenprint scaling option was detected")

    def map_choice(
        self,
        field_name: str,
        value: str,
        option_names: tuple[str, ...],
        applied: dict[str, str],
        unsupported: list[str],
        warnings: list[str],
    ) -> None:
        for option_name in option_names:
            if self._supports(option_name, value):
                applied[option_name] = value
                return

        if not self.capabilities:
            applied[option_names[0]] = value
            warnings.append(f"Applied {option_names[0]} without detected queue capabilities")
            return

        unsupported.append(field_name)
        warnings.append(f"Dropped {field_name} because value is not in detected queue capabilities")

    def map_bool(
        self,
        field_name: str,
        value: bool,
        option_names: tuple[str, ...],
        applied: dict[str, str],
        unsupported: list[str],
        warnings: list[str],
    ) -> None:
        bool_value = "true" if value else "false"
        for option_name in option_names:
            choices = self.capabilities.get(option_name, set())
            for candidate in (("true", "on", "yes", "1") if value else ("false", "off", "no", "0")):
                selected = next((choice for choice in choices if choice.lower() == candidate), None)
                if selected is not None:
                    applied[option_name] = selected
                    return

        if not self.capabilities:
            applied[option_names[0]] = bool_value
            warnings.append(f"Applied {option_names[0]} without detected queue capabilities")
            return

        unsupported.append(field_name)
        warnings.append(f"Dropped {field_name} because no compatible CUPS option was detected")

    def _choose_color_model(self, value: ColorMode) -> str | None:
        choices = self.capabilities.get("ColorModel", set())
        if value == "monochrome":
            for candidate in ("Gray", "Grey", "Black", "KGray"):
                if candidate in choices:
                    return candidate
        if value == "color":
            for candidate in ("RGB", "CMYK", "Color", "CMY"):
                if candidate in choices:
                    return candidate
        if value == "auto":
            for candidate in ("RGB", "CMYK", "Color", "Gray"):
                if candidate in choices:
                    return candidate
        return None

    def _choose_quality_value(self, option_name: str, value: Quality) -> str | None:
        choices = self.capabilities.get(option_name, set())
        preferred = {
            "draft": ("300dpi", "Draft", "Fast", "3"),
            "normal": ("600dpi", "601x600dpi", "Standard", "Normal", "4"),
            "high": ("1200dpi", "High", "Best", "Photo", "5"),
        }
        for candidate in preferred[value]:
            if candidate in choices:
                return candidate
        if value == "high" and option_name == "Resolution":
            resolutions: list[tuple[tuple[int, int], str]] = []
            for choice in choices:
                if choice in preferred["normal"]:
                    continue
                match = re.fullmatch(r"(\d+)(?:x(\d+))?dpi", choice, re.IGNORECASE)
                if match:
                    x_dpi = int(match.group(1))
                    y_dpi = int(match.group(2) or match.group(1))
                    resolutions.append(((x_dpi, y_dpi), choice))
            if resolutions:
                highest = max(resolutions)
                # Do not label an ordinary 600 dpi queue as offering high quality.
                if highest[0] > (600, 600):
                    return highest[1]
        return None

    def _supports(self, option_name: str, value: str) -> bool:
        if option_name not in self.capabilities:
            return False
        choices = self.capabilities[option_name]
        return not choices or value in choices
