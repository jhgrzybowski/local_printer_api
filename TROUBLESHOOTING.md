# Troubleshooting

This document explains how to debug the backend, session authentication, CUPS, Gutenprint, LPD transport, printer queue, and LAN access.

---

## Quick diagnostic flow

From the server:

```bash
python3 scripts/probe.py
scripts/debug_printer_lpd.sh
lpstat -p Canon_MG5350 -l
lpstat -v Canon_MG5350
lpoptions -p Canon_MG5350 -l
sudo journalctl -u cups --no-pager -n 100
```

From a LAN client:

```bash
export PRINTER_BACKEND="http://ubuntu26-remote.local:8000"
curl -4 -i "$PRINTER_BACKEND/health"
curl -4 -s "$PRINTER_BACKEND/status" | jq
curl -4 -s "$PRINTER_BACKEND/options" | jq
```

For Docker deployments, also check the host CUPS socket:

```bash
test -S /run/cups/cups.sock && echo /run/cups/cups.sock
test -S /var/run/cups/cups.sock && echo /var/run/cups/cups.sock
docker compose exec api ls -l /run/cups/cups.sock
docker compose exec api python -c "import cups; print('cups ok')"
```

---

## Protected endpoint returns 401

`/health`, `/status`, and `/options` work without a session. Upload, preview,
print, jobs, preferences, history, and `/auth/me` require the
`local_printer_session` cookie. A 401 from those routes means the cookie is
missing, expired, or invalid; it does not by itself indicate a CUPS problem.

On a LAN client, use the same backend hostname for login and later requests:

```bash
export PRINTER_BACKEND="http://192.168.100.99:8000"
curl -i -c cookies.txt -X POST "$PRINTER_BACKEND/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"username":"YOUR_USER","password":"YOUR_PASSWORD"}'
curl -i -b cookies.txt "$PRINTER_BACKEND/auth/me"
curl -i -b cookies.txt "$PRINTER_BACKEND/jobs"
```

If login succeeds but `/auth/me` returns 401, check that the login response
includes `Set-Cookie`, that the cookie jar contains `local_printer_session`,
and that subsequent requests use `-b cookies.txt`. The cookie is scoped to
the backend hostname, so switching between an IP and a `.local` name can
leave it out. Log in again when the session expires or after logout.

For a browser frontend, send credentialed requests and use an origin allowed
by `CORS_ALLOWED_ORIGINS`. With the default `SameSite=Lax` cookie over HTTP,
the frontend and API must be same-site; accessing Vite through `localhost`
while calling the API at `192.168.100.99` will not send the session cookie.
Use the LAN hostname or IP for the frontend as described in `ENVIRONMENT.md`.
If `SESSION_COOKIE_SECURE=true`, use HTTPS so the browser can send the cookie.

Accounts can be created through `/auth/signup` on the LAN. Keep the service
bound to the trusted LAN; do not expose it to the internet.

---

## Uploaded file or history entry disappeared

The API removes uploads and their previews after `UPLOAD_TTL_DAYS` (7 by
default) and print history after `HISTORY_TTL_DAYS` (90 by default). Maintenance
runs hourly in the API process. Active CUPS jobs and files currently in use
are protected; if CUPS is unavailable, cleanup waits for the next interval.

`GET /history` returns only the newest 50 entries by default. Check
`total`, `limit`, and `offset`, or request another page:

```bash
curl -i -b cookies.txt "$PRINTER_BACKEND/history?limit=50&offset=50"
```

An expired history record cannot be recovered from the API, and its CUPS job
may no longer appear in the user's API job view even if CUPS retained it.
Increase the retention settings before expiry if longer access is needed.
CUPS job retention is configured separately in `cupsd.conf`.

---

## Backend not reachable from another machine

### Check Uvicorn bind address

On the server:

```bash
ss -ltnp | grep 8000
```

Good for LAN access:

```text
0.0.0.0:8000
```

Only local to the server:

```text
127.0.0.1:8000
```

Fix:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

---

### mDNS `.local` gives mixed results

If this happens:

```bash
nc -vz ubuntu26-remote.local 8000
```

and you see a first failure followed by success, the hostname may resolve to multiple addresses, usually IPv6 and IPv4.

Use IPv4 explicitly:

