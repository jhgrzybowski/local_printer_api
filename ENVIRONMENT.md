# Environment and Setup: Canon PIXMA MG5350 LAN Backend

This document captures the verified local environment used for this project. It is intentionally specific to the current server, network, printer, CUPS queue, and driver setup.

For generic installation and API usage, see `README.md`.

---

## Hardware and network

### Server

| Item | Value |
|---|---|
| OS | Ubuntu 26 server |
| Hostname | `ubuntu26-remote` |
| mDNS hostname | `ubuntu26-remote.local` |
| LAN IP | `192.168.100.99` |
| Backend port | `8000` |
| Backend framework | FastAPI + Uvicorn |
| Print system | Local CUPS daemon |

The backend should usually be run with:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

The Docker deployment runs the same FastAPI app in a container and mounts the
host CUPS socket. Host CUPS remains the queue/job source of truth.

LAN clients can reach it through:

```text
http://ubuntu26-remote.local:8000
http://192.168.100.99:8000
```

When testing from macOS, prefer IPv4 if mDNS resolves both IPv6 and IPv4:

```bash
curl -4 -i http://ubuntu26-remote.local:8000/health
```

---

### Printer

| Item                      | Value                                      |
| ------------------------- | ------------------------------------------ |
| Model                     | Canon PIXMA MG5350                         |
| Web UI name               | Canon MG5300 series                        |
| Firmware version observed | `2.030`                                    |
| Printer IP                | `192.168.100.100`                          |
| Wireless                  | Yes                                        |
| Verified CUPS queue       | `Canon_MG5350`                             |
| Verified device URI       | `lpd://192.168.100.100/PASSTHRU`           |
| Verified driver/model     | `gutenprint.5.3://bjc-PIXMA-MG5350/expert` |

Observed/relevant printer ports:

|        Port | Meaning                   | Status / note                                        |
| ----------: | ------------------------- | ---------------------------------------------------- |
|    `80/tcp` | Canon embedded HTTP UI    | Open                                                 |
|   `515/tcp` | LPD                       | Open and verified working                            |
|   `631/tcp` | IPP-ish/CUPS-like service | Open, but not usable as IPP Everywhere in this setup |
|  `9100/tcp` | JetDirect/raw socket      | Refused in testing                                   |
| `8611/8612` | Canon BJNP-related ports  | Refused in testing                                   |
|   `161/udp` | SNMP                      | Not available / refused in testing                   |

---

## Important finding: IPP Everywhere does not work here

The printer is reachable on port `631`, but driverless IPP setup failed.

Failed setup shape:

```bash
DEVICE_URI=ipp://192.168.100.100/ipp/print
MODEL=everywhere
```

Observed failure:

```text
lpadmin: Unable to create PPD: Printer does not support required IPP attributes or document formats.
```

Direct driverless probing also failed to obtain enough capability information.

Conclusion:

```text
Do not default this printer to IPP Everywhere / MODEL=everywhere.
Use Gutenprint + LPD instead.
```

---

## Verified working CUPS setup

The working combination is:

```text
Queue: Canon_MG5350
Device URI: lpd://192.168.100.100/PASSTHRU
Driver/model: gutenprint.5.3://bjc-PIXMA-MG5350/expert
```

Direct CUPS text print succeeded with:

```bash
cat > /tmp/canon-test.txt <<'EOF'
Canon MG5350 CUPS/Gutenprint test
Queue: Canon_MG5350
Transport: LPD PASSTHRU
EOF

lp -d Canon_MG5350 /tmp/canon-test.txt
```

Confirmed queue URI:

```bash
lpstat -v Canon_MG5350
```

Expected:

```text
device for Canon_MG5350: lpd://192.168.100.100/PASSTHRU
```

Completed/known job example:

```text
Canon_MG5350-1 jgrzybo 1024 Sun 10 May 2026 05:10:43 PM UTC
```

---

## System packages

Install:

```bash
sudo apt update
sudo apt install -y \
  cups \
  cups-client \
  python3-cups \
  poppler-utils \
  printer-driver-gutenprint
```

Important package decisions:

* Use `python3-cups` from apt.
* Do not install `pycups` from pip.
* Use venv with `--system-site-packages`.

