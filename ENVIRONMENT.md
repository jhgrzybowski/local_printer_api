# Verified environment: Canon PIXMA MG5350 LAN backend

This is a record of the observed Ubuntu host, printer, and CUPS setup. For
installation and API usage, see [README.md](README.md). For container deployment,
see [DEPLOYMENT_DOCKER.md](DEPLOYMENT_DOCKER.md); for failure diagnostics, see
[TROUBLESHOOTING.md](TROUBLESHOOTING.md).

## Host and network

| Item | Observed value |
| --- | --- |
| Server OS | Ubuntu 26.04 Server |
| Hostname | `ubuntu26-remote` |
| mDNS hostname | `ubuntu26-remote.local` |
| LAN address | `192.168.100.99` |
| API port | `8000` |
| Print system | Host CUPS daemon |

The API was reached from the LAN at
`http://ubuntu26-remote.local:8000` and
`http://192.168.100.99:8000`. On macOS, `curl -4` helped when mDNS
resolved both IPv6 and IPv4 addresses.

## Printer and CUPS observations

| Item | Observed value |
| --- | --- |
| Printer | Canon PIXMA MG5350 |
| Embedded web UI name | Canon MG5300 series |
| Firmware version | `2.030` |
| Printer LAN address | `192.168.100.100` |
| Connection | Wireless |
| Working CUPS queue | `Canon_MG5350` |
| Working device URI | `lpd://192.168.100.100/PASSTHRU` |
| Working driver/model | `gutenprint.5.3://bjc-PIXMA-MG5350/expert` |

Observed printer ports during setup:

| Port | Service | Observation |
| ---: | --- | --- |
| `80/tcp` | Embedded HTTP UI | Open |
| `515/tcp` | LPD | Open; print tested |
| `631/tcp` | IPP-like service | Open; IPP Everywhere setup failed |
| `9100/tcp` | Raw socket | Refused |
| `8611/8612` | Canon BJNP-related | Refused |
| `161/udp` | SNMP | Unavailable in testing |

Although port 631 was open, driverless IPP setup failed because the printer
did not provide the attributes or document formats CUPS required. The
Gutenprint MG5350 driver with the LPD `PASSTHRU` URI printed successfully.
Other discovered Gutenprint models were MG5300 and MG5300-series fallbacks;
the exact MG5350 model above was the working choice.

Both a direct CUPS text print and a print submitted through the API succeeded.
The API print used one monochrome, simplex A4 page with effective options
`copies=1`, `Duplex=None`, `PageSize=A4`, `ColorModel=Gray`, and
`Resolution=600dpi`. These are setup observations, not a guarantee that
every option or transport will work in another environment.

Use [README.md](README.md#basic-installation) for packages, Python bindings,
and queue setup with `scripts/setup_printer.sh`. See
[TROUBLESHOOTING.md](TROUBLESHOOTING.md#driverless-ipp-setup-fails) for the
IPP failure and queue repair commands.

## Runtime configuration

The host CUPS daemon owns the queue and jobs. Docker Compose mounts its
socket into the API container and publishes the container's port `8000` on
the LAN address selected by `BACKEND_HOST` and `BACKEND_PORT`. Those two
variables are Compose settings, not API settings. A direct Uvicorn run sets
its listen address through `--host` and `--port`.

The API reads its printer, temporary storage, database, upload, preview, and
CORS settings from [app/settings.py](app/settings.py). Compose supplies
deployment-specific values in [docker-compose.yml](docker-compose.yml),
including a persistent database volume at
`/var/lib/local-printer-api/app.db`. The API's `DB_PATH` default now uses
that same durable location. For a host install, create the directory and
give the API account write access before starting the service. If an
existing database is under the old `/var/tmp/printer-backend/app.db` path,
move it while the API is stopped or set `DB_PATH` to another durable path.
Ubuntu may remove idle files under `/var/tmp`. Follow
[DEPLOYMENT_DOCKER.md](DEPLOYMENT_DOCKER.md#run-with-docker-compose) for the
current container configuration and verification steps.

### CORS Security Model

**Why CORS addresses are safe in this public repository:**

- **Private network range:** `192.168.100.0/24` is RFC 1918 (non-routable private IP), not accessible from the internet
- **mDNS hostnames:** `.local` domains resolve only within the local network via multicast DNS
- **Public nature of CORS:** CORS policy is intentionally public; it's revealed in browser preflight OPTIONS requests
- **No credentials exposed:** CORS allowlist contains no authentication tokens, API keys, or secrets

**Customization for different deployments:**

Override `CORS_ALLOWED_ORIGINS` via environment variable (comma-separated list).
An override replaces the complete default list, so include every browser origin
your deployment needs:

```bash
# Docker Compose
export CORS_ALLOWED_ORIGINS="http://192.168.1.10:8000,http://192.168.1.10:5173"

# Plain environment
export CORS_ALLOWED_ORIGINS="http://192.168.1.10:5173"
```

The authoritative default is `DEFAULT_CORS_ALLOWED_ORIGINS` in
`app/settings.py`; leave the variable unset or empty to use that list.

`CORS_ALLOWED_ORIGINS` is a comma-separated list of browser frontend origins.
The Print Bar frontend should use
`VITE_PRINTER_API_BASE_URL=http://192.168.100.99:8000` or the appropriate hostname.

### Cross-site dev origins and session cookies

The session cookie is set with `SameSite=Lax`.  On plain HTTP, a browser
considers two URLs cross-site when their registered host differs — so
`http://localhost:5173` (frontend) and `http://192.168.100.99:8000` (backend)
are cross-site even though both are on the LAN.  The browser will **not** send
the session cookie on cross-site XHR/fetch requests, which means every
auth-protected endpoint returns `401` if a CORS override allows that origin.
Adding localhost to the allowlist alone does not make the cookie cross-site.

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
${BACKEND_HOST:-192.168.100.99}:${BACKEND_PORT:-8000}:8000
```

See `DEPLOYMENT_DOCKER.md` for the full container workflow.

## Office conversion settings

| Variable | Default | Purpose |
| --- | --- | --- |
| `INSTALL_OFFICE` | `true` | Docker build argument; install LibreOffice and fonts. |
| `OFFICE_ENABLED` | `true` | Enable conversion when runtime dependencies are available. |
| `OFFICE_TIMEOUT_SECONDS` | `90` | Wall-clock and CPU limit for a conversion. |
| `OFFICE_MEMORY_MB` | `1536` | Per-process virtual address-space limit. |
| `OFFICE_MAX_PAGES` | `500` | Maximum pages in the generated PDF. |

All numeric limits must be positive. `/capabilities` reports effective runtime
availability and upload/page/time limits. Changes to conversion settings require
a backend restart. See [Office integration](OFFICE_AND_APP_INTEGRATION.md).