```bash
curl -4 -i http://ubuntu26-remote.local:8000/health
nc -4 -vz ubuntu26-remote.local 8000
```

Or use the direct IP:

```bash
curl -i http://192.168.100.99:8000/health
```

---

## CUPS service problems

Check service status:

```bash
systemctl status cups --no-pager
```

Start/enable CUPS:

```bash
sudo systemctl enable --now cups
```

Recent logs:

```bash
sudo journalctl -u cups --no-pager -n 100
```

Check available CUPS commands:

```bash
command -v lpadmin
command -v lpstat
command -v lpoptions
command -v lpinfo
command -v lp
command -v cancel
```

---

## Python cannot import `cups`

Symptom:

```bash
python -c "import cups; print('cups ok')"
```

fails.

Fix:

```bash
sudo apt install -y python3-cups
rm -rf .venv
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
python -c "import cups; print('cups ok')"
```

Do not fix this by adding `pycups` to `requirements.txt`.

---

## `pycups` pip build fails

Observed failure:

```text
fatal error: Python.h: No such file or directory
Failed building wheel for pycups
```

Project decision:

```text
Use apt package python3-cups instead.
Do not install pycups from pip.
```

Correct setup:

```bash
sudo apt install -y python3-cups
python3 -m venv --system-site-packages .venv
```

---

## CUPS queue missing

Check:

```bash
lpstat -p Canon_MG5350 -l
```

If missing, run:

```bash
sudo scripts/setup_printer.sh
```

Or force repair:

```bash
sudo scripts/setup_printer.sh --force
```

Expected queue URI:

```bash
lpstat -v Canon_MG5350
```

Expected:

```text
device for Canon_MG5350: lpd://192.168.100.100/PASSTHRU
```

---

## Driverless IPP setup fails

Symptom:

```text
lpadmin: Unable to create PPD: Printer does not support required IPP attributes or document formats.
```

This is expected for the tested Canon MG5350 setup.

Do not default to:

```text
MODEL=everywhere
ipp://192.168.100.100/ipp/print
```

Use:

```text
gutenprint.5.3://bjc-PIXMA-MG5350/expert
lpd://192.168.100.100/PASSTHRU
```

---

## Gutenprint driver not found

Check:

```bash
lpinfo -m | grep -Ei 'MG5350|MG5300|gutenprint'
```

If empty:

```bash
sudo apt update
sudo apt install -y printer-driver-gutenprint
```

Expected useful model:

```text
gutenprint.5.3://bjc-PIXMA-MG5350/expert
```

---

## Printer reachability

Ping:

```bash
ping -c 3 192.168.100.100
```

Check relevant ports:

```bash
nc -vz 192.168.100.100 80
nc -vz 192.168.100.100 515
nc -vz 192.168.100.100 631
nc -vz 192.168.100.100 9100
```

For the verified setup:

|   Port | Expected                              |
| -----: | ------------------------------------- |
|   `80` | open                                  |
|  `515` | open                                  |
|  `631` | may be open but not driverless-usable |
| `9100` | refused / not used                    |

---

## LPD transport debugging

Verified working URI:

```text
lpd://192.168.100.100/PASSTHRU
```

Fallback URIs to test only if needed:

```text
lpd://192.168.100.100/lp
lpd://192.168.100.100/print
```

Recreate queue manually with exact model:

```bash
sudo lpadmin -x Canon_MG5350 2>/dev/null || true

sudo lpadmin -p Canon_MG5350 \
  -E \
  -v "lpd://192.168.100.100/PASSTHRU" \
  -m "gutenprint.5.3://bjc-PIXMA-MG5350/expert"

sudo cupsenable Canon_MG5350
sudo cupsaccept Canon_MG5350
```

Direct tiny print test:

```bash
cat > /tmp/canon-test.txt <<'EOF'
Canon MG5350 CUPS/Gutenprint test
Queue: Canon_MG5350
Transport: LPD PASSTHRU
EOF

lp -d Canon_MG5350 /tmp/canon-test.txt
```

Watch jobs:

```bash
lpstat -o Canon_MG5350
lpstat -W not-completed -o Canon_MG5350
lpstat -W completed -o Canon_MG5350
lpstat -W all -o Canon_MG5350
```