---

## Python environment

Create venv:

```bash
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
```

Verify CUPS bindings:

```bash
python -c "import cups; print('cups ok')"
```

Why this matters:

* `cups` Python module is installed system-wide by `python3-cups`.
* A normal venv without `--system-site-packages` may not see it.
* Installing `pycups` through pip failed due to native build requirements and missing Python headers.

Observed pip failure:

```text
fatal error: Python.h: No such file or directory
Failed building wheel for pycups
```

Project decision:

```text
Do not add pycups to requirements.txt.
```

---

## Gutenprint model discovery

After installing Gutenprint:

```bash
lpinfo -m | grep -Ei 'MG5350|MG5300'
```

Observed available models:

```text
gutenprint.5.3://bjc-MG5300-series/expert Canon MG5300 series - CUPS+Gutenprint v5.3.4
gutenprint.5.3://bjc-PIXMA-MG5300/expert Canon PIXMA MG5300 - CUPS+Gutenprint v5.3.4
gutenprint.5.3://bjc-PIXMA-MG5350/expert Canon PIXMA MG5350 - CUPS+Gutenprint v5.3.4
```

Preferred model:

```text
gutenprint.5.3://bjc-PIXMA-MG5350/expert
```

Avoid auto-selecting the generic first result if the exact MG5350 entry exists.

Recommended discovery order:

1. `gutenprint.5.3://bjc-PIXMA-MG5350/expert`
2. `gutenprint.5.3://bjc-PIXMA-MG5300/expert`
3. `gutenprint.5.3://bjc-MG5300-series/expert`

---

## Setup script behavior

The setup script should default to:

```bash
QUEUE_NAME="${QUEUE_NAME:-Canon_MG5350}"
PRINTER_IP="${PRINTER_IP:-192.168.100.100}"
DEVICE_URI="${DEVICE_URI:-lpd://192.168.100.100/PASSTHRU}"
MODEL="${MODEL:-gutenprint.5.3://bjc-PIXMA-MG5350/expert}"
```

Expected setup command:

```bash
sudo lpadmin -p Canon_MG5350 \
  -E \
  -v "lpd://192.168.100.100/PASSTHRU" \
  -m "gutenprint.5.3://bjc-PIXMA-MG5350/expert"

sudo cupsenable Canon_MG5350
sudo cupsaccept Canon_MG5350
```

The script should be idempotent:

* If the queue exists and matches URI/model, exit successfully.
* If the queue exists but does not match, do not overwrite silently.
* Use `--force` or `FORCE=1` for reconfiguration.
* `--test` may submit a tiny text print.

---

## Backend runtime

Recommended run command:

```bash
source .venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

From macOS:

```bash
export PRINTER_BACKEND="http://ubuntu26-remote.local:8000"
curl -4 -i "$PRINTER_BACKEND/health"
```

Known working `/health` response:

```json
{
  "status": "ok",
  "service": "local-printer-api"
}
```

If `.local` causes mixed IPv6/IPv4 behavior, use:

```bash
export PRINTER_BACKEND="http://192.168.100.99:8000"
```

---

## First verified API print path

A real API print succeeded through:

```text
macOS curl
→ FastAPI backend
→ local CUPS queue Canon_MG5350
→ Gutenprint MG5350 driver
→ LPD transport lpd://192.168.100.100/PASSTHRU
→ Canon PIXMA MG5350
```

Example effective options for a one-page monochrome simplex A4 print:

```json
{
  "copies": "1",
  "Duplex": "None",
  "PageSize": "A4",
  "ColorModel": "Gray",
  "Resolution": "600dpi"
}
```

Unsupported or partially supported high-level options should be reported diagnostically, not treated as fatal unless they are required for correctness.

---

## Codex / agent environment notes

Codex sandbox worked on this Ubuntu 26 server.

Known diagnostics:

```bash
command -v bwrap
# /usr/bin/bwrap

/usr/bin/bwrap --version
# bubblewrap 0.11.1

codex --version
# codex-cli 0.129.0

