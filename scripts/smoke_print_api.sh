#!/usr/bin/env bash
set -euo pipefail

PRINTER_BACKEND="${PRINTER_BACKEND:-http://ubuntu26-remote.local:8000}"
SMOKE_USERNAME="${SMOKE_USERNAME:-smoke_user}"
SMOKE_PASSWORD="${SMOKE_PASSWORD:-smokepass123}"
DO_PRINT=0
PDF_PATH=""

usage() {
  cat <<EOF
Usage: $0 [--dry-run] [--print] [pdf-path]

Defaults:
  PRINTER_BACKEND=$PRINTER_BACKEND
  SMOKE_USERNAME=$SMOKE_USERNAME  (override with env var)
  SMOKE_PASSWORD=***              (override with env var SMOKE_PASSWORD)

By default this script is a dry run: it checks health/status/options and uploads
a PDF, but it does not submit a print job. Pass --print to submit one safe
one-page monochrome simplex job.

A session is created automatically (signup if the account does not exist yet,
then login) and the cookie is reused for all auth-protected requests.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      DO_PRINT=0
      ;;
    --print)
      DO_PRINT=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      if [[ -n "$PDF_PATH" ]]; then
        echo "Only one PDF path may be provided" >&2
        exit 2
      fi
      PDF_PATH="$1"
      ;;
  esac
  shift
done

require_command() {
  local name="$1"
  if ! command -v "$name" >/dev/null 2>&1; then
    echo "Required command not found: $name" >&2
    exit 1
  fi
}

section() {
  echo
  echo "== $1 =="
}

request() {
  echo "$*"
  "$@"
}

make_pdf() {
  local path="$1"
  if command -v python3 >/dev/null 2>&1; then
    python3 - "$path" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
pdf = b"""%PDF-1.4
1 0 obj
<< /Type /Catalog /Pages 2 0 R >>
endobj
2 0 obj
<< /Type /Pages /Kids [3 0 R] /Count 1 >>
endobj
3 0 obj
<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>
endobj
4 0 obj
<< /Length 66 >>
stream
BT /F1 12 Tf 36 120 Td (Canon MG5350 API smoke test) Tj ET
endstream
endobj
5 0 obj
<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>
endobj
xref
0 6
0000000000 65535 f 
0000000009 00000 n 
0000000058 00000 n 
0000000115 00000 n 
0000000241 00000 n 
0000000357 00000 n 
trailer
<< /Root 1 0 R /Size 6 >>
startxref
427
%%EOF
"""
path.write_bytes(pdf)
PY
  else
    cat > "$path" <<'EOF'
%PDF-1.4
1 0 obj
<< /Type /Catalog /Pages 2 0 R >>
endobj
2 0 obj
<< /Type /Pages /Kids [3 0 R] /Count 1 >>
endobj
3 0 obj
<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >>
endobj
xref
0 4
0000000000 65535 f
trailer
<< /Root 1 0 R /Size 4 >>
startxref
190
%%EOF
EOF
  fi
}

json_value() {
    local key="$1"
  python3 -c "import json,sys; print(json.load(sys.stdin)[sys.argv[1]])" "$key"
}

require_command curl
require_command python3

if [[ -z "$PDF_PATH" ]]; then
  PDF_PATH="/tmp/canon-api-smoke.pdf"
  make_pdf "$PDF_PATH"
fi

if [[ ! -f "$PDF_PATH" ]]; then
  echo "PDF not found: $PDF_PATH" >&2
  exit 1
fi

COOKIE_JAR="$(mktemp /tmp/smoke-cookie-XXXXXX.txt)"
trap 'rm -f "$COOKIE_JAR"' EXIT

section "Backend"
echo "PRINTER_BACKEND=$PRINTER_BACKEND"
echo "PDF_PATH=$PDF_PATH"
echo "Mode=$([[ "$DO_PRINT" == "1" ]] && echo print || echo dry-run)"

section "Health"
request curl -4 -sS "$PRINTER_BACKEND/health"
echo

section "Status"
request curl -4 -sS "$PRINTER_BACKEND/status"
echo

section "Options"
request curl -4 -sS "$PRINTER_BACKEND/options"
echo

section "Auth"
auth_payload="{\"username\":\"${SMOKE_USERNAME}\",\"password\":\"${SMOKE_PASSWORD}\"}"

echo "Signing up (ignored if account already exists)..."
curl -4 -sS -o /dev/null -w "signup status: %{http_code}\n" \
  -X POST "$PRINTER_BACKEND/auth/signup" \
  -H "content-type: application/json" \
  -d "$auth_payload" \
  -c "$COOKIE_JAR"

echo "Logging in..."
login_response="$(curl -4 -sS \
  -X POST "$PRINTER_BACKEND/auth/login" \
  -H "content-type: application/json" \
  -d "$auth_payload" \
  -c "$COOKIE_JAR" -b "$COOKIE_JAR")"
echo "$login_response"
if ! printf '%s' "$login_response" | python3 -c "import json,sys; d=json.load(sys.stdin); assert 'user' in d" 2>/dev/null; then
  echo "Login failed — check SMOKE_USERNAME / SMOKE_PASSWORD" >&2
  exit 1
fi
echo "Session established."

section "Upload"
upload_response="$(curl -4 -sS \
  -X POST "$PRINTER_BACKEND/files" \
  -b "$COOKIE_JAR" \
  -F "file=@${PDF_PATH};type=application/pdf")"
echo "$upload_response"
file_id="$(printf '%s' "$upload_response" | json_value "file_id")"
echo "Uploaded file_id=$file_id"

if [[ "$DO_PRINT" != "1" ]]; then
  echo
  echo "Dry run complete. Pass --print to submit a real print job."
  exit 0
fi

section "Print"
print_response="$(curl -4 -sS \
  -X POST "$PRINTER_BACKEND/print" \
  -b "$COOKIE_JAR" \
  -H "content-type: application/json" \
  -d "{
    \"file_id\": \"${file_id}\",
    \"options\": {
      \"copies\": 1,
      \"pages\": \"1\",
      \"paper_size\": \"A4\",
      \"orientation\": \"portrait\",
      \"color_mode\": \"monochrome\",
      \"duplex\": \"none\",
      \"quality\": \"normal\"
    }
  }")"
echo "$print_response"
job_id="$(printf '%s' "$print_response" | json_value "job_id")"
echo "Submitted job_id=$job_id"

section "Job"
request curl -4 -sS -b "$COOKIE_JAR" "$PRINTER_BACKEND/jobs/$job_id"
echo