`lpstat -o` and `lpstat -W not-completed -o` show active/not-completed jobs.
Completed, canceled, or aborted jobs can remain in CUPS history and appear with
`lpstat -W completed -o` or API calls such as `/jobs?scope=completed`. Those
historical jobs are not active queue work and CUPS can reject normal
cancellation with messages such as `already completed - can't cancel`.

Cancel an active job:

```bash
cancel Canon_MG5350-2
```

Purge one historical job record, if the current CUPS permissions allow it:

```bash
cancel -x Canon_MG5350-2
```

CUPS history retention is controlled by CUPS configuration such as
`PreserveJobHistory`, `PreserveJobFiles`, and `MaxJobs`. The API does not edit
`cupsd.conf`; adjust those settings manually on the server only if that is the
desired operations policy.

---

## Print job submitted but nothing prints

Check queue state:

```bash
lpstat -p Canon_MG5350 -l
lpstat -v Canon_MG5350
lpstat -W all -o Canon_MG5350
```

Check logs:

```bash
sudo journalctl -u cups --no-pager -n 100
```

Check printer:

* powered on,
* connected to Wi-Fi,
* paper loaded,
* no paper jam,
* no cover/lid error,
* no visible panel error.

Confirm device URI:

```bash
lpstat -v Canon_MG5350
```

Expected:

```text
device for Canon_MG5350: lpd://192.168.100.100/PASSTHRU
```

---

## `/status` says queue is missing

Check CUPS:

```bash
lpstat -p Canon_MG5350 -l
```

If missing:

```bash
sudo scripts/setup_printer.sh
```

Then re-test API:

```bash
curl -4 -s "$PRINTER_BACKEND/status" | jq
```

---

## `/options` is empty or incomplete

Check raw CUPS options:

```bash
lpoptions -p Canon_MG5350 -l
```

If this fails, the queue may be missing or misconfigured.

If it works, compare with:

```bash
curl -4 -s "$PRINTER_BACKEND/options?debug=true" | jq
```

The API should derive frontend-friendly choices from detected CUPS/PPD options.

---

## Print option warnings

Warnings are not necessarily errors.

Examples of expected warnings:

* option mapped through Gutenprint-specific PPD value,
* option dropped because not exposed by the queue,
* `collate` ignored when `copies=1`,
* `fit_to_page` unsupported if no scaling option is detected.

Warnings become important when:

* the physical output does not match expectation,
* `copies > 1`,
* landscape orientation is required,
* photo/glossy media is required,
* fit-to-page behavior is required.

Use `/options?debug=true` and `lpoptions -p Canon_MG5350 -l` to inspect real support.

---

## Page range problems

Supported examples:

```text
1
1,3,5
1-3
1,3-5,8
```

For PDFs:

* page ranges are applied by creating a temporary filtered PDF,
* page order should be preserved.

For images:

* only page `1` is valid,
* any other page range should fail.

Invalid examples:

```text
0
-1
5-3
abc
99 in a 3-page file
```

---

## Preview fails

Check Poppler:

```bash
pdftoppm -h | head
```

If missing:

```bash
sudo apt install -y poppler-utils
```

Preview limitations:

* preview is approximate,
* it does not guarantee exact driver-level output,
* margins/scaling may differ from the physical print.

---

## Upload returns unsupported MIME

Check file type:

```bash
file test.pdf
```

Supported:

```text
application/pdf
image/png
image/jpeg
text/plain
```

Unsupported files should return `415`.

Oversized files should return `413`.

---

## Smoke test script

Dry-run:

```bash
PRINTER_BACKEND=http://ubuntu26-remote.local:8000 \
  scripts/smoke_print_api.sh --dry-run
```

Real print:

```bash
PRINTER_BACKEND=http://ubuntu26-remote.local:8000 \
  scripts/smoke_print_api.sh --print
```

Dry-run should not submit a print job.

---

## Full validation checklist

```bash
python -m py_compile $(find app scripts tests -name '*.py')
python -m pytest -q
bash -n scripts/*.sh
python3 scripts/probe.py
scripts/debug_printer_lpd.sh
curl -4 -s "$PRINTER_BACKEND/health" | jq
curl -4 -s "$PRINTER_BACKEND/status" | jq
curl -4 -s "$PRINTER_BACKEND/options" | jq
```

Only after this:

```bash
PRINTER_BACKEND=http://ubuntu26-remote.local:8000 \
  scripts/smoke_print_api.sh --print
```
