"""Queue setup must distinguish the exact MG5350 PPD from its fallbacks."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "setup_printer.sh"
MODELS = {
    "MG5350": "Canon PIXMA MG5350 - CUPS+Gutenprint v5.3.4",
    "MG5300": "Canon PIXMA MG5300 - CUPS+Gutenprint v5.3.4",
    "series": "Canon MG5300 series - CUPS+Gutenprint v5.3.4",
}


def run_setup(
    tmp_path: Path,
    nickname: str | None,
    *args: str,
    model: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], str]:
    commands = tmp_path / "commands"
    commands.mkdir(parents=True)
    mock = commands / "mock"
    mock.write_text(
        """#!/usr/bin/env bash
case "${0##*/}" in
  lpinfo)
    cat <<'EOF'
gutenprint.5.3://bjc-PIXMA-MG5350/expert Canon PIXMA MG5350 - CUPS+Gutenprint v5.3.4
gutenprint.5.3://bjc-PIXMA-MG5300/expert Canon PIXMA MG5300 - CUPS+Gutenprint v5.3.4
gutenprint.5.3://bjc-MG5300-series/expert Canon MG5300 series - CUPS+Gutenprint v5.3.4
EOF
    ;;
  lpstat)
    if [[ "$1" == -v ]]; then
      echo "device for Canon_MG5350: lpd://192.168.100.100/PASSTHRU"
    else
      echo "printer Canon_MG5350 is idle"
    fi
    ;;
  lpoptions)
    cat <<'EOF'
ColorModel/Color Model: *RGB Gray
StpQuality/Print Quality: *Standard Photo
Duplex/2-Sided Printing: *None DuplexNoTumble
EOF
    ;;
  lpadmin|cupsenable|cupsaccept|lp) printf '%s %s\\n' "${0##*/}" "$*" >> "$MOCK_LOG" ;;
esac
"""
    )
    mock.chmod(0o755)
    for name in ("lpinfo", "lpstat", "lpoptions", "lpadmin", "cupsenable", "cupsaccept", "lp"):
        (commands / name).symlink_to(mock)

    ppd_dir = tmp_path / "ppd"
    ppd_dir.mkdir()
    if nickname is not None:
        (ppd_dir / "Canon_MG5350.ppd").write_text(f'*NickName: "{nickname}"\n')

    log = tmp_path / "calls.log"
    env = os.environ.copy()
    env.update(
        PATH=f"{commands}:{env['PATH']}",
        PPD_DIR=str(ppd_dir),
        MOCK_LOG=str(log),
    )
    env.pop("MODEL", None)
    if model is not None:
        env["MODEL"] = model
    result = subprocess.run(["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True)
    return result, log.read_text() if log.exists() else ""


def test_matching_mg5350_queue_is_left_unchanged(tmp_path: Path) -> None:
    result, calls = run_setup(tmp_path, MODELS["MG5350"])

    assert result.returncode == 0, result.stderr
    assert "No changes needed" in result.stdout
    assert calls == ""


@pytest.mark.parametrize("fallback", ["MG5300", "series"])
def test_fallback_queue_requires_force(tmp_path: Path, fallback: str) -> None:
    result, calls = run_setup(tmp_path, MODELS[fallback])

    assert result.returncode == 2
    assert f"Existing PPD NickName: {MODELS[fallback]}" in result.stdout
    assert f"Expected PPD NickName: {MODELS['MG5350']}" in result.stdout
    assert calls == ""

    forced, calls = run_setup(tmp_path / "forced", MODELS[fallback], "--force")
    assert forced.returncode == 0, forced.stderr
    assert "lpadmin -x Canon_MG5350" in calls
    assert "-m gutenprint.5.3://bjc-PIXMA-MG5350/expert" in calls


def test_missing_ppd_does_not_claim_model_match(tmp_path: Path) -> None:
    result, calls = run_setup(tmp_path, None)

    assert result.returncode == 2
    assert "unavailable (check PPD permissions)" in result.stdout
    assert calls == ""


def test_model_override_is_used_for_existing_queue(tmp_path: Path) -> None:
    result, calls = run_setup(
        tmp_path,
        MODELS["MG5300"],
        model="gutenprint.5.3://bjc-PIXMA-MG5300/expert",
    )

    assert result.returncode == 0, result.stderr
    assert "No changes needed" in result.stdout
    assert calls == ""
