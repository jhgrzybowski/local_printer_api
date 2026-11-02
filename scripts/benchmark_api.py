#!/usr/bin/env python3
"""Measure the latency of a print-dialog session against a running API.

The script signs up a throwaway account, uploads generated documents, and
times the requests a print dialog makes: printer status, options, upload,
preview, validation, and (with --print) real submissions. It uses only the
standard library so it runs on the target host without extra packages.

    python3 scripts/benchmark_api.py --base-url http://127.0.0.1:8000
    python3 scripts/benchmark_api.py --print --json results.json

--print submits real jobs. Point it at a test queue or be ready for paper.
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import secrets
import statistics
import sys
import time
import urllib.error
import urllib.request
import uuid
import zlib
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable


def text_pdf(pages: int, lines: int = 46) -> bytes:
    """Build a dense text PDF with vector rules, similar to an office document."""
    objects: list[bytes] = []
    page_ids = []
    font_id = 3
    objects.append(b"")  # 1: catalog, filled in below
    objects.append(b"")  # 2: page tree, filled in below
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for number in range(1, pages + 1):
        body = [b"BT /F1 10 Tf 56 790 Td 13 TL"]
        for line in range(lines):
            text = f"Page {number} line {line + 1}: The quick brown fox jumps over the lazy dog 0123456789."
            body.append(f"({text}) Tj T*".encode())
        body.append(b"ET 0.5 w")
        for row in range(0, 760, 20):
            body.append(f"40 {row + 40} m 555 {row + 40} l S".encode())
        stream = zlib.compress(b"\n".join(body))
        objects.append(
            b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(stream) + stream + b"\nendstream"
        )
        content_id = len(objects)
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> /Contents {content_id} 0 R >>".encode()
        )
        page_ids.append(len(objects))
    objects[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)
    objects[1] = f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>".encode()
    output = bytearray(b"%PDF-1.4\n")
    offsets = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(output))
        output += f"{index} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(output)
    output += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        output += f"{offset:010d} 00000 n \n".encode()
    output += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(output)


def photo_jpeg(width: int = 3000, height: int = 2000) -> bytes | None:
    try:
        from io import BytesIO

        from PIL import Image
    except ImportError:
        return None
    image = Image.effect_mandelbrot((width, height), (-2.2, -1.2, 1.0, 1.2), 120).convert("RGB")
    buffer = BytesIO()
    image.save(buffer, "JPEG", quality=88)
    return buffer.getvalue()


class Client:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))

    def request(self, method: str, path: str, body: Any = None, raw: bytes | None = None,
                headers: dict[str, str] | None = None) -> tuple[int, bytes]:
        data = raw
        all_headers = dict(headers or {})
        if body is not None:
            data = json.dumps(body).encode()
            all_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base_url + path, data=data, method=method, headers=all_headers)
        try:
            with self.opener.open(request, timeout=120) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def size(self, path: str, encoding: str) -> int:
        """Bytes on the wire for one GET with the given Accept-Encoding."""
        request = urllib.request.Request(self.base_url + path, headers={"Accept-Encoding": encoding})
        with self.opener.open(request, timeout=120) as response:
            return len(response.read())

    def json(self, method: str, path: str, body: Any = None, expect: tuple[int, ...] = (200,)) -> Any:
        status, payload = self.request(method, path, body)
        if status not in expect:
            raise RuntimeError(f"{method} {path} -> {status}: {payload[:300]!r}")
        return json.loads(payload) if payload else None

    def upload(self, name: str, content: bytes, mime: str) -> dict[str, Any]:
        boundary = uuid.uuid4().hex
        body = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\n"
            f"Content-Type: {mime}\r\n\r\n"
        ).encode() + content + f"\r\n--{boundary}--\r\n".encode()
        status, payload = self.request(
            "POST", "/files", raw=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        if status != 200:
            raise RuntimeError(f"upload {name} -> {status}: {payload[:300]!r}")
        return json.loads(payload)


def measure(samples: int, action: Callable[[], Any]) -> list[float]:
    timings = []
    for _ in range(samples):
        started = time.perf_counter()
        action()
        timings.append((time.perf_counter() - started) * 1000)
    return timings


def summary(timings: list[float]) -> dict[str, float]:
    ordered = sorted(timings)
    p95_index = max(0, min(len(ordered) - 1, round(0.95 * (len(ordered) - 1))))
    return {
        "n": len(ordered),
        "p50_ms": round(statistics.median(ordered), 1),
        "p95_ms": round(ordered[p95_index], 1),
        "mean_ms": round(statistics.fmean(ordered), 1),
    }


def wait_for_job(client: Client, job_id: int, timeout: float = 120) -> tuple[str, float]:
    started = time.perf_counter()
    state = "unknown"
    while time.perf_counter() - started < timeout:
        status, payload = client.request("GET", f"/jobs/{job_id}")
        if status == 200:
            state = json.loads(payload).get("state", "unknown")
            if state in {"completed", "canceled", "aborted"}:
                break
        time.sleep(0.05)
    return state, (time.perf_counter() - started) * 1000


def run(args: argparse.Namespace) -> dict[str, Any]:
    client = Client(args.base_url)
    username = f"bench{secrets.token_hex(4)}"
    client.json("POST", "/auth/signup", {"username": username, "password": "benchmark-password"})
    results: dict[str, Any] = {}
    samples = args.iterations

    def record(name: str, timings: list[float]) -> None:
        results[name] = summary(timings)
        print(f"{name:<44} {json.dumps(results[name])}", flush=True)

    record("GET /status", measure(samples, lambda: client.json("GET", "/status")))
    record("GET /options", measure(samples, lambda: client.json("GET", "/options")))
    results["bytes: GET /options (identity)"] = client.size("/options", "identity")
    results["bytes: GET /options (gzip)"] = client.size("/options", "gzip")
    print(f"{'bytes: GET /options identity / gzip':<44} {results['bytes: GET /options (identity)']} / "
          f"{results['bytes: GET /options (gzip)']}", flush=True)

    one_page = text_pdf(1)
    twenty_pages = text_pdf(20)
    record("POST /files (1-page PDF)", measure(samples, lambda: client.upload("one.pdf", one_page, "application/pdf")))
    record("POST /files (20-page PDF)", measure(max(3, samples // 3), lambda: client.upload("twenty.pdf", twenty_pages, "application/pdf")))

    document = client.upload("twenty.pdf", twenty_pages, "application/pdf")
    file_id = document["file_id"]
    record("GET /files/{id}/preview (page list)", measure(samples, lambda: client.json("GET", f"/files/{file_id}/preview")))

    def cold_page() -> None:
        fresh = client.upload("one.pdf", one_page, "application/pdf")
        started = time.perf_counter()
        status, _ = client.request("GET", f"/files/{fresh['file_id']}/preview/1")
        cold_page.timings.append((time.perf_counter() - started) * 1000)  # type: ignore[attr-defined]
        if status != 200:
            raise RuntimeError(f"preview -> {status}")
    cold_page.timings = []  # type: ignore[attr-defined]
    for _ in range(samples):
        cold_page()
    record("GET preview page (cold render, text page)", cold_page.timings)  # type: ignore[attr-defined]

    record("GET preview page (cached)", measure(samples, lambda: client.request("GET", f"/files/{file_id}/preview/1")))
    results["bytes: preview page (text, full)"] = len(client.request("GET", f"/files/{file_id}/preview/1")[1])
    listing = client.json("GET", f"/files/{file_id}/preview")["pages"]
    has_thumbs = "thumb_url" in listing[0]

    def first_preview(thumb: bool) -> None:
        """Open a document until its first page image has arrived."""
        fresh = client.upload("one.pdf", one_page, "application/pdf")
        page = client.json("GET", f"/files/{fresh['file_id']}/preview")["pages"][0]
        status, _ = client.request("GET", page["thumb_url"] if thumb else page["url"])
        if status != 200:
            raise RuntimeError(f"preview -> {status}")
    record("Open doc -> first page visible (full)", measure(samples, lambda: first_preview(False)))
    if has_thumbs:
        record("Open doc -> first page visible (thumb)", measure(samples, lambda: first_preview(True)))
        results["bytes: preview page (text, thumb)"] = len(client.request("GET", listing[0]["thumb_url"])[1])
        print(f"{'bytes: preview page full / thumb':<44} {results['bytes: preview page (text, full)']} / "
              f"{results['bytes: preview page (text, thumb)']}", flush=True)

    photo = photo_jpeg()
    if photo is not None:
        def cold_photo() -> None:
            fresh = client.upload("photo.jpg", photo, "image/jpeg")
            started = time.perf_counter()
            _, body = client.request("GET", f"/files/{fresh['file_id']}/preview/1")
            cold_photo.timings.append((time.perf_counter() - started) * 1000)  # type: ignore[attr-defined]
            results["bytes: preview page (photo, full)"] = len(body)
        cold_photo.timings = []  # type: ignore[attr-defined]
        for _ in range(max(3, samples // 3)):
            cold_photo()
        record("GET preview (cold render, 6 MP photo)", cold_photo.timings)  # type: ignore[attr-defined]

    def all_pages() -> None:
        fresh = client.upload("twenty.pdf", twenty_pages, "application/pdf")
        pages = client.json("GET", f"/files/{fresh['file_id']}/preview")["pages"]
        with ThreadPoolExecutor(max_workers=6) as pool:
            statuses = list(pool.map(lambda page: client.request("GET", page["url"])[0], pages))
        if any(status != 200 for status in statuses):
            raise RuntimeError(f"preview statuses {statuses}")
    record("All 20 preview pages, 6 parallel (cold)", measure(max(3, samples // 3), all_pages))

    plain = {"file_id": file_id, "strict_options": False,
             "options": {"copies": 1, "paper_size": "A4", "color_mode": "monochrome", "duplex": "none"}}
    ranged = {**plain, "options": {**plain["options"], "pages": "1-3,5,8-12"}}
    record("POST /print/validate (all pages)", measure(samples, lambda: client.json("POST", "/print/validate", plain)))
    record("POST /print/validate (page range)", measure(samples, lambda: client.json("POST", "/print/validate", ranged)))

    def dialog_session() -> None:
        """Open a document, then change three settings as a user would."""
        fresh = client.upload("one.pdf", one_page, "application/pdf")
        client.json("GET", "/status")
        client.json("GET", "/options")
        client.json("GET", f"/files/{fresh['file_id']}/preview")
        client.request("GET", f"/files/{fresh['file_id']}/preview/1")
        for change in ({"color_mode": "monochrome"}, {"duplex": "long-edge"}, {"copies": 2}):
            payload = {"file_id": fresh["file_id"], "strict_options": False, "options": {"paper_size": "A4", **change}}
            client.json("POST", "/print/validate", payload)
    record("Dialog session: open + preview + 3 changes", measure(max(3, samples // 2), dialog_session))

    if args.print:
        def submit() -> None:
            response = client.json("POST", "/print", {"file_id": file_id, "options": {"pages": "1", "paper_size": "A4"}})
            submit.job_ids.append(int(response["job_id"]))  # type: ignore[attr-defined]
        submit.job_ids = []  # type: ignore[attr-defined]
        record("POST /print (1 page)", measure(max(3, samples // 2), submit))
        done = []
        for job_id in submit.job_ids:  # type: ignore[attr-defined]
            state, _ = wait_for_job(client, job_id)
            done.append(state)
        results["submitted_job_states"] = done

        def print_to_done() -> None:
            response = client.json("POST", "/print", {"file_id": file_id, "options": {"pages": "1", "paper_size": "A4"}})
            state, _ = wait_for_job(client, int(response["job_id"]))
            if state != "completed":
                raise RuntimeError(f"job ended {state}")
        record("Print -> CUPS job completed (1 page)", measure(3, print_to_done))

        def normal_quality_to_done() -> None:
            response = client.json("POST", "/print", {"file_id": file_id, "options": {
                "pages": "1", "paper_size": "A4", "quality": "normal", "color_mode": "color"}})
            state, _ = wait_for_job(client, int(response["job_id"]))
            if state != "completed":
                raise RuntimeError(f"job ended {state}")
        record("Print quality=normal -> job completed", measure(3, normal_quality_to_done))
        record("GET /jobs?scope=all", measure(samples, lambda: client.json("GET", "/jobs?scope=all")))
        record("GET /history", measure(samples, lambda: client.json("GET", "/history")))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--iterations", type=int, default=15)
    parser.add_argument("--print", action="store_true", help="also submit real print jobs")
    parser.add_argument("--json", help="write results to this file")
    args = parser.parse_args()
    results = run(args)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as output:
            json.dump(results, output, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