codex sandbox linux /usr/bin/id
# succeeds
```

Kernel/AppArmor state during setup:

```text
kernel.unprivileged_userns_clone = 1
kernel.apparmor_restrict_unprivileged_userns = 0
```

Recommended Codex permissions for this project:

```bash
codex --sandbox workspace-write --ask-for-approval on-request
```

Avoid `danger-full-access` except for short, explicit system-setup tasks.

---

## Container settings

The API reads these environment variables and preserves the documented defaults:

```text
QUEUE_NAME=Canon_MG5350
PRINTER_IP=192.168.100.100
BACKEND_HOST=192.168.100.99
BACKEND_PORT=8000
TMP_DIR=/var/tmp/printer-backend
DB_PATH=/var/tmp/printer-backend/app.db
MAX_UPLOAD_MB=50
PREVIEW_DPI=110
CORS_ALLOWED_ORIGINS=http://192.168.100.99:8000,http://ubuntu26-remote.local:8000,http://drukarka.local:8000,http://192.168.100.99:5173,...
```

`DB_PATH` controls where SQLite stores the auth/history database.  The
default (`/var/tmp/printer-backend/app.db`) is inside the same writable temp
directory used for uploaded files so no extra directory creation is needed for
plain `uvicorn` runs.  Set a custom path when you want the database outside of
`/var/tmp` (e.g. for persistence across reboots).

### CORS Security Model

**Why CORS addresses are safe in this public repository:**

- **Private network range:** `192.168.100.0/24` is RFC 1918 (non-routable private IP), not accessible from the internet
- **mDNS hostnames:** `.local` domains resolve only within the local network via multicast DNS
- **Public nature of CORS:** CORS policy is intentionally public; it's revealed in browser preflight OPTIONS requests
- **No credentials exposed:** CORS allowlist contains no authentication tokens, API keys, or secrets

**Customization for different deployments:**

Override `CORS_ALLOWED_ORIGINS` via environment variable (comma-separated list):

```bash
# Docker Compose
export CORS_ALLOWED_ORIGINS="http://my-api:8000,http://my-frontend:5173"

# Plain environment
export CORS_ALLOWED_ORIGINS="http://different-ip:8000"
```

**Default policy includes:**

- Backend API: `192.168.100.99:8000`, `ubuntu26-remote.local:8000`, `drukarka.local:8000`
- Vite dev ports (5173-5175) on all configured hosts
- Production ports (80, 443) on `drukarka.local` (with and without explicit port)
- Localhost for local development

See `app/settings.py` for the complete allowlist.

`CORS_ALLOWED_ORIGINS` is a comma-separated list of browser frontend origins.
The Print Bar frontend should use
`VITE_PRINTER_API_BASE_URL=http://192.168.100.99:8000` or the appropriate hostname.

### Cross-site dev origins and session cookies

The session cookie is set with `SameSite=Lax`.  On plain HTTP, a browser
considers two URLs cross-site when their registered host differs — so
`http://localhost:5173` (frontend) and `http://192.168.100.99:8000` (backend)
are cross-site even though both are on the LAN.  The browser will **not** send
the session cookie on cross-site XHR/fetch requests, which means every
auth-protected endpoint returns `401` even though a CORS preflight would
succeed.

`localhost` and `127.0.0.1` origins are therefore **excluded from the default
CORS allowlist** to prevent this confusing partial-works situation.

**How to develop across machines on the LAN:**

Access the Vite dev server through the same registered host as the backend:

```bash
# On the backend server, start the frontend dev server bound to the LAN IP:
npm run dev -- --host 0.0.0.0

# Then open the browser at the LAN IP, not localhost:
http://192.168.100.99:5173
```

Both the frontend and the API share the registered host `192.168.100.99`, so
cookies are sent same-site and authentication works.

Alternatively, add a same-origin reverse proxy (e.g. nginx) in front of both
services at a single origin.

For Docker Compose, the verified default is to mount the host CUPS socket:

```text
/run/cups/cups.sock:/run/cups/cups.sock
```

The Compose port mapping binds to the documented LAN host instead of all
interfaces:

```text
${BACKEND_HOST:-192.168.100.99}:8000:8000
```

See `DEPLOYMENT_DOCKER.md` for the full container workflow.
