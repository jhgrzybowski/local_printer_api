from __future__ import annotations

import ctypes.util
import fcntl
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile

from app.services.office_formats import OFFICE_COMPONENTS, OFFICE_FORMATS
from app.services.pdf_metadata import PdfMetadataError, get_pdf_page_count

OUTPUT_LIMIT_BYTES = 100 * 1024 * 1024


class ConversionError(ValueError):
    def __init__(self, message: str, status_code: int = 422):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class OfficeConverter:
    def __init__(self, root: Path):
        self.root = root
        self.timeout = int(os.getenv("OFFICE_TIMEOUT_SECONDS", "90"))
        self.max_pages = int(os.getenv("OFFICE_MAX_PAGES", "500"))
        self.memory_mb = int(os.getenv("OFFICE_MEMORY_MB", "1536"))
        self.cpu_time_soft_limit_seconds = self.timeout
        self.cpu_time_hard_limit_seconds = self.timeout * (os.cpu_count() or 1) + 1
        if min(self.timeout, self.max_pages, self.memory_mb) < 1:
            raise ValueError("Office conversion limits must be positive")

    def executable(self) -> str | None:
        if os.getenv("OFFICE_ENABLED", "true").lower() not in {"1", "true", "yes"}:
            return None
        if ctypes.util.find_library("seccomp") is None:
            return None
        return shutil.which("libreoffice") or shutil.which("soffice")

    def format_availability(self) -> dict[str, bool]:
        if not self.executable():
            return {extension: False for extension in OFFICE_FORMATS}

        dpkg_query = shutil.which("dpkg-query")
        if not dpkg_query:
            return {extension: False for extension in OFFICE_FORMATS}

        installed_components: set[str] = set()
        for component in set(OFFICE_COMPONENTS.values()):
            try:
                result = subprocess.run(
                    [dpkg_query, "-W", "-f=${db:Status-Status}", f"libreoffice-{component}"],
                    capture_output=True,
                    text=True,
                    timeout=2,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            if result.returncode == 0 and result.stdout.strip() == "installed":
                installed_components.add(component)

        return {
            extension: OFFICE_COMPONENTS[extension] in installed_components
            for extension in OFFICE_FORMATS
        }

    def convert(self, source: Path, extension: str, destination: Path) -> int:
        executable = self.executable()
        if not executable:
            raise ConversionError("Office conversion is unavailable; check /capabilities", 503)
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / ".office.lock").open("a+b") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ConversionError("Office converter is busy; retry the upload later", 503) from exc
            with tempfile.TemporaryDirectory(prefix="office-", dir=self.root) as workspace:
                work = Path(workspace).resolve()
                profile = work / "profile" / "user"
                profile.mkdir(parents=True)
                (profile / "registrymodifications.xcu").write_text(
                    '<?xml version="1.0"?><oor:items xmlns:oor="http://openoffice.org/2001/registry">'
                    '<item oor:path="/org.openoffice.Office.Common/Security/Scripting">'
                    '<prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop>'
                    '<prop oor:name="DisableMacrosExecution" oor:op="fuse"><value>true</value></prop>'
                    '<prop oor:name="BlockUntrustedRefererLinks" oor:op="fuse"><value>true</value></prop>'
                    '</item></oor:items>', encoding="utf-8")
                input_path = work / f"document.{extension}"
                shutil.copyfile(source, input_path)
                output = work / "output"
                output.mkdir()
                filter_name = OFFICE_FORMATS[extension][2]
                export_options = '{"SinglePageSheets":{"type":"boolean","value":"false"},"ExportHiddenSlides":{"type":"boolean","value":"false"}}'
                command = [sys.executable, str(Path(__file__).with_name("office_worker.py")),
                           str(self.memory_mb), str(self.timeout), str(OUTPUT_LIMIT_BYTES), executable,
                           f"-env:UserInstallation={(work / 'profile').as_uri()}",
                           "--headless", "--nologo", "--nodefault", "--norestore",
                           "--convert-to", f"pdf:{filter_name}:{export_options}",
                           "--outdir", str(output), str(input_path)]
                environment = {**os.environ, "TMPDIR": str(work), "SAL_USE_VCLPLUGIN": "svp", "LANG": "C.UTF-8", "TZ": "UTC"}
                try:
                    process = subprocess.Popen(command, cwd=work, env=environment, start_new_session=True,
                                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    try:
                        returncode = process.wait(timeout=self.timeout)
                    finally:
                        # Also terminate any helper descendants after an early launcher exit.
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        process.wait()
                except subprocess.TimeoutExpired as exc:
                    raise ConversionError("Office conversion exceeded its time limit", 504) from exc
                except OSError as exc:
                    raise ConversionError("Office converter could not start", 503) from exc
                pdf = output / "document.pdf"
                if returncode != 0 or not pdf.is_file():
                    if returncode == -signal.SIGXCPU:
                        raise ConversionError("Office conversion exceeded its CPU time limit", 504)
                    output_size = pdf.stat().st_size if pdf.is_file() else 0
                    if returncode == -signal.SIGXFSZ or output_size >= OUTPUT_LIMIT_BYTES:
                        raise ConversionError("Converted document exceeded its output file size limit", 413)
                    raise ConversionError("Office conversion failed; check for corruption, encryption, or unsupported content")
                try:
                    pages = get_pdf_page_count(pdf)
                except PdfMetadataError as exc:
                    raise ConversionError("Office converter did not produce a readable PDF") from exc
                if not 1 <= pages <= self.max_pages:
                    raise ConversionError(f"Converted document must contain 1 to {self.max_pages} pages", 413)
                destination.parent.mkdir(parents=True, exist_ok=True)
                pdf.replace(destination)
                return pages
