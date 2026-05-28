# Docker Deployment

This deployment keeps the FastAPI API in a container while the host CUPS daemon
remains the source of truth for queues, capabilities, jobs, and cancellations.

The container does not run its own CUPS daemon. It uses the host CUPS Unix socket.

---

## Host assumptions

Before starting the container, the Ubuntu host must already have a working CUPS
queue.

Verified Canon PIXMA MG5350 setup:

```text
Queue: Canon_MG5350
Device URI: lpd://192.168.100.100/PASSTHRU
Driver/model: gutenprint.5.3://bjc-PIXMA-MG5350/expert
```

Do not default this printer to IPP Everywhere. In the verified environment, IPP
Everywhere failed to provide the required CUPS attributes/formats. Use
Gutenprint with the LPD `PASSTHRU` URI for this printer.

Check the host queue:

```bash
lpstat -p Canon_MG5350 -l
lpstat -v Canon_MG5350
lpoptions -p Canon_MG5350 -l
```

Expected URI:

```text
device for Canon_MG5350: lpd://192.168.100.100/PASSTHRU
```

---

## Verify the CUPS socket path

On Ubuntu, the socket is usually under `/run/cups/cups.sock`. Some tools display
it through `/var/run/cups/cups.sock`, which is commonly a symlink to `/run`.

```bash
test -S /run/cups/cups.sock && echo /run/cups/cups.sock
test -S /var/run/cups/cups.sock && echo /var/run/cups/cups.sock
ls -l /run/cups/cups.sock /var/run/cups/cups.sock 2>/dev/null || true
```

The default `docker-compose.yml` mounts:

```yaml
/run/cups/cups.sock:/run/cups/cups.sock
```

If your host only has a different socket path, update the left side of that
volume mount.

---

## Build the image

```bash
docker build -t local-printer-api:latest .
```

Or use the Make target:

```bash
make docker-build
```

The image uses a Debian base and installs `python3-cups` from apt. Do not add
`pycups` to `requirements.txt`; the `cups` module comes from the OS package.

---

## Run with Docker Compose

```bash
docker compose up -d --build
```

By default, Compose binds the published API port to the documented LAN address:

```text
192.168.100.99:8000:8000
```

Set `BACKEND_HOST` before running Compose if your server uses a different LAN
IP. For a local-only reviewer smoke test, use `127.0.0.1`.

```bash
BACKEND_HOST=127.0.0.1 docker compose up -d --build
```

The compose file:

- exposes `${BACKEND_HOST:-192.168.100.99}:8000:8000`,
- mounts the host CUPS socket,
- stores uploads, filtered PDFs, and previews in the `printer-backend-tmp` named
  volume,
- stores SQLite users, sessions, preferences, and print history in the
  `printer-backend-db` named volume at `/var/lib/local-printer-api/app.db`,
- sets explicit environment variables for the printer queue, host metadata,
  temporary directory, upload limit, and preview DPI.

Current defaults:

```env
QUEUE_NAME=Canon_MG5350
PRINTER_IP=192.168.100.100
BACKEND_HOST=192.168.100.99
BACKEND_PORT=8000
TMP_DIR=/var/tmp/printer-backend
DB_PATH=/var/lib/local-printer-api/app.db
MAX_UPLOAD_MB=50
PREVIEW_DPI=110
CORS_ALLOWED_ORIGINS=http://192.168.100.99,http://192.168.100.99:80,http://ubuntu26-remote.local,http://ubuntu26-remote.local:80,http://drukarka.local,http://drukarka.local:80,http://192.168.100.99:5173,http://ubuntu26-remote.local:5173,http://localhost:5173,http://127.0.0.1:5173
```

Browser clients are allowed only when their exact origin is listed in
`CORS_ALLOWED_ORIGINS`; wildcard CORS is not enabled by default. For the Print
Bar frontend on the same Ubuntu server, set:

```env
VITE_PRINTER_API_BASE_URL=http://192.168.100.99:8000
```

The service uses open LAN signup plus HttpOnly cookie sessions. Keep it
reachable only on the trusted LAN address. Do not bind it to `0.0.0.0` or
publish it to the internet.

---

## Verify the running API

From the Docker host:

```bash
export PRINTER_BACKEND="http://${BACKEND_HOST:-192.168.100.99}:8000"
curl -i "$PRINTER_BACKEND/health"
curl -s "$PRINTER_BACKEND/status"
curl -s "$PRINTER_BACKEND/options"
```

Verify CORS for the Print Bar frontend origin:

```bash
curl -i \
  -H "Origin: http://192.168.100.99:5173" \
  "$PRINTER_BACKEND/health"
```

Expected `/health` response:

```json
{"status":"ok","service":"local-printer-api"}
```

The response headers should include:

```text
access-control-allow-origin: http://192.168.100.99:5173
```

Dry-run smoke test:

```bash
scripts/smoke_print_api.sh --dry-run
```

That checks health, status, options, and upload without submitting a print job.
Use `--print` only when you intentionally want a one-page test print.

Protected endpoints require a session cookie:

```bash
curl -c cookies.txt -s -X POST "$PRINTER_BACKEND/auth/signup" \
  -H "Content-Type: application/json" \
  -d '{"username":"alice","password":"correct horse battery staple"}' | jq

curl -b cookies.txt -s "$PRINTER_BACKEND/jobs" | jq
curl -b cookies.txt -s "$PRINTER_BACKEND/me/preferences" | jq
curl -b cookies.txt -s "$PRINTER_BACKEND/history" | jq
```

---

## Troubleshooting container CUPS access

Check that the socket is mounted inside the container:

```bash
docker compose exec api ls -l /run/cups/cups.sock
```

Check that Python can import the OS CUPS bindings:

```bash
docker compose exec api python -c "import cups; print('cups ok')"
```

Run the existing probe script on the host for deeper queue diagnostics:

```bash
python3 scripts/probe.py
```

If `/status`, `/options`, or `/jobs` fail in the container but work on the host,
first verify the socket mount path and permissions. Keep CUPS errors visible;
they are diagnostics from the source-of-truth print system.

---

## Future CUPS-in-container mode

A future deployment could add a separate `cups` service or a dedicated image that
runs CUPS in a container. That is intentionally not part of the default mode
because it adds another daemon, queue configuration, device-driver state, and
failure surface. The current default keeps one CUPS authority: the Ubuntu host.
