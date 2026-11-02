from __future__ import annotations

from app.services.ppd import parse_ppd


def parse_lpoptions(output: str) -> dict[str, set[str]]:
    capabilities: dict[str, set[str]] = {}
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or ":" not in line:
            continue
        option_part, choices_part = line.split(":", 1)
        option_name = option_part.split("/", 1)[0].strip()
        if not option_name:
            continue
        choices = {
            choice.lstrip("*").strip()
            for choice in choices_part.split()
            if choice.lstrip("*").strip()
        }
        capabilities[option_name] = choices
    return capabilities


def parse_ppd_options(output: str) -> dict[str, set[str]]:
    return parse_ppd(output).capabilities()
