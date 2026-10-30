"""Parse the parts of a CUPS PPD that a print dialog needs.

CUPS answers `lpoptions -l` with option keywords only. The PPD also carries
the human labels ("4x6", "Rear tray", "Photo Paper Plus Glossy II"), defaults,
groups, paper dimensions, printable areas, and option conflicts. This parser
reads those without pycups so it can run and be tested anywhere.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Mapping


MAIN_KEYWORD_RE = re.compile(
    r"^\*(?P<keyword>[^\s:/]+)(?:\s+(?P<option>[^\s:/]+)(?:/(?P<label>[^:]*))?)?:\s*(?P<value>.*)$"
)
OPEN_UI_RE = re.compile(r"^\*(?:JCL)?OpenUI\s+\*(?P<option>[^\s:/]+)(?:/(?P<label>[^:]*))?:\s*(?P<ui>\S+)")
CLOSE_UI_RE = re.compile(r"^\*(?:JCL)?CloseUI:\s+\*(?P<option>[^\s:/]+)")
OPEN_GROUP_RE = re.compile(r"^\*Open(?:Sub)?Group:\s*(?P<name>[^/\s]+)(?:/(?P<label>.*))?$")
CLOSE_GROUP_RE = re.compile(r"^\*Close(?:Sub)?Group:")
CONSTRAINT_RE = re.compile(r"^\*(?:Non)?UIConstraints:\s*(?P<body>.+)$")
HEX_RE = re.compile(r"<([0-9A-Fa-f]+)>")
SIZE_IN_NAME_RE = re.compile(r"^w(?P<w>\d+(?:\.\d+)?)h(?P<h>\d+(?:\.\d+)?)")

# Standard sizes in points, used when a queue has no PPD geometry.
STANDARD_PAPER_POINTS: dict[str, tuple[float, float]] = {
    "A3": (842, 1191),
    "A4": (595, 842),
    "A5": (420, 595),
    "A6": (297, 420),
    "B5": (516, 729),
    "ISOB5": (499, 709),
    "Letter": (612, 792),
    "Legal": (612, 1008),
    "Executive": (522, 756),
    "Statement": (396, 612),
    "Postcard": (283, 420),
    "DL": (312, 624),
    "C5": (459, 649),
    "C6": (323, 459),
    "COM10": (297, 684),
}


@dataclass(frozen=True)
class PpdChoice:
    value: str
    label: str


@dataclass(frozen=True)
class PpdOption:
    keyword: str
    label: str
    ui: str
    group: str
    default: str | None
    choices: tuple[PpdChoice, ...]
    hint: str | None = None

    def choice_label(self, value: str) -> str | None:
        return next((choice.label for choice in self.choices if choice.value == value), None)


@dataclass(frozen=True)
class PaperSize:
    keyword: str
    label: str
    width: float
    height: float
    # Left, bottom, right, top in points from the lower-left corner.
    imageable: tuple[float, float, float, float] | None = None


@dataclass(frozen=True)
class PpdInfo:
    nickname: str | None = None
    options: Mapping[str, PpdOption] = field(default_factory=dict)
    paper_sizes: Mapping[str, PaperSize] = field(default_factory=dict)
    # (option, choice or None, option, choice or None); None matches any choice
    # other than the "off" values None/False/Off.
    constraints: tuple[tuple[str, str | None, str, str | None], ...] = ()

    def capabilities(self) -> dict[str, set[str]]:
        return {
            keyword: {choice.value for choice in option.choices}
            for keyword, option in self.options.items()
            if option.choices
        }

    def conflicts(self, selected: Mapping[str, str]) -> list[str]:
        """Describe each PPD constraint the selected options violate."""
        found = []
        for option_a, choice_a, option_b, choice_b in self.constraints:
            if _constraint_matches(selected, option_a, choice_a) and _constraint_matches(selected, option_b, choice_b):
                found.append(
                    f"{option_a}={selected[option_a]} cannot be combined with {option_b}={selected[option_b]}"
                )
        return found


def _constraint_matches(selected: Mapping[str, str], option: str, choice: str | None) -> bool:
    if option not in selected:
        return False
    value = selected[option]
    if choice is None:
        return value.lower() not in {"none", "false", "off"}
    return value == choice


def decode_label(text: str) -> str:
    """Translate PPD <hex> escapes; labels are otherwise plain text."""
    def replace(match: re.Match[str]) -> str:
        digits = match.group(1)
        if len(digits) % 2:
            return match.group(0)
        return bytes.fromhex(digits).decode("latin-1")
    return HEX_RE.sub(replace, text).strip()


def parse_ppd(text: str) -> PpdInfo:
    options: dict[str, dict[str, object]] = {}
    defaults: dict[str, str] = {}
    hints: dict[str, str] = {}
    dimensions: dict[str, tuple[float, float]] = {}
    imageable: dict[str, tuple[float, float, float, float]] = {}
    constraints: list[tuple[str, str | None, str, str | None]] = []
    nickname: str | None = None
    group_stack: list[str] = []
    open_option: str | None = None
    in_quoted_value = False

    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r\n")
        if in_quoted_value:
            # Multi-line invocation strings end at the next double quote.
            if '"' in line:
                in_quoted_value = False
            continue
        if not line.startswith("*") or line.startswith("*%"):
            continue

        open_match = OPEN_UI_RE.match(line)
        if open_match:
            open_option = open_match.group("option")
            options.setdefault(open_option, {
                "label": decode_label(open_match.group("label") or open_option),
                "ui": open_match.group("ui"),
                "group": group_stack[-1] if group_stack else "General",
                "choices": [],
            })
            continue
        if CLOSE_UI_RE.match(line):
            open_option = None
            continue
        group_match = OPEN_GROUP_RE.match(line)
        if group_match:
            group_stack.append(decode_label(group_match.group("label") or group_match.group("name")))
            continue
        if CLOSE_GROUP_RE.match(line):
            if group_stack:
                group_stack.pop()
            continue
        constraint_match = CONSTRAINT_RE.match(line)
        if constraint_match:
            parsed = _parse_constraint(constraint_match.group("body"))
            if parsed is not None:
                constraints.append(parsed)
            continue

        match = MAIN_KEYWORD_RE.match(line)
        if not match:
            continue
        keyword = match.group("keyword")
        option_keyword = match.group("option")
        value = match.group("value").strip()
        if value.count('"') == 1:
            in_quoted_value = True

        if keyword == "NickName":
            nickname = value.strip('"')
        elif keyword.startswith("Default") and option_keyword is None:
            defaults[keyword[len("Default"):]] = value
        elif keyword == "OPOptionHints" and option_keyword:
            hints[option_keyword] = value.strip('"')
        elif keyword == "PaperDimension" and option_keyword:
            numbers = _numbers(value)
            if len(numbers) == 2:
                dimensions[option_keyword] = (numbers[0], numbers[1])
        elif keyword == "ImageableArea" and option_keyword:
            numbers = _numbers(value)
            if len(numbers) == 4:
                imageable[option_keyword] = (numbers[0], numbers[1], numbers[2], numbers[3])
        elif option_keyword and keyword == open_option and open_option in options:
            choices = options[open_option]["choices"]
            assert isinstance(choices, list)
            choices.append(PpdChoice(option_keyword, decode_label(match.group("label") or option_keyword)))

    parsed_options = {
        keyword: PpdOption(
            keyword=keyword,
            label=str(data["label"]),
            ui=str(data["ui"]),
            group=str(data["group"]),
            default=defaults.get(keyword),
            choices=tuple(data["choices"]),  # type: ignore[arg-type]
            hint=hints.get(keyword),
        )
        for keyword, data in options.items()
        if data["choices"]
    }

    page_size = parsed_options.get("PageSize")
    paper_sizes: dict[str, PaperSize] = {}
    for keyword, (width, height) in dimensions.items():
        label = page_size.choice_label(keyword) if page_size else None
        paper_sizes[keyword] = PaperSize(keyword, label or keyword, width, height, imageable.get(keyword))
    return PpdInfo(nickname, parsed_options, paper_sizes, tuple(constraints))


def paper_size(keyword: str, ppd: PpdInfo | None = None) -> PaperSize | None:
    """Look up paper geometry from the PPD, the keyword, or the standard table."""
    if ppd is not None and keyword in ppd.paper_sizes:
        return ppd.paper_sizes[keyword]
    base = keyword.split(".", 1)[0]
    if base in STANDARD_PAPER_POINTS:
        width, height = STANDARD_PAPER_POINTS[base]
        return PaperSize(keyword, base, width, height)
    match = SIZE_IN_NAME_RE.match(keyword)
    if match:
        return PaperSize(keyword, keyword, float(match.group("w")), float(match.group("h")))
    return None


def _parse_constraint(body: str) -> tuple[str, str | None, str, str | None] | None:
    tokens = body.split()
    parsed: list[tuple[str, str | None]] = []
    index = 0
    while index < len(tokens) and len(parsed) < 2:
        token = tokens[index]
        if not token.startswith("*"):
            return None
        choice = None
        if index + 1 < len(tokens) and not tokens[index + 1].startswith("*"):
            choice = tokens[index + 1]
            index += 1
        parsed.append((token[1:], choice))
        index += 1
    if len(parsed) != 2:
        return None
    return parsed[0][0], parsed[0][1], parsed[1][0], parsed[1][1]


def _numbers(value: str) -> list[float]:
    try:
        return [float(item) for item in value.strip('"').split()]
    except ValueError:
        return []
