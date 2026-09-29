#!/usr/bin/env python3
"""Opt-in live DOCX/XLSX smoke flow. --print submits one page of each format."""
from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
from io import BytesIO
import json
from pathlib import Path
import secrets
import sys
import time
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pypdf import PdfReader
from tests.office_fixtures import office_document


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend', default='http://192.168.100.99:8000')
    parser.add_argument('--print', action='store_true', dest='do_print')
    args = parser.parse_args()
    backend = args.backend.rstrip('/')
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(path: str, data: bytes | None = None, content_type: str | None = None) -> bytes:
        headers = {'Content-Type': content_type} if content_type else {}
        with opener.open(urllib.request.Request(backend+path, data=data, headers=headers), timeout=110) as response:
            return response.read()

    def post(path: str, data: dict) -> dict:
        return json.loads(request(path, json.dumps(data).encode(), 'application/json'))

    assert json.loads(request('/capabilities'))['office']['available']
    username = 'office_smoke_' + secrets.token_hex(5)
    post('/auth/signup', {'username': username, 'password': secrets.token_urlsafe(24)})
    try:
        for extension in ('docx', 'xlsx'):
            boundary = secrets.token_hex(16)
            body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="smoke.{extension}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode()
                    + office_document(extension) + f'\r\n--{boundary}--\r\n'.encode())
            info = json.loads(request('/files', body, f'multipart/form-data; boundary={boundary}'))
            assert info['converted'] and info['page_count'] == 1, info
            pdf = request(info['pdf_url'])
            assert hashlib.sha256(pdf).hexdigest() == info['printable_sha256']
            assert 'smoke test' in PdfReader(BytesIO(pdf)).pages[0].extract_text()
            assert request(f"/files/{info['file_id']}/preview/1").startswith(b'\x89PNG\r\n\x1a\n')
            payload = {'file_id': info['file_id'], 'strict_options': True, 'options': {
                'copies': 1, 'pages': '1', 'paper_size': 'A4', 'orientation': 'portrait',
                'color_mode': 'monochrome', 'duplex': 'none', 'quality': 'normal',
            }}
            validation = post('/print/validate', payload)
            assert validation['valid'], validation
            result = {'format': extension, 'user': username, 'file_id': info['file_id'],
                      'pdf_sha256': info['printable_sha256'], 'page_count': info['page_count'],
                      'validated_options': validation['applied_options']}
            if args.do_print:
                assert validation['ready_for_print'], validation
                submitted = post('/print', payload)
                assert submitted['applied_options'] == validation['applied_options']
                result['submission'] = submitted
                print(json.dumps({**result, 'phase': 'submitted'}), flush=True)
                deadline = time.monotonic() + 180
                while time.monotonic() < deadline:
                    job = json.loads(request(f"/jobs/{submitted['job_id']}"))
                    if job['is_terminal']:
                        result['job'] = job
                        assert job['state'] == 'completed', job
                        break
                    time.sleep(3)
                else:
                    raise RuntimeError(f"Job {submitted['job_id']} did not reach a terminal state; do not resubmit blindly")
                if submitted['history_id'] is not None:
                    history = json.loads(request(f"/history/{submitted['history_id']}"))
                    assert history['status'] == 'completed', history
                    result['history_status'] = history['status']
            assert request(info['pdf_url']) == pdf
            print(json.dumps({**result, 'phase': 'verified'}), flush=True)
    finally:
        post('/auth/logout', {})


if __name__ == '__main__':
    main()
